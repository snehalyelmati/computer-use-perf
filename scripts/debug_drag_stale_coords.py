"""Debug script: verify that sequential scrollIntoView in drag_and_drop
causes stale source coordinates, making CDP mousePressed hit a drop zone
instead of the draggable piece.

Replicates the exact _viewport_info + CDP drag sequence from semantic.py.
"""

import asyncio
from playwright.async_api import async_playwright

# Pieces at ~y=600, drop zones at ~y=800 (below viewport).
# Viewport 1280x720 — drop zones offscreen, requiring scroll.
TEST_HTML = """
<!DOCTYPE html>
<html>
<head><style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { width: 1280px; height: 1600px; font-family: monospace; }
  .spacer { height: 500px; background: #eee; display: flex; align-items: center;
            justify-content: center; color: #999; font-size: 24px; }
  .pieces { display: flex; gap: 16px; padding: 20px; background: #e0e7ff; }
  .piece { width: 78px; height: 96px; background: white; border: 2px solid #6366f1;
           display: flex; align-items: center; justify-content: center;
           font-size: 24px; font-weight: bold; cursor: move; border-radius: 8px; }
  .drop-row { display: flex; gap: 16px; padding: 20px; margin-top: 80px; }
  .slot { width: 128px; height: 128px; border: 2px dashed #9ca3af; background: #f3f4f6;
          display: flex; align-items: center; justify-content: center;
          font-size: 14px; color: #9ca3af; border-radius: 8px; }
  .slot.filled { border-color: #22c55e; background: #dcfce7; color: #166534; font-size: 24px; }
</style></head>
<body>
  <div class="spacer">Spacer</div>
  <div class="pieces" id="pieces">
    <div class="piece" draggable="true" id="p0">H</div>
    <div class="piece" draggable="true" id="p1">A</div>
    <div class="piece" draggable="true" id="p2">R</div>
  </div>
  <div class="drop-row" id="slots">
    <div class="slot" id="s0">Slot 1</div>
    <div class="slot" id="s1">Slot 2</div>
    <div class="slot" id="s2">Slot 3</div>
  </div>
  <script>
    let draggedPiece = null;
    const events = [];

    // Log ALL mouse and drag events on body
    for (const evtName of ['mousedown','mousemove','mouseup','dragstart','drag','dragenter','dragover','drop','dragend']) {
      document.body.addEventListener(evtName, e => {
        const t = e.target;
        events.push({
          type: evtName,
          targetId: t.id || t.parentElement?.id || '?',
          targetTag: t.tagName,
          draggable: t.draggable || false,
          x: Math.round(e.clientX), y: Math.round(e.clientY),
        });
      }, true);
    }

    window.getEvents = () => { const r = [...events]; events.length = 0; return r; };

    document.querySelectorAll('.piece').forEach(el => {
      el.addEventListener('dragstart', e => {
        draggedPiece = el.id;
        e.dataTransfer.setData('text/plain', el.id);
      });
    });
    document.querySelectorAll('.slot').forEach(slot => {
      slot.addEventListener('dragover', e => e.preventDefault());
      slot.addEventListener('drop', e => {
        e.preventDefault();
        if (draggedPiece) {
          const piece = document.getElementById(draggedPiece);
          slot.textContent = piece ? piece.textContent : '?';
          slot.classList.add('filled');
          draggedPiece = null;
        }
      });
    });
  </script>
</body>
</html>
"""

VIEWPORT_INFO_JS = """
function () {
    this.scrollIntoView({block: 'center', inline: 'center'});
    const rect = this.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    const hit = document.elementFromPoint(x, y);
    const onTop = !!(hit && (this.contains(hit) || hit.contains(this)));
    return {x, y, width: rect.width, height: rect.height, onTop};
}
"""


async def call_on_node(session, backend_node_id: int, js: str):
    resolved = await session.send("DOM.resolveNode", {"backendNodeId": backend_node_id})
    object_id = resolved["object"]["objectId"]
    result = await session.send("Runtime.callFunctionOn", {
        "functionDeclaration": js, "objectId": object_id, "returnByValue": True,
    })
    return result.get("result", {}).get("value")


async def get_backend_node_id(session, selector: str) -> int:
    doc = await session.send("DOM.getDocument", {"depth": 0})
    node = await session.send("DOM.querySelector", {
        "nodeId": doc["root"]["nodeId"], "selector": selector,
    })
    info = await session.send("DOM.describeNode", {"nodeId": node["nodeId"]})
    return info["node"]["backendNodeId"]


async def cdp_drag(session, sx, sy, tx, ty):
    """Exact CDP drag sequence from semantic.py lines 745-767."""
    await session.send("Input.dispatchMouseEvent",
                       {"type": "mouseMoved", "x": int(sx), "y": int(sy), "button": "left"})
    await session.send("Input.dispatchMouseEvent",
                       {"type": "mousePressed", "x": int(sx), "y": int(sy), "button": "left", "clickCount": 1})
    await asyncio.sleep(0.05)
    await session.send("Input.dispatchMouseEvent",
                       {"type": "mouseMoved", "x": int(sx + 10), "y": int(sy + 10), "button": "left", "buttons": 1})
    await asyncio.sleep(0.05)
    await session.send("Input.dispatchMouseEvent",
                       {"type": "mouseMoved", "x": int(tx), "y": int(ty), "button": "left", "buttons": 1})
    await asyncio.sleep(0.05)
    await session.send("Input.dispatchMouseEvent",
                       {"type": "mouseReleased", "x": int(tx), "y": int(ty), "button": "left", "clickCount": 1})
    await asyncio.sleep(0.2)


def print_events(events, label):
    """Show captured browser events, filtering out noisy mousemove."""
    interesting = [e for e in events if e["type"] != "mousemove"]
    if not interesting:
        print(f"  {label} events: (none besides mousemove)")
        return
    for e in interesting:
        drag_tag = " [DRAGGABLE]" if e["draggable"] else ""
        print(f"  {label}: {e['type']:12s} on #{e['targetId']:<6s} ({e['targetTag']}) "
              f"at ({e['x']},{e['y']}){drag_tag}")


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        page = await context.new_page()
        await page.set_content(TEST_HTML)
        await page.wait_for_load_state("domcontentloaded")

        session = await context.new_cdp_session(page)
        await session.send("DOM.enable")

        pieces = [await get_backend_node_id(session, f"#p{i}") for i in range(3)]
        slots = [await get_backend_node_id(session, f"#s{i}") for i in range(3)]
        labels = ["H", "A", "R"]

        # =====================================================================
        print("=" * 72)
        print("TEST 1: Stale coords (current semantic.py behavior)")
        print("=" * 72)
        print()

        for i in range(3):
            await page.evaluate("window.getEvents()")  # clear
            print(f"--- Drag '{labels[i]}' -> Slot {i+1} ---")

            src = await call_on_node(session, pieces[i], VIEWPORT_INFO_JS)
            tgt = await call_on_node(session, slots[i], VIEWPORT_INFO_JS)

            # What's at the stale source coords NOW?
            hit = await page.evaluate(f"""(() => {{
                const el = document.elementFromPoint({src['x']}, {src['y']});
                return el ? {{id: el.id, tag: el.tagName, draggable: el.draggable}} : null;
            }})()""")
            print(f"  stale src ({src['x']:.0f},{src['y']:.0f}) -> "
                  f"#{hit['id']} draggable={hit['draggable']}")

            await cdp_drag(session, src["x"], src["y"], tgt["x"], tgt["y"])

            events = await page.evaluate("window.getEvents()")
            print_events(events, "browser")

            slot_text = await page.evaluate(f"document.getElementById('s{i}').textContent.trim()")
            print(f"  result: \"{slot_text}\" {'FILLED' if slot_text not in (f'Slot {i+1}', '') else 'EMPTY'}")
            print()

        # =====================================================================
        # Reset
        await page.set_content(TEST_HTML)
        await page.wait_for_load_state("domcontentloaded")
        session = await context.new_cdp_session(page)
        await session.send("DOM.enable")
        pieces = [await get_backend_node_id(session, f"#p{i}") for i in range(3)]
        slots = [await get_backend_node_id(session, f"#s{i}") for i in range(3)]

        print("=" * 72)
        print("TEST 2: Fresh coords (re-query source after target scroll)")
        print("=" * 72)
        print()

        for i in range(3):
            await page.evaluate("window.getEvents()")
            print(f"--- Drag '{labels[i]}' -> Slot {i+1} ---")

            # Get source info (for onTop check), then target info
            _src = await call_on_node(session, pieces[i], VIEWPORT_INFO_JS)
            tgt = await call_on_node(session, slots[i], VIEWPORT_INFO_JS)

            # Re-query source position AFTER target scroll
            fresh = await page.evaluate(f"""(() => {{
                const el = document.getElementById('p{i}');
                const rect = el.getBoundingClientRect();
                return {{x: rect.left + rect.width/2, y: rect.top + rect.height/2}};
            }})()""")
            print(f"  fresh src ({fresh['x']:.0f},{fresh['y']:.0f})  tgt ({tgt['x']:.0f},{tgt['y']:.0f})")

            hit = await page.evaluate(f"""(() => {{
                const el = document.elementFromPoint({fresh['x']}, {fresh['y']});
                return el ? {{id: el.id, draggable: el.draggable}} : null;
            }})()""")
            print(f"  fresh src hits -> #{hit['id']} draggable={hit['draggable']}")

            await cdp_drag(session, fresh["x"], fresh["y"], tgt["x"], tgt["y"])

            events = await page.evaluate("window.getEvents()")
            print_events(events, "browser")

            slot_text = await page.evaluate(f"document.getElementById('s{i}').textContent.trim()")
            print(f"  result: \"{slot_text}\" {'FILLED' if slot_text not in (f'Slot {i+1}', '') else 'EMPTY'}")
            print()

        # =====================================================================
        # Reset
        await page.set_content(TEST_HTML)
        await page.wait_for_load_state("domcontentloaded")
        session = await context.new_cdp_session(page)
        await session.send("DOM.enable")
        pieces = [await get_backend_node_id(session, f"#p{i}") for i in range(3)]
        slots = [await get_backend_node_id(session, f"#s{i}") for i in range(3)]

        print("=" * 72)
        print("TEST 3: Playwright native drag_and_drop (control group)")
        print("=" * 72)
        print()

        for i in range(3):
            await page.evaluate("window.getEvents()")
            print(f"--- Drag '{labels[i]}' -> Slot {i+1} ---")

            src_loc = page.locator(f"#p{i}")
            tgt_loc = page.locator(f"#s{i}")
            await src_loc.drag_to(tgt_loc)
            await asyncio.sleep(0.2)

            events = await page.evaluate("window.getEvents()")
            print_events(events, "browser")

            slot_text = await page.evaluate(f"document.getElementById('s{i}').textContent.trim()")
            print(f"  result: \"{slot_text}\" {'FILLED' if slot_text not in (f'Slot {i+1}', '') else 'EMPTY'}")
            print()

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
