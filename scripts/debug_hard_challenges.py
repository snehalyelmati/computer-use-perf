"""Debug script for 5 hard challenge types the agent has never reached.

Tests: service_worker, dom_mutation, recursive_iframe, shadow_dom, websocket.

Each challenge is replicated as self-contained HTML and exercised through the
agent's real tool pipeline (snapshot → tool calls → verify).  Failures indicate
capability gaps that need fixes before the agent can complete steps 21-30.

Usage:
    uv run python scripts/debug_hard_challenges.py
"""

import asyncio

from playwright.async_api import async_playwright

from src.agent.context.handlers import cleanup_handler_attributes, extract_handlers
from src.agent.context.snapshot import (
    build_element_index,
    capture_snapshot,
    format_snapshot_for_llm,
)
from src.agent.tools.semantic import (
    ToolContext,
    click_element,
    switch_to_main_frame,
    wait,
)

# ---------------------------------------------------------------------------
# HTML replicas
# ---------------------------------------------------------------------------

SERVICE_WORKER_HTML = """\
<!DOCTYPE html>
<html>
<head><title>Service Worker Challenge</title></head>
<body>
<h2>Service Worker Challenge</h2>
<p>Register a service worker, wait for the cache to be populated, then retrieve the code.</p>
<div>
  <button id="register-btn" onclick="
    this.disabled = true;
    document.getElementById('reg-status').textContent = '● Registered';
    document.getElementById('reg-status').style.color = 'green';
    setTimeout(function() {
      document.getElementById('cache-status').textContent = '● Populated';
      document.getElementById('cache-status').style.color = 'green';
      document.getElementById('retrieve-btn').disabled = false;
    }, 2000);
  ">1. Register Service Worker</button>
  <span id="reg-status" style="color:red;">○ Not registered</span>
</div>
<div style="margin-top:8px;">
  Cache status: <span id="cache-status" style="color:red;">○ Empty</span>
</div>
<div style="margin-top:8px;">
  <button id="retrieve-btn" disabled onclick="
    document.getElementById('code-display').style.display = 'block';
    document.getElementById('code-display').textContent = 'CODE:SW-DEBUG-7X9';
  ">2. Retrieve from Cache</button>
</div>
<div id="code-display" style="display:none; font-size:24px; font-weight:bold; color:green; margin-top:16px;"></div>
</body>
</html>
"""

DOM_MUTATION_HTML = """\
<!DOCTYPE html>
<html>
<head><title>Mutation Challenge</title></head>
<body>
<h2>Mutation Challenge</h2>
<p>Trigger 5 DOM mutations to reveal the code.</p>
<div>Mutations triggered: <span id="counter">0</span> / 5</div>
<button id="trigger-btn" onclick="
  var c = parseInt(document.getElementById('counter').textContent) + 1;
  document.getElementById('counter').textContent = c;
  var el = document.createElement('div');
  el.className = 'mutation-item';
  el.textContent = 'Mutation ' + c;
  document.getElementById('target').appendChild(el);
  document.getElementById('complete-btn').textContent = 'Complete (' + c + '/5)';
  if (c >= 5) {
    document.getElementById('complete-btn').disabled = false;
  }
">Trigger Mutation</button>
<div id="target" style="margin-top:8px; min-height:60px; border:1px dashed #ccc; padding:8px;">
  Mutation target area (elements appear here)
</div>
<button id="complete-btn" disabled onclick="
  document.getElementById('code-display').style.display = 'block';
  document.getElementById('code-display').textContent = 'CODE:MUT-DEBUG-3K5';
">Complete (0/5)</button>
<div id="code-display" style="display:none; font-size:24px; font-weight:bold; color:green; margin-top:16px;"></div>
</body>
</html>
"""

# Shadow DOM uses safe DOM methods (createElement/textContent) instead of innerHTML
SHADOW_DOM_HTML = """\
<!DOCTYPE html>
<html>
<head><title>Shadow DOM Challenge</title></head>
<body>
<h2>Shadow DOM Challenge</h2>
<p>Navigate through 3 nested layers to reveal the code.
Click each layer in order to access the next.</p>
<div>Shadow Level 1</div>
<div>Levels revealed: <span id="level-counter">0</span>/3</div>
<script>
function revealNextLevel() {
  var counter = document.getElementById('level-counter');
  var btn = document.getElementById('reveal-btn');
  var n = parseInt(counter.textContent) + 1;
  counter.textContent = n;
  btn.textContent = 'Reveal Code (' + n + '/3 levels)';
  if (n === 1) {
    var host1 = document.getElementById('shadow-host-1');
    var shadow1 = host1.attachShadow({mode: 'open'});
    var wrapper = document.createElement('div');
    wrapper.style.cssText = 'padding:8px; border:1px solid blue;';
    var h = document.createElement('h3');
    h.textContent = 'Shadow Level 2';
    var p = document.createElement('p');
    p.textContent = 'Layer 1 revealed';
    wrapper.appendChild(h);
    wrapper.appendChild(p);
    shadow1.appendChild(wrapper);
  } else if (n === 2) {
    var host1 = document.getElementById('shadow-host-1');
    var shadow1 = host1.shadowRoot;
    var host2 = document.createElement('div');
    host2.id = 'shadow-host-2';
    shadow1.appendChild(host2);
    var shadow2 = host2.attachShadow({mode: 'open'});
    var wrapper = document.createElement('div');
    wrapper.style.cssText = 'padding:8px; border:1px solid purple;';
    var h = document.createElement('h3');
    h.textContent = 'Shadow Level 3';
    var p = document.createElement('p');
    p.textContent = 'Layer 2 revealed';
    wrapper.appendChild(h);
    wrapper.appendChild(p);
    shadow2.appendChild(wrapper);
  } else if (n === 3) {
    var host1 = document.getElementById('shadow-host-1');
    var shadow1 = host1.shadowRoot;
    var host2 = shadow1.querySelector('#shadow-host-2');
    var shadow2 = host2.shadowRoot;
    var host3 = document.createElement('div');
    host3.id = 'shadow-host-3';
    shadow2.appendChild(host3);
    var shadow3 = host3.attachShadow({mode: 'open'});
    var codeDiv = document.createElement('div');
    codeDiv.style.cssText = 'padding:8px; border:1px solid green; font-size:24px; font-weight:bold; color:green;';
    codeDiv.textContent = 'CODE:SHADOW-DEBUG-2M8';
    shadow3.appendChild(codeDiv);
    btn.disabled = true;
  }
}
</script>
<button id="reveal-btn" onclick="revealNextLevel()">Reveal Code (0/3 levels)</button>
<div id="shadow-host-1" style="margin-top:16px;"></div>
</body>
</html>
"""

WEBSOCKET_HTML = """\
<!DOCTYPE html>
<html>
<head><title>WebSocket Challenge</title></head>
<body>
<h2>WebSocket Challenge</h2>
<p>Connect to the simulated WebSocket server and receive the code.</p>
<button id="connect-btn" onclick="
  this.disabled = true;
  document.getElementById('ws-status').textContent = '● Connecting...';
  document.getElementById('ws-status').style.color = 'orange';
  document.getElementById('terminal').textContent = '$ connecting to server...';
  setTimeout(function() {
    document.getElementById('ws-status').textContent = '● Connected';
    document.getElementById('ws-status').style.color = 'green';
    document.getElementById('terminal').textContent = '$ connected\\n> receiving data...';
    setTimeout(function() {
      document.getElementById('terminal').textContent = '$ connected\\n> receiving data...\\n> CODE:WS-DEBUG-5P1';
      document.getElementById('code-display').style.display = 'block';
      document.getElementById('code-display').textContent = 'CODE:WS-DEBUG-5P1';
    }, 1500);
  }, 1500);
">Connect</button>
<span id="ws-status" style="color:red;">○ Disconnected</span>
<pre id="terminal" style="background:#111; color:#0f0; padding:12px; margin-top:8px; min-height:60px;">$ awaiting connection...</pre>
<div id="code-display" style="display:none; font-size:24px; font-weight:bold; color:green; margin-top:16px;"></div>
</body>
</html>
"""

# Recursive iframe — reuse the pattern from debug_recursive_iframe.py

RECURSIVE_IFRAME_CODE = "CODE:IFRAME-DEBUG-4J6"


def _make_nested_html(depth: int, code: str) -> str:
    """Build HTML with `depth` levels of nested iframes (click-to-reveal)."""
    max_depth = depth

    def _level_html(current: int) -> str:
        if current == max_depth:
            return f"""\
<!DOCTYPE html>
<html>
<body>
<h2>Deepest Level {current}</h2>
<p>depth: {current}/{max_depth}</p>
<div id="code-display" style="font-size:24px;font-weight:bold;color:green;">
  Code: {code}
</div>
<button id="submit-btn" onclick="document.getElementById('result').textContent='SUBMITTED'">
  Submit
</button>
<div id="result"></div>
</body>
</html>"""

        child_html = _level_html(current + 1)
        escaped = (
            child_html.replace("&", "&amp;")
            .replace('"', "&quot;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

        return f"""\
<!DOCTYPE html>
<html>
<body>
<h2>Iframe Level {current}</h2>
<p>depth: {current}/{max_depth}</p>
<p>{max_depth - current} more levels to go</p>
<button id="enter-btn-{current}" onclick="document.getElementById('child-frame-{current}').style.display='block'">
  Enter Level {current + 1}
</button>
<iframe id="child-frame-{current}"
        srcdoc="{escaped}"
        style="display:none; width:100%; height:400px; border:1px solid #ccc;">
</iframe>
</body>
</html>"""

    return _level_html(0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _take_snapshot(page, cdp, handler_map=None):
    """Capture snapshot and build element index."""
    if handler_map is None:
        handler_map = await extract_handlers(page)
    snapshot = await capture_snapshot(page, cdp, handler_map=handler_map)
    await cleanup_handler_attributes(page)
    index = build_element_index(snapshot)
    return snapshot, index


def _find_element_by_text(index, text: str):
    """Find element whose name or text contains the given string."""
    for sid, el in index.elements.items():
        label = el.name or el.text or ""
        if text.lower() in label.lower():
            return sid, el
    return None, None


def _find_element_by_attr(index, attr_name: str, attr_value: str):
    """Find element by HTML attribute in the index."""
    for sid, el in index.elements.items():
        attrs = el.attributes or {}
        if attrs.get(attr_name) == attr_value:
            return sid, el
    return None, None


def _find_iframes_in_snapshot(index):
    """Return all iframe elements."""
    iframes = []
    for sid, el in index.elements.items():
        if (el.node_name or "").upper() == "IFRAME":
            iframes.append((sid, el))
    return iframes


def _search_snapshot_for_code(snapshot, index, prefix: str = "CODE:"):
    """Search snapshot raw_text and element text for a code string."""
    for line in snapshot.raw_text:
        if prefix in line:
            return line.strip()
    for sid, el in index.elements.items():
        txt = el.text or el.name or ""
        if prefix in txt:
            return txt.strip()
    return None


async def _search_dom_for_code(page, code: str):
    """Fall back to live DOM evaluation to find a code string."""
    for frame in page.frames:
        try:
            txt = await frame.evaluate("document.body?.innerText || ''")
            if code in txt:
                return txt
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Challenge runners
# ---------------------------------------------------------------------------


async def _run_service_worker():
    """Service Worker: click Register → wait → click Retrieve → read code."""
    log = []

    def _log(msg):
        log.append(msg)
        print(f"  [service_worker] {msg}")

    expected = "CODE:SW-DEBUG-7X9"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1024, "height": 768})
        page = await context.new_page()
        await page.set_content(SERVICE_WORKER_HTML)
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(0.3)
        cdp = await context.new_cdp_session(page)

        # Initial snapshot
        snapshot, index = await _take_snapshot(page, cdp)
        _log(f"Snapshot: {len(index.elements)} elements")
        llm_text = format_snapshot_for_llm(snapshot)
        _log(f"LLM snapshot:\n{llm_text}")

        tool_ctx = ToolContext(page=page, cdp_session=cdp, element_index=index)

        # Step 1: Click "Register Service Worker"
        btn_id, _ = _find_element_by_attr(index, "id", "register-btn")
        if not btn_id:
            btn_id, _ = _find_element_by_text(index, "Register")
        if not btn_id:
            _log("FAIL: Register button not found")
            await browser.close()
            return False

        result = await click_element(btn_id, tool_ctx)
        _log(f"Click Register: ok={result.ok} msg={result.message[:120]}")
        if not result.ok:
            _log("FAIL: Could not click Register")
            await browser.close()
            return False

        # Step 2: Wait for cache to populate (2s delay + buffer)
        result = await wait(3000, tool_ctx)
        _log(f"Wait 3s: {result.message[:120]}")

        # Step 3: Re-snapshot, find and click "Retrieve from Cache"
        snapshot, index = await _take_snapshot(page, cdp)
        tool_ctx.element_index = index

        btn_id, _ = _find_element_by_attr(index, "id", "retrieve-btn")
        if not btn_id:
            btn_id, _ = _find_element_by_text(index, "Retrieve")
        if not btn_id:
            _log("FAIL: Retrieve button not found in snapshot after wait")
            await browser.close()
            return False

        result = await click_element(btn_id, tool_ctx)
        _log(f"Click Retrieve: ok={result.ok} msg={result.message[:120]}")
        if not result.ok:
            _log("FAIL: Could not click Retrieve")
            await browser.close()
            return False

        # Step 4: Re-snapshot and verify code
        await asyncio.sleep(0.3)
        snapshot, index = await _take_snapshot(page, cdp)
        tool_ctx.element_index = index

        code = _search_snapshot_for_code(snapshot, index)
        if code and expected in code:
            _log(f"Code found in snapshot: {code}")
            await browser.close()
            return True

        # Fallback: check live DOM
        dom_text = await _search_dom_for_code(page, expected)
        if dom_text:
            _log(f"Code found via DOM (NOT in snapshot — investigate): {expected}")
            await browser.close()
            return True

        _log(f"FAIL: code {expected} not found")
        _log(f"Snapshot raw_text: {snapshot.raw_text[:5]}")
        await browser.close()
        return False


async def _run_dom_mutation():
    """DOM Mutation: click Trigger ×5 → click Complete → read code."""
    log = []

    def _log(msg):
        log.append(msg)
        print(f"  [dom_mutation] {msg}")

    expected = "CODE:MUT-DEBUG-3K5"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1024, "height": 768})
        page = await context.new_page()
        await page.set_content(DOM_MUTATION_HTML)
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(0.3)
        cdp = await context.new_cdp_session(page)

        snapshot, index = await _take_snapshot(page, cdp)
        _log(f"Snapshot: {len(index.elements)} elements")

        tool_ctx = ToolContext(page=page, cdp_session=cdp, element_index=index)

        # Click "Trigger Mutation" 5 times
        for i in range(5):
            btn_id, _ = _find_element_by_attr(index, "id", "trigger-btn")
            if not btn_id:
                btn_id, _ = _find_element_by_text(index, "Trigger Mutation")
            if not btn_id:
                _log(f"FAIL: Trigger button not found at click {i+1}")
                await browser.close()
                return False

            result = await click_element(btn_id, tool_ctx)
            _log(f"Click Trigger #{i+1}: ok={result.ok}")
            if not result.ok:
                _log(f"FAIL: click {i+1} failed: {result.message}")
                await browser.close()
                return False

            # Re-snapshot between clicks to pick up DOM changes
            await asyncio.sleep(0.2)
            snapshot, index = await _take_snapshot(page, cdp)
            tool_ctx.element_index = index

        # Verify counter
        counter_text = await page.evaluate(
            "document.getElementById('counter').textContent"
        )
        _log(f"Counter after 5 clicks: {counter_text}")

        # Click "Complete (5/5)"
        btn_id, _ = _find_element_by_attr(index, "id", "complete-btn")
        if not btn_id:
            btn_id, _ = _find_element_by_text(index, "Complete")
        if not btn_id:
            _log("FAIL: Complete button not found")
            await browser.close()
            return False

        result = await click_element(btn_id, tool_ctx)
        _log(f"Click Complete: ok={result.ok} msg={result.message[:120]}")
        if not result.ok:
            _log("FAIL: Could not click Complete")
            await browser.close()
            return False

        # Verify code
        await asyncio.sleep(0.3)
        snapshot, index = await _take_snapshot(page, cdp)
        tool_ctx.element_index = index

        code = _search_snapshot_for_code(snapshot, index)
        if code and expected in code:
            _log(f"Code found in snapshot: {code}")
            await browser.close()
            return True

        dom_text = await _search_dom_for_code(page, expected)
        if dom_text:
            _log(f"Code found via DOM (NOT in snapshot): {expected}")
            await browser.close()
            return True

        _log(f"FAIL: code {expected} not found")
        await browser.close()
        return False


async def _run_recursive_iframe():
    """Recursive iframe: workaround path — reset to main frame before each click."""
    log = []

    def _log(msg):
        log.append(msg)
        print(f"  [recursive_iframe] {msg}")

    depth = 3
    expected = RECURSIVE_IFRAME_CODE
    html = _make_nested_html(depth, expected)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1024, "height": 768})
        page = await context.new_page()
        await page.set_content(html)
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(0.5)
        cdp = await context.new_cdp_session(page)

        snapshot, index = await _take_snapshot(page, cdp)
        tool_ctx = ToolContext(page=page, cdp_session=cdp, element_index=index)
        _log(f"Snapshot: {len(index.elements)} elements")

        passed = True
        for level in range(depth):
            _log(f"--- Level {level} ---")

            # WORKAROUND: always reset to main frame before clicking
            await switch_to_main_frame(tool_ctx)
            tool_ctx.active_frame_id = None

            btn_id, btn_el = _find_element_by_attr(index, "id", f"enter-btn-{level}")
            if not btn_id:
                btn_id, btn_el = _find_element_by_text(
                    index, f"Enter Level {level + 1}"
                )

            if btn_id:
                result = await click_element(btn_id, tool_ctx)
                _log(f"Click Enter Level {level + 1}: ok={result.ok}")
                if not result.ok:
                    _log(f"FAIL: {result.message}")
                    passed = False
                    break
                await asyncio.sleep(0.3)
            else:
                _log(f"FAIL: button not found for level {level}")
                passed = False
                break

            # Re-snapshot
            snapshot, index = await _take_snapshot(page, cdp)
            tool_ctx.element_index = index

        if not passed:
            await browser.close()
            return False

        # Verify code
        code = _search_snapshot_for_code(snapshot, index)
        if code and expected in code:
            _log(f"Code found in snapshot: {code}")
            await browser.close()
            return True

        dom_text = await _search_dom_for_code(page, expected)
        if dom_text:
            _log(f"Code found via DOM (NOT in snapshot): {expected}")
            await browser.close()
            return True

        _log(f"FAIL: code {expected} not found")
        await browser.close()
        return False


async def _run_shadow_dom():
    """Shadow DOM: click Reveal ×3 → verify code visible in snapshot.

    KEY TEST: Can DOMSnapshot.captureSnapshot see elements inside open shadow roots?
    If not, the agent is blind to shadow DOM content.
    """
    log = []

    def _log(msg):
        log.append(msg)
        print(f"  [shadow_dom] {msg}")

    expected = "CODE:SHADOW-DEBUG-2M8"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1024, "height": 768})
        page = await context.new_page()
        await page.set_content(SHADOW_DOM_HTML)
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(0.3)
        cdp = await context.new_cdp_session(page)

        snapshot, index = await _take_snapshot(page, cdp)
        _log(f"Initial snapshot: {len(index.elements)} elements")
        llm_text = format_snapshot_for_llm(snapshot)
        _log(f"LLM snapshot:\n{llm_text}")

        tool_ctx = ToolContext(page=page, cdp_session=cdp, element_index=index)

        # Click "Reveal Code" 3 times
        for i in range(3):
            btn_id, _ = _find_element_by_attr(index, "id", "reveal-btn")
            if not btn_id:
                btn_id, _ = _find_element_by_text(index, "Reveal Code")
            if not btn_id:
                _log(f"FAIL: Reveal button not found at click {i+1}")
                await browser.close()
                return False

            result = await click_element(btn_id, tool_ctx)
            _log(f"Click Reveal #{i+1}: ok={result.ok}")
            if not result.ok:
                _log(f"FAIL: {result.message}")
                await browser.close()
                return False

            await asyncio.sleep(0.3)
            snapshot, index = await _take_snapshot(page, cdp)
            tool_ctx.element_index = index
            _log(f"Snapshot after click {i+1}: {len(index.elements)} elements")

        # Check 1: Is the code in the snapshot?
        code = _search_snapshot_for_code(snapshot, index)
        snapshot_has_code = code is not None and expected in (code or "")

        # Check 2: Is the code in the LLM-formatted snapshot?
        llm_text = format_snapshot_for_llm(snapshot)
        llm_has_code = expected in llm_text

        # Check 3: Is the code in the live DOM (innerText won't cross shadow boundary)?
        dom_text = await page.evaluate("document.body.innerText")
        dom_has_code = expected in dom_text

        # Check 4: Can we reach it via shadowRoot traversal?
        shadow_text = await page.evaluate("""
            (() => {
                try {
                    const h1 = document.getElementById('shadow-host-1');
                    if (!h1 || !h1.shadowRoot) return 'no shadow-host-1';
                    const h2 = h1.shadowRoot.querySelector('#shadow-host-2');
                    if (!h2 || !h2.shadowRoot) return 'no shadow-host-2';
                    const h3 = h2.shadowRoot.querySelector('#shadow-host-3');
                    if (!h3 || !h3.shadowRoot) return 'no shadow-host-3';
                    return h3.shadowRoot.textContent || 'empty';
                } catch(e) { return 'error: ' + e.message; }
            })()
        """)
        shadow_has_code = expected in shadow_text

        # Check 5: Does DOMSnapshot include shadow DOM nodes?
        dom_snap = await cdp.send(
            "DOMSnapshot.captureSnapshot",
            {"computedStyles": [], "includeDOMRects": False},
        )
        snap_strings = dom_snap.get("strings", [])
        snap_has_code_string = any(expected in s for s in snap_strings)

        _log("Results:")
        _log(f"  snapshot search:        {'YES' if snapshot_has_code else 'NO'}")
        _log(f"  LLM formatted snapshot: {'YES' if llm_has_code else 'NO'}")
        _log(f"  DOM innerText:          {'YES' if dom_has_code else 'NO'}")
        _log(
            f"  shadowRoot traversal:   {'YES' if shadow_has_code else 'NO'}"
            f" ({shadow_text[:80]})"
        )
        _log(f"  DOMSnapshot strings:    {'YES' if snap_has_code_string else 'NO'}")

        if snapshot_has_code:
            _log("PASS: Code visible in agent snapshot")
            await browser.close()
            return True

        if snap_has_code_string and not snapshot_has_code:
            _log(
                "PARTIAL: DOMSnapshot has the code string but capture_snapshot"
                " didn't surface it"
            )
            _log("  -> Shadow DOM nodes may not be tagged as interactive")

        if shadow_has_code and not snap_has_code_string:
            _log("FAIL: Shadow DOM content invisible to DOMSnapshot")
            _log("  -> Need to add shadow DOM traversal to snapshot pipeline")
            _log("  -> Workaround: use execute_js to read shadow roots directly")

        if not shadow_has_code:
            _log("FAIL: Shadow DOM content not reachable even via JS traversal")

        await browser.close()
        # Return True if code is findable by *any* means the agent has
        # (snapshot or execute_js), to distinguish "tool gap" from "total failure"
        return snapshot_has_code or (shadow_has_code and snap_has_code_string)


async def _run_websocket():
    """WebSocket: click Connect → wait → read code from terminal area."""
    log = []

    def _log(msg):
        log.append(msg)
        print(f"  [websocket] {msg}")

    expected = "CODE:WS-DEBUG-5P1"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1024, "height": 768})
        page = await context.new_page()
        await page.set_content(WEBSOCKET_HTML)
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(0.3)
        cdp = await context.new_cdp_session(page)

        snapshot, index = await _take_snapshot(page, cdp)
        _log(f"Snapshot: {len(index.elements)} elements")
        llm_text = format_snapshot_for_llm(snapshot)
        _log(f"LLM snapshot:\n{llm_text}")

        tool_ctx = ToolContext(page=page, cdp_session=cdp, element_index=index)

        # Click "Connect"
        btn_id, _ = _find_element_by_attr(index, "id", "connect-btn")
        if not btn_id:
            btn_id, _ = _find_element_by_text(index, "Connect")
        if not btn_id:
            _log("FAIL: Connect button not found")
            await browser.close()
            return False

        result = await click_element(btn_id, tool_ctx)
        _log(f"Click Connect: ok={result.ok} msg={result.message[:120]}")
        if not result.ok:
            _log("FAIL: Could not click Connect")
            await browser.close()
            return False

        # Wait for simulated WS data (1.5s connect + 1.5s data + buffer)
        result = await wait(4000, tool_ctx)
        _log(f"Wait 4s: {result.message[:120]}")

        # Re-snapshot and verify
        snapshot, index = await _take_snapshot(page, cdp)
        tool_ctx.element_index = index

        code = _search_snapshot_for_code(snapshot, index)
        if code and expected in code:
            _log(f"Code found in snapshot: {code}")
            await browser.close()
            return True

        # Check raw_text more broadly
        for line in snapshot.raw_text:
            if expected in line:
                _log(f"Code found in raw_text: {line.strip()}")
                await browser.close()
                return True

        # Fallback: live DOM
        dom_text = await _search_dom_for_code(page, expected)
        if dom_text:
            _log(f"Code found via DOM (NOT in snapshot — investigate): {expected}")
            await browser.close()
            return True

        _log(f"FAIL: code {expected} not found")
        terminal = await page.evaluate(
            'document.getElementById("terminal").textContent'
        )
        _log(f"Terminal text: {terminal}")
        await browser.close()
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    challenges = [
        ("service_worker", _run_service_worker),
        ("dom_mutation", _run_dom_mutation),
        ("recursive_iframe", _run_recursive_iframe),
        ("shadow_dom", _run_shadow_dom),
        ("websocket", _run_websocket),
    ]

    results: dict[str, bool] = {}

    for name, runner in challenges:
        print(f"\n{'=' * 60}")
        print(f"CHALLENGE: {name}")
        print(f"{'=' * 60}")
        try:
            ok = await runner()
            results[name] = ok
        except Exception as e:
            print(f"  [{name}] EXCEPTION: {e}")
            import traceback

            traceback.print_exc()
            results[name] = False

    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")
    for name, ok in results.items():
        status = "PASS" if ok else "FAIL"
        print(f"  {name}: {status}")

    total = len(results)
    passed = sum(1 for ok in results.values() if ok)
    print(f"\n  {passed}/{total} passed")
    print(f"  Overall: {'ALL PASSED' if passed == total else 'SOME FAILED'}")

    if passed < total:
        failed = [name for name, ok in results.items() if not ok]
        print(f"\n  Failed challenges: {', '.join(failed)}")
        print("  Review the diagnostic output above for each failure.")


if __name__ == "__main__":
    asyncio.run(main())
