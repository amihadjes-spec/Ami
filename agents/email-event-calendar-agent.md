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
2. **Fetch New Emails:** Query `search_threads` for `-label:Ami/Event-Checked -in:draft {-in:sent to:ami.hadjes@gmail.com}` (up to 50 per run). If empty, exit silently.
   > **Do NOT scope to `in:inbox`.** Most incoming mail for this account is filtered straight past the inbox into labeled folders (per-sender filters with "Skip Inbox"), so an inbox-scoped query silently returns 0 even when new mail is arriving. The query above searches the whole mailbox and excludes outgoing mail sent to other people, but still includes self-sent reminder emails (`to:ami.hadjes@gmail.com`), since the user sometimes emails himself reminders.
3. **Analyze & Process:** Fetch full content (`get_thread`, FULL_CONTENT) per thread. Look for explicit dates/times (invitations, confirmations, Zoom/Meet links, `.ics` files). Ignore newsletters, generic ads, or dateless receipts. Three categories count as a valid, proposable item:
   * **Calendar Events:** Specific invitations, tickets, meetings, or Zoom/Meet links with explicit dates/times.
   * **Reminders/Tasks:** Action items, follow-ups, deadlines, or explicit requests to remember/do something ("remind me", "action required", "תזכורת", "לטיפולך") that belong in the calendar or task log. Ignore newsletters/ads.
   * **Renewals (insurance/subscriptions/licenses):** Emails confirming a periodic renewal (e.g., "ממשיכים יחד לעוד שנה", policy/subscription renewal confirmations) even with no explicit future deadline stated — treat as a Reminder/Task. Infer the follow-up date as (email date + 1 year − 2 weeks), so there's time to review or shop around before the next renewal.
   * **A date/time must come from readable message text, not inference.** Only extract an event from the plaintext/HTML body actually returned by `get_thread`/`get_message`. Never infer, guess, or hallucinate a date/time from context clues such as a sender's job title, department name, organizational boilerplate, or the mere presence of an attachment (PDF, image, `.docx`) whose content is not itself returned as readable text. If the substantive content is locked inside an attachment the tool doesn't surface as text, treat the thread as dateless — do not propose an event based on the surrounding email alone.
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