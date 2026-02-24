"""Debug script: discover what versions exist and how they affect challenges.

Clicks START multiple times to see what version= values appear,
then visits a few steps across different versions to compare.
"""
import asyncio
from playwright.async_api import async_playwright


SITE_URL = "https://serene-frangipane-7fd25b.netlify.app/"


async def skip_to_step(page, step_num: int, version: int) -> None:
    """Client-side navigate via pushState + popstate."""
    url = f"/step{step_num}?version={version}"
    await page.evaluate(f"""
        (() => {{
            window.history.pushState({{}}, '', '{url}');
            window.dispatchEvent(new PopStateEvent('popstate', {{ state: {{}} }}));
        }})()
    """)
    await page.wait_for_timeout(2000)


async def get_challenge_summary(page) -> dict:
    """Get challenge type and key details."""
    return await page.evaluate("""
        (() => {
            const text = document.body.innerText;
            const challengeMatch = text.match(/Challenge Step (\\d+)/i);
            const puzzleMatch = text.match(/(\\d+) \\+ (\\d+) = \\?/);
            const lines = text.split('\\n').map(l => l.trim()).filter(l => l.length > 0);

            // Extract instruction lines
            const instructionLines = [];
            let capturing = false;
            for (const line of lines) {
                if (line.includes('Complete the challenges') || line.includes('Challenge Step')) {
                    capturing = true;
                    continue;
                }
                if (capturing) {
                    if (line.includes('Enter 6-character code') ||
                        line.includes('Enter Code to Proceed') ||
                        line.includes('Submit Code') ||
                        line.startsWith('Section ') ||
                        line.startsWith('This is filler')) {
                        break;
                    }
                    instructionLines.push(line);
                }
            }

            const buttons = Array.from(document.querySelectorAll('button'))
                .map(b => b.textContent.trim())
                .filter(t => t.length > 0 && t.length < 50);

            const notableButtons = buttons.filter(t =>
                !['Next', 'Proceed', 'Continue', 'Keep Going', 'Go Forward',
                  'Move On', 'Advance', 'Click Here', 'Next Page', 'Next Step',
                  'Next Section', 'Continue Journey', 'Proceed Forward',
                  'Continue Reading', 'Submit Code', 'Close', 'Accept',
                  'Decline', 'Dismiss', 'Close (Fake)'].includes(t)
            );

            const codes = [...new Set((text.match(/\\b[A-Z0-9]{6}\\b/g) || []))];

            return {
                step: challengeMatch ? parseInt(challengeMatch[1]) : null,
                url: window.location.href,
                puzzle: puzzleMatch ? `${puzzleMatch[1]} + ${puzzleMatch[2]} = ?` : null,
                instructions: instructionLines.slice(0, 5).join(' | '),
                notableButtons: notableButtons.slice(0, 8),
                codes: codes.slice(0, 5),
            };
        })()
    """)


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1440, "height": 720})
        page = await context.new_page()

        # 1. Click START multiple times to see what versions we get
        print("=" * 60)
        print("PHASE 1: Click START multiple times, observe version param")
        print("=" * 60)
        versions_seen = set()
        for i in range(6):
            await page.goto(SITE_URL, wait_until="networkidle")
            await page.click("button:has-text('START')")
            await page.wait_for_timeout(1500)
            url = page.url
            version_match = None
            if "version=" in url:
                version_match = url.split("version=")[1].split("&")[0]
            print(f"  Attempt {i+1}: {url} -> version={version_match}")
            if version_match:
                versions_seen.add(version_match)

        print(f"\nVersions seen: {sorted(versions_seen)}")

        # 2. Compare a few steps across different versions
        print("\n" + "=" * 60)
        print("PHASE 2: Compare steps across versions")
        print("=" * 60)

        # First ensure we're in the SPA (click START once)
        await page.goto(SITE_URL, wait_until="networkidle")
        await page.click("button:has-text('START')")
        await page.wait_for_timeout(1500)

        # Test a few interesting steps across versions 1-5
        test_steps = [1, 5, 14, 17, 18, 20, 27]
        test_versions = range(1, 6)

        for step in test_steps:
            print(f"\n{'─' * 60}")
            print(f"Step {step}")
            print(f"{'─' * 60}")
            for version in test_versions:
                await skip_to_step(page, step, version)
                info = await get_challenge_summary(page)
                # Compact one-line summary
                puzzle_str = f" puzzle={info['puzzle']}" if info['puzzle'] else ""
                codes_str = f" codes={info['codes']}" if info['codes'] else ""
                btns_str = ', '.join(info['notableButtons'][:4])
                print(f"  v{version}: {info['instructions'][:80]}{puzzle_str}{codes_str}")
                if btns_str:
                    print(f"       buttons: [{btns_str}]")

        print("\n" + "=" * 60)
        print("DONE")
        print("=" * 60)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
