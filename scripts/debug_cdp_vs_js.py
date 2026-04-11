"""Debug: compare CDP DOMSnapshot node enumeration vs JS querySelectorAll.

Runs handler extraction (stamps data-agent-hid), then captures CDP snapshot
to see which stamped elements CDP can and cannot see.
"""

import asyncio
import json

from playwright.async_api import async_playwright

TARGET_URL = "https://serene-frangipane-7fd25b.netlify.app/"

# Stamp elements with data-agent-hid using the same approach as handler extraction
STAMP_JS = r"""
(() => {
  const REACT_EVENTS = [
    'onClick', 'onChange', 'onInput', 'onSubmit', 'onKeyDown', 'onKeyUp',
    'onKeyPress', 'onFocus', 'onBlur', 'onMouseDown', 'onMouseUp', 'onMouseOver',
    'onDoubleClick', 'onContextMenu',
    'onDragStart', 'onDragOver', 'onDrop', 'onDragEnter',
    'onMouseEnter', 'onMouseLeave',
  ];
  const INLINE_EVENTS = [
    'onclick', 'onchange', 'oninput', 'onsubmit', 'onkeydown', 'onkeyup',
    'onkeypress', 'onfocus', 'onblur', 'onmousedown', 'onmouseup', 'onmouseover',
    'ondblclick', 'oncontextmenu',
    'ondragstart', 'ondragover', 'ondrop', 'ondragenter',
    'onmouseenter', 'onmouseleave',
  ];
  let hid = 0;
  const stamped = [];
  for (const el of document.querySelectorAll('*')) {
    let hasHandler = false;
    // Check inline
    for (const attr of INLINE_EVENTS) {
      if (el[attr] && typeof el[attr] === 'function') { hasHandler = true; break; }
    }
    // Check React props
    if (!hasHandler) {
      for (const key of Object.keys(el)) {
        if (key.startsWith('__reactProps$')) {
          const props = el[key];
          if (props) {
            for (const ev of REACT_EVENTS) {
              if (props[ev] && typeof props[ev] === 'function') { hasHandler = true; break; }
            }
          }
        }
        if (hasHandler) break;
      }
    }
    if (hasHandler) {
      el.setAttribute('data-agent-hid', String(hid));
      stamped.push({
        hid: hid,
        tag: el.tagName,
        text: (el.textContent || '').trim().substring(0, 40),
        classes: el.className ? String(el.className).substring(0, 50) : '',
      });
      hid++;
    }
  }
  return stamped;
})()
"""

CLEANUP_JS = r"""
(() => {
  for (const el of document.querySelectorAll('[data-agent-hid]')) {
    el.removeAttribute('data-agent-hid');
  }
})()
"""


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        page = await context.new_page()

        # Get CDP session
        cdp = await context.new_cdp_session(page)

        await page.goto(TARGET_URL, wait_until="networkidle")
        await page.wait_for_timeout(2000)
        await page.click("button:has-text('START')")
        await page.wait_for_timeout(3000)

        print(f"URL: {page.url}")

        # Step 1: Stamp elements with data-agent-hid via JS
        js_stamped = await page.evaluate(STAMP_JS)
        print(f"\nJS stamped {len(js_stamped)} elements with data-agent-hid")

        # Step 2: Capture CDP DOMSnapshot
        dom_snapshot = await cdp.send(
            "DOMSnapshot.captureSnapshot",
            {
                "computedStyles": ["cursor"],
                "includeDOMRects": True,
                "includePaintOrder": True,
            },
        )

        # Parse the CDP snapshot to find elements with data-agent-hid
        documents = dom_snapshot.get("documents", [])
        strings = dom_snapshot.get("strings", [])

        print(f"\nCDP snapshot: {len(documents)} document(s), {len(strings)} strings")

        for doc_idx, doc in enumerate(documents):
            nodes = doc.get("nodes", {})
            node_names = nodes.get("nodeName", [])
            node_types = nodes.get("nodeType", [])
            attributes_arr = nodes.get("attributes", [])
            node_count = len(node_names)

            print(f"\n  Document {doc_idx}: {node_count} nodes")

            # Count node types
            type_counts = {}
            for nt in node_types:
                type_counts[nt] = type_counts.get(nt, 0) + 1
            print(f"  Node types: {type_counts}")
            print(f"    1=Element, 3=Text, 8=Comment, 9=Document, 10=DocumentType")

            element_count = type_counts.get(1, 0)
            print(f"  Element nodes: {element_count}")

            # Find elements with data-agent-hid in CDP snapshot
            cdp_hids = {}
            cdp_hid_missing = []

            for idx in range(node_count):
                if idx >= len(attributes_arr):
                    continue
                attrs = attributes_arr[idx]
                # attrs is a flat array of [name_idx, value_idx, name_idx, value_idx, ...]
                hid_value = None
                for i in range(0, len(attrs), 2):
                    name_idx = attrs[i]
                    value_idx = attrs[i + 1]
                    name = strings[name_idx] if name_idx < len(strings) else "?"
                    if name == "data-agent-hid":
                        hid_value = strings[value_idx] if value_idx < len(strings) else "?"
                        break

                if hid_value is not None:
                    node_name = strings[node_names[idx]] if node_names[idx] < len(strings) else "?"
                    node_type = node_types[idx] if idx < len(node_types) else -1
                    cdp_hids[hid_value] = {
                        "cdp_index": idx,
                        "node_name": node_name,
                        "node_type": node_type,
                    }

            print(f"\n  CDP found {len(cdp_hids)} elements with data-agent-hid")

            # Compare JS vs CDP
            js_hid_set = {str(s["hid"]) for s in js_stamped}
            cdp_hid_set = set(cdp_hids.keys())

            in_js_not_cdp = js_hid_set - cdp_hid_set
            in_cdp_not_js = cdp_hid_set - js_hid_set

            print(f"\n  JS stamped: {len(js_hid_set)}")
            print(f"  CDP sees:   {len(cdp_hid_set)}")
            print(f"  In JS but NOT in CDP: {len(in_js_not_cdp)}")
            print(f"  In CDP but NOT in JS: {len(in_cdp_not_js)}")

            if in_js_not_cdp:
                print(f"\n  --- Elements MISSING from CDP snapshot ---")
                for hid_str in sorted(in_js_not_cdp, key=int):
                    js_el = next(s for s in js_stamped if str(s["hid"]) == hid_str)
                    print(f"    hid={hid_str}: <{js_el['tag']}> \"{js_el['text'][:30]}\" "
                          f"classes=\"{js_el['classes'][:40]}\"")

            # Now check: how many elements would pass _interactive_reason or
            # _should_include_non_interactive in the snapshot code?
            # Simulate the snapshot logic
            INTERACTIVE_TAGS = {"A", "BUTTON", "INPUT", "SELECT", "TEXTAREA", "OPTION", "IFRAME"}
            INTERACTIVE_ROLES = {
                "button", "checkbox", "combobox", "link", "menuitem", "option",
                "radio", "slider", "spinbutton", "switch", "tab", "textbox",
            }

            interactive_count = 0
            rescued_by_hid = 0
            rescued_by_cursor = 0
            missed = 0

            # Get AX tree for role lookup
            ax_tree = doc.get("layout", {})

            for idx in range(node_count):
                node_type = node_types[idx] if idx < len(node_types) else -1
                if node_type != 1:  # Skip non-elements
                    continue

                node_name = strings[node_names[idx]] if node_names[idx] < len(strings) else ""
                attrs = attributes_arr[idx] if idx < len(attributes_arr) else []

                # Build attr dict
                attr_dict = {}
                for i in range(0, len(attrs), 2):
                    name = strings[attrs[i]] if attrs[i] < len(strings) else ""
                    value = strings[attrs[i + 1]] if attrs[i + 1] < len(strings) else ""
                    attr_dict[name] = value

                has_hid = "data-agent-hid" in attr_dict

                # Check interactive
                is_interactive = False
                if node_name.upper() in INTERACTIVE_TAGS:
                    is_interactive = True
                    interactive_count += 1
                elif has_hid:
                    rescued_by_hid += 1
                    is_interactive = True

                # Check cursor
                computed = doc.get("nodes", {}).get("computedStyles", [])
                cursor = None
                if idx < len(computed) and computed[idx]:
                    cursor_idx = computed[idx][0] if computed[idx] else -1
                    if cursor_idx >= 0 and cursor_idx < len(strings):
                        cursor = strings[cursor_idx]

                if not is_interactive and cursor == "pointer":
                    rescued_by_cursor += 1
                    is_interactive = True

            print(f"\n  --- Simulated snapshot inclusion ---")
            print(f"  Interactive by tag: {interactive_count}")
            print(f"  Rescued by data-agent-hid: {rescued_by_hid}")
            print(f"  Rescued by cursor:pointer: {rescued_by_cursor}")
            print(f"  Total would-be-included: {interactive_count + rescued_by_hid + rescued_by_cursor}")

        # Clean up
        await page.evaluate(CLEANUP_JS)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
