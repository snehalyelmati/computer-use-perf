"""Playwright + CDP browser session management."""

from __future__ import annotations

import asyncio
import logging
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

logger = logging.getLogger(__name__)

_CLOSE_TIMEOUT_SECONDS = 10


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
    """Close the Playwright browser session, tolerating individual failures."""
    try:
        await asyncio.wait_for(
            _close_browser_inner(session), timeout=_CLOSE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Browser close timed out after %ss; some processes may be orphaned",
            _CLOSE_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.warning("Unexpected error during browser close", exc_info=True)


async def _close_browser_inner(session: BrowserSession) -> None:
    for frame_session in session.frame_sessions.values():
        try:
            await frame_session.detach()
        except Exception:
            pass
    for label, method in [
        ("cdp_session.detach", session.cdp_session.detach),
        ("context.close", session.context.close),
        ("browser.close", session.browser.close),
        ("playwright.stop", session.playwright.stop),
    ]:
        try:
            await method()
        except Exception:
            logger.warning("Failed to %s during browser close", label, exc_info=True)
