"""Debug script to verify the hover tool works across different reveal patterns."""

import asyncio

from playwright.async_api import async_playwright

from src.agent.context.handlers import cleanup_handler_attributes, extract_handlers
from src.agent.context.snapshot import capture_snapshot, build_element_index
from src.agent.tools.semantic import ToolContext, hover_element

# Scenario 1: Time-gated reveal on mouseleave (the challenge pattern)
# Code appears only after accumulated hover time >= 1 second, tracked via mouseleave.
MOUSELEAVE_ACCUMULATE_HTML = """
<!DOCTYPE html>
<html>
<body>
<div id="target" tabindex="0" style="width:200px;height:100px;background:#eee;cursor:pointer;display:flex;align-items:center;justify-content:center;">
  <span id="status">Hover me for 1s</span>
</div>
<div id="result"></div>
<script>
let totalTime = 0;
let startTime = null;
const target = document.getElementById('target');
const status = document.getElementById('status');
const result = document.getElementById('result');
target.addEventListener('mouseenter', () => {
    startTime = Date.now();
    status.textContent = 'Hovering...';
});
target.addEventListener('mouseleave', () => {
    if (startTime) {
        totalTime += Date.now() - startTime;
        startTime = null;
    }
    if (totalTime >= 1000) {
        result.textContent = 'CODE:ABC123';
        status.textContent = 'Code revealed!';
    } else {
        status.textContent = 'Hover me for 1s (accumulated: ' + totalTime + 'ms)';
    }
});
</script>
</body>
</html>
"""

# Scenario 2: Immediate reveal on mouseenter (shows while hovering)
# Code appears as soon as mouseenter fires.
MOUSEENTER_IMMEDIATE_HTML = """
<!DOCTYPE html>
<html>
<body>
<div id="target" tabindex="0" style="width:200px;height:100px;background:#eee;cursor:pointer;display:flex;align-items:center;justify-content:center;">
  <span id="status">Hover to reveal</span>
</div>
<div id="result"></div>
<script>
const target = document.getElementById('target');
const status = document.getElementById('status');
const result = document.getElementById('result');
target.addEventListener('mouseenter', () => {
    result.textContent = 'CODE:XYZ789';
    status.textContent = 'Revealed!';
});
target.addEventListener('mouseleave', () => {
    result.textContent = '';
    status.textContent = 'Hover to reveal';
});
</script>
</body>
</html>
"""

# Scenario 3: Timer-based reveal (shows after 500ms of continuous hover)
# Uses mouseenter to start a timer; mouseleave cancels it.
TIMER_REVEAL_HTML = """
<!DOCTYPE html>
<html>
<body>
<div id="target" tabindex="0" style="width:200px;height:100px;background:#eee;cursor:pointer;display:flex;align-items:center;justify-content:center;">
  <span id="status">Hover for 500ms</span>
</div>
<div id="result"></div>
<script>
let timer = null;
const target = document.getElementById('target');
const status = document.getElementById('status');
const result = document.getElementById('result');
target.addEventListener('mouseenter', () => {
    status.textContent = 'Hovering...';
    timer = setTimeout(() => {
        result.textContent = 'CODE:TMR456';
        status.textContent = 'Timer fired!';
    }, 500);
});
target.addEventListener('mouseleave', () => {
    if (timer) { clearTimeout(timer); timer = null; }
    if (!result.textContent) {
        status.textContent = 'Hover for 500ms';
    }
});
</script>
</body>
</html>
"""

# Scenario 4: CSS :hover pseudo-class reveal (pure CSS, no JS handlers)
# A sibling element becomes visible via CSS :hover.
CSS_HOVER_HTML = """
<!DOCTYPE html>
<html>
<head>
<style>
#target { width:200px;height:100px;background:#eee;cursor:pointer;display:flex;align-items:center;justify-content:center; }
#secret { display:none; margin-top:10px; font-weight:bold; }
#target:hover + #secret { display:block; }
</style>
</head>
<body>
<div id="target" tabindex="0"><span>Hover me (CSS)</span></div>
<div id="secret">CODE:CSS001</div>
</body>
</html>
"""

# Scenario 5: React-style state pattern (simulates the challenge page)
# mouseenter sets isHovering=true and records start time.
# mouseleave accumulates time. Code appears when total >= 1000ms.
# Uses a simple state object to mimic React state.
REACT_PATTERN_HTML = """
<!DOCTYPE html>
<html>
<body>
<div id="target" tabindex="0" style="width:200px;height:100px;background:#eee;cursor:pointer;display:flex;align-items:center;justify-content:center;border:2px solid #ccc;transition:all 0.3s;">
  <span id="status">Hover 1s to reveal</span>
</div>
<div id="result"></div>
<script>
const state = { isHovering: false, totalTime: 0, startRef: null };
const target = document.getElementById('target');
const status = document.getElementById('status');
const result = document.getElementById('result');

target.addEventListener('mouseenter', () => {
    state.isHovering = true;
    state.startRef = Date.now();
    target.style.borderColor = '#66f';
    status.textContent = 'Hovering...';
});

target.addEventListener('mouseleave', () => {
    if (state.startRef) {
        const elapsed = Date.now() - state.startRef;
        state.totalTime += elapsed;
    }
    state.isHovering = false;
    state.startRef = null;
    target.style.borderColor = '#ccc';

    if (state.totalTime >= 1000) {
        result.textContent = 'CODE:REACT1';
        target.style.borderColor = '#0a0';
        status.textContent = 'Code revealed!';
    } else {
        status.textContent = 'Need ' + Math.max(0, 1000 - state.totalTime) + 'ms more';
    }
});
</script>
</body>
</html>
"""


import re

_CODE_RE = re.compile(r"CODE:\w+")


async def run_test(page, cdp, label: str, duration_ms: int = 1100) -> tuple[bool, str, str]:
    """Hover on #target, return (code_found, dom_code, tool_msg_code)."""
    handler_map = await extract_handlers(page)
    snapshot = await capture_snapshot(page, cdp, handler_map=handler_map)
    await cleanup_handler_attributes(page)
    index = build_element_index(snapshot)

    # Find the target element
    target_id = None
    for sid, el in index.elements.items():
        attrs = el.attributes or {}
        if attrs.get("id") == "target":
            target_id = sid
            break

    if not target_id:
        print(f"  [{label}] FAIL: no #target element in snapshot")
        return False, "", ""

    tool_ctx = ToolContext(page=page, cdp_session=cdp, element_index=index)
    result = await hover_element(target_id, tool_ctx, duration_ms=duration_ms)

    # Wait a bit for any async renders
    await asyncio.sleep(0.15)

    # Read the result div content (persisted in DOM after hover)
    result_text = await page.evaluate("document.getElementById('result')?.textContent || ''")
    # Also check CSS-revealed content
    secret_text = await page.evaluate(
        "document.getElementById('secret')?.textContent || ''"
    )
    dom_code = result_text or secret_text

    # Also extract code from tool message (captures transient reveals)
    msg_match = _CODE_RE.search(result.message)
    tool_msg_code = msg_match.group(0) if msg_match else ""

    # Code is found if it's in EITHER the DOM or the tool message
    code_found = bool(dom_code.startswith("CODE:") or tool_msg_code)
    source = "dom" if dom_code.startswith("CODE:") else ("tool_msg" if tool_msg_code else "none")
    print(f"  [{label}] ok={result.ok} code_found={code_found} source={source} "
          f"dom={dom_code!r} tool_msg_code={tool_msg_code!r}")
    return code_found, dom_code, tool_msg_code


async def main() -> None:
    results: dict[str, tuple[bool, str, str]] = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        scenarios = [
            ("mouseleave_accumulate", MOUSELEAVE_ACCUMULATE_HTML, 1100),
            ("mouseenter_immediate", MOUSEENTER_IMMEDIATE_HTML, 500),
            ("timer_reveal", TIMER_REVEAL_HTML, 1000),
            ("css_hover", CSS_HOVER_HTML, 500),
            ("react_pattern", REACT_PATTERN_HTML, 1100),
        ]

        for label, html, duration in scenarios:
            context = await browser.new_context(viewport={"width": 800, "height": 600})
            page = await context.new_page()
            await page.set_content(html)
            await page.wait_for_load_state("domcontentloaded")
            cdp = await context.new_cdp_session(page)

            code_found, dom_code, msg_code = await run_test(
                page, cdp, label, duration_ms=duration
            )
            results[label] = (code_found, dom_code, msg_code)
            await context.close()

        await browser.close()

    print("\n--- Summary ---")
    for label, (code_found, dom_code, msg_code) in results.items():
        status = "PASS" if code_found else "FAIL"
        source = "dom" if dom_code.startswith("CODE:") else "tool_msg"
        print(f"  {label}: {status} — dom={dom_code!r} tool_msg={msg_code!r} (via {source})")

    all_pass = all(found for found, _, _ in results.values())
    print(f"\nOverall: {'ALL PASSED' if all_pass else 'SOME FAILED'}")


if __name__ == "__main__":
    asyncio.run(main())
