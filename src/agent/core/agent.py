"""Multi-agent orchestration loop for the browser agent."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import logging
from pathlib import Path
import time
from typing import Any

from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import UserError
from pydantic_ai.models.openrouter import OpenRouterModel

from src.agent.browser.session import close_browser, launch_browser
from src.agent.config import AgentConfig, BrowserConfig, LLMConfig
from src.agent.context.snapshot import (
    ElementSnapshot,
    build_element_index,
    capture_snapshot,
    format_snapshot_for_llm,
    search_elements,
)
from src.agent.metrics import (
    MetricsRecorder,
    cost_stats_from_result,
    new_run_id,
    usage_stats_from_result,
    write_run_summary,
)
from src.agent.models.actions import ClickGuardDecision, OrchestratorDecision, StepOutput, ToolExecutionResult
from src.agent.prompts.system import CLICK_GUARD_PROMPT, ORCHESTRATOR_PROMPT, STEP_PROMPT, SYSTEM_PROMPT
from src.agent.tools import semantic

logger = logging.getLogger(__name__)


@dataclass
class AgentState:
    step: int = 0
    active_frame_id: str | None = None
    memory: list[str] = field(default_factory=list)
    last_summary: str | None = None
    last_snapshot_digest: str | None = None
    no_progress_steps: int = 0
    last_tool: str | None = None
    last_element_id: str | None = None


@dataclass(frozen=True)
class WorkerDeps:
    tool_context: semantic.ToolContext
    metrics: MetricsRecorder
    step: int
    overall_goal: str
    worker_goal: str
    recent_memory: tuple[str, ...]
    no_progress_steps: int
    stuck_threshold: int
    decoy_guard_enabled: bool
    prior_tool: str | None
    prior_element_id: str | None


def _setup_logging(log_dir: str, *, level: str = "INFO") -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level.upper())
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    log_path = str(Path(log_dir) / "agent.log")
    has_file = any(
        isinstance(handler, logging.FileHandler) and getattr(handler, "baseFilename", None) == log_path
        for handler in root.handlers
    )
    if not has_file:
        file_handler = logging.FileHandler(log_path)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    has_stream = any(
        isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler)
        for handler in root.handlers
    )
    if not has_stream:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)


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
    settings["openrouter_usage"] = {"include": True}
    if config.reasoning_effort and config.reasoning_effort != "none":
        settings["openrouter_reasoning"] = {"effort": config.reasoning_effort}
    return settings


def _format_memory(memory: list[str], *, limit: int = 10) -> str:
    if not memory:
        return "None."
    recent = memory[-limit:]
    lines = [f"{idx + 1}. {item}" for idx, item in enumerate(recent)]
    return "\n".join(lines)

def _snapshot_digest(url: str, title: str | None, elements: list[ElementSnapshot]) -> str:
    ids = [element.stable_id for element in elements[:80]]
    material = "\n".join([url, title or "", *ids])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def build_orchestrator_agent(model: OpenRouterModel, *, model_settings: dict[str, Any]) -> Agent[None, OrchestratorDecision]:
    return Agent(
        model,
        output_type=OrchestratorDecision,
        system_prompt=(SYSTEM_PROMPT, ORCHESTRATOR_PROMPT),
        model_settings=model_settings,
        retries=1,
    )

def build_click_guard_agent(model: OpenRouterModel, *, model_settings: dict[str, Any]) -> Agent[None, ClickGuardDecision]:
    return Agent(
        model,
        output_type=ClickGuardDecision,
        system_prompt=(SYSTEM_PROMPT, CLICK_GUARD_PROMPT),
        model_settings=model_settings,
        retries=1,
    )


def build_browser_worker_agent(
    model: OpenRouterModel, *, model_settings: dict[str, Any]
) -> Agent[WorkerDeps, StepOutput]:
    click_guard = build_click_guard_agent(model, model_settings=model_settings)
    agent: Agent[WorkerDeps, StepOutput] = Agent(
        model,
        deps_type=WorkerDeps,
        output_type=StepOutput,
        system_prompt=SYSTEM_PROMPT,
        model_settings=model_settings,
        retries=1,
    )

    def _format_element_brief(element: ElementSnapshot) -> str:
        role = (element.role or "").strip()
        name = (element.name or "").strip()
        text = (element.text or "").strip()
        tag = (element.node_name or "").strip()
        attrs = element.attributes or {}
        important_attrs = {
            key: attrs.get(key)
            for key in [
                "id",
                "name",
                "type",
                "placeholder",
                "aria-label",
                "title",
                "alt",
                "href",
                "value",
            ]
            if attrs.get(key)
        }
        attr_str = (
            " ".join(f'{key}="{value}"' for key, value in important_attrs.items())
            if important_attrs
            else ""
        )
        label_parts = [part for part in [role, name, text, tag] if part]
        label = " | ".join(label_parts) if label_parts else "element"
        if attr_str:
            label = f"{label} ({attr_str})"
        bbox_hint = ""
        if element.bounding_box:
            x, y, w, h = element.bounding_box
            bbox_hint = f" bbox={int(round(x))},{int(round(y))},{int(round(w))},{int(round(h))}"
        frame_hint = ""
        if element.frame_name or element.frame_url:
            frame_name = element.frame_name or ""
            frame_url = element.frame_url or ""
            frame_hint = f" frame={frame_name or frame_url}".strip()
        hints = f"{bbox_hint}{(' ' + frame_hint) if frame_hint else ''}"
        return f"{element.stable_id}: {label}{hints}"

    @agent.tool(name="click_element")
    async def click_element(ctx: RunContext[WorkerDeps], element_id: str) -> ToolExecutionResult:
        start = time.perf_counter()
        if (
            ctx.deps.decoy_guard_enabled
            and ctx.deps.no_progress_steps >= ctx.deps.stuck_threshold
            and ctx.deps.prior_tool == "click_element"
            and ctx.deps.prior_element_id == element_id
        ):
            page_url = getattr(ctx.deps.tool_context.page, "url", "") or ""
            all_elements = list(ctx.deps.tool_context.element_index.elements.values())
            candidates = search_elements(all_elements, query=ctx.deps.worker_goal, limit=8, page_url=page_url)
            candidate_ids = {element.stable_id for element in candidates}

            chosen = ctx.deps.tool_context.element_index.elements.get(element_id)
            chosen_line = _format_element_brief(chosen) if chosen else f"{element_id}: (unknown)"
            candidate_lines = "\n".join(_format_element_brief(element) for element in candidates) if candidates else "None."
            memory_text = "\n".join(f"- {item}" for item in ctx.deps.recent_memory) if ctx.deps.recent_memory else "None."
            guard_prompt = (
                f"Overall goal: {ctx.deps.overall_goal}\n"
                f"Worker goal: {ctx.deps.worker_goal}\n"
                f"Progress: no_progress_steps={ctx.deps.no_progress_steps} "
                f"(stuck_threshold={ctx.deps.stuck_threshold}) "
                f"prior_tool={ctx.deps.prior_tool or 'None'} "
                f"prior_element_id={ctx.deps.prior_element_id or 'None'}\n\n"
                f"Recent memory:\n{memory_text}\n\n"
                f"Chosen click:\n{chosen_line}\n\n"
                f"Alternative candidates:\n{candidate_lines}\n"
            )
            guard_result = await click_guard.run(guard_prompt)
            decision = guard_result.output
            alternatives = [
                alt for alt in (decision.alternatives or []) if alt in candidate_ids and alt != element_id
            ][:5]
            if not decision.allow and alternatives:
                ctx.deps.tool_context.last_tool = "click_element"
                ctx.deps.tool_context.last_element_id = element_id
                duration_ms = int((time.perf_counter() - start) * 1000)
                logger.debug(
                    "tool=%s step=%s ok=%s element_id=%s duration_ms=%s",
                    "click_element",
                    ctx.deps.step,
                    False,
                    element_id,
                    duration_ms,
                )
                ctx.deps.metrics.emit(
                    "tool_call",
                    step=ctx.deps.step,
                    tool="click_element",
                    ok=False,
                    duration_ms=duration_ms,
                    element_id=element_id,
                )
                alt_str = ", ".join(alternatives)
                return ToolExecutionResult(
                    ok=False,
                    message=f"Click blocked by decoy guard: {decision.rationale}. Try: {alt_str}",
                )

        result = await semantic.click_element(element_id, ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.debug(
            "tool=%s step=%s ok=%s element_id=%s duration_ms=%s",
            "click_element",
            ctx.deps.step,
            result.ok,
            element_id,
            duration_ms,
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="click_element",
            ok=result.ok,
            duration_ms=duration_ms,
            element_id=element_id,
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="find_elements")
    async def find_elements(ctx: RunContext[WorkerDeps], query: str, limit: int = 8) -> ToolExecutionResult:
        start = time.perf_counter()
        limit = max(1, min(int(limit), 20))
        page_url = getattr(ctx.deps.tool_context.page, "url", "") or ""
        elements = list(ctx.deps.tool_context.element_index.elements.values())
        matches = search_elements(elements, query=query, limit=limit, page_url=page_url)
        message = "No matches."
        if matches:
            message = "\n".join(_format_element_brief(element) for element in matches)
        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.debug(
            "tool=%s step=%s ok=%s query_len=%s limit=%s duration_ms=%s",
            "find_elements",
            ctx.deps.step,
            True,
            len(query or ""),
            limit,
            duration_ms,
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="find_elements",
            ok=True,
            duration_ms=duration_ms,
            query_len=len(query or ""),
            limit=limit,
        )
        return ToolExecutionResult(ok=True, message=message)

    @agent.tool(name="type_text")
    async def type_text(ctx: RunContext[WorkerDeps], element_id: str, text: str) -> ToolExecutionResult:
        start = time.perf_counter()
        result = await semantic.type_text(element_id, text, ctx.deps.tool_context)
        logger.debug(
            "tool=%s step=%s ok=%s element_id=%s text_len=%s duration_ms=%s",
            "type_text",
            ctx.deps.step,
            result.ok,
            element_id,
            len(text),
            int((time.perf_counter() - start) * 1000),
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="type_text",
            ok=result.ok,
            duration_ms=int((time.perf_counter() - start) * 1000),
            element_id=element_id,
            text_len=len(text),
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="drag_and_drop")
    async def drag_and_drop(ctx: RunContext[WorkerDeps], source_id: str, target_id: str) -> ToolExecutionResult:
        start = time.perf_counter()
        result = await semantic.drag_and_drop(source_id, target_id, ctx.deps.tool_context)
        logger.debug(
            "tool=%s step=%s ok=%s source_id=%s target_id=%s duration_ms=%s",
            "drag_and_drop",
            ctx.deps.step,
            result.ok,
            source_id,
            target_id,
            int((time.perf_counter() - start) * 1000),
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="drag_and_drop",
            ok=result.ok,
            duration_ms=int((time.perf_counter() - start) * 1000),
            source_id=source_id,
            target_id=target_id,
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="select_all")
    async def select_all(ctx: RunContext[WorkerDeps]) -> ToolExecutionResult:
        start = time.perf_counter()
        result = await semantic.select_all(ctx.deps.tool_context)
        logger.debug(
            "tool=%s step=%s ok=%s duration_ms=%s",
            "select_all",
            ctx.deps.step,
            result.ok,
            int((time.perf_counter() - start) * 1000),
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="select_all",
            ok=result.ok,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="copy_selection")
    async def copy_selection(ctx: RunContext[WorkerDeps]) -> ToolExecutionResult:
        start = time.perf_counter()
        result = await semantic.copy_selection(ctx.deps.tool_context)
        logger.debug(
            "tool=%s step=%s ok=%s duration_ms=%s",
            "copy_selection",
            ctx.deps.step,
            result.ok,
            int((time.perf_counter() - start) * 1000),
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="copy_selection",
            ok=result.ok,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="paste")
    async def paste(ctx: RunContext[WorkerDeps]) -> ToolExecutionResult:
        start = time.perf_counter()
        result = await semantic.paste(ctx.deps.tool_context)
        logger.debug(
            "tool=%s step=%s ok=%s duration_ms=%s",
            "paste",
            ctx.deps.step,
            result.ok,
            int((time.perf_counter() - start) * 1000),
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="paste",
            ok=result.ok,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="read_element_text")
    async def read_element_text(ctx: RunContext[WorkerDeps], element_id: str) -> ToolExecutionResult:
        start = time.perf_counter()
        result = await semantic.read_element_text(element_id, ctx.deps.tool_context)
        logger.debug(
            "tool=%s step=%s ok=%s element_id=%s duration_ms=%s",
            "read_element_text",
            ctx.deps.step,
            result.ok,
            element_id,
            int((time.perf_counter() - start) * 1000),
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="read_element_text",
            ok=result.ok,
            duration_ms=int((time.perf_counter() - start) * 1000),
            element_id=element_id,
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="switch_to_iframe")
    async def switch_to_iframe(ctx: RunContext[WorkerDeps], iframe_id: str) -> ToolExecutionResult:
        start = time.perf_counter()
        result = await semantic.switch_to_iframe(iframe_id, ctx.deps.tool_context)
        logger.debug(
            "tool=%s step=%s ok=%s iframe_id=%s duration_ms=%s",
            "switch_to_iframe",
            ctx.deps.step,
            result.ok,
            iframe_id,
            int((time.perf_counter() - start) * 1000),
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="switch_to_iframe",
            ok=result.ok,
            duration_ms=int((time.perf_counter() - start) * 1000),
            iframe_id=iframe_id,
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="switch_to_main_frame")
    async def switch_to_main_frame(ctx: RunContext[WorkerDeps]) -> ToolExecutionResult:
        start = time.perf_counter()
        result = await semantic.switch_to_main_frame(ctx.deps.tool_context)
        logger.debug(
            "tool=%s step=%s ok=%s duration_ms=%s",
            "switch_to_main_frame",
            ctx.deps.step,
            result.ok,
            int((time.perf_counter() - start) * 1000),
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="switch_to_main_frame",
            ok=result.ok,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="navigate_to")
    async def navigate_to(ctx: RunContext[WorkerDeps], url: str) -> ToolExecutionResult:
        start = time.perf_counter()
        result = await semantic.navigate_to(url, ctx.deps.tool_context)
        logger.debug(
            "tool=%s step=%s ok=%s url=%s duration_ms=%s",
            "navigate_to",
            ctx.deps.step,
            result.ok,
            url,
            int((time.perf_counter() - start) * 1000),
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="navigate_to",
            ok=result.ok,
            duration_ms=int((time.perf_counter() - start) * 1000),
            url=url,
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="take_screenshot")
    async def take_screenshot(ctx: RunContext[WorkerDeps]) -> ToolExecutionResult:
        start = time.perf_counter()
        result = await semantic.take_screenshot(ctx.deps.tool_context)
        logger.debug(
            "tool=%s step=%s ok=%s duration_ms=%s",
            "take_screenshot",
            ctx.deps.step,
            result.ok,
            int((time.perf_counter() - start) * 1000),
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="take_screenshot",
            ok=result.ok,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="execute_js")
    async def execute_js(ctx: RunContext[WorkerDeps], code: str) -> ToolExecutionResult:
        start = time.perf_counter()
        result = await semantic.execute_js(code, ctx.deps.tool_context)
        logger.debug(
            "tool=%s step=%s ok=%s code_len=%s duration_ms=%s",
            "execute_js",
            ctx.deps.step,
            result.ok,
            len(code),
            int((time.perf_counter() - start) * 1000),
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="execute_js",
            ok=result.ok,
            duration_ms=int((time.perf_counter() - start) * 1000),
            code_len=len(code),
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="press_key_combination")
    async def press_key_combination(ctx: RunContext[WorkerDeps], keys: list[str]) -> ToolExecutionResult:
        start = time.perf_counter()
        result = await semantic.press_key_combination(keys, ctx.deps.tool_context)
        logger.debug(
            "tool=%s step=%s ok=%s keys=%s duration_ms=%s",
            "press_key_combination",
            ctx.deps.step,
            result.ok,
            "+".join(keys),
            int((time.perf_counter() - start) * 1000),
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="press_key_combination",
            ok=result.ok,
            duration_ms=int((time.perf_counter() - start) * 1000),
            keys=keys,
        )
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

        run_id = new_run_id()
        _setup_logging(self.agent_config.log_dir, level=self.agent_config.log_level)
        metrics = MetricsRecorder(
            log_dir=self.agent_config.log_dir,
            run_id=run_id,
            enabled=self.agent_config.metrics_enabled,
        )
        run_started = time.perf_counter()
        total_input_tokens = 0
        total_output_tokens = 0
        total_cost_usd = 0.0
        metrics.emit(
            "run_start",
            target_url=self.agent_config.target_url,
            goal=self.agent_config.goal,
            max_steps=self.agent_config.max_steps,
            model=self.llm_config.model,
        )
        logger.info(
            "Run start run_id=%s url=%s max_steps=%s model=%s",
            run_id,
            self.agent_config.target_url,
            self.agent_config.max_steps,
            self.llm_config.model,
        )

        model = _build_openrouter_model(self.llm_config)
        model_settings = _model_settings(self.llm_config)
        orchestrator = build_orchestrator_agent(model, model_settings=model_settings)
        browser_worker = build_browser_worker_agent(model, model_settings=model_settings)

        session = await launch_browser(self.browser_config)
        try:
            await session.page.goto(self.agent_config.target_url)
            for step in range(self.agent_config.max_steps):
                self.state.step = step + 1
                step_started = time.perf_counter()
                snapshot_started = time.perf_counter()
                snapshot = await capture_snapshot(session.page, session.cdp_session)
                snapshot_duration_ms = int((time.perf_counter() - snapshot_started) * 1000)
                metrics.emit(
                    "snapshot",
                    step=self.state.step,
                    duration_ms=snapshot_duration_ms,
                    url=snapshot.url,
                    title=snapshot.title,
                    elements=len(snapshot.elements),
                )
                logger.info(
                    "Step %s snapshot duration_ms=%s elements=%s url=%s",
                    self.state.step,
                    snapshot_duration_ms,
                    len(snapshot.elements),
                    snapshot.url,
                )
                snapshot_digest = _snapshot_digest(snapshot.url, snapshot.title, list(snapshot.elements))
                if self.state.last_snapshot_digest == snapshot_digest:
                    self.state.no_progress_steps += 1
                else:
                    self.state.no_progress_steps = 0
                self.state.last_snapshot_digest = snapshot_digest

                element_index = build_element_index(snapshot)
                tool_context = semantic.build_tool_context(
                    session,
                    element_index,
                    active_frame_id=self.state.active_frame_id,
                )
                snapshot_text_orchestrator = format_snapshot_for_llm(
                    snapshot,
                    max_elements=self.agent_config.max_elements,
                    query=self.agent_config.goal,
                )

                memory_text = _format_memory(self.state.memory, limit=self.agent_config.memory_steps)
                progress_text = (
                    "Progress: "
                    f"no_progress_steps={self.state.no_progress_steps} "
                    f"(stuck_threshold={self.agent_config.stuck_threshold}) "
                    f"last_tool={self.state.last_tool or 'None'} "
                    f"last_element_id={self.state.last_element_id or 'None'}"
                )
                stuck_hint = ""
                if self.state.no_progress_steps >= self.agent_config.stuck_threshold:
                    stuck_hint = (
                        "\n\nSTUCK RECOVERY: The page snapshot appears unchanged for multiple steps. "
                        "Do NOT repeat the previous action; instead, explore alternatives by reading element text, "
                        "shortlisting candidates (find_elements), switching frames, navigating, or using a different "
                        "candidate with the same label but a different bbox/frame."
                    )
                orchestrator_prompt = (
                    f"Overall goal: {self.agent_config.goal}\n\n"
                    f"{progress_text}\n\n"
                    f"Memory (recent):\n{memory_text}\n\n"
                    f"Page snapshot:\n{snapshot_text_orchestrator}\n"
                    f"{stuck_hint}"
                )
                orchestrator_started = time.perf_counter()
                decision_result = await orchestrator.run(orchestrator_prompt)
                orchestrator_duration_ms = int((time.perf_counter() - orchestrator_started) * 1000)
                orchestrator_usage = usage_stats_from_result(decision_result)
                orchestrator_cost = cost_stats_from_result(decision_result)
                total_input_tokens += orchestrator_usage.input_tokens
                total_output_tokens += orchestrator_usage.output_tokens
                if orchestrator_cost:
                    total_cost_usd += orchestrator_cost.cost_usd
                metrics.emit(
                    "agent_call",
                    step=self.state.step,
                    agent="orchestrator",
                    duration_ms=orchestrator_duration_ms,
                    input_tokens=orchestrator_usage.input_tokens,
                    output_tokens=orchestrator_usage.output_tokens,
                    requests=orchestrator_usage.requests,
                    tool_calls=orchestrator_usage.tool_calls,
                    cost_usd=(orchestrator_cost.cost_usd if orchestrator_cost else None),
                    upstream_inference_cost_usd=(
                        orchestrator_cost.upstream_inference_cost_usd if orchestrator_cost else None
                    ),
                )
                decision = decision_result.output
                logger.info(
                    "Step %s orchestrator duration_ms=%s in_tokens=%s out_tokens=%s cost_usd=%s decision=%s",
                    self.state.step,
                    orchestrator_duration_ms,
                    orchestrator_usage.input_tokens,
                    orchestrator_usage.output_tokens,
                    (orchestrator_cost.cost_usd if orchestrator_cost else None),
                    decision.worker_goal,
                )
                if decision.done:
                    logger.info("Done (orchestrator): %s", decision.rationale or "")
                    metrics.emit(
                        "step_end",
                        step=self.state.step,
                        done=True,
                        duration_ms=int((time.perf_counter() - step_started) * 1000),
                    )
                    break

                snapshot_text_worker = format_snapshot_for_llm(
                    snapshot,
                    max_elements=self.agent_config.max_elements,
                    query=decision.worker_goal,
                )
                deps = WorkerDeps(
                    tool_context=tool_context,
                    metrics=metrics,
                    step=self.state.step,
                    overall_goal=self.agent_config.goal,
                    worker_goal=decision.worker_goal,
                    recent_memory=tuple(self.state.memory[-self.agent_config.memory_steps :]),
                    no_progress_steps=self.state.no_progress_steps,
                    stuck_threshold=self.agent_config.stuck_threshold,
                    decoy_guard_enabled=self.agent_config.decoy_guard_enabled,
                    prior_tool=self.state.last_tool,
                    prior_element_id=self.state.last_element_id,
                )
                worker_prompt = (
                    STEP_PROMPT.format(goal=decision.worker_goal)
                    + "\n\n"
                    + f"Overall goal: {self.agent_config.goal}\n\n"
                    + f"{progress_text}\n\n"
                    + f"Memory (recent):\n{memory_text}\n\n"
                    + f"Page snapshot:\n{snapshot_text_worker}\n"
                    + (stuck_hint + "\n" if stuck_hint else "")
                )
                worker_started = time.perf_counter()
                with browser_worker.sequential_tool_calls():
                    worker_result = await browser_worker.run(worker_prompt, deps=deps)
                worker_duration_ms = int((time.perf_counter() - worker_started) * 1000)
                worker_usage = usage_stats_from_result(worker_result)
                worker_cost = cost_stats_from_result(worker_result)
                total_input_tokens += worker_usage.input_tokens
                total_output_tokens += worker_usage.output_tokens
                if worker_cost:
                    total_cost_usd += worker_cost.cost_usd
                metrics.emit(
                    "agent_call",
                    step=self.state.step,
                    agent="browser_worker",
                    duration_ms=worker_duration_ms,
                    input_tokens=worker_usage.input_tokens,
                    output_tokens=worker_usage.output_tokens,
                    requests=worker_usage.requests,
                    tool_calls=worker_usage.tool_calls,
                    cost_usd=(worker_cost.cost_usd if worker_cost else None),
                    upstream_inference_cost_usd=(worker_cost.upstream_inference_cost_usd if worker_cost else None),
                )
                step_output = worker_result.output
                self.state.active_frame_id = tool_context.active_frame_id
                self.state.last_summary = step_output.summary
                self.state.memory.append(step_output.summary)
                self.state.last_tool = tool_context.last_tool
                self.state.last_element_id = tool_context.last_element_id
                logger.info(
                    "Step %s worker duration_ms=%s in_tokens=%s out_tokens=%s cost_usd=%s done=%s summary=%s",
                    self.state.step,
                    worker_duration_ms,
                    worker_usage.input_tokens,
                    worker_usage.output_tokens,
                    (worker_cost.cost_usd if worker_cost else None),
                    step_output.done,
                    step_output.summary,
                )
                metrics.emit(
                    "step_end",
                    step=self.state.step,
                    done=bool(step_output.done),
                    duration_ms=int((time.perf_counter() - step_started) * 1000),
                )
                if step_output.done:
                    logger.info(
                        "Worker reported done=true (delegated goal complete); continuing until orchestrator done=true."
                    )
        finally:
            await close_browser(session)
            run_duration_ms = int((time.perf_counter() - run_started) * 1000)
            metrics.emit("run_end", duration_ms=run_duration_ms)
            write_run_summary(
                log_dir=self.agent_config.log_dir,
                run_id=run_id,
                summary={
                    "duration_ms": run_duration_ms,
                    "steps": self.state.step,
                    "last_summary": self.state.last_summary,
                    "input_tokens": total_input_tokens,
                    "output_tokens": total_output_tokens,
                    "total_tokens": total_input_tokens + total_output_tokens,
                    "cost_usd": total_cost_usd,
                },
            )
            logger.info(
                "Run end run_id=%s duration_ms=%s steps=%s total_tokens=%s cost_usd=%s",
                run_id,
                run_duration_ms,
                self.state.step,
                total_input_tokens + total_output_tokens,
                total_cost_usd,
            )
            metrics.close()


async def run_agent(agent_config: AgentConfig, llm_config: LLMConfig, browser_config: BrowserConfig) -> None:
    agent = BrowserAgent(agent_config, llm_config, browser_config)
    await agent.run()


def run_agent_sync(agent_config: AgentConfig, llm_config: LLMConfig, browser_config: BrowserConfig) -> None:
    asyncio.run(run_agent(agent_config, llm_config, browser_config))
