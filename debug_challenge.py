"""
Debug script: run agent on a single challenge.
Usage: uv run debug_challenge.py [challenge_url]
Defaults to local debug_challenge.html if no URL given.
"""

import asyncio
import os
from pathlib import Path
import sys
import time
from groq import AsyncGroq
from playwright.async_api import async_playwright

from src.agent.action_executor import execute_batch
from src.agent.config import (
    CHALLENGE_GOAL,
    LOG_FILE,
    VERBOSE_LOG_FILE,
    STUCK_THRESHOLD,
    FAILURE_RESET_THRESHOLD,
    REPETITION_WINDOW,
)
from src.agent.content_extraction import extract_structured_content
from src.agent.element_utils import extract_elements, format_context
from src.agent.llm_agents import analyze_overview, llm_decide
from src.agent.logging_utils import log, log_verbose
from src.agent.prompts import OVERVIEW_PROMPT, SYSTEM_PROMPT
from src.agent.state_utils import compute_state_hash

MAX_STEPS = 50


async def run_debug(challenge_url: str):
    client = AsyncGroq(api_key=os.environ.get("GROQ_API_KEY"))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        page = await context.new_page()

        await page.add_init_script("""
            const style = document.createElement('style');
            style.textContent = '*, *::before, *::after { animation-duration: 0s !important; transition-duration: 0s !important; }';
            document.head.appendChild(style);
        """)

        log(f"Navigating to {challenge_url}...")
        await page.goto(challenge_url, wait_until="domcontentloaded")
        await asyncio.sleep(1.0)

        start_url = page.url
        log(f"\n[Debug Challenge] {start_url}")

        action_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        overview_messages = [{"role": "system", "content": OVERVIEW_PROMPT}]
        challenge_start = time.time()
        last_results = []
        challenge_summary = ""
        state_hashes = []
        consecutive_failures = 0
        recent_action_sigs = []
        steps_taken = 0

        for step in range(MAX_STEPS):
            steps_taken = step + 1
            current_url = page.url

            # Detect if challenge completed (URL changed)
            if current_url != start_url:
                elapsed = time.time() - challenge_start
                log(f"\nChallenge complete ({elapsed:.1f}s)")
                log(f"  Navigated to: {current_url}")
                break

            elements, handles = await extract_elements(page)
            step_start = time.time()

            state_hash = compute_state_hash(current_url, elements)
            prev_hash = state_hashes[-1] if state_hashes else None
            state_hashes.append(state_hash)
            if len(state_hashes) > STUCK_THRESHOLD:
                state_hashes.pop(0)

            state_changed = prev_hash is None or prev_hash != state_hash
            unchanged_count = len([h for h in state_hashes if h == state_hash])

            if len(state_hashes) >= STUCK_THRESHOLD and len(set(state_hashes)) == 1:
                log("")
                log(f"{'!' * 50}")
                log(
                    f"STUCK: State unchanged {STUCK_THRESHOLD}x | hash={state_hash} | {len(elements)} elements"
                )
                log(f"{'!' * 50}")
                break

            content = await extract_structured_content(page)

            log("")
            inp_count = sum(1 for e in elements if e["tag"] == "inp")
            btn_count = sum(1 for e in elements if e["tag"] == "btn")
            state_indicator = (
                "(changed)"
                if state_changed
                else f"(UNCHANGED {unchanged_count}/{STUCK_THRESHOLD})"
            )
            log(f"{'=' * 50}")
            log(
                f"[Step {step + 1}/{MAX_STEPS}] {len(elements)} elements ({inp_count} inp, {btn_count} btn) | {state_hash} {state_indicator}"
            )

            all_text = content.get("all_text", [])
            hidden = content.get("hidden_content", [])
            data_attrs = content.get("data_attrs", [])
            log(
                f"  Content: {len(all_text)} text, {len(hidden)} hidden, {len(data_attrs)} data_attrs"
            )
            if hidden:
                log(f"  Hidden: {hidden}")
            if data_attrs:
                log(f"  Data attrs: {data_attrs}")

            overview_resp, challenge_summary, _filtered = await analyze_overview(
                client=client,
                content=content,
                elements=elements,
                memory=overview_messages,
                goal=CHALLENGE_GOAL,
                last_results=last_results,
                state_changed=state_changed,
                unchanged_count=unchanged_count,
                challenge_summary=challenge_summary,
            )

            log("  Overview LLM:")
            log(f"    OBJECTIVE: {overview_resp.objective}")
            if overview_resp.task:
                log(f"    TASK: {overview_resp.task}")
            if overview_resp.data:
                log(f"    DATA: {overview_resp.data}")
            if overview_resp.progress:
                log(f"    PROGRESS: {overview_resp.progress}")
            log(f"    NEXT: {overview_resp.next}")

            context_str = format_context(
                CHALLENGE_GOAL,
                overview_resp.objective,
                overview_resp.data,
                overview_resp.next,
                elements,
            )
            actions = await llm_decide(
                client, action_messages, context_str, last_results
            )

            if len(actions) == 1 and actions[0].get("a") == "error":
                log("  WARN LLM error, retrying...")
                continue

            # Repetition detection
            sig = "|".join(f"{a.get('a', '?')}:{a.get('n', '')}" for a in actions)
            recent_action_sigs.append(sig)
            if len(recent_action_sigs) > REPETITION_WINDOW:
                recent_action_sigs.pop(0)

            if (
                len(recent_action_sigs) >= REPETITION_WINDOW
                and len(set(recent_action_sigs)) <= 3
            ):
                log(
                    f"  Repetition detected: {len(set(recent_action_sigs))} unique actions in last {REPETITION_WINDOW} steps - clearing LLM memory"
                )
                overview_messages[:] = [{"role": "system", "content": OVERVIEW_PROMPT}]
                action_messages[:] = [{"role": "system", "content": SYSTEM_PROMPT}]
                challenge_summary = ""
                consecutive_failures = 0
                recent_action_sigs.clear()
                continue

            # Log targets
            if len(actions) > 1:
                log(f"  Batch: {len(actions)} actions")
            for action in actions:
                action_idx = action.get("n", 0)
                if not isinstance(action_idx, int):
                    try:
                        action_idx = int(action_idx)
                    except (ValueError, TypeError):
                        action_idx = 0
                if action_idx < len(elements):
                    el = elements[action_idx]
                    tag = el.get("role") or el["tag"]
                    state = f" [{el['state']}]" if el.get("state") else ""
                    log(f'  Target: [{action_idx}] {tag} "{el["text"]}"{state}')

            results = await execute_batch(page, actions, handles, elements)

            for action, result in results:
                action_type = action.get("a", "?")
                action_idx = action.get("n", 0)
                action_val = action.get("v", "")
                if action_val:
                    log(
                        f'  Result: {action_type}[{action_idx}] "{action_val}" -> {result}'
                    )
                else:
                    log(f"  Result: {action_type}[{action_idx}] -> {result}")

            step_time = time.time() - step_start
            log(
                f"  Step time: {step_time:.1f}s ({len(results)} action{'s' if len(results) > 1 else ''})"
            )

            # Consecutive failure detection
            has_failure = any(
                "verify failed" in r
                or "error" in r
                or "not found" in r
                or "unknown" in r
                for _, r in results
            )
            if has_failure:
                consecutive_failures += 1
            else:
                consecutive_failures = 0

            if consecutive_failures >= FAILURE_RESET_THRESHOLD:
                log(
                    f"  Context reset: {consecutive_failures} consecutive failures - clearing LLM memory for fresh read"
                )
                overview_messages[:] = [{"role": "system", "content": OVERVIEW_PROMPT}]
                action_messages[:] = [{"role": "system", "content": SYSTEM_PROMPT}]
                challenge_summary = ""
                consecutive_failures = 0
                recent_action_sigs.clear()

            if not results:
                log("  No valid actions - waiting 3s")
                await asyncio.sleep(3)
                continue
            last_results = results

            await asyncio.sleep(0.05)

        total_time = time.time() - challenge_start
        log(f"\nDebug session: {total_time:.1f}s ({steps_taken} steps)")

        await browser.close()


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        challenge_url = sys.argv[1]
    else:
        # Default to local debug HTML
        html_path = Path(__file__).parent / "debug_challenge.html"
        challenge_url = f"file://{html_path.resolve()}"

    os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
    with open(LOG_FILE, "w") as f:
        f.write(f"=== Debug Started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    with open(VERBOSE_LOG_FILE, "w") as f:
        f.write(f"=== Debug Started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")

    print("=" * 50)
    print(f"Debug Challenge: {challenge_url}")
    print(f"Max steps: {MAX_STEPS}")
    print("=" * 50)

    asyncio.run(run_debug(challenge_url))
