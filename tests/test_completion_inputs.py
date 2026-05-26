from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.core.agent import AgentState, ToolCallTracker, _completion_inputs_from_step


def _tool_context(url: str = "https://example.com") -> Any:
    return SimpleNamespace(page=SimpleNamespace(url=url))


def test_first_worker_goal_is_not_compared_to_itself() -> None:
    state = AgentState(last_worker_goal="Read page")
    tracker = ToolCallTracker()
    tracker.record(True, tool_name="read_live_text", message="No change")

    inputs = _completion_inputs_from_step(
        state,
        validation=None,
        model_done=False,
        completion_evidence=None,
        worker_goal="Read page",
        tool_context=cast(Any, _tool_context()),
        tool_tracker=tracker,
        prev_url="https://example.com",
        tool_limit_hit=False,
    )

    assert inputs.same_worker_goal is False


def test_worker_goal_matches_prior_trace_goal() -> None:
    state = AgentState(last_worker_goal="Current setup")
    state.step_trace.append({"goal": "Read page"})
    tracker = ToolCallTracker()
    tracker.record(True, tool_name="read_live_text", message="No change")

    inputs = _completion_inputs_from_step(
        state,
        validation=None,
        model_done=False,
        completion_evidence=None,
        worker_goal="Read page",
        tool_context=cast(Any, _tool_context()),
        tool_tracker=tracker,
        prev_url="https://example.com",
        tool_limit_hit=False,
    )

    assert inputs.same_worker_goal is True
