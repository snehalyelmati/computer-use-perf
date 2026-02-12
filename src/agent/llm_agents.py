import json
import re
from groq import AsyncGroq
from .config import MODEL_NAME
from .logging_utils import log

async def analyze_overview(client: AsyncGroq, content: dict, elements: list, memory: list,
                           last_action: dict = None, last_result: str = None,
                           state_changed: bool = True, unchanged_count: int = 0) -> str:
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
    """

    # Build content string (limited for faster LLM calls)
    hidden = content.get('hidden_content', [])
    data_attrs = content.get('data_attrs', [])

    # Format top interactive elements for overview
    el_summary = []
    for el in elements[:30]:  # Top 30 elements
        tag = el.get('role') or el['tag']
        text = el['text'][:30] if el['text'] else el.get('type', '?')
        # Include href for links to help identify real navigation
        href = el.get('href', '')
        if href and href != '#':
            el_summary.append(f"[{el['index']}] {tag}: {text} -> {href[:40]}")
        else:
            el_summary.append(f"[{el['index']}] {tag}: {text}")

    page_content = f"""
URL: {content['url']}
Title: {content['title']}
Headings: {', '.join(content['headings'])}
Forms: {', '.join(content['forms'])}

Interactive elements:
{chr(10).join(el_summary)}

Hidden content (may contain codes): {', '.join(hidden) if hidden else 'none found'}
Data attributes: {', '.join(data_attrs) if data_attrs else 'none found'}

Page content:
{content['full_text'][:10000]}
"""

    # Add warning if state unchanged - make it prominent
    if not state_changed:
        page_content += f"\n\n*** WARNING: State unchanged for {unchanged_count} iterations! Your previous action had NO effect. You MUST try a DIFFERENT action (if you scrolled, try clicking a button instead). ***"

    # Combine previous action with current page state into single user message
    combined_content = ""
    if last_action and last_result:
        action_summary = f"{last_action.get('a', '?')}"
        if 'n' in last_action:
            action_summary += f"[{last_action['n']}]"
        if 'v' in last_action:
            action_summary += f" \"{last_action['v'][:20]}\""
        combined_content = f"Previous action: {action_summary} -> {last_result}\n\n"
    combined_content += f"Current page state:\n{page_content}\n\nWhat should we do next?"

    memory.append({
        "role": "user",
        "content": combined_content
    })

    # Limit memory to prevent context overflow
    if len(memory) > 15:
        memory[:] = [memory[0]] + memory[-12:]

    try:
        response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=memory,
            max_completion_tokens=1000,  # Increased for more detailed analysis
            reasoning_effort="none",    # Disables <think> tags at API level
            temperature=0,
        )
        result = response.choices[0].message.content.strip()

        # Add response to memory
        memory.append({"role": "assistant", "content": result})
        return result if result else "TASK: Complete the page task\nSTEPS: Interact with elements\nDATA: Check content"
    except Exception as e:
        log(f"Overview agent error: {e}")
        return "TASK: Complete the page task\nSTEPS: Interact with the page elements\nDATA: Check page content"

async def llm_decide(client: AsyncGroq, messages: list, context: str, last_action: dict = None, last_result: str = None) -> dict:
    """Get next action from LLM with challenge-level memory."""

    # Add previous action to context for sequencing awareness
    if last_action and last_result:
        action_summary = f"{last_action.get('a', '?')}"
        if 'n' in last_action:
            action_summary += f"[{last_action['n']}]"
        if 'v' in last_action:
            action_summary += f" \"{last_action['v'][:20]}\""
        messages.append({
            "role": "user",
            "content": f"Previous action: {action_summary} -> {last_result}"
        })

    messages.append({"role": "user", "content": context})

    # Less aggressive truncation - keep more history within challenge
    if len(messages) > 20:
        messages[:] = [messages[0]] + messages[-18:]

    try:
        response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            max_completion_tokens=200,
            reasoning_effort="none",
            temperature=0,
        )
    except Exception as e:
        log(f"  ERROR: LLM call failed - {e}")
        return {"a": "error", "error": str(e)}

    content = response.choices[0].message.content
    if not content:
        log("  Action LLM: (empty response)")
        return {"a": "error", "error": "Empty response"}

    content = content.strip()
    # Log raw action LLM output
    log(f"  Action LLM: {content[:200]}")
    messages.append({"role": "assistant", "content": content})

    # Parse JSON - handle multiple formats
    try:
        # Strip markdown code blocks
        if "```" in content:
            match = re.search(r'```(?:json)?\s*(.*?)```', content, re.DOTALL)
            content = match.group(1) if match else content

        action = json.loads(content)

        # Handle tool-calling format: {"name": "browser.click", "arguments": {...}}
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
    except json.JSONDecodeError:
        # Try to extract JSON from text
        match = re.search(r'\{[^}]+\}', content)
        if match:
            try:
                return json.loads(match.group())
            except:
                pass
        log(f"  ERROR: Failed to parse LLM response: {content[:50]}")
        return {"a": "error", "error": f"Parse error"}
