from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.browser.external import (
    AsyncSyncCDPSession,
    AsyncSyncFrame,
    build_external_browser_session,
)


@dataclass
class _SyncCDP:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    detached: bool = False

    def send(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        return {"ok": True, "method": method, "params": params}

    def detach(self) -> None:
        self.detached = True


@dataclass
class _SyncFrame:
    url: str = "https://example.test/frame"
    name: str = "child"
    child_frames: list["_SyncFrame"] = field(default_factory=list)


@dataclass
class _SyncKeyboard:
    pressed: list[str] = field(default_factory=list)

    def press(self, key: str) -> None:
        self.pressed.append(key)


@dataclass
class _SyncContext:
    cdp: _SyncCDP
    targets: list[Any] = field(default_factory=list)

    def new_cdp_session(self, target: Any) -> _SyncCDP:
        self.targets.append(target)
        return self.cdp


@dataclass
class _SyncPage:
    url: str = "https://example.test"
    cdp: _SyncCDP = field(default_factory=_SyncCDP)
    keyboard: _SyncKeyboard = field(default_factory=_SyncKeyboard)
    main_frame: _SyncFrame = field(default_factory=_SyncFrame)
    frames: list[_SyncFrame] = field(default_factory=list)
    evaluations: list[tuple[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.context = _SyncContext(self.cdp)
        if not self.frames:
            self.frames = [self.main_frame]

    def evaluate(self, expression: str, arg: Any = None) -> dict[str, Any]:
        self.evaluations.append((expression, arg))
        return {"expression": expression, "arg": arg}

    def wait_for_load_state(self, state: str, **kwargs: Any) -> None:
        self.evaluations.append((f"wait:{state}", kwargs))

    def wait_for_timeout(self, timeout: float) -> None:
        self.evaluations.append(("timeout", timeout))

    def title(self) -> str:
        return "Example"

    def goto(self, url: str) -> None:
        self.url = url

    def screenshot(self, **kwargs: Any) -> bytes:
        self.evaluations.append(("screenshot", kwargs))
        return b"png"


def test_sync_playwright_page_is_wrapped_as_async_session() -> None:
    page = _SyncPage()
    session = build_external_browser_session(page)

    assert session.page.url == "https://example.test"
    assert isinstance(session.cdp_session, AsyncSyncCDPSession)
    assert page.context.targets == [page]

    result = asyncio.run(session.cdp_session.send("Runtime.evaluate", {"expression": "1"}))
    assert result["method"] == "Runtime.evaluate"
    assert page.cdp.calls == [("Runtime.evaluate", {"expression": "1"})]


def test_sync_page_context_and_frame_methods_unwrap_targets() -> None:
    page = _SyncPage()
    session = build_external_browser_session(page)

    frame = session.page.main_frame
    assert isinstance(frame, AsyncSyncFrame)
    frame_session = asyncio.run(session.page.context.new_cdp_session(frame))

    assert isinstance(frame_session, AsyncSyncCDPSession)
    assert page.context.targets[-1] is page.main_frame

    asyncio.run(session.page.keyboard.press("Enter"))
    assert page.keyboard.pressed == ["Enter"]
