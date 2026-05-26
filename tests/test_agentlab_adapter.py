from __future__ import annotations

import os
import pickle
from dataclasses import dataclass, field
from pathlib import Path
import sys
import types
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.agentlab.computer_use_agent import (
    ComputerUseAgentArgs,
    ComputerUseAgentLabAgent,
    _VALIDATION_OBS_KEY,
    _install_agentlab_validation_bridge,
    _validation_from_obs,
)
from benchmarks.agentlab.run_miniwob_smoke import (
    _RESOURCE_TRACKER_WARNING_FILTER,
    _suppress_resource_tracker_shutdown_noise,
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
        self.validations: list[Any | None] = []
        self.closed: list[str | None] = []

    async def run_one_step(
        self, session: Any, *, goal: str | None = None, validation: Any | None = None
    ) -> RuntimeStepResult:
        self.validations.append(validation)
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
            step_input_tokens=5,
            step_output_tokens=3,
            step_cost_usd=0.0004,
        )

    async def close(self, *, stop_reason: str | None = None, interrupted: bool = False) -> None:
        del interrupted
        self.closed.append(stop_reason)


def test_agent_args_enable_raw_page_output_by_default() -> None:
    args = ComputerUseAgentArgs()
    assert args.use_raw_page_output is True
    assert args.unified is True
    assert args.model == "z-ai/glm-4.7:nitro"
    agent = args.make_agent()
    assert isinstance(agent, ComputerUseAgentLabAgent)
    assert agent.agent_config.unified is True


def test_miniwob_runner_suppresses_resource_tracker_shutdown_warning(monkeypatch: Any) -> None:
    monkeypatch.setenv("PYTHONWARNINGS", "default")

    _suppress_resource_tracker_shutdown_noise()

    filters = os.environ["PYTHONWARNINGS"].split(",")
    assert "default" in filters
    assert _RESOURCE_TRACKER_WARNING_FILTER in filters


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
    assert agent._last_validation is not None
    assert agent._last_validation.status == "neutral"
    assert agent._last_validation.terminal is False
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
    assert info["stats"]["computer_use_input_tokens"] == 5
    assert info["stats"]["computer_use_output_tokens"] == 3
    assert "computer_use_cumulative_input_tokens" not in info["stats"]
    assert "computer_use_cumulative_total_tokens" not in info["stats"]
    assert info["extra_info"]["cumulative_usage"]["total_tokens"] == 18
    assert info["extra_info"]["tool_calls"] == "click_element(el_123)"


def test_get_action_passes_browsergym_validation_to_runtime() -> None:
    runtime = _StubRuntime(AgentConfig(), LLMConfig())
    agent = ComputerUseAgentLabAgent(
        agent_config=AgentConfig(metrics_enabled=False),
        llm_config=LLMConfig(),
        runtime_factory=lambda _ac, _lc: runtime,
    )
    page = _SyncPage()
    obs = agent.obs_preprocessor(
        {"page": page, "goal": "Click the button", "reward": 0, "terminated": False}
    )

    action, _info = agent.get_action(obs)

    assert action == "noop()"
    assert runtime.validations[0] is not None
    assert runtime.validations[0].reward == 0
    assert runtime.validations[0].terminal is False


def test_obs_preprocessor_prefers_private_validation_payload() -> None:
    runtime = _StubRuntime(AgentConfig(), LLMConfig())
    agent = ComputerUseAgentLabAgent(
        agent_config=AgentConfig(metrics_enabled=False),
        llm_config=LLMConfig(),
        runtime_factory=lambda _ac, _lc: runtime,
    )
    page = _SyncPage()
    processed = agent.obs_preprocessor(
        {
            "page": page,
            "goal": "Click the button",
            "reward": 1,
            "terminated": True,
            _VALIDATION_OBS_KEY: {
                "reward": 0,
                "terminated": False,
                "truncated": False,
            },
        }
    )

    assert _VALIDATION_OBS_KEY not in processed
    assert agent._last_validation is not None
    assert agent._last_validation.reward == 0
    assert agent._last_validation.terminal is False


def test_agentlab_validation_bridge_adds_step_result_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    class StepInfo:
        reward = 0.0
        raw_reward = 0.0
        terminated = False
        truncated = False
        task_info = {"task": "stub"}

        def from_step(self, env: Any, action: str, obs_preprocessor: Any) -> str:
            del env, action
            obs_preprocessor({"goal": "Click the button"})
            return "ok"

    browsergym_module = types.ModuleType("browsergym")
    browsergym_module.__path__ = []
    experiments_module = types.ModuleType("browsergym.experiments")
    experiments_module.__path__ = []
    loop_module = types.ModuleType("browsergym.experiments.loop")
    loop_module.StepInfo = StepInfo
    monkeypatch.setitem(sys.modules, "browsergym", browsergym_module)
    monkeypatch.setitem(sys.modules, "browsergym.experiments", experiments_module)
    monkeypatch.setitem(sys.modules, "browsergym.experiments.loop", loop_module)

    _install_agentlab_validation_bridge()

    seen: dict[str, Any] = {}
    result = StepInfo().from_step(
        object(),
        "noop()",
        lambda obs: seen.update(obs),
    )

    assert result == "ok"
    assert seen[_VALIDATION_OBS_KEY]["reward"] == 0.0
    assert seen[_VALIDATION_OBS_KEY]["terminated"] is False


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


def test_browsergym_obs_validation_signal_conversion() -> None:
    signal = _validation_from_obs({"reward": 1, "terminated": True, "truncated": False})

    assert signal is not None
    assert signal.source == "browsergym"
    assert signal.status == "success"
    assert signal.terminal is True
    assert signal.reward == 1.0


def test_browsergym_terminal_zero_reward_is_failure() -> None:
    signal = _validation_from_obs({"reward": 0, "terminated": True})

    assert signal is not None
    assert signal.status == "failure"
    assert signal.terminal is True


def test_close_closes_runtime_and_clears_page() -> None:
    runtime = _StubRuntime(AgentConfig(), LLMConfig())
    agent = ComputerUseAgentLabAgent(
        agent_config=AgentConfig(metrics_enabled=False),
        llm_config=LLMConfig(),
        runtime_factory=lambda _ac, _lc: runtime,
    )
    page = _SyncPage()
    agent.obs_preprocessor({"page": page, "goal": "Click the button"})

    agent.close()

    assert runtime.closed == ["close"]
    assert agent._raw_page is None
