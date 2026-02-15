"""Entrypoint for the general-purpose browser agent."""

from __future__ import annotations

import argparse

from src.agent.config import AgentConfig, BrowserConfig, LLMConfig
from src.agent.core.agent import BrowserAgent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="General-purpose browser agent")
    parser.add_argument("--url", dest="target_url", help="Target URL")
    parser.add_argument(
        "--max-steps",
        type=int,
        default=100,
        help="Max agent steps before stopping",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    agent_config = AgentConfig(target_url=args.target_url, max_steps=args.max_steps)
    llm_config = LLMConfig()
    browser_config = BrowserConfig()

    agent = BrowserAgent(agent_config, llm_config, browser_config)
    # The run loop is async; wiring will happen once the browser context is ready.
    # For now, we keep the entrypoint lightweight.
    # asyncio.run(agent.run())
    print("Agent scaffold initialized.")


if __name__ == "__main__":
    main()
