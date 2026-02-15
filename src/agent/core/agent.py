"""Orchestration loop for the browser agent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from src.agent.config import AgentConfig, BrowserConfig, LLMConfig


@dataclass
class AgentState:
    """Minimal state tracked across steps."""

    step: int = 0
    last_observation: str | None = None


class BrowserAgent:
    """Main agent loop wrapper.

    This is intentionally minimal for the initial scaffold. The orchestration
    loop will be wired up once the browser context + tools are implemented.
    """

    def __init__(
        self,
        agent_config: AgentConfig,
        llm_config: LLMConfig,
        browser_config: BrowserConfig,
    ) -> None:
        self.agent_config = agent_config
        self.llm_config = llm_config
        self.browser_config = browser_config
        self.state = AgentState()

    async def run(self) -> None:
        """Run the agent loop."""

        for _ in self._steps():
            # Placeholder: wire up context extraction + tool execution later.
            self.state.step += 1

    def _steps(self) -> Iterable[int]:
        return range(self.agent_config.max_steps)
