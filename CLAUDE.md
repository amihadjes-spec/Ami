# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

This is not a conventional application repo. It's the operational home for two autonomous
personal-automation agents belonging to `ami.hadjes@gmail.com`, each running as a scheduled,
stateless `claude-code-remote` session (cron, hourly):

- **`agents/email-event-calendar-agent.md`** — scans Gmail, detects emails describing a
  concrete event/reminder/renewal, and proposes adding it to Google Calendar. Runs
  cloud-hosted; state lives entirely in Gmail labels.
- **`agents/whatsapp-event-calendar-agent.md`** — the same idea, sourced from WhatsApp
  messages via WAHA (a self-hosted WhatsApp HTTP API in a local Docker container on Ami's
  machine). Runs locally; state lives in `state/whatsapp-event-agent-state.json`.

Both agents are **suggest-and-confirm only** — neither ever creates or modifies a calendar
event without explicit user approval.

The two `.md` files under `agents/` are prose specifications, not documentation of code that
lives elsewhere — for the email agent, the spec *is* the entire implementation (it's executed
directly by an LLM session with MCP tool access, no script backs it). The WhatsApp agent's
spec is similarly the implementation, except for one piece of deterministic logic it
delegates to a real script (`scripts/waha-poll.mjs`) because that logic must stay identical
and reliable across every stateless run.

When asked to work on "the agents," the actual behavior to read, understand, and edit lives
in these `.md` files — treat them with the same rigor as source code.

## Source of Truth: Agent Specs vs Routine Config

**The `.md` files under `agents/` are the source of truth.** Each one starts with an HTML
comment saying so:

```html
<!-- Source of truth. Copy verbatim into the routine's config after any edit. -->
```

Each agent also runs as a configured routine/trigger inside `claude-code-remote` (system
prompt / instructions set in the trigger UI), which is a **separate, out-of-repo copy** of
this same text. Editing the `.md` file in this repo does **not** automatically update the
live routine — the two can silently drift.

**Whenever you edit an agent spec file:**
1. Make the edit in the repo (`agents/*.md`), commit, and push — this repo is the durable
   record and history of the spec.
2. Separately copy the updated text verbatim into the routine's actual configuration in the
   `claude-code-remote` UI, so the live behavior matches what's in the repo.

Before debugging "the agent isn't behaving per spec," first confirm the routine's configured
instructions actually match the current file verbatim (see
`agent-troubleshooting-checklist.md`) — a stale routine config, not a logic bug, is a common
root cause.

Re-creating or overwriting a trigger also resets its enabled MCP connectors (Gmail, Google
Calendar) — the `create_trigger`/`update_trigger` API has no parameter for this, so it must be
re-verified manually in the UI after any trigger recreation, or the routine will fail silently
for lack of tool access.

## Repository layout

```
agents/    Source-of-truth specs for the two agents (see above)
scripts/   Deterministic Node scripts the WhatsApp agent shells out to
state/     Persistent state for the WhatsApp agent (git-committed, not gitignored)
```

- `scripts/waha-poll.mjs` — all WAHA REST orchestration (chat enumeration, pagination,
  per-chat watermark computation, self-chat identification). Kept as a maintained script
  rather than re-derived by the LLM each run because its logic is fiddly, has already broken
  in several non-obvious ways (see the "Critical fix" sections in the WhatsApp agent spec),
  and must behave identically every run. Env vars: `WAHA_URL` (default
  `http://localhost:3000`), `WAHA_API_KEY`, `WAHA_SESSION` (default `default`),
  `WAHA_NOWEB_STORE_PATH` (default `~/.waha-sessions/noweb/{session}/store.sqlite3`).
  Invoked as `node scripts/waha-poll.mjs <path-to-state-file>`; prints one JSON object to
  stdout (`{ok, sessionStatus, chatsScanned, candidateMessages[], updatedWatermarks{},
  paginationCapHits[]}`).
- `scripts/ntfy-notify.mjs` — sends a one-way informational push via ntfy.sh. Used for every
  `notify_queued` item **except** a fresh `proposal` (proposals go out as a real WhatsApp
  message instead, since only that channel can capture a reply). Requires `NTFY_TOPIC` env
  var. Usage: `node scripts/ntfy-notify.mjs --title "..." --message "..." [--priority ...] [--tags ...]`.
- `state/whatsapp-event-agent-state.json` — the WhatsApp agent's only persistent memory
  across runs (each run is a fresh, stateless session/container). Holds `pending_events`,
  `notify_queued`, `issue_reported`, `whatsapp_watermarks`, `rejected_events`. The exact
  shape is documented inline in `agents/whatsapp-event-calendar-agent.md` under
  "State Storage" — read that before changing the schema.
- `agent-troubleshooting-checklist.md` — the first thing to check when an agent seems to be
  misbehaving: whether the live routine config actually matches the repo spec.

## Development notes

There is no build, lint, or test setup in this repo (no test framework, no linter config, no
build step). `package.json` declares a single dependency, `googleapis`, but neither script in
`scripts/` currently imports it. Both scripts use only Node built-ins (`node:fs`, `node:os`,
`node:sqlite`, global `fetch`) — no `npm install` is required to run them. Node 22+ is assumed
(`node:sqlite` requires it).

To exercise `waha-poll.mjs` manually you need a reachable WAHA instance (`WAHA_URL`) and a
valid `WAHA_API_KEY`/session — both only meaningfully available on Ami's machine where WAHA
actually runs. There's no mock/fixture setup in-repo.

### State file and git are part of the runtime, not just history

The WhatsApp agent's state file is committed to git as part of its normal operation: every
run does `git pull` at the start (to pick up state from whatever container the previous run
used) and `git commit` + `git push` at the end (to persist state for the next run). Commit
messages on this file are agent-generated run summaries, in Hebrew, describing what was
scanned/proposed/resolved that run — this is expected and is effectively the agent's audit
log, not noise. When working in this repo, be aware that `git log` on `state/` is a real
operational record, and a manual edit to the state file competes directly with the next
scheduled run's `git pull`/`git commit` cycle.

## Key conventions when editing agent specs

- **Never silently drop a documented rule.** Several "Critical fix" sections in
  `agents/whatsapp-event-calendar-agent.md` exist because a rule was accidentally lost during
  a prior condensing rewrite and had to be re-diagnosed from a real incident. When editing
  these specs for length or clarity, preserve every standing rule and every "Critical fix" —
  they encode real, previously-reproduced failure modes, not speculative edge cases.
- **Gmail label negation is broken in the MCP tool used here.** `-label:X` in a
  `search_threads` query is unreliable (confirmed against this account). Never rely on it in
  either agent spec or in ad hoc tooling — filter on `labelIds` in code/logic after a positive
  (non-negated) query instead.
- **The two agents are cross-channel-coupled around `Ami/Event-Pending`.** A single proposal
  detected via Gmail can be resolved via either a Gmail reply (email agent) or a WhatsApp
  reply (WhatsApp agent, via the "Gmail-Sourced Proposals" bridge). Both sides re-check that
  `Ami/Event-Pending` is still present immediately before calling `create_event`, to prevent a
  duplicate event if the other channel wins the race. Any change to this handoff needs to
  preserve that idempotency check on both sides — see "WhatsApp Delivery Bridge" in the email
  agent spec and "Gmail-Sourced Proposals" in the WhatsApp agent spec.
- **Quiet hours (22:00–07:00/08:00, `Asia/Jerusalem`) gate notification delivery, not
  processing.** Both agents keep scanning/labeling/queuing during quiet hours; only the
  outbound push to the user is delayed and batched into a single digest.
- **Calendar event creation always follows verify-before-label ordering**: call the create/
  update API, verify the response actually contains an event id before applying any
  "created" label or removing "pending" state, and only then do any follow-up update (e.g.
  adding a source link to the description). Never mark something created based on the call
  merely not throwing.
