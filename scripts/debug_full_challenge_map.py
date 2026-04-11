"""Enumerate all 30 challenges across all versions to build a complete challenge map.

Walks through steps 1-30 for versions 1-5, capturing challenge type,
instructions, interactive elements, and key details.
Outputs a JSON map and a readable summary.
"""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright


SITE_URL = "https://serene-frangipane-7fd25b.netlify.app/"
MAX_STEPS = 30
VERSIONS = [1, 2, 3, 4, 5]


async def skip_to_step(page, step_num: int, version: int) -> None:
    url = f"/step{step_num}?version={version}"
    await page.evaluate(f"""
        (() => {{
            window.history.pushState({{}}, '', '{url}');
            window.dispatchEvent(new PopStateEvent('popstate', {{ state: {{}} }}));
        }})()
    """)
    await page.wait_for_timeout(1500)


async def capture_challenge(page) -> dict:
    """Capture comprehensive challenge info."""
    return await page.evaluate("""
        (() => {
            const text = document.body.innerText;
            const challengeMatch = text.match(/Challenge Step (\\d+)/i);
            const stepOfMatch = text.match(/Step (\\d+) of 30/i);
            const puzzleMatch = text.match(/(\\d+) \\+ (\\d+) = \\?/);

            // Extract instruction block
            const lines = text.split('\\n').map(l => l.trim()).filter(l => l.length > 0);
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

            // Classify challenge type
            let challengeType = 'unknown';
            const instr = instructionLines.join(' ');

            if (instr.includes('Scroll to Reveal') || instr.includes('Scroll down at least'))
                challengeType = 'scroll_reveal';
            else if (instr.includes('Delayed Reveal') || instr.includes('code will appear after waiting'))
                challengeType = 'delayed_reveal';
            else if (instr.includes('Challenge Code for Step') || instr.includes('Enter this code below'))
                challengeType = 'visible_code';
            else if (instr.includes('Hidden DOM Challenge'))
                challengeType = 'hidden_dom';
            else if (instr.includes('Click to Reveal'))
                challengeType = 'click_reveal';
            else if (instr.includes('Memory Challenge'))
                challengeType = 'memory';
            else if (instr.includes('Hover Challenge') || instr.includes('Hover over'))
                challengeType = 'hover';
            else if (instr.includes('Drag-and-Drop') || instr.includes('Drag'))
                challengeType = 'drag_and_drop';
            else if (instr.includes('Keyboard Sequence') || instr.includes('Key Sequence'))
                challengeType = 'keyboard_sequence';
            else if (instr.includes('Audio Challenge'))
                challengeType = 'audio';
            else if (instr.includes('Video Challenge'))
                challengeType = 'video';
            else if (instr.includes('Split Parts') || instr.includes('scattered'))
                challengeType = 'scatter';
            else if (instr.includes('Encoded Code') || instr.includes('Base64'))
                challengeType = 'base64';
            else if (instr.includes('Rotating Code') || instr.includes('Capture'))
                challengeType = 'rotating_capture';
            else if (instr.includes('Sequence Challenge') || instr.includes('Complete all'))
                challengeType = 'multi_action';
            else if (instr.includes('Puzzle Challenge') || puzzleMatch)
                challengeType = 'math_puzzle';
            else if (instr.includes('Multi-Tab'))
                challengeType = 'multi_tab';
            else if (instr.includes('Gesture Challenge') || instr.includes('Draw'))
                challengeType = 'canvas_draw';
            else if (instr.includes('Service Worker'))
                challengeType = 'service_worker';
            else if (instr.includes('Mutation Challenge'))
                challengeType = 'dom_mutation';
            else if (instr.includes('Recursive Iframe') || instr.includes('nested levels'))
                challengeType = 'recursive_iframe';
            else if (instr.includes('Shadow DOM'))
                challengeType = 'shadow_dom';
            else if (instr.includes('WebSocket'))
                challengeType = 'websocket';

            // Notable interactive elements
            const allButtons = Array.from(document.querySelectorAll('button'))
                .map(b => b.textContent.trim())
                .filter(t => t.length > 0 && t.length < 50);

            const navLabels = ['Next', 'Proceed', 'Continue', 'Keep Going', 'Go Forward',
                'Move On', 'Advance', 'Click Here', 'Next Page', 'Next Step',
                'Next Section', 'Continue Journey', 'Proceed Forward',
                'Continue Reading', 'Submit Code', 'Close', 'Accept',
                'Decline', 'Dismiss', 'Close (Fake)'];

            const notableButtons = allButtons.filter(t => !navLabels.includes(t));

            const inputs = Array.from(document.querySelectorAll('input'))
                .filter(i => i.placeholder !== 'Enter 6-character code')
                .map(i => `${i.type}${i.placeholder ? ':' + i.placeholder : ''}`);

            const codes = [...new Set((text.match(/\\b[A-Z0-9]{6}\\b/g) || []))];

            // Puzzle-specific
            const puzzleSolved = text.includes('Puzzle solved');
            const hasSolveBtn = allButtons.includes('Solve');
            const hasNumberInput = !!document.querySelector('input[type="number"]');

            // Canvas-specific
            const hasCanvas = !!document.querySelector('canvas');
            const drawShape = text.match(/Draw a (\\w+)/i);

            // Iframe-specific
            const hasIframe = !!document.querySelector('iframe');
            const depthMatch = text.match(/depth: (\\d+)\\/?(\\d+)?/i);

            // Keyboard sequence details
            const seqMatch = text.match(/Required sequence:\\s*([^\\n]+)/);

            return {
                step: challengeMatch ? parseInt(challengeMatch[1]) : null,
                challengeType,
                instructions: instructionLines.slice(0, 8).join(' | '),
                puzzle: puzzleMatch ? {
                    equation: `${puzzleMatch[1]}+${puzzleMatch[2]}`,
                    answer: parseInt(puzzleMatch[1]) + parseInt(puzzleMatch[2]),
                    solved: puzzleSolved,
                    hasSolveBtn,
                    hasNumberInput,
                } : null,
                canvas: hasCanvas ? {
                    shape: drawShape ? drawShape[1] : null,
                } : null,
                iframe: hasIframe || depthMatch ? {
                    depth: depthMatch ? depthMatch[0] : null,
                } : null,
                keyboardSeq: seqMatch ? seqMatch[1].trim() : null,
                notableButtons: notableButtons.slice(0, 8),
                notableInputs: inputs.slice(0, 5),
                visibleCodes: codes.slice(0, 3),
                totalButtons: allButtons.length,
            };
        })()
    """)


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1440, "height": 720})
        page = await context.new_page()

        # Initialize SPA
        await page.goto(SITE_URL, wait_until="networkidle")
        await page.click("button:has-text('START')")
        await page.wait_for_timeout(2000)

        # Full map: version -> step -> challenge info
        full_map = {}

        for version in VERSIONS:
            print(f"\n{'=' * 70}")
            print(f"VERSION {version}")
            print(f"{'=' * 70}")
            full_map[version] = {}

            for step in range(1, MAX_STEPS + 1):
                await skip_to_step(page, step, version)
                info = await capture_challenge(page)

                full_map[version][step] = info

                # Compact output
                extra = ""
                if info['puzzle']:
                    extra = f" [{info['puzzle']['equation']}]"
                elif info['canvas']:
                    extra = f" [draw {info['canvas']['shape']}]"
                elif info['keyboardSeq']:
                    extra = f" [keys: {info['keyboardSeq'][:30]}]"
                elif info['iframe']:
                    extra = f" [{info['iframe']['depth']}]"

                btns = ', '.join(info['notableButtons'][:3])
                print(f"  Step {step:>2}: {info['challengeType']:<20}{extra:<25} btns=[{btns}]")

        # Save full data
        out_path = Path("logs/full_challenge_map.json")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(full_map, f, indent=2, default=str)
        print(f"\nFull map saved to {out_path}")

        # Print comparison table
        print(f"\n{'=' * 120}")
        print("CHALLENGE TYPE MAP (step x version)")
        print(f"{'=' * 120}")

        # Collect all unique challenge types for legend
        all_types = set()
        for v in VERSIONS:
            for s in range(1, MAX_STEPS + 1):
                all_types.add(full_map[v][s]['challengeType'])

        # Short labels for types
        short = {
            'scroll_reveal': 'SCROLL',
            'delayed_reveal': 'DELAY',
            'visible_code': 'VISIBLE',
            'hidden_dom': 'HIDDOM',
            'click_reveal': 'CLICK',
            'memory': 'MEMORY',
            'hover': 'HOVER',
            'drag_and_drop': 'DRAG',
            'keyboard_sequence': 'KEYS',
            'audio': 'AUDIO',
            'video': 'VIDEO',
            'scatter': 'SCATTER',
            'base64': 'BASE64',
            'rotating_capture': 'CAPTURE',
            'multi_action': 'MULTI',
            'math_puzzle': 'PUZZLE',
            'multi_tab': 'TABS',
            'canvas_draw': 'CANVAS',
            'service_worker': 'SVCWKR',
            'dom_mutation': 'MUTATE',
            'recursive_iframe': 'IFRAME',
            'shadow_dom': 'SHADOW',
            'websocket': 'WEBSKT',
            'unknown': '???',
        }

        header = f"{'Step':>4} |"
        for v in VERSIONS:
            header += f" {'v'+str(v):^10} |"
        print(header)
        print(f"{'─' * 4} |" + (f" {'─' * 10} |" * len(VERSIONS)))

        for s in range(1, MAX_STEPS + 1):
            row = f"{s:>4} |"
            for v in VERSIONS:
                ctype = full_map[v][s]['challengeType']
                label = short.get(ctype, ctype[:8])
                # Mark puzzle steps
                if ctype == 'math_puzzle':
                    eq = full_map[v][s]['puzzle']['equation'] if full_map[v][s]['puzzle'] else ''
                    label = f"PZL{eq}"
                row += f" {label:^10} |"
            print(row)

        # Find back-to-back puzzles (state leak risk)
        print(f"\n{'=' * 70}")
        print("BACK-TO-BACK PUZZLE RISK (React state leak)")
        print(f"{'=' * 70}")
        for v in VERSIONS:
            prev_puzzle = False
            risks = []
            for s in range(1, MAX_STEPS + 1):
                is_puzzle = full_map[v][s]['challengeType'] == 'math_puzzle'
                if is_puzzle and prev_puzzle:
                    prev_eq = full_map[v][s-1]['puzzle']['equation'] if full_map[v][s-1]['puzzle'] else '?'
                    curr_eq = full_map[v][s]['puzzle']['equation'] if full_map[v][s]['puzzle'] else '?'
                    risks.append(f"steps {s-1}({prev_eq})->{s}({curr_eq})")
                prev_puzzle = is_puzzle
            if risks:
                print(f"  v{v}: RISK at {', '.join(risks)}")
            else:
                print(f"  v{v}: No back-to-back puzzles")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
