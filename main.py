"""Entrypoint for the general-purpose browser agent."""

from __future__ import annotations

import argparse
from pathlib import Path

from src.agent.config import AgentConfig, BrowserConfig, LLMConfig, PROVIDER_DEFAULTS
from src.agent.core.agent import run_agent_sync


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed

def _load_task(path: str) -> str:
    content = Path(path).read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError("task file is empty")
    return content


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="General-purpose browser agent")
    parser.add_argument("--url", dest="target_url", required=True, help="Target URL")
    parser.add_argument("--task", required=True, help="Path to a markdown file describing the task")
    parser.add_argument(
        "--provider",
        choices=list(PROVIDER_DEFAULTS.keys()),
        default="openrouter",
        help="LLM provider (default: openrouter)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name (overrides provider default)",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=100,
        help="Max agent steps before stopping",
    )
    parser.add_argument(
        "--max-elements",
        type=int,
        default=60,
        help="Max interactive elements to include in LLM snapshot context",
    )
    parser.add_argument(
        "--stuck-threshold",
        type=int,
        default=3,
        help="Steps with no progress before firing the Oracle advisor",
    )
    parser.add_argument(
        "--unchanged-abort-threshold",
        type=_positive_int,
        default=5,
        help="Abort if the page fingerprint is unchanged for this many consecutive steps",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (e.g. DEBUG, INFO, WARNING)",
    )
    parser.add_argument(
        "--no-metrics",
        action="store_true",
        help="Disable JSONL metrics output",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored log output (auto-disabled when not a TTY)",
    )
    parser.add_argument(
        "--worker-model",
        dest="worker_model",
        default=None,
        help="Model for the browser worker agent (default: same as --model)",
    )
    parser.add_argument(
        "--filter-model",
        dest="filter_model",
        default=None,
        help="Model for the snapshot filter agent (default: same as --model)",
    )
    parser.add_argument(
        "--oracle-model",
        dest="oracle_model",
        default=None,
        help="Model for the Oracle advisor agent (default: same as --model)",
    )
    parser.add_argument(
        "--oracle-interval",
        type=int,
        default=5,
        help="Call Oracle every N steps as a health check (0 disables periodic checks)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=2048,
        help="Max completion tokens per LLM call (prevents runaway repetition)",
    )
    parser.add_argument(
        "--no-handlers",
        action="store_true",
        help="Disable JS event handler extraction from DOM elements",
    )
    parser.add_argument(
        "--save-pages",
        action="store_true",
        help="Save page HTML snapshots to logs/pages/ for local replay",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        task_text = _load_task(args.task)
    except (OSError, ValueError) as exc:
        parser.error(f"Failed to read task file: {exc}")

    agent_config = AgentConfig(
        target_url=args.target_url,
        goal=task_text,
        max_steps=args.max_steps,
        max_elements=args.max_elements,
        stuck_threshold=args.stuck_threshold,
        unchanged_abort_threshold=args.unchanged_abort_threshold,
        oracle_interval=args.oracle_interval,
        log_level=str(args.log_level),
        metrics_enabled=not bool(args.no_metrics),
        color_logs=not bool(args.no_color),
        handlers_enabled=not bool(args.no_handlers),
        save_pages=bool(args.save_pages),
    )
    provider = args.provider
    defaults = PROVIDER_DEFAULTS[provider]
    llm_config = LLMConfig(
        provider=provider,
        model=args.model or defaults["model"],
        worker_model=args.worker_model or defaults.get("worker_model") or None,
        filter_model=args.filter_model or defaults.get("filter_model") or None,
        oracle_model=args.oracle_model or defaults.get("oracle_model") or None,
        api_key_env=defaults["api_key_env"],
        max_tokens=args.max_tokens,
    )
    browser_config = BrowserConfig(headless=bool(args.headless))

    try:
        run_agent_sync(agent_config, llm_config, browser_config)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
