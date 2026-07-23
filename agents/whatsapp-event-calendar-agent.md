<!-- Source of truth. Copy verbatim into the routine's config after any edit. -->

# Agent: WhatsApp Event Detection + Calendar-Add Suggestion

## Purpose

Scan incoming WhatsApp messages (via WAHA — a self-hosted WhatsApp HTTP API running in a local Docker container) to detect messages describing a concrete event/meeting (date + time, location, phrases like "let's meet," "event," "birthday," etc.), check for conflicts with the existing calendar, and **suggest** adding the event to the calendar — never creating it automatically without explicit user approval. Architecturally mirrors [`agents/email-event-calendar-agent.md`](email-event-calendar-agent.md), with WhatsApp replacing Gmail as the "inbox" and a state file replacing Gmail labels (WAHA has no equivalent tagging mechanism).

## Trigger

WAHA supports webhooks/websockets, but these require a persistently listening process, which conflicts with the current one-shot cron model (a session opens, runs once, and closes). Decision: **pull-based polling**, not push/webhooks. The agent runs on a scheduled trigger (cron, hourly), each run being a fresh, stateless session. All real state lives in `state/whatsapp-event-agent-state.json` in the repo (see "State Storage"), not in conversation memory.

## Data Source / Polling Loop

WAHA has no single global message queue with receive/delete semantics (unlike Green-API) — it's a plain REST API for enumerating chats and messages, with no built-in "what's new since last time." Tracking "what's new" is therefore the agent's responsibility, via a **per-chat watermark (timestamp)** stored in state (`whatsapp_watermarks`, see "State Storage").

All orchestration against the WAHA REST API (pagination, watermark computation, initial filtering) happens in a maintained, version-controlled Node script: `scripts/waha-poll.mjs` (not rewritten each run).

At the start of every run:

1. `git pull` the operational branch to get the latest state (the previous run happened in a different container).
2. Load `state/whatsapp-event-agent-state.json` (create with an empty structure if missing). If the file exists but lacks `whatsapp_watermarks`, treat it as `{}` via default-merge, not as an error.
3. Run `node scripts/waha-poll.mjs` (env vars `WAHA_URL`, `WAHA_API_KEY`, `WAHA_SESSION`; current `whatsapp_watermarks` passed as input). The script:
   a. Checks session status (`GET /api/sessions`).
   b. If unreachable (container/network) → returns an error.
   c. If reachable but `status != "WORKING"` → returns `ok:false` with `sessionStatus`.
   d. If `status == "WORKING"` → builds the chat list to scan primarily by reading chat IDs directly from WAHA's own NOWEB persistent store (a local SQLite file), falling back to `GET /api/{session}/chats/overview` if that read fails for any reason (see "Critical fix — chat-list enumeration reads WAHA's NOWEB store directly" for why). For each chat: if no watermark exists, fetch from `now - 24h` (initial backfill); otherwise from `watermark + 1` (delta only). Fetches messages with pagination (`GET /api/{session}/chats/{chatId}/messages`, `filter.timestamp.gte`), filtering out `fromMe == true` and empty `body` — **except in the self-chat**, where `fromMe` is always `true` even for genuine reply messages typed on the phone, so a self-chat message with a valid `replyTo.id` is not discarded (see "Critical fix — fromMe blocks replies in the self-chat"). **Includes media captions**: WAHA already returns the readable caption text in `body` for `imageMessage`/`videoMessage` (not raw image bytes), so filtering on `body` alone is sufficient — no need to check `hasMedia`. **The media file itself (`media.url`) is never downloaded or stored** — only the caption text.
   **Computes a new watermark per chat, with different rules for a fresh backfill versus a regular delta fetch** (see "Critical fix — watermark must not jump to 'now' on an empty delta fetch" for why this distinction exists):
      * If the chat had no prior watermark (this was a `now - 24h` initial backfill): if messages were fetched, watermark = max timestamp among them (including filtered ones); if truly none were fetched in the full 24h window, watermark = `now` — safe here, since the entire window was just proven empty, and this avoids repeating an expensive 24h re-scan every run for a chat with no history.
      * If the chat already had a watermark (this was a `watermark + 1` delta fetch): if messages were fetched, watermark = max timestamp among them (including filtered ones), exactly as before. **If zero messages were fetched, leave the watermark unchanged** — never advance it to `now`. The next run's delta window then simply extends further forward from the same starting point, so any message that actually existed in that window (missed due to a transient API/sync issue, not because nothing was there) stays reachable and gets retried until it's actually seen, rather than being silently skipped forever.
   e. Returns a single JSON object to stdout: `{ok, sessionStatus, chatsScanned, candidateMessages[], updatedWatermarks{}}`.
4. Parse the output:
   a. Communication failure / invalid output → report issue (`issue_key: waha-unreachable`, see "Duplicate Issue Reporting Prevention"), skip event detection this run.
   b. `ok:false` → report issue (`issue_key: waha-session-not-working:{sessionStatus}`), skip event detection this run.
   c. `ok:true` → clear any open `waha-unreachable`/`waha-session-not-working:*` entries from `issue_reported` (the run succeeded), process each item in `candidateMessages[]` per "Event Detection" and "Approval Flow" (as plain text — `chat_id` is WAHA's `from`, `chat_name` is the chat's `name` from `/chats/overview`, falling back to the chat ID itself if `name` hasn't synced yet), and merge `updatedWatermarks` into `whatsapp_watermarks`. If `paginationCapHits[]` is non-empty, report a separate issue per chat (`issue_key: waha-chat-pagination-cap-hit:{chatId}`) — a rare case of one chat exceeding the pagination limit inside `waha-poll.mjs`.
5. Proceed to "Quiet Hours" to decide on sending notifications, then save and `git commit` + `push` the state.

## Event Detection

For each extracted message, check for signals of a concrete event: date and/or time, location, phrases like "let's meet," "event," "birthday," "party," "meeting," Zoom/Meet links, "save the date." Ignore casual chatter with no concrete time.

For each detected event, extract: title, start/end (default duration of one hour if no end time given), location (if any), timezone (default `Asia/Jerusalem`). Check for conflicts against the primary calendar (`list_events` over the event's time range) and flag overlaps. Always pass `singleEvents=True` and `orderBy='startTime'` on every `list_events` call, to correctly expand recurring events.

**Date-only candidates (no extractable time)**: some messages give a clear date (or several) with no clock time anywhere in the readable text — e.g. a multi-item community events roundup that points to an external page for exact times. Don't drop these silently just because there's no time to extract. Propose them as an **all-day placeholder** instead of a timed event: set `event.all_day: true` and `event.date` (a bare `YYYY-MM-DD`, no `start`/`end`), and include a note in the proposal and the eventual calendar description marking it explicitly as `"שעה טרם ידועה — יעודכן בהמשך"` so both Ami and later runs can tell it's a placeholder awaiting a real time, not a confirmed detail. Everything else about detection (title, location, dedup) applies to a date-only candidate exactly as it does to a timed one — a single message can and should yield **multiple separate date-only candidates** if it lists multiple distinct dated items, one per item, not one merged entry (see "Known gap this closes (2026-07-16, part 2)" below).

**Deduplication against pending proposals**: before adding a new suggestion to `pending_events`, check whether an identical one already exists (same `chat_id`, similar title/time). If so, don't add a duplicate; if the new message adds/refines details (e.g., a time that was previously unknown), update the existing suggestion instead of creating a new one.

**Deduplication against events already on the calendar**: matching a candidate to an existing calendar event by title and date alone is not sufficient grounds to silently drop it — title/date similarity only tells you it's *probably the same occasion*, not that every detail already agrees. Before treating a candidate as "already represented, no action needed":
* Compare the candidate's full extracted details (start/end time, location) against the existing event's — not just the title/date.
* If the time range and location both match (or the candidate adds no new information), skip silently, as before.
* If the candidate's time range disagrees with the existing event (e.g. a longer or shorter duration than what's currently on the calendar), don't stay silent — this is a **correction candidate**, not a duplicate and not a fresh event. Send it as a real WhatsApp proposal (`kind: proposal`, reply required — correcting something already on the calendar still needs explicit approval, same as creating one, per "Core Principles"). Only after approval, apply it with `update_event` against the *existing* event's ID (never `create_event` — this must not produce a second event) and then report the outcome with a one-way `kind: modified` notification. A correction-type `pending_events` entry therefore needs `action: "update"` and `target_event_id` set to the existing event's ID (see "State Storage"), instead of the implicit `action: "create"` a normal new-event proposal has.
* **Upgrading an all-day placeholder is a correction, not a new event**: if the existing calendar event is itself an all-day placeholder (`all_day: true`, created via the "Date-only candidates" rule above) and a new candidate for the same title/date now carries a real time, that's the expected follow-up this feature exists for. Handle it exactly like any other correction candidate above — propose (`kind: proposal`) → on approval, `update_event` sets real `start`/`end`, clears `all_day`, and drops the "שעה טרם ידועה" note → report `kind: modified`. Never treat the arrival of a time as grounds for a brand-new, second event alongside the placeholder.
* If the candidate describes a genuinely different location or slot under the same or a similar title (e.g. two separate staffing shifts for the same campaign at two different sites), treat it as a **separate, additional candidate** — same title does not mean same event when the location differs.
* Never let a partial title/date match justify silence when the underlying time or place data actually disagrees — surface the discrepancy instead of assuming the existing record is authoritative.

**Known gap this closes (2026-07-16)**: a Democrats-party volunteer call-out for two staffing shifts (Beer Sheva הגרנד קניון and Lahavim מול הסופר, both 17.7 10:00-12:00) was matched purely on title+date against an existing "דוכן מפלגת הדמוקרטים" calendar event (created via the Gmail bridge: 17.7 10:00-**11:00**, Lahavim only) and silently dropped — even though the end time was wrong (11:00 vs. the broadcast's 12:00) and the Beer Sheva shift wasn't on the calendar at all. This section's rules are what should have caught both discrepancies instead of treating the whole broadcast as already handled.

**Known gap this closes (2026-07-16, part 2)**: a Lahavim municipal summer-events roundup message (three items: שבת בפארק 18.7, פותחים את הזוליתא + a same-evening World Cup screening mention 19.7, סרט על הדשא 21.7) was correctly recognized as containing three distinct dated items, but none had a clock time in the text, and the spec had no rule for that case — so all three were silently dropped instead of proposed as placeholders. Separately, prior to this fix there was also no rule for what should happen when a later message supplies the missing time for such a placeholder — that gap is what the "Upgrading an all-day placeholder" bullet above closes.

Do not disqualify a message just because it reads as promotional or commercial (e.g. a price, a phone number for registration, a sign-up form link, phrasing like "quick sign-up" or "limited spots"). All monitored groups are ones the user has explicitly opted into, so a group activity/workshop/course/trip announcement with a concrete date, time, and location (e.g. a guided off-road driving session, a group hike, a paid class) is just as valid a candidate as a personal invite — evaluate purely on whether date/time/location signals are present, not on whether the message "sounds like an ad." When extracting the title for such messages, use the core activity/headline (e.g. "הדרכת נהיגת שטח קבוצתית — יער בן שמן"), not the full marketing copy.

## Gmail-Sourced Proposals (Bridge to Email Agent)

**Why this exists**: [`agents/email-event-calendar-agent.md`](email-event-calendar-agent.md) runs as a cloud-hosted routine and has no network path to WAHA — WAHA only listens on `localhost:3000` on this machine, and no tunnel/webhook exposes it externally (confirmed by inspecting the WAHA container's port bindings and checking for any tunnel process/service on this machine — none exists). So when the email agent detects an event and labels a Gmail thread `Ami/Event-Pending`, it has no way to actually deliver a WhatsApp confirmation itself. This agent — which already runs locally with a working WAHA connection — relays those proposals on its behalf, reusing the exact same `pending_events` / `notify_queued` / Approval Flow machinery as natively-detected WhatsApp events.

**History**: a first version of this bridge was added 2026-07-13 (commits `0b098ba`, `55583a9`, `9c60744`, `91a2b15`) and sent one real proposal ("פגישה בקפה גרג") that was approved and created a real calendar event. It was removed the next morning (`37e0948`) because it let **two independent channels** — a reply in Gmail (watched by the email agent) and a reply in WhatsApp (watched by this agent) — both resolve the *same* `Ami/Event-Pending` thread, risking a duplicate calendar event or a race between the two. This version closes that gap with an idempotent "already notified" marker and a cross-channel handoff label (below) instead of relying on both sides just hoping the other doesn't also act.

**Detection** (at the start of every Event Detection phase, alongside the normal WAHA scan):
1. Query Gmail for threads labeled `Ami/Event-Pending` but **not** `Ami/Event-Notified-WhatsApp` — the second label is the idempotency guard that stops the same thread being proposed again on a later run.
2. For each such thread, extract the event details, subject, and the thread/message ID; construct the email link `https://mail.google.com/mail/u/0/#inbox/<ID>`.
3. Add a `pending_events` entry exactly as for a native detection (see "State Storage"), with `source: "gmail"`, `gmail_thread_id: "<ID>"`, and `gmail_link` set, `chat_id` set to the self-chat's own ID (delivery and replies both happen there), status `awaiting_notify`.
4. Queue the proposal via the normal "Detection & Notification Queue" flow below — **do not** send it immediately outside that flow; it must go through the same quiet-hours batching as everything else.

**Message template** (required verbatim for `source: "gmail"` proposals): originally used "מאשר"/"דוחה" (confirmed working end-to-end 2026-07-13), unified to "כן"/"לא" on 2026-07-15 to match the native WhatsApp-detected proposals' wording — both response words are still accepted (see "User Response Processing" below).
```
🤖 *הודעה אוטומטית מסוכן המיילים*

*אירוע:* <EVENT_DETAILS>
*זמן:* <EVENT_TIME>

🔗 *לינק למייל:* <gmail_link>

---
💡 *להרשמה ביומן:* הגב *'כן'*
❌ *להתעלמות/מחיקה:* הגב *'לא'*
```

**Idempotency marker**: immediately after a `source: "gmail"` proposal is successfully sent via `POST /api/sendText` (in "Quiet Hours & Notification Dispatch" below), apply the Gmail label `Ami/Event-Notified-WhatsApp` to that thread, in the same step as saving `notification_message_id`. Never apply this label before a send actually succeeds — a failed send must be retried on the next run, not silently treated as sent.

**Resolution and cross-channel handoff** (extends "User Response Processing" below, for `pending_events` entries with `source: "gmail"`):
* **Approved**: before creating the event, re-fetch the Gmail thread's *current* labels. If `Ami/Event-Pending` is already gone (the email agent's own Gmail-reply channel resolved it first), **do not create the event** — remove the local `pending_events` entry and queue a `duplicate` notification instead of `created`. Otherwise, run the standard dedup check (see "User Response Processing"), then create the event with a description containing `"מקור: נוצר אוטומטית מתוך אימייל"` and `"קישור למייל: <gmail_link>"`, then update Gmail: remove `Ami/Event-Pending`, add `Ami/Event-Created`. Removing `Ami/Event-Pending` is what makes the email agent's own `label:Ami/Event-Pending` query naturally skip this thread from then on — that's the handoff.
* **Rejected**: remove `Ami/Event-Pending` from the Gmail thread (do not add `Ami/Event-Created`) — matches the email agent's own rejection labeling (leaves `Ami/Event-Checked` + `Ami/Event-Suggested` only). Remove the local `pending_events` entry.
* **Modified**: handled like a native modification (update the local draft, re-check conflicts, re-propose); the eventual approval/rejection still follows the label handoff above.

This agent never reads or acts on a *Gmail-side* reply — that stays the email agent's job. It only ever writes the three labels named above (`Ami/Event-Notified-WhatsApp`, `Ami/Event-Pending` removal, `Ami/Event-Created`), and only on threads it is itself resolving via a WhatsApp reply it received.

## State Storage

`state/whatsapp-event-agent-state.json`:

```json
{
  "pending_events": [
    {
      "id": "uuid-or-stable-hash",
      "chat_id": "972501234567@c.us",
      "chat_name": "taken directly from the `name` field of GET /api/{session}/chats",
      "source_message_id": "WAHA id of the message the event was detected in",
      "notification_message_id": "WAHA id.id of the agent's own proposal message sent to the self-chat (populated after sending; used to match replyTo.id)",
      "source": "whatsapp | gmail (default whatsapp if omitted)",
      "gmail_thread_id": "Gmail thread/message ID — present only when source is gmail",
      "gmail_link": "https://mail.google.com/mail/u/0/#inbox/<gmail_thread_id> — present only when source is gmail",
      "detected_at": "2026-07-06T10:00:00+03:00",
      "action": "create | update (default create if omitted)",
      "target_event_id": "existing calendar event ID — present only when action is update (a correction, incl. an all-day-placeholder time upgrade); update_event is used instead of create_event on approval",
      "event": {
        "title": "...",
        "start": "2026-07-10T18:00:00+03:00",
        "end": "2026-07-10T19:00:00+03:00",
        "location": "...",
        "timezone": "Asia/Jerusalem",
        "all_day": "true — only present on a date-only placeholder; mutually exclusive with start/end, which are replaced by a bare `date` (YYYY-MM-DD) instead",
        "date": "2026-07-10 — present only when all_day is true"
      },
      "has_conflict": false,
      "status": "awaiting_notify | notified | awaiting_response"
    }
  ],
  "notify_queued": [
    {
      "kind": "proposal | created | modified | rejected | duplicate | error | run_summary",
      "pending_event_id": "id from pending_events, if relevant",
      "text": "the text to send to the user",
      "queued_at": "2026-07-06T23:10:00+03:00"
    }
  ],
  "issue_reported": [
    {
      "issue_key": "stable identifier for the issue type, e.g. waha-session-not-working:FAILED",
      "first_reported_at": "2026-07-06T09:00:00+03:00",
      "description": "..."
    }
  ],
  "whatsapp_watermarks": {
    "972501234567@c.us": 1751980800
  }
}
## Approval Flow

The agent strictly operates on a **suggest-and-confirm** loop. It never writes to the calendar automatically.

### 1. Detection & Notification Queue
When a candidate event is detected:
* If no conflict is found, queue a `proposal` notification.
* If a conflict is found, append a conflict warning directly to the proposal text (e.g., "⚠️ Note: This conflicts with 'Lunch with Avi' at 13:00") and queue it.
* Set status to `awaiting_notify`.

### 2. Quiet Hours & Notification Dispatch
To avoid buzzing the phone at night, notifications are buffered through `notify_queued` and dispatched based on local time (`Asia/Jerusalem`):
* **Quiet Hours**: 22:00 to 08:00.
* **Scheduled run itself is also skipped in this window**: `run-whatsapp-agent.ps1` checks the local Israel time before doing anything else (before `docker start`, `git pull`, or invoking `claude -p`) and exits immediately if inside 22:00–08:00, logging a "Skipped: quiet hours" line. This is separate from and in addition to the notification buffering described above — it avoids spinning up Docker/git/the agent process at all during quiet hours, not just delaying message delivery.
* If current time is inside quiet hours: Leave items in `notify_queued`.
* If current time is outside quiet hours: Send all queued messages to the self-chat (self-chat ID derived per "Critical fix — self-chat identification under NOWEB", then `POST /api/{session}/sendText`).
* For each successfully sent proposal, update its status in `pending_events` to `awaiting_response` and store the sent message's ID in `notification_message_id`. This ID is critical for matching future replies. For `source: "gmail"` entries specifically, also apply the `Ami/Event-Notified-WhatsApp` label to the Gmail thread at this point (see "Gmail-Sourced Proposals") — only after the send actually succeeds.
* **Two delivery channels, by `kind`**: `proposal` (needs a reply) goes out as a real WhatsApp message via `POST /api/sendText`, exactly as above — it's the only way to later capture a `replyTo.id`. Every other kind (`created | modified | rejected | duplicate | error | run_summary`) goes via `ntfy` instead (`node scripts/ntfy-notify.mjs --title ... --message ...`, topic from `NTFY_TOPIC`) — these are one-way informational pushes, no reply is ever expected on them.
* **ntfy messages must be short pointers, not content copies**: the full event details (title, time, location, conflict warning, link) already went out via the WhatsApp proposal itself — an `ntfy` message reporting on it should identify *which* proposal/result it's about (e.g. a short title + date), not restate everything. This isn't just tidiness — see "Critical fix — ntfy messages must stay short" below for a real size limit this avoids.

### 3. User Response Processing (Self-Chat)
During the polling phase, when scanning messages from the **self-chat**:
* Look for messages that are replies to a proposal (using `replyTo.id`).
* Match the `replyTo.id` against the `notification_message_id` of items in `pending_events` with status `awaiting_response`.
* **Multiple replies to the same proposal**: if more than one candidate message in this run matches the same `notification_message_id`, only the reply with the highest `timestamp` (chronologically last) determines the outcome — earlier ones are not separately processed or treated as an error, they're simply superseded. This is a deliberate, standing rule, not an implementation detail — see "Critical fix — last-reply-wins restored" below for why it's written this explicitly.
* Parse the user's reply text (using the winning reply only, per the rule above, when more than one applies):
  * **Approved** (e.g., "כן", "yes", "אשר", "מאשר", "יאללה", 👍 — "מאשר" kept as a synonym since it was the wording used in the Gmail-sourced template before this was unified to "כן"/"לא"): Before creating anything, run a final `list_events` check over the event's time range (`fullText` search on the title; for `source: "gmail"` entries, search for the `gmail_link`/thread ID instead, since it will appear verbatim in that event's description) to confirm it wasn't already created by another run or the other channel. If it already exists, skip creation, remove the event from `pending_events`, and queue a `duplicate` notification. Otherwise call Google Calendar API `insert_event` to create the event. Send a success confirmation back to the self-chat, remove the event from `pending_events`, and queue a `created` notification. For `source: "gmail"` entries, follow the additional cross-channel handoff in "Gmail-Sourced Proposals" (re-check `Ami/Event-Pending` is still present before creating, then swap it for `Ami/Event-Created`).
  * **Rejected** (e.g., "לא", "no", "דוחה", "ביטול", "אל תוסיף", 👎 — "דוחה" kept as a synonym for the same reason as "מאשר" above): Update status to `rejected`, clean it up from `pending_events`, and optionally acknowledge. For `source: "gmail"` entries, also remove `Ami/Event-Pending` from the Gmail thread (see "Gmail-Sourced Proposals").
  * **Modified** (e.g., "תשנה ל-18:00", "change to 6pm"): Extract the new parameters, update the draft in `pending_events`, check conflicts again, and send an updated proposal.

## Duplicate Issue Reporting Prevention

To prevent spamming the user with repeated notifications about the same infrastructure failure (like WAHA being offline for hours):
* Maintain `issue_reported` in the state file.
* Before reporting an operational issue (unreachable API, session down, etc.), check if `issue_reported` already contains a matching `issue_key`.
* If it exists: **Do not send another notification**.
* If it does not exist: Send the alert immediately to the self-chat (bypassing quiet hours if it's a critical system error) and add it to `issue_reported` with the current timestamp.
* When a run succeeds, clear all matching issue keys from `issue_reported`.

## Critical fix — self-chat identification under NOWEB (2026-07-15)

**Root cause**: the WEBJS engine's `GET /api/{session}/chats` returned `name` and `isGroup` on every chat, so the self-chat could be found by scanning for `!isGroup && name === session.me.pushName`. After migrating to the NOWEB engine (WEBJS's `GET /chats` started crashing with an internal Puppeteer/WhatsApp-Web-redesign incompatibility — see WAHA GitHub issues #2159/#2160, unresolved upstream as of this writing), the same call returns neither field under NOWEB at all. `GET /chats/overview` does return `name`, but it's `null` until WAHA's background history sync catches up for that chat, and it has no `isGroup` field on any engine.

**Fix**: `waha-poll.mjs` now fetches the chat list from `GET /api/{session}/chats/overview` instead of `GET /chats`, with one 5-second retry if it comes back with fewer than 10 chats (a sync-still-catching-up signal, not a failure — proceeds with whatever's available after that rather than blocking the run). Where `isGroup` is still needed, it's derived from the chat ID suffix (`@g.us` = group) rather than a field that may not exist.

More importantly, **self-chat identification no longer scans the chat list at all**: the self-chat's ID is the account's own JID, read directly off `session.me` from `GET /api/sessions` (`session.me.lid`, falling back to `session.me.id`) — confirmed empirically against a live NOWEB session, where `session.me.lid` resolved directly to real self-chat history. This is immediate and independent of chat-list sync state, unlike the old name-matching approach. The name+isGroup heuristic is kept only as a last-resort fallback if `session.me` is ever unexpectedly missing both fields. Because the self-chat can still be absent from a not-yet-fully-synced `/chats/overview` list, it's explicitly added to the scan set if missing — otherwise approval/rejection replies there would silently go unseen.

## Critical fix — chat-list enumeration reads WAHA's NOWEB store directly (2026-07-15)

**Root cause**: `GET /chats` and `GET /chats/overview` under the NOWEB engine both read from an in-memory cache (`NowebInMemoryStore`) that does not get hydrated from WAHA's own persisted history. Confirmed directly, not assumed: after a full, successful background sync (verified via `docker logs` reaching hundreds of `"store sync - 'N' synced chats"` lines), those two endpoints stayed stuck reporting single digits, while the actual persisted data — `.waha-sessions/noweb/{session}/store.sqlite3`, a SQLite file on the same volume — held 350+ chats, checked by querying it directly. `GET /chats/{chatId}/messages` was separately confirmed to work correctly for chat IDs entirely missing from the broken list endpoints, so **only enumeration is affected, not per-chat message fetching**.

This isn't a setup mistake worth re-pairing over: WAHA's own docs warn that changing `config.noweb.store` *after* the session's first connection can corrupt this exact kind of state, so a clean re-pair with the store enabled *before* first connection was tried first — and reproduced the identical symptom (persisted store fully synced, in-memory list endpoints still stuck near-empty). It's a genuine bug in WAHA `2026.6.2`'s NOWEB engine, independent of setup order. No matching upstream GitHub issue was found as of this writing.

**Fix**: `scripts/waha-poll.mjs` reads the chat ID list directly from `store.sqlite3` (read-only, via Node's built-in `node:sqlite` — WAHA itself is the only writer) instead of trusting `GET /chats/overview` for enumeration. `GET /chats/overview` is still called to layer in whatever `name` values it has managed to resolve (falling back to the chat ID itself where it hasn't, exactly as before) — only the authoritative *list of which chats exist* moved to the direct store read. If the SQLite read fails for any reason (wrong path, missing/locked file, a schema change in a future WAHA version), it fails soft and falls back fully to the pre-existing `/chats/overview`-only behavior rather than crashing the run — self-healing if WAHA ever fixes the underlying bug, since at that point the (now-working) API path and this workaround would return the same data anyway.

Verified against the live session after the fix: `chatsScanned` went from 8–9 to 359, matching the SQLite store's own count, with zero errors and zero pagination-cap hits across all chats in a full manual run.

## Critical fix — last-reply-wins restored (2026-07-15)

**Original decision (2026-07-11, real-world incident)**: when multiple *conflicting* replies arrive for the same open proposal, the chronologically latest reply wins. This was first tested by an actual failure: applying it literally, a proposal ("Talk for parents of teens", Lehavim youth club) was approved and created from the latest of three conflicting replies ("no", "No", then "yes" — the "yes" turned out to be a stray/test message, not a real change of mind). The user manually deleted the resulting event and, on follow-up, explicitly re-confirmed the rule itself should stay: rather than adding a disambiguation round-trip (considered and rejected at the time — extra state, extra friction, breaks the single-step approval model), the standing decision was to keep last-reply-wins as-is, accepting that a stray message could occasionally cause an unwanted event, over always adding a confirmation step to every single approval.

**The rule itself went missing from this document** in a later condensing rewrite (`37e0948`, 2026-07-14) — an accidental content loss, not a reversal of the decision. This surfaced again for real on 2026-07-15: a "לא" (no) reply was recorded against a WhatsApp-detected proposal ("הפגנה בכיכר רבין", 18/7), and the user then explicitly asked to send a "כן" (yes) as a deliberate, confirmed change of mind — at which point it became clear the current spec had no written rule at all for what a run should do on encountering both replies, since the rule had quietly disappeared along with the rest of that rewrite's cuts.

**Fix**: restored the rule explicitly in "User Response Processing" above (§3) — highest-`timestamp` reply among all candidates matching the same `notification_message_id` wins; earlier ones are superseded, not separately processed or flagged as errors. Unlike 2026-07-11, this is written down as a first-class, permanent rule rather than something a stateless session has to reconstruct or lose again.

## Critical fix — ntfy messages must stay short (2026-07-15)

**Trigger**: the user saw an `ntfy` push notification that looked cut off ("סריקת..."). Investigated properly rather than guessing:
- `scripts/ntfy-notify.mjs` does no truncation of its own — `body: args.message` is sent exactly as given.
- ntfy.sh's real limit was confirmed empirically (not just from docs): a message over 4,096 bytes isn't truncated — the server silently converts it into a **file attachment** instead of an inline message. Verified by publishing an 8,276-byte test message and downloading the resulting attachment: the full content was intact (nothing lost), but the notification itself changed to a generic `"You received a file: attachment.txt"` line with none of the actual content visible until separately opened.
- A single run's summary text measured well under the limit (~1,080 bytes, ~26%), so the specific notification the user saw was almost certainly just normal iOS notification-banner preview truncation — the full text was there all along. But **consolidated notifications** (several `notify_queued` items batched into one push, e.g. after quiet hours) stack multiple summaries together and could realistically approach or cross 4,096 bytes, at which point the notification would silently degrade into that unhelpful generic attachment line.

**Decision**: rather than adding logic to measure message length and split oversized batches, the simpler fix is to keep `ntfy` messages inherently short in the first place — see the new rule in "Quiet Hours & Notification Dispatch" above (§2). Since `ntfy` is one-way and informational only, and the full content for anything proposal-related already went out over WhatsApp, there's no reason for an `ntfy` message to ever approach the byte limit at all if it's just naming what happened rather than restating it.

## Critical fix — fromMe blocks replies in the self-chat

By default, WAHA polling filters out `fromMe == true` to avoid processing the agent's own messages. However, in the self-chat, **every message sent by the user from their phone also has `fromMe == true`.** The self-chat is also the only channel through which the *user* can hand the agent a brand-new event candidate directly (rather than it being found in a group), so it needs to support two distinct cases, not just one.

**Rule**: When scanning the self-chat, do NOT discard a message where `fromMe == true` if either: (a) it has a valid `replyTo.id` pointing to an active proposal — treated as a genuine user approval/rejection reply, as before; or (b) its WAHA message id is **not** found among the `notification_message_id`s the agent itself has sent (tracked in state) — treated as a fresh, user-authored message (e.g. an event pasted or typed directly into the self-chat) and run through normal event classification exactly like a message from any other chat. Only discard a self-chat `fromMe == true` message when it matches neither condition — i.e. when it *is* one of the agent's own previously-sent outgoing messages, which would otherwise be re-processed as a new candidate every run.

*(Fixed 2026-07-22: the original rule only kept condition (a), so any message the user typed or pasted fresh into the self-chat — not as a reply to an existing proposal — had no `replyTo.id` and was silently discarded before ever reaching event classification. Confirmed via a real case: an appointment-reminder text pasted into the self-chat was never picked up. Don't revert to "keep only if replyTo.id is valid" — that reintroduces this gap.)*

## Critical fix — watermark must not jump to "now" on an empty delta fetch (2026-07-16)

**Root cause, confirmed directly against the state file's history rather than assumed**: a "כן" reply to the "גמר המונדיאל - צפייה בפאב החברתי להבים" proposal was sent at 17:09:27, well inside the self-chat's delta fetch window (16:27:45 → run time) of the run that executed at 18:26. That run's own narrative reported "no matching reply seen," and the watermark it saved for the self-chat was 18:24:31 — the run's own approximate start time, not the reply's actual timestamp. That's the signature of the old rule ("if no messages were fetched, use `now`") firing on what was very likely a transient empty result from that one fetch call, not a chat that was genuinely quiet. Once the watermark advanced past 17:09:27, the reply became permanently unreachable — every later run's delta window started after it, so three consecutive runs (18:26, 20:23, 21:28) each correctly reported "no reply seen," none of them wrongly, because the message was already lost by the time each of them polled. A self-chat-ID mix-up was checked and ruled out: the alternate self-chat identifier's watermark never moved during this whole window, confirming the correct ID was used consistently throughout.

**Fix**: the watermark-advancement rule now only uses `now` as a fallback for a brand-new chat's initial 24h backfill coming back genuinely empty (see "Data Source / Polling Loop" step 3.d above) — safe, since the full window was just proven empty. For a **regular delta fetch on a chat that already had a watermark**, an empty result no longer advances the watermark at all; it's left unchanged, so the next run's window simply grows forward from the same starting point and will retry — and eventually see — anything that was missed due to a transient issue, rather than silently losing it the moment the fallback fires.

**This does not repair proposals already lost to the old behavior** — those need to be resolved directly from the confirmed WhatsApp reply once found (as was done for the "גמר המונדיאל" proposal above, and for the `notification_message_id: null` incident on 2026-07-15), since the message itself remains genuinely un-repollable no matter how the watermark is rolled back or adjusted afterward.
