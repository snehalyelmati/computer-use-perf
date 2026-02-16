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
from src.agent.models.actions import OrchestratorDecision, SnapshotFilterOutput, StepOutput, ToolExecutionResult
from src.agent.prompts.system import FILTER_PROMPT, ORCHESTRATOR_PROMPT, STEP_PROMPT, SYSTEM_PROMPT
from src.agent.tools import semantic

logger = logging.getLogger(__name__)

_LOG_INDENT = "  "

class _ShortNameFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith("src."):
            record.name = record.name.split(".")[-1]
        return True

def _format_phase(step: int, phase: str, *, detail: str | None = None, indent: int = 0) -> str:
    prefix = f"Step {step}"
    spacer = _LOG_INDENT * max(indent, 0)
    message = f"{prefix} {spacer}{phase}"
    if detail:
        message = f"{message} {detail}"
    return message


@dataclass
class AgentState:
    step: int = 0
    active_frame_id: str | None = None
    memory: list[str] = field(default_factory=list)
    last_summary: str | None = None
    last_page_fingerprint: str | None = None
    no_progress_steps: int = 0
    last_tool: str | None = None
    last_element_id: str | None = None
    last_filter_fingerprint: str | None = None
    last_filter_output: SnapshotFilterOutput | None = None
    last_worker_goal: str | None = None


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
    prior_tool: str | None
    prior_element_id: str | None


def _setup_logging(log_dir: str, *, level: str = "INFO") -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    normalized_level = level.upper()
    root.setLevel(normalized_level)
    formatter = logging.Formatter("%(levelname)s %(name)s: %(message)s")
    short_name_filter = _ShortNameFilter()
    log_path = str(Path(log_dir) / "agent.log")
    has_file = any(
        isinstance(handler, logging.FileHandler) and getattr(handler, "baseFilename", None) == log_path
        for handler in root.handlers
    )
    if not has_file:
        file_handler = logging.FileHandler(log_path)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(short_name_filter)
        root.addHandler(file_handler)
    else:
        for handler in root.handlers:
            if isinstance(handler, logging.FileHandler) and getattr(handler, "baseFilename", None) == log_path:
                if not any(isinstance(f, _ShortNameFilter) for f in handler.filters):
                    handler.addFilter(short_name_filter)

    has_stream = any(
        isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler)
        for handler in root.handlers
    )
    if not has_stream:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        stream_handler.addFilter(short_name_filter)
        root.addHandler(stream_handler)
    else:
        for handler in root.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                if not any(isinstance(f, _ShortNameFilter) for f in handler.filters):
                    handler.addFilter(short_name_filter)

    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    if normalized_level == "DEBUG":
        httpx_logger.setLevel(logging.DEBUG)
        httpcore_logger.setLevel(logging.DEBUG)
    else:
        httpx_logger.setLevel(logging.WARNING)
        httpcore_logger.setLevel(logging.WARNING)


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

def _normalize_label(value: str | None) -> str:
    return " ".join((value or "").split()).strip().lower()

def _element_fingerprint_line(element: ElementSnapshot) -> str:
    attrs = element.attributes or {}
    important_attrs = []
    for key in ["id", "name", "type", "placeholder", "aria-label", "title", "alt", "href", "value"]:
        if value := attrs.get(key):
            important_attrs.append(f"{key}={_normalize_label(str(value))}")
    parts = [
        element.stable_id,
        _normalize_label(element.role),
        _normalize_label(element.name),
        _normalize_label(element.text),
        _normalize_label(element.node_name),
        "|".join(important_attrs),
    ]
    return "\t".join(parts)

def _page_fingerprint(snapshot: Any) -> str:
    elements = list(getattr(snapshot, "elements", []) or [])
    elements.sort(key=lambda el: el.stable_id)
    raw_lines = _select_raw_text_lines(list(getattr(snapshot, "raw_text", []) or []), limit=60)
    lines = [snapshot.url or "", snapshot.title or ""]
    for element in elements[:120]:
        lines.append(_element_fingerprint_line(element))
    if raw_lines:
        lines.append("RAW_TEXT:")
        lines.extend(raw_lines)
    material = "\n".join(lines)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()

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
    attr_str = " ".join(f'{key}="{value}"' for key, value in important_attrs.items()) if important_attrs else ""
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
    reason_hint = ""
    if element.interactive_reason and (element.interactive_confidence or 0.0) < 0.55:
        reason_hint = f" reason={element.interactive_reason}"
    viewport_hint = ""
    if element.in_viewport is False:
        viewport_hint = " offscreen"
    hints = f"{bbox_hint}{(' ' + frame_hint) if frame_hint else ''}{reason_hint}{viewport_hint}"
    return f"{element.stable_id}: {label}{hints}"

def _select_raw_text_lines(raw_text: list[str] | tuple[str, ...] | Any, *, limit: int = 120) -> list[str]:
    if not raw_text:
        return []
    seen: set[str] = set()
    candidates: list[tuple[float, int, str]] = []
    for idx, line in enumerate(list(raw_text)[:5000]):
        normalized = " ".join(str(line).split())
        if len(normalized) < 3 or len(normalized) > 220:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        has_digit = any(ch.isdigit() for ch in normalized)
        symbol_count = sum(1 for ch in normalized if ch in ":=/@#_-")
        alpha_count = sum(1 for ch in normalized if ch.isalpha())
        score = 0.0
        score += 2.0 if has_digit else 0.0
        score += min(2.0, float(symbol_count) / 2.0)
        score += min(3.0, float(alpha_count) / 40.0)
        candidates.append((score, idx, normalized))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in candidates[:limit]]

def _snapshot_diff(prev: Any | None, curr: Any) -> tuple[str, list[str]]:
    if not prev:
        return "First snapshot (no prior snapshot to diff).", []
    prev_map = {el.stable_id: el for el in list(getattr(prev, "elements", []) or [])}
    curr_map = {el.stable_id: el for el in list(getattr(curr, "elements", []) or [])}
    new_ids = sorted([sid for sid in curr_map.keys() if sid not in prev_map])
    removed_ids = sorted([sid for sid in prev_map.keys() if sid not in curr_map])
    changed: list[str] = []
    for sid in sorted(set(prev_map.keys()) & set(curr_map.keys())):
        a = prev_map[sid]
        b = curr_map[sid]
        a_key = (_normalize_label(a.role), _normalize_label(a.name), _normalize_label(a.text), _normalize_label(a.node_name))
        b_key = (_normalize_label(b.role), _normalize_label(b.name), _normalize_label(b.text), _normalize_label(b.node_name))
        if a_key != b_key:
            changed.append(sid)
    lines: list[str] = []
    lines.append(f"new_elements={len(new_ids)} changed_labels={len(changed)} removed_elements={len(removed_ids)}")
    detail_ids: list[str] = []
    for sid in new_ids[:8]:
        detail_ids.append(sid)
        lines.append(f"+ {_format_element_brief(curr_map[sid])}")
    for sid in changed[:8]:
        detail_ids.append(sid)
        lines.append(f"~ {_format_element_brief(curr_map[sid])}")
    for sid in removed_ids[:8]:
        detail_ids.append(sid)
        lines.append(f"- {sid}: (removed)")
    return "\n".join(lines), detail_ids


def build_orchestrator_agent(model: OpenRouterModel, *, model_settings: dict[str, Any]) -> Agent[None, OrchestratorDecision]:
    return Agent(
        model,
        output_type=OrchestratorDecision,
        system_prompt=(SYSTEM_PROMPT, ORCHESTRATOR_PROMPT),
        model_settings=model_settings,
        retries=1,
    )

def build_snapshot_filter_agent(
    model: OpenRouterModel, *, model_settings: dict[str, Any]
) -> Agent[None, SnapshotFilterOutput]:
    return Agent(
        model,
        output_type=SnapshotFilterOutput,
        system_prompt=(SYSTEM_PROMPT, FILTER_PROMPT),
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
        start = time.perf_counter()
        result = await semantic.click_element(element_id, ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            _format_phase(
                ctx.deps.step,
                "tool click_element",
                detail=f"ok={result.ok} element_id={element_id} duration_ms={duration_ms}",
                indent=3,
            )
        )
        logger.debug(
            "tool=click_element step=%s ok=%s element_id=%s duration_ms=%s",
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
        logger.info(
            _format_phase(
                ctx.deps.step,
                "tool find_elements",
                detail=f"ok=True query_len={len(query or '')} limit={limit} duration_ms={duration_ms}",
                indent=3,
            )
        )
        logger.debug(
            "tool=find_elements step=%s ok=%s query_len=%s limit=%s duration_ms=%s",
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
        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            _format_phase(
                ctx.deps.step,
                "tool type_text",
                detail=f"ok={result.ok} element_id={element_id} text_len={len(text)} duration_ms={duration_ms}",
                indent=3,
            )
        )
        logger.debug(
            "tool=type_text step=%s ok=%s element_id=%s text_len=%s duration_ms=%s",
            ctx.deps.step,
            result.ok,
            element_id,
            len(text),
            duration_ms,
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="type_text",
            ok=result.ok,
            duration_ms=duration_ms,
            element_id=element_id,
            text_len=len(text),
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="drag_and_drop")
    async def drag_and_drop(ctx: RunContext[WorkerDeps], source_id: str, target_id: str) -> ToolExecutionResult:
        start = time.perf_counter()
        result = await semantic.drag_and_drop(source_id, target_id, ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            _format_phase(
                ctx.deps.step,
                "tool drag_and_drop",
                detail=(
                    f"ok={result.ok} source_id={source_id} target_id={target_id} duration_ms={duration_ms}"
                ),
                indent=3,
            )
        )
        logger.debug(
            "tool=drag_and_drop step=%s ok=%s source_id=%s target_id=%s duration_ms=%s",
            ctx.deps.step,
            result.ok,
            source_id,
            target_id,
            duration_ms,
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="drag_and_drop",
            ok=result.ok,
            duration_ms=duration_ms,
            source_id=source_id,
            target_id=target_id,
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="select_all")
    async def select_all(ctx: RunContext[WorkerDeps]) -> ToolExecutionResult:
        start = time.perf_counter()
        result = await semantic.select_all(ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            _format_phase(
                ctx.deps.step,
                "tool select_all",
                detail=f"ok={result.ok} duration_ms={duration_ms}",
                indent=3,
            )
        )
        logger.debug(
            "tool=select_all step=%s ok=%s duration_ms=%s",
            ctx.deps.step,
            result.ok,
            duration_ms,
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="select_all",
            ok=result.ok,
            duration_ms=duration_ms,
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="copy_selection")
    async def copy_selection(ctx: RunContext[WorkerDeps]) -> ToolExecutionResult:
        start = time.perf_counter()
        result = await semantic.copy_selection(ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            _format_phase(
                ctx.deps.step,
                "tool copy_selection",
                detail=f"ok={result.ok} duration_ms={duration_ms}",
                indent=3,
            )
        )
        logger.debug(
            "tool=copy_selection step=%s ok=%s duration_ms=%s",
            ctx.deps.step,
            result.ok,
            duration_ms,
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="copy_selection",
            ok=result.ok,
            duration_ms=duration_ms,
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="paste")
    async def paste(ctx: RunContext[WorkerDeps]) -> ToolExecutionResult:
        start = time.perf_counter()
        result = await semantic.paste(ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            _format_phase(
                ctx.deps.step,
                "tool paste",
                detail=f"ok={result.ok} duration_ms={duration_ms}",
                indent=3,
            )
        )
        logger.debug(
            "tool=paste step=%s ok=%s duration_ms=%s",
            ctx.deps.step,
            result.ok,
            duration_ms,
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="paste",
            ok=result.ok,
            duration_ms=duration_ms,
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="read_element_text")
    async def read_element_text(ctx: RunContext[WorkerDeps], element_id: str) -> ToolExecutionResult:
        start = time.perf_counter()
        result = await semantic.read_element_text(element_id, ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            _format_phase(
                ctx.deps.step,
                "tool read_element_text",
                detail=f"ok={result.ok} element_id={element_id} duration_ms={duration_ms}",
                indent=3,
            )
        )
        logger.debug(
            "tool=read_element_text step=%s ok=%s element_id=%s duration_ms=%s",
            ctx.deps.step,
            result.ok,
            element_id,
            duration_ms,
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="read_element_text",
            ok=result.ok,
            duration_ms=duration_ms,
            element_id=element_id,
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="scroll")
    async def scroll(ctx: RunContext[WorkerDeps], delta_x: int = 0, delta_y: int = 0) -> ToolExecutionResult:
        start = time.perf_counter()
        result = await semantic.scroll(delta_x, delta_y, ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            _format_phase(
                ctx.deps.step,
                "tool scroll",
                detail=f"ok={result.ok} delta_x={delta_x} delta_y={delta_y} duration_ms={duration_ms}",
                indent=3,
            )
        )
        logger.debug(
            "tool=scroll step=%s ok=%s delta_x=%s delta_y=%s duration_ms=%s",
            ctx.deps.step,
            result.ok,
            delta_x,
            delta_y,
            duration_ms,
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="scroll",
            ok=result.ok,
            duration_ms=duration_ms,
            delta_x=delta_x,
            delta_y=delta_y,
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="switch_to_iframe")
    async def switch_to_iframe(ctx: RunContext[WorkerDeps], iframe_id: str) -> ToolExecutionResult:
        start = time.perf_counter()
        result = await semantic.switch_to_iframe(iframe_id, ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            _format_phase(
                ctx.deps.step,
                "tool switch_to_iframe",
                detail=f"ok={result.ok} iframe_id={iframe_id} duration_ms={duration_ms}",
                indent=3,
            )
        )
        logger.debug(
            "tool=switch_to_iframe step=%s ok=%s iframe_id=%s duration_ms=%s",
            ctx.deps.step,
            result.ok,
            iframe_id,
            duration_ms,
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="switch_to_iframe",
            ok=result.ok,
            duration_ms=duration_ms,
            iframe_id=iframe_id,
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="switch_to_main_frame")
    async def switch_to_main_frame(ctx: RunContext[WorkerDeps]) -> ToolExecutionResult:
        start = time.perf_counter()
        result = await semantic.switch_to_main_frame(ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            _format_phase(
                ctx.deps.step,
                "tool switch_to_main_frame",
                detail=f"ok={result.ok} duration_ms={duration_ms}",
                indent=3,
            )
        )
        logger.debug(
            "tool=switch_to_main_frame step=%s ok=%s duration_ms=%s",
            ctx.deps.step,
            result.ok,
            duration_ms,
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="switch_to_main_frame",
            ok=result.ok,
            duration_ms=duration_ms,
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="navigate_to")
    async def navigate_to(ctx: RunContext[WorkerDeps], url: str) -> ToolExecutionResult:
        start = time.perf_counter()
        result = await semantic.navigate_to(url, ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            _format_phase(
                ctx.deps.step,
                "tool navigate_to",
                detail=f"ok={result.ok} url={url} duration_ms={duration_ms}",
                indent=3,
            )
        )
        logger.debug(
            "tool=navigate_to step=%s ok=%s url=%s duration_ms=%s",
            ctx.deps.step,
            result.ok,
            url,
            duration_ms,
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="navigate_to",
            ok=result.ok,
            duration_ms=duration_ms,
            url=url,
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="take_screenshot")
    async def take_screenshot(ctx: RunContext[WorkerDeps]) -> ToolExecutionResult:
        start = time.perf_counter()
        result = await semantic.take_screenshot(ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            _format_phase(
                ctx.deps.step,
                "tool take_screenshot",
                detail=f"ok={result.ok} duration_ms={duration_ms}",
                indent=3,
            )
        )
        logger.debug(
            "tool=take_screenshot step=%s ok=%s duration_ms=%s",
            ctx.deps.step,
            result.ok,
            duration_ms,
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="take_screenshot",
            ok=result.ok,
            duration_ms=duration_ms,
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="execute_js")
    async def execute_js(ctx: RunContext[WorkerDeps], code: str) -> ToolExecutionResult:
        start = time.perf_counter()
        result = await semantic.execute_js(code, ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            _format_phase(
                ctx.deps.step,
                "tool execute_js",
                detail=f"ok={result.ok} code_len={len(code)} duration_ms={duration_ms}",
                indent=3,
            )
        )
        logger.debug(
            "tool=execute_js step=%s ok=%s code_len=%s duration_ms=%s",
            ctx.deps.step,
            result.ok,
            len(code),
            duration_ms,
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="execute_js",
            ok=result.ok,
            duration_ms=duration_ms,
            code_len=len(code),
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="press_key_combination")
    async def press_key_combination(ctx: RunContext[WorkerDeps], keys: list[str]) -> ToolExecutionResult:
        start = time.perf_counter()
        result = await semantic.press_key_combination(keys, ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            _format_phase(
                ctx.deps.step,
                "tool press_key_combination",
                detail=f"ok={result.ok} keys={'+'.join(keys)} duration_ms={duration_ms}",
                indent=3,
            )
        )
        logger.debug(
            "tool=press_key_combination step=%s ok=%s keys=%s duration_ms=%s",
            ctx.deps.step,
            result.ok,
            "+".join(keys),
            duration_ms,
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="press_key_combination",
            ok=result.ok,
            duration_ms=duration_ms,
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
        snapshot_filter = build_snapshot_filter_agent(model, model_settings=model_settings)
        browser_worker = build_browser_worker_agent(model, model_settings=model_settings)

        session = await launch_browser(self.browser_config)
        try:
            await session.page.goto(self.agent_config.target_url)
            prev_snapshot = None
            stop_reason: str | None = None
            for step in range(self.agent_config.max_steps):
                self.state.step = step + 1
                step_started = time.perf_counter()
                logger.info(
                    _format_phase(
                        self.state.step,
                        "start",
                        detail=(
                            f"no_progress_steps={self.state.no_progress_steps} "
                            f"stuck_threshold={self.agent_config.stuck_threshold}"
                        ),
                    )
                )
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
                if snapshot.diagnostics:
                    for name, duration_ms in (snapshot.diagnostics.durations_ms or {}).items():
                        metrics.emit(
                            "cdp_call",
                            step=self.state.step,
                            name=name,
                            duration_ms=int(duration_ms),
                            **(snapshot.diagnostics.size_hints or {}),
                        )
                logger.info(
                    _format_phase(
                        self.state.step,
                        "snapshot",
                        detail=(
                            f"duration_ms={snapshot_duration_ms} elements={len(snapshot.elements)} "
                            f"url={snapshot.url}"
                        ),
                        indent=1,
                    )
                )
                diff_text, _diff_ids = _snapshot_diff(prev_snapshot, snapshot)
                page_fingerprint = _page_fingerprint(snapshot)
                if self.state.last_page_fingerprint == page_fingerprint:
                    self.state.no_progress_steps += 1
                else:
                    self.state.no_progress_steps = 0
                self.state.last_page_fingerprint = page_fingerprint

                if self.state.no_progress_steps >= self.agent_config.unchanged_abort_threshold:
                    stop_reason = "unchanged_fingerprint_abort"
                    logger.warning(
                        _format_phase(
                            self.state.step,
                            "abort",
                            detail=(
                                "reason=unchanged_fingerprint "
                                f"count={self.state.no_progress_steps} "
                                f"threshold={self.agent_config.unchanged_abort_threshold}"
                            ),
                            indent=1,
                        )
                    )
                    metrics.emit(
                        "step_end",
                        step=self.state.step,
                        done=True,
                        stop_reason=stop_reason,
                        duration_ms=int((time.perf_counter() - step_started) * 1000),
                    )
                    break

                element_index = build_element_index(snapshot)
                tool_context = semantic.build_tool_context(
                    session,
                    element_index,
                    active_frame_id=self.state.active_frame_id,
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

                filter_output = self.state.last_filter_output
                if self.state.last_filter_fingerprint != page_fingerprint or filter_output is None:
                    raw_lines = _select_raw_text_lines(list(snapshot.raw_text), limit=120)
                    page_url = getattr(session.page, "url", "") or ""
                    candidates = search_elements(
                        list(snapshot.elements),
                        query=self.agent_config.goal,
                        limit=min(80, max(20, self.agent_config.max_elements)),
                        page_url=page_url,
                    )
                    candidate_lines = "\n".join(_format_element_brief(el) for el in candidates) if candidates else "None."
                    raw_text_block = "\n".join(raw_lines) if raw_lines else "None."
                    last_summary = self.state.last_summary or "None."
                    last_worker_goal = self.state.last_worker_goal or "None."
                    filter_prompt = (
                        f"Overall goal: {self.agent_config.goal}\n\n"
                        f"{progress_text}\n"
                        f"Last worker goal: {last_worker_goal}\n"
                        f"Last step summary: {last_summary}\n\n"
                        f"Diff since prior snapshot:\n{diff_text}\n\n"
                        f"Candidate interactive elements (stable ids + labels):\n{candidate_lines}\n\n"
                        f"Candidate page text lines:\n{raw_text_block}\n"
                    )

                    logger.debug(
                        "snapshot_filter prompt step=%s chars=%s",
                        self.state.step,
                        len(filter_prompt),
                    )
                    filter_started = time.perf_counter()
                    filter_result = await snapshot_filter.run(filter_prompt)
                    filter_duration_ms = int((time.perf_counter() - filter_started) * 1000)
                    filter_usage = usage_stats_from_result(filter_result)
                    filter_cost = cost_stats_from_result(filter_result)
                    total_input_tokens += filter_usage.input_tokens
                    total_output_tokens += filter_usage.output_tokens
                    if filter_cost:
                        total_cost_usd += filter_cost.cost_usd
                    metrics.emit(
                        "agent_call",
                        step=self.state.step,
                        agent="snapshot_filter",
                        duration_ms=filter_duration_ms,
                        input_tokens=filter_usage.input_tokens,
                        output_tokens=filter_usage.output_tokens,
                        requests=filter_usage.requests,
                        tool_calls=filter_usage.tool_calls,
                        cost_usd=(filter_cost.cost_usd if filter_cost else None),
                        upstream_inference_cost_usd=(filter_cost.upstream_inference_cost_usd if filter_cost else None),
                    )
                    filter_output = filter_result.output
                    valid_ids = set(element_index.elements.keys())
                    priority_ids: list[str] = []
                    for sid in filter_output.priority_element_ids or []:
                        if sid in valid_ids and sid not in priority_ids:
                            priority_ids.append(sid)
                    filter_output = SnapshotFilterOutput(
                        useful_text_lines=list(filter_output.useful_text_lines or []),
                        priority_element_ids=priority_ids,
                        notes=filter_output.notes,
                    )
                    self.state.last_filter_output = filter_output
                    self.state.last_filter_fingerprint = page_fingerprint
                    logger.info(
                        _format_phase(
                            self.state.step,
                            "filter",
                            detail=(
                                f"duration_ms={filter_duration_ms} "
                                f"useful_lines={len(filter_output.useful_text_lines or [])} "
                                f"priority_ids={len(priority_ids)}"
                            ),
                            indent=1,
                        )
                    )

                useful_lines = filter_output.useful_text_lines if filter_output else []
                useful_block = "\n".join(useful_lines) if useful_lines else "None."
                priority_ids = (filter_output.priority_element_ids if filter_output else [])[:20]
                priority_lines = (
                    "\n".join(_format_element_brief(element_index.elements[sid]) for sid in priority_ids if sid in element_index.elements)
                    if priority_ids
                    else "None."
                )

                snapshot_text_orchestrator = format_snapshot_for_llm(
                    snapshot,
                    max_elements=self.agent_config.max_elements,
                    query=self.agent_config.goal,
                    priority_ids=priority_ids,
                )
                orchestrator_prompt = (
                    f"Overall goal: {self.agent_config.goal}\n\n"
                    f"{progress_text}\n\n"
                    f"Filtered useful lines:\n{useful_block}\n\n"
                    f"Diff since prior snapshot:\n{diff_text}\n\n"
                    f"Priority interactive elements:\n{priority_lines}\n\n"
                    f"Memory (recent):\n{memory_text}\n\n"
                    f"Page snapshot:\n{snapshot_text_orchestrator}\n"
                    f"{stuck_hint}"
                )
                logger.debug(
                    "orchestrator prompt step=%s chars=%s",
                    self.state.step,
                    len(orchestrator_prompt),
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
                self.state.last_worker_goal = decision.worker_goal
                logger.info(
                    _format_phase(
                        self.state.step,
                        "orchestrator",
                        detail=(
                            f"duration_ms={orchestrator_duration_ms} "
                            f"worker={decision.worker} "
                            f"goal={decision.worker_goal} "
                            f"done={decision.done} "
                            f"rationale={decision.rationale or 'None'}"
                        ),
                        indent=1,
                    )
                )
                logger.debug(
                    "orchestrator step=%s duration_ms=%s input_tokens=%s output_tokens=%s cost_usd=%s",
                    self.state.step,
                    orchestrator_duration_ms,
                    orchestrator_usage.input_tokens,
                    orchestrator_usage.output_tokens,
                    (orchestrator_cost.cost_usd if orchestrator_cost else None),
                )
                if decision.done:
                    logger.info(
                        _format_phase(
                            self.state.step,
                            "orchestrator done",
                            detail=(decision.rationale or ""),
                            indent=2,
                        )
                    )
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
                    priority_ids=priority_ids,
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
                    prior_tool=self.state.last_tool,
                    prior_element_id=self.state.last_element_id,
                )
                worker_prompt = (
                    STEP_PROMPT.format(goal=decision.worker_goal)
                    + "\n\n"
                    + f"Overall goal: {self.agent_config.goal}\n\n"
                    + f"{progress_text}\n\n"
                    + f"Filtered useful lines:\n{useful_block}\n\n"
                    + f"Diff since prior snapshot:\n{diff_text}\n\n"
                    + f"Priority interactive elements:\n{priority_lines}\n\n"
                    + f"Memory (recent):\n{memory_text}\n\n"
                    + f"Page snapshot:\n{snapshot_text_worker}\n"
                    + (stuck_hint + "\n" if stuck_hint else "")
                )
                logger.debug(
                    "worker prompt step=%s chars=%s",
                    self.state.step,
                    len(worker_prompt),
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
                    _format_phase(
                        self.state.step,
                        "worker",
                        detail=(
                            f"duration_ms={worker_duration_ms} "
                            f"done={step_output.done} summary={step_output.summary}"
                        ),
                        indent=1,
                    )
                )
                logger.debug(
                    "worker step=%s duration_ms=%s input_tokens=%s output_tokens=%s cost_usd=%s",
                    self.state.step,
                    worker_duration_ms,
                    worker_usage.input_tokens,
                    worker_usage.output_tokens,
                    (worker_cost.cost_usd if worker_cost else None),
                )
                metrics.emit(
                    "step_end",
                    step=self.state.step,
                    done=False,
                    worker_done=bool(step_output.done),
                    duration_ms=int((time.perf_counter() - step_started) * 1000),
                )
                if step_output.done:
                    logger.info(
                        _format_phase(
                            self.state.step,
                            "worker done",
                            detail="delegated goal complete; continuing until orchestrator done=true",
                            indent=2,
                        )
                    )
                logger.info(
                    _format_phase(
                        self.state.step,
                        "end",
                        detail=f"duration_ms={int((time.perf_counter() - step_started) * 1000)}",
                    )
                )
                prev_snapshot = snapshot
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
                    "stop_reason": stop_reason,
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
