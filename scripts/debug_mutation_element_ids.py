"""
Debug script to verify mutation-based element ID resolution.

Tests that when a tool action dynamically adds new interactive elements,
the mutation feedback includes stable element IDs that can be immediately
used for follow-up actions (without waiting for a full snapshot).

Scenarios:
  1. Basic: button/link/input/role-div added, plain div filtered out
  2. Nested: parent container appended with interactive children (one-level deep)
  3. Cap: >10 interactive elements added — only first 10 tracked
  4. Removed-before-collect: element added then removed before collection
  5. Contenteditable / tabindex / onclick / draggable attributes
  6. No interactive additions: only plain text changes — zero overhead
  7. Dedup: _build_change_lines suppresses addedText duplicates
  8. Mixed: interactive + non-interactive + attr changes together
  9. Rapid re-inject: inject→collect→inject→collect cycle works cleanly
"""

import asyncio
import json
import sys

from playwright.async_api import async_playwright

sys.path.insert(0, ".")

from src.agent.tools.semantic import (
    _OBSERVER_INJECT_JS,
    _OBSERVER_COLLECT_JS,
    _build_change_lines,
)
from src.agent.context.snapshot import build_stable_id_from_backend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PASS_COUNT = 0
FAIL_COUNT = 0


def check(label: str, condition: bool) -> bool:
    global PASS_COUNT, FAIL_COUNT
    if condition:
        PASS_COUNT += 1
        print(f"  [PASS] {label}")
    else:
        FAIL_COUNT += 1
        print(f"  [FAIL] {label}")
    return condition


async def inject_and_collect(cdp, page, click_selector, *, settle: float = 0.3):
    """Inject observer, click, settle, collect."""
    await cdp.send("Runtime.evaluate",
                    {"expression": _OBSERVER_INJECT_JS, "returnByValue": True})
    await page.click(click_selector)
    await asyncio.sleep(settle)
    result = await cdp.send("Runtime.evaluate",
                            {"expression": _OBSERVER_COLLECT_JS, "returnByValue": True})
    return result.get("result", {}).get("value")


async def resolve_and_cleanup(cdp, mutations):
    """Resolve backendNodeIds and clean up markers (mirrors _resolve_new_interactive)."""
    resolved = []
    for item in mutations.get("newInteractive", []):
        marker = item["marker"]
        try:
            ev = await cdp.send("Runtime.evaluate", {
                "expression": f'document.querySelector("[data-agent-mut-id=\\"{marker}\\"]")',
                "returnByValue": False,
            })
            oid = ev.get("result", {}).get("objectId")
            if not oid:
                continue
            desc = await cdp.send("DOM.describeNode", {"objectId": oid})
            bid = desc.get("node", {}).get("backendNodeId")
            if not bid:
                continue
            sid = build_stable_id_from_backend(None, int(bid))
            resolved.append({
                "stable_id": sid, "backend_node_id": int(bid),
                "tag": item.get("tag", ""), "role": item.get("role", ""),
                "text": item.get("text", ""), "name": item.get("name", ""),
            })
            await cdp.send("Runtime.evaluate", {
                "expression": (
                    f'document.querySelector("[data-agent-mut-id=\\"{marker}\\"]")'
                    f'?.removeAttribute("data-agent-mut-id")'
                ),
                "returnByValue": True,
            })
        except Exception:
            continue
    return resolved


async def remaining_markers(page) -> int:
    return await page.evaluate('document.querySelectorAll("[data-agent-mut-id]").length')


# ---------------------------------------------------------------------------
# Test pages
# ---------------------------------------------------------------------------

BASIC_PAGE = """<!DOCTYPE html><html><body>
<button id="add-btn" onclick="addElements()">Add</button>
<div id="c"></div>
<script>
let n=0;
function addElements(){
  const c=document.getElementById('c');
  const btn=document.createElement('button'); btn.textContent='Btn '+(++n); c.appendChild(btn);
  const a=document.createElement('a'); a.href='#'; a.textContent='Link '+n; c.appendChild(a);
  const inp=document.createElement('input'); inp.placeholder='Input '+n; c.appendChild(inp);
  const rd=document.createElement('div'); rd.setAttribute('role','button'); rd.textContent='RoleBtn '+n; c.appendChild(rd);
  const p=document.createElement('div'); p.textContent='Plain '+n; c.appendChild(p);
}
</script></body></html>"""

NESTED_PAGE = """<!DOCTYPE html><html><body>
<button id="add-nested" onclick="addNested()">Add</button>
<div id="c"></div>
<script>
function addNested(){
  const c=document.getElementById('c');
  const w=document.createElement('div');
  const b1=document.createElement('button'); b1.textContent='Child1'; w.appendChild(b1);
  const a1=document.createElement('a'); a1.href='#'; a1.textContent='Child2'; w.appendChild(a1);
  const s1=document.createElement('span'); s1.textContent='PlainChild'; w.appendChild(s1);
  c.appendChild(w);
}
</script></body></html>"""

CAP_PAGE = """<!DOCTYPE html><html><body>
<button id="add-many" onclick="addMany()">Add</button>
<div id="c"></div>
<script>
function addMany(){
  const c=document.getElementById('c');
  for(let i=0;i<15;i++){
    const b=document.createElement('button'); b.textContent='B'+i; c.appendChild(b);
  }
}
</script></body></html>"""

REMOVED_PAGE = """<!DOCTYPE html><html><body>
<button id="add-remove" onclick="addRemove()">Add</button>
<div id="c"></div>
<script>
function addRemove(){
  const c=document.getElementById('c');
  const b=document.createElement('button'); b.textContent='Ephemeral'; c.appendChild(b);
  b.remove();
}
</script></body></html>"""

ATTR_PAGE = """<!DOCTYPE html><html><body>
<button id="add-attr" onclick="addAttr()">Add</button>
<div id="c"></div>
<script>
function addAttr(){
  const c=document.getElementById('c');
  const ce=document.createElement('div'); ce.setAttribute('contenteditable','true'); ce.textContent='CE'; c.appendChild(ce);
  const ti=document.createElement('div'); ti.setAttribute('tabindex','0'); ti.textContent='Tab'; c.appendChild(ti);
  const oc=document.createElement('div'); oc.setAttribute('onclick','void(0)'); oc.textContent='OnClick'; c.appendChild(oc);
  const dr=document.createElement('div'); dr.setAttribute('draggable','true'); dr.textContent='Drag'; c.appendChild(dr);
  const pl=document.createElement('div'); pl.textContent='PlainAttr'; c.appendChild(pl);
}
</script></body></html>"""

NO_INTERACTIVE_PAGE = """<!DOCTYPE html><html><body>
<button id="add-text" onclick="addText()">Add</button>
<div id="c"></div>
<script>
function addText(){
  const c=document.getElementById('c');
  const s=document.createElement('span'); s.textContent='Just some text'; c.appendChild(s);
  const d=document.createElement('div'); d.textContent='More plain text'; c.appendChild(d);
}
</script></body></html>"""

MIXED_PAGE = """<!DOCTYPE html><html><body>
<button id="toggle" onclick="toggle()">Toggle</button>
<div id="target" aria-expanded="false">Target</div>
<div id="c"></div>
<script>
function toggle(){
  const t=document.getElementById('target');
  t.setAttribute('aria-expanded', t.getAttribute('aria-expanded')==='false'?'true':'false');
  const c=document.getElementById('c');
  const b=document.createElement('button'); b.textContent='NewBtn'; c.appendChild(b);
  const s=document.createElement('span'); s.textContent='InfoText'; c.appendChild(s);
}
</script></body></html>"""

REINJECT_PAGE = """<!DOCTYPE html><html><body>
<button id="step1" onclick="step1()">Step1</button>
<button id="step2" onclick="step2()">Step2</button>
<div id="c"></div>
<script>
function step1(){
  const c=document.getElementById('c');
  const b=document.createElement('button'); b.textContent='FromStep1'; c.appendChild(b);
}
function step2(){
  const c=document.getElementById('c');
  const b=document.createElement('button'); b.textContent='FromStep2'; c.appendChild(b);
}
</script></body></html>"""


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

async def test_basic(pw):
    print(f"\n{'='*60}\nTEST: Basic interactive element detection\n{'='*60}")
    browser = await pw.chromium.launch(headless=True)
    page = await browser.new_page()
    await page.set_content(BASIC_PAGE)
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("DOM.enable")
    await cdp.send("Runtime.enable")

    mutations = await inject_and_collect(cdp, page, "#add-btn")
    ni = mutations.get("newInteractive", [])
    resolved = await resolve_and_cleanup(cdp, mutations)
    rm = await remaining_markers(page)

    check("4 interactive elements detected", len(ni) == 4)
    check("No plain div (no role)", all(
        not (i["tag"] == "div" and not i["role"]) for i in ni))
    check("Has role=button", any(i["role"] == "button" for i in ni))
    check("Resolved 4 IDs", len(resolved) == 4)
    check("All IDs are el_ format", all(
        r["stable_id"].startswith("el_") and len(r["stable_id"]) == 15 for r in resolved))
    check("All IDs unique", len(set(r["stable_id"] for r in resolved)) == 4)
    check("Markers cleaned up", rm == 0)

    await browser.close()


async def test_nested(pw):
    print(f"\n{'='*60}\nTEST: Nested children (one-level deep)\n{'='*60}")
    browser = await pw.chromium.launch(headless=True)
    page = await browser.new_page()
    await page.set_content(NESTED_PAGE)
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("DOM.enable")
    await cdp.send("Runtime.enable")

    mutations = await inject_and_collect(cdp, page, "#add-nested")
    ni = mutations.get("newInteractive", [])
    resolved = await resolve_and_cleanup(cdp, mutations)
    rm = await remaining_markers(page)

    check("Button child detected", any(i["tag"] == "button" for i in ni))
    check("Link child detected", any(i["tag"] == "a" for i in ni))
    check("Plain span NOT detected", not any(
        i["tag"] == "span" and not i["role"] for i in ni))
    check("Resolved >=2 IDs", len(resolved) >= 2)
    check("Markers cleaned up", rm == 0)

    await browser.close()


async def test_cap(pw):
    print(f"\n{'='*60}\nTEST: Cap at 10 interactive elements\n{'='*60}")
    browser = await pw.chromium.launch(headless=True)
    page = await browser.new_page()
    await page.set_content(CAP_PAGE)
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("DOM.enable")
    await cdp.send("Runtime.enable")

    mutations = await inject_and_collect(cdp, page, "#add-many")
    ni = mutations.get("newInteractive", [])
    resolved = await resolve_and_cleanup(cdp, mutations)
    rm = await remaining_markers(page)

    check("Exactly 10 tracked (capped)", len(ni) == 10)
    check("Resolved 10 IDs", len(resolved) == 10)
    check("Markers cleaned up", rm == 0)

    # Verify 15 buttons actually exist in DOM
    total = await page.evaluate('document.querySelectorAll("#c button").length')
    check("All 15 buttons exist in DOM", total == 15)

    await browser.close()


async def test_removed_before_collect(pw):
    print(f"\n{'='*60}\nTEST: Element removed before collection\n{'='*60}")
    browser = await pw.chromium.launch(headless=True)
    page = await browser.new_page()
    await page.set_content(REMOVED_PAGE)
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("DOM.enable")
    await cdp.send("Runtime.enable")

    mutations = await inject_and_collect(cdp, page, "#add-remove")
    ni = mutations.get("newInteractive", [])
    rm = await remaining_markers(page)

    # The node was pushed to interactiveNodes but then removed before collect.
    # The collect JS checks document.contains() and skips it.
    check("Removed element filtered out", len(ni) == 0)
    check("No leftover markers", rm == 0)

    await browser.close()


async def test_attribute_interactivity(pw):
    print(f"\n{'='*60}\nTEST: Attribute-based interactivity (contenteditable/tabindex/onclick/draggable)\n{'='*60}")
    browser = await pw.chromium.launch(headless=True)
    page = await browser.new_page()
    await page.set_content(ATTR_PAGE)
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("DOM.enable")
    await cdp.send("Runtime.enable")

    mutations = await inject_and_collect(cdp, page, "#add-attr")
    ni = mutations.get("newInteractive", [])
    texts = {i["text"] for i in ni}
    resolved = await resolve_and_cleanup(cdp, mutations)
    rm = await remaining_markers(page)

    check("contenteditable detected", "CE" in texts)
    check("tabindex detected", "Tab" in texts)
    check("onclick detected", "OnClick" in texts)
    check("draggable detected", "Drag" in texts)
    check("Plain div NOT detected", "PlainAttr" not in texts)
    check("4 interactive elements", len(ni) == 4)
    check("Resolved 4 IDs", len(resolved) == 4)
    check("Markers cleaned up", rm == 0)

    await browser.close()


async def test_no_interactive(pw):
    print(f"\n{'='*60}\nTEST: No interactive additions (zero overhead)\n{'='*60}")
    browser = await pw.chromium.launch(headless=True)
    page = await browser.new_page()
    await page.set_content(NO_INTERACTIVE_PAGE)
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("DOM.enable")
    await cdp.send("Runtime.enable")

    mutations = await inject_and_collect(cdp, page, "#add-text")
    ni = mutations.get("newInteractive", [])
    rm = await remaining_markers(page)

    check("Zero interactive tracked", len(ni) == 0)
    check("addedText still captured", len(mutations.get("addedText", [])) >= 1)
    check("No markers placed", rm == 0)

    await browser.close()


async def test_dedup_formatting(pw):
    print(f"\n{'='*60}\nTEST: _build_change_lines dedup (no duplicates)\n{'='*60}")

    # Simulate a mutations dict with overlapping addedText and resolvedInteractive
    mutations = {
        "addedText": [
            {"t": "Dynamic Button", "tag": "button"},
            {"t": "Plain Info", "tag": "span"},
            {"t": "Role Btn", "tag": "div"},
        ],
        "resolvedInteractive": [
            {"stable_id": "el_aaa111222333", "tag": "button", "role": "",
             "text": "Dynamic Button", "name": ""},
            {"stable_id": "el_bbb444555666", "tag": "div", "role": "button",
             "text": "Role Btn", "name": ""},
        ],
        "removedText": [],
        "attrChanges": [],
        "startUrl": "http://x",
        "currentUrl": "http://x",
    }
    lines = _build_change_lines(mutations)
    text = "\n".join(lines)
    print(f"  Output:\n{text}")

    # "Dynamic Button" (button) should NOT appear as plain addedText
    check('"Dynamic Button" not in plain + lines',
          '+ "Dynamic Button" (button)' not in text)
    # "Role Btn" (div) should NOT appear as plain addedText
    check('"Role Btn" not in plain + lines',
          '+ "Role Btn" (div)' not in text)
    # "Plain Info" should still appear
    check('"Plain Info" still in + lines',
          '+ "Plain Info" (span)' in text)
    # Interactive lines present
    check('+ interactive el_aaa for button',
          '+ interactive el_aaa111222333: button "Dynamic Button"' in text)
    check('+ interactive el_bbb for div role=button',
          '+ interactive el_bbb444555666: div "button"' in text)
    # Spacing: "+ interactive" not "+interactive"
    check('Space after + in interactive lines',
          "+interactive" not in text)


async def test_mixed(pw):
    print(f"\n{'='*60}\nTEST: Mixed (interactive + text + attr changes)\n{'='*60}")
    browser = await pw.chromium.launch(headless=True)
    page = await browser.new_page()
    await page.set_content(MIXED_PAGE)
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("DOM.enable")
    await cdp.send("Runtime.enable")

    mutations = await inject_and_collect(cdp, page, "#toggle")
    ni = mutations.get("newInteractive", [])
    resolved = await resolve_and_cleanup(cdp, mutations)
    rm = await remaining_markers(page)

    check("1 interactive button detected", len(ni) == 1 and ni[0]["tag"] == "button")
    check("attr change captured", len(mutations.get("attrChanges", [])) >= 1)
    check("addedText has InfoText", any(
        (i.get("t") or "") == "InfoText" for i in mutations.get("addedText", [])))
    check("Resolved 1 ID", len(resolved) == 1)
    check("Markers cleaned up", rm == 0)

    # Format and check dedup
    mutations["resolvedInteractive"] = resolved
    lines = _build_change_lines(mutations)
    text = "\n".join(lines)
    print(f"  Formatted output:\n{text}")

    check("Interactive line present", "+ interactive" in text)
    check("Attr change line present", "~" in text)
    check("InfoText in plain + line", '"InfoText"' in text)

    await browser.close()


async def test_reinject_cycle(pw):
    print(f"\n{'='*60}\nTEST: Re-inject cycle (inject->collect->inject->collect)\n{'='*60}")
    browser = await pw.chromium.launch(headless=True)
    page = await browser.new_page()
    await page.set_content(REINJECT_PAGE)
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("DOM.enable")
    await cdp.send("Runtime.enable")

    # Cycle 1
    m1 = await inject_and_collect(cdp, page, "#step1")
    ni1 = m1.get("newInteractive", [])
    r1 = await resolve_and_cleanup(cdp, m1)
    rm1 = await remaining_markers(page)

    check("Cycle1: 1 interactive", len(ni1) == 1)
    check("Cycle1: text=FromStep1", ni1[0]["text"] == "FromStep1")
    check("Cycle1: resolved 1 ID", len(r1) == 1)
    check("Cycle1: markers cleaned", rm1 == 0)

    # Cycle 2 — fresh observer on same page
    m2 = await inject_and_collect(cdp, page, "#step2")
    ni2 = m2.get("newInteractive", [])
    r2 = await resolve_and_cleanup(cdp, m2)
    rm2 = await remaining_markers(page)

    check("Cycle2: 1 interactive", len(ni2) == 1)
    check("Cycle2: text=FromStep2", ni2[0]["text"] == "FromStep2")
    check("Cycle2: resolved 1 ID", len(r2) == 1)
    check("Cycle2: different ID from cycle1", r2[0]["stable_id"] != r1[0]["stable_id"])
    check("Cycle2: markers cleaned", rm2 == 0)

    # No leftover __mutObs
    leftover = await page.evaluate("!!window.__mutObs")
    check("No leftover __mutObs on window", not leftover)

    await browser.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    async with async_playwright() as pw:
        await test_basic(pw)
        await test_nested(pw)
        await test_cap(pw)
        await test_removed_before_collect(pw)
        await test_attribute_interactivity(pw)
        await test_no_interactive(pw)
        await test_dedup_formatting(pw)
        await test_mixed(pw)
        await test_reinject_cycle(pw)

    print(f"\n{'='*60}")
    print(f"SUMMARY: {PASS_COUNT} passed, {FAIL_COUNT} failed")
    print(f"{'='*60}")
    if FAIL_COUNT:
        sys.exit(1)
    else:
        print("All tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
