"""Entrypoint for the general-purpose browser agent."""

from __future__ import annotations

import argparse

from src.agent.config import AgentConfig, BrowserConfig, LLMConfig
from src.agent.core.agent import run_agent_sync


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="General-purpose browser agent")
    parser.add_argument("--url", dest="target_url", required=True, help="Target URL")
    parser.add_argument("--goal", required=True, help="High-level task for the agent to complete")
    parser.add_argument(
        "--max-steps",
        type=int,
        default=100,
        help="Max agent steps before stopping",
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
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    agent_config = AgentConfig(
        target_url=args.target_url,
        goal=args.goal,
        max_steps=args.max_steps,
        log_level=str(args.log_level),
        metrics_enabled=not bool(args.no_metrics),
    )
    llm_config = LLMConfig()
    browser_config = BrowserConfig(headless=bool(args.headless))

    run_agent_sync(agent_config, llm_config, browser_config)


if __name__ == "__main__":
    main()
