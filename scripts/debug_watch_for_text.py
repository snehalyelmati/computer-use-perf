#!/usr/bin/env python3
"""Debug script for watch_for_text tool.

Launches a browser with a minimal test page and exercises:
  1. Delayed appearance — button injected after 2s → watch finds and clicks it
  2. Immediate find — text already present → clicks immediately
  3. Timeout path — non-existent text → ok=False
  4. Empty text — validation error
"""

import asyncio
import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.async_api import async_playwright

from src.agent.tools.semantic import watch_for_text, ToolContext, ToolResult

TEST_HTML = """
<!DOCTYPE html>
<html>
<head><title>watch_for_text test</title></head>
<body>
  <div id="status">waiting</div>
  <button id="existing-btn">Existing Button</button>
  <script>
    // Inject a button after 2 seconds
    setTimeout(() => {
      const btn = document.createElement('button');
      btn.id = 'delayed-btn';
      btn.textContent = 'Delayed Button';
      btn.addEventListener('click', () => {
        document.getElementById('status').textContent = 'delayed-clicked';
      });
      document.body.appendChild(btn);
    }, 2000);

    // Wire up existing button
    document.getElementById('existing-btn').addEventListener('click', () => {
      document.getElementById('status').textContent = 'existing-clicked';
    });
  </script>
</body>
</html>
"""


class MinimalToolContext:
    """Minimal stand-in for ToolContext for testing watch_for_text."""

    def __init__(self, page):
        self.page = page
        self.last_tool = None
        self.last_element_id = None


async def run_tests():
    passed = 0
    failed = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        # --- Test 1: Delayed appearance ---
        print("\n[Test 1] Delayed appearance (button injected after 2s)")
        page = await browser.new_page()
        await page.set_content(TEST_HTML)
        ctx = MinimalToolContext(page)
        result = await watch_for_text("Delayed Button", ctx, timeout_ms=5000)
        status = await page.text_content("#status")
        if result.ok and status == "delayed-clicked":
            print(f"  PASS: {result.message}  (status={status})")
            passed += 1
        else:
            print(f"  FAIL: ok={result.ok} message={result.message} status={status}")
            failed += 1
        await page.close()

        # --- Test 2: Immediate find ---
        print("\n[Test 2] Immediate find (text already present)")
        page = await browser.new_page()
        await page.set_content(TEST_HTML)
        ctx = MinimalToolContext(page)
        result = await watch_for_text("Existing Button", ctx, timeout_ms=3000)
        status = await page.text_content("#status")
        if result.ok and status == "existing-clicked":
            print(f"  PASS: {result.message}  (status={status})")
            passed += 1
        else:
            print(f"  FAIL: ok={result.ok} message={result.message} status={status}")
            failed += 1
        await page.close()

        # --- Test 3: Timeout path ---
        print("\n[Test 3] Timeout (non-existent text)")
        page = await browser.new_page()
        await page.set_content(TEST_HTML)
        ctx = MinimalToolContext(page)
        result = await watch_for_text("NoSuchText", ctx, timeout_ms=1000)
        if not result.ok and "timeout" in result.message.lower():
            print(f"  PASS: {result.message}")
            passed += 1
        else:
            print(f"  FAIL: ok={result.ok} message={result.message}")
            failed += 1
        await page.close()

        # --- Test 4: Empty text validation ---
        print("\n[Test 4] Empty text validation")
        page = await browser.new_page()
        await page.set_content(TEST_HTML)
        ctx = MinimalToolContext(page)
        result = await watch_for_text("", ctx)
        if not result.ok and "empty" in result.message.lower():
            print(f"  PASS: {result.message}")
            passed += 1
        else:
            print(f"  FAIL: ok={result.ok} message={result.message}")
            failed += 1
        await page.close()

        await browser.close()

    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    ok = asyncio.run(run_tests())
    sys.exit(0 if ok else 1)
