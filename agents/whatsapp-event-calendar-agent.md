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
```

- `pending_events` — events detected and not yet resolved (approved/rejected/found to already exist). Any session handling what looks like an approval/rejection/change-request must first reload `pending_events` from the state file rather than relying on conversation memory.
- `notify_queued` — messages awaiting delivery due to quiet hours (new proposals as well as results of approvals/rejections processed during quiet hours).
- `issue_reported` — see "Duplicate Issue Reporting Prevention."
- `whatsapp_watermarks` — maps `chat_id` → unix-seconds timestamp of the last message checked in that chat. Missing key = never checked = 24h backfill on the next run.

At the end of every run: save the file, `git add`, `git commit` with a concise message, `git push` to the operational branch. Without pushing, the next run (a fresh container/clone) won't see the update.

## Approval Flow

For every incoming text message that is not a new event description, first check `pending_events` for an open suggestion from the same `chat_id`:

- Words like "yes"/"confirm" → **approval**: create the calendar event (`create_event`) with the proposed details. Before creating, re-check with `list_events` that a duplicate wasn't already created (e.g., double approval, or several trigger runs since the suggestion). Then remove the suggestion from `pending_events` and add a `notify_queued` entry with `kind: "created"` (or `"duplicate"` if it already existed).
- Words like "change"/"modify" → **change request**: don't create anything. Mark the suggestion as awaiting new details and queue a request asking what to change. When the detail arrives in the next message from the same `chat_id`, update only that field and re-propose for approval (back to the start of this flow).
- Words like "no"/"reject" → **rejection**: remove the suggestion from `pending_events` without creating an event. It is never re-suggested (the source message is now older than the chat's watermark, so it won't be re-scanned).
- If several suggestions are open at once for the same `chat_id` and it's unclear which the reply targets, ask the user to choose (via the next `notify_queued` message) instead of guessing.

**Matching a reply to a suggestion**: match `replyTo.id` (from an incoming message) against `notification_message_id` of each `pending_event` in `awaiting_response` status — not against `source_message_id`. An exact match identifies which suggestion the reply is for; the reply's body ("yes"/"no"/"change") determines the action. For `pending_events` created before this field existed (and therefore lacking `notification_message_id`), fall back to a time-window heuristic as a safety net only, not as standard behavior.

**Multiple replies to the same suggestion**: if several valid replies (matching `replyTo.id`) arrive for one open suggestion, the reply with the highest `timestamp` (chronologically last) wins; earlier ones are ignored. Rationale: a later reply reflects the user's final decision (e.g., "no," then reconsidering to "yes"). See "Last-reply-wins: real-world failure and decision" below for an important caveat about this rule.

## Run Summary

At the end of every run (after the polling loop, regardless of whether anything was detected), always append one `notify_queued` entry with `kind: "run_summary"`: a short line summarizing the run for the notification feed (e.g. "27 messages checked, no new events detected"). Informational only, not a substitute for the individual proposal/result entries already queued elsewhere. Subject to the same quiet-hours batching as any other `notify_queued` entry.

## Quiet Hours

**22:00–07:00, Asia/Jerusalem (not UTC).**

- The polling loop, event detection, `pending_events` updates, and conflict checks always run normally regardless of the hour.
- Only **sending a message to the user** is deferred — whether a new proposal or the result of handling a reply that arrived during quiet hours. All of these go into `notify_queued` and stay there.
- Before sending anything, check the current time in `Asia/Jerusalem` (e.g. `TZ=Asia/Jerusalem date +%H:%M`). If between 22:00 (inclusive) and 07:00 (exclusive) — **send nothing this run**; finish quietly (state is still saved/pushed as usual).
- From 07:00 onward: collect **all** entries in `notify_queued` (including ones accumulated overnight from separate messages) and send **one consolidated message**, not one per entry. Afterward, clear the entries that were sent.
- **No exceptions** — even an urgent-looking proposal waits with everything else until quiet hours end.
- Outside quiet hours (07:00–22:00), a new entry queued during the current run is sent immediately.
- **Two separate output channels, by message type:**
  - **New event proposal** (any `kind` that requires a reply from the user to proceed) — sent as a real WhatsApp message to the self-chat via `POST /api/sendText`, per "Read-only constraint" below. This is the sole exception to the read-only rule.
  - **Every other `notify_queued` kind** (`created | modified | rejected | duplicate | error | run_summary` — informational, no reply needed) — sent via ntfy (`scripts/ntfy-notify.mjs`, fixed topic `ami-whatsapp-agent-x7k2p`, `POST https://ntfy.sh/<topic>`, message body as UTF-8 text, short `Title` header), not as a WhatsApp message. Multiple accumulated entries of this kind are sent as one consolidated ntfy message.
  - Both channels obey the same quiet-hours rules above.

## Duplicate Issue Reporting Prevention

Identical to the Email Agent: an issue (e.g. `waha-unreachable`, or `waha-session-not-working:{status}`) is reported to the user once, recorded in `issue_reported` by a stable `issue_key`, and not reported again until it's actually resolved (a subsequent run completes the same operation without error, at which point the entry is removed from `issue_reported`).

## Core Principles

- The agent never creates a calendar event without explicit user approval.
- Exactly one summary line is sent to the notification feed per run (see "Run Summary") — no extra/repeated messages beyond that when there's nothing to report.
- No proactive messages during quiet hours (22:00–07:00, Asia/Jerusalem) — polling, detection, and conflict checks continue; only sending is deferred and batched.
- Never quote a WhatsApp message in full — only the event-relevant details.
- Every checked message updates its chat's watermark before the run ends, even if skipped as irrelevant — otherwise that chat gets an unnecessary backfill next run.

## Read-Only Constraint: One Defined Exception for Writing to WhatsApp

The approval mechanism depends on matching `replyTo.id` to a suggestion message (see "Approval Flow"). There is no other way in this system to capture an approval/rejection/change reply — so sending a new event proposal **must** go out as a real WhatsApp message to the self-chat, or there's nothing to quote and no `replyTo.id` to compare against. This is the single, fully-scoped exception to the general read-only rule:

**Permitted, and only this:** a single `POST /api/sendText` call to the self-chat, only to send a new event proposal (or a disambiguation request among multiple open proposals) that requires a quoted `reply` from the user. Immediately after sending, save the `id.id` returned by WAHA into the relevant `pending_event`'s `notification_message_id` field.

**Everything else** (run summary, post-approval/rejection/change results, issue reports — anything not requiring a reply) goes out via ntfy only, never as a WhatsApp message.

The agent **never** calls any other WAHA endpoint that writes/sends/changes state — including but not limited to `sendImage`/`sendFile`/`sendLocation`/`sendContactVcard`/`sendPoll`, and **`POST /api/{session}/chats/{chatId}/messages/read`** — despite its name, this has a real side effect (marks messages as read on the actual WhatsApp account: blue checkmarks, unread badge reset) and is **forbidden**, exactly like any other send endpoint.

**Allowed read endpoints:**
- `GET /api/sessions`
- `GET /api/{session}/chats`
- `GET /api/{session}/chats/{chatId}/messages`
- `POST /api/sendText` — **only** to the self-chat, **only** for a new proposal/disambiguation request, as above.

No other WAHA endpoint may be used by this agent, without exception.

## Critical Fix — fromMe Blocks Replies in the Self-Chat (2026-07-11)

**Root cause found:** the `replyTo.id`-based approval mechanism never actually detected a reply, *even when `replyTo.id` was valid and matched correctly*. `scripts/waha-poll.mjs` filtered `if (m.fromMe) continue;` *before* ever checking `replyTo` — and every approval/rejection reply is sent in the self-chat (since that's where proposals are sent), where **every** message reports `fromMe: true`, including a genuine reply the user typed/swiped on their phone. WhatsApp doesn't distinguish "I sent this to myself via the API" from "I typed this to myself on my phone" — both report `fromMe: true`. Verified manually against a live account: every self-chat reply had a valid `replyTo.id` but was discarded by the `fromMe` filter before that was ever checked.

**Fix**: a `fromMe: true` message is no longer discarded if (a) its chat is the self-chat, and (b) it has a valid `replyTo.id` (which distinguishes a genuine reply from the agent's own original proposal message, which has no `replyTo`). Behavior in all other chats (groups, contacts) is unchanged.

**Self-chat identification — not by comparing `chatId` to `session.me.id`:** an initial attempt to match `m.from` against `chatId` (or `session.me.id` from `GET /api/sessions`) failed: WAHA/WEBJS reports `from` as the account's `@lid` identifier for messages sent via the API (e.g. `153768486285323@lid`), but as the account's `@c.us` phone identifier for messages actually typed on the phone (e.g. `972526031305@c.us`) — two different identifiers for the same account, neither of which reliably equals `chatId` (verified directly against a live WAHA instance). Solution: identify the self-chat once at the start of each run using a more stable rule — the self-chat's name in WhatsApp is always the account's own `pushName` (`session.me.pushName`, since no other contact could have that name), not identifier comparison.

Verified against a live WAHA instance after the fix: 3 historical reply messages (from earlier manual tests, `replyTo.id: "3EB0F01E50E5D02EFE297E"`) surfaced correctly when tested against a rolled-back copy of the state (not the real state, to avoid triggering an unwanted event from old test data). Groups/contacts checked in parallel against the real, unchanged state showed the same 6 candidate messages as before the fix — no regression.

## Last-Reply-Wins Rule: Real-World Failure and Decision (2026-07-11)

**Confirmed with the user (2026-07-11):** when multiple *conflicting* replies arrive for the same open suggestion, the chronologically latest (`timestamp`) reply wins, per "Approval Flow" above.

**This rule produced a wrong outcome in practice, manually reverted (2026-07-11):** applying it literally, `pending_event` `wa-labim-parents-summer-talk-20260712` ("Talk for parents of teens — who's afraid of summer?", 12 Jul 2026 20:00–21:00, Lehavim youth club, source: "Lehavim residents" group) was approved and created on `ami.hadjes@gmail.com` based on the latest of 3 conflicting replies ("yes" at 12:39, after "no" at 09:27 and "No" at 11:26 — all 11 Jul 2026, Asia/Jerusalem). The user **manually deleted the event immediately afterward** (event id `5cf9kc85l48946on4vrubh1g1k`, status `cancelled`) and confirmed on follow-up that "no" was in fact the correct call — the late "yes" was likely a test/mistake, not a genuine change of mind.

**Immediate operational note:** if `waha-poll.mjs` ever re-surfaces any message tied to this case (the source message, the proposal with `notification_message_id: 3EB0F01E50E5D02EFE297E`, or any of the three replies) — **do not recreate this event**. As of this writing there is no technical safeguard against it (confirmed on 2026-07-11: no orphaned record remains anywhere in the state file for this event, so there's nothing to clean up), and no realistic path for it to resurface (all relevant watermarks have already passed these messages) — but if a watermark for either chat is ever reset (state corruption, restore from an old backup, forced backfill), this is an explicit note not to recreate it.

**Proposed disambiguation-step alternative — considered and rejected (2026-07-11):** rather than auto-applying "last reply wins" when more than one valid reply exists, the agent could pause, send a clarification message quoting all conflicting replies (with timestamps) to the self-chat, and require a fresh reply to *that* message as the final word — mirroring the existing "multiple open suggestions, unclear which" handling. This would have prevented the incident above, and arguably fits the "always require explicit approval" principle better (a single unambiguous reply is explicit approval; several conflicting replies arguably aren't). Downside: an extra round-trip, against the whole point of the self-chat reply mechanism (single-step approval, see "Read-only constraint" above); requires extra state (a third `pending_event` status: `awaiting_disambiguation`) and its own `notification_message_id`, plus handling the edge case of the clarification reply itself receiving conflicting replies. **Decision: not implemented** — the last-reply-wins rule stays as-is. This was a deliberate call after weighing both sides with the user, not an open item; worth revisiting only if further real-world cases of conflicting replies causing bad outcomes accumulate.

## Local Execution (Sole Viable Path)

**As of 2026-07-08**, local CLI execution is not just the only path proven to work end-to-end — it's the only one **structurally possible**. WAHA runs only as a local Docker container on `localhost:3000`, unreachable from any external cloud environment (unlike Green-API, a cloud service that was reachable but blocked by org egress policy). So even if that old Green-API egress block were lifted, it wouldn't matter: a cloud trigger can never reach a WAHA instance running on this machine. **No further investment in a cloud-trigger path for this agent.**

| | Local execution (CLI) |
|---|---|
| Availability | Works, provided the machine is on at the scheduled hour **and** Docker Desktop + the `waha` container are running with the session in `WORKING` state |
| Trigger mechanism | Scheduled run (local cron / Task Scheduler) of the Claude Code CLI, against the same `state/whatsapp-event-agent-state.json` in the repo |
| Network dependency | Only `localhost:3000` (WAHA) — no external egress dependency for polling itself |
| Secrets | `WAHA_URL`/`WAHA_API_KEY` loaded from the local user environment (in `run-whatsapp-agent.ps1`) |
| Calendar connector | Connected via the user's local session |
| State (git) | `git pull`/`commit`/`push` to the operational branch at the end of each run |

## Infrastructure Notes

- **New Docker dependency**: unlike Green-API (a managed cloud service), WAHA requires Docker Desktop and the `waha` container to actually be running at trigger time. `run-whatsapp-agent.ps1` best-effort starts Docker Desktop if it's off (non-blocking); if the container/session still isn't available, the run reports it via `issue_reported` rather than failing uncontrollably.
- **WAHA's API key rotates on every container restart** unless set as a fixed env var (`WAHA_API_KEY`) at container run time (`docker run -e WAHA_API_KEY=...` or compose) — must be configured this way, or scheduled runs will fail with a stale key whenever Docker/the container restarts.
- **No meaningful risk of missed polling short-term** — the per-chat watermark mechanism means a failed run is caught up by the next one (up to the pagination cap in `waha-poll.mjs`).
- **State lives in git** — `git push` at the end of each run must actually succeed (to the agent's fixed operational branch), or subsequent runs start from stale state and risk duplicate suggestions/notifications.
- **Container rebuild (2026-07-10)**: the `waha` container had gone missing entirely (not even stopped). Recreated with `docker run`, port mapping `3000:3000` (correcting a prior `8080` mismatch against `WAHA_URL`), volume mount from `.waha-sessions` to `/app/.sessions`, explicit `WAHA_DASHBOARD_USERNAME`/`WAHA_DASHBOARD_PASSWORD`, and `--restart unless-stopped` so `docker start waha` in `run-whatsapp-agent.ps1` always finds an existing container after a reboot.
- **Engine correction (2026-07-10)**: the actual engine is **WEBJS**, not NOWEB as originally documented — confirmed by the session path `.waha-sessions/webjs/default`.

## Resolved Investigation: `filter.timestamp.gte` (2026-07-11)

An earlier run log raised an unconfirmed suspicion that WAHA might not be enforcing `filter.timestamp.gte`, based on the same 2 messages reappearing. Verified directly against a live WAHA instance with 4 boundary tests on one chat: `gte` one second after a known message excludes it (later messages only pass); `gte` exactly at a known message's timestamp includes it (inclusive `>=`); a far-future `gte` returns 0 results; no `gte` at all on the same chat returns 73 messages (far more than the filtered result).

**Conclusion: `filter.timestamp.gte` is enforced correctly server-side** — the original suspicion was wrong. The actual cause of repeated messages was **state-file corruption**, not WAHA behavior: (a) a BOM at the start of the state file silently broke `JSON.parse` in `loadWatermarks`, resetting all watermarks to `{}` and forcing full backfill every run; (b) some watermarks were stored in milliseconds instead of unix seconds, producing meaningless `gte` values. Both were already fixed in earlier automated runs (BOM removed; all 1,010 values converted to seconds). Verified against the current file (2026-07-11): no BOM, all 1,010 `whatsapp_watermarks` entries consistently in seconds (`min: 1783625904`, `max: 1783790696`, close to `now` at check time) — no remnants of either issue. No further action needed.
