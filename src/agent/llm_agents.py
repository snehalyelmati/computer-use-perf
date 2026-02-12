import json
import re
from groq import AsyncGroq
import asyncio
from .config import MODEL_NAME, ACTION_MODEL_NAME, FILTER_MODEL_NAME
from .element_utils import format_element_summary
from .logging_utils import log, log_verbose

def _extract_summary(text: str) -> str:
    """Parse GOAL, DATA, and PROGRESS lines from overview LLM response.

    Returns a compact summary string to persist in the system message.
    """
    lines = []
    for label in ("GOAL", "DATA", "PROGRESS"):
        match = re.search(rf'^{label}:\s*(.+)', text, re.MULTILINE | re.IGNORECASE)
        if match:
            lines.append(f"{label}: {match.group(1).strip()}")
    return "\n".join(lines) if lines else ""

async def filter_page_content(client: AsyncGroq, all_text: list[str]) -> list[str]:
    """Filter filler text (lorem ipsum, section headers, repeated patterns) using a small LLM."""
    if len(all_text) <= 30:
        return all_text

    prompt = "Extract ONLY useful text lines (task instructions, codes, values, form labels, errors). Remove filler (section headers, lorem ipsum, repeated patterns). Return one line per output line, no numbering."

    async def _filter_chunk(chunk: list[str]) -> list[str]:
        numbered = "\n".join(f"{i+1}. {line}" for i, line in enumerate(chunk))
        response = await client.chat.completions.create(
            model=FILTER_MODEL_NAME,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": numbered},
            ],
            max_completion_tokens=500,
            temperature=0,
        )
        result = response.choices[0].message.content.strip()
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

async def extract_learning(client: AsyncGroq, challenge_summary: str) -> str:
    """Extract a general learning from a completed interaction, async."""
    if not challenge_summary:
        return ""
    try:
        response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": "Given this interaction summary, what's a general lesson about navigating web pages effectively? Be concise."},
                {"role": "user", "content": challenge_summary},
            ],
            max_completion_tokens=200,
            reasoning_effort="none",
            temperature=0,
        )
        result = response.choices[0].message.content.strip()
        log(f"  Learning extracted: {result}")
        return result
    except Exception as e:
        log(f"  Learning extraction error: {e}")
        return ""


async def analyze_overview(client: AsyncGroq, content: dict, elements: list, memory: list,
                           last_action: dict = None, last_result: str = None,
                           state_changed: bool = True, unchanged_count: int = 0,
                           challenge_summary: str = "",
                           agent_learnings: list[str] = None) -> tuple[str, str]:
    """Overview agent - analyzes full page with memory of previous actions.

    Args:
        client: Groq client
        content: Structured page content from extract_structured_content()
        elements: List of interactive elements with indices
        memory: List of previous messages for context (modified in place)
        last_action: Previous action dict
        last_result: Result string from previous action
        state_changed: Whether page state changed since last step
        unchanged_count: Number of consecutive unchanged states
        challenge_summary: Persistent summary from previous steps (survives truncation)

    Returns:
        tuple: (overview_text, updated_challenge_summary)
    """
    from .prompts import OVERVIEW_PROMPT

    # Update system message with persistent challenge summary and learnings
    system_content = OVERVIEW_PROMPT
    if challenge_summary:
        system_content += f"\n\nCurrent context:\n{challenge_summary}"
    if agent_learnings:
        system_content += "\n\nLearnings from previous pages:\n" + "\n".join(f"- {l}" for l in agent_learnings)
    memory[0] = {"role": "system", "content": system_content}

    # Build structured content (replaces noisy full_text dump)
    hidden = content.get('hidden_content', [])
    data_attrs = content.get('data_attrs', [])

    # Use deduped all_text (covers all HTML tags)
    all_text = content.get('all_text', [])
    filtered_text = await filter_page_content(client, all_text)
    if len(filtered_text) != len(all_text):
        log(f"  Filtered text: {len(all_text)} -> {len(filtered_text)} items")
    forms = content.get('forms', [])
    structured_text = "\n".join(filtered_text)
    if forms:
        structured_text += f"\nForms: {', '.join(forms)}"

    el_summary = format_element_summary(elements)

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

    # Combine previous action with current page state into single user message
    combined_content = ""
    if last_action and last_result:
        action_summary = f"{last_action.get('a', '?')}"
        if 'n' in last_action:
            action_summary += f"[{last_action['n']}]"
        if 'v' in last_action:
            action_summary += f" \"{last_action['v']}\""
        combined_content = f"Previous action: {action_summary} -> {last_result}\n\n"
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
        response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=memory,
            max_completion_tokens=1500,
            reasoning_effort="none",
            temperature=0,
        )
        result = response.choices[0].message.content.strip()

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

async def llm_decide(client: AsyncGroq, messages: list, context: str, last_action: dict = None, last_result: str = None) -> list[dict]:
    """Get next action(s) from LLM with challenge-level memory.

    Returns a list of action dicts (always a list, even for single actions).
    """

    # Add previous action to context for sequencing awareness
    if last_action and last_result:
        action_summary = f"{last_action.get('a', '?')}"
        if 'n' in last_action:
            action_summary += f"[{last_action['n']}]"
        if 'v' in last_action:
            action_summary += f" \"{last_action['v']}\""
        messages.append({
            "role": "user",
            "content": f"Previous action: {action_summary} -> {last_result}"
        })

    messages.append({"role": "user", "content": context})

    # Log full LLM input to verbose log
    log_verbose(f"=== Action LLM Input (Step) ===\n{context}\n=== End Action LLM Input ===")

    # Less aggressive truncation - keep more history within challenge
    if len(messages) > 20:
        messages[:] = [messages[0]] + messages[-18:]

    try:
        response = await client.chat.completions.create(
            model=ACTION_MODEL_NAME,
            messages=messages,
            max_completion_tokens=350,
            temperature=0,
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

    content = response.choices[0].message.content
    if not content:
        log("  Action LLM: (empty response)")
        return [{"a": "error", "error": "Empty response"}]

    content = content.strip()
    # Log raw action LLM output
    log(f"  Action LLM: {content}")
    messages.append({"role": "assistant", "content": content})

    # Parse JSON - handle single action or array of actions
    try:
        # Strip markdown code blocks
        if "```" in content:
            match = re.search(r'```(?:json)?\s*(.*?)```', content, re.DOTALL)
            content = match.group(1) if match else content

        parsed = json.loads(content)

        # Normalize to list
        if isinstance(parsed, dict):
            return [_convert_tool_call(parsed)]
        elif isinstance(parsed, list):
            return [_convert_tool_call(a) for a in parsed if isinstance(a, dict)][:4]
        else:
            return [{"a": "error", "error": "Unexpected format"}]

    except json.JSONDecodeError:
        # Try array first
        match = re.search(r'\[.*\]', content, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
                if isinstance(parsed, list):
                    return [_convert_tool_call(a) for a in parsed if isinstance(a, dict)][:4]
            except:
                pass
        # Fall back to single object
        match = re.search(r'\{[^}]+\}', content)
        if match:
            try:
                return [json.loads(match.group())]
            except:
                pass
        log(f"  ERROR: Failed to parse LLM response: {content}")
        return [{"a": "error", "error": "Parse error"}]
