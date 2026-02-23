"""Runtime configuration for the browser agent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ModelProvider = Literal["openrouter", "cerebras", "groq"]

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
    "groq": {
        "model": "moonshotai/kimi-k2-instruct-0905",
        "worker_model": "",
        "filter_model": "",
        "api_key_env": "GROQ_API_KEY",
    },
}


@dataclass(frozen=True)
class ModelPricing:
    """Price per million tokens (USD)."""

    input_per_mtok: float
    output_per_mtok: float
    cache_write_per_mtok: float | None = None  # None → same as input_per_mtok
    cache_read_per_mtok: float | None = None  # None → same as input_per_mtok


MODEL_PRICES: dict[str, ModelPricing] = {
    "qwen-3-235b-a22b-instruct-2507": ModelPricing(0.60, 1.20),
    "zai-glm-4.7": ModelPricing(2.25, 2.75),
}


@dataclass(frozen=True)
class LLMConfig:
    """LLM configuration for the browser agent.

    Supports multiple providers (OpenRouter, Cerebras, Groq).
    Use PROVIDER_DEFAULTS to get default model and api_key_env per provider.
    """

    provider: ModelProvider = "openrouter"
    base_url: str = "https://openrouter.ai/api/v1"
    model: str = "moonshotai/kimi-k2-0905:exacto"
    worker_model: str | None = None
    filter_model: str | None = None
    oracle_model: str | None = None
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = None
    api_key_env: str = "OPENROUTER_API_KEY"
    timeout_seconds: int = 60
    max_retries: int = 2
    max_tokens: int = 2048


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
    step_timeout_seconds: int = 300
    log_dir: str = "logs"
    max_log_runs: int = 10
    max_elements: int = 200
    memory_steps: int = 10
    worker_context_steps: int = 3
    stuck_threshold: int = 3
    unchanged_abort_threshold: int = 5
    oracle_interval: int = 5
    oracle_trace_window: int = 15
    max_worker_tool_calls: int = 10
    widen_on_oracle: bool = False
    # Snapshot semantics
    desc_text_preview_enabled: bool = True
    desc_text_preview_max_chars: int = 240
    desc_text_preview_max_nodes: int = 200
    # Stuck detection
    progress_fingerprint_enabled: bool = True
    progress_fingerprint_max_elements: int = 120
    progress_fingerprint_raw_lines: int = 60
    progress_fingerprint_raw_chars: int = 8000
    # Snapshot formatting / token efficiency
    class_sanitize_mode: Literal["off", "aggressive"] = "aggressive"
    class_sanitize_max_tokens: int = 6
    class_sanitize_max_chars: int = 80
    class_sanitize_fallback_tokens: int = 2
    snapshot_attr_value_max_len: int = 120
    log_level: str = "INFO"
    metrics_enabled: bool = True
    color_logs: bool = True
    handlers_enabled: bool = True
    scroll_containers_enabled: bool = True
    save_pages: bool = False
    raw_text_limit_prompt: int = 300
    raw_text_limit_fingerprint: int = 200
    raw_text_limit_diff: int = 80
    raw_text_diff_detail_limit: int = 8
    diff_changed_limit: int = 100
    raw_text_line_max_len: int = 800
    raw_text_scan_cap: int = 20000
    raw_text_dedupe_prefix_len: int = 240
    raw_text_dedupe_suffix_len: int = 120
    # Tool timing (ms)
    settle_ms: int = 200
    draw_settle_ms: int = 400
    draw_point_interval_ms: int = 20
    drag_phase_interval_ms: int = 50
    networkidle_timeout_ms: int = 3000
    unified: bool = False
