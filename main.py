"""
Fast browser automation agent for web challenges.
Target: 30 challenges in <5 minutes.
"""

import asyncio
import json
import os
import re
import time
from bs4 import BeautifulSoup
from groq import AsyncGroq
from playwright.async_api import async_playwright, Page

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
    for tag in soup.find_all(['script', 'style', 'noscript', 'iframe']):
        tag.decompose()

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


async def analyze_overview(client: AsyncGroq, content: dict) -> str:
    """Overview agent - analyzes full page to understand the task."""

    # Build content string (up to ~10k tokens)
    page_content = f"""
URL: {content['url']}
Title: {content['title']}
Headings: {', '.join(content['headings'])}
Forms: {', '.join(content['forms'])}

Page content:
{content['full_text'][:35000]}
"""

    try:
        response = await client.chat.completions.create(
            model="meta-llama/llama-4-maverick-17b-128e-instruct",
            messages=[
                {"role": "system", "content": OVERVIEW_PROMPT.format(content=page_content)},
                {"role": "user", "content": "Analyze this page."}
            ],
            max_tokens=400,
            temperature=0,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        log(f"Overview agent error: {e}")
        return "TASK: Complete the page task\nSTEPS: Interact with the page elements\nDATA: Check page content"


async def extract_elements(page: Page) -> list:
    """Extract interactive elements with indices."""

    js_code = """
    () => {
        const elements = [];
        const selector = 'button, input, textarea, select, a[href], [onclick], [role="button"]';

        document.querySelectorAll(selector).forEach((el) => {
            const tag = el.tagName.toLowerCase();
            const type = el.type || '';
            const text = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || '').trim().slice(0, 40);
            const visible = el.offsetParent !== null || el.offsetWidth > 0;

            if (!visible) return;

            let abbr = tag;
            if (tag === 'button') abbr = 'btn';
            else if (tag === 'input') abbr = 'inp';
            else if (tag === 'textarea') abbr = 'txt';
            else if (tag === 'select') abbr = 'sel';
            else if (tag === 'a') abbr = 'link';

            elements.push({
                tag: abbr,
                text: text,
                type: type,
                index: elements.length
            });
        });

        return elements;
    }
    """
    return await page.evaluate(js_code)


def format_context(overview: str, elements: list) -> str:
    """Format the analysis and elements for the action LLM."""

    parts = []

    # Overview from analysis
    parts.append("=== PAGE ANALYSIS ===")
    parts.append(overview)

    # Elements
    parts.append("\n=== INTERACTIVE ELEMENTS ===")
    inputs = [el for el in elements if el["tag"] in ("inp", "txt", "sel")]
    buttons = [el for el in elements if el["tag"] not in ("inp", "txt", "sel")]

    # Show all inputs and limited buttons
    shown = inputs + buttons[:15]

    el_strs = []
    for el in shown:
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

    log(f"Action LLM: {len(messages)} messages")

    try:
        response = await client.chat.completions.create(
            model="meta-llama/llama-4-maverick-17b-128e-instruct",
            messages=messages,
            max_tokens=50,
            temperature=0,
        )
    except Exception as e:
        log(f"LLM ERROR: {e}")
        return {"a": "error", "error": str(e)}

    content = response.choices[0].message.content.strip()
    log(f"LLM: {content}")
    messages.append({"role": "assistant", "content": content})

    # Parse JSON
    try:
        if "```" in content:
            match = re.search(r'```(?:json)?\s*(.*?)```', content, re.DOTALL)
            content = match.group(1) if match else content

        action = json.loads(content)
        return action
    except:
        match = re.search(r'\{[^}]+\}', content)
        if match:
            try:
                return json.loads(match.group())
            except:
                pass
        return {"a": "error", "error": f"Parse error: {content}"}


async def execute(page: Page, action: dict) -> str:
    """Execute action on page."""

    action_type = action.get("a", "error")
    index = action.get("n", 0)
    value = action.get("v", "")

    if action_type in ("done", "error"):
        return action_type

    try:
        if action_type == "click":
            selector = 'button, input, textarea, select, a[href], [onclick], [role="button"]'
            els = await page.query_selector_all(selector)
            visible = [el for el in els if await el.is_visible()]

            if index < len(visible):
                await visible[index].click(force=True, timeout=2000)
                return f"clicked [{index}]"
            return f"[{index}] not found"

        elif action_type == "type":
            selector = 'button, input, textarea, select, a[href], [onclick], [role="button"]'
            els = await page.query_selector_all(selector)
            visible = [el for el in els if await el.is_visible()]

            if index < len(visible):
                await visible[index].fill(str(value), force=True, timeout=2000)
                return f"typed '{value}'"
            return f"[{index}] not found"

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
        challenge_start = time.time()
        last_actions = []
        steps = 0
        stuck_count = 0
        cached_overview = None

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
                    last_actions.clear()
                    steps = 0
                    stuck_count = 0
                    cached_overview = None

                log(f"\n[Challenge {challenge}] {current_url}")
                prev_url = current_url

                if "complete" in current_url.lower() or "finish" in current_url.lower():
                    log("🎉 All challenges completed!")
                    break

                if challenge > 30:
                    log("✓ Completed 30 challenges!")
                    break

            # OBSERVE
            # Run overview analysis once per challenge, or re-run every 5 steps if stuck
            if cached_overview is None or (steps > 0 and steps % 5 == 0):
                content = await extract_structured_content(page)
                cached_overview = await analyze_overview(client, content)
                log(f"Overview: {cached_overview[:150]}...")

            elements = await extract_elements(page)
            context_str = format_context(cached_overview, elements)

            # THINK
            action = await llm_decide(client, messages, context_str)

            if action.get("a") == "error":
                log(f"Error: {action.get('error')}")
                stuck_count += 1
                if stuck_count >= 3:
                    break
                continue

            # ACT
            result = await execute(page, action)
            log(f"Action: {action} -> {result}")

            # Track stuck
            sig = f"{action.get('a')}_{action.get('n')}_{str(action.get('v', ''))[:10]}"
            last_actions.append(sig)
            if len(last_actions) > 5:
                last_actions.pop(0)
            steps += 1

            # Stuck detection
            if len(last_actions) >= 3 and len(set(last_actions[-3:])) == 1:
                stuck_count += 1
                log(f"STUCK: Repeated action ({stuck_count}/3)")
                last_actions.clear()
                cached_overview = None  # Re-analyze page

                if stuck_count >= 3:
                    log("Giving up on challenge")
                    break
                continue

            if steps > 20:
                log("STUCK: Too many steps")
                stuck_count += 1
                if stuck_count >= 2:
                    break
                steps = 0
                cached_overview = None
                continue

            await asyncio.sleep(0.1)

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
