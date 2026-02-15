"""Runtime configuration for the browser agent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ModelProvider = Literal["openrouter"]


@dataclass(frozen=True)
class LLMConfig:
    """LLM configuration for OpenRouter-backed models."""

    provider: ModelProvider = "openrouter"
    base_url: str = "https://openrouter.ai/api/v1"
    model: str = "moonshotai/kimi-k2-0905:exacto"
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = None
    api_key_env: str = "OPENROUTER_API_KEY"
    timeout_seconds: int = 60


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
    decoy_guard_enabled: bool = True
    log_level: str = "INFO"
    metrics_enabled: bool = True
