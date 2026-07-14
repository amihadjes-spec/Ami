# Agent: Email Event Detection + Calendar Proposal

## Purpose
Scan the Gmail inbox, detect emails containing concrete events/meetings (date + time), check for conflicts in the calendar, and **propose** adding the event to the user's calendar—without creating it automatically unless explicit confirmation is given.

## Trigger
Since Gmail MCP tools lack an active "new email arrived" webhook, the trigger is implemented via a scheduled hourly poll (cron) to check for unexamined messages. 

Each execution creates a clean, stateless session. All persistent state is tracked directly via Gmail labels rather than session memory.

## State Management & Duplicate Prevention
The agent manages state using four dedicated Gmail labels (automatically created on the first run if missing):

* `Ami/Event-Checked`: Thread has been processed (whether an event was found or not).
* `Ami/Event-Suggested`: An event was detected and proposed to the user.
* `Ami/Event-Pending`: Proposal is awaiting user feedback. This acts as the definitive source of truth across different sessions. Any session handling a user response must first query `label:Ami/Event-Pending` to recover open proposals.
* `Ami/Event-Notify-Queued`: A detected proposal waiting to be sent to the user (held during quiet hours or batched).

## Execution Workflow (Each Run)
1. **Verify Labels:** Ensure `Ami/Event-Checked` and `Ami/Event-Suggested` exist (`list_labels` / `create_label`).
2. **Fetch New Emails:** Query `search_threads` for `in:inbox -label:Ami/Event-Checked` (up to 50 per run). If empty, exit silently.
3. **Analyze & Process:** Fetch full content (`get_thread`, FULL_CONTENT) per thread. Look for explicit dates/times (invitations, confirmations, Zoom/Meet links, `.ics` files). Ignore newsletters, generic ads, or dateless receipts.
4. **Mark as Checked:** Immediately apply `Ami/Event-Checked` to the thread.
5. **Extract Metadata:** For valid events, extract: Title, Start/End time (default duration: 1 hour), Location/Link, and Timezone (default: `Asia/Jerusalem`).
6. **Conflict Check:** Query `list_events` on the event's timeframe to flag overlapping entries.
7. **Queue Proposals:** If an event is found, apply `Ami/Event-Suggested`, `Ami/Event-Pending`, and `Ami/Event-Notify-Queued` to the thread.
8. **Dispatch Notification:** Evaluate the current time in `Asia/Jerusalem` to handle messaging based on Quiet Hours.

## Quiet Hours
**22:00–07:00 (Asia/Jerusalem timezone, not UTC).**

* **Processing Continues:** The hourly cron continues to scan, apply labels, and check conflicts normally during these hours.
* **Delayed Delivery:** Active push notifications/messages to the user are paused. Proposals receive the `Ami/Event-Notify-Queued` label and are held.
* **Morning Digest:** In the first hourly run after 07:00, the agent collects all threads marked with `Ami/Event-Notify-Queued` and dispatches **a single consolidated digest message** to the user. After sending, the `Ami/Event-Notify-Queued` label is removed.
* **No Exceptions:** Urgent or close-proximity events detected during overnight hours are also held until 07:00. Outside of quiet hours, notifications are dispatched instantly.

## Confirmation Flow & Event Creation
When a user responds to a proposal (immediately or in a later session), the agent processes the action as follows:

1. **Approval Detection:** Affirmative responses containing phrases like "yes", "confirm", "go ahead", "כן", "מאשר", "✓" trigger event creation.
2. **Calendar Insertion:** The agent executes `create_event` using the verified details. The `Ami/Event-Pending` label is then removed.
3. **Double-Booking Prevention:** Right before executing `create_event`, the agent runs a final `list_events` check (via timeframe and `fullText`) to ensure the event wasn't already created (e.g., accidental duplicate approval). If it exists, creation is skipped, `Ami/Event-Pending` is removed, and the user is informed.
4. **Modification Requests:** If the user updates specific details (e.g., "Yes, but change the time to 15:00"), the agent updates the target field, preserves the remaining original data, and creates the event.
5. **Rejection:** Negative responses ("no", "skip", "לא") cancel creation. `Ami/Event-Pending` is removed, leaving only `Ami/Event-Checked` and `Ami/Event-Suggested`.

### Edge Case Handlers
* **Ambiguous Approvals:** If a user says "yes" but multiple pending proposals exist, the agent references the open `Ami/Event-Pending` threads and prompts the user to select the correct one.
* **Delayed Approvals:** If a user approves a proposal hours later across a new session, the script accurately recovers the context using the `Ami/Event-Pending` thread markup.
* **Re-evaluation Safeguard:** Previously rejected or processed threads never trigger duplicate proposals because they lack the `Ami/Event-Pending` markup and are skipped by the initial `Ami/Event-Checked` inbox filter.

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