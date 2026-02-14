"""Debug script to verify all 4 JSON call sites produce valid Pydantic responses.

Tests each LLM call with sample prompts against the real API.
Usage: uv run debug_llm.py [--provider groq|cerebras]
"""

import asyncio
import os
import sys
import time
import argparse
from typing import cast

import src.agent.config as config
from src.agent.providers import PROVIDER_MODELS
from src.agent.llm_client import complete
from src.agent.schemas import (
    OracleResponse,
    OverviewResponse,
    ActionResponse,
    LearningResponse,
)
from src.agent.prompts import OVERVIEW_PROMPT, ORACLE_PROMPT, SYSTEM_PROMPT


# -- Sample data --------------------------------------------------------------

SAMPLE_ELEMENTS = """[0] inp "Enter 6-character code" (code-input) value=""
[1] btn "Submit Code"
[2] btn "Reset"
[3] link "Help" -> /help
[4] btn "Next Step" [disabled]"""

SAMPLE_PAGE_TEXT = """Enter the secret code to proceed.
The code is hidden somewhere on this page.
Hint: look at the data attributes."""

SAMPLE_OVERVIEW_INPUT = f"""Current page state:
URL: https://example.com/step1
Title: Challenge 1 - Enter Code

Interactive elements:
{SAMPLE_ELEMENTS}

Hidden content: secret-code=XK9F2P
Data attributes: data-answer=XK9F2P

Page content:
{SAMPLE_PAGE_TEXT}

What should we do next?"""

SAMPLE_ACTION_CONTEXT = f"""=== PAGE ANALYSIS ===
GOAL: Enter the 6-character code to proceed
OBJECTIVE: Enter the 6-character code to proceed
DATA: code=XK9F2P
NEXT: Type 'XK9F2P' into element [0], then click Submit [1]

=== INTERACTIVE ELEMENTS ===
{SAMPLE_ELEMENTS}"""


# -- Test functions -----------------------------------------------------------


async def test_oracle(client):
    """Test evaluate_step's Oracle call."""
    prompt = ORACLE_PROMPT.format(
        challenge_step_count=1,
        page_feedback="none",
        recent_verdicts="none (first evaluation)",
        goal="Enter the 6-character code",
        objective="Enter the 6-character code",
        data="code=XK9F2P",
        progress="Starting",
        action_results="  type[0] \"XK9F2P\" -> typed 'XK9F2P' into [0] [OK]\n  click[1] -> clicked [1] [OK]",
        url="https://example.com/step1",
        title="Challenge 1",
        elements=SAMPLE_ELEMENTS,
        hidden_content="secret-code=XK9F2P",
        data_attrs="data-answer=XK9F2P",
        page_text=SAMPLE_PAGE_TEXT,
        progress_indicators="none found",
        state_changes="none detected",
        new_text="none",
    )
    start = time.time()
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
    )
    elapsed = time.time() - start
    print(f"  Status: {response.status}")
    if response.reason:
        print(f"  Reason: {response.reason}")
    print(
        f"  Tokens: {usage.prompt_tokens}p + {usage.completion_tokens}c | {elapsed:.1f}s"
    )
    return response


async def test_overview(client):
    """Test analyze_overview's Overview call."""
    start = time.time()
    response, usage = await complete(
        client,
        model=cast(str, config.MODEL_NAME),
        messages=[
            {"role": "system", "content": OVERVIEW_PROMPT},
            {"role": "user", "content": SAMPLE_OVERVIEW_INPUT},
        ],
        max_completion_tokens=1400,
        reasoning_effort=config.REASONING_EFFORT,
        response_model=OverviewResponse,
    )
    elapsed = time.time() - start
    print(f"  Objective: {response.objective}")
    print(f"  Data: {response.data}")
    print(f"  Next: {response.next}")
    print(
        f"  Tokens: {usage.prompt_tokens}p + {usage.completion_tokens}c | {elapsed:.1f}s"
    )
    return response


async def test_action(client):
    """Test llm_decide's Action call."""
    start = time.time()
    response, usage = await complete(
        client,
        model=cast(str, config.ACTION_MODEL_NAME),
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": SAMPLE_ACTION_CONTEXT},
        ],
        max_completion_tokens=700,
        response_model=ActionResponse,
    )
    elapsed = time.time() - start
    actions = [item.model_dump(exclude_none=True) for item in response.actions]
    print(f"  Actions: {actions}")
    print(
        f"  Tokens: {usage.prompt_tokens}p + {usage.completion_tokens}c | {elapsed:.1f}s"
    )
    return response


async def test_learning(client):
    """Test extract_learning's Learning call."""
    start = time.time()
    response, usage = await complete(
        client,
        model=cast(str, config.MODEL_NAME),
        messages=[
            {
                "role": "system",
                "content": 'Given this interaction summary, extract a general strategy lesson about navigating web pages. NEVER include specific codes, values, URLs, or data from this interaction - only reusable strategies. Output JSON: {"learning": "one sentence strategy lesson"}',
            },
            {
                "role": "user",
                "content": "GOAL: Enter a hidden code\nDATA: code=XK9F2P found in data attributes\nPROGRESS: Completed in 1 step by checking hidden content",
            },
        ],
        max_completion_tokens=400,
        reasoning_effort=config.REASONING_EFFORT,
        response_model=LearningResponse,
    )
    elapsed = time.time() - start
    print(f"  Learning: {response.learning}")
    print(
        f"  Tokens: {usage.prompt_tokens}p + {usage.completion_tokens}c | {elapsed:.1f}s"
    )
    return response


# -- Main --------------------------------------------------------------------


async def main():
    parser = argparse.ArgumentParser(description="Debug LLM JSON responses")
    parser.add_argument("--provider", default="groq", choices=["groq", "cerebras"])
    args = parser.parse_args()

    config.PROVIDER = args.provider
    defaults = PROVIDER_MODELS[config.PROVIDER]
    config.MODEL_NAME = defaults["model"]
    config.ORACLE_MODEL = defaults["oracle"]
    config.ACTION_MODEL_NAME = defaults["action"]
    config.FILTER_MODEL_NAME = defaults["filter"]
    config.REASONING_EFFORT = None

    if config.PROVIDER == "cerebras":
        from cerebras.cloud.sdk import AsyncCerebras

        client = AsyncCerebras()
    else:
        from groq import AsyncGroq

        client = AsyncGroq(api_key=os.environ.get("GROQ_API_KEY"))

    print(f"Provider: {config.PROVIDER}")
    print(
        f"Models: overview={config.MODEL_NAME}, oracle={config.ORACLE_MODEL}, action={config.ACTION_MODEL_NAME}"
    )
    print()

    tests = [
        ("Oracle (OracleResponse)", test_oracle),
        ("Overview (OverviewResponse)", test_overview),
        ("Action (ActionResponse)", test_action),
        ("Learning (LearningResponse)", test_learning),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        print(f"{'=' * 50}")
        print(f"Testing: {name}")
        print(f"{'=' * 50}")
        try:
            await fn(client)
            print("  PASS")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {e}")
            failed += 1
        print()

    print(f"{'=' * 50}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'=' * 50}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
