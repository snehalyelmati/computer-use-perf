"""Debug script to verify recursive iframe navigation works across nesting depths.

Tests the agent's ability to:
1. Discover iframe elements at each nesting level in the snapshot
2. switch_to_iframe into progressively deeper frames
3. Click elements inside the active frame (respecting the active_frame_error guard)
4. Return to the main frame after completing the challenge

Scenarios mirror the real challenge: 3, 4, and 5 levels of nested iframes, each with
an "Enter Level N" button that must be clicked before descending into the next iframe.
The deepest level reveals a code that must be read.

FINDINGS:
  - BUG: The active_frame_error guard prevents clicking buttons inside a child iframe
    after switch_to_iframe. When the agent switches to iframe-0, active_frame_id is set
    to iframe-0's child document frame_id. But "Enter Level 2" button lives in a DEEPER
    document (iframe-1's parent doc = iframe-0's child doc). The snapshot assigns the
    button a frame_id matching the document it lives in, which differs from the iframe
    element's frame_id that was used for switch_to_iframe.

    Root cause: IFRAME elements get frame_id = their child document's frameId. But
    elements INSIDE that child document also get that same frameId. So after
    switch_to_iframe(iframe-0), active_frame_id = iframe-0's child doc frameId. The
    "Enter Level 2" button IS in that child doc, so its frame_id SHOULD match. The bug
    is that the DOMSnapshot frameId values are numeric document indices, not the actual
    Chrome frame IDs — and the IFRAME element's frame_id comes from contentDocumentIndex
    resolution while the button's frame_id comes from its parent document's frameId.
    These use different numbering.

  - Flat scenario (no click gate) passes because switch_to_iframe doesn't check the
    guard — it just updates active_frame_id.
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
    switch_to_iframe,
    switch_to_main_frame,
)


def _make_nested_html(depth: int, code: str) -> str:
    """Build an HTML page with `depth` levels of nested iframes.

    Structure at each level:
      - A heading "Iframe Level N" with depth indicator "depth: K/D"
      - An "Enter Level N+1" button that reveals the child iframe when clicked
      - A child iframe (srcdoc) containing the next level

    The deepest level shows the code and a "Submit" button.
    """
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


DEPTH_3_CODE = "CODE:IFRAME3"
DEPTH_4_CODE = "CODE:IFRAME4"
DEPTH_5_CODE = "CODE:IFRAME5"


def _flat_nested_html(depth: int, code: str) -> str:
    """Like _make_nested_html but all iframes are visible from the start."""
    max_depth = depth

    def _level_html(current: int) -> str:
        if current == max_depth:
            return f"""\
<!DOCTYPE html>
<html>
<body>
<h2>Deepest Level {current}</h2>
<p>depth: {current}/{max_depth}</p>
<div id="code-display">Code: {code}</div>
<button id="submit-btn">Submit</button>
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
<iframe id="child-frame-{current}"
        srcdoc="{escaped}"
        style="width:100%; height:400px; border:1px solid #ccc;">
</iframe>
</body>
</html>"""

    return _level_html(0)


# ---- Test helpers ----


async def _take_snapshot(page, cdp, handler_map=None):
    """Capture snapshot and build element index."""
    if handler_map is None:
        handler_map = await extract_handlers(page)
    snapshot = await capture_snapshot(page, cdp, handler_map=handler_map)
    await cleanup_handler_attributes(page)
    index = build_element_index(snapshot)
    return snapshot, index


def _find_element_by_attr(index, attr_name: str, attr_value: str):
    """Find element by HTML attribute in the index."""
    for sid, el in index.elements.items():
        attrs = el.attributes or {}
        if attrs.get(attr_name) == attr_value:
            return sid, el
    return None, None


def _find_element_by_text(index, text: str):
    """Find element whose name or text contains the given string."""
    for sid, el in index.elements.items():
        label = el.name or el.text or ""
        if text.lower() in label.lower():
            return sid, el
    return None, None


def _find_iframes_in_snapshot(index):
    """Return all iframe elements."""
    iframes = []
    for sid, el in index.elements.items():
        if (el.node_name or "").upper() == "IFRAME":
            iframes.append((sid, el))
    return iframes


# ---- Scenario: flat (no click gate) ----


async def _run_flat_scenario(label: str, html: str, depth: int, expected_code: str):
    """Navigate through pre-visible nested iframes via switch_to_iframe only."""
    log = []

    def _log(msg):
        log.append(f"  [{label}] {msg}")
        print(log[-1])

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1024, "height": 768})
        page = await context.new_page()
        await page.set_content(html)
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(0.5)
        cdp = await context.new_cdp_session(page)

        snapshot, index = await _take_snapshot(page, cdp)
        iframes = _find_iframes_in_snapshot(index)
        _log(f"Snapshot: {len(index.elements)} elements, {len(iframes)} iframes")

        llm_text = format_snapshot_for_llm(snapshot)
        for line in llm_text.strip().split("\n"):
            _log(f"  | {line}")

        _log("Element frame_id map:")
        for sid, el in index.elements.items():
            el_id = (el.attributes or {}).get("id", "")
            _log(f"  {sid} id={el_id:<20s} node={el.node_name:<8s} frame_id={el.frame_id}")

        tool_ctx = ToolContext(page=page, cdp_session=cdp, element_index=index)
        passed = True

        for level in range(depth):
            iframe_id, iframe_el = _find_element_by_attr(index, "id", f"child-frame-{level}")
            if not iframe_id:
                _log(f"FAIL: iframe child-frame-{level} not found")
                passed = False
                break
            result = await switch_to_iframe(iframe_id, tool_ctx)
            _log(f"Level {level}: switch_to_iframe -> active={tool_ctx.active_frame_id} ok={result.ok}")
            if not result.ok:
                passed = False
                break

        code_found = False
        if passed:
            for line in snapshot.raw_text:
                if expected_code in line:
                    _log(f"Code found in raw_text: {line}")
                    code_found = True
                    break
            if not code_found:
                for frame in page.frames:
                    try:
                        txt = await frame.evaluate(
                            "document.getElementById('code-display')?.textContent || ''"
                        )
                        if expected_code in txt:
                            _log(f"Code found via DOM eval: {txt.strip()}")
                            code_found = True
                            break
                    except Exception:
                        continue
            if not code_found:
                _log(f"FAIL: code {expected_code} not found")
                passed = False

        await switch_to_main_frame(tool_ctx)
        await browser.close()

    status = "PASS" if (passed and code_found) else "FAIL"
    _log(f"Result: {status}")
    return passed and code_found


# ---- Scenario: click-to-reveal (the real challenge pattern) ----


async def _run_click_reveal_scenario(label: str, html: str, depth: int, expected_code: str):
    """Navigate through nested iframes clicking "Enter Level N" at each level."""
    log = []

    def _log(msg):
        log.append(f"  [{label}] {msg}")
        print(log[-1])

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1024, "height": 768})
        page = await context.new_page()
        await page.set_content(html)
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(0.5)
        cdp = await context.new_cdp_session(page)

        snapshot, index = await _take_snapshot(page, cdp)
        _log(f"Snapshot: {len(index.elements)} elements, {len(_find_iframes_in_snapshot(index))} iframes")

        llm_text = format_snapshot_for_llm(snapshot)
        for line in llm_text.strip().split("\n"):
            _log(f"  | {line}")

        _log("Element frame_id map:")
        for sid, el in index.elements.items():
            el_id = (el.attributes or {}).get("id", "")
            _log(f"  {sid} id={el_id:<20s} node={el.node_name:<8s} frame_id={el.frame_id}")

        # Show frame tree from Page.getFrameTree for comparison
        try:
            ft = await cdp.send("Page.getFrameTree")
            main_fid = ft.get("frameTree", {}).get("frame", {}).get("id")
            _log(f"Page.getFrameTree main frame: {main_fid}")

            def _walk_tree(node, indent=0):
                fid = node.get("frame", {}).get("id", "?")
                name = node.get("frame", {}).get("name", "")
                url = node.get("frame", {}).get("url", "")
                _log(f"  {'  ' * indent}frame_id={fid} name={name} url={url}")
                for child in node.get("childFrames", []):
                    _walk_tree(child, indent + 1)

            _walk_tree(ft.get("frameTree", {}))
        except Exception as e:
            _log(f"Frame tree error: {e}")

        # Also show DOMSnapshot document frameIds for comparison
        try:
            dom_snap = await cdp.send(
                "DOMSnapshot.captureSnapshot",
                {"computedStyles": [], "includeDOMRects": False},
            )
            docs = dom_snap.get("documents", [])
            _log(f"DOMSnapshot has {len(docs)} documents:")
            for i, doc in enumerate(docs):
                doc_fid = doc.get("frameId", "?")
                _log(f"  doc[{i}] frameId={doc_fid}")
        except Exception as e:
            _log(f"DOMSnapshot error: {e}")

        tool_ctx = ToolContext(page=page, cdp_session=cdp, element_index=index)
        passed = True

        for level in range(depth):
            _log(f"--- Level {level} ---")

            # Find and click "Enter Level N+1"
            btn_id, btn_el = _find_element_by_attr(index, "id", f"enter-btn-{level}")
            if not btn_id:
                btn_id, btn_el = _find_element_by_text(index, f"Enter Level {level + 1}")

            if btn_id:
                _log(f"Button {btn_id} frame_id={btn_el.frame_id}, active_frame={tool_ctx.active_frame_id}")
                result = await click_element(btn_id, tool_ctx)
                _log(f"Click 'Enter Level {level + 1}': ok={result.ok} msg={result.message}")
                if not result.ok:
                    _log("FAIL: click blocked (likely active_frame_error guard)")
                    passed = False
                    break
                await asyncio.sleep(0.3)
            else:
                _log(f"FAIL: no button found for level {level}")
                passed = False
                break

            # Re-snapshot and switch to next iframe
            snapshot, index = await _take_snapshot(page, cdp)
            tool_ctx.element_index = index

            iframe_id, iframe_el = _find_element_by_attr(index, "id", f"child-frame-{level}")
            if not iframe_id:
                _log(f"FAIL: iframe child-frame-{level} not found after click")
                passed = False
                break

            result = await switch_to_iframe(iframe_id, tool_ctx)
            _log(f"switch_to_iframe: active={tool_ctx.active_frame_id} ok={result.ok}")
            if not result.ok:
                passed = False
                break

        code_found = False
        if passed:
            snapshot, index = await _take_snapshot(page, cdp)
            tool_ctx.element_index = index
            for line in snapshot.raw_text:
                if expected_code in line:
                    _log(f"Code found: {line}")
                    code_found = True
                    break
            if not code_found:
                for frame in page.frames:
                    try:
                        txt = await frame.evaluate(
                            "document.getElementById('code-display')?.textContent || ''"
                        )
                        if expected_code in txt:
                            _log(f"Code via DOM: {txt.strip()}")
                            code_found = True
                            break
                    except Exception:
                        continue
            if not code_found:
                _log(f"FAIL: code not found")
                passed = False

        await switch_to_main_frame(tool_ctx)

        # ---- Guard test ----
        _log("--- Guard test ---")
        snapshot, index = await _take_snapshot(page, cdp)
        tool_ctx.element_index = index
        iframes = _find_iframes_in_snapshot(index)
        if iframes:
            await switch_to_iframe(iframes[0][0], tool_ctx)
            for sid, el in index.elements.items():
                if (el.frame_id or "") != (tool_ctx.active_frame_id or "") and el.node_name == "BUTTON":
                    r = await click_element(sid, tool_ctx)
                    if not r.ok and "not in the active frame" in r.message:
                        _log(f"Guard OK: blocked cross-frame click")
                    else:
                        _log(f"Guard WARN: did not block cross-frame click")
                    break
            await switch_to_main_frame(tool_ctx)

        await browser.close()

    status = "PASS" if (passed and code_found) else "FAIL"
    _log(f"Result: {status}")
    return passed and code_found


# ---- Scenario: workaround — click without frame guard ----


async def _run_workaround_scenario(label: str, html: str, depth: int, expected_code: str):
    """Same as click-reveal but resets to main frame before each click.

    This tests the workaround: instead of staying in the iframe context,
    switch back to main_frame before clicking the next button, since all
    elements from all frames appear in the snapshot regardless.
    """
    log = []

    def _log(msg):
        log.append(f"  [{label}] {msg}")
        print(log[-1])

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
        passed = True

        _log(f"Snapshot: {len(index.elements)} elements")

        for level in range(depth):
            _log(f"--- Level {level} ---")

            # WORKAROUND: always reset to main frame before clicking
            await switch_to_main_frame(tool_ctx)
            tool_ctx.active_frame_id = None

            btn_id, btn_el = _find_element_by_attr(index, "id", f"enter-btn-{level}")
            if not btn_id:
                btn_id, btn_el = _find_element_by_text(index, f"Enter Level {level + 1}")

            if btn_id:
                _log(f"Click enter-btn-{level} (frame_id={btn_el.frame_id}, active=None)")
                result = await click_element(btn_id, tool_ctx)
                _log(f"  ok={result.ok}")
                if not result.ok:
                    _log(f"  FAIL: {result.message}")
                    passed = False
                    break
                await asyncio.sleep(0.3)
            else:
                _log(f"FAIL: button not found")
                passed = False
                break

            # Re-snapshot
            snapshot, index = await _take_snapshot(page, cdp)
            tool_ctx.element_index = index

        code_found = False
        if passed:
            for line in snapshot.raw_text:
                if expected_code in line:
                    _log(f"Code found: {line}")
                    code_found = True
                    break
            if not code_found:
                for frame in page.frames:
                    try:
                        txt = await frame.evaluate(
                            "document.getElementById('code-display')?.textContent || ''"
                        )
                        if expected_code in txt:
                            _log(f"Code via DOM: {txt.strip()}")
                            code_found = True
                            break
                    except Exception:
                        continue
            if not code_found:
                _log(f"FAIL: code not found")
                passed = False

        await browser.close()

    status = "PASS" if (passed and code_found) else "FAIL"
    _log(f"Result: {status}")
    return passed and code_found


async def main() -> None:
    results: dict[str, bool] = {}

    scenarios = [
        # 1. Flat (pre-visible iframes, no click gate) — baseline
        ("flat_3_levels", _flat_nested_html, 3, DEPTH_3_CODE, _run_flat_scenario),
        # 2-4. Click-to-reveal at various depths — exposes the frame guard bug
        ("click_reveal_3", _make_nested_html, 3, DEPTH_3_CODE, _run_click_reveal_scenario),
        ("click_reveal_4", _make_nested_html, 4, DEPTH_4_CODE, _run_click_reveal_scenario),
        ("click_reveal_5", _make_nested_html, 5, DEPTH_5_CODE, _run_click_reveal_scenario),
        # 5-6. Workaround: reset to main frame before each click
        ("workaround_3", _make_nested_html, 3, DEPTH_3_CODE, _run_workaround_scenario),
        ("workaround_5", _make_nested_html, 5, DEPTH_5_CODE, _run_workaround_scenario),
    ]

    for label, html_fn, depth, code, runner in scenarios:
        print(f"\n{'=' * 60}")
        print(f"SCENARIO: {label} (depth={depth})")
        print(f"{'=' * 60}")
        html = html_fn(depth, code)
        ok = await runner(label, html, depth, code)
        results[label] = ok

    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")
    for label, ok in results.items():
        print(f"  {label}: {'PASS' if ok else 'FAIL'}")

    total = len(results)
    passed = sum(1 for ok in results.values() if ok)
    print(f"\n  {passed}/{total} passed")
    print(f"  Overall: {'ALL PASSED' if passed == total else 'SOME FAILED'}")

    if passed < total:
        print("\n  DIAGNOSIS:")
        print("  The click-reveal scenarios fail because the active_frame_error guard")
        print("  blocks clicking buttons whose frame_id doesn't match the active_frame_id.")
        print("  DOMSnapshot assigns numeric document-index frame_ids, not Chrome hex frame IDs.")
        print("  IFRAME elements get their child document's frameId, while sibling elements")
        print("  (buttons in the same document) get the parent document's frameId.")
        print("  After switch_to_iframe, active_frame_id = child doc id, but the next")
        print("  button to click has parent doc id -> mismatch -> blocked.")
        print()
        print("  WORKAROUND: Reset to main_frame before clicking, relying on")
        print("  _session_for_element to resolve the correct CDP session per element.")


if __name__ == "__main__":
    asyncio.run(main())
