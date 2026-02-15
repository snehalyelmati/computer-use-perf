from pathlib import Path
import sys
from typing import Any, cast

import pytest
from pydantic_ai.models.test import TestModel

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.context.snapshot import ElementIndex
from src.agent.core.agent import build_browser_worker_agent, build_orchestrator_agent, WorkerDeps
from src.agent.metrics import MetricsRecorder
from src.agent.models.actions import OrchestratorDecision, StepOutput
from src.agent.tools.semantic import ToolContext


@pytest.mark.asyncio
async def test_orchestrator_has_no_function_tools() -> None:
    model = TestModel(
        call_tools=[],
        custom_output_args=OrchestratorDecision(done=True, worker_goal="stop"),
    )
    agent = build_orchestrator_agent(model, model_settings={})
    result = await agent.run("goal: test\nsnapshot: none")
    assert result.output.done is True
    params = model.last_model_request_parameters
    assert params is not None
    assert params.function_tools == []


@pytest.mark.asyncio
async def test_browser_worker_registers_semantic_tools() -> None:
    model = TestModel(
        call_tools=[],
        custom_output_args=StepOutput(done=False, summary="noop", next_goal=None),
    )
    agent = build_browser_worker_agent(model, model_settings={})

    empty_index = ElementIndex(elements={})
    tool_context = ToolContext(
        page=cast(Any, object()),
        cdp_session=cast(Any, object()),
        element_index=empty_index,
        frame_sessions={},
        active_frame_id=None,
    )
    metrics = MetricsRecorder(log_dir="/tmp", run_id="test", enabled=False)
    deps = WorkerDeps(tool_context=tool_context, metrics=metrics, step=1)
    result = await agent.run("goal: test\nsnapshot: none", deps=deps)
    assert result.output.summary == "noop"

    params = model.last_model_request_parameters
    assert params is not None
    tool_names = {t.name for t in params.function_tools}
    assert {
        "click_element",
        "type_text",
        "drag_and_drop",
        "select_all",
        "copy_selection",
        "paste",
        "read_element_text",
        "switch_to_iframe",
        "switch_to_main_frame",
        "navigate_to",
        "take_screenshot",
        "execute_js",
        "press_key_combination",
    }.issubset(tool_names)
