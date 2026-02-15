from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ProviderName = Literal["groq", "cerebras"]


@dataclass(frozen=True)
class TokenPricingPer1M:
    """Dummy pricing map (USD) per 1M tokens.

    Update these values with real pricing later.
    """

    input_usd_per_1m: float
    output_usd_per_1m: float
    cached_input_usd_per_1m: float = 0.0
    currency: str = "USD"


@dataclass(frozen=True)
class ModelSpec:
    provider: ProviderName
    name: str

    # Request capabilities
    supports_response_format_json_object: bool = False
    supports_reasoning_effort: bool = False
    reasoning_effort_allowed: tuple[str, ...] = ()
    default_reasoning_effort: str | None = None

    # Provider-specific defaults
    supports_disable_reasoning: bool = False
    disable_reasoning_by_default: bool = False

    # Response parsing
    response_text_fields_priority: tuple[str, ...] = ("content", "reasoning")

    pricing: TokenPricingPer1M = TokenPricingPer1M(
        input_usd_per_1m=0.0,
        output_usd_per_1m=0.0,
        cached_input_usd_per_1m=0.0,
    )


@dataclass(frozen=True)
class ProviderDefaults:
    provider: ProviderName
    model: str
    oracle: str
    action: str
    filter: str


# ---- Registry ----------------------------------------------------------------

# NOTE: Dummy pricing placeholders (0.0). Update later.

_MODEL_SPECS: dict[tuple[ProviderName, str], ModelSpec] = {
    # Groq
    (
        "groq",
        "qwen/qwen3-32b",
    ): ModelSpec(
        provider="groq",
        name="qwen/qwen3-32b",
        supports_response_format_json_object=False,
        supports_reasoning_effort=True,
        reasoning_effort_allowed=("none", "low", "medium", "high"),
        default_reasoning_effort="none",
        disable_reasoning_by_default=False,
        pricing=TokenPricingPer1M(0.0, 0.0, 0.0),
    ),
    (
        "groq",
        "meta-llama/llama-4-scout-17b-16e-instruct",
    ): ModelSpec(
        provider="groq",
        name="meta-llama/llama-4-scout-17b-16e-instruct",
        supports_response_format_json_object=True,
        supports_reasoning_effort=False,
        pricing=TokenPricingPer1M(0.0, 0.0, 0.0),
    ),
    ("groq", "llama-3.1-8b-instant"): ModelSpec(
        provider="groq",
        name="llama-3.1-8b-instant",
        supports_response_format_json_object=True,
        supports_reasoning_effort=False,
        pricing=TokenPricingPer1M(0.0, 0.0, 0.0),
    ),
    ("groq", "moonshotai/kimi-k2-instruct-0905"): ModelSpec(
        provider="groq",
        name="moonshotai/kimi-k2-instruct-0905",
        # Groq's server-side json_object enforcement is brittle with this model
        # (can 400 with json_validate_failed). Prefer prompt-based JSON + local parsing.
        supports_response_format_json_object=False,
        supports_reasoning_effort=False,
        pricing=TokenPricingPer1M(0.0, 0.0, 0.0),
    ),
    # Cerebras
    ("cerebras", "qwen-3-32b"): ModelSpec(
        provider="cerebras",
        name="qwen-3-32b",
        supports_response_format_json_object=False,
        supports_reasoning_effort=True,
        reasoning_effort_allowed=("low", "medium", "high"),
        default_reasoning_effort=None,
        supports_disable_reasoning=True,
        disable_reasoning_by_default=True,
        pricing=TokenPricingPer1M(0.0, 0.0, 0.0),
    ),
    ("cerebras", "llama3.1-8b"): ModelSpec(
        provider="cerebras",
        name="llama3.1-8b",
        supports_response_format_json_object=True,
        supports_reasoning_effort=False,
        supports_disable_reasoning=False,
        disable_reasoning_by_default=False,
        pricing=TokenPricingPer1M(0.0, 0.0, 0.0),
    ),
    ("cerebras", "llama-3.3-70b"): ModelSpec(
        provider="cerebras",
        name="llama-3.3-70b",
        supports_response_format_json_object=True,
        supports_reasoning_effort=False,
        supports_disable_reasoning=False,
        disable_reasoning_by_default=False,
        pricing=TokenPricingPer1M(0.0, 0.0, 0.0),
    ),
    ("cerebras", "zai-glm-4.7"): ModelSpec(
        provider="cerebras",
        name="zai-glm-4.7",
        supports_response_format_json_object=False,
        supports_reasoning_effort=True,
        reasoning_effort_allowed=("low", "medium", "high"),
        default_reasoning_effort=None,
        supports_disable_reasoning=True,
        disable_reasoning_by_default=True,
        pricing=TokenPricingPer1M(0.0, 0.0, 0.0),
    ),
}


_PROVIDER_DEFAULTS: dict[ProviderName, ProviderDefaults] = {
    "groq": ProviderDefaults(
        provider="groq",
        model="qwen/qwen3-32b",
        oracle="moonshotai/kimi-k2-instruct-0905",
        action="meta-llama/llama-4-scout-17b-16e-instruct",
        filter="llama-3.1-8b-instant",
    ),
    "cerebras": ProviderDefaults(
        provider="cerebras",
        model="qwen-3-32b",
        oracle="qwen-3-32b",
        action="llama3.1-8b",
        filter="llama3.1-8b",
    ),
}


def get_provider_defaults(provider: ProviderName) -> ProviderDefaults:
    return _PROVIDER_DEFAULTS[provider]


def get_model_spec(provider: ProviderName, model: str) -> ModelSpec | None:
    return _MODEL_SPECS.get((provider, model))


def find_unique_model_spec(model: str) -> ModelSpec | None:
    found = [spec for (_p, m), spec in _MODEL_SPECS.items() if m == model]
    if len(found) == 1:
        return found[0]
    return None


# Backwards-compatible mapping used by older scripts.
PROVIDER_MODELS: dict[str, dict[str, str]] = {
    p: {
        "model": d.model,
        "oracle": d.oracle,
        "action": d.action,
        "filter": d.filter,
    }
    for p, d in _PROVIDER_DEFAULTS.items()
}
