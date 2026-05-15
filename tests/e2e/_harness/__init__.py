"""Shared infrastructure for LIVE end-to-end tests.

Private package (underscore-prefixed) so pytest doesn't collect it as
a test module.

Two concerns, two modules:
  - :mod:`helpers` — drive the agent: ``build_agent``, ``run_scenario``.
  - :mod:`trace`   — render what happened: ``format_agent_trace``,
                     ``save_trace_log``, ``record_llm_calls``.

Tests import from the package directly::

    from tests.e2e._harness import build_agent, run_scenario, save_trace_log
"""

from tests.e2e._harness.helpers import build_agent, run_scenario
from tests.e2e._harness.trace import (
    TEST_LOG_DIR,
    format_agent_trace,
    record_llm_calls,
    save_trace_log,
)

__all__ = [
    "build_agent",
    "run_scenario",
    "format_agent_trace",
    "record_llm_calls",
    "save_trace_log",
    "TEST_LOG_DIR",
]
