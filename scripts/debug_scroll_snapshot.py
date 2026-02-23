"""Debug snapshot output for scrollable containers."""

from __future__ import annotations

import argparse
import asyncio

from src.agent.browser.session import close_browser, launch_browser
from src.agent.config import BrowserConfig
from src.agent.context.handlers import cleanup_handler_attributes, extract_handlers
from src.agent.context.scroll_containers import (
    cleanup_scroll_container_attributes,
    extract_scroll_containers,
)
from src.agent.context.snapshot import ElementSnapshot, capture_snapshot


def _element_brief(element: ElementSnapshot) -> str:
    parts = []
    if element.role:
        parts.append(element.role.strip())
    if element.name:
        parts.append(f'name="{element.name.strip()}"')
    if element.text:
        parts.append(f'text="{element.text.strip()}"')
    tag = element.node_name or ""
    attrs = element.attributes or {}
    attr_bits = []
    for key in ("id", "class", "aria-label", "placeholder"):
        if key in attrs and attrs[key]:
            attr_bits.append(f"{key}={attrs[key]}")
    attr_str = " ".join(attr_bits)
    bbox = ""
    if element.bounding_box:
        x, y, w, h = element.bounding_box
        bbox = f" bbox={int(round(x))},{int(round(y))},{int(round(w))},{int(round(h))}"
    return f"{element.stable_id} {tag} {' '.join(parts)} {attr_str}{bbox}".strip()


async def _run(url: str | None, html_path: str | None, *, headless: bool) -> None:
    session = await launch_browser(BrowserConfig(headless=headless))
    try:
        page = session.page
        if html_path:
            with open(html_path, "r", encoding="utf-8") as handle:
                html = handle.read()
            await page.set_content(html, wait_until="domcontentloaded")
        elif url:
            await page.goto(url, wait_until="domcontentloaded")
        else:
            raise ValueError("Provide --url or --html")
        await extract_scroll_containers(page)
        handlers = await extract_handlers(page)
        snapshot = await capture_snapshot(page, session.cdp_session, handler_map=handlers)
        await cleanup_handler_attributes(page)
        await cleanup_scroll_container_attributes(page)

        scroll_elements = [
            element
            for element in snapshot.elements
            if element.interactive_reason == "scroll_container"
        ]
        print(f"Scroll containers found: {len(scroll_elements)}")
        for element in scroll_elements[:25]:
            print(f"- {_element_brief(element)}")

        scroll_text = [line for line in snapshot.raw_text if "scroll" in line.lower()]
        if scroll_text:
            print("\nRaw text lines containing 'scroll':")
            for line in scroll_text[:20]:
                print(f"- {line}")
    finally:
        await close_browser(session)


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect scrollable containers in snapshots.")
    parser.add_argument("--url", help="Target URL to inspect.")
    parser.add_argument("--html", help="Path to a saved HTML file to inspect.")
    parser.add_argument("--headless", action="store_true", help="Run browser headless.")
    args = parser.parse_args()
    asyncio.run(_run(args.url, args.html, headless=args.headless))


if __name__ == "__main__":
    main()
