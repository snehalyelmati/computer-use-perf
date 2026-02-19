"""Debug: compare old vs new handler extraction on a React page.

Runs both old (inline-first, no fiber, no bestHandler) and new (fiber+props first,
bestHandler quality selection) extraction JS side-by-side on the challenge site to
confirm that "Complete Challenge" gets a distinct handler hint from decoy buttons.
"""

import asyncio

from playwright.async_api import async_playwright

TARGET_URL = "https://serene-frangipane-7fd25b.netlify.app/"

MAX_LEN = 120

# ── Old extraction (inline-first, no fiber, no quality comparison) ──────────
OLD_JS = """
(() => {
  const MAX_LEN = """ + str(MAX_LEN) + """;
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
    let s = String(src).trim().replace(/\\s+/g, ' ');
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
  const buttons = document.querySelectorAll('button');
  const results = [];
  for (const el of buttons) {
    const label = (el.textContent || '').trim().substring(0, 40);
    const handlers = {};
    // Inline first (old behavior)
    for (const attr of INLINE_EVENTS) {
      const fn = el[attr];
      if (fn && typeof fn === 'function') {
        const eventName = attr.substring(2);
        const summary = handlerSummary(fn);
        if (summary) handlers[eventName] = summary;
      }
    }
    // React props (blocked by inline guard)
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
    results.push({ label, click: handlers['click'] || null });
  }
  return results;
})()
"""

# ── New extraction (from handlers.py) ──────────────────────────────────────
NEW_JS = """
(() => {
  const MAX_LEN = """ + str(MAX_LEN) + """;
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
    let s = String(src).trim().replace(/\\s+/g, ' ');
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
    if (/^function\\s*[_$a-zA-Z]{0,2}\\s*\\(/.test(s) && s.length < 40) return true;
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
  const buttons = document.querySelectorAll('button');
  const results = [];
  for (const el of buttons) {
    const label = (el.textContent || '').trim().substring(0, 40);
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
    // 2. Inline last
    for (const attr of INLINE_EVENTS) {
      const fn = el[attr];
      if (fn && typeof fn === 'function') {
        const eventName = attr.substring(2);
        const summary = handlerSummary(fn);
        if (summary) handlers[eventName] = bestHandler(handlers[eventName], summary);
      }
    }
    results.push({ label, click: handlers['click'] || null });
  }
  return results;
})()
"""


async def test_page(page, label):
    """Run old vs new extraction on the current page and compare."""
    print(f"\n{'=' * 70}")
    print(f"{label}")
    print(f"URL: {page.url}")
    print(f"{'=' * 70}")

    old_results = await page.evaluate(OLD_JS)
    new_results = await page.evaluate(NEW_JS)

    print(f"\n{'Button Label':<35} {'OLD click handler':<50} {'NEW click handler':<50}")
    print("-" * 135)

    for old, new in zip(old_results, new_results):
        old_click = (old["click"] or "(none)")[:48]
        new_click = (new["click"] or "(none)")[:48]
        changed = " <<<" if old_click != new_click else ""
        print(f"{old['label']:<35} {old_click:<50} {new_click:<50}{changed}")

    # Summary
    diffs = sum(1 for o, n in zip(old_results, new_results) if o["click"] != n["click"])
    print(f"\nButtons with DIFFERENT handler text: {diffs}/{len(old_results)}")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 720})

        # Test START page
        await page.goto(TARGET_URL, wait_until="networkidle")
        await page.wait_for_timeout(2000)
        await test_page(page, "START PAGE")

        # Navigate to step 1
        try:
            await page.click("button:has-text('START')", timeout=5000)
            await page.wait_for_timeout(3000)
            await test_page(page, "STEP 1 (after clicking START)")
        except Exception as e:
            print(f"\nCould not navigate to step 1: {e}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
