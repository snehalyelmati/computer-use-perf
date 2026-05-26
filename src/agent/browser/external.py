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


def _to_impl(value: Any) -> Any:
    unwrapped = _unwrap(value)
    return getattr(unwrapped, "_impl_obj", unwrapped)


def _has_playwright_impl(value: Any) -> bool:
    return getattr(value, "_impl_obj", None) is not None


def _playwright_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    aliases = {
        "full_page": "fullPage",
        "mask_color": "maskColor",
        "omit_background": "omitBackground",
        "wait_until": "waitUntil",
    }
    return {aliases.get(key, key): value for key, value in kwargs.items() if value is not None}


class AsyncPlaywrightImplCDPSession:
    """Async wrapper around BrowserGym's underlying Playwright CDP session."""

    def __init__(self, inner: Any) -> None:
        self._inner = _to_impl(inner)

    async def send(self, method: str, params: dict[str, Any] | None = None) -> Any:
        return await self._inner.send(method, params or {})

    async def detach(self) -> None:
        await self._inner.detach()


class AsyncPlaywrightImplKeyboard:
    """Async wrapper around BrowserGym's underlying Playwright Keyboard."""

    def __init__(self, inner: Any) -> None:
        self._inner = _to_impl(inner)

    async def press(self, key: str) -> None:
        await self._inner.press(key)

    async def type(self, text: str) -> None:
        await self._inner.type(text)


class AsyncPlaywrightImplFrame:
    """Async-compatible facade for an underlying Playwright Frame."""

    def __init__(self, inner: Any) -> None:
        self._inner = _to_impl(inner)

    @property
    def child_frames(self) -> list["AsyncPlaywrightImplFrame"]:
        return [AsyncPlaywrightImplFrame(frame) for frame in self._inner.child_frames]

    @property
    def url(self) -> str:
        return self._inner.url

    @property
    def name(self) -> str:
        return self._inner.name


class AsyncPlaywrightImplBrowserContext:
    """Async wrapper around BrowserGym's underlying Playwright BrowserContext."""

    def __init__(self, inner: Any) -> None:
        self._inner = _to_impl(inner)

    async def new_cdp_session(self, page_or_frame: Any) -> AsyncPlaywrightImplCDPSession:
        return AsyncPlaywrightImplCDPSession(
            await self._inner.new_cdp_session(_to_impl(page_or_frame))
        )

    @property
    def pages(self) -> list[Any]:
        return [AsyncPlaywrightImplPage(page) for page in getattr(self._inner, "pages", [])]


class AsyncPlaywrightImplPage:
    """Async facade over BrowserGym's sync page using Playwright's impl object."""

    def __init__(self, inner: Any) -> None:
        self._inner = _to_impl(inner)

    @property
    def url(self) -> str:
        return self._inner.url

    @property
    def context(self) -> AsyncPlaywrightImplBrowserContext:
        return AsyncPlaywrightImplBrowserContext(self._inner.context)

    @property
    def main_frame(self) -> AsyncPlaywrightImplFrame:
        return AsyncPlaywrightImplFrame(self._inner.main_frame)

    @property
    def frames(self) -> list[AsyncPlaywrightImplFrame]:
        return [AsyncPlaywrightImplFrame(frame) for frame in self._inner.frames]

    @property
    def keyboard(self) -> AsyncPlaywrightImplKeyboard:
        return AsyncPlaywrightImplKeyboard(self._inner.keyboard)

    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        return await self._inner.evaluate(expression, arg)

    async def goto(self, url: str, **kwargs: Any) -> Any:
        return await self._inner.goto(url, **_playwright_kwargs(kwargs))

    async def screenshot(self, **kwargs: Any) -> bytes:
        return await self._inner.screenshot(**_playwright_kwargs(kwargs))

    async def title(self) -> str:
        return await self._inner.title()

    async def wait_for_load_state(self, state: str = "load", **kwargs: Any) -> None:
        await self._inner.wait_for_load_state(state, **_playwright_kwargs(kwargs))

    async def wait_for_timeout(self, timeout: float) -> None:
        await self._inner.wait_for_timeout(timeout)


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

    async def type(self, text: str) -> None:
        self._inner.type(text)


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


async def build_external_browser_session_async(page: Any) -> ExternalBrowserSession:
    """Wrap a BrowserGym page while preserving Playwright's owning event loop."""

    if _is_async_method(page, "evaluate"):
        raise TypeError(
            "build_external_browser_session_async expects BrowserGym's sync Playwright page"
        )
    if _has_playwright_impl(page):
        async_page = AsyncPlaywrightImplPage(page)
        context = async_page.context
        cdp_session = await context.new_cdp_session(async_page)
        return ExternalBrowserSession(
            page=async_page,
            cdp_session=cdp_session,
            context=context,
            frame_sessions={},
        )
    return build_external_browser_session(page)
