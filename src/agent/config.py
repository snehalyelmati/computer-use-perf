"""Runtime configuration for the browser agent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ModelProvider = Literal["openrouter", "cerebras"]

PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "openrouter": {
        "model": "moonshotai/kimi-k2-0905:exacto",
        "worker_model": "",
        "filter_model": "",
        "api_key_env": "OPENROUTER_API_KEY",
    },
    "cerebras": {
        "model": "qwen-3-235b-a22b-instruct-2507",
        "worker_model": "",
        "filter_model": "",
        "api_key_env": "CEREBRAS_API_KEY",
    },
}


@dataclass(frozen=True)
class ModelPricing:
    """Price per million tokens (USD)."""

    input_per_mtok: float
    output_per_mtok: float


MODEL_PRICES: dict[str, ModelPricing] = {
    "qwen-3-235b-a22b-instruct-2507": ModelPricing(0.60, 1.20),
    "zai-glm-4.7": ModelPricing(2.25, 2.75),
}


@dataclass(frozen=True)
class LLMConfig:
    """LLM configuration for the browser agent.

    Supports multiple providers (OpenRouter, Cerebras).
    Use PROVIDER_DEFAULTS to get default model and api_key_env per provider.
    """

    provider: ModelProvider = "openrouter"
    base_url: str = "https://openrouter.ai/api/v1"
    model: str = "moonshotai/kimi-k2-0905:exacto"
    worker_model: str | None = None
    filter_model: str | None = None
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = None
    api_key_env: str = "OPENROUTER_API_KEY"
    timeout_seconds: int = 60
    max_retries: int = 2


@dataclass(frozen=True)
class BrowserConfig:
    """Browser automation configuration."""

    headless: bool = False
    viewport_width: int = 1280
    viewport_height: int = 720
    cdp_url: str | None = None


@dataclass(frozen=True)
class AgentConfig:
    """Top-level agent runtime settings."""

    target_url: str | None = None
    goal: str | None = None
    max_steps: int = 100
    log_dir: str = "logs"
    max_elements: int = 60
    memory_steps: int = 10
    stuck_threshold: int = 2
    unchanged_abort_threshold: int = 3
    log_level: str = "INFO"
    metrics_enabled: bool = True
    color_logs: bool = True
