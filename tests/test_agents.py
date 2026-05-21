from pathlib import Path
import logging
import sys
from typing import Any, cast

import pytest
from pydantic_ai.models.test import TestModel

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.context.snapshot import ElementIndex
from src.agent.config import LLMConfig
from src.agent.core.agent import (
    WorkerDeps,
    build_browser_worker_agent,
    build_orchestrator_agent,
    _model_settings,
    _setup_logging,
    _teardown_logging,
)
from src.agent.metrics import MetricsRecorder
from src.agent.models.actions import OrchestratorDecision, SnapshotFilterOutput, StepOutput
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
    deps = WorkerDeps(
        tool_context=tool_context,
        metrics=metrics,
        step=1,
    )
    result = await agent.run("goal: test\nsnapshot: none", deps=deps)
    assert result.output.summary == "noop"

    params = model.last_model_request_parameters
    assert params is not None
    tool_names = {t.name for t in params.function_tools}
    assert {
        "click_element",
        "click_at",
        "find_elements",
        "type_text",
        "drag_and_drop",
        "inspect_element",
        "search_page_attributes",
        "scroll",
        "wait",
        "switch_to_iframe",
        "switch_to_main_frame",
        "navigate_to",
        "take_screenshot",
        "execute_js",
        "press_key_combination",
    }.issubset(tool_names)


def test_openrouter_settings_do_not_restrict_provider_by_default() -> None:
    settings = _model_settings(LLMConfig(provider="openrouter"))

    assert "openrouter_provider" not in settings


def test_snapshot_filter_output_coerces_common_string_fields() -> None:
    output = SnapshotFilterOutput(
        useful_text_lines="1. Select 10/26/2016\n- Submit the form",
        priority_element_ids="Keep el_a1b2c3d4e5f6, el_111122223333-2, and ignore el_bad",
    )

    assert output.useful_text_lines == ["Select 10/26/2016", "Submit the form"]
    assert output.priority_element_ids == ["el_a1b2c3d4e5f6", "el_111122223333-2"]


def test_setup_logging_replaces_stale_agent_file_handlers(tmp_path: Path) -> None:
    logger = logging.getLogger("tests.logging")
    first = tmp_path / "first"
    second = tmp_path / "second"
    _teardown_logging()

    try:
        _setup_logging(str(first), level="INFO", color=False)
        logger.info("first-only")
        _setup_logging(str(second), level="INFO", color=False)
        logger.info("second-only")
    finally:
        _teardown_logging()

    assert "first-only" in (first / "agent.log").read_text()
    assert "second-only" not in (first / "agent.log").read_text()
    assert "second-only" in (second / "agent.log").read_text()
