"""LIVE end-to-end scenarios — real LLM, real integrations, real sends.

Boots the actual ``AgentBase`` the same way ``app/main.py`` does and lets
the agent reason / pick actions / call integrations for real. No LLM mock,
no integration patches.

Each test follows the same black-box shape:

    1. Note the test-start timestamp.
    2. Send a trigger to the agent. Let it do its full lifecycle
       (route → todos → action set selection → action steps → task_end).
    3. After the lifecycle finishes, ask the integration itself: "is
       there a message in my chat from craftbot, sent during the test
       window?" If yes, the test passes. If no, it fails.

Why black-box? With a real LLM, the agent may take many paths to fulfil
a request — direct send, task wrapping, asking for clarification first.
The only thing the user actually cares about is whether the message
arrives. So we verify by querying the integration's own chat history
*after* the agent settles, not by spying on which methods it called.

Shared plumbing — agent boot, scenario runner, integration queries —
lives in [_live_helpers.py](tests/e2e/_live_helpers.py).

**These tests are opt-in.** Default ``pytest`` runs skip them. Run with::

    python -m pytest tests/e2e/test_live_scenarios.py -v -m live -s

They will spend LLM tokens and send real messages to your real accounts.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from tests.e2e._harness import (
    build_agent,
    format_agent_trace,
    run_scenario,
    save_trace_log,
)
from tests.e2e._integrations import whatsapp


pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# Scenario: WhatsApp — send a ping to myself
# ---------------------------------------------------------------------------


def test_live_whatsapp_send_ping_to_me():
    """User asks the agent to whatsapp themselves. After the agent settles,
    we ask whatsapp directly: 'do you have a recent outgoing message from
    me to me?' If yes, the test passes regardless of which code path the
    agent took to get there.

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
        # Give the wwebjs bridge a moment to flush the send to whatsapp's
        # servers before we query the chat history.
        await asyncio.sleep(2.0)
        return await whatsapp.recent_messages_in_self_chat(since_ts=test_start_ts)

    recent = asyncio.run(_run())

    # Write the full agent trace to a per-test log file under
    # tests/e2e/logs/. Faithful to production — same in-memory event
    # stream the agent reads back into its own prompts. Open the file
    # to see the lifecycle: route → action_set selection → task →
    # action execution → task_end.
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


# NOTE: A read-side scenario ("list my unread whatsapp messages") was drafted
# but is intentionally left out for now. Read actions like
# get_whatsapp_unread_chats live only in task action sets (per
# [_routing.py](app/data/action/integrations/_routing.py)), so the agent
# always goes route → task_start → setup → action_set selection → action
# selection → execute → task_end. That's 5+ react cycles with real LLM
# calls between each one, which makes timing and pump-tuning non-trivial.
#
# The send scenario above already drives a task lifecycle when the LLM
# picks that path, so the multi-react machinery is exercised. A dedicated
# read test is the next natural addition once we trust the pump.
