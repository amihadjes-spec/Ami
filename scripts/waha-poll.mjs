import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';

const WAHA_URL = process.env.WAHA_URL || 'http://localhost:3000';
const WAHA_API_KEY = process.env.WAHA_API_KEY || '';
const WAHA_SESSION = process.env.WAHA_SESSION || 'default';
const STATE_FILE = process.argv[2];

// Default matches this machine's WAHA volume mount (.waha-sessions bound to
// /app/.sessions in the container); overridable in case the volume path or
// session name ever changes. See "getChatIdsFromNowebStore" for why this is
// read at all.
const WAHA_NOWEB_STORE_PATH =
  process.env.WAHA_NOWEB_STORE_PATH ||
  `${homedir()}/.waha-sessions/noweb/${WAHA_SESSION}/store.sqlite3`;

const BACKFILL_SECONDS = 24 * 3600;
const PAGE_SIZE = 500;
const MAX_PAGES_PER_CHAT = 20;

function loadWatermarks(path) {
  if (!path) return {};
  try {
    const raw = JSON.parse(readFileSync(path, 'utf8'));
    return raw.whatsapp_watermarks || {};
  } catch {
    return {};
  }
}

// Tracks the agent's own previously-sent proposal message IDs (from
// pending_events[].notification_message_id), so a fromMe self-chat message
// can be told apart from a fresh message the user typed/pasted directly.
// See "Critical fix — fromMe blocks replies in the self-chat" (condition b).
function loadSentNotificationIds(path) {
  if (!path) return new Set();
  try {
    const raw = JSON.parse(readFileSync(path, 'utf8'));
    const ids = (raw.pending_events || [])
      .map((e) => e.notification_message_id)
      .filter((id) => typeof id === 'string' && id.length > 0);
    return new Set(ids);
  } catch {
    return new Set();
  }
}

async function getJson(path, params) {
  const url = new URL(WAHA_URL + path);
  if (params) {
    for (const [k, v] of Object.entries(params)) url.searchParams.set(k, String(v));
  }
  const res = await fetch(url, { headers: { 'X-Api-Key': WAHA_API_KEY } });
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`${path} -> HTTP ${res.status}: ${text}`);
  }
  return res.json();
}

function chatIdOf(chat) {
  return typeof chat.id === 'string' ? chat.id : chat.id?._serialized;
}

function isGroupId(chatId) {
  return typeof chatId === 'string' && chatId.endsWith('@g.us');
}

async function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// GET /chats returns no `name`/`isGroup` fields under the NOWEB engine (only
// under WEBJS). /chats/overview includes `name` (populated as WAHA's
// background history sync catches up — it can be null right after a fresh
// connect) but never an `isGroup` field on any engine; isGroup is derived
// from the chatId suffix instead (`@g.us` = group).
async function fetchChatsOverview() {
  // The overview list fills in gradually while WAHA's NOWEB store finishes
  // its background sync, so a request made moments after the session
  // connects can return far fewer chats than actually exist. One short
  // retry is enough to let a burst of in-flight sync settle without turning
  // this into an open-ended wait — if it's still small after that, proceed
  // with whatever's there rather than blocking the run.
  const MIN_EXPECTED_CHATS = 10;
  const RETRY_DELAY_MS = 5000;
  let chats = await getJson(`/api/${WAHA_SESSION}/chats/overview`);
  if (chats.length < MIN_EXPECTED_CHATS) {
    await sleep(RETRY_DELAY_MS);
    chats = await getJson(`/api/${WAHA_SESSION}/chats/overview`);
  }
  return chats;
}

// WORKAROUND FOR A KNOWN WAHA BUG (2026-07-15, WAHA 2026.6.2, NOWEB engine):
// GET /chats and GET /chats/overview both read from an in-memory cache
// (`NowebInMemoryStore`) that never gets hydrated from the actual persisted
// history — confirmed by direct investigation: after a full, successful
// background sync (verified via docker logs reaching hundreds of "synced
// chats"), those endpoints stayed stuck at single digits while
// store.sqlite3 on disk correctly held 350+ chats. This reproduced
// identically even when the NOWEB store config was set correctly *before*
// the session's first-ever connection (the one config-ordering mistake
// WAHA's own docs warn can cause exactly this kind of corruption) — so it
// isn't a setup error on our end, it's a genuine bug in how that in-memory
// cache gets populated. GET /chats/{chatId}/messages was separately
// confirmed to work fine for chat IDs missing from the broken list
// endpoints, so only chat *enumeration* is affected, not per-chat message
// fetching.
//
// This reads the chat ID list directly from WAHA's own NOWEB persistent
// store (a SQLite file on the same volume WAHA itself writes to,
// .waha-sessions/noweb/{session}/store.sqlite3 — read-only here, WAHA is
// the only writer) instead of trusting the broken API endpoints for
// enumeration. If this ever breaks — wrong path, missing file, schema
// change in a future WAHA version — it fails soft and the caller falls
// back to the (known-broken-right-now, but self-healing if WAHA ever fixes
// it) `fetchChatsOverview()` path rather than crashing the whole run.
async function getChatIdsFromNowebStore() {
  try {
    const { DatabaseSync } = await import('node:sqlite');
    const db = new DatabaseSync(WAHA_NOWEB_STORE_PATH, { readOnly: true });
    try {
      const rows = db.prepare('SELECT id FROM chats').all();
      const ids = rows.map((r) => r.id).filter((id) => typeof id === 'string' && id.length > 0);
      return ids.length > 0 ? ids : null;
    } finally {
      db.close();
    }
  } catch {
    // Missing node:sqlite (older Node), missing/locked file, schema drift
    // in a future WAHA version, or any other read failure — all treated
    // the same: this is a best-effort optimization, not a hard dependency.
    return null;
  }
}

// Builds the chat list to poll: prefers the full, accurate ID list from
// WAHA's own persisted store (see getChatIdsFromNowebStore) and layers in
// whatever `name` values the (partially broken but not entirely useless)
// overview API has managed to resolve, falling back to the chat ID itself
// per-chat exactly as before when a name isn't available. If the store
// read fails outright, falls back fully to the pre-existing overview-only
// behavior.
async function fetchChatsToScan() {
  const overviewChats = await fetchChatsOverview();
  const storeIds = await getChatIdsFromNowebStore();
  if (!storeIds) return overviewChats;

  const nameById = new Map();
  for (const c of overviewChats) {
    const id = chatIdOf(c);
    if (id && c.name) nameById.set(id, c.name);
  }
  return storeIds.map((id) => ({ id, name: nameById.get(id) || null }));
}

async function fetchChatMessagesSince(chatId, gte) {
  const messages = [];
  for (let page = 0; page < MAX_PAGES_PER_CHAT; page++) {
    const batch = await getJson(`/api/${WAHA_SESSION}/chats/${encodeURIComponent(chatId)}/messages`, {
      sortBy: 'timestamp',
      sortOrder: 'asc',
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
      'filter.timestamp.gte': gte,
    });
    messages.push(...batch);
    if (batch.length < PAGE_SIZE) return { messages, cappedOut: false };
  }
  return { messages, cappedOut: true };
}

async function main() {
  const now = Math.floor(Date.now() / 1000);
  const watermarks = loadWatermarks(STATE_FILE);
  const sentNotificationIds = loadSentNotificationIds(STATE_FILE);

  const sessions = await getJson('/api/sessions');
  const session = sessions.find((s) => s.name === WAHA_SESSION);
  if (!session || session.status !== 'WORKING') {
    console.log(JSON.stringify({ ok: false, sessionStatus: session ? session.status : 'NOT_FOUND' }));
    return;
  }

  const chats = await fetchChatsToScan();

  const candidateMessages = [];
  const updatedWatermarks = {};
  const paginationCapHits = [];

  // The self-chat's ID is the account's own JID — derived directly from the
  // authenticated session (`GET /api/sessions`), not by matching pushName
  // against the chat list. This is robust regardless of chat-list sync
  // state (unlike the old name-matching approach) and confirmed empirically
  // // Fixed 2026-07-22: `session.me.id` (phone-based, e.g. ...@c.us) and
  // `session.me.lid` (WhatsApp's newer linked-device identifier) are two
  // DIFFERENT namespaces for the same account. Incoming self-chat messages
  // from WAHA arrive tagged with the phone-based id, not the lid — so
  // preferring `lid` alone caused every real self-chat message to fail the
  // `chatId === selfChatId` check below and be silently dropped as "not the
  // self-chat". We now match against BOTH identifiers.
  const selfChatIds = (() => {
    const ids = new Set();
    if (session.me && session.me.id) ids.add(session.me.id);
    if (session.me && session.me.lid) ids.add(session.me.lid);
    if (ids.size === 0) {
      const pushName = session.me && session.me.pushName;
      if (pushName) {
        const self = chats.find((c) => !isGroupId(chatIdOf(c)) && c.name === pushName);
        if (self) ids.add(chatIdOf(self));
      }
    }
    return ids;
  })();
  const selfChatId = selfChatIds.size > 0 ? [...selfChatIds][0] : null;
  // The self-chat is where every proposal/approval reply happens, so it
  // must always be scanned even if it hasn't synced into the (gradually
  // filling) overview list yet — a chat-list gap here would silently drop
  // approval replies rather than just missing a low-priority chat.
 if (selfChatId && !chats.some((c) => selfChatIds.has(chatIdOf(c)))) {
    chats.push({ id: selfChatId, name: (session.me && session.me.pushName) || selfChatId });
  }

  for (const chat of chats) {
    const chatId = chatIdOf(chat);
    if (!chatId) continue;
    const chatName = chat.name || chatId;
    const watermark = watermarks[chatId];
    const gte = watermark ? watermark + 1 : now - BACKFILL_SECONDS;

    let messages, cappedOut;
    try {
      ({ messages, cappedOut } = await fetchChatMessagesSince(chatId, gte));
    } catch {
      continue;
    }
    if (cappedOut) paginationCapHits.push(chatId);

    const isSelfChat = selfChatIds.has(chatId);
    let maxTs = 0;
    for (const m of messages) {
      if (typeof m.timestamp === 'number' && m.timestamp > maxTs) maxTs = m.timestamp;

      if (m.fromMe) {
        if (!isSelfChat) continue;
        const hasReply = !!(m.replyTo && m.replyTo.id);
        const isKnownOutgoing = sentNotificationIds.has(m.id);
        // Discard only the agent's own previously-sent proposals (no reply,
        // already tracked). Keep genuine approval/rejection replies (a) and
        // fresh user-authored messages typed/pasted straight into the
        // self-chat (b) — see "Critical fix — fromMe blocks replies in the
        // self-chat".
        if (!hasReply && isKnownOutgoing) continue;
      }
      if (!m.body || !m.body.trim()) continue;
      candidateMessages.push({
        chatId,
        chatName,
        messageId: m.id,
        from: m.from,
        author: m.author || m.participant || null,
        timestamp: m.timestamp,
        body: m.body,
        hasMedia: !!m.hasMedia,
        replyToMessageId: m.replyTo && m.replyTo.id ? m.replyTo.id : null,
      });
    }

    // See "Critical fix — watermark must not jump to 'now' on an empty delta
    // fetch": only a genuinely empty *initial backfill* (no prior watermark)
    // may fast-forward to `now`. A regular delta fetch that finds nothing
    // leaves the watermark untouched so a transient miss gets retried next
    // run instead of being silently skipped forever.
    if (messages.length && maxTs > 0) {
      updatedWatermarks[chatId] = maxTs;
    } else if (!watermark) {
      updatedWatermarks[chatId] = now;
    } else {
      updatedWatermarks[chatId] = watermark;
    }
  }

  console.log(JSON.stringify({
    ok: true,
    sessionStatus: 'WORKING',
    chatsScanned: chats.length,
    candidateMessages,
    updatedWatermarks,
    paginationCapHits,
  }));
}

main().catch((e) => {
  console.log(JSON.stringify({ ok: false, reason: 'unreachable', error: e.message }));
  process.exitCode = 1;
});