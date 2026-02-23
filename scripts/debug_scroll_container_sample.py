"""Minimal HTML sample to verify scroll container detection."""

from __future__ import annotations

import asyncio

from src.agent.browser.session import close_browser, launch_browser
from src.agent.config import BrowserConfig
from src.agent.context.handlers import cleanup_handler_attributes, extract_handlers
from src.agent.context.scroll_containers import (
    cleanup_scroll_container_attributes,
    extract_scroll_containers,
)
from src.agent.context.snapshot import capture_snapshot


HTML_SAMPLE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Scroll Sample</title>
    <style>
      .scroll-box {
        width: 300px;
        height: 120px;
        overflow: auto;
        border: 2px solid #444;
      }
      .content { height: 500px; padding: 8px; }
    </style>
  </head>
  <body>
    <div class="scroll-box" id="scroll-box">
      <div class="content">Scroll inside this box</div>
    </div>
  </body>
</html>
"""


async def main() -> None:
    session = await launch_browser(BrowserConfig(headless=True))
    try:
        page = session.page
        await page.set_content(HTML_SAMPLE, wait_until="domcontentloaded")
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
        for element in scroll_elements:
            print(
                f"- {element.stable_id} {element.node_name}"
                f" class={element.attributes.get('class', '')}"
                f" bbox={element.bounding_box}"
            )
    finally:
        await close_browser(session)


if __name__ == "__main__":
    asyncio.run(main())
