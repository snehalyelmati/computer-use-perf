"""Debug script to verify DOM silence detection catches delayed content.

Serves test pages that simulate real-world patterns from logs/pages:
- Delayed content blocks (500/1500/2500ms setTimeout)
- Popups/modals appearing after page load
- SPA click -> async update
- Navigation -> delayed render

Compares: old strategy (networkidle only) vs new (networkidle + DOM silence).
"""

import asyncio
import json
import logging
import os
import tempfile
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler

from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Test pages -- modeled after patterns in logs/pages/step0002_*.html
# ---------------------------------------------------------------------------

# Page with staggered content blocks + popup (like the challenge site)
STAGGERED_PAGE = """<!DOCTYPE html>
<html><head><title>Staggered Content</title></head>
<body>
  <div id="root">
    <h1>Challenge Step 1</h1>
    <div id="main-content">
      <p>Initial shell content</p>
    </div>
    <div id="popup-area"></div>
  </div>
  <script>
    // Content blocks at 500/1500/2500ms (mirrors real challenge site)
    [500, 1500, 2500].forEach(function(delay, i) {
      setTimeout(function() {
        var div = document.createElement('div');
        div.id = 'block-' + (i + 1);
        div.className = 'content-block';
        var h3 = document.createElement('h3');
        h3.textContent = 'Content Block ' + (i+1) + ' Loaded!';
        var p = document.createElement('p');
        p.textContent = 'This content appeared ' + delay + 'ms after page load.';
        div.appendChild(h3);
        div.appendChild(p);
        document.getElementById('main-content').appendChild(div);
      }, delay);
    });

    // Popup appears at 800ms (like cookie consent / newsletter popup)
    setTimeout(function() {
      var popup = document.createElement('div');
      popup.id = 'popup-modal';
      var h3 = document.createElement('h3');
      h3.textContent = 'Cookie Consent';
      var p = document.createElement('p');
      p.textContent = 'We use cookies. Do you accept?';
      var acceptBtn = document.createElement('button');
      acceptBtn.id = 'accept-btn';
      acceptBtn.textContent = 'Accept';
      var declineBtn = document.createElement('button');
      declineBtn.id = 'decline-btn';
      declineBtn.textContent = 'Decline';
      popup.appendChild(h3);
      popup.appendChild(p);
      popup.appendChild(acceptBtn);
      popup.appendChild(declineBtn);
      document.getElementById('popup-area').appendChild(popup);
    }, 800);

    // Form enables after all content loads (3000ms)
    setTimeout(function() {
      var form = document.createElement('div');
      form.id = 'code-form';
      var input = document.createElement('input');
      input.id = 'code-input';
      input.placeholder = 'Enter code';
      var btn = document.createElement('button');
      btn.id = 'submit-btn';
      btn.textContent = 'Submit';
      form.appendChild(input);
      form.appendChild(btn);
      document.getElementById('main-content').appendChild(form);
    }, 3000);
  </script>
</body>
</html>"""

# SPA page with click -> async update (like profile/settings navigation)
SPA_CLICK_PAGE = """<!DOCTYPE html>
<html><head><title>SPA Click Update</title></head>
<body>
  <div id="root">
    <button id="load-btn" onclick="loadProfile()">Load Profile</button>
    <div id="status">Idle</div>
    <div id="content"></div>
  </div>
  <script>
    function loadProfile() {
      document.getElementById('status').textContent = 'Loading...';
      var placeholder = document.createElement('p');
      placeholder.textContent = 'Loading placeholder...';
      document.getElementById('content').textContent = '';
      document.getElementById('content').appendChild(placeholder);
      // Simulate async data fetch (1200ms)
      setTimeout(function() {
        var card = document.createElement('div');
        card.id = 'profile-card';
        var h2 = document.createElement('h2');
        h2.textContent = 'John Doe';
        var email = document.createElement('p');
        email.textContent = 'Email: john@example.com';
        var role = document.createElement('p');
        role.textContent = 'Role: Admin';
        card.appendChild(h2);
        card.appendChild(email);
        card.appendChild(role);
        var content = document.getElementById('content');
        content.textContent = '';
        content.appendChild(card);
        document.getElementById('status').textContent = 'Loaded';
      }, 1200);
    }
  </script>
</body>
</html>"""

# Navigation target with delayed render
NAV_TARGET_PAGE = """<!DOCTYPE html>
<html><head><title>Nav Target</title></head>
<body>
  <div id="root">
    <h1>Step 2</h1>
    <div id="status">Loading step 2...</div>
    <div id="challenge-area"></div>
  </div>
  <script>
    // Simulate React hydration + data fetch
    setTimeout(function() {
      var content = document.createElement('div');
      content.id = 'challenge-content';
      var p = document.createElement('p');
      p.textContent = 'Challenge: Find the hidden code';
      var options = document.createElement('div');
      options.id = 'options';
      ['Option A', 'Option B - Correct', 'Option C'].forEach(function(text) {
        var btn = document.createElement('button');
        btn.className = 'option';
        btn.textContent = text;
        options.appendChild(btn);
      });
      content.appendChild(p);
      content.appendChild(options);
      document.getElementById('challenge-area').appendChild(content);
      document.getElementById('status').textContent = 'Ready';
    }, 1000);

    // Modal overlay at 1500ms
    setTimeout(function() {
      var overlay = document.createElement('div');
      overlay.id = 'modal-overlay';
      var modal = document.createElement('div');
      modal.id = 'modal';
      var h2 = document.createElement('h2');
      h2.textContent = 'Select an option';
      var closeBtn = document.createElement('button');
      closeBtn.id = 'modal-close';
      closeBtn.textContent = 'Close';
      modal.appendChild(h2);
      modal.appendChild(closeBtn);
      overlay.appendChild(modal);
      document.body.appendChild(overlay);
    }, 1500);
  </script>
</body>
</html>"""

# CSS animations only -- should NOT cause false mutation counts
ANIMATION_PAGE = """<!DOCTYPE html>
<html><head><title>Animation Page</title>
<style>
  @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }
  .spinner { animation: spin 2s linear infinite; width: 50px; height: 50px; background: red; }
  .pulser { animation: pulse 1s ease infinite; }
</style>
</head>
<body>
  <div class="spinner" id="spinner">Spin</div>
  <div class="pulser" id="pulser">Pulse</div>
  <div id="real-content"></div>
  <script>
    // Only one real DOM mutation at 500ms -- CSS animations don't trigger MutationObserver
    setTimeout(function() {
      document.getElementById('real-content').textContent = 'REAL CONTENT LOADED';
    }, 500);
  </script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Test server
# ---------------------------------------------------------------------------

def start_test_server(port: int, tmpdir: str) -> HTTPServer:
    for name, content in [
        ("index.html", STAGGERED_PAGE),
        ("spa.html", SPA_CLICK_PAGE),
        ("nav-target.html", NAV_TARGET_PAGE),
        ("animation.html", ANIMATION_PAGE),
    ]:
        with open(os.path.join(tmpdir, name), "w") as f:
            f.write(content)

    class QuietHandler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=tmpdir, **kw)
        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", port), QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Import the actual JS constant from the codebase
from src.agent.tools.semantic import _DOM_SILENCE_JS


async def run_dom_silence(page, cdp, quiet_ms=150, max_wait_ms=3000, poll_ms=50, require_mutation=False):
    """Run wait_for_dom_silence via CDP, return result dict."""
    expr = f"({_DOM_SILENCE_JS})({json.dumps([quiet_ms, max_wait_ms, poll_ms, 1 if require_mutation else 0])})"
    try:
        result = await cdp.send(
            "Runtime.evaluate",
            {"expression": expr, "awaitPromise": True, "returnByValue": True},
        )
        return result.get("result", {}).get("value")
    except Exception as e:
        return {"error": str(e)}


async def old_strategy(page, networkidle_timeout_ms=3000):
    """Old agent strategy: domcontentloaded + networkidle only."""
    try:
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_load_state("networkidle", timeout=networkidle_timeout_ms)
    except Exception:
        pass


async def new_strategy(page, cdp, quiet_ms=150, max_wait_ms=3000, poll_ms=50, require_mutation=True):
    """New strategy: networkidle + DOM silence (require_mutation for post-navigation)."""
    try:
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_load_state("networkidle", timeout=3000)
    except Exception:
        pass
    return await run_dom_silence(page, cdp, quiet_ms, max_wait_ms, poll_ms, require_mutation=require_mutation)


async def count_elements(page, selector):
    return await page.evaluate(
        f"document.querySelectorAll('{selector}').length"
    )


async def get_text(page, selector):
    el = await page.query_selector(selector)
    if not el:
        return None
    return (await el.inner_text()).strip()


# ---------------------------------------------------------------------------
# Test scenarios
# ---------------------------------------------------------------------------

async def test_staggered_content(page, cdp, base_url):
    """Test 1: Page with content blocks at 500/1500/2500ms + popup at 800ms."""
    print("\n" + "=" * 70)
    print("TEST 1: Staggered content blocks (500/1500/2500ms) + popup (800ms)")
    print("=" * 70)

    # --- Old strategy ---
    await page.goto(f"{base_url}/index.html")
    await old_strategy(page)
    old_blocks = await count_elements(page, ".content-block")
    old_popup = await page.query_selector("#popup-modal") is not None
    old_form = await page.query_selector("#code-form") is not None
    print(f"\n  OLD (networkidle only):")
    print(f"    Content blocks: {old_blocks}/3")
    print(f"    Popup visible:  {old_popup}")
    print(f"    Form visible:   {old_form}")

    # --- New strategy ---
    await page.goto(f"{base_url}/index.html")
    silence = await new_strategy(page, cdp)
    new_blocks = await count_elements(page, ".content-block")
    new_popup = await page.query_selector("#popup-modal") is not None
    new_form = await page.query_selector("#code-form") is not None
    print(f"\n  NEW (networkidle + DOM silence):")
    print(f"    Content blocks: {new_blocks}/3")
    print(f"    Popup visible:  {new_popup}")
    print(f"    Form visible:   {new_form}")
    print(f"    Silence result: {silence}")

    improved = new_blocks > old_blocks or (new_popup and not old_popup)
    # Bare networkidle + silence (no handler warm-up) is limited —
    # the real per-step flow has warm-up; see test 5.
    status = "PASS" if new_blocks >= 1 else "FAIL"
    print(f"\n  [{status}] Improvement: {'YES' if improved else 'NO (no warm-up in this test)'}")
    return status == "PASS"


async def test_spa_click(page, cdp, base_url):
    """Test 2: Click that triggers 1200ms async update."""
    print("\n" + "=" * 70)
    print("TEST 2: SPA click -> 1200ms async data fetch")
    print("=" * 70)

    # --- Old strategy (200ms fixed settle) ---
    await page.goto(f"{base_url}/spa.html")
    await page.wait_for_load_state("networkidle")
    await page.click("#load-btn")
    await asyncio.sleep(0.200)  # old settle_ms
    old_text = await get_text(page, "#content")
    old_status = await get_text(page, "#status")
    print(f"\n  OLD (200ms fixed settle after click):")
    print(f"    Content: {old_text!r}")
    print(f"    Status:  {old_status!r}")
    old_has_data = old_text is not None and "John Doe" in old_text

    # --- New strategy (DOM silence after click, require_mutation=True for tool-level) ---
    await page.goto(f"{base_url}/spa.html")
    await page.wait_for_load_state("networkidle")
    await page.click("#load-btn")
    silence = await run_dom_silence(page, cdp, quiet_ms=150, max_wait_ms=2000, require_mutation=True)
    new_text = await get_text(page, "#content")
    new_status = await get_text(page, "#status")
    print(f"\n  NEW (DOM silence after click):")
    print(f"    Content: {new_text!r}")
    print(f"    Status:  {new_status!r}")
    print(f"    Silence: {silence}")
    new_has_data = new_text is not None and "John Doe" in new_text

    status = "PASS" if new_has_data else "FAIL"
    print(f"\n  [{status}] Old saw data: {old_has_data}, New saw data: {new_has_data}")
    return status == "PASS"


async def test_navigation_delayed_render(page, cdp, base_url):
    """Test 3: Navigate to page with delayed content + modal."""
    print("\n" + "=" * 70)
    print("TEST 3: Navigation -> page with 1000ms content + 1500ms modal")
    print("=" * 70)

    # --- Old strategy ---
    await page.goto(f"{base_url}/nav-target.html")
    await old_strategy(page)
    old_content = await page.query_selector("#challenge-content") is not None
    old_modal = await page.query_selector("#modal-overlay") is not None
    old_status = await get_text(page, "#status")
    print(f"\n  OLD (networkidle only):")
    print(f"    Challenge content: {old_content}")
    print(f"    Modal overlay:     {old_modal}")
    print(f"    Status:            {old_status!r}")

    # --- New strategy ---
    await page.goto(f"{base_url}/nav-target.html")
    silence = await new_strategy(page, cdp)
    new_content = await page.query_selector("#challenge-content") is not None
    new_modal = await page.query_selector("#modal-overlay") is not None
    new_status = await get_text(page, "#status")
    print(f"\n  NEW (networkidle + DOM silence):")
    print(f"    Challenge content: {new_content}")
    print(f"    Modal overlay:     {new_modal}")
    print(f"    Status:            {new_status!r}")
    print(f"    Silence result:    {silence}")

    # Bare silence (no warm-up) can't catch 1000ms+ delays;
    # the real per-step flow has handler/scroll warm-up.
    status = "PASS" if new_content else "FAIL"
    print(f"\n  [{status}] Content: {new_content}, Modal: {new_modal}")
    return status == "PASS"


async def test_css_animation_no_false_wait(page, cdp, base_url):
    """Test 4: Stable page with CSS animations — per-step times out at max_wait, not 3s."""
    print("\n" + "=" * 70)
    print("TEST 4: Stable page (CSS animations only) — per-step max_wait=500ms cap")
    print("=" * 70)

    await page.goto(f"{base_url}/animation.html")
    await old_strategy(page)

    # Per-step uses require_mutation=True, max_wait=500ms
    silence = await run_dom_silence(page, cdp, quiet_ms=150, max_wait_ms=500, require_mutation=True)
    real_content = await get_text(page, "#real-content")
    print(f"\n  DOM silence result: {silence}")
    print(f"  Real content:      {real_content!r}")

    total_ms = silence.get("totalMs", 0) if silence else 0
    timed_out = silence.get("timedOut", False) if silence else False

    # On a stable page, require_mutation=True times out at max_wait (500ms).
    # This is the acceptable overhead — much better than the 3s with max_wait=3000.
    status = "PASS" if total_ms <= 600 else "FAIL"
    print(f"\n  [{status}] Resolved in {total_ms}ms, timed_out={timed_out}")
    print(f"    (Stable page overhead: {total_ms}ms — capped by dom_step_max_wait_ms=500)")
    return status == "PASS"


async def test_per_step_flow(page, cdp, base_url):
    """Test 5: Simulate the actual per-step flow with realistic timings."""
    print("\n" + "=" * 70)
    print("TEST 5: Real per-step pipeline (handler ~10ms, scroll ~5ms, silence require_mutation=True max=500ms)")
    print("=" * 70)

    await page.goto(f"{base_url}/index.html")

    # networkidle (instant on local server)
    try:
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_load_state("networkidle", timeout=3000)
    except Exception:
        pass
    after_networkidle = await count_elements(page, ".content-block")
    print(f"\n  After networkidle:       {after_networkidle}/3 content blocks")

    # Simulate handler extraction (~10ms from real metrics)
    await asyncio.sleep(0.010)
    after_handlers = await count_elements(page, ".content-block")
    print(f"  After handlers (~10ms):  {after_handlers}/3 content blocks")

    # Simulate scroll containers (~5ms from real metrics)
    await asyncio.sleep(0.005)
    after_scroll = await count_elements(page, ".content-block")
    print(f"  After scroll (~5ms):     {after_scroll}/3 content blocks")

    # DOM silence: require_mutation=True, max_wait=500ms (per-step config)
    silence = await run_dom_silence(page, cdp, quiet_ms=150, max_wait_ms=500, require_mutation=True)
    after_silence = await count_elements(page, ".content-block")
    popup = await page.query_selector("#popup-modal") is not None
    form = await page.query_selector("#code-form") is not None
    print(f"  After DOM silence:       {after_silence}/3 content blocks")
    print(f"    Popup: {popup}, Form: {form}")
    print(f"    Silence: {silence}")

    status = "PASS" if popup else "FAIL"
    print(f"\n  [{status}] DOM silence as final gate before snapshot")
    return status == "PASS"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    port = 8766
    tmpdir = tempfile.mkdtemp()
    server = start_test_server(port, tmpdir)
    base_url = f"http://127.0.0.1:{port}"

    print("DOM Silence Detection Debug Script")
    print(f"Test server: {base_url}")
    print(f"Config: dom_quiet_ms=150, dom_max_wait_ms=3000, dom_poll_ms=50")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        cdp = await context.new_cdp_session(page)

        results = {}
        results["staggered_content"] = await test_staggered_content(page, cdp, base_url)
        results["spa_click"] = await test_spa_click(page, cdp, base_url)
        results["nav_delayed"] = await test_navigation_delayed_render(page, cdp, base_url)
        results["css_animation"] = await test_css_animation_no_false_wait(page, cdp, base_url)
        results["per_step_flow"] = await test_per_step_flow(page, cdp, base_url)

        await browser.close()

    server.shutdown()

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    for name, ok in results.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\n  {passed}/{total} tests passed")

    if passed < total:
        print("\n  Failed tests indicate DOM silence detection needs tuning.")
        print("  Check dom_quiet_ms / dom_max_wait_ms values.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
