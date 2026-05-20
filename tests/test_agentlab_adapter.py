from __future__ import annotations

import pickle
from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.agentlab.computer_use_agent import (
    ComputerUseAgentArgs,
    ComputerUseAgentLabAgent,
)
from src.agent.config import AgentConfig, LLMConfig
from src.agent.core.step_runtime import RuntimeStepResult


@dataclass
class _SyncCDP:
    detached: bool = False

    def send(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"method": method, "params": params}

    def detach(self) -> None:
        self.detached = True


@dataclass
class _SyncFrame:
    url: str = "https://example.test"
    name: str = ""
    child_frames: list["_SyncFrame"] = field(default_factory=list)


@dataclass
class _SyncKeyboard:
    def press(self, key: str) -> None:
        del key


@dataclass
class _SyncContext:
    cdp: _SyncCDP

    def new_cdp_session(self, target: Any) -> _SyncCDP:
        del target
        return self.cdp


@dataclass
class _SyncPage:
    url: str = "https://example.test"
    cdp: _SyncCDP = field(default_factory=_SyncCDP)
    main_frame: _SyncFrame = field(default_factory=_SyncFrame)
    keyboard: _SyncKeyboard = field(default_factory=_SyncKeyboard)

    def __post_init__(self) -> None:
        self.context = _SyncContext(self.cdp)
        self.frames = [self.main_frame]


class _StubRuntime:
    def __init__(self, _agent_config: AgentConfig, _llm_config: LLMConfig) -> None:
        self.calls: list[tuple[Any, str | None]] = []
        self.closed: list[str | None] = []

    async def run_one_step(self, session: Any, *, goal: str | None = None) -> RuntimeStepResult:
        self.calls.append((session, goal))
        return RuntimeStepResult(
            step=1,
            done=False,
            summary="clicked button",
            stop_reason=None,
            worker_goal="Click the target button",
            tool_calls="click_element(el_123)",
            input_tokens=11,
            output_tokens=7,
            cost_usd=0.001,
            duration_ms=250,
            log_dir="/tmp/run",
            trace=[{"step": 1, "summary": "clicked button"}],
        )

    async def close(self, *, stop_reason: str | None = None, interrupted: bool = False) -> None:
        del interrupted
        self.closed.append(stop_reason)


def test_agent_args_enable_raw_page_output_by_default() -> None:
    args = ComputerUseAgentArgs()
    assert args.use_raw_page_output is True
    agent = args.make_agent()
    assert isinstance(agent, ComputerUseAgentLabAgent)


def test_obs_preprocessor_strips_raw_page_before_pickle() -> None:
    runtime = _StubRuntime(AgentConfig(), LLMConfig())
    agent = ComputerUseAgentLabAgent(
        agent_config=AgentConfig(metrics_enabled=False),
        llm_config=LLMConfig(),
        runtime_factory=lambda _ac, _lc: runtime,
    )
    page = _SyncPage()
    processed = agent.obs_preprocessor({"page": page, "goal": "Click the button", "url": page.url})

    assert "page" not in processed
    pickle.dumps(processed)


def test_get_action_runs_internal_step_and_returns_noop() -> None:
    runtime = _StubRuntime(AgentConfig(), LLMConfig())
    agent = ComputerUseAgentLabAgent(
        agent_config=AgentConfig(metrics_enabled=False),
        llm_config=LLMConfig(),
        runtime_factory=lambda _ac, _lc: runtime,
    )
    page = _SyncPage()
    obs = agent.obs_preprocessor({"page": page, "goal": "Click the button"})

    action, info = agent.get_action(obs)

    assert action == "noop()"
    assert runtime.calls[0][1] == "Click the button"
    assert page.cdp.detached is True
    assert info["stats"]["computer_use_steps"] == 1
    assert info["extra_info"]["tool_calls"] == "click_element(el_123)"


def test_get_action_without_raw_page_returns_noop_with_error() -> None:
    runtime = _StubRuntime(AgentConfig(), LLMConfig())
    agent = ComputerUseAgentLabAgent(
        agent_config=AgentConfig(metrics_enabled=False),
        llm_config=LLMConfig(),
        runtime_factory=lambda _ac, _lc: runtime,
    )

    action, info = agent.get_action({"goal": "Do the task"})

    assert action == "noop()"
    assert info["stats"]["computer_use_error"] == 1
    assert runtime.calls == []
