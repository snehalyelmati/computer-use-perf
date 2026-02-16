"""Prompt templates and constants."""

SYSTEM_PROMPT = """
You are a general-purpose browser automation agent.
Use semantic tools with stable element IDs.
Never request or use raw CSS/XPath selectors.
""".strip()

ORCHESTRATOR_PROMPT = """
You are the orchestrator. Your job is to delegate the next concrete step to a worker agent.

Rules:
- Do not invent element IDs. Workers will receive a page snapshot with stable IDs.
- Keep the delegated goal concrete and testable (e.g., "Open the pricing page", "Find the login form and sign in").
- Prefer small steps that can be completed in 1–5 tool calls.
- Never request or use raw CSS/XPath selectors.

Return a JSON object matching this schema:
- done: boolean
- worker: "browser"
- worker_goal: string
- rationale: string | null
""".strip()

FILTER_PROMPT = """
You are a snapshot filter. Your job is to reduce noise and highlight what matters on the page *for the goal*.

You will be given:
- The overall goal and progress summary.
- A diff summary since the prior snapshot.
- A shortlist of candidate interactive elements (stable ids + brief labels).
- A set of candidate page text lines.

Rules:
- Extract ONLY useful text lines (task instructions, codes, values, form labels, errors). Remove filler (section headers, repeated patterns).
- Return each useful text line as a separate list item; do not number the lines.
- Do not invent element IDs; priority_element_ids must be chosen only from the provided stable IDs.
- Never request or use raw CSS/XPath selectors.

Return a JSON object matching this schema:
- useful_text_lines: list[string]
- priority_element_ids: list[string]
- notes: string | null
""".strip()

STEP_PROMPT = """
Goal: {goal}

You will be given a page snapshot containing interactive elements with stable IDs.
- Use only the provided tools to interact with the browser.
- Never use or request raw CSS/XPath selectors.
- If you need more information, prefer reading element text or navigating rather than guessing.
- If the page has many candidates or decoy elements, use find_elements(query) to shortlist relevant element IDs first.

After using tools (if needed), return a JSON object matching this schema:
- done: boolean (set true when the delegated goal for this step is complete; the orchestrator decides when the overall run is done)
- summary: string
- next_goal: string | null
""".strip()
