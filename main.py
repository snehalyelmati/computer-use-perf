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
from src.agent.llm_client import LLMClient
from src.agent.config import (
    DEFAULT_BASE_URL,
    LOG_DIR,
    LOG_FILE,
    VERBOSE_LOG_FILE,
    STUCK_THRESHOLD,
    FAILURE_RESET_THRESHOLD,
    REPETITION_WINDOW,
)
from src.agent.providers import PROVIDER_MODELS
from src.agent.content_extraction import extract_structured_content
from src.agent.element_utils import extract_elements, format_context
from src.agent.llm_agents import (
    analyze_overview,
    llm_decide,
    extract_learning,
    diagnose_failure,
    evaluate_step,
    _compute_element_diff,
    _compute_text_diff,
)
from src.agent.logging_utils import log, log_verbose
from src.agent.prompts import OVERVIEW_PROMPT, SYSTEM_PROMPT
from src.agent.state_utils import compute_state_hash
from src.agent.grounding import (
    ground_data_to_observed,
    scrub_values,
    update_observed_values,
)


async def run_agent(base_url: str = DEFAULT_BASE_URL, client: LLMClient | None = None):
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
        prev_all_text = []  # Previous step's raw text for Oracle diff
        prev_filtered_text = []  # Previous step's filtered text for Overview diff
        pending_learning_task = None
        last_action_pos = None  # (x, y) of last interacted element for proximity
        last_oracle_verdict = None  # Oracle feedback from previous step
        oracle_history = []  # Recent non-OK Oracle verdicts for pattern detection
        failed_attempts = []  # Local failure patterns that survive intra-challenge resets
        consecutive_override_count = 0  # Circuit-breaker for Oracle OVERRIDE loops
        challenge_step_count = 0  # Steps spent on current challenge
        observed_values: set[str] = (
            set()
        )  # Grounding set that survives intra-challenge resets
        grounding_note = ""  # Note about dropped ungrounded DATA

        def _record_failed_attempt(trigger: str, recent_sigs: list[str], results_list):
            """Record a local failure pattern (scrub values; bounded)."""
            from src.agent.config import MAX_FAILED_APPROACHES

            parts = [f"trigger={trigger}"]
            if recent_sigs:
                parts.append(f"actions={' -> '.join(recent_sigs)}")

            failures = []
            for _a, r in results_list or []:
                low = (r or "").lower()
                if any(
                    x in low
                    for x in (
                        "verify failed",
                        "error",
                        "not found",
                        "unknown",
                        "timeout",
                    )
                ):
                    failures.append(scrub_values(str(r)))
            if failures:
                parts.append("failures=" + "; ".join(failures))

            entry = " | ".join(parts)
            if entry not in failed_attempts:
                failed_attempts.append(entry)
            if len(failed_attempts) > MAX_FAILED_APPROACHES:
                failed_attempts[:] = failed_attempts[-MAX_FAILED_APPROACHES:]

        for step in range(500):
            challenge_step_count += 1  # Increment at start of each step
            current_url = page.url

            # New challenge detected
            if current_url != prev_url:
                if prev_url:
                    elapsed = time.time() - challenge_start
                    log(f"OK Challenge {challenge} complete ({elapsed:.1f}s)")
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
                    prev_filtered_text = []
                    last_action_pos = (
                        None  # Reset proximity - new challenge starts fresh
                    )
                    last_oracle_verdict = None  # Reset oracle feedback
                    oracle_history.clear()  # Reset oracle memory
                    failed_attempts.clear()  # Reset failed attempts
                    consecutive_override_count = 0  # Reset circuit-breaker
                    challenge_step_count = 0  # Reset step counter for new challenge
                    observed_values.clear()  # Reset grounding memory
                    grounding_note = ""

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
                log(f"{'!' * 50}")
                log(
                    f"STUCK: State unchanged {STUCK_THRESHOLD}x | hash={state_hash} | {len(elements)} elements"
                )
                log(f"{'!' * 50}")
                break

            # Get fresh overview with memory
            content = await extract_structured_content(page)

            # Update grounding set from page-observed content
            update_observed_values(
                observed_values,
                content.get("all_text", []),
                content.get("hidden_content", []),
                content.get("data_attrs", []),
            )

            # Log step header with spacing
            log("")  # Blank line before step
            inp_count = sum(1 for e in elements if e["tag"] == "inp")
            btn_count = sum(1 for e in elements if e["tag"] == "btn")
            state_indicator = (
                "(changed)"
                if state_changed
                else f"(UNCHANGED {unchanged_count}/{STUCK_THRESHOLD})"
            )
            log(f"{'=' * 50}")
            log(
                f"[Step {step + 1}] {len(elements)} elements ({inp_count} inp, {btn_count} btn) | {state_hash} {state_indicator}"
            )

            # Log content stats
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

            # Save any completed learning to file (for later review/prompt optimization)
            if pending_learning_task and pending_learning_task.done():
                learning = pending_learning_task.result()
                if learning:
                    with open(learnings_file, "a") as f:
                        f.write(f"{learning}\n")
                    log(f"  Learning saved to {learnings_file}")
                pending_learning_task = None

            # Compute diffs BEFORE updating prev_ state (for Oracle)
            step_element_diff = _compute_element_diff(prev_elements, elements)
            step_text_diff = _compute_text_diff(
                prev_all_text, content.get("all_text", [])
            )

            (
                overview_resp,
                challenge_summary,
                last_filtered_text,
            ) = await analyze_overview(
                client,
                content,
                elements,
                overview_messages,
                last_results,
                state_changed,
                unchanged_count,
                challenge_summary,
                prev_elements,
                last_action_pos,
                prev_all_text,
                prev_filtered_text=prev_filtered_text,
                oracle_verdict=last_oracle_verdict,
                failed_attempts=failed_attempts,
                grounding_note=grounding_note,
            )

            # Ground Overview DATA: drop unobserved values (prevents placeholders)
            grounding_note = ""
            grounding = ground_data_to_observed(overview_resp.data, observed_values)
            if grounding.dropped_pairs:
                grounding_note = "Dropped ungrounded DATA entries: " + ", ".join(
                    grounding.dropped_pairs
                )
            if grounding.data != overview_resp.data:
                overview_resp = overview_resp.model_copy(
                    update={"data": grounding.data}
                )
            prev_elements = elements
            prev_all_text = content.get("all_text", [])
            prev_filtered_text = last_filtered_text

            # Log overview fields
            log(f"  Overview LLM:")
            log(f"    GOAL: {overview_resp.goal}")
            if overview_resp.task:
                log(f"    TASK: {overview_resp.task}")
            if overview_resp.data:
                log(f"    DATA: {overview_resp.data}")
            if overview_resp.progress:
                log(f"    PROGRESS: {overview_resp.progress}")
            log(f"    NEXT: {overview_resp.next}")

            # Check if Oracle declared WRONG_GOAL (from previous step) - reset context
            if last_oracle_verdict and last_oracle_verdict.status == "WRONG_GOAL":
                log(f"  ORACLE WRONG_GOAL: Resetting context for fresh evaluation")
                log(f"    Reason: {last_oracle_verdict.reason or 'N/A'}")
                _record_failed_attempt(
                    "oracle_wrong_goal_reset", recent_action_sigs, last_results
                )
                overview_messages[:] = [{"role": "system", "content": OVERVIEW_PROMPT}]
                action_messages[:] = [{"role": "system", "content": SYSTEM_PROMPT}]
                recent_action_sigs.clear()
                oracle_history.clear()  # Reset oracle memory on goal reset
                last_oracle_verdict = None  # Clear so we don't keep resetting
                continue  # Re-run Overview with fresh context

            # Determine the directive and goal to use
            action_goal = overview_resp.goal  # default
            if (
                last_oracle_verdict
                and last_oracle_verdict.status == "OVERRIDE"
                and last_oracle_verdict.next_directive
            ):
                next_directive = last_oracle_verdict.next_directive
                log(f"  ORACLE OVERRIDE: Using Oracle's directive")
                log(f"    Directive: {next_directive}")
            elif (
                last_oracle_verdict
                and last_oracle_verdict.status == "REDIRECT"
                and last_oracle_verdict.correct_goal
            ):
                next_directive = overview_resp.next
                action_goal = last_oracle_verdict.correct_goal
                log(f"  ORACLE REDIRECT: Correcting goal for Action LLM")
                log(f"    Corrected goal: {action_goal}")
            else:
                next_directive = overview_resp.next

            # Always route through Action LLM
            context_str = format_context(
                action_goal, overview_resp.data, next_directive, elements
            )
            actions = await llm_decide(
                client, action_messages, context_str, last_results
            )
            if len(actions) == 1 and actions[0].get("a") == "error":
                log("  WARN LLM error, retrying...")
                continue

            # Handle empty actions - skip execution, let next iteration reassess
            if not actions:
                log("  No actions from LLM - continuing")
                continue

            # Compute action signature for repetition detection (uses index for exact match)
            def _action_sig(a):
                action_type = a.get("a", "?")
                idx = a.get("n", 0)
                if isinstance(idx, int) and 0 <= idx < len(elements):
                    return f"{action_type}:{elements[idx]['tag']}:{idx}"
                return f"{action_type}:{a.get('v', '?')}"

            sig = "|".join(_action_sig(a) for a in actions)
            recent_action_sigs.append(sig)
            if len(recent_action_sigs) > REPETITION_WINDOW:
                recent_action_sigs.pop(0)

            # Detect repetition: if only 1 unique signature in last REPETITION_WINDOW steps (exact repeat)
            if (
                len(recent_action_sigs) >= REPETITION_WINDOW
                and len(set(recent_action_sigs)) == 1
            ):
                log(
                    f"  Repetition detected: {len(set(recent_action_sigs))} unique actions in last {REPETITION_WINDOW} steps - running diagnosis"
                )
                _record_failed_attempt("repetition", recent_action_sigs, last_results)
                challenge_summary = await diagnose_failure(
                    client,
                    challenge_summary,
                    content,
                    elements,
                    last_results,
                    "repetition",
                    recent_action_sigs,
                    failed_attempts=failed_attempts,
                )
                overview_messages[:] = [{"role": "system", "content": OVERVIEW_PROMPT}]
                action_messages[:] = [{"role": "system", "content": SYSTEM_PROMPT}]
                consecutive_failures = 0
                recent_action_sigs.clear()
                continue  # Skip executing repeated action, start fresh

            if len(actions) > 1:
                log(f"  Batch: {len(actions)} actions")

            # Safety: never type/watch unobserved values (prevents placeholder leakage)
            grounded_actions = []
            for a in actions:
                if a.get("a") in ("type", "watch") and a.get("v") is not None:
                    v = str(a.get("v"))
                    if v and v not in observed_values:
                        log(
                            f"  Dropping ungrounded action value for {a.get('a')}[{a.get('n', '?')}]"
                        )
                        continue
                grounded_actions.append(a)
            actions = grounded_actions
            if not actions:
                log("  All actions dropped by grounding - continuing")
                continue

            # ACT - execute batch with verification
            results = await execute_batch(page, actions, handles, elements)

            # Log each executed action result
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
            if len(results) != len(actions):
                log(
                    f"  Batch cut short: {len(results)}/{len(actions)} executed ({step_time:.1f}s)"
                )
            else:
                log(
                    f"  Step time: {step_time:.1f}s ({len(results)} action{'s' if len(results) > 1 else ''})"
                )

            # EVALUATE - Oracle judges if we're on track (uses pre-computed diffs)
            last_oracle_verdict = await evaluate_step(
                client,
                overview=overview_resp,
                actions=actions,
                results=results,
                content=content,
                elements=elements,
                element_diff=step_element_diff,
                text_diff=step_text_diff,
                challenge_step_count=challenge_step_count,
                oracle_history=oracle_history,
            )

            # Track non-OK verdicts for Oracle memory
            if last_oracle_verdict.status != "OK":
                oracle_history.append(last_oracle_verdict)
                if len(oracle_history) > 5:
                    oracle_history.pop(0)

            # Track consecutive OVERRIDEs for circuit-breaker
            if last_oracle_verdict.status == "OVERRIDE":
                consecutive_override_count += 1
            else:
                consecutive_override_count = 0

            # Circuit-breaker: too many consecutive OVERRIDEs without progress
            from src.agent.config import ORACLE_OVERRIDE_CIRCUIT_BREAKER

            if consecutive_override_count >= ORACLE_OVERRIDE_CIRCUIT_BREAKER:
                log(
                    f"  Oracle circuit-breaker: {consecutive_override_count} consecutive OVERRIDEs - running diagnosis"
                )
                _record_failed_attempt(
                    "oracle_override_loop", recent_action_sigs, results
                )
                challenge_summary = await diagnose_failure(
                    client,
                    challenge_summary,
                    content,
                    elements,
                    results,
                    "oracle_override_loop",
                    recent_action_sigs,
                    failed_attempts=failed_attempts,
                )
                overview_messages[:] = [{"role": "system", "content": OVERVIEW_PROMPT}]
                action_messages[:] = [{"role": "system", "content": SYSTEM_PROMPT}]
                consecutive_override_count = 0
                consecutive_failures = 0
                recent_action_sigs.clear()
                oracle_history.clear()

            # Check per-challenge step budget
            from src.agent.config import CHALLENGE_STEP_BUDGET

            if challenge_step_count >= CHALLENGE_STEP_BUDGET:
                log(
                    f"  Challenge step budget exceeded ({challenge_step_count} steps) - running diagnosis"
                )
                _record_failed_attempt(
                    "step_budget_exceeded", recent_action_sigs, results
                )
                challenge_summary = await diagnose_failure(
                    client,
                    challenge_summary,
                    content,
                    elements,
                    results,
                    "step_budget_exceeded",
                    recent_action_sigs,
                    failed_attempts=failed_attempts,
                )
                overview_messages[:] = [{"role": "system", "content": OVERVIEW_PROMPT}]
                action_messages[:] = [{"role": "system", "content": SYSTEM_PROMPT}]
                challenge_step_count = 0  # Reset budget
                consecutive_failures = 0
                recent_action_sigs.clear()

            # Track consecutive failures for context reset
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
                    f"  Context reset: {consecutive_failures} consecutive failures - running diagnosis"
                )
                _record_failed_attempt(
                    "consecutive_failures", recent_action_sigs, results
                )
                challenge_summary = await diagnose_failure(
                    client,
                    challenge_summary,
                    content,
                    elements,
                    results,
                    "consecutive_failures",
                    recent_action_sigs,
                    failed_attempts=failed_attempts,
                )
                overview_messages[:] = [{"role": "system", "content": OVERVIEW_PROMPT}]
                action_messages[:] = [{"role": "system", "content": SYSTEM_PROMPT}]
                consecutive_failures = 0
                recent_action_sigs.clear()

            # Store all executed results for next iteration's context
            if not results:
                log("  No actions - continuing")
                continue
            last_results = results

            await asyncio.sleep(0.05)  # Reduced delay

        total_time = time.time() - total_start

        log("\n" + "=" * 50)
        log("SUMMARY")
        log("=" * 50)
        log(f"Challenges: {challenge}")
        log(f"Time: {total_time:.1f}s ({total_time / 60:.1f}m)")

        await browser.close()


def main():
    import argparse
    import src.agent.config as config

    parser = argparse.ArgumentParser(description="Fast Browser Agent")
    parser.add_argument("--url", default=config.DEFAULT_BASE_URL, help="Target URL")
    parser.add_argument("--model", default=None, help="Overview/Oracle model name")
    parser.add_argument("--action-model", default=None, help="Action model name")
    parser.add_argument(
        "--reasoning",
        default=None,
        choices=["none", "low", "medium", "high"],
        help="Reasoning effort (for models that support it)",
    )
    parser.add_argument(
        "--provider",
        default=config.PROVIDER,
        choices=["groq", "cerebras"],
        help="LLM provider",
    )
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
    for src, link in [
        (LOG_FILE, f"{LOG_DIR}/agent.log"),
        (VERBOSE_LOG_FILE, f"{LOG_DIR}/agent_verbose.log"),
    ]:
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
