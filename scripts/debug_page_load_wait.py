"""Debug script to verify whether the agent properly waits for page content to load.

Tests three scenarios:
1. Initial page load with delayed content (simulated API fetch)
2. Click that triggers async content update (SPA-style)
3. Click that triggers full navigation to a page with delayed content

Runs the agent's actual waiting logic against a local test server.
"""

import asyncio
import logging
from http.server import HTTPServer, SimpleHTTPRequestHandler
import threading
import tempfile
import os

from playwright.async_api import async_playwright

# ── Inline test pages ────────────────────────────────────────────────

DELAYED_PAGE = """<!DOCTYPE html>
<html>
<head><title>Delayed Content Test</title></head>
<body>
  <h1>Page Shell</h1>
  <div id="status">Loading...</div>
  <div id="content"></div>
  <button id="async-btn" onclick="loadAsync()">Load More</button>
  <a id="nav-link" href="/page2.html">Go to Page 2</a>
  <script>
    // Simulate async API fetch that takes 1.5s
    setTimeout(function() {
      document.getElementById('content').textContent = 'DATA LOADED AFTER 1500ms';
      document.getElementById('status').textContent = 'Ready';
    }, 1500);

    // Simulate click that triggers 2s async update
    function loadAsync() {
      document.getElementById('status').textContent = 'Fetching...';
      document.getElementById('async-btn').disabled = true;
      setTimeout(function() {
        var p = document.createElement('p');
        p.id = 'async-text';
        p.textContent = 'ASYNC CONTENT LOADED AFTER 2000ms';
        document.getElementById('content').appendChild(p);
        document.getElementById('status').textContent = 'Complete';
        document.getElementById('async-btn').disabled = false;
      }, 2000);
    }
  </script>
</body>
</html>"""

PAGE2 = """<!DOCTYPE html>
<html>
<head><title>Page 2 - Also Delayed</title></head>
<body>
  <h1>Page 2 Shell</h1>
  <div id="status">Loading page 2...</div>
  <div id="content"></div>
  <script>
    setTimeout(function() {
      var p = document.createElement('p');
      p.id = 'page2-text';
      p.textContent = 'PAGE 2 DATA LOADED AFTER 800ms';
      document.getElementById('content').appendChild(p);
      document.getElementById('status').textContent = 'Page 2 Ready';
    }, 800);
  </script>
</body>
</html>"""

# Heavier page: streaming content that arrives in chunks
STREAMING_PAGE = """<!DOCTYPE html>
<html>
<head><title>Streaming Content Test</title></head>
<body>
  <h1>Streaming Page</h1>
  <div id="status">Streaming...</div>
  <ul id="items"></ul>
  <script>
    var items = ['Item A (300ms)', 'Item B (800ms)', 'Item C (1500ms)',
                   'Item D (2200ms)', 'Item E (3000ms)'];
    var delays = [300, 800, 1500, 2200, 3000];
    var loaded = 0;

    items.forEach(function(text, i) {
      setTimeout(function() {
        var li = document.createElement('li');
        li.textContent = text;
        li.id = 'item-' + i;
        document.getElementById('items').appendChild(li);
        loaded++;
        if (loaded === items.length) {
          document.getElementById('status').textContent = 'All items loaded';
        }
      }, delays[i]);
    });
  </script>
</body>
</html>"""

# SPA page: client-side routing simulation
SPA_PAGE = """<!DOCTYPE html>
<html>
<head><title>SPA Navigation Test</title></head>
<body>
  <h1>SPA App</h1>
  <nav>
    <button id="route-home" onclick="navigate('home')">Home</button>
    <button id="route-profile" onclick="navigate('profile')">Profile</button>
    <button id="route-settings" onclick="navigate('settings')">Settings</button>
  </nav>
  <div id="status">Idle</div>
  <div id="app-content">
    <p>Welcome to the home page</p>
  </div>
  <script>
    var asyncContent = {
      profile: { delay: 1200, text: 'PROFILE: John Doe, joined 2024' },
      settings: { delay: 1800, text: 'SETTINGS: Dark mode ON, Language EN' }
    };

    function navigate(route) {
      document.getElementById('status').textContent = 'Navigating...';
      // Immediate DOM update (like React setState)
      var el = document.getElementById('app-content');
      el.textContent = '';
      var p = document.createElement('p');
      p.id = route + '-data';
      p.textContent = route.toUpperCase() + ': Loading...';
      el.appendChild(p);
      history.pushState({route: route}, '', '/' + route);

      // Deferred data fetch
      if (asyncContent[route]) {
        setTimeout(function() {
          document.getElementById(route + '-data').textContent =
            asyncContent[route].text;
          document.getElementById('status').textContent = 'Loaded: ' + route;
        }, asyncContent[route].delay);
      } else {
        document.getElementById('status').textContent = 'Loaded: ' + route;
      }
    }
  </script>
</body>
</html>"""


# ── Test server ──────────────────────────────────────────────────────

def start_test_server(port: int, tmpdir: str) -> HTTPServer:
    """Start a simple HTTP server serving test pages from tmpdir."""
    for name, content in [
        ("index.html", DELAYED_PAGE),
        ("page2.html", PAGE2),
        ("streaming.html", STREAMING_PAGE),
        ("spa.html", SPA_PAGE),
    ]:
        with open(os.path.join(tmpdir, name), "w") as f:
            f.write(content)

    class QuietHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=tmpdir, **kwargs)
        def log_message(self, format, *args):
            pass

    server = HTTPServer(("127.0.0.1", port), QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ── Agent wait logic (extracted) ─────────────────────────────────────

async def agent_wait_for_page(page, networkidle_timeout_ms: int = 3000):
    """Replicate the agent's per-step waiting logic from agent.py:1978-1984."""
    try:
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_load_state("networkidle", timeout=networkidle_timeout_ms)
    except Exception:
        pass


async def check_content(page, selector: str, description: str) -> bool:
    """Check if an element with the given selector exists and has content."""
    try:
        el = await page.query_selector(selector)
        if el:
            text = await el.inner_text()
            print(f"  [FOUND] {description}: '{text}'")
            return True
        else:
            print(f"  [MISSING] {description}: selector '{selector}' not found")
            return False
    except Exception as e:
        print(f"  [ERROR] {description}: {e}")
        return False


# ── Test scenarios ───────────────────────────────────────────────────

async def test_initial_load(page, base_url: str):
    """Test 1: Does the agent see delayed content on initial page load?"""
    print("\n" + "=" * 70)
    print("TEST 1: Initial page load with 1500ms delayed content")
    print("=" * 70)

    await page.goto(f"{base_url}/index.html")
    await agent_wait_for_page(page)

    print("\nAfter agent's standard wait sequence:")
    found_delayed = await check_content(page, "#content", "Delayed content (1500ms)")
    status = await check_content(page, "#status", "Status indicator")

    # Check if content actually has the loaded text
    el = await page.query_selector("#content")
    text = (await el.inner_text()).strip() if el else ""
    has_data = "DATA LOADED" in text

    if not has_data:
        print("\n  >> PROBLEM: Agent would capture snapshot BEFORE delayed content loaded!")
        print("     networkidle doesn't help — no network activity, just setTimeout.")
    else:
        print("\n  >> OK: Delayed content was visible after agent wait.")
    return has_data


async def test_click_async_update(page, base_url: str):
    """Test 2: After clicking a button that triggers 2s async update."""
    print("\n" + "=" * 70)
    print("TEST 2: Click triggers 2000ms async content update")
    print("=" * 70)

    await page.goto(f"{base_url}/index.html")
    await page.wait_for_timeout(2000)  # let initial load finish

    print("\nClicking 'Load More' button...")
    await page.click("#async-btn")
    await page.wait_for_timeout(200)  # agent's settle_ms

    print("\nAfter agent's 200ms settle wait:")
    found_async = await check_content(page, "#async-text", "Async content (2000ms)")
    await check_content(page, "#status", "Status indicator")

    if not found_async:
        print("\n  >> PROBLEM: 200ms settle is too short for 2000ms async content.")

    # Simulate next step's wait
    print("\nAfter next step's wait_for_load_state:")
    await agent_wait_for_page(page)
    found_after_step = await check_content(page, "#async-text", "Async content (after step wait)")
    await check_content(page, "#status", "Status indicator")

    if not found_after_step:
        print("\n  >> PROBLEM: Even next step's wait doesn't catch this!")
        print("     networkidle returns immediately (no pending network).")
    return found_after_step


async def test_navigation_click(page, base_url: str):
    """Test 3: Click that navigates to a new page with delayed content."""
    print("\n" + "=" * 70)
    print("TEST 3: Navigation click -> new page with 800ms delayed content")
    print("=" * 70)

    await page.goto(f"{base_url}/index.html")
    await page.wait_for_timeout(2000)

    print("\nClicking navigation link to page 2...")
    await page.click("#nav-link")
    await page.wait_for_timeout(200)  # settle_ms

    print("\nAfter agent's 200ms settle (same step as click):")
    found = await check_content(page, "#page2-text", "Page 2 delayed content (800ms)")
    await check_content(page, "#status", "Status indicator")

    # Simulate next step wait
    print("\nAfter next step's wait_for_load_state:")
    await agent_wait_for_page(page)
    found_after = await check_content(page, "#page2-text", "Page 2 content (after step wait)")
    await check_content(page, "#status", "Status indicator")

    if not found_after:
        print("\n  >> PROBLEM: Page 2's delayed content not visible even after step wait.")
    return found_after


async def test_streaming_content(page, base_url: str):
    """Test 4: Page with content arriving in chunks over 3 seconds."""
    print("\n" + "=" * 70)
    print("TEST 4: Streaming content (items at 300/800/1500/2200/3000ms)")
    print("=" * 70)

    await page.goto(f"{base_url}/streaming.html")
    await agent_wait_for_page(page)

    print("\nAfter agent's standard wait sequence:")
    items_found = 0
    for i in range(5):
        if await check_content(page, f"#item-{i}", f"Item {i}"):
            items_found += 1

    await check_content(page, "#status", "Status indicator")
    print(f"\n  Found {items_found}/5 items after agent wait.")
    if items_found < 5:
        print(f"  >> PROBLEM: Only {items_found}/5 items visible. Content arriving")
        print("     via setTimeout is invisible to networkidle.")
    return items_found == 5


async def test_spa_navigation(page, base_url: str):
    """Test 5: SPA client-side navigation with async data loading."""
    print("\n" + "=" * 70)
    print("TEST 5: SPA navigation with async data fetch (1200ms)")
    print("=" * 70)

    await page.goto(f"{base_url}/spa.html")
    await page.wait_for_timeout(500)

    print("\nClicking 'Profile' route button...")
    await page.click("#route-profile")
    await page.wait_for_timeout(200)  # settle_ms

    print("\nAfter 200ms settle:")
    profile_el = await page.query_selector("#profile-data")
    has_real_data = False
    if profile_el:
        text = await profile_el.inner_text()
        has_real_data = "John Doe" in text
        print(f"  Profile text: '{text}'")
        print(f"  Has real data: {has_real_data}")
        if not has_real_data:
            print("  >> PROBLEM: Agent sees 'Loading...' placeholder, not real data.")

    # Next step wait
    print("\nAfter next step's wait_for_load_state:")
    await agent_wait_for_page(page)
    profile_el = await page.query_selector("#profile-data")
    if profile_el:
        text = await profile_el.inner_text()
        has_real_data = "John Doe" in text
        print(f"  Profile text: '{text}'")
        print(f"  Has real data: {has_real_data}")
        if not has_real_data:
            print("  >> PROBLEM: Still stale! networkidle doesn't wait for setTimeout.")

    return has_real_data


async def test_timing_sweep(page, base_url: str):
    """Test 6: Measure exactly how much wait time is needed."""
    print("\n" + "=" * 70)
    print("TEST 6: Timing sweep — how much total wait is needed?")
    print("=" * 70)

    waits = [0, 100, 200, 500, 1000, 1500, 2000, 3000]
    print(f"\n{'Extra wait':<12} {'Initial (1500ms)':<20} {'Streaming (5 items)':<20}")
    print("-" * 52)

    for wait_ms in waits:
        # Initial delayed content
        await page.goto(f"{base_url}/index.html")
        await agent_wait_for_page(page)
        if wait_ms > 0:
            await page.wait_for_timeout(wait_ms)
        el = await page.query_selector("#content")
        text = (await el.inner_text()).strip() if el else ""
        initial_ok = "YES" if "DATA LOADED" in text else "no"

        # Streaming content
        await page.goto(f"{base_url}/streaming.html")
        await agent_wait_for_page(page)
        if wait_ms > 0:
            await page.wait_for_timeout(wait_ms)
        items = 0
        for i in range(5):
            if await page.query_selector(f"#item-{i}"):
                items += 1
        streaming_ok = f"{items}/5"

        print(f"{wait_ms:>6}ms     {initial_ok:<20} {streaming_ok:<20}")

    print("\n  This shows the gap between what the agent waits for and when")
    print("  content actually appears. networkidle only helps for actual")
    print("  network requests, NOT setTimeout/requestAnimationFrame.")


# ── Main ─────────────────────────────────────────────────────────────

async def main():
    port = 8765
    tmpdir = tempfile.mkdtemp()
    server = start_test_server(port, tmpdir)
    base_url = f"http://127.0.0.1:{port}"

    print("Page Load Wait Debug Script")
    print(f"Test server running at {base_url}")
    print(f"Agent config: settle_ms=200, networkidle_timeout_ms=3000")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        results = {}
        results["initial_load"] = await test_initial_load(page, base_url)
        results["click_async"] = await test_click_async_update(page, base_url)
        results["nav_click"] = await test_navigation_click(page, base_url)
        results["streaming"] = await test_streaming_content(page, base_url)
        results["spa_nav"] = await test_spa_navigation(page, base_url)
        await test_timing_sweep(page, base_url)

        await browser.close()

    server.shutdown()

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    for name, ok in results.items():
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}")
    print(f"\n  {passed}/{total} tests passed")

    if passed < total:
        print("\n  ROOT CAUSE ANALYSIS:")
        print("  1. networkidle only detects fetch/XHR, not setTimeout/RAF content")
        print("  2. 200ms settle after actions is too short for most async updates")
        print("  3. No mechanism to detect 'content is still loading' indicators")
        print("  4. wait_for_load_state('domcontentloaded') is a no-op on already-")
        print("     loaded pages — it only helps mid-navigation")
        print()
        print("  POTENTIAL FIXES:")
        print("  A. Add a requestAnimationFrame-based idle detection after actions")
        print("  B. Wait for MutationObserver silence (no mutations for N ms)")
        print("  C. Increase settle_ms or make it adaptive based on mutation rate")
        print("  D. Add wait_for_load_state('networkidle') inside navigate_to tool")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
