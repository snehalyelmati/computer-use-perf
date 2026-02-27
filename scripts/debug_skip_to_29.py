"""Skip to step 29, solve it, advance to step 30, and watch the final-step fix.

Version 3 step 29 is a WebSocket challenge: click Connect, wait for the
simulated data, click Reveal Code, then submit the code to advance.
On step 30 the off-by-one bug makes Reveal Code return null —
_fix_final_step_code_bug() should fire immediately and navigate to /finish.

Usage:
    uv run scripts/debug_skip_to_29.py
"""
import asyncio
import re
import sys

sys.path.insert(0, ".")

from playwright.async_api import async_playwright

from src.agent.core.agent import _fix_final_step_code_bug

SITE_URL = "https://serene-frangipane-7fd25b.netlify.app/"
VERSION = 3
CODE_RE = re.compile(r"\b[A-Z0-9]{6}\b")


async def skip_to_step(page, step: int) -> None:
    await page.evaluate(f"""(() => {{
        window.history.pushState({{}}, '', '/step{step}?version={VERSION}');
        window.dispatchEvent(new PopStateEvent('popstate', {{ state: {{}} }}));
    }})()""")
    await page.wait_for_timeout(2000)


async def get_raw_text(page) -> list[str]:
    text = await page.evaluate("document.body.innerText")
    return [line.strip() for line in text.split("\n") if line.strip()]


async def dump_page(page, label: str, max_lines: int = 12) -> list[str]:
    """Print URL + page text."""
    raw = await get_raw_text(page)
    print(f"\n--- {label} ---")
    print(f"  URL: {page.url}")
    for line in raw[:max_lines]:
        print(f"  | {line}")
    if len(raw) > max_lines:
        print(f"  | ... ({len(raw) - max_lines} more lines)")
    return raw


async def click_button(page, text_match: str) -> bool:
    """Click the first button whose text contains text_match (case-insensitive)."""
    return await page.evaluate(f"""(() => {{
        const btn = Array.from(document.querySelectorAll('button'))
            .find(b => b.textContent.trim().toLowerCase().includes(
                {text_match.lower()!r}));
        if (!btn) return false;
        btn.click();
        return true;
    }})()""")


async def submit_code(page, code: str) -> str:
    """Enter a code into the input and click Submit Code. Returns status string."""
    return await page.evaluate(f"""(() => {{
        const input = document.querySelector('input[placeholder*="code" i]')
                   || document.querySelector('input[type="text"]');
        if (!input) return 'no_input';
        const nativeSetter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value').set;
        nativeSetter.call(input, '{code}');
        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
        input.dispatchEvent(new Event('change', {{ bubbles: true }}));

        const btn = Array.from(document.querySelectorAll('button'))
            .find(b => b.textContent.trim() === 'Submit Code');
        if (!btn) return 'no_submit_btn';
        btn.click();
        return 'submitted';
    }})()""")


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1440, "height": 720})
        page = await context.new_page()

        # ── Start ──
        print("Starting challenge site...")
        await page.goto(SITE_URL, wait_until="networkidle")
        await page.click("button:has-text('START')")
        await page.wait_for_timeout(2000)

        # ── Skip to step 29 ──
        print("\n" + "=" * 60)
        print("STEP 29 (WebSocket challenge, v3)")
        print("=" * 60)
        await skip_to_step(page, 29)
        await dump_page(page, "Step 29 loaded")

        # 1. Click Connect
        print("\n  [1] Clicking 'Connect'...")
        ok = await click_button(page, "connect")
        print(f"      Clicked: {ok}")
        if not ok:
            print("      ABORT: No Connect button")
            await browser.close()
            sys.exit(1)

        # 2. Wait for simulated WebSocket to finish
        print("  [2] Waiting for WebSocket simulation (6s)...")
        await page.wait_for_timeout(6000)
        await dump_page(page, "After WebSocket connect")

        # 3. Click Reveal Code
        print("\n  [3] Clicking 'Reveal Code'...")
        ok = await click_button(page, "reveal code")
        print(f"      Clicked: {ok}")
        await page.wait_for_timeout(2000)
        raw = await dump_page(page, "After Reveal Code")

        # 4. Extract the code
        joined = " ".join(raw)
        codes = CODE_RE.findall(joined)
        print(f"\n      Codes found: {codes}")

        if not codes:
            print("      ABORT: No code found after Reveal Code")
            await browser.close()
            sys.exit(1)

        code = codes[-1]
        print(f"      Using code: {code}")

        # 5. Submit code to advance to step 30
        print(f"\n  [4] Submitting code '{code}'...")
        result = await submit_code(page, code)
        print(f"      Result: {result}")

        if result != "submitted":
            print(f"      ABORT: Could not submit ({result})")
            await browser.close()
            sys.exit(1)

        await page.wait_for_timeout(3000)

        # ── Step 30 ──
        print("\n" + "=" * 60)
        print("STEP 30 (final step — off-by-one bug)")
        print("=" * 60)
        raw = await dump_page(page, "Step 30 loaded")

        # Confirm we're on step 30
        if "step30" not in page.url.lower() and "step 30" not in " ".join(raw).lower():
            print("\n  WARNING: May not be on step 30!")
            print(f"  URL: {page.url}")

        # Try Reveal Code — should produce no code (the bug)
        print("\n  Clicking 'Reveal Code' (expect null due to off-by-one)...")
        ok = await click_button(page, "reveal code")
        print(f"  Clicked: {ok}")
        await page.wait_for_timeout(2000)
        raw = await dump_page(page, "After Reveal Code on step 30")

        page_codes = CODE_RE.findall(" ".join(raw))
        print(f"\n  Codes visible: {page_codes}")
        if not page_codes:
            print("  CONFIRMED: No code revealed (off-by-one bug)")
        else:
            print("  NOTE: A code appeared — bug may not be present")

        # ── Test the fix ──
        print("\n" + "=" * 60)
        print("TESTING _fix_final_step_code_bug()")
        print("=" * 60)
        raw = await get_raw_text(page)
        print(f"  URL before fix: {page.url}")
        fixed = await _fix_final_step_code_bug(page, raw)
        print(f"  Fix fired: {fixed}")
        print(f"  URL after fix:  {page.url}")

        if fixed and "/finish" in page.url:
            await page.wait_for_timeout(1500)
            await dump_page(page, "Finish page")
            print("\n  SUCCESS: Fix navigated to /finish immediately")
        elif fixed:
            print(f"\n  UNEXPECTED: Fix fired but URL is {page.url}")
        else:
            print("\n  FAILURE: Fix did not fire on step 30 of 30")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
