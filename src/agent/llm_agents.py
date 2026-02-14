import asyncio
import re
from typing import cast

from . import config
from .config import MAX_BATCH_SIZE
from .element_utils import format_element_summary, format_elements_by_proximity
from .llm_client import LLMClient, complete
from .logging_utils import log, log_verbose
from .schemas import OracleResponse, OverviewResponse, ActionResponse, LearningResponse
from .text_budget import select_lines_for_budget


async def filter_page_content(client: LLMClient, all_text: list[str]) -> list[str]:
    """Filter filler text (lorem ipsum, section headers, repeated patterns) using a small LLM."""
    if len(all_text) <= 30:
        return all_text

    prompt = "Extract ONLY useful text lines (task instructions, codes, values, form labels, errors). Remove filler (section headers, lorem ipsum, repeated patterns). Return one line per output line, no numbering."

    async def _filter_chunk(chunk: list[str]) -> list[str]:
        numbered = "\n".join(f"{i + 1}. {line}" for i, line in enumerate(chunk))
        content, _usage = await complete(
            client,
            model=cast(str, config.FILTER_MODEL_NAME),
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": numbered},
            ],
            max_completion_tokens=1000,
            call_type="filter",
        )
        result = content or ""
        return [line.strip() for line in result.splitlines() if line.strip()]

    try:
        if len(all_text) <= 100:
            return await _filter_chunk(all_text)

        # Map-reduce: chunk into groups of 100, filter in parallel
        chunks = [all_text[i : i + 100] for i in range(0, len(all_text), 100)]
        results = await asyncio.gather(*[_filter_chunk(c) for c in chunks])
        combined = []
        for r in results:
            combined.extend(r)
        return combined
    except RuntimeError:
        raise
    except Exception as e:
        log(f"  Filter error (returning unfiltered): {e}")
        return all_text


def _extract_progress_indicators(all_text: list[str]) -> list[str]:
    """Find progress patterns like 'Step 2/6', '3 of 5', 'Progress: 50%'."""
    patterns = [
        r"\d+\s*/\s*\d+",  # 2/6, 3 / 5
        r"\d+\s+of\s+\d+",  # 3 of 5
        r"step\s*\d+",  # step 2, Step 3
        r"progress[:\s]+\d+%?",  # Progress: 50%
        r"challenge\s*\d+",  # Challenge 5
    ]
    indicators: list[str] = []
    seen = set()
    for text in all_text:
        low = text.lower()
        for pattern in patterns:
            if re.search(pattern, low):
                t = text.strip()
                if t and t not in seen:
                    seen.add(t)
                    indicators.append(t)
                break
    return indicators


def _extract_feedback_text(all_text: list[str]) -> list[str]:
    """Extract error/warning/feedback lines from page text."""
    keywords = [
        "wrong",
        "error",
        "failed",
        "invalid",
        "try again",
        "incorrect",
        "warning",
    ]
    feedback: list[str] = []
    seen = set()
    for line in all_text:
        if any(kw in line.lower() for kw in keywords):
            t = line.strip()
            if t and t not in seen:
                seen.add(t)
                feedback.append(t)
    return feedback


async def evaluate_step(
    client: LLMClient,
    goal: str,
    overview: OverviewResponse,
    actions: list[dict],  # Actions that were executed
    results: list[tuple],  # (action, result_string) tuples
    content: dict,  # Page content
    elements: list,  # Current elements
    element_diff: str,  # From _compute_element_diff()
    text_diff: str,  # From _compute_text_diff()
    challenge_step_count: int = 0,  # Steps spent on current challenge
    oracle_history: list[OracleResponse] | None = None,  # Recent non-OK verdicts
) -> OracleResponse:
    """Oracle evaluation of step progress. Returns OracleResponse."""
    from .prompts import ORACLE_PROMPT

    objective = overview.objective or "(not specified)"
    data = overview.data or "(none)"
    progress = overview.progress or "(none)"

    # Format action results
    action_results = _format_results(results) if results else "No actions executed"

    # Extract page content for Oracle context
    all_text = content.get("all_text", [])
    hidden = content.get("hidden_content", [])
    data_attrs = content.get("data_attrs", [])
    progress_indicators = _extract_progress_indicators(all_text)
    page_feedback = _extract_feedback_text(all_text)

    page_text_lines = select_lines_for_budget(
        all_text,
        max_chars=config.ORACLE_PAGE_TEXT_BUDGET_CHARS,
    )
    feedback_lines = select_lines_for_budget(
        page_feedback,
        max_chars=min(1000, config.ORACLE_MEMORY_BUDGET_CHARS),
    )
    progress_lines = select_lines_for_budget(
        progress_indicators,
        max_chars=min(800, config.ORACLE_MEMORY_BUDGET_CHARS),
    )

    # Format elements summary (compact version)
    el_summary_full = format_element_summary(elements)
    el_summary_lines = select_lines_for_budget(
        el_summary_full.splitlines(),
        max_chars=config.ELEMENT_SUMMARY_BUDGET_CHARS,
    )
    el_summary = "\n".join(el_summary_lines)

    # Format Oracle memory (recent non-OK verdicts, most recent first)
    if oracle_history:
        lines: list[str] = []
        for v in reversed(oracle_history):
            entry = f"- {v.status}"
            if v.reason:
                entry += f": {v.reason}"
            lines.append(entry)
        kept = select_lines_for_budget(
            lines, max_chars=config.ORACLE_MEMORY_BUDGET_CHARS
        )
        recent_verdicts = "\n".join(kept) if kept else "none"
    else:
        recent_verdicts = "none (first evaluation)"

    # Build the Oracle prompt with full page context
    hidden_lines = select_lines_for_budget(
        hidden,
        max_chars=config.HIDDEN_CONTENT_BUDGET_CHARS,
    )
    data_attr_lines = select_lines_for_budget(
        data_attrs,
        max_chars=config.DATA_ATTRS_BUDGET_CHARS,
    )
    element_diff_lines = (
        select_lines_for_budget(
            element_diff.splitlines(),
            max_chars=config.DIFF_BUDGET_CHARS,
        )
        if element_diff
        else []
    )
    text_diff_lines = (
        select_lines_for_budget(
            text_diff.splitlines(),
            max_chars=config.DIFF_BUDGET_CHARS,
        )
        if text_diff
        else []
    )

    prompt = ORACLE_PROMPT.format(
        challenge_step_count=challenge_step_count,
        page_feedback="\n".join(feedback_lines) if feedback_lines else "none",
        goal=goal or "(not specified)",
        objective=objective,
        data=data,
        progress=progress,
        action_results=action_results,
        url=content.get("url", "?"),
        title=content.get("title", "?"),
        elements=el_summary,
        hidden_content=", ".join(hidden_lines) if hidden_lines else "none",
        data_attrs=", ".join(data_attr_lines) if data_attr_lines else "none",
        page_text="\n".join(page_text_lines),
        progress_indicators=", ".join(progress_lines)
        if progress_lines
        else "none found",
        state_changes="\n".join(element_diff_lines)
        if element_diff_lines
        else "none detected",
        new_text="\n".join(text_diff_lines) if text_diff_lines else "none",
        recent_verdicts=recent_verdicts,
    )

    try:
        response, usage = await complete(
            client,
            model=cast(str, config.ORACLE_MODEL),
            messages=[
                {
                    "role": "system",
                    "content": "You are the ORACLE supervisor. Output JSON directives. Be decisive.",
                },
                {"role": "user", "content": prompt},
            ],
            max_completion_tokens=1000,
            reasoning_effort=config.REASONING_EFFORT,
            response_model=OracleResponse,
            call_type="oracle",
        )

        if usage:
            log(
                f"  Oracle LLM ({usage.prompt_tokens} prompt + {usage.completion_tokens} completion tokens)"
            )

        log_verbose(
            f"=== Oracle Output ===\n{response.model_dump_json()}\n=== End Oracle Output ==="
        )
        status_msg = f"  Oracle: {response.status}"
        if response.reason:
            reason_preview = response.reason[:60] + (
                "..." if len(response.reason) > 60 else ""
            )
            status_msg += f" - {reason_preview}"
        log(status_msg)
        return response
    except Exception as e:
        log(f"  Oracle error: {e}")
        return OracleResponse(status="WARN", reason=f"Oracle error: {str(e)[:200]}")


async def diagnose_failure(
    client: LLMClient,
    challenge_summary: str,
    content: dict,
    elements: list,
    last_results: list[tuple[dict, str]],
    trigger: str,
    recent_action_sigs: list[str] | None = None,
    failed_attempts: list[str] | None = None,
) -> str:
    """Run a diagnostic LLM call before resetting conversation memory.

    # TODO: Deprecate - replace with structured diagnosis

    Analyzes the failure pattern and produces an informed recovery plan
    so the agent doesn't start blind after a reset.

    Returns an updated challenge_summary in GOAL/DATA/PROGRESS format.
    """
    from .prompts import DIAGNOSIS_PROMPT

    try:
        # Build comprehensive context for diagnosis
        hidden = content.get("hidden_content", [])
        data_attrs = content.get("data_attrs", [])
        all_text = content.get("all_text", [])
        page_text_lines = select_lines_for_budget(
            all_text,
            max_chars=config.DIAGNOSIS_PAGE_TEXT_BUDGET_CHARS,
        )
        el_summary_full = format_element_summary(elements)
        el_summary_lines = select_lines_for_budget(
            el_summary_full.splitlines(),
            max_chars=config.ELEMENT_SUMMARY_BUDGET_CHARS,
        )
        el_summary = "\n".join(el_summary_lines)
        results_summary = _format_results(last_results or [])

        hidden_lines = select_lines_for_budget(
            hidden,
            max_chars=config.HIDDEN_CONTENT_BUDGET_CHARS,
        )
        data_attr_lines = select_lines_for_budget(
            data_attrs,
            max_chars=config.DATA_ATTRS_BUDGET_CHARS,
        )

        parts = [f"FAILURE TRIGGER: {trigger}"]

        if challenge_summary:
            parts.append(f"CURRENT STATE:\n{challenge_summary}")

        if results_summary:
            parts.append(f"RECENT RESULTS:\n{results_summary}")

        parts.append(f"""CURRENT PAGE:
URL: {content.get("url", "?")}
Title: {content.get("title", "?")}

Interactive elements:
{el_summary}

Hidden content: {", ".join(hidden_lines) if hidden_lines else "none"}
Data attributes: {", ".join(data_attr_lines) if data_attr_lines else "none"}

Page text:
{chr(10).join(page_text_lines)}""")

        if recent_action_sigs:
            parts.append(f"RECENT ACTION PATTERN: {' -> '.join(recent_action_sigs)}")

        if failed_attempts:
            parts.append(
                "PREVIOUS FAILED ATTEMPTS:\n"
                + "\n".join(f"- {a}" for a in failed_attempts)
            )

        parts.append(
            "ALLOWED ACTION VERBS: click, type, hover, scroll, wait, drag, draw, watch, key"
        )

        user_message = "\n\n".join(parts)

        log(f"  Running diagnosis ({trigger})...")
        log_verbose(
            f"=== Diagnosis Input ===\n{user_message}\n=== End Diagnosis Input ==="
        )

        content_text, usage = await complete(
            client,
            model=cast(str, config.ORACLE_MODEL),
            messages=[
                {"role": "system", "content": DIAGNOSIS_PROMPT},
                {"role": "user", "content": user_message},
            ],
            max_completion_tokens=3000,
            reasoning_effort=config.REASONING_EFFORT,
            call_type="diagnosis",
        )
        result = content_text or ""

        if usage:
            log(
                f"  Diagnosis LLM ({usage.prompt_tokens} prompt + {usage.completion_tokens} completion tokens)"
            )

        log_verbose(f"=== Diagnosis Output ===\n{result}\n=== End Diagnosis Output ===")

        if result:
            log(
                f"  Diagnosis: {result[:200]}..."
                if len(result) > 200
                else f"  Diagnosis: {result}"
            )
            return result

        log(f"  Diagnosis produced empty response, keeping existing")
        return challenge_summary
    except RuntimeError:
        raise
    except Exception as e:
        log(f"  Diagnosis error: {e}")
        return challenge_summary


async def extract_learning(client: LLMClient, challenge_summary: str) -> str:
    """Extract a general learning from a completed interaction, async."""
    if not challenge_summary:
        return ""
    try:
        response, _usage = await complete(
            client,
            model=cast(str, config.MODEL_NAME),
            messages=[
                {
                    "role": "system",
                    "content": 'Given this interaction summary, extract a general strategy lesson about navigating web pages. NEVER include specific codes, values, URLs, or data from this interaction - only reusable strategies. Output JSON: {"learning": "one sentence strategy lesson"}',
                },
                {"role": "user", "content": challenge_summary},
            ],
            max_completion_tokens=400,
            reasoning_effort=config.REASONING_EFFORT,
            response_model=LearningResponse,
            call_type="learning",
        )
        log(f"  Learning extracted: {response.learning}")
        return response.learning
    except RuntimeError:
        raise
    except Exception as e:
        log(f"  Learning extraction error: {e}")
        return ""


def _format_results(last_results: list[tuple[dict, str]] | None) -> str:
    """Format batch results into a human-readable summary with success/failure indicators."""
    if not last_results:
        return ""
    lines = []
    for action, result in last_results:
        action_summary = f"{action.get('a', '?')}"
        if "n" in action:
            action_summary += f"[{action['n']}]"
        if "v" in action:
            action_summary += f' "{action["v"]}"'
        # Add success/failure indicator
        is_failure = any(
            x in result.lower()
            for x in ("error", "not found", "failed", "timeout", "unknown")
        )
        indicator = "FAILED" if is_failure else "OK"
        lines.append(f"  {action_summary} -> {result} [{indicator}]")
    return "Previous actions:\n" + "\n".join(lines)


def _compute_element_diff(prev_elements: list, elements: list) -> str:
    """Compute diff between previous and current elements, highlighting state changes, new/removed elements."""
    if not prev_elements:
        return ""

    def _norm(v: object) -> str:
        if v is None:
            return ""
        if not isinstance(v, str):
            v = str(v)
        return " ".join(v.split())

    def _el_key(el: dict) -> tuple[str, ...]:
        return (
            _norm(el.get("tag")),
            _norm(el.get("role")),
            _norm(el.get("name")),
            _norm(el.get("text")),
            _norm(el.get("type")),
            _norm(el.get("href")),
        )

    prev_by_key = {}
    for i, el in enumerate(prev_elements):
        key = _el_key(el)
        prev_by_key[key] = (i, el)

    curr_by_key = {}
    for i, el in enumerate(elements):
        key = _el_key(el)
        curr_by_key[key] = (i, el)

    state_changes: list[str] = []
    new_elements: list[str] = []
    removed_elements: list[str] = []

    # Detect state changes and new elements
    for key, (idx, el) in curr_by_key.items():
        if key in prev_by_key:
            prev_idx, prev_el = prev_by_key[key]
            changes: list[str] = []

            prev_state = _norm(prev_el.get("state", ""))
            curr_state = _norm(el.get("state", ""))
            if prev_state != curr_state:
                changes.append(f"state: '{prev_state}' -> '{curr_state}'")

            for flag in ("disabled", "checked", "selected"):
                a = bool(prev_el.get(flag))
                b = bool(el.get(flag))
                if a != b:
                    changes.append(f"{flag}: {a} -> {b}")

            prev_val = _norm(prev_el.get("value", ""))
            curr_val = _norm(el.get("value", ""))
            if prev_val != curr_val and (prev_val or curr_val):
                changes.append("value changed")

            if changes:
                tag = el.get("role") or el["tag"]
                text = _norm(el.get("text", ""))
                state_changes.append(f'[{idx}] {tag} "{text}" ' + ", ".join(changes))
        else:
            tag = el.get("role") or el["tag"]
            text = _norm(el.get("text", ""))
            new_elements.append(f'[{idx}] {tag} "{text}"')

    # Detect removed elements
    for key, (idx, el) in prev_by_key.items():
        if key not in curr_by_key:
            tag = el.get("role") or el["tag"]
            text = _norm(el.get("text", ""))
            removed_elements.append(f'[{idx}] {tag} "{text}"')

    parts: list[str] = []
    if state_changes:
        parts.append("STATE CHANGES:")
        parts.extend(f"- {line}" for line in state_changes)
    if new_elements:
        parts.append("NEW ELEMENTS:")
        parts.extend(f"- {line}" for line in new_elements)
    if removed_elements:
        parts.append("REMOVED ELEMENTS:")
        parts.extend(f"- {line}" for line in removed_elements)
    return "\n".join(parts)


def _compute_text_diff(prev_text: list[str], curr_text: list[str]) -> str:
    """Compute diff between previous and current page text, highlighting new lines."""
    if not prev_text:
        return ""

    prev_set = set(prev_text)
    new_lines = [line for line in curr_text if line not in prev_set]

    if not new_lines:
        return ""

    lines = ["NEW TEXT APPEARED:"]
    lines.extend(f"- {line}" for line in new_lines)
    return "\n".join(lines)


async def analyze_overview(
    client: LLMClient,
    content: dict,
    elements: list,
    memory: list,
    goal: str | None = None,
    last_results: list[tuple[dict, str]] | None = None,
    state_changed: bool = True,
    unchanged_count: int = 0,
    challenge_summary: str = "",
    prev_elements: list | None = None,
    last_action_pos: tuple[int, int] | None = None,
    prev_all_text: list[str] | None = None,
    prev_filtered_text: list[str] | None = None,
    oracle_verdict: OracleResponse | None = None,
    failed_attempts: list[str] | None = None,
    grounding_note: str | None = None,
) -> tuple[OverviewResponse, str, list[str]]:
    """Overview agent - analyzes full page with memory of previous actions.

    Returns:
        tuple: (OverviewResponse, updated_challenge_summary, filtered_text)
    """
    from .prompts import OVERVIEW_PROMPT

    # Update system message with persistent challenge summary
    system_content = OVERVIEW_PROMPT
    if challenge_summary:
        system_content += f"\n\nFailure analysis - use this to avoid repeating mistakes:\n{challenge_summary}"
    memory[0] = {"role": "system", "content": system_content}

    # Build structured content (replaces noisy full_text dump)
    hidden = content.get("hidden_content", [])
    data_attrs = content.get("data_attrs", [])

    # Use deduped all_text (covers all HTML tags)
    all_text = content.get("all_text", [])
    filtered_text = await filter_page_content(client, all_text)
    if len(filtered_text) != len(all_text):
        log(f"  Filtered text: {len(all_text)} -> {len(filtered_text)} items")
    structured_lines = select_lines_for_budget(
        filtered_text,
        max_chars=config.OVERVIEW_PAGE_TEXT_BUDGET_CHARS,
    )
    structured_text = "\n".join(structured_lines)

    el_summary_full = format_elements_by_proximity(elements, last_action_pos)

    def _score_el_line(line: str) -> int:
        s = line.strip().lower()
        if not s:
            return 0
        if s.startswith("nearby") or s.startswith("other"):
            return 100
        # Prefer inputs/selects and things with embedded values.
        if any(x in s for x in (" inp ", " txt ", " sel ", " textbox", "combobox")):
            return 60
        if ' data="' in s or ' value="' in s:
            return 50
        return 10

    el_summary_lines = select_lines_for_budget(
        el_summary_full.splitlines(),
        max_chars=config.ELEMENT_SUMMARY_BUDGET_CHARS,
        score_fn=_score_el_line,
    )
    el_summary = "\n".join(el_summary_lines)

    # Deduplicate: remove data_attrs already present in element dataValue fields
    el_data_values = set()
    for el in elements:
        if el.get("dataValue"):
            for attr in el["dataValue"].split("; "):
                el_data_values.add(attr.strip())
    data_attrs = [a for a in data_attrs if a not in el_data_values]

    hidden_lines = select_lines_for_budget(
        hidden,
        max_chars=config.HIDDEN_CONTENT_BUDGET_CHARS,
    )
    data_attr_lines = select_lines_for_budget(
        data_attrs,
        max_chars=config.DATA_ATTRS_BUDGET_CHARS,
    )

    page_content = f"""URL: {content["url"]}
Title: {content["title"]}

Interactive elements:
{el_summary}

 Hidden content: {", ".join(hidden_lines) if hidden_lines else "none"}
Data attributes: {", ".join(data_attr_lines) if data_attr_lines else "none"}

Page content:
{structured_text}"""

    # Add warning if state unchanged
    if not state_changed:
        page_content += f"\n\n*** WARNING: State unchanged for {unchanged_count} iterations! Your previous action had NO effect. You MUST try a COMPLETELY DIFFERENT approach. ***"

    # Combine previous results with current page state into single user message
    combined_content = ""

    fixed_goal = goal or getattr(config, "CHALLENGE_GOAL", "")
    if fixed_goal:
        combined_content += f"FIXED GOAL:\n{fixed_goal}\n\n"

    # Add Oracle directive (placed near the top, right after fixed goal).
    if oracle_verdict and oracle_verdict.status in (
        "WARN",
        "OVERRIDE",
        "WRONG_GOAL",
    ):
        directive_parts = [f"ORACLE DIRECTIVE ({oracle_verdict.status}):"]
        if oracle_verdict.reason:
            directive_parts.append(f"Reason: {oracle_verdict.reason}")
        if oracle_verdict.status == "WRONG_GOAL":
            directive_parts.append(
                "Your current objective/interpretation is invalid. Re-read the page and re-evaluate what is required."
            )
        if oracle_verdict.next_directive:
            directive_parts.append(f"Directive: {oracle_verdict.next_directive}")
        if oracle_verdict.avoid:
            directive_parts.append(f"AVOID: {oracle_verdict.avoid}")
        combined_content += "\n".join(directive_parts) + "\n\n"

    if grounding_note:
        combined_content += f"GROUNDING NOTE:\n{grounding_note.strip()}\n\n"

    if failed_attempts:
        kept = select_lines_for_budget(
            failed_attempts,
            max_chars=config.FAILED_ATTEMPTS_BUDGET_CHARS,
        )
        if kept:
            combined_content += "PREVIOUSLY TRIED (avoid repeating exact patterns):\n"
            combined_content += "\n".join(f"- {a}" for a in kept) + "\n\n"
    results_summary = _format_results(last_results or [])
    if results_summary:
        combined_content += results_summary + "\n\n"
    # Add element state diff
    diff_summary = _compute_element_diff(prev_elements or [], elements)
    if diff_summary:
        diff_lines = select_lines_for_budget(
            diff_summary.splitlines(),
            max_chars=config.DIFF_BUDGET_CHARS,
        )
        if diff_lines:
            combined_content += "\n".join(diff_lines) + "\n\n"
    # Add text diff to highlight new text (feedback messages, errors, etc.)
    text_diff = _compute_text_diff(prev_filtered_text or [], filtered_text)
    if text_diff:
        text_lines = select_lines_for_budget(
            text_diff.splitlines(),
            max_chars=config.DIFF_BUDGET_CHARS,
        )
        if text_lines:
            combined_content += "\n".join(text_lines) + "\n\n"
    combined_content += (
        f"Current page state:\n{page_content}\n\nWhat should we do next?"
    )

    memory.append({"role": "user", "content": combined_content})

    # Log full LLM input to verbose log
    log_verbose(
        f"=== Overview LLM Input (Step) ===\n{combined_content}\n=== End Overview LLM Input ==="
    )

    # Limit memory - increased window since per-message size is smaller now
    if len(memory) > 19:
        memory[:] = [memory[0]] + memory[-16:]

    try:
        response, usage = await complete(
            client,
            model=cast(str, config.MODEL_NAME),
            messages=memory,
            max_completion_tokens=1400,
            reasoning_effort=config.REASONING_EFFORT,
            response_model=OverviewResponse,
            call_type="overview",
        )

        if usage:
            log(
                f"  Overview LLM ({len(memory)} msgs, {usage.prompt_tokens} prompt + {usage.completion_tokens} completion tokens):"
            )

        # Add response to memory as raw JSON
        memory.append(
            {
                "role": "assistant",
                "content": response.model_dump_json(exclude_none=True),
            }
        )

        # Reconstruct challenge_summary from fields (persists across memory truncation)
        parts = []
        if response.objective:
            parts.append(f"OBJECTIVE: {response.objective}")
        if response.data:
            parts.append(f"DATA: {response.data}")
        if response.progress:
            parts.append(f"PROGRESS: {response.progress}")
        updated_summary = "\n".join(parts) if parts else challenge_summary

        return (response, updated_summary, filtered_text)
    except RuntimeError:
        raise
    except Exception as e:
        log(f"Overview agent error: {e}")
        fallback = OverviewResponse(
            objective="Make progress toward completing the current page task",
            next="Click or scroll to discover required values",
            task="scroll 500",
        )
        return (fallback, challenge_summary, filtered_text)


def _convert_tool_call(action: dict) -> dict:
    """Convert tool-calling format to standard action format."""
    if "arguments" in action and "name" in action:
        name = action["name"].lower()
        args = action["arguments"]
        if "click" in name:
            return {"a": "click", "n": args.get("n", 0)}
        elif "type" in name:
            return {"a": "type", "n": args.get("n", 0), "v": args.get("v", "")}
        elif "scroll" in name:
            return {"a": "scroll", "v": args.get("v", "down")}
    return action


async def llm_decide(
    client: LLMClient,
    messages: list,
    context: str,
    last_results: list[tuple[dict, str]] | None = None,
) -> list[dict]:
    """Get next action(s) from LLM with challenge-level memory.

    Returns a list of action dicts (always a list, even for single actions).
    """

    # Add previous results to context for sequencing awareness
    results_summary = _format_results(last_results or [])
    if results_summary:
        messages.append({"role": "user", "content": results_summary})

    messages.append({"role": "user", "content": context})

    # Log full LLM input to verbose log
    log_verbose(
        f"=== Action LLM Input (Step) ===\n{context}\n=== End Action LLM Input ==="
    )

    # Less aggressive truncation - keep more history within challenge
    if len(messages) > 20:
        messages[:] = [messages[0]] + messages[-18:]

    try:
        response, usage = await complete(
            client,
            model=cast(str, config.ACTION_MODEL_NAME),
            messages=messages,
            max_completion_tokens=700,
            response_model=ActionResponse,
            call_type="action",
        )
    except RuntimeError:
        raise
    except Exception as e:
        log(f"  ERROR: LLM call failed - {e}")
        return [{"a": "error", "error": str(e)}]

    if usage:
        log(
            f"  Action LLM ({len(messages)} msgs, {usage.prompt_tokens} prompt + {usage.completion_tokens} completion tokens):"
        )

    # Convert ActionItem models to dicts
    actions = [item.model_dump(exclude_none=True) for item in response.actions]

    # Log raw action LLM output
    raw_json = response.model_dump_json(exclude_none=True)
    log(f"  Action LLM: {raw_json}")
    messages.append({"role": "assistant", "content": raw_json})

    # Apply tool-call conversion and batch limit
    actions = [_convert_tool_call(a) for a in actions][:MAX_BATCH_SIZE]

    return actions
