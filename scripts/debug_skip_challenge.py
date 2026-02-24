"""Debug script: verify pushState+popstate skip — focus on the React state leak.

Confirmed from test A: pushState + popstate triggers React Router client-side nav.
Now test: does skipping via pushState give FRESH component state (fix the puzzle leak)?

Uses DOM-level interactions (evaluate + this.click()) to bypass popup overlays.
"""
import asyncio
from playwright.async_api import async_playwright


SITE_URL = "https://serene-frangipane-7fd25b.netlify.app/"


async def get_puzzle_state(page) -> dict:
    """Check the puzzle component's state on the current page."""
    return await page.evaluate("""
        (() => {
            const text = document.body.innerText;
            const challengeMatch = text.match(/Challenge Step (\\d+)/i);
            const puzzleMatch = text.match(/(\\d+) \\+ (\\d+) = \\?/);
            const hasSolveBtn = !!document.querySelector('button') &&
                Array.from(document.querySelectorAll('button')).some(b => b.textContent.trim() === 'Solve');
            const hasNumberInput = !!document.querySelector('input[type="number"]');
            const puzzleSolved = text.includes('Puzzle solved');
            const codeRevealedMatch = text.match(/Code revealed:[\\s]*([A-Z0-9]{6})/);
            // Also look for standalone 6-char codes near the puzzle
            const allCodes = text.match(/\\b[A-Z0-9]{6}\\b/g) || [];
            return {
                url: window.location.href,
                challengeStep: challengeMatch ? parseInt(challengeMatch[1]) : null,
                puzzle: puzzleMatch ? `${puzzleMatch[1]} + ${puzzleMatch[2]} = ?` : null,
                puzzleAnswer: puzzleMatch ? parseInt(puzzleMatch[1]) + parseInt(puzzleMatch[2]) : null,
                hasSolveButton: hasSolveBtn,
                hasNumberInput: hasNumberInput,
                puzzleSolved: puzzleSolved,
                revealedCode: codeRevealedMatch ? codeRevealedMatch[1] : null,
                allCodes: allCodes,
            };
        })()
    """)


async def skip_to_step(page, step_num: int) -> None:
    """Client-side navigate to a step via pushState + popstate."""
    url = f"/step{step_num}?version=3"
    await page.evaluate(f"""
        (() => {{
            window.history.pushState({{}}, '', '{url}');
            window.dispatchEvent(new PopStateEvent('popstate', {{ state: {{}} }}));
        }})()
    """)
    await page.wait_for_timeout(2000)


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1440, "height": 720})
        page = await context.new_page()

        # Start
        await page.goto(SITE_URL, wait_until="networkidle")
        await page.click("button:has-text('START')")
        await page.wait_for_timeout(2000)
        print(f"Started on: {page.url}\n")

        # ─── TEST: Reproduce the state leak, then test if skip fixes it ───

        # 1. Go to step 17 (math puzzle step)
        print("=" * 60)
        print("Step 1: Navigate to step 17 (math puzzle)")
        print("=" * 60)
        await skip_to_step(page, 17)
        s17 = await get_puzzle_state(page)
        print(f"Step {s17['challengeStep']}: {s17['puzzle']}")
        print(f"  Solve button: {s17['hasSolveButton']}")
        print(f"  Number input: {s17['hasNumberInput']}")
        print(f"  Puzzle solved: {s17['puzzleSolved']}")

        if not s17['hasNumberInput'] or not s17['hasSolveButton']:
            print("ERROR: Step 17 doesn't have expected puzzle UI")
            await browser.close()
            return

        # 2. Solve the puzzle via DOM interactions
        print(f"\nSolving: {s17['puzzle']} -> {s17['puzzleAnswer']}")
        await page.evaluate(f"""
            (() => {{
                const input = document.querySelector('input[type="number"]');
                const nativeSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                nativeSetter.call(input, '{s17["puzzleAnswer"]}');
                input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                input.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }})()
        """)
        await page.wait_for_timeout(500)

        # Click Solve via DOM
        await page.evaluate("""
            (() => {
                const btns = Array.from(document.querySelectorAll('button'));
                const solveBtn = btns.find(b => b.textContent.trim() === 'Solve');
                if (solveBtn) solveBtn.click();
            })()
        """)
        await page.wait_for_timeout(1000)

        s17_after = await get_puzzle_state(page)
        print(f"After solving:")
        print(f"  Puzzle solved: {s17_after['puzzleSolved']}")
        print(f"  Revealed code: {s17_after['revealedCode']}")
        print(f"  All codes: {s17_after['allCodes']}")

        step17_code = s17_after['revealedCode'] or (s17_after['allCodes'][0] if s17_after['allCodes'] else None)
        print(f"  Step 17 code: {step17_code}")

        # 3. Now DO NOT submit the code — just skip to step 18 via pushState
        #    This simulates what would happen if we skip after solving
        print("\n" + "=" * 60)
        print("Step 2: Skip to step 18 via pushState (WITHOUT submitting step 17 code)")
        print("=" * 60)
        await skip_to_step(page, 18)
        s18 = await get_puzzle_state(page)
        print(f"Step {s18['challengeStep']}: {s18['puzzle']}")
        print(f"  Solve button: {s18['hasSolveButton']}")
        print(f"  Number input: {s18['hasNumberInput']}")
        print(f"  Puzzle solved: {s18['puzzleSolved']}")
        print(f"  Revealed code: {s18['revealedCode']}")
        print(f"  All codes: {s18['allCodes']}")

        if s18['hasNumberInput'] and s18['hasSolveButton']:
            print("\n>>> FRESH STATE! pushState skip gives clean puzzle component.")
            print(f"    Step 18 has its own unsolved puzzle: {s18['puzzle']}")
        elif s18['puzzleSolved']:
            print(f"\n>>> STALE STATE! Puzzle still shows 'solved' with code {s18['revealedCode']}")
            if step17_code and s18['revealedCode'] == step17_code:
                print(f"    Code {s18['revealedCode']} is from step 17 — React state leaked!")
            else:
                print(f"    Code differs from step 17 ({step17_code}) — may be step 18's own code")
        else:
            print(f"\n>>> UNEXPECTED STATE")

        # 4. Also test: submit step 17 code first, THEN let normal navigation happen
        #    This reproduces the exact bug scenario from the run
        print("\n" + "=" * 60)
        print("Step 3: Full reproduction — solve step 17, submit code, check step 18")
        print("=" * 60)

        # Go back to step 17
        await skip_to_step(page, 17)
        s17b = await get_puzzle_state(page)
        print(f"Back on step {s17b['challengeStep']}: {s17b['puzzle']}")
        print(f"  Has solve: {s17b['hasSolveButton']}, Has input: {s17b['hasNumberInput']}")
        print(f"  Puzzle solved: {s17b['puzzleSolved']}")

        if s17b['hasNumberInput'] and s17b['hasSolveButton']:
            # Solve
            await page.evaluate(f"""
                (() => {{
                    const input = document.querySelector('input[type="number"]');
                    const nativeSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    nativeSetter.call(input, '{s17b["puzzleAnswer"]}');
                    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }})()
            """)
            await page.wait_for_timeout(300)
            await page.evaluate("""
                (() => {
                    const btns = Array.from(document.querySelectorAll('button'));
                    const solveBtn = btns.find(b => b.textContent.trim() === 'Solve');
                    if (solveBtn) solveBtn.click();
                })()
            """)
            await page.wait_for_timeout(1000)

            s17b_solved = await get_puzzle_state(page)
            code = s17b_solved['revealedCode'] or (s17b_solved['allCodes'][0] if s17b_solved['allCodes'] else None)
            print(f"Solved step 17, code: {code}")

            # Submit the code via DOM
            if code:
                await page.evaluate(f"""
                    (() => {{
                        const input = document.querySelector('input[placeholder*="code"]');
                        if (!input) return 'no code input';
                        const nativeSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value').set;
                        nativeSetter.call(input, '{code}');
                        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    }})()
                """)
                await page.wait_for_timeout(300)

                # Click Submit Code via DOM
                await page.evaluate("""
                    (() => {
                        const btns = Array.from(document.querySelectorAll('button'));
                        const submitBtn = btns.find(b => b.textContent.trim() === 'Submit Code');
                        if (submitBtn) submitBtn.click();
                    })()
                """)
                await page.wait_for_timeout(3000)

                # Check where we ended up
                s_after_submit = await get_puzzle_state(page)
                print(f"\nAfter submitting step 17 code:")
                print(f"  URL: {s_after_submit['url']}")
                print(f"  Step: {s_after_submit['challengeStep']}")
                print(f"  Puzzle: {s_after_submit['puzzle']}")
                print(f"  Solve button: {s_after_submit['hasSolveButton']}")
                print(f"  Number input: {s_after_submit['hasNumberInput']}")
                print(f"  Puzzle solved: {s_after_submit['puzzleSolved']}")
                print(f"  Revealed code: {s_after_submit['revealedCode']}")

                if s_after_submit['challengeStep'] == 18:
                    if s_after_submit['puzzleSolved']:
                        print(f"\n>>> BUG REPRODUCED: Step 18 arrived with stale solved state!")
                        print(f"    Code shown: {s_after_submit['revealedCode']}, Step 17 code: {code}")

                        # 5. NOW test: can pushState skip fix it?
                        print(f"\n--- Attempting pushState skip to step 19 to recover ---")
                        await skip_to_step(page, 19)
                        s19 = await get_puzzle_state(page)
                        print(f"Step {s19['challengeStep']}: {s19['puzzle']}")
                        print(f"  Solve button: {s19['hasSolveButton']}")
                        print(f"  Number input: {s19['hasNumberInput']}")
                        print(f"  Puzzle solved: {s19['puzzleSolved']}")

                        # Also try: skip BACK to step 18 to see if it resets
                        print(f"\n--- Skip back to step 18 to see if state resets ---")
                        await skip_to_step(page, 18)
                        s18_retry = await get_puzzle_state(page)
                        print(f"Step {s18_retry['challengeStep']}: {s18_retry['puzzle']}")
                        print(f"  Solve button: {s18_retry['hasSolveButton']}")
                        print(f"  Number input: {s18_retry['hasNumberInput']}")
                        print(f"  Puzzle solved: {s18_retry['puzzleSolved']}")
                        print(f"  Revealed code: {s18_retry['revealedCode']}")

                        if s18_retry['hasNumberInput'] and s18_retry['hasSolveButton']:
                            print("\n>>> RECOVERY SUCCESS: pushState to another step then back resets puzzle state!")
                        elif s18_retry['puzzleSolved']:
                            print("\n>>> RECOVERY FAILED: Puzzle still shows stale solved state")
                        else:
                            print("\n>>> UNEXPECTED STATE after recovery attempt")
                    else:
                        print(f"\n>>> No state leak — step 18 puzzle is fresh!")
        else:
            print("Step 17 already in solved state, can't reproduce")

        print("\n" + "=" * 60)
        print("ALL TESTS DONE")
        print("=" * 60)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
