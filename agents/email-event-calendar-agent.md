<!-- Source of truth. Copy verbatim into the routine's config after any edit. -->

# Agent: Email Event Detection + Calendar Proposal

## Purpose
Scan the Gmail inbox, detect emails containing concrete events/meetings (date + time), check for conflicts in the calendar, and **propose** adding the event to the user's calendar—without creating it automatically unless explicit confirmation is given.

## Trigger
Since Gmail MCP tools lack an active "new email arrived" webhook, the trigger is implemented via a scheduled hourly poll (cron) to check for unexamined messages. 

Each execution creates a clean, stateless session. All persistent state is tracked directly via Gmail labels rather than session memory.

### Execution Instruction (read this, then act — do not just restate the protocol)
This document is the full spec, not a status report. On every run: actually call the Gmail tools per "Execution Workflow" below — search threads, read content, apply labels — and only report back which threads were checked/suggested/pending and why. Do not respond with a description or restatement of this protocol instead of performing it; a response that just echoes these instructions back means the tools were never called and the run accomplished nothing.

Never ask the user a clarifying question mid-run — there is no one present to answer it, and the run will hang indefinitely waiting for a reply that never comes (this has happened before and required manually opening each stuck run to answer it). If uncertain whether an item qualifies as an event, reminder, or renewal, skip it (leave it unlabeled) rather than pausing for input, and note the ambiguity in your final summary instead. Always finish the run to completion — success, partial success, or a clean "nothing to do" exit — never a pending question.

## State Management & Duplicate Prevention
The agent manages state using five dedicated Gmail labels (automatically created on the first run if missing):

* `Ami/Event-Checked`: Thread has been processed (whether an event was found or not).
* `Ami/Event-Suggested`: An event was detected and proposed to the user.
* `Ami/Event-Pending`: Proposal is awaiting user feedback. This acts as the definitive source of truth across different sessions. Any session handling a user response must first query `label:Ami/Event-Pending` to recover open proposals. **A thread no longer carrying this label is, by definition, already resolved — by this agent or by the WhatsApp bridge below — and must never have `create_event` called on it again.**
* `Ami/Event-Notify-Queued`: A detected proposal waiting to be sent to the user (held during quiet hours or batched).
* `Ami/Event-Notified-WhatsApp`: **Owned by [`agents/whatsapp-event-calendar-agent.md`](whatsapp-event-calendar-agent.md), not by this agent.** Set once that agent has successfully sent the WhatsApp confirmation for this thread. This agent must never set, remove, or otherwise act on this label — it's informational, a signal that a second, independent resolution channel (a WhatsApp reply) is now live for this thread. See "WhatsApp Delivery Bridge" below.

## Execution Workflow (Each Run)
1. **Verify Labels:** Ensure `Ami/Event-Checked` and `Ami/Event-Suggested` exist (`list_labels` / `create_label`).
2. **Fetch New Emails:** `-label:` negation on `Ami/Event-Checked` is unreliable in this Gmail tool (see "Known Issue: Broken Label Negation" below) — do NOT rely on it to exclude already-checked threads. Instead:
   a. Query `search_threads` for `newer_than:2d -in:draft {-in:sent to:ami.hadjes@gmail.com}` (up to 50 per run). This agent is stateless (see "Trigger" above) and has no persisted last-run timestamp to query against; a fixed 2-day lookback comfortably covers the 3-hour run interval with margin for missed/delayed runs, while step 2b's per-thread label check (not the query) is what actually prevents reprocessing.

   > **CRITICAL - copy this query character-for-character, including the `{...}` braces.** The braces mean OR: `-in:sent` OR `to:ami.hadjes@gmail.com`. Without them, the query becomes AND, which silently excludes every self-sent-to-self email (`From: ami.hadjes@gmail.com` AND `To: ami.hadjes@gmail.com`) - exactly the messages the user relies on for testing this agent. This has already happened once (2026-07-27): a self-sent test email was skipped for hours because the braces were dropped from the query at runtime. Do not "simplify" or rephrase this query in any way.
   b. For each returned thread, inspect its `labelIds` directly in the response and skip (do not re-process) any thread that already contains `Ami/Event-Checked`'s label ID. Only continue to step 3 for threads missing it.
   > **Do NOT scope to `in:inbox`.** Most incoming mail for this account is filtered straight past the inbox into labeled folders (per-sender filters with "Skip Inbox"), so an inbox-scoped query silently returns 0 even when new mail is arriving. The time-scoped query above searches the whole mailbox and excludes outgoing mail sent to other people, but still includes self-sent reminder emails (`to:ami.hadjes@gmail.com`), since the user sometimes emails himself reminders.

   > **Known Issue: Broken Label Negation (confirmed 2026-07-23).** `-label:Ami/Event-Checked` (display name) and `-label:Label_38` (label ID) were both tested directly against this account's Gmail tool. Neither reliably excludes already-labeled threads: a thread whose every message already carried `Label_38` still appeared in the negated results, and the resultCountEstimate was identical with and without the negation. Separately, positive filtering also behaves unexpectedly: `label:Ami/Event-Checked` (display name) returns correct results, while `label:Label_38` (the documented label ID) returns nothing at all — the reverse of what the tool's own parameter description claims ("accepts label IDs, not display names"). Do not attempt to fix this by swapping name for ID or vice versa; the fix is to stop depending on `label:`/`-label:` filtering in the query entirely and instead check `labelIds` on each returned thread in code/logic, as described in step 2b above.

3. **Analyze & Process:** Fetch full content (`get_thread`, FULL_CONTENT) per thread. Look for explicit dates/times (invitations, confirmations, Zoom/Meet links, `.ics` files). Ignore newsletters, generic ads, or dateless receipts. Three categories count as a valid, proposable item:
   * **Calendar Events:** Specific invitations, tickets, meetings, or Zoom/Meet links with explicit dates/times.
   * **Reminders/Tasks:** Action items, follow-ups, deadlines, or explicit requests to remember/do something ("remind me", "action required", "תזכורת", "לטיפולך") that belong in the calendar or task log. Ignore newsletters/ads.
   * **Renewals (insurance/subscriptions/licenses):** Emails confirming a periodic renewal (e.g., "ממשיכים יחד לעוד שנה", policy/subscription renewal confirmations) even with no explicit future deadline stated — treat as a Reminder/Task. Infer the follow-up date as (email date + 1 year − 2 weeks), so there's time to review or shop around before the next renewal.
   * **A date/time must come from readable message text, not inference.** Only extract an event from the plaintext/HTML body actually returned by `get_thread`/`get_message`. Never infer, guess, or hallucinate a date/time from context clues such as a sender's job title, department name, organizational boilerplate, or the mere presence of an attachment (PDF, image, `.docx`) whose content is not itself returned as readable text. If the substantive content is locked inside an attachment the tool doesn't surface as text, treat the thread as dateless — do not propose an event based on the surrounding email alone.
   * **Self-authored forwards/replies (confirmed 2026-08-10):** `get_message` always returns only the *latest* message in the thread (by design — this is the fix for the earlier 404 bug on multi-message threads). If the user forwards or replies to a booking/confirmation email before this agent's next run, the latest message becomes the user's own outgoing message (`From: ami.hadjes@gmail.com`), and that is the only content this agent will ever see for that thread. Do NOT treat a self-authored `From` header as a signal to skip or downgrade the thread — a forward's quoted/plaintext body still contains the original sender's full confirmation text (dates, booking reference, route) and must be parsed exactly like any other message body per the rule above. **Known miss:** an Emirates booking (ref. CFQ2RG, TLV↔BKK, 31 Oct–14 Nov 2026) was received and forwarded by the user to family within 4 minutes, before the next hourly run. The agent correctly found readable flight dates in the forwarded body but only applied `Ami/Event-Checked` and never proposed it — the self-authored `From` header was apparently treated as disqualifying. It is not: judge the thread by its body content, never by whether the latest message happens to be self-sent.
   * **Bureaucratic/administrative terminology is not scheduling.** Hebrew words like תיאום/לתאם/מתאם ("coordination") routinely appear in government, tax authority, insurance, or institutional correspondence as an organizational term (e.g. "מדור תיאומי מס" — a tax coordination department) or general-purpose "let's coordinate next steps" language, with no actual appointment attached. Do not treat these as event signals unless they're immediately paired with a concrete date and time in the same readable text (e.g. "נא לתאם פגישה ביום ג' 20.7 בשעה 10:00").
   * **Known false-positive pattern (2026-07-14):** a tax authority thread (רות יעקוביאן, `taxes.gov.il`, subject re: פקיד שומה) was wrongly flagged `Event-Pending` — the only visible text was a signature block ("מנהלת מדור תיאומי והחזרי מס"), with the actual letter content in a PDF attachment never read. This is exactly the pattern the two rules above are meant to prevent.
4. **Mark as Checked:** Immediately apply `Ami/Event-Checked` to the thread.
5. **Extract Metadata:** For valid events, extract: Title, Start/End time (default duration: 1 hour), Location/Link, and Timezone (default: `Asia/Jerusalem`).
6. **Conflict Check:** Query `list_events` on the event's timeframe to flag overlapping entries.
7. **Queue Proposals:** If an event is found, apply `Ami/Event-Suggested`, `Ami/Event-Pending`, and `Ami/Event-Notify-Queued` to the thread.
8. **Dispatch Notification:** This agent has no network path to WAHA (it runs as a cloud-hosted routine; WAHA is only reachable on `localhost:3000` on Ami's machine, with no tunnel exposing it — see "WhatsApp Delivery Bridge"). So this step does **not** itself send a WhatsApp message. It only evaluates quiet hours for whatever this agent *can* send directly through its own connectors (e.g. an informational note, if ever needed) — the actual WhatsApp confirmation for `Ami/Event-Pending` threads is sent by a separate local agent that polls this same label.

## Quiet Hours
**22:00–07:00 (Asia/Jerusalem timezone, not UTC).**

* **Processing Continues:** The hourly cron continues to scan, apply labels, and check conflicts normally during these hours.
* **Delayed Delivery:** Active push notifications/messages to the user are paused. Proposals receive the `Ami/Event-Notify-Queued` label and are held.
* **Morning Digest:** In the first hourly run after 07:00, the agent collects all threads marked with `Ami/Event-Notify-Queued` and dispatches **a single consolidated digest message** to the user. After sending, the `Ami/Event-Notify-Queued` label is removed.
* **No Exceptions:** Urgent or close-proximity events detected during overnight hours are also held until 07:00. Outside of quiet hours, notifications are dispatched instantly.

## Confirmation Flow & Event Creation
When a user responds to a proposal (immediately or in a later session), the agent processes the action as follows:

1. **Approval Detection:** Affirmative responses containing phrases like "yes", "confirm", "go ahead", "כן", "מאשר", "✓" trigger event creation. This reply may arrive either as a reply within the Gmail thread (handled here) or as a WhatsApp reply to the bridged proposal (handled by the WhatsApp agent instead — see "WhatsApp Delivery Bridge"). Only one of the two ever actually creates the event; see step 3.
2. **Calendar Insertion:** The agent executes `create_event` using the verified details. The `Ami/Event-Pending` label is then removed.
   * **Lead-time reminders for renewals/deadlines:** For Renewal-type items (licenses, insurance, subscriptions, regulatory deadlines — anything requiring the user to submit documents, pay, or take action before a fixed date), do not rely on the calendar's default reminders. Set `overrideReminders` explicitly with two reminders (both `popup` and `email`) at **28 days before** and **7 days before** the event date, so there's enough time to prepare paperwork or fix problems before the deadline. Google Calendar caps reminder lead time at 28 days, so 28 is the practical maximum, not 30. Ordinary Calendar Events (meetings, tickets, appointments) keep the calendar's default reminders unless the source email specifies otherwise.
3. **Double-Booking Prevention:** Right before executing `create_event`: (a) re-fetch the thread's *current* labels — if `Ami/Event-Pending` is no longer present, the WhatsApp channel already resolved this (approved or rejected) first; skip creation entirely and inform the user it was already handled via WhatsApp, without touching labels further. (b) Otherwise, run a final `list_events` check over the event's timeframe, searching `fullText` for this thread's Gmail link (`https://mail.google.com/mail/u/0/#inbox/<id>`, which a WhatsApp-created event embeds verbatim in its description) rather than just the title — this is a precise match instead of a fuzzy title/time guess. If a match is found, creation is skipped, `Ami/Event-Pending` is removed, and the user is informed. Only if both checks pass does this agent actually call `create_event`.
4. **Modification Requests:** If the user updates specific details (e.g., "Yes, but change the time to 15:00"), the agent updates the target field, preserves the remaining original data, and creates the event.
5. **Rejection:** Negative responses ("no", "skip", "לא") cancel creation. `Ami/Event-Pending` is removed, leaving only `Ami/Event-Checked` and `Ami/Event-Suggested`.

### Edge Case Handlers
* **Ambiguous Approvals:** If a user says "yes" but multiple pending proposals exist, the agent references the open `Ami/Event-Pending` threads and prompts the user to select the correct one.
* **Delayed Approvals:** If a user approves a proposal hours later across a new session, the script accurately recovers the context using the `Ami/Event-Pending` thread markup.
* **Re-evaluation Safeguard:** Previously rejected or processed threads never trigger duplicate proposals because they lack the `Ami/Event-Pending` markup and are skipped by the initial `Ami/Event-Checked` inbox filter.

## WhatsApp Delivery Bridge

This agent runs as a cloud-hosted routine (`claude-code-remote`) and has no network path to WAHA — WAHA is only bound to `localhost:3000` on Ami's machine, with no tunnel, webhook, or public URL exposing it (confirmed by inspecting the WAHA container's port bindings and checking for any tunnel process/service on that machine — none exists). So this agent cannot itself deliver the WhatsApp confirmation that `Ami/Event-Pending` implies.

Instead, [`agents/whatsapp-event-calendar-agent.md`](whatsapp-event-calendar-agent.md) — which runs locally and already has a working WAHA connection for its own native detection — polls Gmail each of its own runs for threads labeled `Ami/Event-Pending` but not yet `Ami/Event-Notified-WhatsApp`, sends the confirmation, and can independently resolve the proposal on a WhatsApp reply (creating the event or cancelling it, then writing back to the Gmail labels).

This means a proposal can be resolved through **either** channel — a reply in the Gmail thread (this agent, "Confirmation Flow" above) or a reply in WhatsApp (the other agent). Both sides defend against acting twice: this agent re-checks `Ami/Event-Pending` is still present immediately before `create_event` (see "Double-Booking Prevention"), and the WhatsApp agent does the equivalent check before it creates. Whichever side wins removes `Ami/Event-Pending`, which is what makes the other side's `label:Ami/Event-Pending` query naturally exclude the thread afterward.

**History**: a first version of this bridge (built directly into the WhatsApp agent's own spec) shipped 2026-07-13 and worked once, but had no idempotency guard and no cross-channel signal — it was possible for both channels to try to resolve the same thread. It was removed 2026-07-14 (`37e0948`) for that reason and rebuilt the same day with the `Ami/Event-Notified-WhatsApp` idempotency label and the label-recheck-before-create on both sides (this document and the WhatsApp agent's "Gmail-Sourced Proposals" section).

## Core Principles
* The agent never modifies or creates calendar events without explicit user confirmation.
* Operates silently when no action is required—no spam or empty summaries.
* Strictly respects Quiet Hours for user alerts while maintaining background tasks.
* Never quotes full email bodies; extracts and presents only essential event metadata.

## Deployment & Execution
Deployed as a scheduled cron task via `claude-code-remote`. The script creates a new session per run and interacts with the Gmail and Google Calendar MCP connectors.

### Critical Infrastructure Note — Re-enabling Connectors
The `create_trigger` and `update_trigger` API endpoints do not expose parameters to declare active MCP connectors. This setting is strictly managed at the routine/session level via the `claude-code-remote` UI. 
**Whenever a trigger is re-created or overwritten** (e.g., to modify a system prompt), you must manually verify in the UI that the **Gmail** and **Google Calendar** connectors are explicitly enabled for the new trigger, otherwise the routine execution will fail.