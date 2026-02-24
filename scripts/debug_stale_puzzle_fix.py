"""Verify the stale puzzle state fix works end-to-end.

Reproduces the React state leak on back-to-back math puzzle steps, then tests
the actual _fix_stale_puzzle_state() function from agent.py.

Expected output:
  - WITHOUT FIX: step 18 shows "Puzzle solved" with step 17's stale code,
    no Solve button, no number input → unsolvable
  - WITH FIX: pushState detour resets the component, step 18 shows a fresh
    puzzle with Solve button + number input → solvable with its own code
"""
import asyncio
import re
import sys

from playwright.async_api import async_playwright

from src.agent.core.agent import _fix_stale_puzzle_state


SITE_URL = "https://serene-frangipane-7fd25b.netlify.app/"
VERSION = 3
FIRST_PUZZLE = 17
LEAKED_PUZZLE = 18


async def get_puzzle_state(page) -> dict:
    return await page.evaluate("""
        (() => {
            const text = document.body.innerText;
            const challengeMatch = text.match(/Challenge Step (\\d+)/i);
            const puzzleMatch = text.match(/(\\d+) \\+ (\\d+) = \\?/);
            const hasSolveBtn = Array.from(document.querySelectorAll('button')).some(
                b => b.textContent.trim() === 'Solve');
            const hasNumberInput = !!document.querySelector('input[type="number"]');
            const puzzleSolved = text.includes('Puzzle solved');
            const codeMatch = text.match(/Code revealed:[\\s]*([A-Z0-9]{6})/);
            const allCodes = text.match(/\\b[A-Z0-9]{6}\\b/g) || [];
            return {
                challengeStep: challengeMatch ? parseInt(challengeMatch[1]) : null,
                puzzle: puzzleMatch ? `${puzzleMatch[1]} + ${puzzleMatch[2]} = ?` : null,
                puzzleAnswer: puzzleMatch ? parseInt(puzzleMatch[1]) + parseInt(puzzleMatch[2]) : null,
                hasSolveButton: hasSolveBtn,
                hasNumberInput: hasNumberInput,
                puzzleSolved: puzzleSolved,
                revealedCode: codeMatch ? codeMatch[1] : null,
                allCodes: allCodes,
            };
        })()
    """)


async def skip_to_step(page, step: int) -> None:
    await page.evaluate(f"""(() => {{
        window.history.pushState({{}}, '', '/step{step}?version={VERSION}');
        window.dispatchEvent(new PopStateEvent('popstate', {{ state: {{}} }}));
    }})()""")
    await page.wait_for_timeout(2000)


async def solve_puzzle(page, answer: int) -> str | None:
    """Solve the puzzle and return the revealed code."""
    await page.evaluate(f"""(() => {{
        const input = document.querySelector('input[type="number"]');
        const setter = Object.getOwnPropertyDescriptor(
            HTMLInputElement.prototype, 'value').set;
        setter.call(input, '{answer}');
        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
        input.dispatchEvent(new Event('change', {{ bubbles: true }}));
    }})()""")
    await page.wait_for_timeout(300)
    await page.evaluate("""(() => {
        const btn = Array.from(document.querySelectorAll('button')).find(
            b => b.textContent.trim() === 'Solve');
        if (btn) btn.click();
    })()""")
    await page.wait_for_timeout(1000)
    state = await get_puzzle_state(page)
    return state['revealedCode']


async def submit_code(page, code: str) -> None:
    await page.evaluate(f"""(() => {{
        const input = document.querySelector('input[placeholder*="code"]');
        const setter = Object.getOwnPropertyDescriptor(
            HTMLInputElement.prototype, 'value').set;
        setter.call(input, '{code}');
        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
        input.dispatchEvent(new Event('change', {{ bubbles: true }}));
    }})()""")
    await page.wait_for_timeout(300)
    await page.evaluate("""(() => {
        const btn = Array.from(document.querySelectorAll('button')).find(
            b => b.textContent.trim() === 'Submit Code');
        if (btn) btn.click();
    })()""")
    await page.wait_for_timeout(2000)


def get_raw_text(page) -> list[str]:
    """Simulate what snapshot.raw_text would contain."""
    # We can't easily call capture_snapshot without a CDP session, so we
    # approximate raw_text by getting all visible text nodes from the page.
    # For detection purposes, innerText suffices.
    pass


async def get_raw_text_from_page(page) -> list[str]:
    """Get page text lines similar to snapshot.raw_text."""
    text = await page.evaluate("document.body.innerText")
    return [line.strip() for line in text.split("\n") if line.strip()]


async def main() -> None:
    ok = True
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 720})
        page = await context.new_page()

        await page.goto(SITE_URL, wait_until="networkidle")
        await page.click("button:has-text('START')")
        await page.wait_for_timeout(2000)

        # ── Reproduce the bug ──
        print(f"1. Navigate to step {FIRST_PUZZLE}, solve it, submit code")
        await skip_to_step(page, FIRST_PUZZLE)
        s = await get_puzzle_state(page)
        assert s['hasSolveButton'] and s['hasNumberInput'], \
            f"Step {FIRST_PUZZLE} doesn't have puzzle UI"

        code17 = await solve_puzzle(page, s['puzzleAnswer'])
        print(f"   Solved step {FIRST_PUZZLE}: code = {code17}")
        await submit_code(page, code17)

        # ── WITHOUT FIX: check step 18 ──
        print(f"\n2. WITHOUT FIX — checking step {LEAKED_PUZZLE}")
        s18 = await get_puzzle_state(page)
        raw_text = await get_raw_text_from_page(page)

        print(f"   Step: {s18['challengeStep']}")
        print(f"   Puzzle solved (stale): {s18['puzzleSolved']}")
        print(f"   Code shown: {s18['revealedCode']}")
        print(f"   Stale code matches step {FIRST_PUZZLE}: {s18['revealedCode'] == code17}")
        print(f"   Solve button: {s18['hasSolveButton']}")
        print(f"   Number input: {s18['hasNumberInput']}")
        print(f"   Solvable: {s18['hasSolveButton'] and s18['hasNumberInput']}")

        if not s18['puzzleSolved']:
            print("\n   ERROR: Bug not reproduced — step 18 is fresh (unexpected)")
            ok = False
        elif s18['hasSolveButton']:
            print("\n   ERROR: Step 18 has Solve button despite stale state (unexpected)")
            ok = False
        else:
            print(f"\n   CONFIRMED: Step {LEAKED_PUZZLE} is broken — stale state from step {FIRST_PUZZLE}")

        # ── WITH FIX: run _fix_stale_puzzle_state ──
        print(f"\n3. WITH FIX — running _fix_stale_puzzle_state()")
        fixed = await _fix_stale_puzzle_state(
            page, raw_text, last_step_was_puzzle=True
        )
        print(f"   Fix applied: {fixed}")

        if not fixed:
            print("   ERROR: Fix did not detect the stale state")
            ok = False
        else:
            s18_fixed = await get_puzzle_state(page)
            print(f"   Step: {s18_fixed['challengeStep']}")
            print(f"   Puzzle solved: {s18_fixed['puzzleSolved']}")
            print(f"   Solve button: {s18_fixed['hasSolveButton']}")
            print(f"   Number input: {s18_fixed['hasNumberInput']}")
            solvable = s18_fixed['hasSolveButton'] and s18_fixed['hasNumberInput']
            print(f"   Solvable: {solvable}")

            if not solvable:
                print(f"\n   ERROR: Fix ran but step {LEAKED_PUZZLE} is still not solvable")
                ok = False
            else:
                # Solve step 18 and confirm it has its own code
                code18 = await solve_puzzle(page, s18_fixed['puzzleAnswer'])
                print(f"\n   Solved step {LEAKED_PUZZLE}: code = {code18}")
                print(f"   Different from step {FIRST_PUZZLE} ({code17}): {code18 != code17}")

                if code18 == code17:
                    print("   ERROR: Same code — fix did not actually reset state")
                    ok = False
                else:
                    print(f"\n   SUCCESS: Step {LEAKED_PUZZLE} has its own unique code")

        # ── Edge case: fix should NOT fire when last step was not a puzzle ──
        print(f"\n4. EDGE CASE — fix should not fire when last_step_was_puzzle=False")
        # Re-reproduce the stale state
        await skip_to_step(page, FIRST_PUZZLE)
        s = await get_puzzle_state(page)
        if s['hasSolveButton'] and s['hasNumberInput']:
            await solve_puzzle(page, s['puzzleAnswer'])
            await submit_code(page, (await get_puzzle_state(page))['revealedCode'])
        raw_text = await get_raw_text_from_page(page)
        not_fixed = await _fix_stale_puzzle_state(
            page, raw_text, last_step_was_puzzle=False
        )
        print(f"   Fix applied (should be False): {not_fixed}")
        if not_fixed:
            print("   ERROR: Fix should not have fired")
            ok = False
        else:
            print("   CORRECT: Fix skipped when last step was not a puzzle")

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
