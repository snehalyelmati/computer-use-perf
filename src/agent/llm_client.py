from __future__ import annotations

import asyncio
import contextvars
import json
import re
import time
from typing import Any, Union, cast

import cerebras.cloud.sdk as cerebras_sdk
import groq
from cerebras.cloud.sdk import AsyncCerebras
from groq import AsyncGroq
from pydantic import BaseModel, ValidationError

from .logging_utils import log
from .providers import (
    ProviderName,
    find_unique_model_spec,
    get_model_spec,
)
from .stats import StatsCollector

LLMClient = Union[AsyncGroq, AsyncCerebras]

_STATS_VAR: contextvars.ContextVar[StatsCollector | None] = contextvars.ContextVar(
    "llm_stats_collector", default=None
)


def set_stats_collector(stats: StatsCollector | None) -> None:
    _STATS_VAR.set(stats)


_TRANSIENT_ERRORS = (
    groq.RateLimitError,
    groq.APITimeoutError,
    groq.APIConnectionError,
    groq.InternalServerError,
    cerebras_sdk.RateLimitError,
    cerebras_sdk.APITimeoutError,
    cerebras_sdk.APIConnectionError,
    cerebras_sdk.InternalServerError,
)
_PERMANENT_ERRORS = (
    groq.AuthenticationError,
    groq.NotFoundError,
    groq.BadRequestError,
    cerebras_sdk.AuthenticationError,
    cerebras_sdk.NotFoundError,
    cerebras_sdk.BadRequestError,
)
_RETRYABLE_PARSE_ERRORS = (json.JSONDecodeError, ValidationError, ValueError)

_MAX_RETRIES = 3
_RETRY_DELAYS = [0.5, 1.0, 2.0]


def _strip_think_tags(text: str | None) -> str | None:
    """Remove <think>...</think> tags and markdown code fences from model output."""
    if not text:
        return text
    text = re.sub(r"<think>.*?</think>|<think>.*", "", text, flags=re.DOTALL).strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _detect_provider(client: object) -> ProviderName | None:
    if isinstance(client, AsyncGroq):
        return "groq"
    if isinstance(client, AsyncCerebras):
        return "cerebras"
    return None


def _extract_message_text(message: object, *, fields: tuple[str, ...]) -> str | None:
    for field in fields:
        try:
            v = getattr(message, field, None)
        except Exception:
            v = None
        if v is None:
            continue
        if not isinstance(v, str):
            v = str(v)
        s = v.strip()
        if s:
            return s
    return None


def _first_json_value(text: str) -> object | None:
    """Best-effort extraction of the first JSON value inside text."""
    s = text.strip()
    if not s:
        return None
    decoder = json.JSONDecoder()

    # Try full string first.
    try:
        obj, _end = decoder.raw_decode(s)
        return obj
    except json.JSONDecodeError:
        pass

    # Then scan for the first object/array.
    starts: list[int] = []
    for ch in ("{", "["):
        i = s.find(ch)
        if i >= 0:
            starts.append(i)
    for start in sorted(set(starts)):
        try:
            obj, _end = decoder.raw_decode(s[start:])
            return obj
        except json.JSONDecodeError:
            continue
    return None


def _parse_response_model(model: type[BaseModel], text: str) -> BaseModel:
    """Parse a response model from JSON text, with a fallback extractor."""
    try:
        return model.model_validate_json(text)
    except _RETRYABLE_PARSE_ERRORS:
        obj = _first_json_value(text)
        if obj is None:
            raise
        return model.model_validate(obj)


def _usage_tokens(usage: object) -> tuple[int, int, int]:
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
    cached_prompt_tokens = 0
    if usage:
        try:
            ptd = getattr(usage, "prompt_tokens_details", None)
            cached_prompt_tokens = (
                int(getattr(ptd, "cached_tokens", 0) or 0) if ptd else 0
            )
        except Exception:
            cached_prompt_tokens = 0
    return (prompt_tokens, completion_tokens, cached_prompt_tokens)


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
    call_type: str | None = None,
) -> tuple[Any, Any]:
    """Make an LLM call with retry on transient and parse errors.

    When response_model is set, validates with Pydantic and returns (model_instance, usage).
    Otherwise returns (content_str, usage).
    """

    provider = _detect_provider(client)
    spec = get_model_spec(provider, model) if provider else None
    if spec is None:
        spec = find_unique_model_spec(model)
    if provider is None and spec is not None:
        provider = spec.provider

    # Infer call_type when not provided so failures are attributable.
    ct = call_type
    if ct is None and response_model is not None:
        name = getattr(response_model, "__name__", "")
        ct = {
            "OracleResponse": "oracle",
            "OverviewResponse": "overview",
            "ActionResponse": "action",
            "LearningResponse": "learning",
        }.get(name, None)
    ct = ct or "unknown"

    kwargs: dict[str, Any] = dict(
        model=model,
        messages=messages,
        max_completion_tokens=max_completion_tokens,
        temperature=temperature,
    )

    # Provider/model knobs.
    disable_reasoning: bool | None = None
    if (
        provider == "cerebras"
        and spec is not None
        and getattr(spec, "supports_disable_reasoning", False)
    ):
        disable_reasoning = bool(spec.disable_reasoning_by_default)

    # Reasoning effort.
    effort_to_send: str | None = None
    if spec is not None and spec.supports_reasoning_effort:
        if (
            reasoning_effort is not None
            and reasoning_effort in spec.reasoning_effort_allowed
        ):
            effort_to_send = reasoning_effort
        elif reasoning_effort is None and spec.default_reasoning_effort is not None:
            effort_to_send = spec.default_reasoning_effort

    # If the caller explicitly requested reasoning effort on Cerebras, enable reasoning.
    if (
        provider == "cerebras"
        and effort_to_send is not None
        and disable_reasoning is not None
    ):
        disable_reasoning = False

    if provider == "cerebras":
        # Disable thinking by default when supported.
        if disable_reasoning is not None:
            kwargs["disable_reasoning"] = disable_reasoning
        # Cerebras rejects "none"; only send low/medium/high.
        if effort_to_send in ("low", "medium", "high"):
            kwargs["reasoning_effort"] = effort_to_send
    else:
        # Groq supports reasoning_effort for some models.
        if effort_to_send is not None:
            kwargs["reasoning_effort"] = effort_to_send

    if (
        response_model is not None
        and response_format is None
        and spec is not None
        and spec.supports_response_format_json_object
    ):
        response_format = {"type": "json_object"}
    if response_format is not None:
        kwargs["response_format"] = response_format

    stats = _STATS_VAR.get()

    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        start = time.time()
        try:
            response: Any = await client.chat.completions.create(**kwargs)
            duration_s = max(0.0, time.time() - start)

            usage = getattr(response, "usage", None)
            prompt_tokens, completion_tokens, cached_prompt_tokens = _usage_tokens(
                usage
            )

            raw_text = None
            try:
                choices = getattr(response, "choices", None)
                if choices:
                    msg = getattr(choices[0], "message", None)
                    fields = (
                        spec.response_text_fields_priority
                        if spec is not None
                        else ("content", "reasoning")
                    )
                    raw_text = (
                        _extract_message_text(msg, fields=fields) if msg else None
                    )
            except Exception:
                raw_text = None

            text = _strip_think_tags(raw_text) if raw_text else None
            if text is None:
                err = ValueError("Empty response")
                last_error = err
                if stats is not None:
                    stats.record_llm_call(
                        call_type=ct,
                        model=model,
                        provider=provider or "unknown",
                        attempt=attempt + 1,
                        ok=False,
                        duration_s=duration_s,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        cached_prompt_tokens=cached_prompt_tokens,
                        error=str(err),
                    )
                if attempt < _MAX_RETRIES - 1:
                    delay = _RETRY_DELAYS[attempt]
                    log(
                        f"  LLM retry ({attempt + 1}/{_MAX_RETRIES}): {err} - retrying in {delay}s"
                    )
                    await asyncio.sleep(delay)
                    continue
                raise err

            if response_model is not None:
                try:
                    parsed = cast(
                        BaseModel, _parse_response_model(response_model, text)
                    )
                except _RETRYABLE_PARSE_ERRORS as e:
                    last_error = e
                    if stats is not None:
                        stats.record_llm_call(
                            call_type=ct,
                            model=model,
                            provider=provider or "unknown",
                            attempt=attempt + 1,
                            ok=False,
                            duration_s=duration_s,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            cached_prompt_tokens=cached_prompt_tokens,
                            error=str(e),
                        )
                    if attempt < _MAX_RETRIES - 1:
                        delay = _RETRY_DELAYS[attempt]
                        log(
                            f"  LLM retry ({attempt + 1}/{_MAX_RETRIES}): {e} - retrying in {delay}s"
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise

                if stats is not None:
                    stats.record_llm_call(
                        call_type=ct,
                        model=model,
                        provider=provider or "unknown",
                        attempt=attempt + 1,
                        ok=True,
                        duration_s=duration_s,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        cached_prompt_tokens=cached_prompt_tokens,
                    )
                return (parsed, usage)

            if stats is not None:
                stats.record_llm_call(
                    call_type=ct,
                    model=model,
                    provider=provider or "unknown",
                    attempt=attempt + 1,
                    ok=True,
                    duration_s=duration_s,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cached_prompt_tokens=cached_prompt_tokens,
                )
            return (text, usage)

        except _PERMANENT_ERRORS as e:
            # json_validate_failed = model ran out of tokens generating JSON; retryable.
            if "json_validate_failed" in str(e):
                last_error = e
                if attempt < _MAX_RETRIES - 1:
                    delay = _RETRY_DELAYS[attempt]
                    log(
                        f"  LLM retry ({attempt + 1}/{_MAX_RETRIES}): JSON generation failed - retrying in {delay}s"
                    )
                    await asyncio.sleep(delay)
                    continue
            raise RuntimeError(f"Model config error: {e}") from e
        except _TRANSIENT_ERRORS as e:
            last_error = e
            duration_s = max(0.0, time.time() - start)
            if stats is not None:
                stats.record_llm_call(
                    call_type=ct,
                    model=model,
                    provider=provider or "unknown",
                    attempt=attempt + 1,
                    ok=False,
                    duration_s=duration_s,
                    prompt_tokens=0,
                    completion_tokens=0,
                    cached_prompt_tokens=0,
                    error=str(e),
                )
            if attempt < _MAX_RETRIES - 1:
                delay = _RETRY_DELAYS[attempt]
                log(
                    f"  LLM retry ({attempt + 1}/{_MAX_RETRIES}): {e} - retrying in {delay}s"
                )
                await asyncio.sleep(delay)

    err = last_error
    if err is None:
        raise RuntimeError("LLM call failed after retries")
    raise err
