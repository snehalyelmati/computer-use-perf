"""Multi-agent orchestration loop for the browser agent."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import Any

from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import UserError
from pydantic_ai.models.openrouter import OpenRouterModel

from src.agent.browser.session import close_browser, launch_browser
from src.agent.config import AgentConfig, BrowserConfig, LLMConfig
from src.agent.context.snapshot import (
    build_element_index,
    capture_snapshot,
    format_snapshot_for_llm,
)
from src.agent.models.actions import OrchestratorDecision, StepOutput, ToolExecutionResult
from src.agent.prompts.system import ORCHESTRATOR_PROMPT, STEP_PROMPT, SYSTEM_PROMPT
from src.agent.tools import semantic

logger = logging.getLogger(__name__)


@dataclass
class AgentState:
    step: int = 0
    active_frame_id: str | None = None
    memory: list[str] = field(default_factory=list)
    last_summary: str | None = None


@dataclass(frozen=True)
class WorkerDeps:
    tool_context: semantic.ToolContext


def _setup_logging(log_dir: str) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(Path(log_dir) / "agent.log"),
            logging.StreamHandler(),
        ],
    )


def _build_openrouter_model(config: LLMConfig) -> OpenRouterModel:
    # PydanticAI's OpenRouter provider uses OPENROUTER_API_KEY by default.
    # We keep env var selection in our config for consistency.
    # If the env var name differs from OPENROUTER_API_KEY, copy it over.
    import os

    if config.api_key_env != "OPENROUTER_API_KEY":
        if value := os.environ.get(config.api_key_env):
            os.environ.setdefault("OPENROUTER_API_KEY", value)
    try:
        return OpenRouterModel(config.model)
    except UserError:
        raise


def _model_settings(config: LLMConfig) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "timeout": float(config.timeout_seconds),
        "parallel_tool_calls": False,
    }
    if config.reasoning_effort and config.reasoning_effort != "none":
        settings["openrouter_reasoning"] = {"effort": config.reasoning_effort}
    return settings


def _format_memory(memory: list[str], *, limit: int = 10) -> str:
    if not memory:
        return "None."
    recent = memory[-limit:]
    lines = [f"{idx + 1}. {item}" for idx, item in enumerate(recent)]
    return "\n".join(lines)


def build_orchestrator_agent(model: OpenRouterModel, *, model_settings: dict[str, Any]) -> Agent[None, OrchestratorDecision]:
    return Agent(
        model,
        output_type=OrchestratorDecision,
        system_prompt=(SYSTEM_PROMPT, ORCHESTRATOR_PROMPT),
        model_settings=model_settings,
        retries=1,
    )


def build_browser_worker_agent(
    model: OpenRouterModel, *, model_settings: dict[str, Any]
) -> Agent[WorkerDeps, StepOutput]:
    agent: Agent[WorkerDeps, StepOutput] = Agent(
        model,
        deps_type=WorkerDeps,
        output_type=StepOutput,
        system_prompt=SYSTEM_PROMPT,
        model_settings=model_settings,
        retries=1,
    )

    @agent.tool(name="click_element")
    async def click_element(ctx: RunContext[WorkerDeps], element_id: str) -> ToolExecutionResult:
        result = await semantic.click_element(element_id, ctx.deps.tool_context)
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="type_text")
    async def type_text(ctx: RunContext[WorkerDeps], element_id: str, text: str) -> ToolExecutionResult:
        result = await semantic.type_text(element_id, text, ctx.deps.tool_context)
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="drag_and_drop")
    async def drag_and_drop(ctx: RunContext[WorkerDeps], source_id: str, target_id: str) -> ToolExecutionResult:
        result = await semantic.drag_and_drop(source_id, target_id, ctx.deps.tool_context)
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="select_all")
    async def select_all(ctx: RunContext[WorkerDeps]) -> ToolExecutionResult:
        result = await semantic.select_all(ctx.deps.tool_context)
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="copy_selection")
    async def copy_selection(ctx: RunContext[WorkerDeps]) -> ToolExecutionResult:
        result = await semantic.copy_selection(ctx.deps.tool_context)
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="paste")
    async def paste(ctx: RunContext[WorkerDeps]) -> ToolExecutionResult:
        result = await semantic.paste(ctx.deps.tool_context)
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="read_element_text")
    async def read_element_text(ctx: RunContext[WorkerDeps], element_id: str) -> ToolExecutionResult:
        result = await semantic.read_element_text(element_id, ctx.deps.tool_context)
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="switch_to_iframe")
    async def switch_to_iframe(ctx: RunContext[WorkerDeps], iframe_id: str) -> ToolExecutionResult:
        result = await semantic.switch_to_iframe(iframe_id, ctx.deps.tool_context)
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="switch_to_main_frame")
    async def switch_to_main_frame(ctx: RunContext[WorkerDeps]) -> ToolExecutionResult:
        result = await semantic.switch_to_main_frame(ctx.deps.tool_context)
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="navigate_to")
    async def navigate_to(ctx: RunContext[WorkerDeps], url: str) -> ToolExecutionResult:
        result = await semantic.navigate_to(url, ctx.deps.tool_context)
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="take_screenshot")
    async def take_screenshot(ctx: RunContext[WorkerDeps]) -> ToolExecutionResult:
        result = await semantic.take_screenshot(ctx.deps.tool_context)
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="execute_js")
    async def execute_js(ctx: RunContext[WorkerDeps], code: str) -> ToolExecutionResult:
        result = await semantic.execute_js(code, ctx.deps.tool_context)
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="press_key_combination")
    async def press_key_combination(ctx: RunContext[WorkerDeps], keys: list[str]) -> ToolExecutionResult:
        result = await semantic.press_key_combination(keys, ctx.deps.tool_context)
        return ToolExecutionResult(ok=result.ok, message=result.message)

    return agent


class BrowserAgent:
    """Top-level orchestrator that delegates browser work to worker agents."""

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
        if not self.agent_config.target_url:
            raise ValueError("target_url is required")
        if not self.agent_config.goal:
            raise ValueError("goal is required")

        _setup_logging(self.agent_config.log_dir)

        model = _build_openrouter_model(self.llm_config)
        model_settings = _model_settings(self.llm_config)
        orchestrator = build_orchestrator_agent(model, model_settings=model_settings)
        browser_worker = build_browser_worker_agent(model, model_settings=model_settings)

        session = await launch_browser(self.browser_config)
        try:
            await session.page.goto(self.agent_config.target_url)
            for step in range(self.agent_config.max_steps):
                self.state.step = step + 1
                snapshot = await capture_snapshot(session.page, session.cdp_session)
                element_index = build_element_index(snapshot)
                tool_context = semantic.build_tool_context(
                    session,
                    element_index,
                    active_frame_id=self.state.active_frame_id,
                )
                deps = WorkerDeps(tool_context=tool_context)
                snapshot_text = format_snapshot_for_llm(snapshot, max_elements=self.agent_config.max_elements)

                memory_text = _format_memory(self.state.memory, limit=self.agent_config.memory_steps)
                orchestrator_prompt = (
                    f"Overall goal: {self.agent_config.goal}\n\n"
                    f"Memory (recent):\n{memory_text}\n\n"
                    f"Page snapshot:\n{snapshot_text}\n"
                )
                decision_result = await orchestrator.run(orchestrator_prompt)
                decision = decision_result.output
                logger.info("Step %s orchestrator decision: %s", self.state.step, decision.worker_goal)
                if decision.done:
                    logger.info("Done (orchestrator): %s", decision.rationale or "")
                    break

                worker_prompt = (
                    STEP_PROMPT.format(goal=decision.worker_goal)
                    + "\n\n"
                    + f"Overall goal: {self.agent_config.goal}\n\n"
                    + f"Memory (recent):\n{memory_text}\n\n"
                    + f"Page snapshot:\n{snapshot_text}\n"
                )
                with browser_worker.sequential_tool_calls():
                    worker_result = await browser_worker.run(worker_prompt, deps=deps)
                step_output = worker_result.output
                self.state.active_frame_id = tool_context.active_frame_id
                self.state.last_summary = step_output.summary
                self.state.memory.append(step_output.summary)
                logger.info("Step %s summary: %s", self.state.step, step_output.summary)
                if step_output.done:
                    logger.info("Done (worker).")
                    break
        finally:
            await close_browser(session)


async def run_agent(agent_config: AgentConfig, llm_config: LLMConfig, browser_config: BrowserConfig) -> None:
    agent = BrowserAgent(agent_config, llm_config, browser_config)
    await agent.run()


def run_agent_sync(agent_config: AgentConfig, llm_config: LLMConfig, browser_config: BrowserConfig) -> None:
    asyncio.run(run_agent(agent_config, llm_config, browser_config))

