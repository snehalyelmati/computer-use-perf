"""
Fast browser automation agent for web challenges.
Target: 30 challenges in <5 minutes.
"""

import asyncio
import hashlib
import json
import os
import re
import time
from bs4 import BeautifulSoup
from groq import AsyncGroq
from playwright.async_api import async_playwright, Page


def compute_state_hash(url: str, elements: list) -> str:
    """Hash of current page state for change detection."""
    el_sig = "|".join(f"{e['tag']}:{e['text'][:10]}" for e in elements[:20])
    return hashlib.md5(f"{url}|{el_sig}".encode()).hexdigest()[:8]

# Logging setup
LOG_FILE = "agent.log"
STUCK_THRESHOLD = 5  # Number of unchanged states before considering stuck

def log(msg: str):
    """Log to both console and file."""
    timestamp = time.strftime("%H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

# System prompt for action decisions
SYSTEM_PROMPT = """You execute browser actions. Output ONLY valid JSON.

Actions:
{"a":"click","n":0} - click element at index 0
{"a":"type","n":1,"v":"text"} - type text in element at index 1
{"a":"scroll","v":"down"} - scroll down/up

IMPORTANT: Follow the PAGE ANALYSIS instructions exactly.
- The NEXT ACTION tells you what to do
- The DATA section has exact values to use
- Match element names from INTERACTIVE ELEMENTS list

Output ONLY one JSON object."""

# Overview agent prompt - general purpose, no assumptions
OVERVIEW_PROMPT = """Analyze this page and determine what to do next.

Output:
1. GOAL: What is the main task on this page?
2. DATA: Extract any codes, values, or data from the page content that might be needed.
3. NEXT: What is the ONE action to take now? Reference elements by their index number [N].

If state is UNCHANGED, your previous action had no effect - try something different."""


async def extract_structured_content(page: Page) -> dict:
    """Extract structured content from page using BeautifulSoup."""

    html = await page.content()
    soup = BeautifulSoup(html, 'lxml')

    # Remove noise elements (but NOT hidden elements - they may contain answers!)
    noise_tags = ['script', 'style', 'noscript', 'iframe', 'nav', 'footer', 'header', 'aside']
    for tag in soup.find_all(noise_tags):
        tag.decompose()

    # Remove role-based noise
    for el in soup.find_all(attrs={'role': ['banner', 'navigation', 'contentinfo']}):
        el.decompose()

    # Extract hidden content that might contain codes/answers
    hidden_content = []
    for el in soup.find_all(attrs={'hidden': True}):
        text = el.get_text(strip=True)
        if text:
            hidden_content.append(f"[hidden] {text}")
    for el in soup.find_all(class_=re.compile(r'hidden|invisible|sr-only', re.I)):
        text = el.get_text(strip=True)
        if text and text not in str(hidden_content):
            hidden_content.append(f"[hidden] {text}")

    # Extract attributes that might contain important data
    data_attrs = []
    for el in soup.find_all(True):  # All elements
        if el.attrs:
            for key, val in el.attrs.items():
                if val and isinstance(val, str) and len(val) <= 50:
                    # Capture data-*, aria-*, title, alt attributes
                    if key.startswith('data-') or key in ('aria-label', 'title', 'alt'):
                        data_attrs.append(f"{key}={val}")

    # Extract structured data
    title = soup.find('h1')
    title_text = title.get_text(strip=True) if title else ""

    headings = [h.get_text(strip=True) for h in soup.find_all(['h2', 'h3', 'h4'])]

    paragraphs = []
    for p in soup.find_all('p'):
        text = p.get_text(strip=True)
        if text and len(text) > 10:
            paragraphs.append(text)

    # Extract form elements with context
    forms = []
    for inp in soup.find_all(['input', 'textarea']):
        input_type = inp.get('type', 'text')
        placeholder = inp.get('placeholder', '')
        label = ''
        if inp.get('id'):
            label_el = soup.find('label', {'for': inp.get('id')})
            if label_el:
                label = label_el.get_text(strip=True)
        forms.append(f"{input_type}: {label or placeholder or 'input'}")

    # Get full text for analysis
    full_text = soup.get_text(separator='\n', strip=True)

    # Track if limits were hit
    limits_hit = []
    if len(hidden_content) > 10:
        limits_hit.append(f"hidden_content: {len(hidden_content)} -> 10")
    if len(data_attrs) > 20:
        limits_hit.append(f"data_attrs: {len(data_attrs)} -> 20")

    return {
        "title": title_text,
        "headings": headings[:5],
        "paragraphs": paragraphs[:10],
        "forms": forms,
        "full_text": full_text,
        "hidden_content": hidden_content[:10],
        "data_attrs": data_attrs[:20],
        "limits_hit": limits_hit,
        "url": page.url
    }


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
            model="qwen/qwen3-32b",
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


async def extract_elements(page: Page) -> tuple[list, list]:
    """Extract interactive elements with indices and return element handles.

    Returns:
        tuple: (metadata_list, element_handles) where indices match between both
    """
    selector = 'button, input, textarea, select, a[href], [onclick], [role="button"], [role="radio"], [role="checkbox"]'
    handles = await page.query_selector_all(selector)

    elements = []
    visible_handles = []

    for handle in handles:
        try:
            if not await handle.is_visible():
                continue

            # Extract metadata from element including role and state
            metadata = await handle.evaluate('''el => {
                const tag = el.tagName.toLowerCase();
                const type = el.type || '';
                const text = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || '').trim().slice(0, 40);
                const role = el.getAttribute('role') || '';
                const state = el.getAttribute('data-state') || '';
                const disabled = el.disabled || el.getAttribute('aria-disabled') === 'true';
                const href = el.getAttribute('href') || '';

                let abbr = tag;
                if (tag === 'button') abbr = 'btn';
                else if (tag === 'input') abbr = 'inp';
                else if (tag === 'textarea') abbr = 'txt';
                else if (tag === 'select') abbr = 'sel';
                else if (tag === 'a') abbr = 'link';

                return { tag: abbr, text: text, type: type, role: role, state: state, disabled: disabled, href: href };
            }''')

            # Assign sequential index that matches position in visible_handles
            metadata['index'] = len(elements)
            elements.append(metadata)
            visible_handles.append(handle)

        except Exception:
            # Element may have been removed from DOM
            continue

    return elements, visible_handles


def format_context(overview: str, elements: list) -> str:
    """Format the analysis and elements for the action LLM.

    Note: Elements now have sequential indices (0, 1, 2...) that match
    the element handles list, so we show them all without reordering.
    """

    parts = []

    # Overview from analysis
    parts.append("=== PAGE ANALYSIS ===")
    parts.append(overview)

    # Elements - show all with enriched info (role, state)
    parts.append("\n=== INTERACTIVE ELEMENTS ===")

    el_strs = []
    for el in elements:
        # Use role if available, otherwise tag
        tag = el.get('role') or el['tag']

        # Build state string
        state = ""
        if el.get('state'):
            state = f" [{el['state']}]"
        if el.get('disabled'):
            state += " [disabled]"

        text = el["text"][:25] if el["text"] else el["type"] or "?"
        # Include href for links
        href = el.get('href', '')
        if href and href != '#':
            el_strs.append(f"[{el['index']}] {tag} \"{text}\" -> {href[:30]}{state}")
        else:
            el_strs.append(f"[{el['index']}] {tag} \"{text}\"{state}")

    parts.append("\n".join(el_strs))

    return "\n".join(parts)


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
            model="qwen/qwen3-32b",
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


async def execute(page: Page, action: dict, handles: list) -> str:
    """Execute action on page using stored element handles.

    Args:
        page: Playwright page
        action: Action dict from LLM (e.g., {"a": "click", "n": 0})
        handles: List of ElementHandles from extract_elements()
    """

    action_type = action.get("a", "error")
    index = action.get("n", 0)
    value = action.get("v", "")

    if action_type in ("done", "error"):
        return action_type

    try:
        if action_type == "click":
            if index < len(handles):
                await handles[index].click(force=True, timeout=2000)
                await asyncio.sleep(0.3)  # Allow page to react
                return f"clicked [{index}]"
            return f"[{index}] not found (only {len(handles)} elements)"

        elif action_type == "type":
            if index < len(handles):
                await handles[index].fill(str(value), force=True, timeout=2000)
                await asyncio.sleep(0.2)  # Allow form to register
                return f"typed '{value}'"
            return f"[{index}] not found (only {len(handles)} elements)"

        elif action_type == "scroll":
            direction = value or "down"
            amount = 500 if direction == "down" else -500
            await page.evaluate(f"window.scrollBy(0, {amount})")
            return f"scrolled {direction}"

        elif action_type == "wait":
            await asyncio.sleep(min(float(value) if value else 1, 3))
            return "waited"

    except Exception as e:
        return f"error: {e}"

    return f"unknown: {action_type}"


async def run_agent(base_url: str = "https://serene-frangipane-7fd25b.netlify.app"):
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
            limits_hit = content.get('limits_hit', [])

            if hidden:
                log(f"  Hidden: {hidden}")
            if data_attrs:
                log(f"  Data attrs: {data_attrs[:5]}{'...' if len(data_attrs) > 5 else ''}")
            if limits_hit:
                log(f"  ⚠ Limits hit: {limits_hit}")

            overview = await analyze_overview(
                client, content, elements, overview_messages,
                last_action, last_result, state_changed, unchanged_count
            )

            # Log full overview (multi-line)
            log(f"  Overview LLM:")
            for line in overview.split('\n')[:10]:  # First 10 lines
                if line.strip():
                    log(f"    {line.strip()[:100]}")

            context_str = format_context(overview, elements)

            # THINK - pass action memory and previous action/result for sequencing
            action = await llm_decide(client, action_messages, context_str, last_action, last_result)

            if action.get("a") == "error":
                log(f"  ⚠ LLM error, retrying...")
                continue

            # Show what element we're targeting
            action_idx = action.get("n", 0)
            if action_idx < len(elements):
                el = elements[action_idx]
                tag = el.get('role') or el['tag']
                state = f" [{el['state']}]" if el.get('state') else ""
                log(f"  Target: [{action_idx}] {tag} \"{el['text'][:40]}\"{state}")

            # ACT
            result = await execute(page, action, handles)

            # Log execution result
            action_type = action.get("a", "?")
            action_val = action.get("v", "")
            step_time = time.time() - step_start
            if action_val:
                log(f"  Result: {action_type}[{action_idx}] \"{action_val[:30]}\" -> {result} ({step_time:.1f}s)")
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
    base_url = sys.argv[1] if len(sys.argv) > 1 else "https://serene-frangipane-7fd25b.netlify.app"

    with open(LOG_FILE, "w") as f:
        f.write(f"=== Started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")

    print("=" * 50)
    print("Fast Browser Agent")
    print(f"Target: {base_url}")
    print("=" * 50)

    asyncio.run(run_agent(base_url))


if __name__ == "__main__":
    main()
