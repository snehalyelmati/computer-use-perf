"""Debug script to verify that _SCROLL_ELEMENT_JS requires a regular function (not arrow)
for correct `this` binding via CDP Runtime.callFunctionOn.

Tests both the old arrow function (broken) and new regular function (fixed).
"""

import asyncio

from playwright.async_api import async_playwright

HTML = """
<!DOCTYPE html>
<html>
<body style="margin:0; padding:20px;">
  <h2>Scroll Test</h2>
  <div id="scroller" style="width:300px; height:200px; overflow-y:scroll; border:2px solid blue;">
    <div style="height:1500px; background:linear-gradient(to bottom, #eee, #333);">
      Tall inner content — scroll me!
    </div>
  </div>
</body>
</html>
"""

# Old arrow function (broken — `this` is undefined)
ARROW_FN = """
([dx, dy]) => {
    function canScroll(el) {
        const style = getComputedStyle(el);
        const oy = style.overflowY;
        const canY = dy !== 0 && (oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight + 1;
        return canY;
    }
    let target = this;
    while (target && target !== document.documentElement) {
        if (canScroll(target)) break;
        target = target.parentElement;
    }
    const root = document.scrollingElement || document.documentElement;
    if (!target || target === document.documentElement) {
        target = root;
    }
    const useWindow = target === root;
    const before = {
        x: useWindow ? window.scrollX : target.scrollLeft,
        y: useWindow ? window.scrollY : target.scrollTop,
    };
    if (useWindow) { window.scrollBy(dx, dy); } else { target.scrollBy(dx, dy); }
    const after = {
        x: useWindow ? window.scrollX : target.scrollLeft,
        y: useWindow ? window.scrollY : target.scrollTop,
    };
    const tag = useWindow ? 'window' : (target.tagName || 'element');
    return { before, after, targetTag: tag };
}
""".strip()

# New regular function (fixed — `this` is bound to the target element)
REGULAR_FN = """
function ([dx, dy]) {
    function canScroll(el) {
        const style = getComputedStyle(el);
        const oy = style.overflowY;
        const canY = dy !== 0 && (oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight + 1;
        return canY;
    }
    let target = this;
    while (target && target !== document.documentElement) {
        if (canScroll(target)) break;
        target = target.parentElement;
    }
    const root = document.scrollingElement || document.documentElement;
    if (!target || target === document.documentElement) {
        target = root;
    }
    const useWindow = target === root;
    const before = {
        x: useWindow ? window.scrollX : target.scrollLeft,
        y: useWindow ? window.scrollY : target.scrollTop,
    };
    if (useWindow) { window.scrollBy(dx, dy); } else { target.scrollBy(dx, dy); }
    const after = {
        x: useWindow ? window.scrollX : target.scrollLeft,
        y: useWindow ? window.scrollY : target.scrollTop,
    };
    const tag = useWindow ? 'window' : (target.tagName || 'element');
    return { before, after, targetTag: tag };
}
""".strip()


async def call_on_node(session, backend_node_id: int, fn: str, args: list) -> dict | None:
    """Resolve a backendNodeId and call a function on it via CDP."""
    try:
        resolve = await session.send(
            "DOM.resolveNode", {"backendNodeId": backend_node_id}
        )
        object_id = resolve["object"]["objectId"]
    except Exception as e:
        print(f"  resolveNode failed: {e}")
        return None
    try:
        result = await session.send(
            "Runtime.callFunctionOn",
            {
                "functionDeclaration": fn,
                "objectId": object_id,
                "arguments": args,
                "returnByValue": True,
            },
        )
        return result
    except Exception as e:
        print(f"  callFunctionOn failed: {e}")
        return None


async def get_backend_node_id(session, selector: str) -> int:
    """Get the backendNodeId for a CSS selector."""
    doc = await session.send("DOM.getDocument", {"depth": 0})
    root_id = doc["root"]["nodeId"]
    result = await session.send(
        "DOM.querySelector", {"nodeId": root_id, "selector": selector}
    )
    node_id = result["nodeId"]
    desc = await session.send("DOM.describeNode", {"nodeId": node_id})
    return desc["node"]["backendNodeId"]


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(HTML)
        await page.wait_for_load_state("domcontentloaded")

        session = await page.context.new_cdp_session(page)
        await session.send("DOM.enable")

        backend_id = await get_backend_node_id(session, "#scroller")
        print(f"Scroller backendNodeId: {backend_id}\n")

        args = [{"value": [0, 100]}]

        # --- Test 1: Arrow function (BROKEN) ---
        print("=== Test 1: Arrow function (old, broken) ===")
        result = await call_on_node(session, backend_id, ARROW_FN, args)
        if result:
            value = result.get("result", {}).get("value")
            print(f"  Raw result type: {result.get('result', {}).get('type')}")
            print(f"  Value: {value}")
            if value and isinstance(value, dict):
                tag = value.get("targetTag", "?")
                before_y = value["before"]["y"]
                after_y = value["after"]["y"]
                print(f"  targetTag={tag}, before.y={before_y}, after.y={after_y}")
                if tag == "window":
                    print("  BUG CONFIRMED: arrow function scrolled WINDOW instead of the element!")
                elif after_y == before_y:
                    print("  BUG CONFIRMED: scroll position didn't change!")
            else:
                print("  BUG CONFIRMED: no value returned (this was undefined)")
        else:
            print("  BUG CONFIRMED: callFunctionOn returned nothing")

        # Reset scroll position
        await page.evaluate("document.getElementById('scroller').scrollTop = 0")

        # --- Test 2: Regular function (FIXED) ---
        print("\n=== Test 2: Regular function (new, fixed) ===")
        result = await call_on_node(session, backend_id, REGULAR_FN, args)
        if result:
            value = result.get("result", {}).get("value")
            print(f"  Raw result type: {result.get('result', {}).get('type')}")
            print(f"  Value: {value}")
            if value and isinstance(value, dict):
                tag = value.get("targetTag", "?")
                before_y = value["before"]["y"]
                after_y = value["after"]["y"]
                print(f"  targetTag={tag}, before.y={before_y}, after.y={after_y}")
                if tag == "DIV" and after_y > before_y:
                    print("  FIX CONFIRMED: regular function scrolled the ELEMENT correctly!")
                else:
                    print(f"  UNEXPECTED: tag={tag}, delta_y={after_y - before_y}")
            else:
                print("  FAIL: no value returned")
        else:
            print("  FAIL: callFunctionOn returned nothing")

        # --- Summary ---
        print("\n=== Summary ===")

        # Re-run arrow to check
        await page.evaluate("document.getElementById('scroller').scrollTop = 0")
        arrow_result = await call_on_node(session, backend_id, ARROW_FN, args)
        arrow_value = arrow_result.get("result", {}).get("value") if arrow_result else None

        await page.evaluate("document.getElementById('scroller').scrollTop = 0")
        reg_result = await call_on_node(session, backend_id, REGULAR_FN, args)
        reg_value = reg_result.get("result", {}).get("value") if reg_result else None

        arrow_ok = (
            isinstance(arrow_value, dict)
            and arrow_value.get("targetTag") not in ("window", None)
            and arrow_value.get("after", {}).get("y", 0) > arrow_value.get("before", {}).get("y", 0)
        )
        reg_ok = (
            isinstance(reg_value, dict)
            and reg_value.get("targetTag") not in ("window", None)
            and reg_value.get("after", {}).get("y", 0) > reg_value.get("before", {}).get("y", 0)
        )

        print(f"  Arrow fn scrolled element correctly: {arrow_ok}  (expected: False)")
        print(f"  Regular fn scrolled element correctly: {reg_ok}  (expected: True)")

        assert not arrow_ok, "Arrow function should NOT correctly scroll the element"
        assert reg_ok, "Regular function SHOULD correctly scroll the element"

        print("\nAll assertions passed!")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
