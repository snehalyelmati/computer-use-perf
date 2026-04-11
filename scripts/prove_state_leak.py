"""Definitive proof: can the leaked math puzzle step be solved?

Tests ALL possible recovery strategies on the back-to-back puzzle bug.
If no strategy can produce a fresh Solve button + number input for step 18,
then it is impossible to legitimately solve all 30 challenges in version 3
through normal browser interaction.

Strategies tested:
  A. Direct: solve step 17, submit, arrive at step 18 — check state
  B. pushState away (step 19) then back (step 18) — does React remount?
  C. pushState to step 16 (non-puzzle) then forward to step 18
  D. Refresh-like: history.go(0) or location.reload()
  E. Force React remount via removing/re-adding the root DOM node
  F. Direct JS state manipulation to reset useState
"""
import asyncio
import json
from playwright.async_api import async_playwright


SITE_URL = "https://serene-frangipane-7fd25b.netlify.app/"

# All version -> (first_puzzle_step, second_puzzle_step) pairs
VERSION_PAIRS = {
    1: (19, 20),
    2: (18, 19),
    3: (17, 18),
    4: (18, 19),
    5: (19, 20),
}


async def get_puzzle_state(page) -> dict:
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
            const allCodes = text.match(/\\b[A-Z0-9]{6}\\b/g) || [];
            const hasCodeInput = !!document.querySelector('input[placeholder*="code"]');
            const hasSubmitBtn = Array.from(document.querySelectorAll('button')).some(
                b => b.textContent.trim() === 'Submit Code');
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
                hasCodeInput: hasCodeInput,
                hasSubmitBtn: hasSubmitBtn,
            };
        })()
    """)


async def skip_to_step(page, step_num: int, version: int = 3) -> None:
    url = f"/step{step_num}?version={version}"
    await page.evaluate(f"""
        (() => {{
            window.history.pushState({{}}, '', '{url}');
            window.dispatchEvent(new PopStateEvent('popstate', {{ state: {{}} }}));
        }})()
    """)
    await page.wait_for_timeout(2000)


async def solve_puzzle(page, answer: int) -> None:
    """Solve the math puzzle via DOM manipulation."""
    await page.evaluate(f"""
        (() => {{
            const input = document.querySelector('input[type="number"]');
            if (!input) return;
            const nativeSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            nativeSetter.call(input, '{answer}');
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


async def submit_code(page, code: str) -> None:
    """Submit a 6-char code via the code input + Submit Code button."""
    await page.evaluate(f"""
        (() => {{
            const input = document.querySelector('input[placeholder*="code"]');
            if (!input) return;
            const nativeSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            nativeSetter.call(input, '{code}');
            input.dispatchEvent(new Event('input', {{ bubbles: true }}));
            input.dispatchEvent(new Event('change', {{ bubbles: true }}));
        }})()
    """)
    await page.wait_for_timeout(300)
    await page.evaluate("""
        (() => {
            const btns = Array.from(document.querySelectorAll('button'));
            const submitBtn = btns.find(b => b.textContent.trim() === 'Submit Code');
            if (submitBtn) submitBtn.click();
        })()
    """)
    await page.wait_for_timeout(2000)


def print_state(label: str, state: dict) -> None:
    print(f"  [{label}]")
    print(f"    Step: {state['challengeStep']}  URL: {state['url']}")
    print(f"    Puzzle: {state['puzzle']}")
    print(f"    Solve button: {state['hasSolveButton']}  Number input: {state['hasNumberInput']}")
    print(f"    Puzzle solved: {state['puzzleSolved']}  Code: {state['revealedCode']}")
    solvable = state['hasSolveButton'] and state['hasNumberInput']
    print(f"    ==> SOLVABLE: {solvable}")
    return solvable


async def main() -> None:
    version = 3
    first_step, leaked_step = VERSION_PAIRS[version]

    print(f"Testing version {version}: steps {first_step} -> {leaked_step}")
    print(f"=" * 70)

    results = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 720})
        page = await context.new_page()

        await page.goto(SITE_URL, wait_until="networkidle")
        await page.click("button:has-text('START')")
        await page.wait_for_timeout(2000)

        # ─── STRATEGY A: Direct navigation (reproduce the bug) ───
        print(f"\n{'─'*70}")
        print(f"STRATEGY A: Solve step {first_step}, submit code, check step {leaked_step}")
        print(f"{'─'*70}")

        await skip_to_step(page, first_step, version)
        s = await get_puzzle_state(page)
        print_state(f"Step {first_step} before solving", s)

        if s['hasSolveButton'] and s['hasNumberInput']:
            await solve_puzzle(page, s['puzzleAnswer'])
            s_solved = await get_puzzle_state(page)
            code17 = s_solved['revealedCode']
            print(f"  Solved step {first_step}, code = {code17}")

            await submit_code(page, code17)
            s_leaked = await get_puzzle_state(page)
            solvable_a = print_state(f"Step {leaked_step} after normal navigation", s_leaked)
            results['A_direct'] = {
                'solvable': solvable_a,
                'puzzleSolved': s_leaked['puzzleSolved'],
                'stale_code': s_leaked.get('revealedCode') == code17,
                'code_shown': s_leaked.get('revealedCode'),
            }
        else:
            print(f"  ERROR: Step {first_step} not in expected state")
            results['A_direct'] = {'error': 'first step not solvable'}

        # ─── STRATEGY B: pushState away (step+1) then back ───
        print(f"\n{'─'*70}")
        print(f"STRATEGY B: pushState to step {leaked_step+1}, then back to step {leaked_step}")
        print(f"{'─'*70}")

        await skip_to_step(page, leaked_step + 1, version)
        s_away = await get_puzzle_state(page)
        print_state(f"Step {leaked_step+1} (away)", s_away)

        await skip_to_step(page, leaked_step, version)
        s_back = await get_puzzle_state(page)
        solvable_b = print_state(f"Step {leaked_step} after skip away+back", s_back)
        results['B_skip_away_back'] = {
            'solvable': solvable_b,
            'puzzleSolved': s_back['puzzleSolved'],
        }

        # ─── STRATEGY C: pushState to a non-puzzle step then to leaked step ───
        print(f"\n{'─'*70}")
        print(f"STRATEGY C: pushState to step {first_step-1} (non-puzzle), then to step {leaked_step}")
        print(f"{'─'*70}")

        await skip_to_step(page, first_step - 1, version)
        s_non = await get_puzzle_state(page)
        print_state(f"Step {first_step-1} (non-puzzle)", s_non)

        await skip_to_step(page, leaked_step, version)
        s_c = await get_puzzle_state(page)
        solvable_c = print_state(f"Step {leaked_step} after non-puzzle detour", s_c)
        results['C_non_puzzle_detour'] = {
            'solvable': solvable_c,
            'puzzleSolved': s_c['puzzleSolved'],
        }

        # If strategy B or C recovered, try solving the puzzle
        if solvable_b or solvable_c:
            print(f"\n  >>> RECOVERY POSSIBLE — attempting to solve step {leaked_step}")
            s_now = await get_puzzle_state(page)
            if s_now['hasSolveButton'] and s_now['hasNumberInput']:
                await solve_puzzle(page, s_now['puzzleAnswer'])
                s_after = await get_puzzle_state(page)
                print(f"  After solving: code = {s_after['revealedCode']}")
                if s_after['revealedCode'] and s_after['revealedCode'] != code17:
                    print(f"  >>> DIFFERENT CODE from step {first_step}! This is step {leaked_step}'s own code.")
                    results['recovery_solve'] = {
                        'success': True,
                        'code': s_after['revealedCode'],
                        'different_from_first': True,
                    }

        # ─── STRATEGY D: Full page reload ───
        print(f"\n{'─'*70}")
        print(f"STRATEGY D: location.reload() on step {leaked_step}")
        print(f"{'─'*70}")

        # First reproduce the bug state again
        await skip_to_step(page, first_step, version)
        s = await get_puzzle_state(page)
        if s['hasSolveButton'] and s['hasNumberInput']:
            await solve_puzzle(page, s['puzzleAnswer'])
            s_solved = await get_puzzle_state(page)
            await submit_code(page, s_solved['revealedCode'])

        # Now try reload
        try:
            await page.reload(wait_until="networkidle", timeout=5000)
            await page.wait_for_timeout(2000)
            s_d = await get_puzzle_state(page)
            print_state(f"Step {leaked_step} after reload", s_d)
            results['D_reload'] = {
                'loaded': True,
                'solvable': s_d['hasSolveButton'] and s_d['hasNumberInput'],
                'note': 'Netlify may 404 on direct step URLs',
            }
        except Exception as e:
            print(f"  Reload failed: {e}")
            results['D_reload'] = {'loaded': False, 'error': str(e)}

        # ─── STRATEGY E: Re-navigate from start and solve naturally ───
        print(f"\n{'─'*70}")
        print(f"STRATEGY E: Fresh start, skip directly to step {leaked_step} (no prior puzzle)")
        print(f"{'─'*70}")

        # Start over
        await page.goto(SITE_URL, wait_until="networkidle")
        await page.click("button:has-text('START')")
        await page.wait_for_timeout(2000)

        await skip_to_step(page, leaked_step, version)
        s_e = await get_puzzle_state(page)
        solvable_e = print_state(f"Step {leaked_step} without solving step {first_step} first", s_e)
        results['E_fresh_skip'] = {
            'solvable': solvable_e,
            'note': f'Skipped directly to step {leaked_step} without solving step {first_step}',
        }

        await browser.close()

    # ─── SUMMARY ───
    print(f"\n{'='*70}")
    print("SUMMARY OF ALL STRATEGIES")
    print(f"{'='*70}")
    print(json.dumps(results, indent=2))

    any_recovery = any(
        v.get('solvable', False)
        for k, v in results.items()
        if k != 'A_direct' and k != 'E_fresh_skip'
    )

    print(f"\n{'='*70}")
    if results.get('A_direct', {}).get('stale_code'):
        print("CONFIRMED: React state leak is REAL")
        print(f"  Step {leaked_step} shows stale code from step {first_step}")
    if any_recovery:
        print(f"RECOVERY IS POSSIBLE via pushState detour")
        print(f"  An agent CAN work around the bug, but must detect the stale state")
        print(f"  and perform pushState navigation to a non-puzzle step and back")
    else:
        print(f"NO RECOVERY FOUND through browser interaction")
        print(f"  It appears IMPOSSIBLE to solve all 30 challenges in version {version}")
        print(f"  without the site fixing the React key bug")
    print(f"{'='*70}")


if __name__ == "__main__":
    asyncio.run(main())
