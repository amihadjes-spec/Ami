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
   d. If `status == "WORKING"` → fetches all chats (`GET /api/{session}/chats`, one call). For each chat: if no watermark exists, fetch from `now - 24h` (initial backfill); otherwise from `watermark + 1` (delta only). Fetches messages with pagination (`GET /api/{session}/chats/{chatId}/messages`, `filter.timestamp.gte`), filtering out `fromMe == true` and empty `body` — **except in the self-chat**, where `fromMe` is always `true` even for genuine reply messages typed on the phone, so a self-chat message with a valid `replyTo.id` is not discarded (see "Critical fix — fromMe blocks replies in the self-chat"). **Includes media captions**: WAHA already returns the readable caption text in `body` for `imageMessage`/`videoMessage` (not raw image bytes), so filtering on `body` alone is sufficient — no need to check `hasMedia`. **The media file itself (`media.url`) is never downloaded or stored** — only the caption text. Computes a new watermark per chat (max timestamp across all fetched messages, including filtered ones; if no messages were fetched, use `now`, so a quiet chat isn't re-scanned 24h back every hour).
   e. Returns a single JSON object to stdout: `{ok, sessionStatus, chatsScanned, candidateMessages[], updatedWatermarks{}}`.
4. Parse the output:
   a. Communication failure / invalid output → report issue (`issue_key: waha-unreachable`, see "Duplicate Issue Reporting Prevention"), skip event detection this run.
   b. `ok:false` → report issue (`issue_key: waha-session-not-working:{sessionStatus}`), skip event detection this run.
   c. `ok:true` → clear any open `waha-unreachable`/`waha-session-not-working:*` entries from `issue_reported` (the run succeeded), process each item in `candidateMessages[]` per "Event Detection" and "Approval Flow" (as plain text — `chat_id` is WAHA's `from`, `chat_name` is the chat's `name` from `/chats`), and merge `updatedWatermarks` into `whatsapp_watermarks`. If `paginationCapHits[]` is non-empty, report a separate issue per chat (`issue_key: waha-chat-pagination-cap-hit:{chatId}`) — a rare case of one chat exceeding the pagination limit inside `waha-poll.mjs`.
5. Proceed to "Quiet Hours" to decide on sending notifications, then save and `git commit` + `push` the state.

## Event Detection

For each extracted message, check for signals of a concrete event: date and/or time, location, phrases like "let's meet," "event," "birthday," "party," "meeting," Zoom/Meet links, "save the date." Ignore casual chatter with no concrete time.

For each detected event, extract: title, start/end (default duration of one hour if no end time given), location (if any), timezone (default `Asia/Jerusalem`). Check for conflicts against the primary calendar (`list_events` over the event's time range) and flag overlaps. Always pass `singleEvents=True` and `orderBy='startTime'` on every `list_events` call, to correctly expand recurring events.

**Deduplication**: before adding a new suggestion to `pending_events`, check whether an identical one already exists (same `chat_id`, similar title/time). If so, don't add a duplicate; if the new message adds/refines details (e.g., a time that was previously unknown), update the existing suggestion instead of creating a new one.

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
      "detected_at": "2026-07-06T10:00:00+03:00",
      "event": {
        "title": "...",
        "start": "2026-07-10T18:00:00+03:00",
        "end": "2026-07-10T19:00:00+03:00",
        "location": "...",
        "timezone": "Asia/Jerusalem"
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
* If current time is inside quiet hours: Leave items in `notify_queued`.
* If current time is outside quiet hours: Send all queued messages to the self-chat (`GET /api/{session}/chats` to find the self-chat ID, then `POST /api/{session}/sendText`).
* For each successfully sent proposal, update its status in `pending_events` to `awaiting_response` and store the sent message's ID in `notification_message_id`. This ID is critical for matching future replies.

### 3. User Response Processing (Self-Chat)
During the polling phase, when scanning messages from the **self-chat**:
* Look for messages that are replies to a proposal (using `replyTo.id`).
* Match the `replyTo.id` against the `notification_message_id` of items in `pending_events` with status `awaiting_response`.
* Parse the user's reply text:
  * **Approved** (e.g., "כן", "yes", "אשר", "יאללה", 👍): Call Google Calendar API `insert_event` to create the event. Send a success confirmation back to the self-chat, remove the event from `pending_events`, and queue a `created` notification.
  * **Rejected** (e.g., "לא", "no", "ביטול", "אל תוסיף", 👎): Update status to `rejected`, clean it up from `pending_events`, and optionally acknowledge.
  * **Modified** (e.g., "תשנה ל-18:00", "change to 6pm"): Extract the new parameters, update the draft in `pending_events`, check conflicts again, and send an updated proposal.

## Duplicate Issue Reporting Prevention

To prevent spamming the user with repeated notifications about the same infrastructure failure (like WAHA being offline for hours):
* Maintain `issue_reported` in the state file.
* Before reporting an operational issue (unreachable API, session down, etc.), check if `issue_reported` already contains a matching `issue_key`.
* If it exists: **Do not send another notification**.
* If it does not exist: Send the alert immediately to the self-chat (bypassing quiet hours if it's a critical system error) and add it to `issue_reported` with the current timestamp.
* When a run succeeds, clear all matching issue keys from `issue_reported`.

## Critical fix — fromMe blocks replies in the self-chat

By default, WAHA polling filters out `fromMe == true` to avoid processing the agent's own messages. However, in the self-chat, **every message sent by the user from their phone also has `fromMe == true`**.
* **Rule**: When scanning the self-chat, do NOT discard messages where `fromMe == true` if they contain a valid `replyTo.id` pointing to an active proposal. These are treated as genuine user approvals.
