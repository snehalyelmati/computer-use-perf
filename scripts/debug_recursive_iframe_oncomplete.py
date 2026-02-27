"""Diagnostic: Introspect Hv component's onComplete, proof structure, and hook chain.

Navigates to step 24, reaches the stuck state (deepest level with Extract Code
visible), then dumps:
  1. Hv fiber's memoizedProps (looking for onComplete)
  2. Extract Code onClick.toString() (reveals w() and proof structure)
  3. Full hook chain with types and values
  4. Tests calling onComplete directly

Usage:
    uv run python scripts/debug_recursive_iframe_oncomplete.py
"""

import asyncio
import json

from playwright.async_api import async_playwright

SITE_URL = "https://serene-frangipane-7fd25b.netlify.app/"


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()

        print("1. Loading site and navigating to step 24...")
        await page.goto(SITE_URL, wait_until="networkidle")
        await page.click("button:has-text('START')")
        await page.wait_for_timeout(2000)

        await page.evaluate("""(() => {
            window.history.pushState({}, '', '/step24?version=2');
            window.dispatchEvent(new PopStateEvent('popstate', { state: {} }));
        })()""")
        await page.wait_for_timeout(2000)

        print("2. Entering all levels to reach stuck state...")
        for level in [2, 3]:
            await page.wait_for_timeout(1500)
            await page.evaluate(f"""(() => {{
                const btn = Array.from(document.querySelectorAll('button'))
                    .find(b => b.textContent.includes('Enter Level {level}'));
                if (btn) btn.click();
            }})()""")
            await page.wait_for_timeout(500)

        # Confirm stuck state
        text = await page.evaluate("document.body.innerText")
        assert "deepest level" in text.lower(), "Not at deepest level!"
        assert "extract code" in text.lower(), "No Extract Code button!"
        print("   Stuck state confirmed.\n")

        # ── 1. Extract Code onClick source ──
        print("=" * 60)
        print("1. Extract Code button onClick source (full)")
        print("=" * 60)
        onclick_src = await page.evaluate("""(() => {
            const btn = Array.from(document.querySelectorAll('button'))
                .find(b => b.textContent.trim().toLowerCase().includes('extract code'));
            if (!btn) return 'no_button';
            const pk = Object.keys(btn).find(k => k.startsWith('__reactProps'));
            if (!pk) return 'no_props';
            return btn[pk].onClick ? btn[pk].onClick.toString() : 'no_onClick';
        })()""")
        print(onclick_src)

        # ── 2. Walk fiber tree, dump memoizedProps for each component ──
        print("\n" + "=" * 60)
        print("2. Fiber tree walk — memoizedProps (looking for onComplete)")
        print("=" * 60)
        fiber_props = await page.evaluate("""(() => {
            const btn = Array.from(document.querySelectorAll('button'))
                .find(b => b.textContent.trim().toLowerCase().includes('extract code'));
            if (!btn) return { error: 'no_button' };
            const fk = Object.keys(btn).find(k => k.startsWith('__reactFiber$'));
            if (!fk) return { error: 'no_fiber' };

            let fiber = btn[fk];
            const path = [];
            while (fiber && path.length < 20) {
                const info = {
                    tag: fiber.tag,
                    type: fiber.type
                        ? (fiber.type.name || fiber.type.displayName || typeof fiber.type)
                        : null,
                };
                if (fiber.memoizedProps && typeof fiber.memoizedProps === 'object') {
                    const propKeys = Object.keys(fiber.memoizedProps);
                    info.propKeys = propKeys;
                    // Dump values for interesting props
                    for (const key of propKeys) {
                        const val = fiber.memoizedProps[key];
                        if (typeof val === 'function') {
                            info['prop_' + key] = '[function] ' + val.toString().substring(0, 200);
                        } else if (typeof val === 'number' || typeof val === 'string' || typeof val === 'boolean') {
                            info['prop_' + key] = val;
                        } else if (val && typeof val === 'object') {
                            try {
                                info['prop_' + key] = JSON.stringify(val).substring(0, 200);
                            } catch(e) {
                                info['prop_' + key] = '[object, not serializable]';
                            }
                        }
                    }
                }
                path.push(info);
                fiber = fiber.return;
            }
            return path;
        })()""")
        for i, entry in enumerate(fiber_props if isinstance(fiber_props, list) else [fiber_props]):
            print(f"\n  [{i}] tag={entry.get('tag')} type={entry.get('type')}")
            if entry.get('propKeys'):
                print(f"      propKeys: {entry['propKeys']}")
            for k, v in entry.items():
                if k.startswith('prop_'):
                    val_str = str(v)
                    if len(val_str) > 120:
                        val_str = val_str[:120] + "..."
                    print(f"      {k}: {val_str}")

        # ── 3. Find the Hv fiber specifically and dump its full hook chain ──
        print("\n" + "=" * 60)
        print("3. Hv component — full hook chain")
        print("=" * 60)
        hooks = await page.evaluate("""(() => {
            const btn = Array.from(document.querySelectorAll('button'))
                .find(b => b.textContent.trim().toLowerCase().includes('extract code'));
            if (!btn) return { error: 'no_button' };
            const fk = Object.keys(btn).find(k => k.startsWith('__reactFiber$'));
            if (!fk) return { error: 'no_fiber' };

            // Walk up looking for fiber with onComplete in memoizedProps
            let fiber = btn[fk];
            let target = null;
            while (fiber) {
                if (fiber.memoizedProps && typeof fiber.memoizedProps.onComplete === 'function') {
                    target = fiber;
                    break;
                }
                fiber = fiber.return;
            }

            if (!target) {
                // Fallback: look by component name
                fiber = btn[fk];
                while (fiber) {
                    if (fiber.type && fiber.type.name === 'Hv') {
                        target = fiber;
                        break;
                    }
                    fiber = fiber.return;
                }
            }

            if (!target) return { error: 'Hv not found by onComplete or name' };

            const result = {
                foundBy: target.memoizedProps && typeof target.memoizedProps.onComplete === 'function'
                    ? 'onComplete' : 'name',
                typeName: target.type ? target.type.name : null,
                propKeys: target.memoizedProps ? Object.keys(target.memoizedProps) : [],
            };

            // Dump all props values
            if (target.memoizedProps) {
                result.props = {};
                for (const [k, v] of Object.entries(target.memoizedProps)) {
                    if (typeof v === 'function') {
                        result.props[k] = '[fn] ' + v.toString().substring(0, 300);
                    } else {
                        try {
                            result.props[k] = JSON.parse(JSON.stringify(v));
                        } catch(e) {
                            result.props[k] = String(v).substring(0, 200);
                        }
                    }
                }
            }

            // Dump full hook chain
            let hook = target.memoizedState;
            const hooks = [];
            let idx = 0;
            while (hook && idx < 10) {
                const entry = { idx };
                const val = hook.memoizedState;
                entry.type = typeof val;
                if (val === null || val === undefined) {
                    entry.value = val;
                } else if (typeof val === 'number' || typeof val === 'string' || typeof val === 'boolean') {
                    entry.value = val;
                } else if (val && typeof val === 'object' && val.current !== undefined) {
                    // Likely a ref
                    entry.isRef = true;
                    try { entry.value = JSON.parse(JSON.stringify(val.current)); }
                    catch(e) { entry.value = '[not serializable]'; }
                } else {
                    try { entry.value = JSON.parse(JSON.stringify(val)); }
                    catch(e) { entry.value = '[not serializable]'; }
                }
                entry.hasQueue = !!hook.queue;
                entry.hasDispatch = !!(hook.queue && typeof hook.queue.dispatch === 'function');
                hooks.push(entry);
                hook = hook.next;
                idx++;
            }
            result.hooks = hooks;

            return result;
        })()""")
        print(json.dumps(hooks, indent=2))

        # ── 4. Test calling onComplete directly ──
        print("\n" + "=" * 60)
        print("4. Test calling onComplete directly")
        print("=" * 60)
        test_result = await page.evaluate("""(() => {
            const btn = Array.from(document.querySelectorAll('button'))
                .find(b => b.textContent.trim().toLowerCase().includes('extract code'));
            if (!btn) return { error: 'no_button' };
            const fk = Object.keys(btn).find(k => k.startsWith('__reactFiber$'));
            if (!fk) return { error: 'no_fiber' };

            let fiber = btn[fk];
            while (fiber) {
                if (fiber.memoizedProps && typeof fiber.memoizedProps.onComplete === 'function') {
                    break;
                }
                fiber = fiber.return;
            }
            if (!fiber) return { error: 'no onComplete fiber' };

            const onComplete = fiber.memoizedProps.onComplete;
            const numLevels = fiber.memoizedProps.numLevels || fiber.memoizedProps.levels;

            // Read hook chain to build proof
            let hook = fiber.memoizedState;
            let hookIdx = 0;
            let levelClickTimes = null;
            let currentLevel = null;
            while (hook && hookIdx < 10) {
                const val = hook.memoizedState;
                if (typeof val === 'number' && hookIdx === 0) {
                    currentLevel = val;
                }
                if (val && typeof val === 'object' && val.current !== undefined && hookIdx >= 1) {
                    levelClickTimes = val.current;
                }
                hook = hook.next;
                hookIdx++;
            }

            // Try calling onComplete with various proof shapes
            const results = [];

            // Try 1: empty object
            try {
                const r = onComplete({});
                results.push({ proof: '{}', result: r, type: typeof r });
            } catch(e) {
                results.push({ proof: '{}', error: e.message });
            }

            // Try 2: with levelClickTimes
            try {
                const proof = { levelClickTimes: levelClickTimes, completedAt: Date.now() };
                const r = onComplete(proof);
                results.push({ proof: JSON.stringify(proof).substring(0, 200), result: r, type: typeof r });
            } catch(e) {
                results.push({ proof: 'with times', error: e.message });
            }

            // Try 3: just the code string (maybe it's a simpler callback)
            try {
                const r = onComplete('TEST123');
                results.push({ proof: 'string TEST123', result: r, type: typeof r });
            } catch(e) {
                results.push({ proof: 'string', error: e.message });
            }

            return {
                numLevelsFromProps: numLevels,
                currentLevel,
                levelClickTimes,
                allPropKeys: Object.keys(fiber.memoizedProps),
                callResults: results,
            };
        })()""")
        print(json.dumps(test_result, indent=2))

        # Check if calling onComplete changed the page
        await page.wait_for_timeout(1000)
        text_after = await page.evaluate("document.body.innerText")
        codes = await page.evaluate(
            r"document.body.innerText.match(/\b[A-Z0-9]{6}\b/g)"
        )
        print(f"\nAfter onComplete calls:")
        print(f"  Codes on page: {codes}")
        print(f"  Extract Code still visible: {'extract code' in text_after.lower()}")
        if codes:
            print(f"  Page text around code: {text_after[text_after.lower().find('code'):text_after.lower().find('code')+200]}")

        print("\n=== DONE ===")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
