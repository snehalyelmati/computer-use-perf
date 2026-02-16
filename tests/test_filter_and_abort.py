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
from src.agent.context.snapshot import ElementSnapshot, PageSnapshot
from src.agent.core import agent as agent_mod
from src.agent.metrics import UsageStats
from src.agent.models.actions import OrchestratorDecision, SnapshotFilterOutput, StepOutput


@dataclass
class _StubPage:
    url: str = "about:blank"

    async def goto(self, url: str) -> None:
        self.url = url


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

    async def run(self, prompt: str, deps: Any | None = None) -> _StubResult:
        return _StubResult(output=self._runner(prompt, deps))

    def sequential_tool_calls(self):
        return contextlib.nullcontext()


def _snapshot(*, url: str, title: str, element_name: str) -> PageSnapshot:
    return PageSnapshot(
        url=url,
        title=title,
        elements=[
            ElementSnapshot(
                stable_id="el_1",
                backend_node_id=1,
                node_name="BUTTON",
                role="button",
                name=element_name,
                text=None,
                bounding_box=(10, 10, 100, 30),
                attributes={},
                frame_id="frame_1",
                frame_url=url,
                frame_name=None,
            )
        ],
        raw_text=[f"{title} {element_name}"],
    )


@pytest.mark.asyncio
async def test_filter_is_cached_when_fingerprint_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    snapshots = [
        _snapshot(url="https://example.com", title="Test", element_name="Next"),
        _snapshot(url="https://example.com", title="Test", element_name="Next"),
    ]

    async def fake_launch_browser(_config: BrowserConfig) -> _StubSession:
        return _StubSession(page=_StubPage(url="https://example.com"), cdp_session=object(), frame_sessions={})

    async def fake_close_browser(_session: _StubSession) -> None:
        return None

    async def fake_capture_snapshot(_page: _StubPage, _cdp_session: Any) -> PageSnapshot:
        return snapshots.pop(0)

    captured_summary: dict[str, Any] = {}

    def fake_write_run_summary(*, log_dir: str, run_id: str, summary: dict[str, Any], filename: str = "run_summary.json") -> Path:
        captured_summary.update(summary)
        return tmp_path / filename

    filter_calls = {"count": 0}

    def filter_runner(_prompt: str, _deps: Any | None) -> SnapshotFilterOutput:
        filter_calls["count"] += 1
        return SnapshotFilterOutput(useful_text_lines=["Use the Next button"], priority_element_ids=["el_1"], notes=None)

    def orchestrator_runner(_prompt: str, _deps: Any | None) -> OrchestratorDecision:
        return OrchestratorDecision(done=False, worker_goal="noop")

    def worker_runner(_prompt: str, _deps: Any | None) -> StepOutput:
        return StepOutput(done=False, summary="noop", next_goal=None)

    monkeypatch.setattr(agent_mod, "launch_browser", fake_launch_browser)
    monkeypatch.setattr(agent_mod, "close_browser", fake_close_browser)
    monkeypatch.setattr(agent_mod, "_teardown_logging", lambda: None)
    monkeypatch.setattr(agent_mod, "capture_snapshot", fake_capture_snapshot)
    monkeypatch.setattr(agent_mod, "write_run_summary", fake_write_run_summary)
    monkeypatch.setattr(agent_mod, "_build_model", lambda _config: object())
    monkeypatch.setattr(agent_mod, "_model_settings", lambda _config: {})
    monkeypatch.setattr(agent_mod, "usage_stats_from_result", lambda _res: UsageStats(0, 0, 0, 0, 0, 0, 0, 0, 0))
    monkeypatch.setattr(agent_mod, "cost_stats_from_result", lambda _res, _model: None)

    monkeypatch.setattr(agent_mod, "build_snapshot_filter_agent", lambda *_a, **_k: _StubAgent(filter_runner))
    monkeypatch.setattr(agent_mod, "build_orchestrator_agent", lambda *_a, **_k: _StubAgent(orchestrator_runner))
    monkeypatch.setattr(agent_mod, "build_browser_worker_agent", lambda *_a, **_k: _StubAgent(worker_runner))

    agent = agent_mod.BrowserAgent(
        AgentConfig(
            target_url="https://example.com",
            goal="Test goal",
            max_steps=2,
            log_dir=str(tmp_path),
            metrics_enabled=False,
        ),
        LLMConfig(),
        BrowserConfig(headless=True),
    )
    await agent.run()

    assert filter_calls["count"] == 1
    assert captured_summary.get("steps") == 2


@pytest.mark.asyncio
async def test_filter_reruns_when_fingerprint_changes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    snapshots = [
        _snapshot(url="https://example.com", title="Test", element_name="Next"),
        _snapshot(url="https://example.com", title="Test", element_name="Continue"),
    ]

    async def fake_launch_browser(_config: BrowserConfig) -> _StubSession:
        return _StubSession(page=_StubPage(url="https://example.com"), cdp_session=object(), frame_sessions={})

    async def fake_close_browser(_session: _StubSession) -> None:
        return None

    async def fake_capture_snapshot(_page: _StubPage, _cdp_session: Any) -> PageSnapshot:
        return snapshots.pop(0)

    filter_calls = {"count": 0}

    def filter_runner(_prompt: str, _deps: Any | None) -> SnapshotFilterOutput:
        filter_calls["count"] += 1
        return SnapshotFilterOutput(useful_text_lines=[], priority_element_ids=["el_1"], notes=None)

    monkeypatch.setattr(agent_mod, "launch_browser", fake_launch_browser)
    monkeypatch.setattr(agent_mod, "close_browser", fake_close_browser)
    monkeypatch.setattr(agent_mod, "_teardown_logging", lambda: None)
    monkeypatch.setattr(agent_mod, "capture_snapshot", fake_capture_snapshot)
    monkeypatch.setattr(agent_mod, "write_run_summary", lambda **_k: tmp_path / "run_summary.json")
    monkeypatch.setattr(agent_mod, "_build_model", lambda _config: object())
    monkeypatch.setattr(agent_mod, "_model_settings", lambda _config: {})
    monkeypatch.setattr(agent_mod, "usage_stats_from_result", lambda _res: UsageStats(0, 0, 0, 0, 0, 0, 0, 0, 0))
    monkeypatch.setattr(agent_mod, "cost_stats_from_result", lambda _res, _model: None)

    monkeypatch.setattr(agent_mod, "build_snapshot_filter_agent", lambda *_a, **_k: _StubAgent(filter_runner))
    monkeypatch.setattr(agent_mod, "build_orchestrator_agent", lambda *_a, **_k: _StubAgent(lambda *_: OrchestratorDecision(done=False, worker_goal="noop")))
    monkeypatch.setattr(agent_mod, "build_browser_worker_agent", lambda *_a, **_k: _StubAgent(lambda *_: StepOutput(done=False, summary="noop", next_goal=None)))

    agent = agent_mod.BrowserAgent(
        AgentConfig(
            target_url="https://example.com",
            goal="Test goal",
            max_steps=2,
            log_dir=str(tmp_path),
            metrics_enabled=False,
        ),
        LLMConfig(),
        BrowserConfig(headless=True),
    )
    await agent.run()

    assert filter_calls["count"] == 2


@pytest.mark.asyncio
async def test_abort_when_fingerprint_unchanged_for_threshold(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    snapshots = [_snapshot(url="https://example.com", title="Test", element_name="Next") for _ in range(10)]

    async def fake_launch_browser(_config: BrowserConfig) -> _StubSession:
        return _StubSession(page=_StubPage(url="https://example.com"), cdp_session=object(), frame_sessions={})

    async def fake_close_browser(_session: _StubSession) -> None:
        return None

    async def fake_capture_snapshot(_page: _StubPage, _cdp_session: Any) -> PageSnapshot:
        return snapshots.pop(0)

    captured_summary: dict[str, Any] = {}

    def fake_write_run_summary(*, log_dir: str, run_id: str, summary: dict[str, Any], filename: str = "run_summary.json") -> Path:
        captured_summary.update(summary)
        return tmp_path / filename

    monkeypatch.setattr(agent_mod, "launch_browser", fake_launch_browser)
    monkeypatch.setattr(agent_mod, "close_browser", fake_close_browser)
    monkeypatch.setattr(agent_mod, "_teardown_logging", lambda: None)
    monkeypatch.setattr(agent_mod, "capture_snapshot", fake_capture_snapshot)
    monkeypatch.setattr(agent_mod, "write_run_summary", fake_write_run_summary)
    monkeypatch.setattr(agent_mod, "_build_model", lambda _config: object())
    monkeypatch.setattr(agent_mod, "_model_settings", lambda _config: {})
    monkeypatch.setattr(agent_mod, "usage_stats_from_result", lambda _res: UsageStats(0, 0, 0, 0, 0, 0, 0, 0, 0))
    monkeypatch.setattr(agent_mod, "cost_stats_from_result", lambda _res, _model: None)

    monkeypatch.setattr(
        agent_mod,
        "build_snapshot_filter_agent",
        lambda *_a, **_k: _StubAgent(lambda *_: SnapshotFilterOutput(useful_text_lines=[], priority_element_ids=[], notes=None)),
    )
    monkeypatch.setattr(agent_mod, "build_orchestrator_agent", lambda *_a, **_k: _StubAgent(lambda *_: OrchestratorDecision(done=False, worker_goal="noop")))
    monkeypatch.setattr(agent_mod, "build_browser_worker_agent", lambda *_a, **_k: _StubAgent(lambda *_: StepOutput(done=False, summary="noop", next_goal=None)))

    agent = agent_mod.BrowserAgent(
        AgentConfig(
            target_url="https://example.com",
            goal="Test goal",
            max_steps=10,
            unchanged_abort_threshold=3,
            log_dir=str(tmp_path),
            metrics_enabled=False,
        ),
        LLMConfig(),
        BrowserConfig(headless=True),
    )
    await agent.run()

    assert captured_summary.get("stop_reason") == "unchanged_fingerprint_abort"
    assert captured_summary.get("steps") == 4

