"""Generic agent-driving infrastructure for LIVE end-to-end tests.

Two public functions, both integration-agnostic:

- ``build_agent(require=[...])`` — construct the real AgentBase, skip the
  test if the LLM provider or any required integration isn't configured.
- ``run_scenario(agent, user_message, ...)`` — send a chat through the
  same entry point the CraftBot UI uses, drain the trigger queue, return
  bridge status dicts. Captures every LLM call onto
  ``agent._test_llm_calls`` for the trace logger to render afterwards.

Event-loop note
---------------

Keep boot + react + assertions inside one ``asyncio.run`` call. The
integration manager binds its async resources to the loop that
``_initialize_external_libraries`` runs on; switching loops mid-test
strands the bridge and ``react()`` silently exits early.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import pytest

# Module-level setup mirrors app/main.py registrations BEFORE importing AgentBase.
os.environ.setdefault("GUI_MODE_ENABLED", "False")

from agent_core import StateRegistry, ConfigRegistry  # noqa: E402
from app.state.agent_state import STATE  # noqa: E402
from app.config import (  # noqa: E402
    get_project_root, get_llm_provider, get_api_key, get_base_url, get_llm_model,
    get_vlm_provider, get_vlm_model,
)

StateRegistry.register(lambda: STATE)
ConfigRegistry.register_workspace_root(str(get_project_root()))

from app.agent_base import AgentBase  # noqa: E402
from craftos_integrations import get_client, list_connected  # noqa: E402

from tests.e2e._harness.trace import record_llm_calls  # noqa: E402


def build_agent(*, require: list[str] | tuple[str, ...] = ()) -> AgentBase:
    """Construct ``AgentBase`` the same way ``app/main.py`` does.

    Skips the test (via ``pytest.skip``) when the LLM provider or any
    integration in ``require`` isn't configured on this host, so the same
    test file is safe to keep in the suite even on machines without those
    credentials.
    """
    provider = get_llm_provider()
    if not provider:
        pytest.skip("no LLM provider configured in settings.json")
    api_key = get_api_key(provider)
    if not api_key and provider != "remote":
        pytest.skip(f"no API key for provider {provider!r} in settings.json")
    for pid in require:
        if pid not in list_connected():
            pytest.skip(f"integration {pid!r} not connected on this host.")

    return AgentBase(
        data_dir="app/data",
        chroma_path="./chroma_db",
        llm_provider=provider,
        llm_api_key=api_key,
        llm_base_url=get_base_url(provider),
        llm_model=get_llm_model(),
        vlm_provider=get_vlm_provider(),
        vlm_model=get_vlm_model(),
        deferred_init=False,
    )


async def run_scenario(
    agent: AgentBase,
    user_message: str,
    *,
    platform: str | None = None,
    wait_for: list[str] | tuple[str, ...] = (),
    max_iterations: int = 20,
    per_iter_timeout: float = 30.0,
    ready_timeout: float = 45.0,
) -> dict[str, dict]:
    """Run one live scenario end-to-end.

    1. Start the integration manager.
    2. Clear runtime state so leftover tasks from prior runs don't
       contaminate the LLM's view (does NOT wipe USER.md / MEMORY.md).
    3. Wait for each integration in ``wait_for`` to report ``ready``.
    4. Fire ``agent._handle_chat_message({"text": user_message, ...})`` —
       the SAME call path as the CraftBot UI sending a chat input.
    5. Drain the trigger queue, calling ``react()`` on each, until either
       the queue stays empty past a short grace window, ``max_iterations``
       is hit, or the next trigger doesn't fire within ``per_iter_timeout``.

    Returns ``{integration_id: status_dict}`` for every ``wait_for`` entry
    so the test can pull ``owner_phone`` / ``owner_email`` / etc.
    """
    await agent._initialize_external_libraries()

    # Reset in-memory + persisted runtime state. Keeps USER.md / MEMORY.md.
    await agent.triggers.clear()
    agent.task_manager.reset()
    agent.state_manager.reset()
    agent.event_stream_manager.clear_all()
    try:
        from app.usage.session_storage import get_session_storage
        get_session_storage().clear_all()
    except Exception:
        pass

    async with record_llm_calls(agent):
        # Poll each requested integration's session status until it's ready.
        bridge_statuses: dict[str, dict] = {}
        for pid in wait_for:
            client = get_client(pid)
            if client is None:
                pytest.fail(f"integration {pid!r} client not registered")
            deadline = time.time() + ready_timeout
            while time.time() < deadline:
                try:
                    status = client.get_session_status()
                    if asyncio.iscoroutine(status):
                        status = await status
                    if isinstance(status, dict) and (status.get("ready") or status.get("ok")):
                        bridge_statuses[pid] = status
                        break
                except Exception:
                    pass
                await asyncio.sleep(1.0)
            else:
                pytest.fail(f"integration {pid!r} never became ready in {ready_timeout}s.")

        # Production entry point.
        payload: dict[str, Any] = {"text": user_message}
        if platform:
            payload["platform"] = platform
        await agent._handle_chat_message(payload)

        # Drain the trigger queue. Each react() may enqueue follow-up triggers
        # (task lifecycle) — keep pulling until the queue stays empty past a
        # short grace window.
        for _ in range(max_iterations):
            deadline = asyncio.get_event_loop().time() + 1.5
            while not agent.triggers._heap and asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.1)
            if not agent.triggers._heap:
                break
            try:
                trig = await asyncio.wait_for(agent.triggers.get(), timeout=per_iter_timeout)
            except asyncio.TimeoutError:
                break
            await agent.react(trig)

    return bridge_statuses
