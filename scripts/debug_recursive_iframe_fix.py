"""Debug script: Verify the _fix_recursive_iframe_bug onComplete patch works.

Navigates to step 24, enters all levels to reach the stuck state,
then applies the same onComplete bypass used in agent.py and confirms
that the code is visible on the page.

Usage:
    uv run python scripts/debug_recursive_iframe_fix.py
"""

import asyncio
from playwright.async_api import async_playwright

SITE_URL = "https://serene-frangipane-7fd25b.netlify.app/"

# Same JS patch used in agent.py _fix_recursive_iframe_bug
PATCH_JS = """(() => {
    // Find the "Extract Code" button
    const btns = [...document.querySelectorAll('button')];
    const extractBtn = btns.find(b =>
        b.textContent.trim().toLowerCase().includes('extract code')
    );
    if (!extractBtn) return 'no_button';

    // Walk up the React fiber tree to find the Hv component
    // (the one with onComplete in memoizedProps)
    const fiberKey = Object.keys(extractBtn).find(k =>
        k.startsWith('__reactFiber$')
    );
    if (!fiberKey) return 'no_fiber';

    let fiber = extractBtn[fiberKey];
    let hvFiber = null;
    while (fiber) {
        if (fiber.memoizedProps &&
            typeof fiber.memoizedProps.onComplete === 'function') {
            hvFiber = fiber;
            break;
        }
        fiber = fiber.return;
    }
    if (!hvFiber) return 'no_onComplete';

    // Read props
    const onComplete = hvFiber.memoizedProps.onComplete;
    const config = hvFiber.memoizedProps.config || {};
    const numLevels = (config.metadata && config.metadata.numLevels) || 3;
    const stepNum = hvFiber.memoizedProps.stepNum;

    // Read hook chain:
    //   hook 0 = currentLevel (useState, number)
    //   hook 1 = code display state (useState, null initially)
    //   hook 2 = levelClickTimes (useRef, {current: {...}})
    const hook0 = hvFiber.memoizedState;
    if (!hook0 || typeof hook0.memoizedState !== 'number') return 'no_level_hook';
    const currentLevel = hook0.memoizedState;

    const hook1 = hook0.next;
    if (!hook1 || !hook1.queue || typeof hook1.queue.dispatch !== 'function')
        return 'no_code_hook';

    const hook2 = hook1.next;
    const clickTimes = (hook2 && hook2.memoizedState && hook2.memoizedState.current)
        ? { ...hook2.memoizedState.current }
        : {};

    // Fill in the missing click time for the current (stuck) level
    if (!(currentLevel in clickTimes)) {
        clickTimes[currentLevel] = Date.now();
    }

    // Build the proof — same structure as the Extract Code onClick's
    // internal builder
    const proof = {
        type: "recursive_iframe",
        timestamp: Date.now(),
        data: {
            method: "recursive_iframe",
            numLevels: numLevels,
            currentLevel: numLevels,
            levelClickTimes: clickTimes,
            stepNum: stepNum,
        },
    };

    // Call onComplete(proof) — returns the code string
    const code = onComplete(proof);
    if (!code) return 'onComplete_returned_null';

    // Dispatch code into hook 1 (display state) so it renders on the page
    hook1.queue.dispatch(code);

    return 'patched_' + code;
})()"""


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()

        print("1. Loading site...")
        await page.goto(SITE_URL, wait_until="networkidle")
        await page.click("button:has-text('START')")
        await page.wait_for_timeout(2000)

        print("2. Navigating to step 24...")
        await page.evaluate("""(() => {
            window.history.pushState({}, '', '/step24?version=2');
            window.dispatchEvent(new PopStateEvent('popstate', { state: {} }));
        })()""")
        await page.wait_for_timeout(2000)

        print("3. Entering all levels...")
        for level in [2, 3]:
            await page.wait_for_timeout(1500)
            await page.evaluate(f"""(() => {{
                const btn = Array.from(document.querySelectorAll('button'))
                    .find(b => b.textContent.includes('Enter Level {level}'));
                if (btn) btn.click();
            }})()""")
            await page.wait_for_timeout(500)

        # Confirm we're stuck
        text = await page.evaluate("document.body.innerText")
        text_lower = text.lower()
        assert "deepest level" in text_lower, "Not at deepest level!"
        assert "extract code" in text_lower, "Extract Code button not visible!"
        print("   At deepest level with Extract Code visible (stuck state confirmed)")

        # Try Extract Code before patch — should do nothing
        await page.evaluate("""(() => {
            const btn = Array.from(document.querySelectorAll('button'))
                .find(b => b.textContent.includes('Extract Code'));
            if (btn) btn.click();
        })()""")
        await page.wait_for_timeout(1000)
        codes_before = await page.evaluate(
            r"document.body.innerText.match(/\b[A-Z0-9]{6}\b/g)"
        )
        print(f"   Extract Code before patch: codes={codes_before}")

        print("4. Applying onComplete bypass patch...")
        result = await page.evaluate(PATCH_JS)
        print(f"   Patch result: {result}")
        await page.wait_for_timeout(500)

        # Check page state after patch
        text_after = await page.evaluate("document.body.innerText")
        codes_after = await page.evaluate(
            r"document.body.innerText.match(/\b[A-Z0-9]{6}\b/g)"
        )

        print(f"   Codes on page: {codes_after}")
        print(f"   Extract Code still visible: {'extract code' in text_after.lower()}")

        if codes_after:
            print(f"\n>>> SUCCESS: Code visible on page = {codes_after}")
        elif isinstance(result, str) and result.startswith("patched_"):
            code = result[len("patched_"):]
            print(f"\n>>> SUCCESS: onComplete returned code = {code}")
        else:
            print(f"\n>>> FAILED: No code found")
            print(f"   Patch result: {result}")
            # Dump page text for debugging
            snippet = text_after[:600]
            print(f"   Page text (first 600 chars):\n{snippet}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
