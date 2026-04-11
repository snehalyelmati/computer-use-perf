"""Debug script: test tool calling with/without JSON schema instructions across providers.

Tests removing "Return a JSON object matching this schema:" from prompts
for Groq, Cerebras, and OpenRouter to ensure reliable tool calling.
"""

import asyncio
import json
import os
import sys

# ---------- shared payloads ----------

TOOL = {
    "type": "function",
    "function": {
        "name": "final_result",
        "description": "Structured output returned by the snapshot filter stage.",
        "parameters": {
            "properties": {
                "useful_text_lines": {
                    "description": "High-signal page text lines (one line per item, no numbering).",
                    "items": {"type": "string"},
                    "type": "array",
                },
                "priority_element_ids": {
                    "description": "Shortlist of stable element ids likely to matter for the goal.",
                    "items": {"type": "string"},
                    "type": "array",
                },
                "notes": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "description": "Optional short notes about what matters on the page right now.",
                },
            },
            "title": "SnapshotFilterOutput",
            "type": "object",
        },
    },
}

USER_MSG = """Overall goal: Complete all 30 challenges on this site as quickly as possible.

Instructions:
- Each page is one challenge. Find the 6-digit code for that page.
- Submit the code. Each successful submission completes one challenge.
- NEVER INVENT CODES.

Last worker goal: None.
Last step summary: None.

Diff since prior snapshot:
First snapshot (no prior snapshot to diff).

Page snapshot (full interactive element tree):
URL: https://serene-frangipane-7fd25b.netlify.app/
Title: Browser Navigation Challenge - The Ultimate Test

Interactive elements (showing up to 60):
<head>
  - el_17eecf490ec6: META reason=non_interactive_hint
<body>
  - el_486128db770b: generic | DIV (id="root") [click:function _o(){}]
  <div min-h-screen>
    - el_185cdfca9647: button | START | BUTTON offscreen [click:function _o(){}]

Page text lines:
Browser Navigation Challenge - The Ultimate Test
The Ultimate Test for Browser Automation
START"""

_RULES = """You are a snapshot pruner. Your job is to remove only obvious filler from the snapshot tree. Everything not in your list will be removed — the orchestrator will never see it.

Rules:
- Be CONSERVATIVE: only remove elements you are CERTAIN are filler.
- KEEP all elements that could plausibly be useful.
- Extract ONLY useful text lines (task instructions, codes, values, form labels, errors).
- Do not invent element IDs; priority_element_ids must be chosen only from the provided stable IDs."""

SYSTEM_WITH_SCHEMA = _RULES + """

Return a JSON object matching this schema:
- useful_text_lines: list[string]
- priority_element_ids: list[string]
- notes: string | null"""

SYSTEM_WITHOUT_SCHEMA = _RULES

NUM_TRIALS = 5


# ---------- provider-specific clients ----------

async def call_groq(system: str) -> dict:
    from groq import AsyncGroq
    client = AsyncGroq(api_key=os.environ["GROQ_API_KEY"])
    resp = await client.chat.completions.create(
        model="moonshotai/kimi-k2-instruct-0905",
        messages=[{"role": "system", "content": system}, {"role": "user", "content": USER_MSG}],
        tools=[TOOL], tool_choice="required",
        frequency_penalty=0.5, presence_penalty=0.3, max_tokens=2048, n=1,
        parallel_tool_calls=False, stream=False,
    )
    choice = resp.choices[0]
    if choice.message.tool_calls:
        return {"ok": True, "args": json.loads(choice.message.tool_calls[0].function.arguments)}
    return {"ok": False, "content": choice.message.content or ""}


async def call_cerebras(system: str) -> dict:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(
        api_key=os.environ["CEREBRAS_API_KEY"],
        base_url="https://api.cerebras.ai/v1",
    )
    # Cerebras doesn't support frequency_penalty, presence_penalty, parallel_tool_calls
    resp = await client.chat.completions.create(
        model="qwen-3-235b-a22b-instruct-2507",
        messages=[{"role": "system", "content": system}, {"role": "user", "content": USER_MSG}],
        tools=[TOOL], tool_choice="required",
        max_tokens=2048, n=1, stream=False,
    )
    choice = resp.choices[0]
    if choice.message.tool_calls:
        return {"ok": True, "args": json.loads(choice.message.tool_calls[0].function.arguments)}
    return {"ok": False, "content": choice.message.content or ""}


async def call_openrouter(system: str) -> dict:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(
        api_key=os.environ["OPENROUTER_API_KEY"],
        base_url="https://openrouter.ai/api/v1",
    )
    resp = await client.chat.completions.create(
        model="moonshotai/kimi-k2-0905:exacto",
        messages=[{"role": "system", "content": system}, {"role": "user", "content": USER_MSG}],
        tools=[TOOL], tool_choice="required",
        frequency_penalty=0.5, presence_penalty=0.3, max_tokens=2048, n=1,
        parallel_tool_calls=False, stream=False,
    )
    choice = resp.choices[0]
    if choice.message.tool_calls:
        return {"ok": True, "args": json.loads(choice.message.tool_calls[0].function.arguments)}
    return {"ok": False, "content": choice.message.content or ""}


# ---------- runner ----------

async def test_variant(label: str, call_fn, system: str) -> tuple[int, int]:
    print(f"\n{'=' * 60}")
    print(f"{label}")
    print(f"{'=' * 60}")
    ok = 0
    fail = 0
    for i in range(NUM_TRIALS):
        print(f"  Trial {i+1}/{NUM_TRIALS}...", end=" ")
        try:
            result = await call_fn(system)
            if result["ok"]:
                print(f"SUCCESS keys={list(result['args'].keys())}")
                ok += 1
            else:
                print(f"NO TOOL CALL content={result['content'][:120]}")
                fail += 1
        except Exception as e:
            body = getattr(e, "body", None)
            if isinstance(body, dict) and "error" in body:
                code = body["error"].get("code", "?")
                gen = body["error"].get("failed_generation", "")[:120]
                print(f"FAILED ({code}) {gen}")
            else:
                print(f"FAILED: {type(e).__name__}: {e}")
            fail += 1
    print(f"  => {ok}/{NUM_TRIALS} succeeded")
    return ok, fail


async def main() -> None:
    providers = sys.argv[1:] if len(sys.argv) > 1 else ["groq", "cerebras", "openrouter"]

    dispatch = {
        "groq": ("Groq (kimi-k2-instruct-0905)", call_groq, "GROQ_API_KEY"),
        "cerebras": ("Cerebras (qwen-3-235b)", call_cerebras, "CEREBRAS_API_KEY"),
        "openrouter": ("OpenRouter (kimi-k2-0905:exacto)", call_openrouter, "OPENROUTER_API_KEY"),
    }

    results = []
    for name in providers:
        label, fn, key_env = dispatch[name]
        if key_env not in os.environ:
            print(f"\nSkipping {label} — {key_env} not set")
            continue

        ok_with, _ = await test_variant(f"{label} — WITH schema instruction", fn, SYSTEM_WITH_SCHEMA)
        ok_without, _ = await test_variant(f"{label} — WITHOUT schema instruction", fn, SYSTEM_WITHOUT_SCHEMA)
        results.append((label, ok_with, ok_without))

    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")
    print(f"{'Provider':<45} {'With':>6} {'Without':>8}")
    print("-" * 60)
    for label, w, wo in results:
        print(f"{label:<45} {w}/{NUM_TRIALS}   {wo}/{NUM_TRIALS}")


if __name__ == "__main__":
    asyncio.run(main())
