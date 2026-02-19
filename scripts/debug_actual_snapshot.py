"""Debug: run the ACTUAL extract_handlers + capture_snapshot pipeline
and check whether the challenge div appears in the snapshot.
"""

import asyncio
import sys

sys.path.insert(0, ".")

from playwright.async_api import async_playwright

from src.agent.context.handlers import (
    cleanup_handler_attributes,
    extract_handlers,
    format_handlers_for_llm,
    prioritize_handlers,
)
from src.agent.context.snapshot import capture_snapshot, format_snapshot_for_llm

TARGET_URL = "https://serene-frangipane-7fd25b.netlify.app/"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        page = await context.new_page()
        cdp = await context.new_cdp_session(page)

        await page.goto(TARGET_URL, wait_until="networkidle")
        await page.wait_for_timeout(2000)
        await page.click("button:has-text('START')")
        await page.wait_for_timeout(3000)

        print(f"URL: {page.url}")

        # Step 1: Extract handlers (stamps data-agent-hid)
        handler_map = await extract_handlers(page)
        print(f"\nHandler extraction: {len(handler_map)} elements with handlers")

        # Show challenge div handler
        for hid, handlers in handler_map.items():
            # Check if this is the challenge div by looking at click handler
            click = handlers.get("click", "")
            if "=>v(h" in click or "cursor-pointer" in click.lower():
                print(f"  hid={hid}: {handlers}")

        # Step 2: Capture snapshot (the ACTUAL function from the codebase)
        snapshot = await capture_snapshot(page, cdp, handler_map=handler_map)
        print(f"\nSnapshot: {len(snapshot.elements)} elements")

        # Step 3: Check if challenge div is in the snapshot
        challenge_found = False
        for el in snapshot.elements:
            name = el.name or ""
            text = el.text or ""
            tag = el.node_name or ""
            combined = f"{name} {text}".lower()
            if "hidden dom" in combined:
                challenge_found = True
                print(f"\n  CHALLENGE DIV FOUND in snapshot:")
                print(f"    stable_id: {el.stable_id}")
                print(f"    node_name: {el.node_name}")
                print(f"    name: {el.name}")
                print(f"    text: {(el.text or '')[:80]}")
                print(f"    role: {el.role}")
                print(f"    reason: {el.interactive_reason}")
                print(f"    confidence: {el.interactive_confidence}")
                print(f"    attributes: {el.attributes}")
                if el.handlers:
                    hint = format_handlers_for_llm(el.handlers)
                    print(f"    handlers: {hint}")

            # Also check for cursor-pointer elements
            if el.interactive_reason == "cursor_pointer":
                print(f"  cursor_pointer: {el.stable_id} tag={tag} name=\"{name[:40]}\"")

            if el.interactive_reason == "detected_handler" and tag.upper() == "DIV":
                print(f"  detected_handler DIV: {el.stable_id} name=\"{name[:40]}\" text=\"{text[:40]}\"")

        if not challenge_found:
            print("\n  CHALLENGE DIV NOT FOUND in snapshot by text!")
            print("  Checking all DIV elements for the click counter handler:")
            for el in snapshot.elements:
                tag = (el.node_name or "").upper()
                if tag == "DIV" and el.handlers:
                    hint = format_handlers_for_llm(el.handlers)
                    print(f"    {el.stable_id}: reason={el.interactive_reason} "
                          f"handlers={hint} "
                          f"attrs={el.attributes}")
                    # Check if this is the challenge div by handler content
                    click_h = el.handlers.get("click", "")
                    if "=>v(h" in click_h or "h=>h+1" in click_h:
                        print(f"      ^^^ THIS IS THE CHALLENGE DIV (click counter)")

        # Step 4: Show all elements with their details
        print(f"\n--- All snapshot elements ({len(snapshot.elements)}) ---")
        for el in snapshot.elements:
            tag = (el.node_name or "").upper()
            name = el.name or ""
            reason = el.interactive_reason or ""
            handlers_str = ""
            if el.handlers:
                handlers_str = format_handlers_for_llm(el.handlers)
            attrs_str = ""
            if el.attributes:
                attrs_str = str({k: v[:30] for k, v in el.attributes.items() if v})[:80]
            print(f"  {el.stable_id}: {tag:<8} reason={reason:<20} "
                  f"name=\"{name[:25]}\" {handlers_str[:50]} {attrs_str[:60]}")

        # Cleanup
        await cleanup_handler_attributes(page)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
