from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Union

from groq import AsyncGroq
from cerebras.cloud.sdk import AsyncCerebras
import groq
import cerebras.cloud.sdk as cerebras_sdk
from pydantic import BaseModel, ValidationError

from .logging_utils import log
from .providers import JSON_MODE_MODELS, REASONING_MODELS

LLMClient = Union[AsyncGroq, AsyncCerebras]

_TRANSIENT_ERRORS = (
    groq.RateLimitError, groq.APITimeoutError, groq.APIConnectionError, groq.InternalServerError,
    cerebras_sdk.RateLimitError, cerebras_sdk.APITimeoutError, cerebras_sdk.APIConnectionError, cerebras_sdk.InternalServerError,
)
_PERMANENT_ERRORS = (
    groq.AuthenticationError, groq.NotFoundError, groq.BadRequestError,
    cerebras_sdk.AuthenticationError, cerebras_sdk.NotFoundError, cerebras_sdk.BadRequestError,
)
_RETRYABLE_PARSE_ERRORS = (json.JSONDecodeError, ValidationError, ValueError)

_MAX_RETRIES = 3
_RETRY_DELAYS = [0.5, 1.0, 2.0]


def _strip_think_tags(text: str | None) -> str | None:
    """Remove <think>...</think> tags and markdown code fences from model output."""
    if not text:
        return text
    # Strip think tags (closed and unclosed/truncated)
    text = re.sub(r'<think>.*?</think>|<think>.*', '', text, flags=re.DOTALL).strip()
    # Strip markdown code fences (```json ... ``` or ``` ... ```)
    text = re.sub(r'^```(?:json)?\s*\n?', '', text)
    text = re.sub(r'\n?```\s*$', '', text)
    return text.strip()


async def complete(
    client: LLMClient,
    *,
    model: str,
    messages: list[dict[str, Any]],
    max_completion_tokens: int,
    temperature: float = 0,
    response_format: dict[str, str] | None = None,
    reasoning_effort: str | None = None,
    response_model: type[BaseModel] | None = None,
) -> tuple[Any, Any]:
    """Make an LLM call with retry on transient and parse errors.

    When response_model is set, auto-enables JSON mode, validates with Pydantic,
    and returns (model_instance, usage). Otherwise returns (content_str, usage).
    """
    kwargs: dict[str, Any] = dict(
        model=model, messages=messages,
        max_completion_tokens=max_completion_tokens, temperature=temperature,
    )
    # Resolve reasoning: explicit value > model default > skip for unsupported
    if model in REASONING_MODELS:
        effort = reasoning_effort if reasoning_effort is not None else REASONING_MODELS[model]
        kwargs["reasoning_effort"] = effort
    if response_model is not None and response_format is None and model in JSON_MODE_MODELS:
        response_format = {"type": "json_object"}
    if response_format is not None:
        kwargs["response_format"] = response_format

    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            response = await client.chat.completions.create(**kwargs)
            raw_content = response.choices[0].message.content if response.choices else None
            content = _strip_think_tags(raw_content) if raw_content else None

            if response_model is not None:
                if content is None:
                    raise ValueError("Empty response")
                parsed = response_model.model_validate_json(content)
                return (parsed, response.usage)

            return (content, response.usage)
        except _PERMANENT_ERRORS as e:
            # json_validate_failed = model ran out of tokens generating JSON; retryable
            if "json_validate_failed" in str(e):
                last_error = e
                if attempt < _MAX_RETRIES - 1:
                    delay = _RETRY_DELAYS[attempt]
                    log(f"  LLM retry ({attempt + 1}/{_MAX_RETRIES}): JSON generation failed — retrying in {delay}s")
                    await asyncio.sleep(delay)
                continue
            raise RuntimeError(f"Model config error: {e}") from e
        except (*_TRANSIENT_ERRORS, *_RETRYABLE_PARSE_ERRORS) as e:
            last_error = e
            if attempt < _MAX_RETRIES - 1:
                delay = _RETRY_DELAYS[attempt]
                log(f"  LLM retry ({attempt + 1}/{_MAX_RETRIES}): {e} — retrying in {delay}s")
                await asyncio.sleep(delay)

    raise last_error
