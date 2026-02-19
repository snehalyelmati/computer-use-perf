"""Debug: verify that el.onclick (React event delegation) overrides __reactProps$.

The agent's extraction code checks el.onclick BEFORE __reactProps$. If el.onclick
is set (by React's event delegation), it captures the minified dispatch wrapper
'function _o(){}' and the real handler from __reactProps$ is never used.
"""

import asyncio

from playwright.async_api import async_playwright

TARGET_URL = "https://serene-frangipane-7fd25b.netlify.app/"

# Minimal check: for every button, compare el.onclick vs __reactProps$.onClick
COMPARE_JS = """
(() => {
  const MAX_LEN = 150;
  function trunc(src) {
    if (!src) return null;
    let s = String(src).trim().replace(/\\s+/g, ' ');
    return s.length > MAX_LEN ? s.substring(0, MAX_LEN) + '...' : s;
  }

  const REACT_EVENTS = ['onClick','onChange','onInput','onSubmit','onKeyDown',
    'onFocus','onBlur','onMouseDown','onMouseUp','onMouseEnter','onMouseLeave'];

  const results = [];
  const elements = document.querySelectorAll('button, input, [role="radio"], form, canvas, div[class*="cursor-pointer"]');

  for (const el of elements) {
    const label = (el.textContent || '').trim().substring(0, 40);
    const tag = el.tagName;
    const entry = { label, tag };

    // el.onclick
    entry.elOnclick = (el.onclick && typeof el.onclick === 'function')
      ? { src: trunc(el.onclick.toString()), name: el.onclick.name || '(anon)' }
      : null;

    // __reactProps$ onClick
    const keys = Object.keys(el);
    entry.propsOnClick = null;
    entry.fiberOnClick = null;

    for (const key of keys) {
      if (key.startsWith('__reactProps$') || key.startsWith('__reactProps')) {
        const props = el[key];
        if (props && props.onClick && typeof props.onClick === 'function') {
          entry.propsOnClick = { src: trunc(props.onClick.toString()), name: props.onClick.name || '(anon)' };
        }
      }
      if (key.startsWith('__reactFiber$') || key.startsWith('__reactInternalInstance$')) {
        try {
          const fiber = el[key];
          if (fiber && fiber.memoizedProps && fiber.memoizedProps.onClick) {
            const fn = fiber.memoizedProps.onClick;
            entry.fiberOnClick = { src: trunc(fn.toString()), name: fn.name || '(anon)' };
          }
        } catch(e) {}
      }
    }

    // What the current agent extraction would produce
    entry.agentWouldGet = null;
    if (entry.elOnclick) {
      entry.agentWouldGet = entry.elOnclick;
      entry.source = 'inline';
    } else if (entry.propsOnClick) {
      entry.agentWouldGet = entry.propsOnClick;
      entry.source = 'reactProps';
    } else {
      entry.source = 'none';
    }

    // Is the inline handler different from the React props handler?
    entry.inlineOverridesProps = !!(
      entry.elOnclick && entry.propsOnClick &&
      entry.elOnclick.src !== entry.propsOnClick.src
    );

    if (entry.elOnclick || entry.propsOnClick || entry.fiberOnClick) {
      results.push(entry);
    }
  }

  return results;
})()
"""


async def test_page(page, label):
    """Run the comparison on the current page."""
    print(f"\n{'=' * 60}")
    print(f"{label}")
    print(f"URL: {page.url}")
    print(f"{'=' * 60}")

    results = await page.evaluate(COMPARE_JS)

    overrides = [r for r in results if r['inlineOverridesProps']]
    inline_only = [r for r in results if r['elOnclick'] and not r['propsOnClick']]
    props_only = [r for r in results if r['propsOnClick'] and not r['elOnclick']]
    both_same = [r for r in results if r['elOnclick'] and r['propsOnClick'] and not r['inlineOverridesProps']]

    print(f"\nTotal elements with click handlers: {len(results)}")
    print(f"  el.onclick OVERRIDES reactProps (different src): {len(overrides)}")
    print(f"  el.onclick only (no reactProps):                 {len(inline_only)}")
    print(f"  reactProps only (no el.onclick):                 {len(props_only)}")
    print(f"  Both same:                                       {len(both_same)}")

    if overrides:
        print(f"\n--- Overridden handlers (inline hides real handler) ---")
        for r in overrides[:5]:
            print(f"\n  [{r['tag']}] '{r['label']}'")
            print(f"    el.onclick:     {r['elOnclick']['name']:12s} → {r['elOnclick']['src'][:80]}")
            print(f"    reactProps:     {r['propsOnClick']['name']:12s} → {r['propsOnClick']['src'][:80]}")
            if r['fiberOnClick']:
                print(f"    fiber:          {r['fiberOnClick']['name']:12s} → {r['fiberOnClick']['src'][:80]}")
            print(f"    Agent sees:     {r['agentWouldGet']['src'][:80]}  ← WRONG")

    if props_only:
        print(f"\n--- reactProps only (agent gets real handler) ---")
        for r in props_only[:3]:
            print(f"  [{r['tag']}] '{r['label']}' → {r['propsOnClick']['name']}: {r['propsOnClick']['src'][:80]}")

    if both_same:
        print(f"\n--- Both same (no override) ---")
        for r in both_same[:3]:
            print(f"  [{r['tag']}] '{r['label']}' → {r['elOnclick']['src'][:80]}")

    if inline_only:
        print(f"\n--- el.onclick only (no React props) ---")
        for r in inline_only[:3]:
            print(f"  [{r['tag']}] '{r['label']}' → {r['elOnclick']['name']}: {r['elOnclick']['src'][:80]}")

    return results


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page(viewport={"width": 1280, "height": 720})

        # Test START page
        await page.goto(TARGET_URL, wait_until="networkidle")
        await page.wait_for_timeout(2000)
        await test_page(page, "START PAGE")

        # Navigate to step 1
        await page.click("button:has-text('START')")
        await page.wait_for_timeout(3000)
        await test_page(page, "STEP 1")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
