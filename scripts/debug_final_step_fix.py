"""Verify the final-step code-reveal bug fix works end-to-end.

Reproduces the off-by-one bug on step 30 of 30, where markChallengeComplete(30)
returns codes.get(31) = undefined, and then tests _fix_final_step_code_bug().

Expected output:
  - BUG CONFIRMED: Clicking "Reveal Code" on step 30 produces no code (null)
  - FIX: should navigate to /finish immediately on step 30 of 30
  - EDGE CASES: should not fire on non-final steps or mismatched URLs
"""
import asyncio
import sys

sys.path.insert(0, ".")

from playwright.async_api import async_playwright

from src.agent.core.agent import _fix_final_step_code_bug


SITE_URL = "https://serene-frangipane-7fd25b.netlify.app/"
VERSION = 3
TOTAL_STEPS = 30


async def get_raw_text_from_page(page) -> list[str]:
    """Get page text lines similar to snapshot.raw_text."""
    text = await page.evaluate("document.body.innerText")
    return [line.strip() for line in text.split("\n") if line.strip()]


async def skip_to_step(page, step: int) -> None:
    await page.evaluate(f"""(() => {{
        window.history.pushState({{}}, '', '/step{step}?version={VERSION}');
        window.dispatchEvent(new PopStateEvent('popstate', {{ state: {{}} }}));
    }})()""")
    await page.wait_for_timeout(2000)


async def click_reveal_code(page) -> str | None:
    """Click the Reveal Code button and return whatever code it reveals (or None)."""
    result = await page.evaluate("""(() => {
        const btns = Array.from(document.querySelectorAll('button'));
        const revealBtn = btns.find(b =>
            b.textContent.trim().toLowerCase().includes('reveal code')
        );
        if (!revealBtn) return 'no_button';
        revealBtn.click();
        return 'clicked';
    })()""")
    if result != "clicked":
        return None
    await page.wait_for_timeout(1000)
    # Check if a code appeared
    code = await page.evaluate("""(() => {
        const text = document.body.innerText;
        const match = text.match(/\\b[A-Z0-9]{6}\\b/);
        return match ? match[0] : null;
    })()""")
    return code


async def main() -> None:
    ok = True
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 720})
        page = await context.new_page()

        await page.goto(SITE_URL, wait_until="networkidle")
        await page.click("button:has-text('START')")
        await page.wait_for_timeout(2000)

        # ── 1. Reproduce the bug on step 30 ──
        print(f"1. Navigate to step {TOTAL_STEPS} and attempt Reveal Code")
        await skip_to_step(page, TOTAL_STEPS)
        raw_text = await get_raw_text_from_page(page)
        print(f"   URL: {page.url}")
        print(f"   Raw text preview: {raw_text[:5]}")

        # Verify we're on the final step
        has_final_step_text = any(
            f"step {TOTAL_STEPS} of {TOTAL_STEPS}" in line.lower()
            or f"step {TOTAL_STEPS}" in line.lower()
            for line in raw_text
        )
        print(f"   Final step text detected: {has_final_step_text}")

        # Try clicking Reveal Code
        code = await click_reveal_code(page)
        print(f"   Code after Reveal Code click: {code}")

        if code is not None:
            print("   NOTE: Code was revealed — bug may not be present on this version")
        else:
            print("   CONFIRMED: Reveal Code produces no code (off-by-one bug)")

        # Check if the challenge was marked complete despite null code
        completed = await page.evaluate(f"""(() => {{
            const text = document.body.innerText;
            return {{
                hasRevealCode: text.toLowerCase().includes('reveal code'),
                hasCode: /\\b[A-Z0-9]{{6}}\\b/.test(text),
                bodyText: text.substring(0, 500)
            }};
        }})()""")
        print(f"   Page still has Reveal Code button: {completed['hasRevealCode']}")
        print(f"   Page has any 6-char code: {completed['hasCode']}")

        # ── 2. Test fix fires immediately on step 30 of 30 ──
        print(f"\n2. FIX on step 30 of 30 (SHOULD fire immediately)")
        await skip_to_step(page, TOTAL_STEPS)
        raw_text = await get_raw_text_from_page(page)
        print(f"   URL before fix: {page.url}")
        print(f"   Raw text sample: {raw_text[:3]}")
        fixed = await _fix_final_step_code_bug(page, raw_text)
        print(f"   Fix applied: {fixed}")
        print(f"   URL after fix: {page.url}")

        if not fixed:
            print("   ERROR: Fix should have fired on final step")
            ok = False
        else:
            # Verify we're on /finish
            if "/finish" in page.url:
                print("   CORRECT: Navigated to /finish")
            else:
                print(f"   ERROR: Expected /finish in URL, got {page.url}")
                ok = False

            # Check if the congratulations page rendered
            await page.wait_for_timeout(1000)
            finish_text = await page.evaluate("document.body.innerText")
            has_congrats = any(
                word in finish_text.lower()
                for word in ["congratulations", "complete", "finish", "well done"]
            )
            print(f"   Congratulations page detected: {has_congrats}")
            print(f"   Finish page text preview: {finish_text[:200]}")

            if not has_congrats:
                print("   WARNING: No congratulations text found on /finish page")

        # ── 3. Edge case: fix should NOT fire on non-final steps ──
        print(f"\n3. EDGE CASE — fix should not fire on step 15 of 30")
        await skip_to_step(page, 15)
        raw_text = await get_raw_text_from_page(page)
        fixed = await _fix_final_step_code_bug(page, raw_text)
        print(f"   Fix applied (should be False): {fixed}")
        if fixed:
            print("   ERROR: Fix should not fire on non-final step")
            ok = False
        else:
            print("   CORRECT: Fix skipped on non-final step")

        # ── 4. Edge case: fix should NOT fire with mismatched URL ──
        print(f"\n4. EDGE CASE — raw text says step 30 but URL is /step15")
        fake_raw_text = [f"Step {TOTAL_STEPS} of {TOTAL_STEPS}", "some other text"]
        await skip_to_step(page, 15)
        fixed = await _fix_final_step_code_bug(page, fake_raw_text)
        print(f"   Fix applied (should be False): {fixed}")
        if fixed:
            print("   ERROR: Fix should not fire with URL/text mismatch")
            ok = False
        else:
            print("   CORRECT: Fix skipped (URL mismatch)")

        await browser.close()

    print(f"\n{'='*60}")
    if ok:
        print("ALL CHECKS PASSED")
    else:
        print("SOME CHECKS FAILED")
    print(f"{'='*60}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
