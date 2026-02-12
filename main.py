"""
Fast browser automation agent for web challenges.
Target: 30 challenges in <5 minutes.
"""

import asyncio
import os
import time
from groq import AsyncGroq
from playwright.async_api import async_playwright

from src.agent.action_executor import execute
from src.agent.config import DEFAULT_BASE_URL, LOG_FILE, STUCK_THRESHOLD
from src.agent.content_extraction import extract_structured_content
from src.agent.element_utils import extract_elements, format_context
from src.agent.llm_agents import analyze_overview, llm_decide
from src.agent.logging_utils import log
from src.agent.prompts import OVERVIEW_PROMPT, SYSTEM_PROMPT
from src.agent.state_utils import compute_state_hash

async def run_agent(base_url: str = DEFAULT_BASE_URL):
    """Run the agent through all challenges."""

    client = AsyncGroq(api_key=os.environ.get("GROQ_API_KEY"))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        page = await context.new_page()

        # Disable animations
        await page.add_init_script("""
            const style = document.createElement('style');
            style.textContent = '*, *::before, *::after { animation-duration: 0s !important; transition-duration: 0s !important; }';
            document.head.appendChild(style);
        """)

        total_start = time.time()

        # Navigate and start
        log(f"Navigating to {base_url}...")
        await page.goto(base_url, wait_until="domcontentloaded")
        await asyncio.sleep(0.5)

        try:
            await page.click("text=Start", timeout=5000)
            log("Clicked Start!")
        except:
            log("No Start button")

        await asyncio.sleep(0.3)

        # Agent loop
        challenge = 1
        prev_url = ""
        # Challenge-level memory for both LLMs
        action_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        overview_messages = [{"role": "system", "content": OVERVIEW_PROMPT}]
        challenge_start = time.time()
        last_action = None
        last_result = None
        challenge_summary = ""  # Persistent summary that survives memory truncation
        state_hashes = []  # Track last STUCK_THRESHOLD state hashes

        for step in range(500):
            current_url = page.url

            # New challenge detected
            if current_url != prev_url:
                if prev_url:
                    elapsed = time.time() - challenge_start
                    log(f"✓ Challenge {challenge} complete ({elapsed:.1f}s)")
                    challenge += 1
                    challenge_start = time.time()
                    # Clear both LLM memories on new challenge
                    action_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
                    overview_messages = [{"role": "system", "content": OVERVIEW_PROMPT}]
                    state_hashes.clear()
                    last_action = None
                    last_result = None
                    challenge_summary = ""

                log(f"\n[Challenge {challenge}] {current_url}")
                prev_url = current_url

            # OBSERVE - fresh every step
            elements, handles = await extract_elements(page)

            step_start = time.time()

            # State-based stuck detection
            state_hash = compute_state_hash(current_url, elements)
            prev_hash = state_hashes[-1] if state_hashes else None
            state_hashes.append(state_hash)
            if len(state_hashes) > STUCK_THRESHOLD:
                state_hashes.pop(0)

            # Check if state changed
            state_changed = prev_hash is None or prev_hash != state_hash
            unchanged_count = len([h for h in state_hashes if h == state_hash])

            # Check if stuck (same state for STUCK_THRESHOLD iterations)
            if len(state_hashes) >= STUCK_THRESHOLD and len(set(state_hashes)) == 1:
                log("")
                log(f"{'!'*50}")
                log(f"STUCK: State unchanged {STUCK_THRESHOLD}x | hash={state_hash} | {len(elements)} elements")
                log(f"{'!'*50}")
                break

            # Get fresh overview with memory
            content = await extract_structured_content(page)

            # Log step header with spacing
            log("")  # Blank line before step
            inp_count = sum(1 for e in elements if e['tag'] == 'inp')
            btn_count = sum(1 for e in elements if e['tag'] == 'btn')
            state_indicator = "(changed)" if state_changed else f"(UNCHANGED {unchanged_count}/{STUCK_THRESHOLD})"
            log(f"{'='*50}")
            log(f"[Step {step+1}] {inp_count} inp, {btn_count} btn | {state_hash} {state_indicator}")

            # Log DOM extraction results
            hidden = content.get('hidden_content', [])
            data_attrs = content.get('data_attrs', [])
            if hidden:
                log(f"  Hidden: {hidden}")
            if data_attrs:
                log(f"  Data attrs: {data_attrs}")

            overview, challenge_summary = await analyze_overview(
                client, content, elements, overview_messages,
                last_action, last_result, state_changed, unchanged_count,
                challenge_summary
            )

            # Log full overview (multi-line)
            log(f"  Overview LLM:")
            for line in overview.split('\n'):
                if line.strip():
                    log(f"    {line.strip()}")

            context_str = format_context(overview, elements)

            # THINK - pass action memory and previous action/result for sequencing
            action = await llm_decide(client, action_messages, context_str, last_action, last_result)

            if action.get("a") == "error":
                log(f"  ⚠ LLM error, retrying...")
                continue

            # Show what element we're targeting
            action_idx = action.get("n", 0)
            if not isinstance(action_idx, int):
                try:
                    action_idx = int(action_idx)
                except (ValueError, TypeError):
                    action_idx = 0
            if action_idx < len(elements):
                el = elements[action_idx]
                tag = el.get('role') or el['tag']
                state = f" [{el['state']}]" if el.get('state') else ""
                log(f"  Target: [{action_idx}] {tag} \"{el['text']}\"{state}")

            # ACT
            result = await execute(page, action, handles)

            # Log execution result
            action_type = action.get("a", "?")
            action_val = action.get("v", "")
            step_time = time.time() - step_start
            if action_val:
                log(f"  Result: {action_type}[{action_idx}] \"{action_val}\" -> {result} ({step_time:.1f}s)")
            else:
                log(f"  Result: {action_type}[{action_idx}] -> {result} ({step_time:.1f}s)")

            # Store for next iteration's memory
            last_action = action
            last_result = result

            await asyncio.sleep(0.05)  # Reduced delay

        total_time = time.time() - total_start

        log("\n" + "=" * 50)
        log("SUMMARY")
        log("=" * 50)
        log(f"Challenges: {challenge}")
        log(f"Time: {total_time:.1f}s ({total_time/60:.1f}m)")

        await browser.close()

def main():
    import sys
    base_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE_URL

    os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
    with open(LOG_FILE, "w") as f:
        f.write(f"=== Started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")

    print("=" * 50)
    print("Fast Browser Agent")
    print(f"Target: {base_url}")
    print("=" * 50)

    asyncio.run(run_agent(base_url))

if __name__ == "__main__":
    main()
