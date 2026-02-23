from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any, Callable

import contextlib
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.config import AgentConfig, BrowserConfig, LLMConfig
from src.agent.context.snapshot import PageSnapshot
from src.agent.core import agent as agent_mod
from src.agent.metrics import UsageStats
from src.agent.models.actions import OracleAdvice, OrchestratorDecision, SnapshotFilterOutput, StepOutput, UnifiedStepOutput


@dataclass
class _StubPage:
    url: str = "about:blank"

    async def goto(self, url: str) -> None:
        self.url = url

    async def wait_for_load_state(self, state: str = "load", **kwargs: object) -> None:
        pass


@dataclass
class _StubSession:
    page: _StubPage
    cdp_session: Any
    frame_sessions: dict[str, Any]


@dataclass
class _StubResult:
    output: Any

    def new_messages(self) -> list[Any]:
        return []


class _StubAgent:
    def __init__(self, runner: Callable[[str, Any | None], Any]) -> None:
        self._runner = runner

    async def run(self, prompt: str, deps: Any | None = None, **kwargs: Any) -> _StubResult:
        return _StubResult(output=self._runner(prompt, deps))

    def sequential_tool_calls(self):
        return contextlib.nullcontext()


def _snapshot(*, url: str) -> PageSnapshot:
    return PageSnapshot(url=url, title="Test", elements=[], raw_text=[])


@pytest.mark.asyncio
async def test_unified_stops_on_done_without_tool_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshots = [_snapshot(url="https://example.com")]

    async def fake_launch_browser(_config: BrowserConfig) -> _StubSession:
        return _StubSession(page=_StubPage(url="https://example.com"), cdp_session=object(), frame_sessions={})

    async def fake_close_browser(_session: _StubSession) -> None:
        return None

    async def fake_capture_snapshot(_page: _StubPage, _cdp_session: Any, **_kwargs: Any) -> PageSnapshot:
        return snapshots.pop(0)

    captured_summary: dict[str, Any] = {}

    def fake_write_run_summary(
        *, log_dir: str, run_id: str, summary: dict[str, Any], filename: str = "run_summary.json"
    ) -> Path:
        captured_summary.update(summary)
        return tmp_path / filename

    monkeypatch.setattr(agent_mod, "launch_browser", fake_launch_browser)
    monkeypatch.setattr(agent_mod, "close_browser", fake_close_browser)
    monkeypatch.setattr(agent_mod, "_teardown_logging", lambda: None)
    monkeypatch.setattr(agent_mod, "capture_snapshot", fake_capture_snapshot)
    monkeypatch.setattr(agent_mod, "write_run_summary", fake_write_run_summary)
    monkeypatch.setattr(agent_mod, "_build_model", lambda *_a, **_k: object())
    monkeypatch.setattr(agent_mod, "_model_settings", lambda *_a, **_k: {})
    monkeypatch.setattr(agent_mod, "usage_stats_from_result", lambda _res: UsageStats(0, 0, 0, 0, 0, 0, 0, 0, 0))
    monkeypatch.setattr(agent_mod, "cost_stats_from_result", lambda *_a, **_k: None)

    monkeypatch.setattr(
        agent_mod,
        "build_snapshot_filter_agent",
        lambda *_a, **_k: _StubAgent(lambda *_: SnapshotFilterOutput(useful_text_lines=[], priority_element_ids=[], notes=None)),
    )
    monkeypatch.setattr(
        agent_mod,
        "build_orchestrator_agent",
        lambda *_a, **_k: _StubAgent(lambda *_: OrchestratorDecision(done=False, worker_goal="noop")),
    )
    monkeypatch.setattr(
        agent_mod,
        "build_browser_worker_agent",
        lambda *_a, **_k: _StubAgent(lambda *_: StepOutput(done=False, summary="noop")),
    )
    monkeypatch.setattr(
        agent_mod,
        "build_oracle_agent",
        lambda *_a, **_k: _StubAgent(lambda *_: OracleAdvice(all_clear=True, diagnosis="ok", recommendation="continue", avoid=[])),
    )
    monkeypatch.setattr(
        agent_mod,
        "build_unified_agent",
        lambda *_a, **_k: _StubAgent(
            lambda *_: UnifiedStepOutput(
                done=True,
                step_goal="Conclude task",
                summary="Goal already satisfied; no action needed.",
                rationale="The provided context indicates completion.",
            )
        ),
    )

    agent = agent_mod.BrowserAgent(
        AgentConfig(
            target_url="https://example.com",
            goal="Test goal",
            max_steps=2,
            unified=True,
            log_dir=str(tmp_path),
            metrics_enabled=False,
        ),
        LLMConfig(),
        BrowserConfig(headless=True),
    )
    await agent.run()

    assert captured_summary.get("steps") == 1
    assert captured_summary.get("stop_reason") == "done"


@pytest.mark.asyncio
async def test_unified_done_gate_overrides_done_when_all_tool_calls_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshots = [_snapshot(url="https://example.com"), _snapshot(url="https://example.com")]

    async def fake_launch_browser(_config: BrowserConfig) -> _StubSession:
        return _StubSession(page=_StubPage(url="https://example.com"), cdp_session=object(), frame_sessions={})

    async def fake_close_browser(_session: _StubSession) -> None:
        return None

    async def fake_capture_snapshot(_page: _StubPage, _cdp_session: Any, **_kwargs: Any) -> PageSnapshot:
        return snapshots.pop(0)

    captured_summary: dict[str, Any] = {}

    def fake_write_run_summary(
        *, log_dir: str, run_id: str, summary: dict[str, Any], filename: str = "run_summary.json"
    ) -> Path:
        captured_summary.update(summary)
        return tmp_path / filename

    def unified_runner(_prompt: str, deps: Any | None) -> UnifiedStepOutput:
        assert deps is not None
        assert getattr(deps, "tool_tracker", None) is not None
        deps.tool_tracker.record(False)
        return UnifiedStepOutput(
            done=True,
            step_goal="Try an action",
            summary="Attempted an action but it failed.",
            rationale="Testing done-gate behavior.",
        )

    monkeypatch.setattr(agent_mod, "launch_browser", fake_launch_browser)
    monkeypatch.setattr(agent_mod, "close_browser", fake_close_browser)
    monkeypatch.setattr(agent_mod, "_teardown_logging", lambda: None)
    monkeypatch.setattr(agent_mod, "capture_snapshot", fake_capture_snapshot)
    monkeypatch.setattr(agent_mod, "write_run_summary", fake_write_run_summary)
    monkeypatch.setattr(agent_mod, "_build_model", lambda *_a, **_k: object())
    monkeypatch.setattr(agent_mod, "_model_settings", lambda *_a, **_k: {})
    monkeypatch.setattr(agent_mod, "usage_stats_from_result", lambda _res: UsageStats(0, 0, 0, 0, 0, 0, 0, 0, 0))
    monkeypatch.setattr(agent_mod, "cost_stats_from_result", lambda *_a, **_k: None)

    monkeypatch.setattr(
        agent_mod,
        "build_snapshot_filter_agent",
        lambda *_a, **_k: _StubAgent(lambda *_: SnapshotFilterOutput(useful_text_lines=[], priority_element_ids=[], notes=None)),
    )
    monkeypatch.setattr(
        agent_mod,
        "build_orchestrator_agent",
        lambda *_a, **_k: _StubAgent(lambda *_: OrchestratorDecision(done=False, worker_goal="noop")),
    )
    monkeypatch.setattr(
        agent_mod,
        "build_browser_worker_agent",
        lambda *_a, **_k: _StubAgent(lambda *_: StepOutput(done=False, summary="noop")),
    )
    monkeypatch.setattr(
        agent_mod,
        "build_oracle_agent",
        lambda *_a, **_k: _StubAgent(lambda *_: OracleAdvice(all_clear=True, diagnosis="ok", recommendation="continue", avoid=[])),
    )
    monkeypatch.setattr(
        agent_mod,
        "build_unified_agent",
        lambda *_a, **_k: _StubAgent(unified_runner),
    )

    agent = agent_mod.BrowserAgent(
        AgentConfig(
            target_url="https://example.com",
            goal="Test goal",
            max_steps=2,
            unified=True,
            log_dir=str(tmp_path),
            metrics_enabled=False,
        ),
        LLMConfig(),
        BrowserConfig(headless=True),
    )
    await agent.run()

    assert captured_summary.get("steps") == 2
    assert captured_summary.get("stop_reason") == "max_steps"

