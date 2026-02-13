import json
import re
from groq import AsyncGroq
import asyncio
from . import config
from .config import MAX_BATCH_SIZE
from .element_utils import format_element_summary, format_elements_by_proximity
from .logging_utils import log, log_verbose


def _strip_think_tags(text: str) -> str:
    """Remove <think>...</think> tags and their content from model output."""
    if not text:
        return text
    # Handle both closed tags and unclosed tags (truncated output)
    return re.sub(r'<think>.*?</think>|<think>.*', '', text, flags=re.DOTALL).strip()


def _extract_summary(text: str) -> str:
    """Parse GOAL, DATA, and PROGRESS sections from overview LLM response.

    Returns a compact summary string to persist in the system message.
    Captures multi-line content (bullet lists, etc.) not just the first line.
    """
    lines = []
    for label in ("GOAL", "TASK", "DATA", "PROGRESS"):
        pattern = rf'^{label}:\s*(.*?)(?=^(?:GOAL|DATA|PROGRESS|NEXT):|\Z)'
        match = re.search(pattern, text, re.MULTILINE | re.IGNORECASE | re.DOTALL)
        if match:
            content = match.group(1).strip()
            if content:
                lines.append(f"{label}: {content}")
    return "\n".join(lines) if lines else ""


def parse_actions_from_overview(overview: str) -> list[dict] | None:
    """Extract JSON actions from NEXT section of overview output.

    Returns list of action dicts if parsing succeeds, None otherwise.
    """
    # Find NEXT section
    next_match = re.search(r'^NEXT:\s*(.+?)(?=^[A-Z]+:|\Z)', overview, re.MULTILINE | re.DOTALL | re.IGNORECASE)
    if not next_match:
        log("  [parse_actions] No NEXT section found")
        return None

    next_content = next_match.group(1).strip()

    # Try to find JSON object in NEXT content
    # Look for {"actions": [...]} pattern
    json_match = re.search(r'\{[^{}]*"actions"\s*:\s*\[[^\]]*\][^{}]*\}', next_content, re.DOTALL)
    if not json_match:
        # Try simpler pattern - just find any JSON object
        json_match = re.search(r'\{.*\}', next_content, re.DOTALL)

    if not json_match:
        log(f"  [parse_actions] No JSON found in NEXT: {next_content[:100]}")
        return None

    json_str = json_match.group(0)

    try:
        parsed = json.loads(json_str)

        # Handle {"actions": [...]} format
        if isinstance(parsed, dict) and "actions" in parsed:
            actions = parsed["actions"]
            if isinstance(actions, list) and len(actions) > 0:
                # Validate each action has required 'a' field
                valid_actions = [a for a in actions if isinstance(a, dict) and "a" in a]
                if valid_actions:
                    return valid_actions[:MAX_BATCH_SIZE]

        # Handle single action {"a": "click", "n": 0}
        if isinstance(parsed, dict) and "a" in parsed:
            return [parsed]

        log(f"  [parse_actions] Invalid action format: {json_str[:100]}")
        return None

    except json.JSONDecodeError as e:
        log(f"  [parse_actions] JSON parse error: {e}")
        return None

async def filter_page_content(client: AsyncGroq, all_text: list[str]) -> list[str]:
    """Filter filler text (lorem ipsum, section headers, repeated patterns) using a small LLM."""
    if len(all_text) <= 30:
        return all_text

    prompt = "Extract ONLY useful text lines (task instructions, codes, values, form labels, errors). Remove filler (section headers, lorem ipsum, repeated patterns). Return one line per output line, no numbering."

    async def _filter_chunk(chunk: list[str]) -> list[str]:
        numbered = "\n".join(f"{i+1}. {line}" for i, line in enumerate(chunk))
        response = await client.chat.completions.create(
            model=config.FILTER_MODEL_NAME,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": numbered},
            ],
            max_completion_tokens=500,
            temperature=0,
        )
        result = _strip_think_tags(response.choices[0].message.content)
        return [line.strip() for line in result.splitlines() if line.strip()]

    try:
        if len(all_text) <= 100:
            return await _filter_chunk(all_text)

        # Map-reduce: chunk into groups of 100, filter in parallel
        chunks = [all_text[i:i+100] for i in range(0, len(all_text), 100)]
        results = await asyncio.gather(*[_filter_chunk(c) for c in chunks])
        combined = []
        for r in results:
            combined.extend(r)
        return combined
    except Exception as e:
        log(f"  Filter error (returning unfiltered): {e}")
        return all_text

def _extract_progress_indicators(all_text: list[str]) -> list[str]:
    """Find progress patterns like 'Step 2/6', '3 of 5', 'Progress: 50%'."""
    patterns = [
        r'\d+\s*/\s*\d+',           # 2/6, 3 / 5
        r'\d+\s+of\s+\d+',          # 3 of 5
        r'step\s*\d+',              # step 2, Step 3
        r'progress[:\s]+\d+%?',     # Progress: 50%
        r'challenge\s*\d+',         # Challenge 5
    ]
    indicators = []
    for text in all_text[:50]:
        for pattern in patterns:
            if re.search(pattern, text.lower()):
                indicators.append(text.strip())
                break
    return indicators[:5]


def _extract_feedback_text(all_text: list[str]) -> list[str]:
    """Extract error/warning/feedback lines from page text."""
    keywords = ['wrong', 'error', 'failed', 'invalid', 'try again', 'incorrect', 'warning']
    feedback = []
    for line in all_text:
        if any(kw in line.lower() for kw in keywords):
            feedback.append(line.strip())
    return feedback[:10]


async def evaluate_step(
    client: AsyncGroq,
    overview: str,           # Full overview output with GOAL/TASK/DATA/PROGRESS
    actions: list[dict],     # Actions that were executed
    results: list[tuple],    # (action, result_string) tuples
    content: dict,           # Page content
    elements: list,          # Current elements
    element_diff: str,       # From _compute_element_diff()
    text_diff: str,          # From _compute_text_diff()
    challenge_step_count: int = 0,  # Steps spent on current challenge
) -> dict:
    """Oracle evaluation of step progress. Returns structured dict with status and directives."""
    from .prompts import ORACLE_PROMPT

    # Parse GOAL/TASK/DATA/PROGRESS sections from overview
    def _extract_section(text: str, label: str) -> str:
        pattern = rf'^{label}:\s*(.*?)(?=^(?:GOAL|TASK|DATA|PROGRESS|NEXT):|\Z)'
        match = re.search(pattern, text, re.MULTILINE | re.IGNORECASE | re.DOTALL)
        return match.group(1).strip() if match else ""

    goal = _extract_section(overview, "GOAL")
    task = _extract_section(overview, "TASK")
    data = _extract_section(overview, "DATA")
    progress = _extract_section(overview, "PROGRESS")

    # Format action results
    action_results = _format_results(results) if results else "No actions executed"

    # Extract page content for Oracle context
    all_text = content.get('all_text', [])
    hidden = content.get('hidden_content', [])
    data_attrs = content.get('data_attrs', [])
    progress_indicators = _extract_progress_indicators(all_text)
    page_feedback = _extract_feedback_text(all_text)

    # Format elements summary (compact version)
    el_summary = format_element_summary(elements)

    # Build the Oracle prompt with full page context
    prompt = ORACLE_PROMPT.format(
        challenge_step_count=challenge_step_count,
        page_feedback='\n'.join(page_feedback) if page_feedback else 'none',
        goal=goal or "(not specified)",
        task=task or "(not specified)",
        data=data or "(none)",
        progress=progress or "(none)",
        action_results=action_results,
        url=content.get('url', '?'),
        title=content.get('title', '?'),
        elements=el_summary,
        hidden_content=', '.join(hidden) if hidden else 'none',
        data_attrs=', '.join(data_attrs) if data_attrs else 'none',
        page_text='\n'.join(all_text),  # Full page text
        progress_indicators=", ".join(progress_indicators) if progress_indicators else "none found",
        state_changes=element_diff if element_diff else "none detected",
        new_text=text_diff if text_diff else "none",
    )

    def _parse_oracle_output(text: str) -> dict:
        """Parse structured Oracle output into dict."""
        result = {
            "status": "OK",
            "reason": None,
            "correct_goal": None,
            "next_actions": None,
            "avoid": None,
            "evidence": None,
            "explore": None,
            "raw": text
        }

        # Extract STATUS
        status_match = re.search(r'^STATUS:\s*(OK|WARN|REDIRECT|OVERRIDE|WRONG_GOAL)', text, re.MULTILINE | re.IGNORECASE)
        if status_match:
            result["status"] = status_match.group(1).upper()

        # Extract REASON/ISSUE
        reason_match = re.search(r'^(?:REASON|ISSUE):\s*(.+?)(?=^[A-Z_]+:|\Z)', text, re.MULTILINE | re.DOTALL)
        if reason_match:
            result["reason"] = reason_match.group(1).strip()

        # Extract CORRECT_GOAL
        goal_match = re.search(r'^CORRECT_GOAL:\s*(.+?)(?=^[A-Z_]+:|\Z)', text, re.MULTILINE | re.DOTALL)
        if goal_match:
            result["correct_goal"] = goal_match.group(1).strip()

        # Extract NEXT_ACTIONS - look for JSON
        actions_match = re.search(r'^NEXT_ACTIONS:\s*(\{.*\})', text, re.MULTILINE | re.DOTALL)
        if actions_match:
            try:
                actions_json = json.loads(actions_match.group(1))
                if isinstance(actions_json, dict) and "actions" in actions_json:
                    result["next_actions"] = actions_json["actions"][:MAX_BATCH_SIZE]
            except json.JSONDecodeError:
                pass

        # Extract AVOID
        avoid_match = re.search(r'^AVOID:\s*(.+?)(?=^[A-Z_]+:|\Z)', text, re.MULTILINE | re.DOTALL)
        if avoid_match:
            result["avoid"] = avoid_match.group(1).strip()

        # Extract EVIDENCE (for WRONG_GOAL)
        evidence_match = re.search(r'^EVIDENCE:\s*(.+?)(?=^[A-Z_]+:|\Z)', text, re.MULTILINE | re.DOTALL)
        if evidence_match:
            result["evidence"] = evidence_match.group(1).strip()

        # Extract EXPLORE (for WRONG_GOAL)
        explore_match = re.search(r'^EXPLORE:\s*(.+?)(?=^[A-Z_]+:|\Z)', text, re.MULTILINE | re.DOTALL)
        if explore_match:
            result["explore"] = explore_match.group(1).strip()

        return result

    try:
        kwargs = dict(
            model=config.ORACLE_MODEL,
            messages=[
                {"role": "system", "content": "You are the ORACLE supervisor. Output structured directives. Be decisive."},
                {"role": "user", "content": prompt},
            ],
            max_completion_tokens=500,  # More tokens for OVERRIDE with actions
            temperature=0,
        )
        if config.REASONING_EFFORT:
            kwargs["reasoning_effort"] = config.REASONING_EFFORT
        response = await client.chat.completions.create(**kwargs)
        result = _strip_think_tags(response.choices[0].message.content)

        usage = response.usage
        if usage:
            log(f"  Oracle LLM ({usage.prompt_tokens} prompt + {usage.completion_tokens} completion tokens)")

        if result:
            log_verbose(f"=== Oracle Output ===\n{result}\n=== End Oracle Output ===")
            parsed = _parse_oracle_output(result)
            # Log status and reason
            status_msg = f"  Oracle: {parsed['status']}"
            if parsed['reason']:
                status_msg += f" - {parsed['reason'][:60]}{'...' if len(parsed['reason']) > 60 else ''}"
            log(status_msg)
            return parsed

        return {"status": "OK", "reason": None, "correct_goal": None, "next_actions": None, "avoid": None, "raw": ""}
    except Exception as e:
        log(f"  Oracle error: {e}")
        return {"status": "OK", "reason": None, "correct_goal": None, "next_actions": None, "avoid": None, "raw": ""}


async def diagnose_failure(client: AsyncGroq, challenge_summary: str, content: dict,
                           elements: list, last_results: list[tuple[dict, str]],
                           trigger: str,
                           recent_action_sigs: list[str] = None) -> str:
    """Run a diagnostic LLM call before resetting conversation memory.

    Analyzes the failure pattern and produces an informed recovery plan
    so the agent doesn't start blind after a reset.

    Returns an updated challenge_summary in GOAL/DATA/PROGRESS format.
    """
    from .prompts import DIAGNOSIS_PROMPT, SYSTEM_PROMPT

    try:
        # Build comprehensive context for diagnosis
        hidden = content.get('hidden_content', [])
        data_attrs = content.get('data_attrs', [])
        all_text = content.get('all_text', [])
        el_summary = format_element_summary(elements)
        results_summary = _format_results(last_results or [])

        parts = [f"FAILURE TRIGGER: {trigger}"]

        if challenge_summary:
            parts.append(f"CURRENT STATE:\n{challenge_summary}")

        if results_summary:
            parts.append(f"RECENT RESULTS:\n{results_summary}")

        parts.append(f"""CURRENT PAGE:
URL: {content.get('url', '?')}
Title: {content.get('title', '?')}

Interactive elements:
{el_summary}

Hidden content: {', '.join(hidden) if hidden else 'none'}
Data attributes: {', '.join(data_attrs) if data_attrs else 'none'}

Page text:
{chr(10).join(all_text[:80])}""")

        if recent_action_sigs:
            parts.append(f"RECENT ACTION PATTERN: {' -> '.join(recent_action_sigs)}")

        parts.append(f"AVAILABLE ACTIONS (only these):\n{SYSTEM_PROMPT}")

        user_message = "\n\n".join(parts)

        log(f"  Running diagnosis ({trigger})...")
        log_verbose(f"=== Diagnosis Input ===\n{user_message}\n=== End Diagnosis Input ===")

        kwargs = dict(
            model=config.ORACLE_MODEL,
            messages=[
                {"role": "system", "content": DIAGNOSIS_PROMPT},
                {"role": "user", "content": user_message},
            ],
            max_completion_tokens=1500,
            temperature=0,
        )
        if config.REASONING_EFFORT:
            kwargs["reasoning_effort"] = config.REASONING_EFFORT
        response = await client.chat.completions.create(**kwargs)
        result = _strip_think_tags(response.choices[0].message.content)

        usage = response.usage
        if usage:
            log(f"  Diagnosis LLM ({usage.prompt_tokens} prompt + {usage.completion_tokens} completion tokens)")

        log_verbose(f"=== Diagnosis Output ===\n{result}\n=== End Diagnosis Output ===")

        if result:
            log(f"  Diagnosis: {result[:200]}..." if len(result) > 200 else f"  Diagnosis: {result}")
            return result

        log(f"  Diagnosis produced empty response, keeping existing")
        return challenge_summary
    except Exception as e:
        log(f"  Diagnosis error: {e}")
        return challenge_summary


async def extract_learning(client: AsyncGroq, challenge_summary: str) -> str:
    """Extract a general learning from a completed interaction, async."""
    if not challenge_summary:
        return ""
    try:
        kwargs = dict(
            model=config.MODEL_NAME,
            messages=[
                {"role": "system", "content": "Given this interaction summary, extract a general strategy lesson about navigating web pages. NEVER include specific codes, values, URLs, or data from this interaction — only reusable strategies. One sentence max."},
                {"role": "user", "content": challenge_summary},
            ],
            max_completion_tokens=600,
            temperature=0,
        )
        if config.REASONING_EFFORT:
            kwargs["reasoning_effort"] = config.REASONING_EFFORT
        response = await client.chat.completions.create(**kwargs)
        result = _strip_think_tags(response.choices[0].message.content)
        log(f"  Learning extracted: {result}")
        return result
    except Exception as e:
        log(f"  Learning extraction error: {e}")
        return ""


def _format_results(last_results: list[tuple[dict, str]]) -> str:
    """Format batch results into a human-readable summary with success/failure indicators."""
    if not last_results:
        return ""
    lines = []
    for action, result in last_results:
        action_summary = f"{action.get('a', '?')}"
        if 'n' in action:
            action_summary += f"[{action['n']}]"
        if 'v' in action:
            action_summary += f" \"{action['v']}\""
        # Add success/failure indicator
        is_failure = any(x in result.lower() for x in ("error", "not found", "failed", "timeout", "unknown"))
        indicator = "FAILED" if is_failure else "OK"
        lines.append(f"  {action_summary} -> {result} [{indicator}]")
    return "Previous actions:\n" + "\n".join(lines)


def _compute_element_diff(prev_elements: list, elements: list) -> str:
    """Compute diff between previous and current elements, highlighting state changes, new/removed elements."""
    if not prev_elements:
        return ""

    def _el_key(el):
        return f"{el['tag']}:{el.get('text', '')[:30]}"

    prev_by_key = {}
    for i, el in enumerate(prev_elements):
        key = _el_key(el)
        prev_by_key[key] = (i, el)

    curr_by_key = {}
    for i, el in enumerate(elements):
        key = _el_key(el)
        curr_by_key[key] = (i, el)

    state_changes = []
    new_elements = []
    removed_elements = []

    # Detect state changes and new elements
    for key, (idx, el) in curr_by_key.items():
        if key in prev_by_key:
            prev_idx, prev_el = prev_by_key[key]
            prev_state = prev_el.get('state', '')
            curr_state = el.get('state', '')
            if prev_state != curr_state:
                tag = el.get('role') or el['tag']
                text = el.get('text', '')[:20]
                changes = []
                if 'disabled' in prev_state and 'disabled' not in curr_state:
                    changes.append("now enabled (was disabled)")
                elif 'disabled' not in prev_state and 'disabled' in curr_state:
                    changes.append("now disabled")
                if 'checked' not in prev_state and 'checked' in curr_state:
                    changes.append("now checked")
                elif 'checked' in prev_state and 'checked' not in curr_state:
                    changes.append("now unchecked")
                if not changes:
                    changes.append(f"state: '{prev_state}' -> '{curr_state}'")
                state_changes.append(f"[{idx}] {tag} \"{text}\" {', '.join(changes)}")
        else:
            tag = el.get('role') or el['tag']
            text = el.get('text', '')[:20]
            new_elements.append(f"[{idx}] {tag} \"{text}\"")

    # Detect removed elements
    for key, (idx, el) in prev_by_key.items():
        if key not in curr_by_key:
            tag = el.get('role') or el['tag']
            text = el.get('text', '')[:20]
            removed_elements.append(f"{tag} \"{text}\"")

    parts = []
    if state_changes:
        parts.append("*** State changes: " + " | ".join(state_changes[:10]) + " ***")
    if new_elements:
        parts.append("*** New elements: " + ", ".join(new_elements[:10]) + " ***")
    if removed_elements:
        parts.append("*** Removed elements: " + ", ".join(removed_elements[:10]) + " ***")
    return "\n".join(parts)


def _compute_text_diff(prev_text: list[str], curr_text: list[str]) -> str:
    """Compute diff between previous and current page text, highlighting new lines."""
    if not prev_text:
        return ""

    prev_set = set(prev_text)
    new_lines = [line for line in curr_text if line not in prev_set]

    if not new_lines:
        return ""

    # Limit to first 10 new lines to avoid noise
    preview = new_lines[:10]
    result = "*** New text appeared: " + " | ".join(preview)
    if len(new_lines) > 10:
        result += f" ... (+{len(new_lines) - 10} more)"
    return result + " ***"


async def analyze_overview(client: AsyncGroq, content: dict, elements: list, memory: list,
                           last_results: list[tuple[dict, str]] = None,
                           state_changed: bool = True, unchanged_count: int = 0,
                           challenge_summary: str = "",
                           prev_elements: list = None,
                           last_action_pos: tuple = None,
                           prev_all_text: list[str] = None,
                           oracle_verdict: dict = None) -> tuple[str, str]:
    """Overview agent - analyzes full page with memory of previous actions.

    Args:
        client: Groq client
        content: Structured page content from extract_structured_content()
        elements: List of interactive elements with indices
        memory: List of previous messages for context (modified in place)
        last_results: List of (action_dict, result_string) tuples from previous step
        state_changed: Whether page state changed since last step
        unchanged_count: Number of consecutive unchanged states
        challenge_summary: Persistent summary from previous steps (survives truncation)
        prev_elements: Elements from previous step for diff computation

    Returns:
        tuple: (overview_text, updated_challenge_summary)
    """
    from .prompts import OVERVIEW_PROMPT

    # Update system message with persistent challenge summary
    system_content = OVERVIEW_PROMPT
    if challenge_summary:
        system_content += f"\n\nFailure analysis — use this to avoid repeating mistakes:\n{challenge_summary}"
    memory[0] = {"role": "system", "content": system_content}

    # Build structured content (replaces noisy full_text dump)
    hidden = content.get('hidden_content', [])
    data_attrs = content.get('data_attrs', [])

    # Use deduped all_text (covers all HTML tags)
    all_text = content.get('all_text', [])
    filtered_text = await filter_page_content(client, all_text)
    if len(filtered_text) != len(all_text):
        log(f"  Filtered text: {len(all_text)} -> {len(filtered_text)} items")
    structured_text = "\n".join(filtered_text)

    el_summary = format_elements_by_proximity(elements, last_action_pos)

    # Deduplicate: remove data_attrs already present in element dataValue fields
    el_data_values = set()
    for el in elements:
        if el.get('dataValue'):
            for attr in el['dataValue'].split('; '):
                el_data_values.add(attr.strip())
    data_attrs = [a for a in data_attrs if a not in el_data_values]

    page_content = f"""URL: {content['url']}
Title: {content['title']}

Interactive elements:
{el_summary}

Hidden content: {', '.join(hidden) if hidden else 'none'}
Data attributes: {', '.join(data_attrs) if data_attrs else 'none'}

Page content:
{structured_text}"""

    # Add warning if state unchanged
    if not state_changed:
        page_content += f"\n\n*** WARNING: State unchanged for {unchanged_count} iterations! Your previous action had NO effect. You MUST try a COMPLETELY DIFFERENT approach. ***"

    # Combine previous results with current page state into single user message
    combined_content = ""
    # Add Oracle directive if not OK
    if oracle_verdict and oracle_verdict.get("status") in ("WARN", "REDIRECT", "OVERRIDE", "WRONG_GOAL"):
        directive_parts = [f"ORACLE DIRECTIVE ({oracle_verdict['status']}):"]
        if oracle_verdict.get("reason"):
            directive_parts.append(f"Reason: {oracle_verdict['reason']}")
        if oracle_verdict.get("status") == "WRONG_GOAL":
            directive_parts.append("YOUR CURRENT GOAL IS INVALID. You must completely re-evaluate what this page requires.")
            if oracle_verdict.get("evidence"):
                directive_parts.append(f"Evidence: {oracle_verdict['evidence']}")
            if oracle_verdict.get("explore"):
                directive_parts.append(f"Explore: {oracle_verdict['explore']}")
        if oracle_verdict.get("correct_goal"):
            directive_parts.append(f"Correct Goal: {oracle_verdict['correct_goal']}")
        if oracle_verdict.get("avoid"):
            directive_parts.append(f"AVOID: {oracle_verdict['avoid']}")
        combined_content = "\n".join(directive_parts) + "\n\n"
    results_summary = _format_results(last_results or [])
    if results_summary:
        combined_content += results_summary + "\n\n"
    # Add element state diff
    diff_summary = _compute_element_diff(prev_elements or [], elements)
    if diff_summary:
        combined_content += diff_summary + "\n\n"
    # Add text diff to highlight new text (feedback messages, errors, etc.)
    text_diff = _compute_text_diff(prev_all_text or [], filtered_text)
    if text_diff:
        combined_content += text_diff + "\n\n"
    combined_content += f"Current page state:\n{page_content}\n\nWhat should we do next?"

    memory.append({
        "role": "user",
        "content": combined_content
    })

    # Log full LLM input to verbose log
    log_verbose(f"=== Overview LLM Input (Step) ===\n{combined_content}\n=== End Overview LLM Input ===")

    # Limit memory — increased window since per-message size is smaller now
    if len(memory) > 19:
        memory[:] = [memory[0]] + memory[-16:]

    try:
        kwargs = dict(
            model=config.MODEL_NAME,
            messages=memory,
            max_completion_tokens=800,
            temperature=0,
        )
        if config.REASONING_EFFORT:
            kwargs["reasoning_effort"] = config.REASONING_EFFORT
        response = await client.chat.completions.create(**kwargs)
        result = _strip_think_tags(response.choices[0].message.content)

        # Log token usage
        usage = response.usage
        if usage:
            log(f"  Overview LLM ({len(memory)} msgs, {usage.prompt_tokens} prompt + {usage.completion_tokens} completion tokens):")

        # Add response to memory
        memory.append({"role": "assistant", "content": result})

        if not result:
            return ("GOAL: Complete the page task\nDATA: Check content\nPROGRESS: Starting\nNEXT: Interact with elements", challenge_summary)

        # Extract and update persistent summary
        new_summary = _extract_summary(result)
        updated_summary = new_summary if new_summary else challenge_summary

        return (result, updated_summary)
    except Exception as e:
        err_msg = str(e).lower()
        if any(k in err_msg for k in ("not supported", "invalid model", "model not found", "authentication", "api key")):
            raise RuntimeError(f"Model config error: {e}") from e
        log(f"Overview agent error: {e}")
        return ("GOAL: Complete the page task\nDATA: Check page content\nPROGRESS: Starting\nNEXT: Interact with elements", challenge_summary)

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

async def llm_decide(client: AsyncGroq, messages: list, context: str, last_results: list[tuple[dict, str]] = None) -> list[dict]:
    """Get next action(s) from LLM with challenge-level memory.

    Returns a list of action dicts (always a list, even for single actions).
    """

    # Add previous results to context for sequencing awareness
    results_summary = _format_results(last_results or [])
    if results_summary:
        messages.append({
            "role": "user",
            "content": results_summary
        })

    messages.append({"role": "user", "content": context})

    # Log full LLM input to verbose log
    log_verbose(f"=== Action LLM Input (Step) ===\n{context}\n=== End Action LLM Input ===")

    # Less aggressive truncation - keep more history within challenge
    if len(messages) > 20:
        messages[:] = [messages[0]] + messages[-18:]

    try:
        response = await client.chat.completions.create(
            model=config.ACTION_MODEL_NAME,
            messages=messages,
            max_completion_tokens=350,
            temperature=0,
            response_format={"type": "json_object"},
        )
    except Exception as e:
        err_msg = str(e).lower()
        if any(k in err_msg for k in ("not supported", "invalid model", "model not found", "authentication", "api key")):
            raise RuntimeError(f"Model config error: {e}") from e
        log(f"  ERROR: LLM call failed - {e}")
        return [{"a": "error", "error": str(e)}]

    # Log token usage
    usage = response.usage
    if usage:
        log(f"  Action LLM ({len(messages)} msgs, {usage.prompt_tokens} prompt + {usage.completion_tokens} completion tokens):")

    content = _strip_think_tags(response.choices[0].message.content)
    if not content:
        log("  Action LLM: (empty response)")
        return [{"a": "error", "error": "Empty response"}]
    # Log raw action LLM output
    log(f"  Action LLM: {content}")
    messages.append({"role": "assistant", "content": content})

    # Parse JSON — response_format guarantees valid JSON object
    try:
        parsed = json.loads(content)

        # Expected format: {"actions": [...]}
        if isinstance(parsed, dict):
            if "actions" in parsed and isinstance(parsed["actions"], list):
                return [_convert_tool_call(a) for a in parsed["actions"] if isinstance(a, dict)][:MAX_BATCH_SIZE]
            # Fallback: single action object like {"a":"click","n":0}
            if "a" in parsed:
                return [_convert_tool_call(parsed)]
            # Unknown keys — try to find action-like dicts in values
            for v in parsed.values():
                if isinstance(v, list):
                    actions = [_convert_tool_call(a) for a in v if isinstance(a, dict) and "a" in a][:MAX_BATCH_SIZE]
                    if actions:
                        return actions
            return [{"a": "error", "error": "No actions found in response"}]
        elif isinstance(parsed, list):
            return [_convert_tool_call(a) for a in parsed if isinstance(a, dict)][:MAX_BATCH_SIZE]
        else:
            return [{"a": "error", "error": "Unexpected format"}]

    except json.JSONDecodeError:
        log(f"  ERROR: Failed to parse LLM response: {content}")
        return [{"a": "error", "error": "Parse error"}]
