"""Debug script: enumerate all 30 challenges via pushState skip.

Opens the site, clicks START, then walks through every step
collecting the challenge type, instructions, interactive elements,
and any visible codes. Outputs a summary table at the end.
"""
import asyncio
import json
from playwright.async_api import async_playwright


SITE_URL = "https://serene-frangipane-7fd25b.netlify.app/"
MAX_STEPS = 30


async def skip_to_step(page, step_num: int) -> None:
    """Client-side navigate via pushState + popstate."""
    url = f"/step{step_num}?version=3"
    await page.evaluate(f"""
        (() => {{
            window.history.pushState({{}}, '', '{url}');
            window.dispatchEvent(new PopStateEvent('popstate', {{ state: {{}} }}));
        }})()
    """)
    await page.wait_for_timeout(2000)


async def capture_challenge_info(page, step_num: int) -> dict:
    """Capture detailed info about the current challenge."""
    return await page.evaluate("""
        (() => {
            const text = document.body.innerText;
            const lines = text.split('\\n').map(l => l.trim()).filter(l => l.length > 0);

            // Challenge step confirmation
            const challengeMatch = text.match(/Challenge Step (\\d+)/i);
            const stepOfMatch = text.match(/Step (\\d+) of 30/i);

            // Find the challenge instruction block — usually after "Complete the challenges"
            // and before the code input area
            const instructionLines = [];
            let capturing = false;
            for (const line of lines) {
                if (line.includes('Complete the challenges') || line.includes('Challenge Step')) {
                    capturing = true;
                    continue;
                }
                if (capturing) {
                    // Stop at code input / submit area / filler content
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

            // Interactive elements summary
            const buttons = Array.from(document.querySelectorAll('button')).map(b => ({
                text: b.textContent.trim().substring(0, 60),
                disabled: b.disabled,
                type: b.type || '',
            }));

            const inputs = Array.from(document.querySelectorAll('input')).map(i => ({
                type: i.type,
                placeholder: i.placeholder || '',
                value: i.value || '',
            }));

            // Specific challenge indicators
            const hasPuzzle = text.includes('Puzzle Challenge') || /\\d+ \\+ \\d+ = \\?/.test(text);
            const puzzleMatch = text.match(/(\\d+) \\+ (\\d+) = \\?/);
            const hasCountdown = text.includes('Delayed Reveal') || text.includes('countdown') || text.includes('waiting');
            const hasHover = text.includes('Hover') || text.includes('hover');
            const hasDrag = text.includes('Drag') || text.includes('drag');
            const hasKeyboard = text.includes('keyboard') || text.includes('Key Sequence') || text.includes('key sequence');
            const hasScroll = text.includes('Scroll') || text.includes('scroll');
            const hasClick = text.includes('Click to Reveal') || text.includes('click the button');
            const hasAudio = text.includes('Audio') || text.includes('audio') || text.includes('Play Audio');
            const hasVideo = text.includes('Video') || text.includes('video') || text.includes('Frame');
            const hasBase64 = text.includes('Base64') || text.includes('base64') || text.includes('REVDT');
            const hasMemory = text.includes('Memory') || text.includes('memory') || text.includes('disappear');
            const hasCapture = text.includes('Capture') || text.includes('capture');
            const hasMultiAction = text.includes('Multi-Action') || text.includes('multi-action') || text.includes('Complete all');
            const hasScatter = text.includes('Scatter') || text.includes('scatter') || text.includes('scattered');
            const hasMaze = text.includes('Maze') || text.includes('maze') || text.includes('navigate');
            const hasSequence = text.includes('Sequence') || text.includes('sequence');

            // Puzzle solved state (stale?)
            const puzzleSolved = text.includes('Puzzle solved');
            const codeRevealedMatch = text.match(/Code revealed:[\\s]*([A-Z0-9]{6})/);

            // All 6-char codes visible
            const allCodes = [...new Set((text.match(/\\b[A-Z0-9]{6}\\b/g) || []))];

            // Count decoy-like navigation buttons
            const navButtonTexts = ['Next', 'Proceed', 'Continue', 'Keep Going', 'Go Forward',
                'Move On', 'Advance', 'Click Here', 'Next Page', 'Next Step',
                'Next Section', 'Continue Journey', 'Proceed Forward', 'Continue Reading'];
            const decoyButtons = buttons.filter(b =>
                navButtonTexts.some(t => b.text === t) && b.type !== 'submit'
            );

            return {
                step: challengeMatch ? parseInt(challengeMatch[1]) : null,
                stepOf: stepOfMatch ? parseInt(stepOfMatch[1]) : null,
                url: window.location.href,
                instructions: instructionLines.slice(0, 10).join(' | '),
                challengeIndicators: {
                    puzzle: hasPuzzle,
                    countdown: hasCountdown,
                    hover: hasHover,
                    drag: hasDrag,
                    keyboard: hasKeyboard,
                    scroll: hasScroll,
                    clickReveal: hasClick,
                    audio: hasAudio,
                    video: hasVideo,
                    base64: hasBase64,
                    memory: hasMemory,
                    capture: hasCapture,
                    multiAction: hasMultiAction,
                    scatter: hasScatter,
                    maze: hasMaze,
                    sequence: hasSequence,
                },
                puzzleInfo: hasPuzzle ? {
                    equation: puzzleMatch ? `${puzzleMatch[1]} + ${puzzleMatch[2]} = ?` : null,
                    solved: puzzleSolved,
                    revealedCode: codeRevealedMatch ? codeRevealedMatch[1] : null,
                    hasSolveButton: buttons.some(b => b.text === 'Solve'),
                    hasNumberInput: inputs.some(i => i.type === 'number'),
                } : null,
                interactiveElements: {
                    buttonCount: buttons.length,
                    inputCount: inputs.length,
                    decoyButtonCount: decoyButtons.length,
                    notableButtons: buttons
                        .filter(b => !navButtonTexts.some(t => b.text === t) &&
                                     b.text !== 'Submit Code' && b.text.length > 0 &&
                                     b.text.length < 40)
                        .map(b => b.text)
                        .slice(0, 10),
                    notableInputs: inputs
                        .filter(i => i.placeholder !== 'Enter 6-character code')
                        .map(i => `${i.type}${i.placeholder ? ': ' + i.placeholder : ''}`)
                        .slice(0, 5),
                },
                visibleCodes: allCodes.slice(0, 5),
            };
        })()
    """)


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

        all_challenges = []

        for step in range(1, MAX_STEPS + 1):
            print(f"{'─' * 60}")
            print(f"Step {step}")
            print(f"{'─' * 60}")

            await skip_to_step(page, step)
            info = await capture_challenge_info(page, step)

            if info['step'] != step:
                print(f"  WARNING: Expected step {step}, got step {info['step']}")

            # Determine challenge type from indicators
            indicators = info['challengeIndicators']
            active = [k for k, v in indicators.items() if v]
            challenge_type = ', '.join(active) if active else 'unknown'

            print(f"  Type: {challenge_type}")
            print(f"  Instructions: {info['instructions'][:120]}")
            print(f"  Buttons: {info['interactiveElements']['buttonCount']} total, "
                  f"{info['interactiveElements']['decoyButtonCount']} decoy")
            if info['interactiveElements']['notableButtons']:
                print(f"  Notable buttons: {info['interactiveElements']['notableButtons']}")
            if info['interactiveElements']['notableInputs']:
                print(f"  Notable inputs: {info['interactiveElements']['notableInputs']}")
            if info['puzzleInfo']:
                pi = info['puzzleInfo']
                print(f"  Puzzle: {pi['equation']} | solved={pi['solved']} | "
                      f"solve_btn={pi['hasSolveButton']} | num_input={pi['hasNumberInput']}")
            if info['visibleCodes']:
                print(f"  Visible codes: {info['visibleCodes']}")

            all_challenges.append({
                'step': step,
                'type': challenge_type,
                'instructions': info['instructions'],
                'notable_buttons': info['interactiveElements']['notableButtons'],
                'notable_inputs': info['interactiveElements']['notableInputs'],
                'button_count': info['interactiveElements']['buttonCount'],
                'decoy_count': info['interactiveElements']['decoyButtonCount'],
                'puzzle_info': info['puzzleInfo'],
                'visible_codes': info['visibleCodes'],
            })

        # Summary table
        print(f"\n{'=' * 80}")
        print(f"CHALLENGE SUMMARY")
        print(f"{'=' * 80}")
        print(f"{'Step':>4} | {'Type':<30} | {'Buttons':>4} | {'Decoy':>5} | {'Notable Elements'}")
        print(f"{'─' * 4} | {'─' * 30} | {'─' * 4} | {'─' * 5} | {'─' * 40}")

        for c in all_challenges:
            notable = ', '.join(c['notable_buttons'][:3])
            if c['notable_inputs']:
                notable += ' | inputs: ' + ', '.join(c['notable_inputs'][:2])
            print(f"{c['step']:>4} | {c['type']:<30} | {c['button_count']:>4} | "
                  f"{c['decoy_count']:>5} | {notable[:50]}")

        # Save full data
        out_path = "logs/challenge_enumeration.json"
        with open(out_path, "w") as f:
            json.dump(all_challenges, f, indent=2)
        print(f"\nFull data saved to {out_path}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
