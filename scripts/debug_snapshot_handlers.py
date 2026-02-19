"""Debug: compare OLD vs NEW handler extraction and their effect on snapshot.

Loads the challenge page step4 and compares:
1. Which elements get data-agent-hid with OLD extraction JS
2. Which elements get data-agent-hid with NEW extraction JS
3. Whether the cursor-pointer challenge div gets captured in both cases
"""

import asyncio
import sys

from playwright.async_api import async_playwright

TARGET_URL = "https://serene-frangipane-7fd25b.netlify.app/"

MAX_LEN = 120

# OLD extraction JS (before our changes - inline first, no fiber, no bestHandler)
OLD_EXTRACT_JS = r"""
(() => {
  const MAX_LEN = 120;
  const INLINE_EVENTS = [
    'onclick', 'onchange', 'oninput', 'onsubmit', 'onkeydown', 'onkeyup',
    'onkeypress', 'onfocus', 'onblur', 'onmousedown', 'onmouseup', 'onmouseover',
    'ondblclick', 'oncontextmenu',
    'ondragstart', 'ondragover', 'ondrop', 'ondragenter',
    'onmouseenter', 'onmouseleave',
  ];
  const REACT_EVENTS = [
    'onClick', 'onChange', 'onInput', 'onSubmit', 'onKeyDown', 'onKeyUp',
    'onKeyPress', 'onFocus', 'onBlur', 'onMouseDown', 'onMouseUp', 'onMouseOver',
    'onDoubleClick', 'onContextMenu',
    'onDragStart', 'onDragOver', 'onDrop', 'onDragEnter',
    'onMouseEnter', 'onMouseLeave',
  ];
  function truncate(src) {
    if (!src) return null;
    let s = String(src).trim().replace(/\s+/g, ' ');
    if (s.length > MAX_LEN) s = s.substring(0, MAX_LEN) + '...';
    return s;
  }
  function handlerSummary(fn) {
    if (!fn || typeof fn !== 'function') return null;
    let src;
    try { src = fn.toString(); } catch(e) { return null; }
    if (!src) return null;
    return truncate(src);
  }
  const result = {};
  let hid = 0;
  const allElements = document.querySelectorAll('*');
  for (const el of allElements) {
    const handlers = {};
    // 1. Inline handlers (onclick, onchange, etc.) — FIRST in old code
    for (const attr of INLINE_EVENTS) {
      const fn = el[attr];
      if (fn && typeof fn === 'function') {
        const eventName = attr.substring(2);
        const summary = handlerSummary(fn);
        if (summary) handlers[eventName] = summary;
      }
    }
    // 2. React props
    try {
      const keys = Object.keys(el);
      for (const key of keys) {
        if (key.startsWith('__reactProps$') || key.startsWith('__reactProps')) {
          const props = el[key];
          if (props && typeof props === 'object') {
            for (const rEvent of REACT_EVENTS) {
              if (props[rEvent] && typeof props[rEvent] === 'function') {
                const eventName = rEvent.substring(2).toLowerCase();
                if (!handlers[eventName]) {
                  const summary = handlerSummary(props[rEvent]);
                  if (summary) handlers[eventName] = summary;
                }
              }
            }
          }
        }
      }
    } catch(e) {}
    if (Object.keys(handlers).length > 0) {
      const id = String(hid);
      el.setAttribute('data-agent-hid', id);
      result[id] = handlers;
      hid++;
    }
  }
  return result;
})()
"""

# NEW extraction JS (after our changes - React first, fiber path, bestHandler)
NEW_EXTRACT_JS = r"""
(() => {
  const MAX_LEN = 120;
  const INLINE_EVENTS = [
    'onclick', 'onchange', 'oninput', 'onsubmit', 'onkeydown', 'onkeyup',
    'onkeypress', 'onfocus', 'onblur', 'onmousedown', 'onmouseup', 'onmouseover',
    'ondblclick', 'oncontextmenu',
    'ondragstart', 'ondragover', 'ondrop', 'ondragenter',
    'onmouseenter', 'onmouseleave',
  ];
  const REACT_EVENTS = [
    'onClick', 'onChange', 'onInput', 'onSubmit', 'onKeyDown', 'onKeyUp',
    'onKeyPress', 'onFocus', 'onBlur', 'onMouseDown', 'onMouseUp', 'onMouseOver',
    'onDoubleClick', 'onContextMenu',
    'onDragStart', 'onDragOver', 'onDrop', 'onDragEnter',
    'onMouseEnter', 'onMouseLeave',
  ];
  function truncate(src) {
    if (!src) return null;
    let s = String(src).trim().replace(/\s+/g, ' ');
    if (s.length > MAX_LEN) s = s.substring(0, MAX_LEN) + '...';
    return s;
  }
  function handlerSummary(fn) {
    if (!fn || typeof fn !== 'function') return null;
    let src;
    try { src = fn.toString(); } catch(e) { return null; }
    if (!src) return null;
    const t = truncate(src);
    const name = fn.name;
    if (name && name.length > 2 && !/^[_$][a-zA-Z]?$/.test(name)
        && !t.startsWith('function ' + name)) {
      return truncate(name + '() ' + src);
    }
    return t;
  }
  function isOpaque(s) {
    if (!s) return true;
    if (/^function\s*[_$a-zA-Z]{0,2}\s*\(/.test(s) && s.length < 40) return true;
    if (s.includes('[native code]')) return true;
    return false;
  }
  function bestHandler(existing, candidate) {
    if (!candidate) return existing;
    if (!existing) return candidate;
    if (isOpaque(existing) && !isOpaque(candidate)) return candidate;
    if (!isOpaque(existing) && isOpaque(candidate)) return existing;
    return candidate.length > existing.length ? candidate : existing;
  }
  const result = {};
  let hid = 0;
  const allElements = document.querySelectorAll('*');
  for (const el of allElements) {
    const handlers = {};
    // 1. React props + fiber first
    try {
      const keys = Object.keys(el);
      for (const key of keys) {
        if (key.startsWith('__reactProps$') || key.startsWith('__reactProps')) {
          const props = el[key];
          if (props && typeof props === 'object') {
            for (const rEvent of REACT_EVENTS) {
              if (props[rEvent] && typeof props[rEvent] === 'function') {
                const eventName = rEvent.substring(2).toLowerCase();
                const summary = handlerSummary(props[rEvent]);
                if (summary) handlers[eventName] = bestHandler(handlers[eventName], summary);
              }
            }
          }
        }
        if (key.startsWith('__reactFiber$') || key.startsWith('__reactInternalInstance$')) {
          try {
            const fiber = el[key];
            if (fiber && fiber.memoizedProps) {
              for (const rEvent of REACT_EVENTS) {
                if (fiber.memoizedProps[rEvent] && typeof fiber.memoizedProps[rEvent] === 'function') {
                  const eventName = rEvent.substring(2).toLowerCase();
                  const summary = handlerSummary(fiber.memoizedProps[rEvent]);
                  if (summary) handlers[eventName] = bestHandler(handlers[eventName], summary);
                }
              }
            }
          } catch(e) {}
        }
      }
    } catch(e) {}
    // 2. Inline handlers last
    for (const attr of INLINE_EVENTS) {
      const fn = el[attr];
      if (fn && typeof fn === 'function') {
        const eventName = attr.substring(2);
        const summary = handlerSummary(fn);
        if (summary) handlers[eventName] = bestHandler(handlers[eventName], summary);
      }
    }
    if (Object.keys(handlers).length > 0) {
      const id = String(hid);
      el.setAttribute('data-agent-hid', id);
      result[id] = handlers;
      hid++;
    }
  }
  return result;
})()
"""

CLEANUP_JS = r"""
(() => {
  const els = document.querySelectorAll('[data-agent-hid]');
  for (const el of els) {
    el.removeAttribute('data-agent-hid');
  }
})()
"""

# Inspect which elements got data-agent-hid and their properties
INSPECT_STAMPED_JS = r"""
(() => {
  const stamped = document.querySelectorAll('[data-agent-hid]');
  const results = [];
  for (const el of stamped) {
    const hid = el.getAttribute('data-agent-hid');
    const tag = el.tagName;
    const text = (el.textContent || '').trim().substring(0, 60);
    const classes = el.className ? String(el.className).substring(0, 80) : '';
    const cursor = window.getComputedStyle(el).cursor;
    const role = el.getAttribute('role') || '';
    const isInteractiveTag = ['A','BUTTON','INPUT','SELECT','TEXTAREA','OPTION','IFRAME'].includes(tag);
    results.push({
      hid, tag, text: text.substring(0, 40), classes: classes.substring(0, 60),
      cursor, role, isInteractiveTag,
      // Would this element be captured by snapshot without data-agent-hid?
      wouldBeInteractive: isInteractiveTag || role !== '' || cursor === 'pointer',
    });
  }
  return results;
})()
"""

# Check the challenge div specifically
CHECK_CHALLENGE_JS = r"""
(() => {
  // Find all divs with cursor-pointer class
  const cursorDivs = document.querySelectorAll('.cursor-pointer, div[class*="cursor-pointer"]');
  const results = [];
  for (const el of cursorDivs) {
    const hasHid = el.hasAttribute('data-agent-hid');
    const hid = el.getAttribute('data-agent-hid');
    const text = (el.textContent || '').trim().substring(0, 100);
    const cursor = window.getComputedStyle(el).cursor;
    const hasReactProps = Object.keys(el).some(k => k.startsWith('__reactProps$'));
    const hasReactFiber = Object.keys(el).some(k => k.startsWith('__reactFiber$'));

    // Check what React props exist
    let reactOnClick = null;
    for (const key of Object.keys(el)) {
      if (key.startsWith('__reactProps$')) {
        const props = el[key];
        if (props && props.onClick) {
          reactOnClick = typeof props.onClick === 'function'
            ? props.onClick.toString().substring(0, 100) : String(props.onClick);
        }
      }
    }

    // Check parent too
    const parent = el.parentElement;
    let parentHasHid = false;
    let parentReactOnClick = null;
    if (parent) {
      parentHasHid = parent.hasAttribute('data-agent-hid');
      for (const key of Object.keys(parent)) {
        if (key.startsWith('__reactProps$')) {
          const props = parent[key];
          if (props && props.onClick) {
            parentReactOnClick = typeof props.onClick === 'function'
              ? props.onClick.toString().substring(0, 100) : String(props.onClick);
          }
        }
      }
    }

    results.push({
      tag: el.tagName, text: text.substring(0, 60), cursor,
      hasHid, hid, hasReactProps, hasReactFiber, reactOnClick,
      parentTag: parent ? parent.tagName : null,
      parentHasHid, parentReactOnClick,
    });
  }
  return results;
})()
"""


async def run_extraction(page, label, extract_js):
    """Run handler extraction and inspect results."""
    # Clean up any prior stamps
    await page.evaluate(CLEANUP_JS)

    # Run extraction
    handlers = await page.evaluate(extract_js)
    handler_count = len(handlers)

    # Inspect stamped elements
    stamped = await page.evaluate(INSPECT_STAMPED_JS)

    # Check challenge div specifically
    challenge = await page.evaluate(CHECK_CHALLENGE_JS)

    print(f"\n{'=' * 70}")
    print(f"{label}")
    print(f"{'=' * 70}")
    print(f"Total elements with handlers (data-agent-hid): {handler_count}")
    print(f"Total stamped elements found: {len(stamped)}")

    # Categorize stamped elements
    interactive = [s for s in stamped if s['isInteractiveTag']]
    non_interactive_rescued = [s for s in stamped if not s['isInteractiveTag']]
    cursor_pointer = [s for s in stamped if s['cursor'] == 'pointer' and not s['isInteractiveTag']]

    print(f"\n  Interactive tags (BUTTON/INPUT/etc): {len(interactive)}")
    print(f"  Non-interactive rescued by data-agent-hid: {len(non_interactive_rescued)}")
    print(f"  cursor:pointer divs with handlers: {len(cursor_pointer)}")

    if non_interactive_rescued:
        print(f"\n  --- Non-interactive elements with handlers ---")
        for s in non_interactive_rescued[:15]:
            print(f"    hid={s['hid']:>3} <{s['tag']}> cursor={s['cursor']:<10} "
                  f"text=\"{s['text'][:30]}\" classes=\"{s['classes'][:40]}\"")

    print(f"\n  --- Challenge div (cursor-pointer) ---")
    if challenge:
        for c in challenge:
            print(f"    <{c['tag']}> cursor={c['cursor']} hasHid={c['hasHid']} hid={c['hid']}")
            print(f"      text: {c['text'][:60]}")
            print(f"      reactProps: {c['hasReactProps']} reactFiber: {c['hasReactFiber']}")
            print(f"      reactOnClick: {c['reactOnClick']}")
            print(f"      parent: <{c['parentTag']}> hasHid={c['parentHasHid']}")
            print(f"      parentReactOnClick: {c['parentReactOnClick']}")
    else:
        print("    NOT FOUND on page")

    # Show a few handler samples
    print(f"\n  --- Sample handlers ---")
    for hid_key in sorted(handlers.keys(), key=int)[:5]:
        events = handlers[hid_key]
        matched = [s for s in stamped if s['hid'] == hid_key]
        tag = matched[0]['tag'] if matched else '?'
        text = matched[0]['text'][:25] if matched else '?'
        print(f"    hid={hid_key}: <{tag}> \"{text}\"")
        for ev, src in events.items():
            print(f"      {ev}: {src[:80]}")

    # Clean up
    await page.evaluate(CLEANUP_JS)
    return handler_count, stamped, challenge


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 720})

        await page.goto(TARGET_URL, wait_until="networkidle")
        await page.wait_for_timeout(2000)

        # Click START
        await page.click("button:has-text('START')")
        await page.wait_for_timeout(3000)

        # We're on step1 now — run comparison here
        print(f"\nURL: {page.url}")
        print(f"Testing on: STEP 1")

        old_count, old_stamped, old_challenge = await run_extraction(
            page, "OLD EXTRACTION (inline-first, no fiber)", OLD_EXTRACT_JS
        )
        new_count, new_stamped, new_challenge = await run_extraction(
            page, "NEW EXTRACTION (React-first, fiber, bestHandler)", NEW_EXTRACT_JS
        )

        # Summary comparison
        print(f"\n{'=' * 70}")
        print("COMPARISON SUMMARY")
        print(f"{'=' * 70}")
        print(f"  OLD: {old_count} elements with handlers")
        print(f"  NEW: {new_count} elements with handlers")
        print(f"  Difference: {new_count - old_count:+d}")

        old_non_interactive = len([s for s in old_stamped if not s['isInteractiveTag']])
        new_non_interactive = len([s for s in new_stamped if not s['isInteractiveTag']])
        print(f"\n  OLD non-interactive rescued: {old_non_interactive}")
        print(f"  NEW non-interactive rescued: {new_non_interactive}")
        print(f"  Difference: {new_non_interactive - old_non_interactive:+d}")

        old_chal = old_challenge[0] if old_challenge else None
        new_chal = new_challenge[0] if new_challenge else None
        print(f"\n  Challenge div captured (OLD): {old_chal['hasHid'] if old_chal else 'NOT FOUND'}")
        print(f"  Challenge div captured (NEW): {new_chal['hasHid'] if new_chal else 'NOT FOUND'}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
