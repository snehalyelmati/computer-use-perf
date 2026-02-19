"""Detect scrollable containers and stamp DOM nodes for snapshot capture."""

from __future__ import annotations

import logging

from playwright.async_api import Page

logger = logging.getLogger(__name__)

_MARK_SCROLL_CONTAINERS_JS = """
(() => {
  const els = document.querySelectorAll('*');
  let count = 0;
  for (const el of els) {
    if (!(el instanceof HTMLElement)) continue;
    if (el.tagName === 'HTML' || el.tagName === 'BODY') continue;
    const style = window.getComputedStyle(el);
    const overflowValues = new Set([
      style.overflow,
      style.overflowX,
      style.overflowY,
    ]);
    const canScrollByStyle =
      overflowValues.has('auto') || overflowValues.has('scroll') || overflowValues.has('overlay');
    const canScrollByContent =
      (el.scrollHeight - el.clientHeight) > 1 || (el.scrollWidth - el.clientWidth) > 1;
    if (canScrollByStyle && canScrollByContent) {
      el.setAttribute('data-agent-scroll', '1');
      count += 1;
    }
  }
  return count;
})()
"""

_CLEANUP_SCROLL_CONTAINERS_JS = """
(() => {
  const els = document.querySelectorAll('[data-agent-scroll]');
  for (const el of els) {
    el.removeAttribute('data-agent-scroll');
  }
})()
"""


async def extract_scroll_containers(page: Page) -> int:
    """Mark scrollable elements before snapshot capture. Use when scrollable containers should be interactable. Runs a single page.evaluate()."""
    try:
        result = await page.evaluate(_MARK_SCROLL_CONTAINERS_JS)
        if isinstance(result, int):
            return result
        return 0
    except Exception:
        logger.debug("Scroll container marking failed", exc_info=True)
        return 0


async def cleanup_scroll_container_attributes(page: Page) -> None:
    """Remove ``data-agent-scroll`` markers after snapshot capture. Use after extract_scroll_containers(). Runs a single page.evaluate()."""
    try:
        await page.evaluate(_CLEANUP_SCROLL_CONTAINERS_JS)
    except Exception:
        logger.debug("Scroll container cleanup failed", exc_info=True)
