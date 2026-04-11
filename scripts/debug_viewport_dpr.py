"""Verify that _in_viewport correctly classifies elements across the full screen on HiDPI displays.

Launches a headed browser with a test page containing elements at known CSS positions,
captures a snapshot, and checks that all visible elements are classified as in-viewport.
"""

import asyncio

from playwright.async_api import async_playwright


HTML = """
<html><body style="margin:0; width:100vw; height:100vh; position:relative;">
  <div id="top-left"     style="position:absolute; left:10px;   top:10px;   width:50px; height:50px; background:red;"></div>
  <div id="top-right"    style="position:absolute; right:10px;  top:10px;   width:50px; height:50px; background:green;"></div>
  <div id="bottom-left"  style="position:absolute; left:10px;   bottom:10px; width:50px; height:50px; background:blue;"></div>
  <div id="bottom-right" style="position:absolute; right:10px;  bottom:10px; width:50px; height:50px; background:purple;"></div>
  <div id="center"       style="position:absolute; left:50%;    top:50%;    width:50px; height:50px; background:orange; transform:translate(-50%,-50%);"></div>
  <button id="btn-right" style="position:absolute; right:60px;  top:50%;    padding:8px 16px;">Click Me</button>
  <div id="offscreen"    style="position:absolute; left:3000px; top:10px;   width:50px; height:50px; background:gray;"></div>
</body></html>
"""


async def main():
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False)
    context = await browser.new_context(viewport={"width": 1920, "height": 1080})
    page = await context.new_page()

    await page.set_content(HTML)
    await page.wait_for_timeout(500)

    cdp = await context.new_cdp_session(page)

    # Get layout metrics
    lm = await cdp.send("Page.getLayoutMetrics")
    css_vp = lm["cssVisualViewport"]
    vis_vp = lm["visualViewport"]
    dpr = vis_vp["clientWidth"] / css_vp["clientWidth"]

    print(f"CSS viewport:    {css_vp['clientWidth']}x{css_vp['clientHeight']}")
    print(f"Device viewport: {vis_vp['clientWidth']}x{vis_vp['clientHeight']}")
    print(f"DPR:             {dpr}")
    print(f"page.viewport:   {page.viewport_size}")
    print()

    # Get DOMSnapshot bounds
    snap = await cdp.send(
        "DOMSnapshot.captureSnapshot",
        {"computedStyles": [], "includeDOMRects": True},
    )
    strings = snap["strings"]
    doc = snap["documents"][0]
    nodes = doc["nodes"]
    layout = doc["layout"]
    layout_indices = layout["nodeIndex"]
    bounds_data = layout["bounds"]
    attrs = nodes.get("attributes", [])

    def get_bounds(node_idx):
        if node_idx in layout_indices:
            li = layout_indices.index(node_idx)
            if bounds_data and isinstance(bounds_data[0], list):
                return bounds_data[li]
            s = li * 4
            return bounds_data[s : s + 4]
        return None

    def get_id(node_idx):
        if node_idx < len(attrs):
            a = attrs[node_idx]
            for i in range(0, len(a), 2):
                if strings[a[i]] == "id":
                    return strings[a[i + 1]]
        return None

    # Check each element
    print(f"{'id':<18} {'CDP bounds (device px)':<35} {'in_viewport (old)':<20} {'in_viewport (fixed)'}")
    print("-" * 100)

    old_vw = page.viewport_size["width"]   # CSS pixels (old, buggy)
    old_vh = page.viewport_size["height"]
    new_vw = vis_vp["clientWidth"]          # Device pixels (fixed)
    new_vh = vis_vp["clientHeight"]

    for idx in range(len(nodes.get("nodeName", []))):
        eid = get_id(idx)
        if not eid:
            continue
        b = get_bounds(idx)
        if not b:
            continue
        x, y, w, h = b

        def in_vp(vw, vh):
            if w <= 0 or h <= 0:
                return False
            return not (x + w < 0 or y + h < 0 or x > vw or y > vh)

        old_result = in_vp(old_vw, old_vh)
        new_result = in_vp(new_vw, new_vh)
        marker = ""
        if old_result != new_result:
            marker = " <-- FIX"
        print(
            f"{eid:<18} [{x:7.1f}, {y:7.1f}, {w:7.1f}, {h:7.1f}]  "
            f"{'True' if old_result else 'FALSE':<20} "
            f"{'True' if new_result else 'FALSE'}{marker}"
        )

    await browser.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
