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

  const chats = await getJson(`/api/${WAHA_SESSION}/chats`);

  const candidateMessages = [];
  const updatedWatermarks = {};
  const paginationCapHits = [];

  // The self-chat (where the agent posts event proposals and the user
  // replies to approve/reject them) can't be recognized from a single
  // message's `from`/`to` fields: WAHA reports the agent's own API-sent
  // notifications under the account's `@lid` identity, but the user's
  // replies typed on their phone come back under the `@c.us` (phone number)
  // identity instead - both forms mean "this account", but they don't match
  // each other or `chatId` consistently (confirmed against a live WAHA
  // instance). What *is* stable: WhatsApp shows the self-chat's name as your
  // own account pushName (there's no contact on the other end to name it
  // after), so match the chat once, up front.
  const selfChatId = (() => {
    const pushName = session.me && session.me.pushName;
    if (!pushName) return null;
    const self = chats.find((c) => !c.isGroup && c.name === pushName);
    return self ? chatIdOf(self) : null;
  })();

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
      // fromMe is true for every message in the self-chat, including the
      // user's own quoted approve/reject replies - only exempt those from
      // the usual fromMe skip (a present replyTo.id distinguishes a real
      // reply from the agent's own notification message, which has none),
      // so the Approval Flow gets a chance to match it against
      // `notification_message_id`. Every other chat (groups, real contacts)
      // keeps the original fromMe skip unchanged.
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

    // WAHA omits the top-level `timestamp` field on messages when `filter.timestamp.gte`
    // is used (confirmed against a live WAHA instance), so per-message timestamps from
    // `fetchChatMessagesSince` can't be used to compute the new watermark. Since we've
    // now checked this chat's messages up through `now`, use that directly as the
    // checkpoint instead (never decreases, since it's compared against `watermark` above).
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
