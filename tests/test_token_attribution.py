# -*- coding: utf-8 -*-
"""
Tests for per-task LLM token attribution.

Verifies that `attribute_usage_to_current_task` correctly bumps the
cumulative token counters on the active Task and emits a TASK_TOKEN_UPDATE
event on the bus so the browser can tick its per-task token display.
"""

from __future__ import annotations

import pytest

from agent_core.core.hooks.types import UsageEventData
from agent_core.core.task.task import Task
from app.state.agent_state import STATE
from app.ui_layer.events.event_bus import EventBus
from app.ui_layer.events.event_types import UIEvent, UIEventType
from app.usage.task_attribution import attribute_usage_to_current_task


@pytest.fixture
def fresh_state():
    """Snapshot and restore STATE.current_task / STATE.event_bus per test."""
    prev_task = STATE.current_task
    prev_bus = STATE.event_bus
    yield
    STATE.current_task = prev_task
    STATE.event_bus = prev_bus


def _make_event(input_tokens=100, output_tokens=50, cached_tokens=20):
    return UsageEventData(
        service_type="llm_anthropic",
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
    )


def test_no_active_task_is_noop(fresh_state):
    """When no task is active, attribution is a silent no-op."""
    STATE.current_task = None
    STATE.event_bus = EventBus()
    # Must not raise
    attribute_usage_to_current_task(_make_event())
    assert STATE.event_bus.get_history() == []


def test_increments_counters_on_active_task(fresh_state):
    """A single call bumps the task's three counters."""
    task = Task(id="t1", name="test", instruction="x")
    STATE.current_task = task
    STATE.event_bus = EventBus()

    attribute_usage_to_current_task(_make_event(100, 50, 20))

    assert task.input_tokens == 100
    assert task.output_tokens == 50
    assert task.cache_tokens == 20


def test_accumulates_across_multiple_calls(fresh_state):
    """Counters accumulate, not overwrite."""
    task = Task(id="t2", name="test", instruction="x")
    STATE.current_task = task
    STATE.event_bus = EventBus()

    attribute_usage_to_current_task(_make_event(100, 50, 20))
    attribute_usage_to_current_task(_make_event(40, 10, 5))
    attribute_usage_to_current_task(_make_event(7, 3, 0))

    assert task.input_tokens == 147
    assert task.output_tokens == 63
    assert task.cache_tokens == 25


def test_emits_task_token_update_event(fresh_state):
    """Each attribution emits a TASK_TOKEN_UPDATE carrying running totals."""
    task = Task(id="t3", name="test", instruction="x")
    STATE.current_task = task
    bus = EventBus()
    STATE.event_bus = bus

    captured: list[UIEvent] = []
    bus.subscribe(UIEventType.TASK_TOKEN_UPDATE, captured.append)

    attribute_usage_to_current_task(_make_event(100, 50, 20))
    attribute_usage_to_current_task(_make_event(40, 10, 5))

    assert len(captured) == 2

    # First event: counters at first call's values
    assert captured[0].type == UIEventType.TASK_TOKEN_UPDATE
    assert captured[0].task_id == "t3"
    assert captured[0].data == {
        "task_id": "t3",
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_tokens": 20,
    }

    # Second event: cumulative running totals
    assert captured[1].data == {
        "task_id": "t3",
        "input_tokens": 140,
        "output_tokens": 60,
        "cache_tokens": 25,
    }


def test_works_without_event_bus(fresh_state):
    """If no bus is registered, counters still update; no crash."""
    task = Task(id="t4", name="test", instruction="x")
    STATE.current_task = task
    STATE.event_bus = None  # explicit

    attribute_usage_to_current_task(_make_event(100, 50, 20))

    assert task.input_tokens == 100
    assert task.output_tokens == 50
    assert task.cache_tokens == 20


def test_handles_none_token_fields_as_zero(fresh_state):
    """Pre-existing tasks may have None token fields (legacy data)."""
    task = Task(id="t5", name="test", instruction="x")
    # Simulate legacy Task loaded from older persistence with no token fields
    task.input_tokens = None  # type: ignore[assignment]
    task.output_tokens = None  # type: ignore[assignment]
    task.cache_tokens = None  # type: ignore[assignment]
    STATE.current_task = task
    STATE.event_bus = EventBus()

    attribute_usage_to_current_task(_make_event(10, 5, 1))

    assert task.input_tokens == 10
    assert task.output_tokens == 5
    assert task.cache_tokens == 1
