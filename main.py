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

def log(msg: str):
    """Log to both console and file."""
    timestamp = time.strftime("%H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

# System prompt for action decisions
SYSTEM_PROMPT = """You are a browser automation agent. Output ONLY valid JSON.

Actions:
{"a":"click","n":0} - click element at index 0
{"a":"type","n":1,"v":"text"} - type text in element at index 1
{"a":"scroll","v":"down"} - scroll down

CRITICAL: Output ONLY the JSON object. No explanation. No text."""

# Overview agent prompt - general purpose, no assumptions
OVERVIEW_PROMPT = """Analyze this web page and determine what actions are needed.

{content}

Provide a brief analysis:
1. TASK: What does this page want the user to do?
2. STEPS: What specific actions are needed?
3. DATA: What information on the page is relevant to completing the task?

Be specific and actionable."""


async def extract_structured_content(page: Page) -> dict:
    """Extract structured content from page using BeautifulSoup."""

    html = await page.content()
    soup = BeautifulSoup(html, 'lxml')

    # Remove noise elements
    noise_tags = ['script', 'style', 'noscript', 'iframe', 'nav', 'footer', 'header', 'aside']
    for tag in soup.find_all(noise_tags):
        tag.decompose()

    # Remove hidden elements
    for el in soup.find_all(attrs={'hidden': True}):
        el.decompose()
    for el in soup.find_all(class_='hidden'):
        el.decompose()
    for el in soup.find_all(attrs={'style': re.compile(r'display:\s*none', re.I)}):
        el.decompose()

    # Remove role-based noise
    for el in soup.find_all(attrs={'role': ['banner', 'navigation', 'contentinfo']}):
        el.decompose()

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

    return {
        "title": title_text,
        "headings": headings[:5],
        "paragraphs": paragraphs[:10],
        "forms": forms,
        "full_text": full_text,
        "url": page.url
    }


async def analyze_overview(client: AsyncGroq, content: dict, memory: list) -> str:
    """Overview agent - analyzes full page with memory of previous actions.

    Args:
        client: Groq client
        content: Structured page content from extract_structured_content()
        memory: List of previous messages for context (modified in place)
    """

    # Build content string (limited for faster LLM calls)
    page_content = f"""
URL: {content['url']}
Title: {content['title']}
Headings: {', '.join(content['headings'])}
Forms: {', '.join(content['forms'])}

Page content:
{content['full_text'][:15000]}
"""

    # Add current page state to memory
    memory.append({
        "role": "user",
        "content": f"Current page state:\n{page_content}\n\nWhat should we do next?"
    })

    # Limit memory to prevent context overflow
    if len(memory) > 15:
        memory[:] = [memory[0]] + memory[-12:]

    try:
        response = await client.chat.completions.create(
            model="qwen/qwen3-32b",
            messages=memory,
            max_completion_tokens=500,  # Use max_completion_tokens (max_tokens deprecated)
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
    selector = 'button, input, textarea, select, a[href], [onclick], [role="button"]'
    handles = await page.query_selector_all(selector)

    elements = []
    visible_handles = []

    for handle in handles:
        try:
            if not await handle.is_visible():
                continue

            # Extract metadata from element
            metadata = await handle.evaluate('''el => {
                const tag = el.tagName.toLowerCase();
                const type = el.type || '';
                const text = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || '').trim().slice(0, 40);

                let abbr = tag;
                if (tag === 'button') abbr = 'btn';
                else if (tag === 'input') abbr = 'inp';
                else if (tag === 'textarea') abbr = 'txt';
                else if (tag === 'select') abbr = 'sel';
                else if (tag === 'a') abbr = 'link';

                return { tag: abbr, text: text, type: type };
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

    # Elements - show all with their sequential indices
    parts.append("\n=== INTERACTIVE ELEMENTS ===")

    el_strs = []
    for el in elements:
        text = el["text"].replace(" ", "_") if el["text"] else el["type"] or "?"
        el_strs.append(f"[{el['index']}]{el['tag']}:{text[:20]}")

    parts.append(" ".join(el_strs))

    return "\n".join(parts)


async def llm_decide(client: AsyncGroq, messages: list, context: str) -> dict:
    """Get next action from LLM."""

    messages.append({"role": "user", "content": context})

    # Limit context
    if len(messages) > 12:
        messages[:] = [messages[0]] + messages[-10:]

    try:
        response = await client.chat.completions.create(
            model="openai/gpt-oss-120b",  # Larger model for better reasoning
            messages=messages,
            max_completion_tokens=200,
            reasoning_effort="low",
        )
    except Exception as e:
        log(f"  ERROR: LLM call failed - {e}")
        return {"a": "error", "error": str(e)}

    content = response.choices[0].message.content
    if not content:
        log("  ERROR: LLM returned empty response")
        return {"a": "error", "error": "Empty response"}

    content = content.strip()
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
                return f"clicked [{index}]"
            return f"[{index}] not found (only {len(handles)} elements)"

        elif action_type == "type":
            if index < len(handles):
                await handles[index].fill(str(value), force=True, timeout=2000)
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
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        overview_messages = [{"role": "system", "content": OVERVIEW_PROMPT.format(content="")}]
        challenge_start = time.time()
        last_action = None
        last_result = None
        state_hashes = []  # Track last 3 state hashes

        for step in range(500):
            current_url = page.url

            # New challenge detected
            if current_url != prev_url:
                if prev_url:
                    elapsed = time.time() - challenge_start
                    log(f"✓ Challenge {challenge} complete ({elapsed:.1f}s)")
                    challenge += 1
                    challenge_start = time.time()
                    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
                    overview_messages = [{"role": "system", "content": OVERVIEW_PROMPT.format(content="")}]
                    state_hashes.clear()

                log(f"\n[Challenge {challenge}] {current_url}")
                prev_url = current_url

            # OBSERVE - fresh every step
            elements, handles = await extract_elements(page)

            step_start = time.time()

            # State-based stuck detection
            state_hash = compute_state_hash(current_url, elements)
            prev_hash = state_hashes[-1] if state_hashes else None
            state_hashes.append(state_hash)
            if len(state_hashes) > 3:
                state_hashes.pop(0)

            # Check if state changed
            state_changed = prev_hash is None or prev_hash != state_hash
            unchanged_count = len([h for h in state_hashes if h == state_hash])

            # Check if stuck (same state for 3 iterations)
            if len(state_hashes) >= 3 and len(set(state_hashes)) == 1:
                log(f"STUCK: State unchanged 3x | hash={state_hash} | {len(elements)} elements")
                break

            # Add previous action to overview memory
            if last_action and last_result:
                overview_messages.append({
                    "role": "user",
                    "content": f"Previous action: {last_action} -> {last_result}"
                })

            # Get fresh overview with memory
            content = await extract_structured_content(page)
            overview = await analyze_overview(client, content, overview_messages)

            # Log step header with timing context
            inp_count = sum(1 for e in elements if e['tag'] == 'inp')
            btn_count = sum(1 for e in elements if e['tag'] == 'btn')
            state_indicator = "(changed)" if state_changed else f"(UNCHANGED {unchanged_count}/3)"
            log(f"[Step {step+1}] {inp_count} inp, {btn_count} btn | {state_hash} {state_indicator}")
            log(f"  Task: {overview[:80]}")

            context_str = format_context(overview, elements)

            # THINK
            action = await llm_decide(client, messages, context_str)

            if action.get("a") == "error":
                log(f"  ⚠ LLM error, retrying...")
                continue

            # ACT
            result = await execute(page, action, handles)

            # Format action log (compact)
            action_type = action.get("a", "?")
            action_idx = action.get("n", "")
            action_val = action.get("v", "")
            step_time = time.time() - step_start
            if action_val:
                log(f"  > {action_type}[{action_idx}] \"{action_val[:20]}\" -> {result} ({step_time:.1f}s)")
            else:
                log(f"  > {action_type}[{action_idx}] -> {result} ({step_time:.1f}s)")

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
