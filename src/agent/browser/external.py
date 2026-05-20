"""Adapters for externally owned Playwright browser sessions."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any


def _is_async_method(obj: Any, name: str) -> bool:
    method = getattr(obj, name, None)
    return inspect.iscoroutinefunction(method)


def _unwrap(value: Any) -> Any:
    return getattr(value, "_inner", value)


class AsyncSyncCDPSession:
    """Async-shaped wrapper around BrowserGym's sync CDP session."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def send(self, method: str, params: dict[str, Any] | None = None) -> Any:
        return self._inner.send(method, params or {})

    async def detach(self) -> None:
        return self._inner.detach()


class AsyncSyncKeyboard:
    """Async-shaped wrapper around Playwright sync Keyboard."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def press(self, key: str) -> None:
        self._inner.press(key)


class AsyncSyncFrame:
    """Async-compatible facade for a Playwright sync Frame."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @property
    def child_frames(self) -> list[AsyncSyncFrame]:
        return [AsyncSyncFrame(frame) for frame in self._inner.child_frames]

    @property
    def url(self) -> str:
        return self._inner.url

    @property
    def name(self) -> str:
        return self._inner.name


class AsyncSyncBrowserContext:
    """Async-shaped wrapper around Playwright sync BrowserContext."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def new_cdp_session(self, page_or_frame: Any) -> AsyncSyncCDPSession:
        return AsyncSyncCDPSession(self._inner.new_cdp_session(_unwrap(page_or_frame)))

    @property
    def pages(self) -> list[Any]:
        return list(getattr(self._inner, "pages", []))


class AsyncSyncPage:
    """Async-compatible facade for a Playwright sync Page.

    BrowserGym exposes sync Playwright objects in raw observations, while this
    agent's runtime is async. This facade keeps browser ownership in
    BrowserGym and lets the existing async CDP/snapshot/tool code run unchanged.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @property
    def url(self) -> str:
        return self._inner.url

    @property
    def context(self) -> AsyncSyncBrowserContext:
        return AsyncSyncBrowserContext(self._inner.context)

    @property
    def main_frame(self) -> AsyncSyncFrame:
        return AsyncSyncFrame(self._inner.main_frame)

    @property
    def frames(self) -> list[AsyncSyncFrame]:
        return [AsyncSyncFrame(frame) for frame in self._inner.frames]

    @property
    def keyboard(self) -> AsyncSyncKeyboard:
        return AsyncSyncKeyboard(self._inner.keyboard)

    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        if arg is None:
            return self._inner.evaluate(expression)
        return self._inner.evaluate(expression, arg)

    async def goto(self, url: str) -> Any:
        return self._inner.goto(url)

    async def screenshot(self, **kwargs: Any) -> bytes:
        return self._inner.screenshot(**kwargs)

    async def title(self) -> str:
        return self._inner.title()

    async def wait_for_load_state(self, state: str = "load", **kwargs: Any) -> None:
        call_kwargs = {key: value for key, value in kwargs.items() if value is not None}
        self._inner.wait_for_load_state(state, **call_kwargs)

    async def wait_for_timeout(self, timeout: float) -> None:
        self._inner.wait_for_timeout(timeout)


@dataclass
class ExternalBrowserSession:
    """Browser session wrapper for pages owned by another harness."""

    page: Any
    cdp_session: Any
    context: Any
    frame_sessions: dict[str, Any] = field(default_factory=dict)

    async def detach(self) -> None:
        for frame_session in list(self.frame_sessions.values()):
            try:
                await frame_session.detach()
            except Exception:
                pass
        self.frame_sessions.clear()
        try:
            await self.cdp_session.detach()
        except Exception:
            pass


def build_external_browser_session(page: Any) -> ExternalBrowserSession:
    """Wrap an externally owned Playwright page for the async agent runtime."""

    if _is_async_method(page, "evaluate"):
        raise TypeError(
            "build_external_browser_session expects BrowserGym's sync Playwright page"
        )
    else:
        async_page = AsyncSyncPage(page)
        context = async_page.context
        cdp_session = AsyncSyncCDPSession(page.context.new_cdp_session(page))

    return ExternalBrowserSession(
        page=async_page,
        cdp_session=cdp_session,
        context=context,
        frame_sessions={},
    )
