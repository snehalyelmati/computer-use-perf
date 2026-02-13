"""
Fast browser automation agent for web challenges.
Target: 30 challenges in <5 minutes.
"""

import asyncio
import os
import time
from groq import AsyncGroq
from cerebras.cloud.sdk import AsyncCerebras
from playwright.async_api import async_playwright

from src.agent.action_executor import execute_batch
from src.agent.config import DEFAULT_BASE_URL, LOG_DIR, LOG_FILE, VERBOSE_LOG_FILE, STUCK_THRESHOLD, FAILURE_RESET_THRESHOLD, REPETITION_WINDOW
from src.agent.providers import PROVIDER_MODELS
from src.agent.content_extraction import extract_structured_content
from src.agent.element_utils import extract_elements, format_context
from src.agent.llm_agents import analyze_overview, llm_decide, extract_learning, diagnose_failure, evaluate_step, _compute_element_diff, _compute_text_diff, parse_actions_from_overview
from src.agent.logging_utils import log, log_verbose
from src.agent.prompts import OVERVIEW_PROMPT, SYSTEM_PROMPT
from src.agent.state_utils import compute_state_hash

async def run_agent(base_url: str = DEFAULT_BASE_URL, client=None):
    """Run the agent through all challenges."""

    if client is None:
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
        await asyncio.sleep(2.0)

        try:
            await page.click("text=Start", timeout=5000)
            log("Clicked Start!")
        except:
            log("No Start button")

        await asyncio.sleep(1.0)

        # Agent loop
        challenge = 1
        prev_url = ""
        # Challenge-level memory for both LLMs
        action_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        overview_messages = [{"role": "system", "content": OVERVIEW_PROMPT}]
        challenge_start = time.time()
        last_results = []  # List of (action, result) tuples from previous step
        challenge_summary = ""  # Persistent summary that survives memory truncation
        state_hashes = []  # Track last STUCK_THRESHOLD state hashes
        consecutive_failures = 0  # Track consecutive steps with failures
        recent_action_sigs = []  # Track action signatures for repetition detection
        learnings_file = f"{LOG_DIR}/learnings.txt"  # Save learnings to file for review
        prev_elements = []  # Previous step's elements for diff
        prev_all_text = []  # Previous step's text for diff
        pending_learning_task = None
        last_action_pos = None  # (x, y) of last interacted element for proximity
        last_oracle_verdict = None  # Oracle feedback from previous step
        challenge_step_count = 0  # Steps spent on current challenge

        for step in range(500):
            challenge_step_count += 1  # Increment at start of each step
            current_url = page.url

            # New challenge detected
            if current_url != prev_url:
                if prev_url:
                    elapsed = time.time() - challenge_start
                    log(f"✓ Challenge {challenge} complete ({elapsed:.1f}s)")
                    challenge += 1
                    challenge_start = time.time()
                    # Fire async learning extraction before clearing memory
                    if challenge_summary:
                        pending_learning_task = asyncio.create_task(
                            extract_learning(client, challenge_summary)
                        )
                    # Clear both LLM memories on new challenge
                    action_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
                    overview_messages = [{"role": "system", "content": OVERVIEW_PROMPT}]
                    state_hashes.clear()
                    last_results = []
                    challenge_summary = ""
                    consecutive_failures = 0
                    recent_action_sigs.clear()
                    prev_elements = []
                    prev_all_text = []
                    last_action_pos = None  # Reset proximity - new challenge starts fresh
                    last_oracle_verdict = None  # Reset oracle feedback
                    challenge_step_count = 0  # Reset step counter for new challenge

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
            log(f"[Step {step+1}] {len(elements)} elements ({inp_count} inp, {btn_count} btn) | {state_hash} {state_indicator}")

            # Log content stats
            all_text = content.get('all_text', [])
            hidden = content.get('hidden_content', [])
            data_attrs = content.get('data_attrs', [])
            log(f"  Content: {len(all_text)} text, {len(hidden)} hidden, {len(data_attrs)} data_attrs")
            if hidden:
                log(f"  Hidden: {hidden}")
            if data_attrs:
                log(f"  Data attrs: {data_attrs}")

            # Save any completed learning to file (for later review/prompt optimization)
            if pending_learning_task and pending_learning_task.done():
                learning = pending_learning_task.result()
                if learning:
                    with open(learnings_file, "a") as f:
                        f.write(f"{learning}\n")
                    log(f"  Learning saved to {learnings_file}")
                pending_learning_task = None

            overview, challenge_summary = await analyze_overview(
                client, content, elements, overview_messages,
                last_results, state_changed, unchanged_count,
                challenge_summary, prev_elements,
                last_action_pos, prev_all_text,
                oracle_verdict=last_oracle_verdict
            )
            prev_elements = elements
            prev_all_text = content.get('all_text', [])

            # Log full overview (multi-line)
            log(f"  Overview LLM:")
            for line in overview.split('\n'):
                if line.strip():
                    log(f"    {line.strip()}")

            # Check if Oracle issued OVERRIDE with direct actions (from previous step)
            if (last_oracle_verdict and
                last_oracle_verdict.get("status") == "OVERRIDE" and
                last_oracle_verdict.get("next_actions")):
                actions = last_oracle_verdict["next_actions"]
                log(f"  ORACLE OVERRIDE: Using Oracle's {len(actions)} action(s) directly")
                log(f"    Reason: {last_oracle_verdict.get('reason', 'N/A')}")
            else:
                # TODO: Clean up Action LLM code path later once Overview JSON is stable
                # Try to parse actions directly from Overview's NEXT section
                actions = parse_actions_from_overview(overview)
                if actions is None:
                    # Fallback to Action LLM if parsing fails
                    context_str = format_context(overview, elements)
                    actions = await llm_decide(client, action_messages, context_str, last_results)
                    if len(actions) == 1 and actions[0].get("a") == "error":
                        log(f"  ⚠ LLM error, retrying...")
                        continue
                else:
                    log(f"  Actions from Overview: {len(actions)} action(s)")

            # Compute action signature for repetition detection (uses index for exact match)
            def _action_sig(a):
                action_type = a.get('a', '?')
                idx = a.get('n', 0)
                if isinstance(idx, int) and 0 <= idx < len(elements):
                    return f"{action_type}:{elements[idx]['tag']}:{idx}"
                return f"{action_type}:{a.get('v', '?')}"
            sig = "|".join(_action_sig(a) for a in actions)
            recent_action_sigs.append(sig)
            if len(recent_action_sigs) > REPETITION_WINDOW:
                recent_action_sigs.pop(0)

            # Detect repetition: if only 1 unique signature in last REPETITION_WINDOW steps (exact repeat)
            if len(recent_action_sigs) >= REPETITION_WINDOW and len(set(recent_action_sigs)) == 1:
                log(f"  Repetition detected: {len(set(recent_action_sigs))} unique actions in last {REPETITION_WINDOW} steps — running diagnosis")
                challenge_summary = await diagnose_failure(
                    client, challenge_summary, content, elements,
                    last_results, "repetition", recent_action_sigs
                )
                overview_messages[:] = [{"role": "system", "content": OVERVIEW_PROMPT}]
                action_messages[:] = [{"role": "system", "content": SYSTEM_PROMPT}]
                consecutive_failures = 0
                recent_action_sigs.clear()
                continue  # Skip executing repeated action, start fresh

            if len(actions) > 1:
                log(f"  Batch: {len(actions)} actions")

            # ACT - execute batch with verification
            results = await execute_batch(page, actions, handles, elements)

            # Log each executed action result
            for action, result in results:
                action_type = action.get("a", "?")
                action_idx = action.get("n", 0)
                action_val = action.get("v", "")
                if action_val:
                    log(f"  Result: {action_type}[{action_idx}] \"{action_val}\" -> {result}")
                else:
                    log(f"  Result: {action_type}[{action_idx}] -> {result}")

            step_time = time.time() - step_start
            if len(results) != len(actions):
                log(f"  Batch cut short: {len(results)}/{len(actions)} executed ({step_time:.1f}s)")
            else:
                log(f"  Step time: {step_time:.1f}s ({len(results)} action{'s' if len(results) > 1 else ''})")

            # EVALUATE - Oracle judges if we're on track
            filtered_text = content.get('all_text', [])
            element_diff = _compute_element_diff(prev_elements, elements)
            text_diff = _compute_text_diff(prev_all_text, filtered_text)
            last_oracle_verdict = await evaluate_step(
                client,
                overview=overview,
                actions=actions,
                results=results,
                content=content,
                elements=elements,
                element_diff=element_diff,
                text_diff=text_diff,
                challenge_step_count=challenge_step_count,
            )

            # Check per-challenge step budget
            from src.agent.config import CHALLENGE_STEP_BUDGET
            if challenge_step_count >= CHALLENGE_STEP_BUDGET:
                log(f"  Challenge step budget exceeded ({challenge_step_count} steps) — running diagnosis")
                challenge_summary = await diagnose_failure(
                    client, challenge_summary, content, elements,
                    results, "step_budget_exceeded", recent_action_sigs
                )
                overview_messages[:] = [{"role": "system", "content": OVERVIEW_PROMPT}]
                action_messages[:] = [{"role": "system", "content": SYSTEM_PROMPT}]
                challenge_step_count = 0  # Reset budget
                consecutive_failures = 0
                recent_action_sigs.clear()

            # Track consecutive failures for context reset
            has_failure = any(
                "verify failed" in r or "error" in r or "not found" in r or "unknown" in r
                for _, r in results
            )
            if has_failure:
                consecutive_failures += 1
            else:
                consecutive_failures = 0

            if consecutive_failures >= FAILURE_RESET_THRESHOLD:
                log(f"  Context reset: {consecutive_failures} consecutive failures — running diagnosis")
                challenge_summary = await diagnose_failure(
                    client, challenge_summary, content, elements,
                    results, "consecutive_failures", recent_action_sigs
                )
                overview_messages[:] = [{"role": "system", "content": OVERVIEW_PROMPT}]
                action_messages[:] = [{"role": "system", "content": SYSTEM_PROMPT}]
                consecutive_failures = 0
                recent_action_sigs.clear()

            # Store all executed results for next iteration's context
            if not results:
                log("  No valid actions — waiting 3s")
                await asyncio.sleep(3)
                continue
            last_results = results

            await asyncio.sleep(0.05)  # Reduced delay

        total_time = time.time() - total_start

        log("\n" + "=" * 50)
        log("SUMMARY")
        log("=" * 50)
        log(f"Challenges: {challenge}")
        log(f"Time: {total_time:.1f}s ({total_time/60:.1f}m)")

        await browser.close()

def main():
    import argparse
    import src.agent.config as config

    parser = argparse.ArgumentParser(description="Fast Browser Agent")
    parser.add_argument("--url", default=config.DEFAULT_BASE_URL, help="Target URL")
    parser.add_argument("--model", default=None, help="Overview/Oracle model name")
    parser.add_argument("--action-model", default=None, help="Action model name")
    parser.add_argument("--reasoning", default=None, choices=["none", "low", "medium", "high"], help="Reasoning effort (for models that support it)")
    parser.add_argument("--provider", default=config.PROVIDER, choices=["groq", "cerebras"], help="LLM provider")
    args = parser.parse_args()

    # Set provider first, then resolve model defaults
    config.PROVIDER = args.provider
    defaults = PROVIDER_MODELS[config.PROVIDER]
    config.MODEL_NAME = args.model or defaults["model"]
    config.ORACLE_MODEL = args.model or defaults["oracle"]
    config.ACTION_MODEL_NAME = args.action_model or defaults["action"]
    config.FILTER_MODEL_NAME = defaults["filter"]
    config.REASONING_EFFORT = args.reasoning
    base_url = args.url

    # Create LLM client based on provider
    if config.PROVIDER == "cerebras":
        client = AsyncCerebras()
    else:
        client = AsyncGroq(api_key=os.environ.get("GROQ_API_KEY"))

    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOG_FILE, "w") as f:
        f.write(f"=== Started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    with open(VERBOSE_LOG_FILE, "w") as f:
        f.write(f"=== Started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")

    # Symlink latest logs for easy access
    for src, link in [(LOG_FILE, f"{LOG_DIR}/agent.log"),
                      (VERBOSE_LOG_FILE, f"{LOG_DIR}/agent_verbose.log")]:
        if os.path.islink(link):
            os.remove(link)
        os.symlink(os.path.basename(src), link)

    print("=" * 50)
    print("Fast Browser Agent")
    print(f"Target: {base_url}")
    print(f"Provider: {config.PROVIDER}")
    print(f"Model: {config.MODEL_NAME}")
    print("=" * 50)

    asyncio.run(run_agent(base_url, client))

if __name__ == "__main__":
    main()
