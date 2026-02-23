"""Debug script to verify the draw tool works — including through overlays."""

import asyncio

from playwright.async_api import async_playwright

from src.agent.context.handlers import cleanup_handler_attributes, extract_handlers
from src.agent.context.snapshot import capture_snapshot, build_element_index
from src.agent.tools.semantic import ToolContext, draw

# Bare canvas, no overlay
CANVAS_HTML = """
<!DOCTYPE html>
<html>
<body style="margin:0">
<canvas id="c" width="400" height="400" tabindex="0" style="border:1px solid black"></canvas>
<script>
const canvas = document.getElementById('c');
const ctx = canvas.getContext('2d');
let drawing = false;
let points = 0;
canvas.addEventListener('mousedown', e => {
    drawing = true;
    ctx.beginPath();
    ctx.moveTo(e.offsetX, e.offsetY);
});
canvas.addEventListener('mousemove', e => {
    if (!drawing) return;
    points++;
    ctx.lineTo(e.offsetX, e.offsetY);
    ctx.stroke();
});
canvas.addEventListener('mouseup', () => {
    drawing = false;
    document.title = 'drawn:' + points;
});
</script>
</body>
</html>
"""

# Canvas with a transparent overlay div covering it entirely
OVERLAY_HTML = """
<!DOCTYPE html>
<html>
<body style="margin:0; position:relative">
<canvas id="c" width="400" height="400" tabindex="0" style="border:1px solid black"></canvas>
<div id="overlay" style="position:absolute; top:0; left:0; width:400px; height:400px; z-index:10; background:rgba(0,0,0,0.01);"></div>
<script>
const canvas = document.getElementById('c');
const ctx = canvas.getContext('2d');
let drawing = false;
let points = 0;
canvas.addEventListener('mousedown', e => {
    drawing = true;
    ctx.beginPath();
    ctx.moveTo(e.offsetX, e.offsetY);
});
canvas.addEventListener('mousemove', e => {
    if (!drawing) return;
    points++;
    ctx.lineTo(e.offsetX, e.offsetY);
    ctx.stroke();
});
canvas.addEventListener('mouseup', () => {
    drawing = false;
    document.title = 'drawn:' + points;
});
</script>
</body>
</html>
"""

# Canvas with pointer-events:none overlay (common pattern — should work)
PASSTHROUGH_OVERLAY_HTML = """
<!DOCTYPE html>
<html>
<body style="margin:0; position:relative">
<canvas id="c" width="400" height="400" tabindex="0" style="border:1px solid black"></canvas>
<div id="overlay" style="position:absolute; top:0; left:0; width:400px; height:400px; z-index:10; background:rgba(255,0,0,0.2); pointer-events:none;"></div>
<script>
const canvas = document.getElementById('c');
const ctx = canvas.getContext('2d');
let drawing = false;
let points = 0;
canvas.addEventListener('mousedown', e => {
    drawing = true;
    ctx.beginPath();
    ctx.moveTo(e.offsetX, e.offsetY);
});
canvas.addEventListener('mousemove', e => {
    if (!drawing) return;
    points++;
    ctx.lineTo(e.offsetX, e.offsetY);
    ctx.stroke();
});
canvas.addEventListener('mouseup', () => {
    drawing = false;
    document.title = 'drawn:' + points;
});
</script>
</body>
</html>
"""


async def run_test(page, cdp, label: str) -> tuple[bool, int]:
    """Draw on canvas, return (ok, points_drawn)."""
    handler_map = await extract_handlers(page)
    snapshot = await capture_snapshot(page, cdp, handler_map=handler_map)
    await cleanup_handler_attributes(page)
    index = build_element_index(snapshot)

    canvas_id = None
    for sid, el in index.elements.items():
        if (el.node_name or "").lower() == "canvas":
            canvas_id = sid
            break

    if not canvas_id:
        print(f"  [{label}] FAIL: no canvas element in snapshot")
        return False, 0

    tool_ctx = ToolContext(page=page, cdp_session=cdp, element_index=index)
    path = [[50, 50], [100, 100], [150, 150], [200, 200]]
    result = await draw(canvas_id, path, tool_ctx)

    # Read the title which the page sets to "drawn:<N>" on mouseup
    title = await page.title()
    points_drawn = 0
    if title.startswith("drawn:"):
        points_drawn = int(title.split(":")[1])

    print(f"  [{label}] ok={result.ok} points_drawn={points_drawn} message={result.message}")
    return result.ok, points_drawn


async def main() -> None:
    results: dict[str, tuple[bool, int]] = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        for label, html in [
            ("no_overlay", CANVAS_HTML),
            ("blocking_overlay", OVERLAY_HTML),
            ("passthrough_overlay", PASSTHROUGH_OVERLAY_HTML),
        ]:
            context = await browser.new_context(viewport={"width": 800, "height": 600})
            page = await context.new_page()
            await page.set_content(html)
            await page.wait_for_load_state("domcontentloaded")
            cdp = await context.new_cdp_session(page)

            ok, pts = await run_test(page, cdp, label)
            results[label] = (ok, pts)
            await context.close()

        await browser.close()

    print("\n--- Summary ---")
    for label, (ok, pts) in results.items():
        status = "PASS" if ok else "FAIL"
        drew = f"points_drawn={pts}" if pts > 0 else "NO DRAW (events blocked)"
        print(f"  {label}: {status} — {drew}")

    # no_overlay and passthrough must draw; blocking_overlay dispatches ok but
    # the overlay intercepts the CDP mouse events so canvas gets 0 points.
    no_ok, no_pts = results["no_overlay"]
    pt_ok, pt_pts = results["passthrough_overlay"]
    bl_ok, bl_pts = results["blocking_overlay"]

    all_good = no_ok and no_pts > 0 and pt_ok and pt_pts > 0 and bl_ok
    print(f"\nOverlay test {'PASSED' if all_good else 'FAILED'}.")
    if bl_pts == 0:
        print("  Note: blocking overlay intercepted CDP mouse events as expected (0 canvas points).")
    elif bl_pts > 0:
        print("  Note: blocking overlay did NOT block — CDP events reached the canvas anyway.")


if __name__ == "__main__":
    asyncio.run(main())
