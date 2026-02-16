from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any

import pytest
from pydantic_ai.models.test import TestModel

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.config import AgentConfig, BrowserConfig, LLMConfig
from src.agent.context.snapshot import PageSnapshot
from src.agent.core import agent as agent_mod
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


@pytest.mark.asyncio
async def test_run_does_not_stop_on_worker_done(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    orchestrator_model = TestModel(
        call_tools=[],
        custom_output_args=OrchestratorDecision(done=False, worker_goal="noop"),
    )
    filter_model = TestModel(
        call_tools=[],
        custom_output_args=SnapshotFilterOutput(useful_text_lines=[], priority_element_ids=[], notes=None),
    )
    worker_model = TestModel(
        call_tools=[],
        custom_output_args=StepOutput(done=True, summary="delegated goal complete", next_goal=None),
    )

    async def fake_launch_browser(_config: BrowserConfig) -> _StubSession:
        return _StubSession(page=_StubPage(), cdp_session=object(), frame_sessions={})

    async def fake_close_browser(_session: _StubSession) -> None:
        return None

    async def fake_capture_snapshot(_page: _StubPage, _cdp_session: Any) -> PageSnapshot:
        return PageSnapshot(url=_page.url, title="Test", elements=[], raw_text=[])

    captured_summary: dict[str, Any] = {}

    def fake_write_run_summary(*, log_dir: str, run_id: str, summary: dict[str, Any], filename: str = "run_summary.json") -> Path:
        captured_summary.update(summary)
        return tmp_path / filename

    orig_build_orchestrator = agent_mod.build_orchestrator_agent
    orig_build_worker = agent_mod.build_browser_worker_agent
    orig_build_filter = agent_mod.build_snapshot_filter_agent

    monkeypatch.setattr(agent_mod, "launch_browser", fake_launch_browser)
    monkeypatch.setattr(agent_mod, "close_browser", fake_close_browser)
    monkeypatch.setattr(agent_mod, "capture_snapshot", fake_capture_snapshot)
    monkeypatch.setattr(agent_mod, "write_run_summary", fake_write_run_summary)
    monkeypatch.setattr(agent_mod, "_build_openrouter_model", lambda _config: object())
    monkeypatch.setattr(agent_mod, "_model_settings", lambda _config: {})

    monkeypatch.setattr(
        agent_mod,
        "build_orchestrator_agent",
        lambda _model, *, model_settings: orig_build_orchestrator(orchestrator_model, model_settings=model_settings),
    )
    monkeypatch.setattr(
        agent_mod,
        "build_snapshot_filter_agent",
        lambda _model, *, model_settings: orig_build_filter(filter_model, model_settings=model_settings),
    )
    monkeypatch.setattr(
        agent_mod,
        "build_browser_worker_agent",
        lambda _model, *, model_settings: orig_build_worker(worker_model, model_settings=model_settings),
    )

    agent = agent_mod.BrowserAgent(
        AgentConfig(
            target_url="https://example.com",
            goal="Solve as many challenges as you can",
            max_steps=2,
            log_dir=str(tmp_path),
            metrics_enabled=False,
        ),
        LLMConfig(),
        BrowserConfig(headless=True),
    )
    await agent.run()

    assert captured_summary.get("steps") == 2
