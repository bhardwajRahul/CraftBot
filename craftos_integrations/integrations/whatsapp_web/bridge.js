#!/usr/bin/env node
/**
 * CraftBot WhatsApp Bridge
 *
 * Standalone Node.js process that wraps whatsapp-web.js and communicates
 * with the Python agent via stdin/stdout JSON lines.
 *
 * Protocol:
 *   Python → Node (stdin):  JSON command per line
 *     { "id": "req_1", "cmd": "send_message", "args": { "to": "...", "text": "..." } }
 *
 *   Node → Python (stdout): JSON event/response per line
 *     { "type": "event", "event": "message", "data": { ... } }
 *     { "type": "response", "id": "req_1", "data": { ... } }
 *
 *   Logs go to stderr so they don't interfere with the JSON protocol.
 */

const { Client, LocalAuth } = require("whatsapp-web.js");
const qrcode = require("qrcode");
const path = require("path");
const readline = require("readline");

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function log(...args) {
  process.stderr.write(`[WA-Bridge] ${args.join(" ")}\n`);
}

/** Send a JSON line to stdout (Python reads this). */
function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

/** Send an event to Python. */
function emitEvent(event, data = {}) {
  emit({ type: "event", event, data });
}

/** Send a command response to Python. */
function emitResponse(id, data = {}) {
  emit({ type: "response", id, data });
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const AUTH_DIR = process.argv[2] || path.join(process.cwd(), ".credentials", "whatsapp_wwebjs_auth");

log(`Auth directory: ${AUTH_DIR}`);

// ---------------------------------------------------------------------------
// WhatsApp Client
// ---------------------------------------------------------------------------

// We deliberately do NOT pin a webVersionCache. Pinning ties us to a
// snapshot from wppconnect-team/wa-version, which (a) prunes old entries
// after a few months → 404 → ``Runtime.callFunctionOn timed out`` during
// init, and (b) drifts away from whatever wwebjs's internal selectors
// actually expect → ``authenticated`` fires but ``ready`` never does, so
// the synthetic-ready fallback kicks in but messages don't actually flow
// because wwebjs's internal listeners haven't attached.
//
// Without webVersionCache, wwebjs loads web.whatsapp.com directly, using
// the same JS that the user's actual browser uses. That tracks WhatsApp's
// current build and matches wwebjs's selectors most reliably. If a future
// WhatsApp update breaks wwebjs's selectors, the fix is to bump the
// ``whatsapp-web.js`` package version, not to re-introduce a pinned HTML
// that will go stale a few months later.

// ``client`` is module-level + ``let`` (not ``const``) so the watchdog/retry
// path can replace it with a fresh instance after a stuck-init recovery.
// Command handlers below reference ``client`` lazily — they always pick up
// the current binding.
let client;

function buildClient() {
  return new Client({
    authStrategy: new LocalAuth({ dataPath: AUTH_DIR }),
    puppeteer: {
      headless: true,
      protocolTimeout: 120000,
      args: [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-background-timer-throttling",
      ],
    },
  });
}

// Track message IDs sent by us so we can skip them in message_create
const ownSentIds = new Set();
let isReady = false;
let catchupDone = false;
let readyTimestamp = 0; // Unix timestamp (seconds) when client became ready
let ownerPhone = "";
let ownerName = "";
let selfChatId = "";

// ---------------------------------------------------------------------------
// Client Events
// ---------------------------------------------------------------------------

// Attach all wwebjs event handlers to ``c``. Called once per buildClient() —
// the watchdog/retry path re-runs this against the freshly built client so
// every retry has the same wiring.
function attachHandlers(c) {

c.on("qr", async (qr) => {
  log("QR code received");
  try {
    const dataUrl = await qrcode.toDataURL(qr);
    emitEvent("qr", { qr_string: qr, qr_data_url: dataUrl });
  } catch (err) {
    emitEvent("qr", { qr_string: qr, qr_data_url: null });
  }
});

c.on("authenticated", () => {
  log("Authenticated");
  authedThisAttempt = true;
  if (initWatchdog) { clearTimeout(initWatchdog); initWatchdog = null; }
  emitEvent("authenticated");

  // Ready-watchdog: when wwebjs's selectors drift from what WhatsApp's
  // current bundle exposes, ``authenticated`` fires but ``ready`` never
  // does — and crucially wwebjs's internal message listeners don't attach,
  // so messages don't flow. We wait 60s for the real ``ready``; if it
  // doesn't arrive, we treat it as a stuck-init failure and reuse the
  // existing watchdog/retry path (destroy → rebuild → reinitialize).
  // Only after the retry budget is exhausted do we fall through to a
  // synthetic ``ready`` so sends still work — receive will be broken in
  // that fallback state, but the bridge is at least usable for outbound.
  setTimeout(async () => {
    if (isReady) return;
    if (initAttempt <= MAX_INIT_RETRIES) {
      log(`'ready' not received within 60s of authenticated — treating as stuck init, retrying (attempt ${initAttempt}/${MAX_INIT_RETRIES + 1})`);
      initAttempt += 1;
      try { await client.destroy(); } catch (err) { log(`destroy during ready-retry: ${err.message}`); }
      client = buildClient();
      attachHandlers(client);
      authedThisAttempt = false;
      startClientWithWatchdog();
      return;
    }
    // Retry budget exhausted — fall through to synthetic so sends still work.
    log("'ready' not received and retries exhausted — synthesizing (sends only, receive will not work)");
    try {
      if (client.info && client.info.wid) {
        ownerPhone = client.info.wid.user || ownerPhone;
        ownerName = client.info.pushname || ownerName;
      }
    } catch (_) { /* best-effort */ }
    isReady = true;
    readyTimestamp = Math.floor(Date.now() / 1000);
    emitEvent("ready", {
      owner_phone: ownerPhone,
      owner_name: ownerName,
      wid: client.info?.wid?._serialized || "",
      synthetic: true,
    });
    emitEvent("error", { message: "ready event never fired — message receive will not work. Try restarting the agent or updating whatsapp-web.js.", fatal: false });
  }, 60_000);
});

c.on("auth_failure", (msg) => {
  log(`Auth failure: ${msg}`);
  emitEvent("auth_failure", { message: String(msg) });
});

c.on("ready", async () => {
  isReady = true;
  readyTimestamp = Math.floor(Date.now() / 1000);
  log("Client ready");

  // Extract owner phone
  try {
    if (client.info && client.info.wid) {
      ownerPhone = client.info.wid.user || "";
      ownerName = client.info.pushname || "";
      log(`Connected as +${ownerPhone} (${ownerName})`);
      // Discover self-chat ID (may be @lid or @c.us)
      try {
        const ownJid = client.info.wid._serialized;
        const selfChat = await client.getChatById(ownJid);
        selfChatId = selfChat?.id?._serialized || ownJid;
        log(`Self-chat ID: ${selfChatId}`);
      } catch (e) {
        selfChatId = client.info.wid._serialized;
        log(`Self-chat fallback to wid: ${selfChatId}`);
      }
    }
  } catch (err) {
    log(`Could not extract owner info: ${err.message}`);
  }

  emitEvent("ready", {
    owner_phone: ownerPhone,
    owner_name: ownerName,
    wid: client.info?.wid?._serialized || "",
  });

  // Catch-up: send current unread chats
  try {
    const chats = await client.getChats();
    const unread = [];
    for (const chat of chats) {
      if (chat.unreadCount > 0) {
        unread.push({
          id: chat.id._serialized,
          name: chat.name || chat.id._serialized,
          unread_count: chat.unreadCount,
          is_group: chat.isGroup,
          is_muted: chat.isMuted,
        });
      }
    }
    emitEvent("catchup", { unread_chats: unread });
    catchupDone = true;
    log(`Catchup complete: ${unread.length} unread chat(s)`);
  } catch (err) {
    log(`Catchup error: ${err.message}`);
    catchupDone = true; // proceed anyway
  }
});

c.on("disconnected", (reason) => {
  isReady = false;
  catchupDone = false;
  readyTimestamp = 0;
  log(`Disconnected: ${reason}`);
  emitEvent("disconnected", { reason: String(reason) });
});

// ---------------------------------------------------------------------------
// Message Events
// ---------------------------------------------------------------------------

c.on("message", async (msg) => {
  // Skip messages from before the bridge was ready (historical sync)
  if (msg.timestamp && msg.timestamp < readyTimestamp) return;

  try {
    const chat = await msg.getChat();
    const contact = await msg.getContact();

    emitEvent("message", {
      id: msg.id._serialized,
      from: msg.from,
      to: msg.to,
      body: msg.body || "",
      timestamp: msg.timestamp,
      from_me: msg.fromMe,
      type: msg.type,
      has_media: msg.hasMedia,
      is_forwarded: msg.isForwarded || false,
      mentioned_ids: msg.mentionedIds || [],
      chat: {
        id: chat.id._serialized,
        name: chat.name || chat.id._serialized,
        is_group: chat.isGroup,
        is_muted: chat.isMuted,
      },
      contact: {
        id: contact.id._serialized,
        name: contact.pushname || contact.name || "",
        number: contact.number || "",
        is_group: contact.isGroup,
      },
    });
  } catch (err) {
    log(`Error handling message: ${err.message}`);
  }
});

c.on("message_create", async (msg) => {
  // Skip messages from before the bridge was ready (historical sync)
  if (msg.timestamp && msg.timestamp < readyTimestamp) return;
  if (!msg.fromMe) return;

  // Skip messages sent by us via the bridge
  const msgId = msg.id?._serialized;
  if (msgId && ownSentIds.has(msgId)) {
    ownSentIds.delete(msgId);
    return;
  }

  try {
    const chat = await msg.getChat();
    const ownJid = client.info?.wid?._serialized || "";
    const isSelfChat = (ownJid && msg.to === ownJid) || (selfChatId && (msg.to === selfChatId || chat.id._serialized === selfChatId));

    emitEvent("message_sent", {
      id: msg.id._serialized,
      from: msg.from,
      to: msg.to,
      body: msg.body || "",
      timestamp: msg.timestamp,
      type: msg.type,
      is_self_chat: isSelfChat,
      chat: {
        id: chat.id._serialized,
        name: chat.name || chat.id._serialized,
        is_group: chat.isGroup,
      },
    });
  } catch (err) {
    log(`Error handling message_create: ${err.message}`);
  }
});

}  // end attachHandlers(c)

// ---------------------------------------------------------------------------
// Init watchdog + retry — auto-recovers from "stuck before authenticated"
//
// Failure mode this protects against: wwebjs's ``client.initialize()`` hangs
// for 2+ minutes during the WhatsApp Web page load (most often when the
// pinned ``webVersionCache`` URL 404s, when leftover Chromium zombies hold
// the auth dir lock, or when WhatsApp pushes a protocol change). The
// "Initialize error: Runtime.callFunctionOn timed out" we see in logs is
// puppeteer's protocolTimeout firing on a wwebjs JS call that never returns.
//
// Strategy: set a 60s watchdog when initialize() is called. If we don't
// reach the ``authenticated`` event within that window, kill Chromium with
// ``client.destroy()``, build a fresh client, re-attach handlers, and
// re-run initialize. After ``MAX_INIT_RETRIES`` failures we emit a fatal
// error and exit non-zero so the Python parent can decide what to do (in
// practice it logs and continues without WhatsApp).
// ---------------------------------------------------------------------------

const MAX_INIT_RETRIES = 2;
const INIT_WATCHDOG_MS = 60_000;
let initAttempt = 0;
let authedThisAttempt = false;
let initWatchdog = null;

async function startClientWithWatchdog() {
  initAttempt += 1;
  authedThisAttempt = false;

  // Cancel any prior watchdog before arming a new one (defensive — should
  // already be cleared by the time we get here).
  if (initWatchdog) clearTimeout(initWatchdog);

  initWatchdog = setTimeout(async () => {
    if (authedThisAttempt) return;  // raced with the auth event
    log(`Stuck before 'authenticated' for ${INIT_WATCHDOG_MS / 1000}s — recovering (attempt ${initAttempt})`);
    if (initAttempt > MAX_INIT_RETRIES) {
      log(`Max init retries reached — bridge giving up`);
      emitEvent("error", { message: "WhatsApp bridge stuck before authentication after retries", fatal: true });
      try { await client.destroy(); } catch (_) {}
      process.exit(1);
    }
    // Tear down the dead Chromium and try fresh
    try { await client.destroy(); } catch (err) { log(`destroy during retry: ${err.message}`); }
    client = buildClient();
    attachHandlers(client);
    startClientWithWatchdog();
  }, INIT_WATCHDOG_MS);

  log(`Initializing WhatsApp client... (attempt ${initAttempt}/${MAX_INIT_RETRIES + 1})`);
  try {
    await client.initialize();
  } catch (err) {
    if (initWatchdog) { clearTimeout(initWatchdog); initWatchdog = null; }
    log(`Initialize error: ${err.message}`);
    if (initAttempt > MAX_INIT_RETRIES) {
      emitEvent("error", { message: err.message, fatal: true });
      process.exit(1);
    }
    try { await client.destroy(); } catch (_) {}
    client = buildClient();
    attachHandlers(client);
    return startClientWithWatchdog();
  }
}

// ---------------------------------------------------------------------------
// Command Handler (stdin)
// ---------------------------------------------------------------------------

async function handleCommand(line) {
  let parsed;
  try {
    parsed = JSON.parse(line);
  } catch {
    log(`Invalid JSON: ${line}`);
    return;
  }

  const { id, cmd, args } = parsed;

  try {
    switch (cmd) {
      case "send_message": {
        if (!isReady) {
          emitResponse(id, { success: false, error: "Client not ready" });
          return;
        }
        let chatId;
        if (args.to.includes("@")) {
          chatId = args.to;
        } else {
          // Resolve number → canonical JID via the server. WhatsApp's
          // LID-based protocol means a locally-constructed `${num}@c.us`
          // can fail with "No LID for user" for contacts the local Store
          // has never seen. getNumberId() primes the LID mapping and
          // also returns null for numbers not on WhatsApp.
          const cleanNum = args.to.replace(/[\s\-\+\(\)]/g, "");
          const wid = await client.getNumberId(cleanNum);
          if (!wid) {
            emitResponse(id, {
              success: false,
              error: `Number ${cleanNum} is not on WhatsApp`,
            });
            return;
          }
          chatId = wid._serialized;
        }
        const sent = await client.sendMessage(chatId, args.text);
        if (sent?.id?._serialized) ownSentIds.add(sent.id._serialized);
        emitResponse(id, {
          success: true,
          message_id: sent?.id?._serialized || null,
          timestamp: new Date().toISOString(),
        });
        break;
      }

      case "get_status": {
        emitResponse(id, {
          success: true,
          ready: isReady,
          owner_phone: ownerPhone,
          owner_name: ownerName,
          wid: client.info?.wid?._serialized || "",
        });
        break;
      }

      case "get_chats": {
        if (!isReady) {
          emitResponse(id, { success: false, error: "Client not ready" });
          return;
        }
        const chats = await client.getChats();
        const result = chats.slice(0, args.limit || 50).map((c) => ({
          id: c.id._serialized,
          name: c.name || c.id._serialized,
          is_group: c.isGroup,
          is_muted: c.isMuted,
          unread_count: c.unreadCount,
          last_message: c.lastMessage?.body || "",
          timestamp: c.lastMessage?.timestamp || 0,
        }));
        emitResponse(id, { success: true, chats: result });
        break;
      }

      case "get_chat_messages": {
        if (!isReady) {
          emitResponse(id, { success: false, error: "Client not ready" });
          return;
        }
        const chatId = args.chat_id.includes("@")
          ? args.chat_id
          : `${args.chat_id}@c.us`;
        const chat = await client.getChatById(chatId);
        const messages = await chat.fetchMessages({ limit: args.limit || 50 });
        const result = messages.map((m) => ({
          id: m.id._serialized,
          body: m.body || "",
          from: m.from,
          from_me: m.fromMe,
          timestamp: m.timestamp,
          type: m.type,
          has_media: m.hasMedia,
        }));
        emitResponse(id, { success: true, messages: result });
        break;
      }

      case "search_contact": {
        // Strategy: search chats first (fast, robust, covers the
        // overwhelming case of "find someone I've messaged"). Only if
        // that returns nothing do we fall back to filtering the full
        // address book inside the browser page. We can't use
        // client.getContacts() here — on large accounts the per-contact
        // RPC serialization exceeds Puppeteer's protocolTimeout.
        if (!isReady) {
          emitResponse(id, { success: false, error: "Client not ready" });
          return;
        }
        const query = (args.name || "").toLowerCase();

        const chats = await client.getChats();
        let matches = chats
          .filter((ch) => {
            const name = (ch.name || "").toLowerCase();
            const number = (ch.id && ch.id.user) || "";
            return name.includes(query) || number.includes(query);
          })
          .slice(0, 20)
          .map((ch) => {
            const serialized = ch.id._serialized;
            // LID-based chats don't have a phone number — ch.id.user is
            // the LID's user portion, which fails as a `to` value in
            // send_message. Surface the full JID instead so the agent
            // round-trips a valid send target through `number`.
            const isLid = serialized.endsWith("@lid");
            return {
              id: serialized,
              name: ch.name || "",
              number: isLid ? serialized : ((ch.id && ch.id.user) || ""),
              is_group: ch.isGroup,
            };
          });

        if (matches.length === 0) {
          // Fallback: reach into the page's Store. Filter runs in-page
          // so only the matches cross the RPC boundary.
          try {
            matches = await client.pupPage.evaluate((q) => {
              const query = (q || "").toLowerCase();
              return window.Store.Contact.getModelsArray()
                .filter((c) => {
                  const name = (c.pushname || c.name || c.formattedName || "").toLowerCase();
                  const number = (c.id && c.id.user) || "";
                  return name.includes(query) || number.includes(query);
                })
                .slice(0, 20)
                .map((c) => {
                  const serialized = c.id._serialized;
                  const isLid = serialized.endsWith("@lid");
                  return {
                    id: serialized,
                    name: c.pushname || c.name || c.formattedName || "",
                    number: isLid ? serialized : ((c.id && c.id.user) || ""),
                    is_group: c.isGroup,
                  };
                });
            }, args.name || "");
          } catch (err) {
            emitResponse(id, {
              success: false,
              error: `In-page contact filter failed: ${err.message}`,
            });
            return;
          }
        }

        emitResponse(id, { success: true, contacts: matches });
        break;
      }

      case "get_unread_chats": {
        if (!isReady) {
          emitResponse(id, { success: false, error: "Client not ready" });
          return;
        }
        const allChats = await client.getChats();
        const unreadChats = allChats
          .filter((c) => c.unreadCount > 0)
          .map((c) => ({
            id: c.id._serialized,
            name: c.name || c.id._serialized,
            unread_count: c.unreadCount,
            is_group: c.isGroup,
            is_muted: c.isMuted,
          }));
        emitResponse(id, { success: true, unread_chats: unreadChats });
        break;
      }

      case "shutdown": {
        log("Shutdown requested");
        emitResponse(id, { success: true });
        await gracefulShutdown();
        break;
      }

      case "logout": {
        // Full disconnect: logs out of WhatsApp server-side AND wipes the
        // LocalAuth data on disk, so the next connect demands a fresh QR.
        // Without this, ``client.destroy()`` alone leaves the session
        // restorable and the bridge auto-reconnects on next start.
        log("Logout requested");
        emitResponse(id, { success: true });
        try {
          if (client) await client.logout();
          log("Logged out");
        } catch (err) {
          log(`Logout error: ${err.message}`);
          // Fall through to destroy/exit — even a partial logout is
          // better than leaving the bridge running.
          try { if (client) await client.destroy(); } catch (_) {}
        }
        process.exit(0);
        break;
      }

      default:
        emitResponse(id, { success: false, error: `Unknown command: ${cmd}` });
    }
  } catch (err) {
    log(`Command error (${cmd}): ${err.message}`);
    emitResponse(id, { success: false, error: err.message });
  }
}

// ---------------------------------------------------------------------------
// Stdin reader
// ---------------------------------------------------------------------------

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", (line) => {
  const trimmed = line.trim();
  if (trimmed) handleCommand(trimmed);
});

rl.on("close", () => {
  log("stdin closed, shutting down");
  gracefulShutdown();
});

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

async function gracefulShutdown() {
  log("Shutting down...");
  try {
    if (client) await client.destroy();
  } catch (err) {
    log(`Destroy error: ${err.message}`);
  }
  process.exit(0);
}

process.on("SIGINT", gracefulShutdown);
process.on("SIGTERM", gracefulShutdown);

// Start: build the initial client, attach handlers, run with watchdog.
// startClientWithWatchdog() handles its own retries + final exit on failure.
client = buildClient();
attachHandlers(client);
startClientWithWatchdog();
