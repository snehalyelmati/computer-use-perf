"""Playwright + CDP browser session management."""

from __future__ import annotations

from dataclasses import dataclass, field

from playwright.async_api import (
    Browser,
    BrowserContext,
    CDPSession,
    Page,
    Playwright,
    async_playwright,
)

from src.agent.config import BrowserConfig


@dataclass
class BrowserSession:
    """Holds the active Playwright browser objects."""

    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page
    cdp_session: CDPSession
    frame_sessions: dict[str, CDPSession] = field(default_factory=dict)


async def launch_browser(config: BrowserConfig) -> BrowserSession:
    """Launch a Playwright browser and return the session wrapper."""

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=config.headless)
    context = await browser.new_context(
        viewport={"width": config.viewport_width, "height": config.viewport_height}
    )
    page = await context.new_page()
    cdp_session = await context.new_cdp_session(page)
    return BrowserSession(
        playwright=playwright,
        browser=browser,
        context=context,
        page=page,
        cdp_session=cdp_session,
        frame_sessions={},
    )

async def close_browser(session: BrowserSession) -> None:
    """Close the Playwright browser session."""

    for frame_session in session.frame_sessions.values():
        try:
            await frame_session.detach()
        except Exception:
            continue
    await session.cdp_session.detach()
    await session.context.close()
    await session.browser.close()
    await session.playwright.stop()
