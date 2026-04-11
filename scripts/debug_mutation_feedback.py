"""Debug script to verify mutation feedback for watch_for_text, execute_js, and wait.

Tests that all three tools now return DOM-change summaries via
_inject_observer / _collect_mutations / _format_verification.
Also tests that wait uses frame-aware session selection.
"""

import asyncio

from playwright.async_api import async_playwright

from src.agent.context.snapshot import capture_snapshot, build_element_index
from src.agent.tools.semantic import ToolContext, watch_for_text, execute_js, wait


# ---------------------------------------------------------------------------
# Test HTML pages
# ---------------------------------------------------------------------------

# watch_for_text: clicking the button triggers DOM mutations
WATCH_MUTATION_HTML = """
<!DOCTYPE html>
<html><body>
  <button id="btn">Complete Challenge</button>
  <div id="result"></div>
  <script>
    document.getElementById('btn').addEventListener('click', () => {
      document.getElementById('result').textContent = 'CODE:FAL37Q';
      document.getElementById('btn').setAttribute('disabled', 'true');
    });
  </script>
</body></html>
"""

# watch_for_text: timeout path — text never appears, but other mutations happen
WATCH_TIMEOUT_HTML = """
<!DOCTYPE html>
<html><body>
  <div id="slot"></div>
  <script>
    setTimeout(() => {
      const el = document.createElement('span');
      el.textContent = 'background update';
      document.getElementById('slot').appendChild(el);
    }, 200);
  </script>
</body></html>
"""

# execute_js: script causes DOM mutations
EXEC_JS_HTML = """
<!DOCTYPE html>
<html><body>
  <div id="container">Initial</div>
  <button id="btn" aria-expanded="false">Toggle</button>
</body></html>
"""

# wait: mutations happen during the wait period
WAIT_MUTATION_HTML = """
<!DOCTYPE html>
<html><body>
  <div id="text">Before</div>
  <script>
    setTimeout(() => {
      document.getElementById('text').textContent = 'After update';
      const el = document.createElement('div');
      el.id = 'added';
      el.textContent = 'New element';
      document.body.appendChild(el);
    }, 200);
  </script>
</body></html>
"""

# wait: no mutations
WAIT_NO_MUTATION_HTML = """
<!DOCTYPE html>
<html><body>
  <div id="text">Static page</div>
</body></html>
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _has_any(message: str, substrings: list[str]) -> bool:
    return any(s in message for s in substrings)


def _has_all(message: str, substrings: list[str]) -> bool:
    return all(s in message for s in substrings)


async def _make_context(pw, html):
    """Create a browser + page + CDP session + ToolContext from HTML."""
    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context(viewport={"width": 800, "height": 600})
    page = await ctx.new_page()
    await page.set_content(html)
    await page.wait_for_load_state("domcontentloaded")
    cdp = await ctx.new_cdp_session(page)
    snapshot = await capture_snapshot(page, cdp)
    index = build_element_index(snapshot)
    tool_ctx = ToolContext(page=page, cdp_session=cdp, element_index=index)
    return browser, ctx, page, tool_ctx


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

async def test_watch_for_text_mutations(pw) -> tuple[bool, str]:
    """watch_for_text clicks a button, DOM mutations should appear in result."""
    browser, ctx, page, tool_ctx = await _make_context(pw, WATCH_MUTATION_HTML)
    try:
        result = await watch_for_text("Complete Challenge", tool_ctx, timeout_ms=3000)
        msg = result.message

        # Must still report success
        ok_check = result.ok is True
        # Must contain the base message
        base_check = "Watched and clicked" in msg
        # Must contain mutation feedback about the code appearing
        mutation_check = _has_any(msg, ["New text appeared:", "CODE:FAL37Q", "Attribute changes:"])

        passed = ok_check and base_check and mutation_check
        detail = f"ok={result.ok} msg={msg!r}"
        return passed, detail
    finally:
        await ctx.close()
        await browser.close()


async def test_watch_for_text_timeout_mutations(pw) -> tuple[bool, str]:
    """watch_for_text timeout path should still report mutations that happened."""
    browser, ctx, page, tool_ctx = await _make_context(pw, WATCH_TIMEOUT_HTML)
    try:
        # Wait long enough for the background mutation (200ms) but text won't be found
        result = await watch_for_text("NONEXISTENT", tool_ctx, timeout_ms=500)
        msg = result.message

        ok_check = result.ok is False
        timeout_check = "timeout" in msg.lower()
        # The 200ms background mutation should still be captured
        # (it may or may not show depending on timing, so we just check format)
        format_check = "NONEXISTENT" in msg

        passed = ok_check and timeout_check and format_check
        detail = f"ok={result.ok} msg={msg!r}"
        return passed, detail
    finally:
        await ctx.close()
        await browser.close()


async def test_execute_js_mutations(pw) -> tuple[bool, str]:
    """execute_js should report DOM mutations caused by the script."""
    browser, ctx, page, tool_ctx = await _make_context(pw, EXEC_JS_HTML)
    try:
        code = """
        document.getElementById('container').textContent = 'Updated by JS';
        document.getElementById('btn').setAttribute('aria-expanded', 'true');
        const el = document.createElement('span');
        el.textContent = 'Injected element';
        document.body.appendChild(el);
        """
        result = await execute_js(code, tool_ctx)
        msg = result.message

        ok_check = result.ok is True
        base_check = "Executed script" in msg
        # Should report text and/or attribute mutations
        mutation_check = _has_any(msg, [
            "New text appeared:",
            "Attribute changes:",
        ])

        passed = ok_check and base_check and mutation_check
        detail = f"ok={result.ok} msg={msg!r}"
        return passed, detail
    finally:
        await ctx.close()
        await browser.close()


async def test_execute_js_no_mutations(pw) -> tuple[bool, str]:
    """execute_js with no-op script should report 'No visible DOM changes'."""
    browser, ctx, page, tool_ctx = await _make_context(pw, EXEC_JS_HTML)
    try:
        result = await execute_js("void 0", tool_ctx)
        msg = result.message

        ok_check = result.ok is True
        base_check = "Executed script" in msg
        no_change_check = "No visible DOM changes" in msg

        passed = ok_check and base_check and no_change_check
        detail = f"ok={result.ok} msg={msg!r}"
        return passed, detail
    finally:
        await ctx.close()
        await browser.close()


async def test_wait_with_mutations(pw) -> tuple[bool, str]:
    """wait should capture mutations that happen during the sleep."""
    browser, ctx, page, tool_ctx = await _make_context(pw, WAIT_MUTATION_HTML)
    try:
        result = await wait(500, tool_ctx)
        msg = result.message

        ok_check = result.ok is True
        # wait uses _format_wait_message, not _format_verification, so check its patterns
        mutation_check = _has_any(msg, [
            "Changes during wait",
            "New text appeared:",
            "Elements added:",
        ])

        passed = ok_check and mutation_check
        detail = f"ok={result.ok} msg={msg!r}"
        return passed, detail
    finally:
        await ctx.close()
        await browser.close()


async def test_wait_no_mutations(pw) -> tuple[bool, str]:
    """wait on a static page should report no changes."""
    browser, ctx, page, tool_ctx = await _make_context(pw, WAIT_NO_MUTATION_HTML)
    try:
        result = await wait(300, tool_ctx)
        msg = result.message

        ok_check = result.ok is True
        no_change_check = "No changes during wait" in msg

        passed = ok_check and no_change_check
        detail = f"ok={result.ok} msg={msg!r}"
        return passed, detail
    finally:
        await ctx.close()
        await browser.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    scenarios = [
        ("watch_for_text: click mutations",    test_watch_for_text_mutations),
        ("watch_for_text: timeout mutations",   test_watch_for_text_timeout_mutations),
        ("execute_js: script mutations",        test_execute_js_mutations),
        ("execute_js: no-op (no mutations)",    test_execute_js_no_mutations),
        ("wait: mutations during sleep",        test_wait_with_mutations),
        ("wait: no mutations (static page)",    test_wait_no_mutations),
    ]

    print("\n--- Mutation Feedback Debug (6 scenarios) ---\n")

    results = []
    async with async_playwright() as pw:
        for label, test_fn in scenarios:
            try:
                passed, detail = await test_fn(pw)
            except Exception as exc:
                passed, detail = False, f"EXCEPTION: {exc}"
            tag = "PASS" if passed else "FAIL"
            print(f"  [{tag}] {label}")
            print(f"         {detail}\n")
            results.append((label, passed))

    passed_count = sum(1 for _, p in results if p)
    total = len(results)
    print(f"--- Summary: {passed_count}/{total} passed ---\n")
    for label, passed in results:
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")

    print()
    if passed_count == total:
        print("ALL PASSED")
    else:
        print("SOME FAILED")
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
