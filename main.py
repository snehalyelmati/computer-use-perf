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
    CHALLENGE_GOAL,
    LOG_DIR,
    LOG_FILE,
    VERBOSE_LOG_FILE,
    STUCK_THRESHOLD,
    FAILURE_RESET_THRESHOLD,
    REPETITION_WINDOW,
)
from src.agent.llm_client import set_stats_collector
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
from src.agent.logging_utils import log
from src.agent.prompts import OVERVIEW_PROMPT, SYSTEM_PROMPT
from src.agent.state_utils import compute_state_hash
from src.agent.grounding import (
    ground_data_to_observed,
    scrub_values,
    update_observed_values,
)

from src.agent.stats import StatsCollector, write_run_stats


async def run_agent(
    base_url: str = DEFAULT_BASE_URL,
    client: LLMClient | None = None,
    *,
    stats: StatsCollector | None = None,
    max_steps: int = 500,
    max_challenges: int | None = None,
):
    """Run the agent through all challenges."""

    if client is None:
        import src.agent.config as agent_config

        if agent_config.PROVIDER == "cerebras":
            client = AsyncCerebras()
        else:
            client = AsyncGroq(api_key=os.environ.get("GROQ_API_KEY"))

    # Ensure global stats hook is scoped to this run only.
    set_stats_collector(stats)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context(viewport={"width": 1280, "height": 720})
            page = await context.new_page()

            # Disable animations
            await page.add_init_script(
                """
                const style = document.createElement('style');
                style.textContent = '*, *::before, *::after { animation-duration: 0s !important; transition-duration: 0s !important; }';
                document.head.appendChild(style);
                """
            )

            total_start = time.time()

            # Navigate and start
            log(f"Navigating to {base_url}...")
            await page.goto(base_url, wait_until="domcontentloaded")
            await asyncio.sleep(2.0)

            try:
                await page.click("text=Start", timeout=5000)
                log("Clicked Start!")
            except Exception:
                log("No Start button")

            await asyncio.sleep(1.0)

            # Agent loop
            challenge = 1
            completed_challenges = 0
            prev_url = ""
            current_goal = CHALLENGE_GOAL

            # Challenge-level memory for both LLMs
            action_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            overview_messages = [{"role": "system", "content": OVERVIEW_PROMPT}]
            challenge_start = time.time()
            last_results: list[tuple[dict, str]] = []
            challenge_summary = ""
            state_hashes: list[str] = []
            consecutive_failures = 0
            recent_action_sigs: list[str] = []
            learnings_file = f"{LOG_DIR}/learnings.txt"
            prev_elements: list[dict] = []
            prev_all_text: list[str] = []
            prev_filtered_text: list[str] = []
            pending_learning_task = None
            last_action_pos = None
            last_oracle_verdict = None
            oracle_history: list = []
            failed_attempts: list[str] = []
            consecutive_override_count = 0
            challenge_step_count = 0
            observed_values: set[str] = set()
            grounding_note = ""

            # Oracle evaluation uses the *next* observed state to judge the previous step.
            last_overview_for_oracle = None
            last_actions_for_oracle: list[dict] | None = None
            last_results_for_oracle: list[tuple[dict, str]] | None = None

            def _record_failed_attempt(
                trigger: str, recent_sigs: list[str], results_list
            ) -> None:
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

            stop_reason: str | None = None

            for step in range(max_steps):
                challenge_step_count += 1
                current_url = page.url

                # New challenge detected (Challenge Mode boundary)
                if current_url != prev_url:
                    if prev_url:
                        elapsed = time.time() - challenge_start
                        log(f"Challenge {challenge} complete ({elapsed:.1f}s)")
                        completed_challenges += 1
                        if stats is not None:
                            stats.end_challenge(completed=True)

                        if (
                            max_challenges is not None
                            and completed_challenges >= max_challenges
                        ):
                            log(f"Reached max_challenges={max_challenges} - stopping")
                            stop_reason = f"max_challenges={max_challenges}"
                            break

                        challenge += 1
                        challenge_start = time.time()

                        # Fire async learning extraction before clearing memory
                        if challenge_summary:
                            pending_learning_task = asyncio.create_task(
                                extract_learning(client, challenge_summary)
                            )

                        # Clear both LLM memories on new challenge
                        action_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
                        overview_messages = [
                            {"role": "system", "content": OVERVIEW_PROMPT}
                        ]
                        state_hashes.clear()
                        last_results = []
                        challenge_summary = ""
                        consecutive_failures = 0
                        recent_action_sigs.clear()
                        prev_elements = []
                        prev_all_text = []
                        prev_filtered_text = []
                        last_action_pos = None
                        last_oracle_verdict = None
                        oracle_history.clear()
                        failed_attempts.clear()
                        consecutive_override_count = 0
                        challenge_step_count = 0
                        observed_values.clear()
                        grounding_note = ""
                        last_overview_for_oracle = None
                        last_actions_for_oracle = None
                        last_results_for_oracle = None

                    log(f"\n[Challenge {challenge}] {current_url}")
                    current_goal = CHALLENGE_GOAL
                    log(f"  Fixed GOAL: {current_goal}")
                    if stats is not None:
                        stats.start_challenge(challenge, current_url)
                    prev_url = current_url

                if stats is not None:
                    stats.increment_step()

                # OBSERVE - fresh every step
                elements, handles = await extract_elements(page)
                step_start = time.time()

                # State-based stuck detection
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

                    # Diagnose and reset per-challenge context, then keep going.
                    content = await extract_structured_content(page)
                    _record_failed_attempt(
                        "stuck_state_hash", recent_action_sigs, last_results
                    )
                    challenge_summary = await diagnose_failure(
                        client,
                        challenge_summary,
                        content,
                        elements,
                        last_results,
                        "stuck_state_hash",
                        recent_action_sigs,
                        failed_attempts=failed_attempts,
                    )
                    overview_messages[:] = [
                        {"role": "system", "content": OVERVIEW_PROMPT}
                    ]
                    action_messages[:] = [{"role": "system", "content": SYSTEM_PROMPT}]
                    consecutive_failures = 0
                    recent_action_sigs.clear()
                    oracle_history.clear()
                    last_oracle_verdict = None
                    consecutive_override_count = 0
                    state_hashes.clear()
                    prev_elements = []
                    prev_all_text = []
                    prev_filtered_text = []
                    last_action_pos = None
                    grounding_note = ""
                    observed_values.clear()
                    last_overview_for_oracle = None
                    last_actions_for_oracle = None
                    last_results_for_oracle = None
                    continue

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
                    f"[Step {step + 1}] {len(elements)} elements ({inp_count} inp, {btn_count} btn) | {state_hash} {state_indicator}"
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

                # Save any completed learning to file
                if pending_learning_task and pending_learning_task.done():
                    learning = pending_learning_task.result()
                    if learning:
                        with open(learnings_file, "a") as f:
                            f.write(f"{learning}\n")
                        log(f"  Learning saved to {learnings_file}")
                    pending_learning_task = None

                # Compute diffs (for Oracle)
                step_element_diff = _compute_element_diff(prev_elements, elements)
                step_text_diff = _compute_text_diff(
                    prev_all_text, content.get("all_text", [])
                )

                # EVALUATE (Oracle): judge previous step using newly observed state.
                if (
                    last_overview_for_oracle is not None
                    and last_results_for_oracle is not None
                ):
                    last_oracle_verdict = await evaluate_step(
                        client=client,
                        goal=current_goal,
                        overview=last_overview_for_oracle,
                        actions=last_actions_for_oracle or [],
                        results=last_results_for_oracle,
                        content=content,
                        elements=elements,
                        element_diff=step_element_diff,
                        text_diff=step_text_diff,
                        challenge_step_count=max(0, challenge_step_count - 1),
                        oracle_history=oracle_history,
                    )

                    if last_oracle_verdict.status != "OK":
                        oracle_history.append(last_oracle_verdict)
                        if len(oracle_history) > 5:
                            oracle_history.pop(0)

                    if last_oracle_verdict.status == "OVERRIDE":
                        consecutive_override_count += 1
                    else:
                        consecutive_override_count = 0

                    from src.agent.config import ORACLE_OVERRIDE_CIRCUIT_BREAKER

                    if consecutive_override_count >= ORACLE_OVERRIDE_CIRCUIT_BREAKER:
                        log(
                            f"  Oracle circuit-breaker: {consecutive_override_count} consecutive OVERRIDEs - running diagnosis"
                        )
                        _record_failed_attempt(
                            "oracle_override_loop",
                            recent_action_sigs,
                            last_results_for_oracle,
                        )
                        challenge_summary = await diagnose_failure(
                            client,
                            challenge_summary,
                            content,
                            elements,
                            last_results_for_oracle,
                            "oracle_override_loop",
                            recent_action_sigs,
                            failed_attempts=failed_attempts,
                        )
                        overview_messages[:] = [
                            {"role": "system", "content": OVERVIEW_PROMPT}
                        ]
                        action_messages[:] = [
                            {"role": "system", "content": SYSTEM_PROMPT}
                        ]
                        consecutive_override_count = 0
                        consecutive_failures = 0
                        recent_action_sigs.clear()
                        oracle_history.clear()
                        last_oracle_verdict = None
                        last_overview_for_oracle = None
                        last_actions_for_oracle = None
                        last_results_for_oracle = None
                        state_hashes.clear()
                        continue

                    if last_oracle_verdict.status == "WRONG_GOAL":
                        log(
                            "  ORACLE WRONG_GOAL: Resetting context for fresh evaluation"
                        )
                        log(f"    Reason: {last_oracle_verdict.reason or 'N/A'}")
                        _record_failed_attempt(
                            "oracle_wrong_goal_reset",
                            recent_action_sigs,
                            last_results_for_oracle,
                        )
                        overview_messages[:] = [
                            {"role": "system", "content": OVERVIEW_PROMPT}
                        ]
                        action_messages[:] = [
                            {"role": "system", "content": SYSTEM_PROMPT}
                        ]
                        recent_action_sigs.clear()
                        oracle_history.clear()
                        last_oracle_verdict = None
                        consecutive_override_count = 0
                        last_overview_for_oracle = None
                        last_actions_for_oracle = None
                        last_results_for_oracle = None
                        state_hashes.clear()
                        continue

                (
                    overview_resp,
                    challenge_summary,
                    last_filtered_text,
                ) = await analyze_overview(
                    client=client,
                    content=content,
                    elements=elements,
                    memory=overview_messages,
                    goal=current_goal,
                    last_results=last_results,
                    state_changed=state_changed,
                    unchanged_count=unchanged_count,
                    challenge_summary=challenge_summary,
                    prev_elements=prev_elements,
                    last_action_pos=last_action_pos,
                    prev_all_text=prev_all_text,
                    prev_filtered_text=prev_filtered_text,
                    oracle_verdict=last_oracle_verdict,
                    failed_attempts=failed_attempts,
                    grounding_note=grounding_note,
                )

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

                log("  Overview LLM:")
                log(f"    OBJECTIVE: {overview_resp.objective}")
                if overview_resp.task:
                    log(f"    TASK: {overview_resp.task}")
                if overview_resp.data:
                    log(f"    DATA: {overview_resp.data}")
                if overview_resp.progress:
                    log(f"    PROGRESS: {overview_resp.progress}")
                log(f"    NEXT: {overview_resp.next}")

                action_goal = current_goal
                if (
                    last_oracle_verdict
                    and last_oracle_verdict.status == "OVERRIDE"
                    and last_oracle_verdict.next_directive
                ):
                    next_directive = last_oracle_verdict.next_directive
                    log("  ORACLE OVERRIDE: Using Oracle's directive")
                    log(f"    Directive: {next_directive}")
                else:
                    next_directive = overview_resp.next

                context_str = format_context(
                    action_goal,
                    overview_resp.objective,
                    overview_resp.data,
                    next_directive,
                    elements,
                )
                actions = await llm_decide(
                    client, action_messages, context_str, last_results
                )

                if len(actions) == 1 and actions[0].get("a") == "error":
                    log("  WARN LLM error, retrying...")
                    last_overview_for_oracle = None
                    last_actions_for_oracle = None
                    last_results_for_oracle = None
                    continue

                if not actions:
                    log("  No actions from LLM - continuing")
                    last_overview_for_oracle = None
                    last_actions_for_oracle = None
                    last_results_for_oracle = None
                    continue

                def _action_sig(a: dict) -> str:
                    action_type = a.get("a", "?")
                    idx = a.get("n", 0)
                    if isinstance(idx, int) and 0 <= idx < len(elements):
                        return f"{action_type}:{elements[idx]['tag']}:{idx}"
                    return f"{action_type}:{a.get('v', '?')}"

                sig = "|".join(_action_sig(a) for a in actions)
                recent_action_sigs.append(sig)
                if len(recent_action_sigs) > REPETITION_WINDOW:
                    recent_action_sigs.pop(0)

                if (
                    len(recent_action_sigs) >= REPETITION_WINDOW
                    and len(set(recent_action_sigs)) == 1
                ):
                    log(
                        f"  Repetition detected: {len(set(recent_action_sigs))} unique actions in last {REPETITION_WINDOW} steps - running diagnosis"
                    )
                    _record_failed_attempt(
                        "repetition", recent_action_sigs, last_results
                    )
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
                    overview_messages[:] = [
                        {"role": "system", "content": OVERVIEW_PROMPT}
                    ]
                    action_messages[:] = [{"role": "system", "content": SYSTEM_PROMPT}]
                    consecutive_failures = 0
                    recent_action_sigs.clear()
                    last_overview_for_oracle = None
                    last_actions_for_oracle = None
                    last_results_for_oracle = None
                    continue

                if len(actions) > 1:
                    log(f"  Batch: {len(actions)} actions")

                grounded_actions: list[dict] = []
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
                    last_overview_for_oracle = None
                    last_actions_for_oracle = None
                    last_results_for_oracle = None
                    continue

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
                if len(results) != len(actions):
                    log(
                        f"  Batch cut short: {len(results)}/{len(actions)} executed ({step_time:.1f}s)"
                    )
                else:
                    log(
                        f"  Step time: {step_time:.1f}s ({len(results)} action{'s' if len(results) > 1 else ''})"
                    )

                # Store context for Oracle evaluation on the next step.
                last_overview_for_oracle = overview_resp
                last_actions_for_oracle = actions
                last_results_for_oracle = results

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
                    overview_messages[:] = [
                        {"role": "system", "content": OVERVIEW_PROMPT}
                    ]
                    action_messages[:] = [{"role": "system", "content": SYSTEM_PROMPT}]
                    challenge_step_count = 0
                    consecutive_failures = 0
                    recent_action_sigs.clear()

                has_failure = any(
                    (
                        "verify failed" in (r or "").lower()
                        or "error" in (r or "").lower()
                        or "not found" in (r or "").lower()
                        or "unknown" in (r or "").lower()
                    )
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
                    overview_messages[:] = [
                        {"role": "system", "content": OVERVIEW_PROMPT}
                    ]
                    action_messages[:] = [{"role": "system", "content": SYSTEM_PROMPT}]
                    consecutive_failures = 0
                    recent_action_sigs.clear()

                if not results:
                    log("  No actions - continuing")
                    continue
                last_results = results

                await asyncio.sleep(0.05)

            # If we exit the loop right after executing actions, Oracle may not have
            # evaluated the final step (it evaluates on the next observation). Do one
            # final observe/evaluate for best-effort completeness.
            if (
                last_overview_for_oracle is not None
                and last_results_for_oracle is not None
            ):
                try:
                    final_elements, _final_handles = await extract_elements(page)
                    final_content = await extract_structured_content(page)
                    final_el_diff = _compute_element_diff(prev_elements, final_elements)
                    final_text_diff = _compute_text_diff(
                        prev_all_text, final_content.get("all_text", [])
                    )
                    _ = await evaluate_step(
                        client=client,
                        goal=current_goal,
                        overview=last_overview_for_oracle,
                        actions=last_actions_for_oracle or [],
                        results=last_results_for_oracle,
                        content=final_content,
                        elements=final_elements,
                        element_diff=final_el_diff,
                        text_diff=final_text_diff,
                        challenge_step_count=max(0, challenge_step_count - 1),
                        oracle_history=oracle_history,
                    )
                except Exception:
                    pass

            if stop_reason is None:
                stop_reason = f"max_steps={max_steps}"

            total_time = time.time() - total_start

            log("\n" + "=" * 50)
            log("SUMMARY")
            log("=" * 50)
            log(f"Challenges completed: {completed_challenges}")
            log(f"Time: {total_time:.1f}s ({total_time / 60:.1f}m)")

            if stats is not None:
                # If the run ended mid-challenge, mark it incomplete.
                stats.end_challenge(completed=False, reason=stop_reason)
                stats.end_run()
                json_path, md_path = write_run_stats(log_dir=LOG_DIR, stats=stats)
                log(f"Run stats: {json_path}")
                log(f"Run stats: {md_path}")

            await browser.close()
    finally:
        set_stats_collector(None)


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
    parser.add_argument(
        "--max-steps",
        type=int,
        default=500,
        help="Max loop steps (safety cap)",
    )
    parser.add_argument(
        "--max-challenges",
        type=int,
        default=None,
        help="Stop after completing N challenges",
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

    stats = StatsCollector(
        run_mode="challenge",
        provider=config.PROVIDER,
        model_overview=config.MODEL_NAME,
        model_oracle=config.ORACLE_MODEL,
        model_action=config.ACTION_MODEL_NAME,
        model_filter=config.FILTER_MODEL_NAME,
        max_steps=args.max_steps,
    )

    asyncio.run(
        run_agent(
            base_url,
            client,
            stats=stats,
            max_steps=args.max_steps,
            max_challenges=args.max_challenges,
        )
    )


if __name__ == "__main__":
    main()
