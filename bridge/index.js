// Read-only WhatsApp bridge.
// Links as a companion device (like WhatsApp Web), listens for NEW messages, and POSTs each one
// to the brain. It never sends anything on WhatsApp — there is deliberately no send path here.

import makeWASocket, {
  DisconnectReason,
  downloadMediaMessage,
  fetchLatestBaileysVersion,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys';
import pino from 'pino';
import qrcode from 'qrcode-terminal';
import path from 'node:path';

const BRAIN_URL = (process.env.BRAIN_URL || 'http://localhost:8000').replace(/\/$/, '');
const BRAIN_TOKEN = process.env.BRAIN_TOKEN || 'change-me';
const FORWARD_AUDIO = (process.env.FORWARD_AUDIO || 'true').toLowerCase() === 'true';
const IGNORE_JIDS = new Set((process.env.IGNORE_JIDS || '').split(',').map((s) => s.trim()).filter(Boolean));
const AUTH_DIR = process.env.AUTH_DIR || path.join(process.cwd(), 'auth');
// HISTORY_SYNC: off | recent | full. History only arrives at link time (re-scan the QR after changing).
const HISTORY_SYNC = (process.env.HISTORY_SYNC || 'off').toLowerCase();

const log = pino({ level: process.env.LOG_LEVEL || 'info' });
const groupNames = new Map(); // jid -> subject (cached)

// ---- helpers ---------------------------------------------------------------

function unwrap(message) {
  // Peel ephemeral / view-once / edited wrappers down to the real content.
  let m = message;
  for (let i = 0; i < 4 && m; i++) {
    const inner = m.ephemeralMessage?.message || m.viewOnceMessage?.message ||
      m.viewOnceMessageV2?.message || m.editedMessage?.message?.protocolMessage?.editedMessage ||
      m.documentWithCaptionMessage?.message;
    if (!inner) break;
    m = inner;
  }
  return m || {};
}

function extractContent(msg) {
  const m = unwrap(msg.message);
  if (m.conversation) return { text: m.conversation };
  if (m.extendedTextMessage) return { text: m.extendedTextMessage.text || '' };
  if (m.imageMessage) return { text: m.imageMessage.caption || '', media_type: 'image' };
  if (m.videoMessage) return { text: m.videoMessage.caption || '', media_type: 'video' };
  if (m.documentMessage) return { text: m.documentMessage.caption || m.documentMessage.fileName || '', media_type: 'document' };
  if (m.audioMessage) return { text: '', media_type: 'audio', mime: m.audioMessage.mimetype, isVoice: !!m.audioMessage.ptt };
  if (m.stickerMessage) return { text: '', media_type: 'sticker' };
  if (m.contactMessage) return { text: `[contact] ${m.contactMessage.displayName || ''}`, media_type: 'contact' };
  if (m.locationMessage) return { text: '[location]', media_type: 'location' };
  if (m.pollCreationMessage) return { text: `[poll] ${m.pollCreationMessage.name || ''}`, media_type: 'poll' };
  if (m.reactionMessage || m.protocolMessage || m.senderKeyDistributionMessage) return null; // not a message to us
  return { text: '', media_type: 'unknown' };
}

async function groupName(sock, jid) {
  if (groupNames.has(jid)) return groupNames.get(jid);
  try {
    const meta = await sock.groupMetadata(jid);
    groupNames.set(jid, meta.subject);
    return meta.subject;
  } catch (e) {
    log.warn({ jid, err: e.message }, 'groupMetadata failed');
    return null;
  }
}

async function postToBrain(payload) {
  const res = await fetch(`${BRAIN_URL}/ingest/whatsapp`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', authorization: `Bearer ${BRAIN_TOKEN}` },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`brain ${res.status}: ${(await res.text()).slice(0, 200)}`);
  return res.json();
}

async function postHistory(batch) {
  const res = await fetch(`${BRAIN_URL}/ingest/whatsapp/history`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', authorization: `Bearer ${BRAIN_TOKEN}` },
    body: JSON.stringify({ messages: batch }),
  });
  if (!res.ok) throw new Error(`brain ${res.status}: ${(await res.text()).slice(0, 200)}`);
  return res.json();
}

// Build the brain payload for one message, or null if it's not something we forward.
async function toPayload(sock, msg) {
  let jid = msg.key.remoteJid;
  if (!jid || jid === 'status@broadcast' || IGNORE_JIDS.has(jid)) return null;
  // WhatsApp's newer "LID" addressing: prefer the phone-number form when Baileys gives it,
  // so the same contact doesn't show up under two ids.
  if (jid.endsWith('@lid')) jid = msg.key.remoteJidAlt || msg.key.senderPn || jid;
  const content = extractContent(msg);
  if (!content) return null;
  const isGroup = jid.endsWith('@g.us');
  let senderId = isGroup ? (msg.key.participant || jid) : jid;
  if (senderId.endsWith('@lid')) senderId = msg.key.participantAlt || msg.key.senderPn || senderId;
  return {
    id: msg.key.id,
    chat_id: jid,
    chat_name: isGroup ? await groupName(sock, jid) : (msg.pushName || null),
    sender_id: senderId,
    sender_name: msg.key.fromMe ? 'me' : (msg.pushName || null),
    from_me: !!msg.key.fromMe,
    is_group: isGroup,
    text: content.text || '',
    media_type: content.media_type || null,
    timestamp: Number(msg.messageTimestamp),
    _content: content,
  };
}

// simple retry queue so a brain restart doesn't lose messages
const queue = [];
let draining = false;
async function drain() {
  if (draining) return;
  draining = true;
  while (queue.length) {
    const item = queue[0];
    try {
      await postToBrain(item);
      queue.shift();
    } catch (e) {
      log.warn({ err: e.message, queued: queue.length }, 'brain unreachable; retrying in 5s');
      await new Promise((r) => setTimeout(r, 5000));
    }
  }
  draining = false;
}

// ---- main ------------------------------------------------------------------

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();
  const sock = makeWASocket({
    version,
    auth: state,
    logger: pino({ level: 'silent' }),
    markOnlineOnConnect: false, // don't make the phone stop ringing for notifications
    syncFullHistory: HISTORY_SYNC === 'full',
    shouldSyncHistoryMessage: () => HISTORY_SYNC !== 'off',
    browser: ['AI_Assistant', 'Desktop', '0.1'],
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', (u) => {
    const { connection, lastDisconnect, qr } = u;
    if (qr) {
      log.info('scan this QR with WhatsApp > Linked devices');
      qrcode.generate(qr, { small: true });
    }
    if (connection === 'open') log.info('whatsapp connected');
    if (connection === 'close') {
      const code = lastDisconnect?.error?.output?.statusCode;
      if (code === DisconnectReason.loggedOut) {
        log.error('logged out — delete the auth folder and re-scan');
        process.exit(1);
      }
      log.warn({ code }, 'connection closed; reconnecting in 3s');
      setTimeout(start, 3000);
    }
  });

  // ---- history backfill (context only; the brain never triages these) ----
  let history = [];
  let historyTimer = null;
  const flushHistory = async () => {
    const batch = history; history = [];
    for (let i = 0; i < batch.length; i += 200) {
      const chunk = batch.slice(i, i + 200);
      try {
        const r = await postHistory(chunk);
        log.info({ received: r.received, stored: r.stored }, 'history batch stored');
      } catch (e) {
        log.warn({ err: e.message }, 'history batch failed; re-queueing');
        history.push(...chunk);
        historyTimer = setTimeout(flushHistory, 10000);
        return;
      }
    }
  };
  const queueHistory = async (messages) => {
    for (const msg of messages) {
      try {
        const p = await toPayload(sock, msg);
        if (p && p.text) { delete p._content; history.push(p); }
      } catch (e) { log.warn({ err: e.message }, 'history message skipped'); }
    }
    clearTimeout(historyTimer);
    historyTimer = setTimeout(flushHistory, 2000); // debounce: WA sends history in many chunks
  };

  if (HISTORY_SYNC !== 'off') {
    sock.ev.on('messaging-history.set', ({ messages, isLatest, progress }) => {
      log.info({ count: messages.length, isLatest, progress }, 'history chunk');
      queueHistory(messages);
    });
  }

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') {
      // 'append' = older messages surfaced by sync; treat as history, never as live
      if (HISTORY_SYNC !== 'off') queueHistory(messages);
      return;
    }
    for (const msg of messages) {
      try {
        const payload = await toPayload(sock, msg);
        if (!payload) continue;
        const content = payload._content;
        delete payload._content;

        if (content.media_type === 'audio' && content.isVoice && FORWARD_AUDIO) {
          try {
            const buf = await downloadMediaMessage(msg, 'buffer', {}, { logger: log, reuploadRequest: sock.updateMediaMessage });
            payload.media_b64 = buf.toString('base64');
            payload.media_mime = content.mime || 'audio/ogg';
          } catch (e) {
            log.warn({ err: e.message }, 'voice note download failed');
          }
        }

        log.info({ chat: payload.chat_name || payload.chat_id, from: payload.sender_name, len: payload.text.length, media: payload.media_type }, 'msg');
        queue.push(payload);
        drain();
      } catch (e) {
        log.error({ err: e.message }, 'failed to handle message');
      }
    }
  });
}

start().catch((e) => {
  log.fatal(e);
  process.exit(1);
});
