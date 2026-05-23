/**
 * Zoro WhatsApp bridge.
 *
 * Runs whatsapp-web.js with a persistent local session and exposes a
 * tiny HTTP API on 127.0.0.1 so the Python side (src/zoro_whatsapp.py)
 * can ask "what's unread" / "send this message" without speaking the
 * WhatsApp protocol itself.
 *
 * Session is stored at ../../data/whatsapp-session — first run prints
 * a QR code in the terminal that you scan with WhatsApp on your phone;
 * subsequent runs reuse the cached session and require no scan.
 *
 * Endpoints (all JSON):
 *   GET  /status         → {status, hasQR}
 *   GET  /qr             → {qr}  (raw string; only while not ready)
 *   GET  /unread?limit=N → {chats: [...]}
 *   GET  /last_incoming  → {message}  (or null)
 *   POST /send {to,text} → {ok, message_id, chat_id}
 *      `to` may be a chat id (NNN@c.us / NNN@g.us) OR a name to fuzzy-match
 *      against existing chats.
 *   POST /call {to,video,method} → {ok, method, ...}
 *      Initiate a WhatsApp voice (video:false) or video (video:true) call.
 *      `to` resolves like /send. `method` is "auto" (default) | "native" |
 *      "link":
 *        - native: ring the contact directly via the WhatsApp Web internal
 *          call store (puppeteer eval; experimental, depends on the WA Web
 *          build exposing a call-start module).
 *        - link:   generate a joinable call link and send it to the contact
 *          as a message (uses the supported createCallLink API; reliable).
 *        - auto:   try native, fall back to link.
 *   POST /call_link {type} → {ok, link, type}
 *      Generate a shareable WhatsApp call link without sending it.
 *      `type` is "voice" (default) | "video".
 *
 * Env vars: ZORO_WHATSAPP_PORT (default 8767).
 */
'use strict';

const http = require('http');
const path = require('path');
const url = require('url');

const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');

const PORT = parseInt(process.env.ZORO_WHATSAPP_PORT || '8767', 10);
const SESSION_DIR = path.join(__dirname, '..', '..', 'data', 'whatsapp-session');
const HISTORY_LIMIT = 50;

let status = 'starting';
let lastQR = null;
const recentIncoming = []; // newest first

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: SESSION_DIR }),
  puppeteer: {
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
  },
});

client.on('qr', (qr) => {
  status = 'qr';
  lastQR = qr;
  console.log('[whatsapp] scan this QR with WhatsApp on your phone:');
  qrcode.generate(qr, { small: true });
});

client.on('authenticated', () => {
  console.log('[whatsapp] authenticated');
});

client.on('auth_failure', (m) => {
  status = 'auth_failure';
  console.error('[whatsapp] auth failure:', m);
});

client.on('ready', () => {
  status = 'ready';
  lastQR = null;
  console.log('[whatsapp] client ready');
});

client.on('disconnected', (reason) => {
  status = 'disconnected';
  console.log(`[whatsapp] disconnected: ${reason}`);
});

client.on('message', async (msg) => {
  // Skip messages we sent ourselves.
  if (msg.fromMe) return;
  try {
    const contact = await msg.getContact();
    recentIncoming.unshift({
      id: msg.id._serialized,
      from: msg.from,
      name:
        contact.pushname || contact.name || contact.number || msg.from,
      body: msg.body || '[media]',
      ts: msg.timestamp * 1000,
      is_group: msg.from.endsWith('@g.us'),
    });
    while (recentIncoming.length > HISTORY_LIMIT) recentIncoming.pop();
  } catch (e) {
    console.warn('[whatsapp] message handler error:', e.message);
  }
});

client.initialize();

// ---- HTTP server ----------------------------------------------------------

function send(res, code, body) {
  res.statusCode = code;
  res.setHeader('Content-Type', 'application/json');
  res.end(JSON.stringify(body));
}

async function resolveChatId(to) {
  if (to.includes('@')) return to;
  const chats = await client.getChats();
  const needle = to.toLowerCase();
  const exact = chats.find((c) => (c.name || '').toLowerCase() === needle);
  if (exact) return exact.id._serialized;
  const sub = chats.find((c) =>
    (c.name || '').toLowerCase().includes(needle),
  );
  if (sub) return sub.id._serialized;
  return null;
}

async function unreadChats(limit) {
  const chats = await client.getChats();
  const unread = chats.filter((c) => c.unreadCount > 0).slice(0, limit);
  const out = [];
  for (const chat of unread) {
    let lastBody = '';
    let lastTs = 0;
    try {
      const msgs = await chat.fetchMessages({ limit: 1 });
      const last = msgs[msgs.length - 1];
      if (last) {
        lastBody = last.body || '[media]';
        lastTs = last.timestamp * 1000;
      }
    } catch (e) {
      // fetchMessages can race during boot; skip body.
    }
    out.push({
      id: chat.id._serialized,
      name: chat.name || chat.id.user,
      is_group: chat.isGroup,
      unread_count: chat.unreadCount,
      last_body: lastBody,
      last_ts: lastTs,
    });
  }
  return out;
}

// ---- Calls ----------------------------------------------------------------

/**
 * Ring a contact directly via the WhatsApp Web internal call store.
 *
 * whatsapp-web.js has no public API for outgoing calls, so we reach into
 * the page's module registry (the same `window.require(...)` mechanism the
 * library uses for everything else). The call-start module name/signature
 * is not stable across WA Web builds, so we probe a few known module names
 * and call-start function names defensively. Returns
 * {ok:true, module, fn} on success or {ok:false, error, tried} otherwise.
 */
async function nativeCall(chatId, isVideo) {
  return client.pupPage.evaluate(
    async (chatId, isVideo) => {
      const tried = [];
      let wid;
      try {
        wid = window.require('WAWebWidFactory').createWid(chatId);
      } catch (e) {
        return { ok: false, error: `wid: ${e.message}`, tried };
      }
      // Group calls aren't supported here; reject group jids early.
      if (chatId.endsWith('@g.us')) {
        return { ok: false, error: 'native call to a group is not supported', tried };
      }

      const moduleNames = [
        'WAWebCallStartFunctions',
        'WAWebApiContactCall',
        'WAWebCallControlState',
        'WAWebApiCall',
      ];
      // Function names that plausibly *start* a call (and not reject/end/query).
      const wantsStart = /(start|place|offer|make|begin|out)/i;
      const skip = /(reject|end|decline|accept|incoming|cancel|terminate|^get|^is|^has|state|listen|subscribe)/i;

      for (const modName of moduleNames) {
        let mod;
        try {
          mod = window.require(modName);
        } catch (e) {
          tried.push(`${modName}:missing`);
          continue;
        }
        if (!mod) {
          tried.push(`${modName}:null`);
          continue;
        }
        const fnNames = Object.keys(mod).filter(
          (k) =>
            typeof mod[k] === 'function' &&
            /call/i.test(k) &&
            wantsStart.test(k) &&
            !skip.test(k),
        );
        for (const fnName of fnNames) {
          // Try the two argument shapes seen across builds.
          try {
            await mod[fnName]([wid], { isVideo: !!isVideo, isGroupCall: false });
            return { ok: true, module: modName, fn: fnName, sig: 'array+opts' };
          } catch (e1) {
            try {
              await mod[fnName](wid, !!isVideo);
              return { ok: true, module: modName, fn: fnName, sig: 'wid+bool' };
            } catch (e2) {
              tried.push(`${modName}.${fnName}:${e2.message}`);
            }
          }
        }
        if (fnNames.length === 0) tried.push(`${modName}:no-start-fn`);
      }
      return { ok: false, error: 'no usable call-start module found', tried };
    },
    chatId,
    isVideo,
  );
}

/**
 * Generate a joinable WhatsApp call link of the given type
 * ("voice" | "video"). Uses the library's supported createCallLink API.
 */
async function makeCallLink(callType) {
  // Start time "now"; WhatsApp treats this as an instant call link.
  return client.createCallLink(new Date(), callType);
}

async function placeCall(to, isVideo, method) {
  const chatId = await resolveChatId(String(to));
  if (!chatId) {
    return { http: 404, body: { error: `no chat matching ${JSON.stringify(to)}` } };
  }
  const callType = isVideo ? 'video' : 'voice';

  // Native ring (default for "auto" and "native").
  if (method === 'auto' || method === 'native') {
    let nat;
    try {
      nat = await nativeCall(chatId, isVideo);
    } catch (e) {
      nat = { ok: false, error: e.message };
    }
    if (nat.ok) {
      return {
        http: 200,
        body: { ok: true, method: 'native', chat_id: chatId, call_type: callType, detail: nat },
      };
    }
    if (method === 'native') {
      return { http: 502, body: { ok: false, error: nat.error || 'native call failed', detail: nat } };
    }
    // else fall through to link
  }

  // Link fallback (and the explicit "link" method): generate + send a link.
  try {
    const link = await makeCallLink(callType);
    if (!link) {
      return { http: 502, body: { ok: false, error: 'failed to generate call link' } };
    }
    const text =
      (isVideo ? '📹 Video call' : '📞 Voice call') + ` — tap to join: ${link}`;
    const sent = await client.sendMessage(chatId, text);
    return {
      http: 200,
      body: {
        ok: true,
        method: 'link',
        chat_id: chatId,
        call_type: callType,
        link,
        message_id: sent.id._serialized,
      },
    };
  } catch (e) {
    return { http: 500, body: { ok: false, error: e.message } };
  }
}

const server = http.createServer(async (req, res) => {
  const parsed = url.parse(req.url, true);
  try {
    if (parsed.pathname === '/status' && req.method === 'GET') {
      return send(res, 200, { status, hasQR: lastQR != null });
    }

    if (parsed.pathname === '/qr' && req.method === 'GET') {
      if (!lastQR) return send(res, 404, { error: 'no QR available' });
      return send(res, 200, { qr: lastQR });
    }

    if (parsed.pathname === '/last_incoming' && req.method === 'GET') {
      return send(res, 200, { message: recentIncoming[0] || null });
    }

    if (parsed.pathname === '/unread' && req.method === 'GET') {
      if (status !== 'ready') {
        return send(res, 503, { error: `not ready (${status})` });
      }
      const limit = parseInt(parsed.query.limit || '10', 10);
      const chats = await unreadChats(limit);
      return send(res, 200, { chats });
    }

    if (parsed.pathname === '/send' && req.method === 'POST') {
      if (status !== 'ready') {
        return send(res, 503, { error: `not ready (${status})` });
      }
      let body = '';
      req.on('data', (c) => (body += c));
      req.on('end', async () => {
        try {
          const { to, text } = JSON.parse(body || '{}');
          if (!to || !text) {
            return send(res, 400, { error: 'to and text required' });
          }
          const chatId = await resolveChatId(String(to));
          if (!chatId) {
            return send(res, 404, {
              error: `no chat matching ${JSON.stringify(to)}`,
            });
          }
          const sent = await client.sendMessage(chatId, String(text));
          return send(res, 200, {
            ok: true,
            message_id: sent.id._serialized,
            chat_id: chatId,
          });
        } catch (e) {
          return send(res, 500, { error: e.message });
        }
      });
      return;
    }

    if (parsed.pathname === '/call' && req.method === 'POST') {
      if (status !== 'ready') {
        return send(res, 503, { error: `not ready (${status})` });
      }
      let body = '';
      req.on('data', (c) => (body += c));
      req.on('end', async () => {
        try {
          const data = JSON.parse(body || '{}');
          const { to } = data;
          if (!to) return send(res, 400, { error: 'to required' });
          const isVideo = !!data.video;
          const method = ['auto', 'native', 'link'].includes(data.method)
            ? data.method
            : 'auto';
          const r = await placeCall(to, isVideo, method);
          return send(res, r.http, r.body);
        } catch (e) {
          return send(res, 500, { error: e.message });
        }
      });
      return;
    }

    if (parsed.pathname === '/call_link' && req.method === 'POST') {
      if (status !== 'ready') {
        return send(res, 503, { error: `not ready (${status})` });
      }
      let body = '';
      req.on('data', (c) => (body += c));
      req.on('end', async () => {
        try {
          const data = JSON.parse(body || '{}');
          const callType = data.type === 'video' ? 'video' : 'voice';
          const link = await makeCallLink(callType);
          if (!link) {
            return send(res, 502, { error: 'failed to generate call link' });
          }
          return send(res, 200, { ok: true, type: callType, link });
        } catch (e) {
          return send(res, 500, { error: e.message });
        }
      });
      return;
    }

    return send(res, 404, { error: 'not found' });
  } catch (e) {
    return send(res, 500, { error: e.message });
  }
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`[whatsapp] HTTP bridge on 127.0.0.1:${PORT}`);
});

function shutdown() {
  console.log('[whatsapp] shutting down');
  try {
    client.destroy();
  } catch (_) {}
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(1), 5000).unref();
}

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
