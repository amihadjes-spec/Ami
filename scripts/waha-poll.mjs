import { readFileSync } from 'node:fs';

const WAHA_URL = process.env.WAHA_URL || 'http://localhost:3000';
const WAHA_API_KEY = process.env.WAHA_API_KEY || '';
const WAHA_SESSION = process.env.WAHA_SESSION || 'default';
const STATE_FILE = process.argv[2];

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

  const sessions = await getJson('/api/sessions');
  const session = sessions.find((s) => s.name === WAHA_SESSION);
  if (!session || session.status !== 'WORKING') {
    console.log(JSON.stringify({ ok: false, sessionStatus: session ? session.status : 'NOT_FOUND' }));
    return;
  }

  const chats = await fetchChatsOverview();

  const candidateMessages = [];
  const updatedWatermarks = {};
  const paginationCapHits = [];

  // The self-chat's ID is the account's own JID — derived directly from the
  // authenticated session (`GET /api/sessions`), not by matching pushName
  // against the chat list. This is robust regardless of chat-list sync
  // state (unlike the old name-matching approach) and confirmed empirically
  // against a live NOWEB session: `session.me.lid` resolves straight to the
  // real self-chat history. Prefer `lid` (how self-sent messages actually
  // address the chat) and fall back to `id`, then to the old name+isGroup
  // heuristic only if `session.me` is unexpectedly missing both — this
  // should not happen in practice, it's a defensive last resort only.
  const selfChatId = (() => {
    if (session.me && session.me.lid) return session.me.lid;
    if (session.me && session.me.id) return session.me.id;
    const pushName = session.me && session.me.pushName;
    if (!pushName) return null;
    const self = chats.find((c) => !isGroupId(chatIdOf(c)) && c.name === pushName);
    return self ? chatIdOf(self) : null;
  })();

  // The self-chat is where every proposal/approval reply happens, so it
  // must always be scanned even if it hasn't synced into the (gradually
  // filling) overview list yet — a chat-list gap here would silently drop
  // approval replies rather than just missing a low-priority chat.
  if (selfChatId && !chats.some((c) => chatIdOf(c) === selfChatId)) {
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

    for (const m of messages) {
      const isSelfChatReply = chatId === selfChatId && m.fromMe && m.replyTo && m.replyTo.id;
      if (m.fromMe && !isSelfChatReply) continue;
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

    updatedWatermarks[chatId] = now;
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