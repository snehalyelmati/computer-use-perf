"""Playwright + CDP browser session management."""

from __future__ import annotations

from dataclasses import dataclass

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from src.agent.config import BrowserConfig


@dataclass
class BrowserSession:
    """Holds the active Playwright browser objects."""

    browser: Browser
    context: BrowserContext
    page: Page


async def launch_browser(config: BrowserConfig) -> BrowserSession:
    """Launch a Playwright browser and return the session wrapper."""

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=config.headless)
    context = await browser.new_context(
        viewport={"width": config.viewport_width, "height": config.viewport_height}
    )
    page = await context.new_page()
    return BrowserSession(browser=browser, context=context, page=page)
