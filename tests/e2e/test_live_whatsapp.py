"""LIVE end-to-end scenarios for WhatsApp.

One file per integration so test files stay scoped and easy to grow.
Generic infrastructure lives in [_harness/](tests/e2e/_harness/);
WhatsApp-specific verification lives in
[_integrations/whatsapp.py](tests/e2e/_integrations/whatsapp.py).

Each test:

1. Builds the real agent (skips if whatsapp_web isn't connected).
2. Sends a chat message through the same entry point the CraftBot UI uses.
3. After the agent's full lifecycle finishes, verifies the outcome
   black-box:
     - send tests: ``whatsapp.recent_messages_in_self_chat(...)``
     - read tests: ``assert_action_called(agent, <expected action>)``
4. Writes a per-test log file under ``tests/e2e/logs/`` with the agent
   trace + full LLM transcript.

**Opt-in.** Default ``pytest`` runs skip these. Run with::

    python -m pytest tests/e2e/test_live_whatsapp.py -v -m live -s

They spend LLM tokens and send REAL WhatsApp messages to the linked
account's own number.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from tests.e2e._harness import (
    assert_action_called,
    build_agent,
    format_agent_trace,
    run_scenario,
    save_trace_log,
)
from tests.e2e._integrations import whatsapp


pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# Send-side scenarios — verified by querying the wwebjs chat history for an
# outgoing message in the owner's own chat, written during the test window.
# ---------------------------------------------------------------------------


def test_live_whatsapp_send_ping_to_me():
    """User asks the agent to whatsapp themselves. After the agent settles,
    we ask whatsapp directly: 'do you have a recent outgoing message from
    me to me?'. Passes regardless of which code path the agent took.

    Manual verification: a message arrives on your phone.
    """
    agent = build_agent(require=["whatsapp_web"])

    # Round down a couple of seconds for clock-skew safety between Python's
    # time.time() and whatsapp-web.js's message timestamps.
    test_start_ts = time.time() - 2

    async def _run() -> list[dict]:
        await run_scenario(
            agent,
            user_message="send me a whatsapp message saying 'ping from test'",
            wait_for=["whatsapp_web"],
        )
        # Bridge flush before query.
        await asyncio.sleep(2.0)
        return await whatsapp.recent_messages_in_self_chat(since_ts=test_start_ts)

    recent = asyncio.run(_run())
    log_path = save_trace_log(
        agent,
        extra={"recent_messages_in_self_chat": len(recent)},
    )
    print(f"\nagent trace: {log_path}")

    assert recent, (
        f"no outgoing whatsapp message landed in your self-chat after "
        f"the test window. trace: {log_path}\n\n"
        + format_agent_trace(agent)
    )


def test_live_whatsapp_threads_user_content_through():
    """Verifies the agent threads a quoted phrase verbatim instead of
    paraphrasing. The sentinel is unique per run so re-runs don't
    false-positive on prior runs' messages.
    """
    sentinel = f"craftbot-e2e-{int(time.time())}"
    agent = build_agent(require=["whatsapp_web"])
    test_start_ts = time.time() - 2

    async def _run() -> list[dict]:
        await run_scenario(
            agent,
            user_message=f"send me a whatsapp with this exact text: '{sentinel}'",
            wait_for=["whatsapp_web"],
        )
        await asyncio.sleep(2.0)
        return await whatsapp.recent_messages_in_self_chat(
            since_ts=test_start_ts, contains=sentinel,
        )

    recent = asyncio.run(_run())
    log_path = save_trace_log(
        agent,
        extra={"sentinel": sentinel, "matched_messages": len(recent)},
    )
    print(f"\nagent trace: {log_path}")

    assert recent, (
        f"no whatsapp message containing the sentinel arrived. The agent "
        f"may have paraphrased instead of threading the exact phrase. "
        f"sentinel={sentinel!r}. trace: {log_path}\n\n"
        + format_agent_trace(agent)
    )


def test_live_whatsapp_sends_unicode_body():
    """Emoji + non-Latin scripts must survive Python → wwebjs subprocess
    JSON → WhatsApp's API → the bridge's chat history readback. Catches
    encoding bugs anywhere in that chain.
    """
    sentinel = f"héllo 你好 🚀 {int(time.time())}"
    rocket = "🚀"
    agent = build_agent(require=["whatsapp_web"])
    test_start_ts = time.time() - 2

    async def _run() -> list[dict]:
        await run_scenario(
            agent,
            user_message=f"send me a whatsapp with this exact text: '{sentinel}'",
            wait_for=["whatsapp_web"],
        )
        await asyncio.sleep(2.0)
        return await whatsapp.recent_messages_in_self_chat(
            since_ts=test_start_ts, contains=rocket,
        )

    recent = asyncio.run(_run())
    # Log file is UTF-8; keep console print pure ASCII for Windows cp1252.
    log_path = save_trace_log(
        agent,
        extra={"sentinel": sentinel, "matched_with_rocket": len(recent)},
    )
    print(f"\nagent trace: {log_path}")

    assert recent, (
        f"no whatsapp message containing the rocket emoji arrived. The "
        f"wwebjs bridge may be mangling unicode. trace: {log_path}"
    )


# ---------------------------------------------------------------------------
# Read-side scenarios — verified by asserting on which action(s) the agent
# invoked, via the ``execute_action`` spy recorded in
# ``agent._test_actions_called``. No outgoing messages expected.
# ---------------------------------------------------------------------------


def test_live_whatsapp_query_session_status_for_my_number():
    """Asking for one's own whatsapp number should make the agent look it
    up via ``get_whatsapp_web_session_status`` (per the integration's
    ## Essentials block — "Never ask the user for it"). Regression check
    that the essentials reach task-mode prompts.
    """
    agent = build_agent(require=["whatsapp_web"])

    async def _run():
        await run_scenario(
            agent,
            user_message=(
                "what is my own whatsapp phone number? look it up — don't ask me."
            ),
            wait_for=["whatsapp_web"],
        )

    asyncio.run(_run())
    assert_action_called(agent, "get_whatsapp_web_session_status")


def test_live_whatsapp_checks_unread_chats():
    """Asking about unread whatsapp messages should fire
    ``get_whatsapp_unread_chats``. Result (0 vs N) doesn't matter; only
    that the agent reached for the right action.
    """
    agent = build_agent(require=["whatsapp_web"])

    async def _run():
        await run_scenario(
            agent,
            user_message="do i have any unread whatsapp messages? list them.",
            wait_for=["whatsapp_web"],
        )

    asyncio.run(_run())
    assert_action_called(agent, "get_whatsapp_unread_chats")


def test_live_whatsapp_searches_contact_by_name():
    """Asking the agent to find a contact by name should fire
    ``search_whatsapp_contact``. We don't assert the search FOUND the
    contact — only that the agent reasoned to search instead of asking
    the user for a phone number.
    """
    agent = build_agent(require=["whatsapp_web"])

    async def _run():
        await run_scenario(
            agent,
            user_message=(
                "search my whatsapp contacts for someone named 'craftbot test'. "
                "tell me what you find."
            ),
            wait_for=["whatsapp_web"],
        )

    asyncio.run(_run())
    assert_action_called(agent, "search_whatsapp_contact")


def test_live_whatsapp_reads_self_chat_history():
    """Asking for the last whatsapp message in self-chat should fire
    ``get_whatsapp_chat_history``. The agent will likely call
    ``get_whatsapp_web_session_status`` first to find the owner's phone,
    then ``get_whatsapp_chat_history`` against it.
    """
    agent = build_agent(require=["whatsapp_web"])

    async def _run():
        await run_scenario(
            agent,
            user_message=(
                "look up the last whatsapp message i sent to myself. "
                "use my own number for the chat."
            ),
            wait_for=["whatsapp_web"],
        )

    asyncio.run(_run())
    assert_action_called(agent, "get_whatsapp_chat_history")
