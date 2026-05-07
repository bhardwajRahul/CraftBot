# -*- coding: utf-8 -*-
"""Lark integration — send-only via REST.

Lark is ByteDance's enterprise messaging platform (the China-region twin
``Feishu`` shares the API; the only difference is the API host). This v1
integration targets **global Lark** (``open.larksuite.com``) with a
custom-app, tenant-access-token auth flow — no long-poll listener, send
methods only.

Long-poll receive is supported by Lark's official ``lark-oapi`` Python
SDK and could be added later as v2 for two-way chat. For v1 the goal is
"CraftBot can post to my Lark workspace", which is the typical
notification use case.

Auth flow:
  1. User creates a Custom App at ``open.larksuite.com/app``.
  2. Grabs App ID + App Secret from the app's Credentials tab.
  3. CraftBot mints a ``tenant_access_token`` via the
     ``/open-apis/auth/v3/tenant_access_token/internal`` endpoint and
     refreshes it before the 2-hour expiry on every send.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .. import (
    BasePlatformClient,
    IntegrationHandler,
    IntegrationSpec,
    PlatformMessage,
    has_credential,
    load_credential,
    register_client,
    register_handler,
    remove_credential,
    save_credential,
)
from ..helpers import Result, request as http_request
from ..logger import get_logger

logger = get_logger(__name__)

LARK_API_BASE = "https://open.larksuite.com/open-apis"


@dataclass
class LarkCredential:
    app_id: str = ""
    app_secret: str = ""
    bot_name: str = ""
    bot_open_id: str = ""
    # Cached tenant_access_token + its absolute Unix expiry. Saved so we
    # don't mint a new token on every restart, but always refresh when
    # within 60s of expiry (defensive — Lark's clocks vs ours).
    tenant_access_token: str = ""
    token_expires_at: float = 0.0


LARK = IntegrationSpec(
    name="lark",
    cred_class=LarkCredential,
    cred_file="lark.json",
    platform_id="lark",
)


# ════════════════════════════════════════════════════════════════════════
# Handler
# ════════════════════════════════════════════════════════════════════════

@register_handler(LARK.name)
class LarkHandler(IntegrationHandler):
    spec = LARK
    display_name = "Lark"
    description = "Messaging via Lark (send-only)"
    auth_type = "token"
    icon = "lark"
    connect_help = [
        "Open Lark Developer Console: open.larksuite.com/app",
        "Sign in with your Lark account",
        "Click 'Create Custom App', give it a name, click Create",
        "App ID + App Secret → 'Credentials & Basic Info' tab",
        "Permissions tab → enable: im:message, im:message.group_at_msg, im:message.p2p_msg",
        "Events & Callbacks tab → enable Bot capability",
        "Version Management → create a version, submit for tenant admin approval",
    ]
    fields = [
        {"key": "app_id", "label": "App ID",
         "placeholder": "cli_xxxxxxxxxx", "password": False},
        {"key": "app_secret", "label": "App Secret",
         "placeholder": "From Credentials tab", "password": True},
    ]

    async def login(self, args: List[str]) -> Tuple[bool, str]:
        if len(args) < 2:
            return False, ("Usage: /lark login <app_id> <app_secret>\n"
                           "Get from open.larksuite.com/app → your app → Credentials tab.")
        app_id, app_secret = args[0], args[1]

        # Validate by minting a tenant_access_token. Lark returns code != 0
        # when credentials are wrong (200 OK with a JSON error body).
        result = http_request(
            "POST", f"{LARK_API_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            expected=(200,),
        )
        if "error" in result:
            return False, f"Lark auth request failed: {result['error']}"
        body = result.get("result", {})
        if body.get("code", -1) != 0:
            return False, f"Invalid Lark credentials: {body.get('msg', 'unknown error')}"

        token = body.get("tenant_access_token", "")
        expire = float(body.get("expire", 0))
        token_expires_at = time.time() + expire

        # Best-effort: fetch bot info so we can show the bot name in status().
        # Falls back gracefully if the call fails (e.g. bot capability not
        # enabled yet on the app).
        bot_name = ""
        bot_open_id = ""
        info = http_request(
            "GET", f"{LARK_API_BASE}/bot/v3/info",
            headers={"Authorization": f"Bearer {token}"},
            expected=(200,),
        )
        if "error" not in info:
            bot = info.get("result", {}).get("bot", {})
            bot_name = bot.get("app_name", "")
            bot_open_id = bot.get("open_id", "")

        save_credential(self.spec.cred_file, LarkCredential(
            app_id=app_id, app_secret=app_secret,
            bot_name=bot_name, bot_open_id=bot_open_id,
            tenant_access_token=token, token_expires_at=token_expires_at,
        ))
        label = bot_name or app_id
        return True, f"Lark connected: {label}"

    async def logout(self, args: List[str]) -> Tuple[bool, str]:
        if not has_credential(self.spec.cred_file):
            return False, "No Lark credentials found."
        remove_credential(self.spec.cred_file)
        return True, "Removed Lark credential."

    async def status(self) -> Tuple[bool, str]:
        if not has_credential(self.spec.cred_file):
            return True, "Lark: Not connected"
        cred = load_credential(self.spec.cred_file, LarkCredential)
        if not cred:
            return True, "Lark: Connected\n  - app configured"
        name = cred.bot_name or "Lark bot"
        ident = cred.bot_open_id or cred.app_id or "app"
        return True, f"Lark: Connected\n  - {name} ({ident})"


# ════════════════════════════════════════════════════════════════════════
# Client
# ════════════════════════════════════════════════════════════════════════

@register_client
class LarkClient(BasePlatformClient):
    spec = LARK
    PLATFORM_ID = LARK.platform_id

    def __init__(self) -> None:
        super().__init__()
        self._cred: Optional[LarkCredential] = None
        # WebSocket listener state. ``_ws_client`` is the lark_oapi.ws.Client
        # instance; ``_ws_thread`` is the daemon thread it runs in (the SDK's
        # ``start()`` is blocking so we can't await it). ``_dispatch_loop``
        # captures the agent's asyncio loop at start_listening time so the
        # synchronous WS-thread handler can ``run_coroutine_threadsafe`` back
        # onto the right loop when forwarding messages to the agent.
        self._ws_client: Optional[Any] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._dispatch_loop: Optional[asyncio.AbstractEventLoop] = None

    def has_credentials(self) -> bool:
        return has_credential(self.spec.cred_file)

    def _load(self) -> LarkCredential:
        if self._cred is None:
            self._cred = load_credential(self.spec.cred_file, LarkCredential)
        if self._cred is None:
            raise RuntimeError("No Lark credentials. Use /lark login first.")
        return self._cred

    def _ensure_token(self) -> str:
        """Return a valid tenant_access_token, refreshing if within 60s of expiry.

        Persists the refreshed token back to disk so other processes
        (or a restart) reuse it instead of minting fresh.
        """
        cred = self._load()
        now = time.time()
        if cred.tenant_access_token and cred.token_expires_at > now + 60:
            return cred.tenant_access_token
        result = http_request(
            "POST", f"{LARK_API_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": cred.app_id, "app_secret": cred.app_secret},
            expected=(200,),
        )
        if "error" in result:
            raise RuntimeError(f"Lark token refresh failed: {result['error']}")
        body = result.get("result", {})
        if body.get("code", -1) != 0:
            raise RuntimeError(f"Lark token refresh rejected: {body.get('msg', 'unknown')}")
        token = body.get("tenant_access_token", "")
        expire = float(body.get("expire", 0))
        cred.tenant_access_token = token
        cred.token_expires_at = now + expire
        save_credential(self.spec.cred_file, cred)
        return token

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._ensure_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    async def connect(self) -> None:
        self._load()
        self._connected = True

    async def send_message(self, recipient: str, text: str, **kwargs) -> Result:
        # Default to open_id for the receive_id type — covers DM-to-user case.
        # Callers that need group/email/user_id can use ``send_text`` directly.
        receive_id_type = kwargs.get("receive_id_type", "open_id")
        return self.send_text(recipient, text, receive_id_type=receive_id_type)

    @property
    def supports_listening(self) -> bool:
        return True

    # ----- Listener (WebSocket via lark_oapi SDK) -----

    async def start_listening(self, callback) -> None:
        """Open a Long-Connection WebSocket to Lark and forward messages.

        Uses ``lark_oapi.ws.Client`` which is the official SDK from ByteDance.
        The SDK's ``start()`` is blocking + synchronous, so we run it on a
        daemon thread and dispatch events back to the agent's asyncio loop
        via ``run_coroutine_threadsafe``.

        Auto-reconnect is enabled in the SDK — we don't need our own retry
        loop. If the network drops, the SDK reconnects with backoff.
        """
        if self._listening:
            return
        try:
            import lark_oapi as lark
        except ImportError:
            raise RuntimeError(
                "lark-oapi not installed. Run: pip install lark-oapi"
            )

        cred = self._load()
        self._message_callback = callback
        self._dispatch_loop = asyncio.get_running_loop()

        def _on_message(event: Any) -> None:
            """Synchronous handler running on the SDK's WS thread."""
            try:
                msg = event.event.message
                sender = event.event.sender
            except AttributeError as e:
                logger.warning(f"[LARK] unexpected event shape: {e}")
                return
            # Bounce back onto the agent's loop for the actual dispatch
            loop = self._dispatch_loop
            if loop and not loop.is_closed():
                asyncio.run_coroutine_threadsafe(
                    self._dispatch_message(msg, sender), loop,
                )

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(_on_message)
            .build()
        )
        self._ws_client = lark.ws.Client(
            cred.app_id, cred.app_secret,
            event_handler=handler,
            domain="https://open.larksuite.com",
            auto_reconnect=True,
            log_level=lark.LogLevel.WARNING,
        )

        def _run_ws() -> None:
            try:
                self._ws_client.start()
            except Exception as e:
                logger.error(f"[LARK] WS client crashed: {e}")

        self._ws_thread = threading.Thread(
            target=_run_ws, name="lark-ws", daemon=True,
        )
        self._ws_thread.start()
        self._listening = True
        logger.info("[LARK] Started WebSocket listener")

    async def stop_listening(self) -> None:
        if not self._listening:
            return
        self._listening = False
        # The SDK doesn't expose a clean ``stop()`` — the WS thread is a
        # daemon, so it dies when the agent process exits. For mid-run
        # disconnect (logout) we drop our reference; the SDK's reconnect
        # loop will keep running but no callback will fire (because
        # ``_dispatch_loop`` is gone after this method).
        self._ws_client = None
        self._ws_thread = None
        self._dispatch_loop = None
        logger.info("[LARK] Stopped WebSocket listener")

    async def _dispatch_message(self, msg: Any, sender: Any) -> None:
        """Convert a Lark P2ImMessageReceiveV1 event into a PlatformMessage."""
        if not self._listening or not self._message_callback:
            return

        # Skip messages sent by the bot itself (Lark echoes our sends back).
        cred = self._cred
        bot_open_id = cred.bot_open_id if cred else ""
        sender_id = getattr(sender, "sender_id", None)
        sender_open_id = getattr(sender_id, "open_id", "") if sender_id else ""
        if bot_open_id and sender_open_id == bot_open_id:
            return

        # Lark message ``content`` is a JSON-encoded string. Text messages:
        # ``{"text": "Hello"}``. Other types (post/image/file) we surface as
        # the raw JSON for now — agent decides what to do with them.
        msg_type = getattr(msg, "message_type", "") or ""
        raw_content = getattr(msg, "content", "") or ""
        text = ""
        try:
            parsed = json.loads(raw_content) if raw_content else {}
            if msg_type == "text":
                text = parsed.get("text", "")
            else:
                text = raw_content  # surface raw JSON for non-text types
        except (json.JSONDecodeError, ValueError):
            text = raw_content

        if not text:
            return

        ts: Optional[datetime] = None
        try:
            create_time = getattr(msg, "create_time", None)
            if create_time:
                # Lark's create_time is millis since epoch as a string
                ts = datetime.fromtimestamp(int(create_time) / 1000.0, tz=timezone.utc)
        except (TypeError, ValueError):
            pass

        chat_id = getattr(msg, "chat_id", "") or ""
        message_id = getattr(msg, "message_id", "") or ""
        chat_type = getattr(msg, "chat_type", "") or ""

        await self._message_callback(PlatformMessage(
            platform=self.spec.platform_id,
            sender_id=sender_open_id or "unknown",
            sender_name=sender_open_id or "Lark user",
            text=text,
            channel_id=chat_id,
            channel_name=f"Lark {chat_type}" if chat_type else "Lark",
            message_id=message_id,
            timestamp=ts,
            raw={
                "source": "Lark", "integrationType": "lark",
                "is_self_message": False, "is_group": chat_type == "group",
                "chat_id": chat_id, "chat_type": chat_type,
                "message_type": msg_type, "raw_content": raw_content,
            },
        ))

    # ----- REST methods -----

    def send_text(self, receive_id: str, text: str,
                  receive_id_type: str = "open_id") -> Result:
        """Send a text message.

        ``receive_id_type`` selects how Lark interprets ``receive_id``:
          - ``open_id`` — a user's open_id (default, returned by user lookup)
          - ``user_id`` — Lark's enterprise user_id
          - ``email`` — the user's company email
          - ``chat_id`` — a group chat id (oc_...)
          - ``union_id`` — cross-app stable id

        Lark's API quirk: the ``content`` field must be a JSON-encoded
        STRING, not an object. Hence the literal ``\"`` escaping below.
        """
        import json as _json
        payload = {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": _json.dumps({"text": text}, ensure_ascii=False),
        }
        return http_request(
            "POST", f"{LARK_API_BASE}/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            headers=self._headers(), json=payload, expected=(200,),
            transform=lambda d: d.get("data", d),
        )

    def reply_text(self, message_id: str, text: str) -> Result:
        """Threaded reply to an existing message id (om_...)."""
        import json as _json
        return http_request(
            "POST", f"{LARK_API_BASE}/im/v1/messages/{message_id}/reply",
            headers=self._headers(),
            json={"msg_type": "text", "content": _json.dumps({"text": text}, ensure_ascii=False)},
            expected=(200,),
            transform=lambda d: d.get("data", d),
        )

    def get_user_by_email(self, email: str) -> Result:
        """Resolve a user's open_id from a company email.

        Uses Lark's batch-get endpoint with a single email; convenient
        for "send a message to alice@company.com" workflows where the
        caller doesn't know the open_id."""
        return http_request(
            "POST", f"{LARK_API_BASE}/contact/v3/users/batch_get_id",
            params={"user_id_type": "open_id"},
            headers=self._headers(), json={"emails": [email]}, expected=(200,),
            transform=lambda d: d.get("data", d),
        )

    def list_chats(self, page_size: int = 50) -> Result:
        """List groups the bot is a member of."""
        return http_request(
            "GET", f"{LARK_API_BASE}/im/v1/chats",
            params={"page_size": min(page_size, 100)},
            headers=self._headers(), expected=(200,),
            transform=lambda d: d.get("data", d),
        )

    def get_bot_info(self) -> Result:
        """Connected bot's own profile (app_name, open_id, etc.)."""
        return http_request(
            "GET", f"{LARK_API_BASE}/bot/v3/info",
            headers=self._headers(), expected=(200,),
            transform=lambda d: d.get("bot", d),
        )
