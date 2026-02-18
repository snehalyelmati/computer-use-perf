"""Multi-agent orchestration loop for the browser agent."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
import hashlib
import logging
import os
from pathlib import Path
import re
import signal
import sys
import time
from collections.abc import Sequence
from typing import Any

from pydantic_ai import Agent, RunContext, ToolDefinition
from pydantic_ai.models import Model
from pydantic_ai.models.cerebras import CerebrasModel
from pydantic_ai.models.groq import GroqModel
from pydantic_ai.models.openrouter import OpenRouterModel

from src.agent.core.resilient_model import ResilientModel

from src.agent.browser.session import close_browser, launch_browser
from src.agent.capture.page_saver import PageSaver
from src.agent.config import AgentConfig, BrowserConfig, LLMConfig
from src.agent.context.handlers import cleanup_handler_attributes, extract_handlers
from src.agent.context.snapshot import (
    ElementSnapshot,
    PageSnapshot,
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
from src.agent.models.actions import OracleAdvice, OrchestratorDecision, SnapshotFilterOutput, StepOutput, ToolExecutionResult
from src.agent.prompts.system import FILTER_PROMPT, ORACLE_PROMPT, ORCHESTRATOR_PROMPT, STEP_PROMPT, SYSTEM_PROMPT
from src.agent.tools import semantic

logger = logging.getLogger(__name__)

_LOG_INDENT = "  "
_STEP_SEPARATOR = "─" * 64


class _ShortNameFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith("src."):
            record.name = record.name.split(".")[-1]
        return True


class _ColorFormatter(logging.Formatter):
    """Formatter that applies ANSI color codes to log messages for terminal output."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"

    # Pattern to match tool status symbols
    _TOOL_OK_PATTERN = re.compile(r"^(\s*)(✓)")
    _TOOL_FAIL_PATTERN = re.compile(r"^(\s*)(✗)")
    # Pattern to match element IDs like (el_abc123)
    _ELEMENT_ID_PATTERN = re.compile(r"\((el_[a-f0-9]+)\)")
    # Pattern to match durations like 123ms
    _DURATION_PATTERN = re.compile(r"\b(\d+ms)\b")
    # Pattern to match done=True or done=False
    _DONE_TRUE_PATTERN = re.compile(r"\bdone=True\b")
    _DONE_FALSE_PATTERN = re.compile(r"\bdone=False\b")
    # Pattern for step headers
    _STEP_HEADER_PATTERN = re.compile(r"^(Step \d+) (start|end)")
    # Pattern for warning indicators
    _WARNING_PATTERN = re.compile(r"\b(abort|STUCK|retry|unchanged|warning)\b", re.IGNORECASE)
    # Pattern for step separator line
    _SEPARATOR_PATTERN = re.compile(r"^(─{20,})$")

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        message = self._apply_colors(message)
        return message

    def _apply_colors(self, message: str) -> str:
        # Tool success (green checkmark)
        message = self._TOOL_OK_PATTERN.sub(
            rf"\1{self.GREEN}✓{self.RESET}", message
        )
        # Tool failure (red X)
        message = self._TOOL_FAIL_PATTERN.sub(
            rf"\1{self.RED}✗{self.RESET}", message
        )
        # Element IDs (cyan)
        message = self._ELEMENT_ID_PATTERN.sub(
            rf"({self.CYAN}\1{self.RESET})", message
        )
        # Durations (dim)
        message = self._DURATION_PATTERN.sub(
            rf"{self.DIM}\1{self.RESET}", message
        )
        # done=True (green)
        message = self._DONE_TRUE_PATTERN.sub(
            rf"{self.GREEN}done=True{self.RESET}", message
        )
        # Step headers (bold)
        message = self._STEP_HEADER_PATTERN.sub(
            rf"{self.BOLD}\1 \2{self.RESET}", message
        )
        # Warnings (yellow)
        message = self._WARNING_PATTERN.sub(
            rf"{self.YELLOW}\1{self.RESET}", message
        )
        # Step separator (dim)
        message = self._SEPARATOR_PATTERN.sub(
            rf"{self.DIM}\1{self.RESET}", message
        )
        return message


# Global flag to track if color logging is enabled
_color_enabled = True


def _get_element_label(element_index: Any, element_id: str) -> str:
    """Get a brief label for an element from the element index."""
    if not element_index or not hasattr(element_index, "elements"):
        return ""
    element = element_index.elements.get(element_id)
    if not element:
        return ""
    # Build a concise label from role, name, or text
    parts = []
    if element.role:
        parts.append(element.role.strip())
    if element.name:
        name = element.name.strip()
        if len(name) > 30:
            name = name[:27] + "..."
        parts.append(f'"{name}"')
    elif element.text:
        text = element.text.strip()
        if len(text) > 30:
            text = text[:27] + "..."
        parts.append(f'"{text}"')
    return " ".join(parts) if parts else ""


def _log_tool_header_if_needed(tracker: ToolCallTracker | None) -> None:
    """Log the 'tools:' header on the first tool call of a step."""
    if tracker and not tracker.first_tool_logged:
        logger.info("    tools:")
        tracker.first_tool_logged = True


def _compact_feedback(message: str, base_prefix: str) -> str | None:
    """Extract a compact verification suffix from an enriched tool result message.

    Returns a short string like ``'→ no DOM changes'`` or ``None`` if no
    verification data is present.
    """
    # Verification is appended after the base message with ". " separators.
    idx = message.find(". ", len(base_prefix) - 5) if base_prefix else message.find(". ")
    if idx == -1:
        return None
    tail = message[idx + 2:]
    if not tail:
        return None

    parts: list[str] = []
    for segment in tail.split(". "):
        seg = segment.strip()
        if not seg:
            continue
        # Shorten common prefixes for compactness
        if seg.startswith("Scroll position changed by "):
            seg = seg.replace("Scroll position changed by ", "moved ")
        elif seg.startswith("Page navigated to: "):
            seg = seg.replace("Page navigated to: ", "nav→")
        elif seg.startswith("Attribute changes: "):
            seg = seg.replace("Attribute changes: ", "attr: ")
        elif seg.startswith("New text appeared: "):
            seg = seg.replace("New text appeared: ", "text+: ")
        elif seg.startswith("Text removed: "):
            seg = seg.replace("Text removed: ", "text-: ")
        elif seg.startswith("Page title: "):
            seg = seg.replace("Page title: ", "title: ")
        elif seg.startswith("No visible DOM changes detected"):
            seg = "no DOM changes"
        elif seg.startswith("Current value: "):
            seg = seg.replace("Current value: ", "val=")
        elif seg.startswith("WARNING: scroll position did not change"):
            seg = "AT BOUNDARY"
        parts.append(seg)

    result = "; ".join(parts)
    # Cap at 120 chars to keep log lines scannable
    if len(result) > 120:
        result = result[:117] + "..."
    return f"→ {result}"


def _format_tool_log(
    tool_name: str,
    ok: bool,
    duration_ms: int,
    *,
    element_id: str | None = None,
    element_label: str | None = None,
    extra: str | None = None,
    feedback: str | None = None,
) -> str:
    """Format a tool call log line with status symbol and details."""
    symbol = "✓" if ok else "✗"
    parts = [f"      {symbol} {tool_name}"]
    if element_label:
        parts.append(f' {element_label}')
    if element_id:
        parts.append(f" ({element_id})")
    parts.append(f" {duration_ms}ms")
    if extra:
        parts.append(f" - {extra}")
    if feedback:
        parts.append(f" {feedback}")
    return "".join(parts)


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
    step_trace: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ToolCallTracker:
    """Track tool calls within a step for logging purposes."""
    first_tool_logged: bool = False


@dataclass(frozen=True)
class WorkerDeps:
    tool_context: semantic.ToolContext
    metrics: MetricsRecorder
    step: int
    tool_tracker: ToolCallTracker | None = None
    allowed_tools: frozenset[str] | None = None


DEFAULT_WORKER_TOOLS: frozenset[str] = frozenset({
    "click_element",
    "hover_element",
    "type_text",
    "drag_and_drop",
    "draw",
    "scroll",
    "wait",
    "switch_to_iframe",
    "switch_to_main_frame",
    "press_key_combination",
})


def _setup_logging(log_dir: str, *, level: str = "INFO", color: bool = True) -> None:
    global _color_enabled
    # Auto-disable color if not a TTY
    use_color = color and sys.stdout.isatty()
    _color_enabled = use_color

    Path(log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    normalized_level = level.upper()
    configured_level = getattr(logging, normalized_level, logging.INFO)
    # Root logger must be at DEBUG so debug file handler receives everything
    root.setLevel(logging.DEBUG)
    plain_formatter = logging.Formatter("%(levelname)s %(name)s: %(message)s")
    debug_formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    color_formatter = _ColorFormatter("%(levelname)s %(name)s: %(message)s") if use_color else plain_formatter
    short_name_filter = _ShortNameFilter()

    # ── agent.log: user-configured level ──
    log_path = str(Path(log_dir) / "agent.log")
    has_file = any(
        isinstance(handler, logging.FileHandler) and getattr(handler, "baseFilename", None) == log_path
        for handler in root.handlers
    )
    if not has_file:
        file_handler = logging.FileHandler(log_path)
        file_handler.setFormatter(plain_formatter)
        file_handler.setLevel(configured_level)
        file_handler.addFilter(short_name_filter)
        root.addHandler(file_handler)
    else:
        for handler in root.handlers:
            if isinstance(handler, logging.FileHandler) and getattr(handler, "baseFilename", None) == log_path:
                handler.setLevel(configured_level)
                if not any(isinstance(f, _ShortNameFilter) for f in handler.filters):
                    handler.addFilter(short_name_filter)

    # ── agent_debug.log: always DEBUG ──
    debug_log_path = str(Path(log_dir) / "agent_debug.log")
    has_debug_file = any(
        isinstance(handler, logging.FileHandler) and getattr(handler, "baseFilename", None) == debug_log_path
        for handler in root.handlers
    )
    if not has_debug_file:
        debug_file_handler = logging.FileHandler(debug_log_path)
        debug_file_handler.setFormatter(debug_formatter)
        debug_file_handler.setLevel(logging.DEBUG)
        debug_file_handler.addFilter(short_name_filter)
        root.addHandler(debug_file_handler)

    # ── Console: user-configured level ──
    has_stream = any(
        isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler)
        for handler in root.handlers
    )
    if not has_stream:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(color_formatter)
        stream_handler.setLevel(configured_level)
        stream_handler.addFilter(short_name_filter)
        root.addHandler(stream_handler)
    else:
        for handler in root.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                handler.setFormatter(color_formatter)
                handler.setLevel(configured_level)
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


def _teardown_logging() -> None:
    """Remove and close handlers added by _setup_logging."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        try:
            handler.close()
            root.removeHandler(handler)
        except Exception:
            pass


def _build_model(config: LLMConfig, *, model_override: str | None = None) -> Model:
    import os

    model_name = model_override or config.model
    if config.provider == "cerebras":
        if config.api_key_env != "CEREBRAS_API_KEY":
            if value := os.environ.get(config.api_key_env):
                os.environ.setdefault("CEREBRAS_API_KEY", value)
        model: Model = CerebrasModel(model_name)
    elif config.provider == "groq":
        if config.api_key_env != "GROQ_API_KEY":
            if value := os.environ.get(config.api_key_env):
                os.environ.setdefault("GROQ_API_KEY", value)
        model = GroqModel(model_name)
    else:
        # OpenRouter (default)
        if config.api_key_env != "OPENROUTER_API_KEY":
            if value := os.environ.get(config.api_key_env):
                os.environ.setdefault("OPENROUTER_API_KEY", value)
        model = OpenRouterModel(model_name)

    if config.max_retries > 0:
        model = ResilientModel(model, max_retries=config.max_retries)
    return model


def _model_settings(config: LLMConfig) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "timeout": float(config.timeout_seconds),
        "parallel_tool_calls": False,
        "max_tokens": config.max_tokens,
        "frequency_penalty": 0.5,
        "presence_penalty": 0.3,
    }
    if config.provider == "openrouter":
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

def _format_step_trace(trace: list[dict[str, Any]]) -> str:
    if not trace:
        return "No steps yet."
    lines: list[str] = []
    for entry in trace:
        url_changed = "yes" if entry.get("url_changed") else "no"
        lines.append(
            f"Step {entry['step']}: [{entry.get('url', '')}] goal={entry.get('goal', '')}"
        )
        lines.append(
            f"  Result: {entry.get('summary', '')}"
        )
        lines.append(
            f"  Diff: {entry.get('diff_summary', '')} | url_changed={url_changed}"
        )
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

def _page_fingerprint(snapshot: Any, *, raw_text_limit: int = 200) -> str:
    elements = list(getattr(snapshot, "elements", []) or [])
    elements.sort(key=lambda el: el.stable_id)
    raw_lines = _select_raw_text_lines(
        list(getattr(snapshot, "raw_text", []) or []),
        limit=raw_text_limit,
    )
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

def _select_raw_text_lines(
    raw_text: list[str] | tuple[str, ...] | Any,
    *,
    limit: int = 300,
    scan_cap: int = 20000,
    max_len: int = 800,
    dedupe_prefix_len: int = 240,
    dedupe_suffix_len: int = 120,
) -> list[str]:
    if not raw_text:
        return []
    seen: set[str] = set()
    candidates: list[tuple[float, int, str]] = []
    for idx, line in enumerate(list(raw_text)[:scan_cap]):
        normalized = " ".join(str(line).split())
        if len(normalized) < 3 or len(normalized) > max_len:
            continue
        key = normalized.lower()
        prefix = key[:dedupe_prefix_len] if dedupe_prefix_len > 0 else key
        suffix = key[-dedupe_suffix_len:] if dedupe_suffix_len > 0 else key
        dedupe_key = f"{prefix}::{suffix}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        has_digit = any(ch.isdigit() for ch in normalized)
        symbol_count = sum(1 for ch in normalized if ch in ":=/@#_-")
        alpha_count = sum(1 for ch in normalized if ch.isalpha())
        lowered = normalized.lower()
        instruction_hits = sum(
            1
            for token in ("click", "select", "reveal", "times", "submit", "enter", "press")
            if token in lowered
        )
        score = 0.0
        score += 2.0 if has_digit else 0.0
        score += min(2.0, float(symbol_count) / 2.0)
        score += min(3.0, float(alpha_count) / 40.0)
        score += min(4.0, float(instruction_hits))
        candidates.append((score, idx, normalized))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in candidates[:limit]]

def _snapshot_diff(
    prev: Any | None,
    curr: Any,
    *,
    priority_ids: Sequence[str] | None = None,
    changed_limit: int = 100,
    raw_text_limit: int = 80,
    raw_text_detail_limit: int = 8,
    raw_text_scan_cap: int = 20000,
    raw_text_line_max_len: int = 800,
    raw_text_dedupe_prefix_len: int = 240,
    raw_text_dedupe_suffix_len: int = 120,
) -> tuple[str, list[str]]:
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
    # Sort changed elements: priority IDs first (in priority order), then the rest capped
    if priority_ids:
        prio_set = set(priority_ids)
        prio_order = {sid: idx for idx, sid in enumerate(priority_ids)}
        prio_changed = sorted([sid for sid in changed if sid in prio_set], key=lambda s: prio_order[s])
        rest_changed = [sid for sid in changed if sid not in prio_set][:changed_limit]
        changed = prio_changed + rest_changed
    else:
        changed = changed[:changed_limit]
    lines: list[str] = []
    lines.append(f"new_elements={len(new_ids)} changed_labels={len(changed)} removed_elements={len(removed_ids)}")
    detail_ids: list[str] = []
    for sid in new_ids[:8]:
        detail_ids.append(sid)
        lines.append(f"+ {_format_element_brief(curr_map[sid])}")
    for sid in changed:
        detail_ids.append(sid)
        lines.append(f"~ {_format_element_brief(curr_map[sid])}")
    for sid in removed_ids[:8]:
        detail_ids.append(sid)
        lines.append(f"- {sid}: (removed)")
    prev_lines = _select_raw_text_lines(
        list(getattr(prev, "raw_text", []) or []),
        limit=raw_text_limit,
        scan_cap=raw_text_scan_cap,
        max_len=raw_text_line_max_len,
        dedupe_prefix_len=raw_text_dedupe_prefix_len,
        dedupe_suffix_len=raw_text_dedupe_suffix_len,
    )
    curr_lines = _select_raw_text_lines(
        list(getattr(curr, "raw_text", []) or []),
        limit=raw_text_limit,
        scan_cap=raw_text_scan_cap,
        max_len=raw_text_line_max_len,
        dedupe_prefix_len=raw_text_dedupe_prefix_len,
        dedupe_suffix_len=raw_text_dedupe_suffix_len,
    )
    prev_set = {line.lower() for line in prev_lines}
    curr_set = {line.lower() for line in curr_lines}
    added = [line for line in curr_lines if line.lower() not in prev_set]
    removed = [line for line in prev_lines if line.lower() not in curr_set]
    if added or removed:
        lines.append(
            f"text_changes=+{len(added)} -{len(removed)} (showing up to {raw_text_detail_limit} each)"
        )
        for line in added[:raw_text_detail_limit]:
            lines.append(f"+text {line}")
        for line in removed[:raw_text_detail_limit]:
            lines.append(f"-text {line}")
    return "\n".join(lines), detail_ids


def build_orchestrator_agent(model: Model, *, model_settings: dict[str, Any]) -> Agent[None, OrchestratorDecision]:
    return Agent(
        model,
        output_type=OrchestratorDecision,
        system_prompt=(SYSTEM_PROMPT, ORCHESTRATOR_PROMPT),
        model_settings=model_settings,
        retries=1,
    )

def build_snapshot_filter_agent(
    model: Model, *, model_settings: dict[str, Any]
) -> Agent[None, SnapshotFilterOutput]:
    return Agent(
        model,
        output_type=SnapshotFilterOutput,
        system_prompt=FILTER_PROMPT,
        model_settings=model_settings,
        retries=1,
    )


def build_oracle_agent(model: Model, *, model_settings: dict[str, Any]) -> Agent[None, OracleAdvice]:
    return Agent(
        model,
        output_type=OracleAdvice,
        system_prompt=ORACLE_PROMPT,
        model_settings=model_settings,
        retries=1,
    )


def build_browser_worker_agent(
    model: Model, *, model_settings: dict[str, Any]
) -> Agent[WorkerDeps, StepOutput]:
    async def _normalize_strict(
        ctx: RunContext[WorkerDeps], tool_defs: list[ToolDefinition]
    ) -> list[ToolDefinition]:
        return [replace(t, strict=False) for t in tool_defs]

    async def _filter_tools(
        ctx: RunContext[WorkerDeps], tool_defs: list[ToolDefinition]
    ) -> list[ToolDefinition]:
        tool_defs = [replace(t, strict=False) for t in tool_defs]
        if ctx.deps.allowed_tools is None:
            return tool_defs
        return [t for t in tool_defs if t.name in ctx.deps.allowed_tools]

    agent: Agent[WorkerDeps, StepOutput] = Agent(
        model,
        deps_type=WorkerDeps,
        output_type=StepOutput,
        system_prompt=SYSTEM_PROMPT,
        model_settings=model_settings,
        prepare_tools=_filter_tools,
        prepare_output_tools=_normalize_strict,
        retries=1,
    )

    @agent.tool(name="click_element")
    async def click_element(ctx: RunContext[WorkerDeps], element_id: str) -> ToolExecutionResult:
        """Click on an element to activate it, follow a link, or toggle a control. Use element_id from the page snapshot."""
        start = time.perf_counter()
        result = await semantic.click_element(element_id, ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        element_label = _get_element_label(ctx.deps.tool_context.element_index, element_id)
        _log_tool_header_if_needed(ctx.deps.tool_tracker)
        logger.info(
            _format_tool_log(
                "click_element",
                result.ok,
                duration_ms,
                element_id=element_id,
                element_label=element_label,
                extra=None if result.ok else result.message,
                feedback=_compact_feedback(result.message, f"Clicked {element_id}") if result.ok else None,
            )
        )
        logger.debug(
            "tool=click_element step=%s ok=%s element_id=%s duration_ms=%s full_message=%s",
            ctx.deps.step,
            result.ok,
            element_id,
            duration_ms,
            result.message,
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

    @agent.tool(name="hover_element")
    async def hover_element(ctx: RunContext[WorkerDeps], element_id: str, duration_ms: int = 1000) -> ToolExecutionResult:
        """Hover over an element for a duration. Use for revealing tooltips, dropdown menus, or hidden content triggered by mouse hover. Use element_id from the page snapshot."""
        start = time.perf_counter()
        result = await semantic.hover_element(element_id, ctx.deps.tool_context, duration_ms=duration_ms)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        element_label = _get_element_label(ctx.deps.tool_context.element_index, element_id)
        _log_tool_header_if_needed(ctx.deps.tool_tracker)
        logger.info(
            _format_tool_log(
                "hover_element",
                result.ok,
                elapsed_ms,
                element_id=element_id,
                element_label=element_label,
                extra=f"duration={duration_ms}ms" if result.ok else result.message,
                feedback=_compact_feedback(result.message, f"Hovered {element_id}") if result.ok else None,
            )
        )
        logger.debug(
            "tool=hover_element step=%s ok=%s element_id=%s duration_ms=%s elapsed_ms=%s full_message=%s",
            ctx.deps.step,
            result.ok,
            element_id,
            duration_ms,
            elapsed_ms,
            result.message,
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="hover_element",
            ok=result.ok,
            duration_ms=elapsed_ms,
            element_id=element_id,
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="find_elements")
    async def find_elements(ctx: RunContext[WorkerDeps], query: str, limit: int = 8) -> ToolExecutionResult:
        """Search for elements by text, label, or role. Use when the target element is not visible in the current snapshot."""
        start = time.perf_counter()
        limit = max(1, min(int(limit), 20))
        page_url = getattr(ctx.deps.tool_context.page, "url", "") or ""
        elements = list(ctx.deps.tool_context.element_index.elements.values())
        matches = search_elements(elements, query=query, limit=limit, page_url=page_url)
        message = "No matches."
        if matches:
            message = "\n".join(_format_element_brief(element) for element in matches)
        duration_ms = int((time.perf_counter() - start) * 1000)
        query_preview = query[:30] + "..." if len(query) > 30 else query
        _log_tool_header_if_needed(ctx.deps.tool_tracker)
        logger.info(
            _format_tool_log(
                "find_elements",
                True,
                duration_ms,
                extra=f'query="{query_preview}" found={len(matches)}',
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
        """Type text into an input or editable field. Replaces any existing content. Use element_id from the page snapshot."""
        start = time.perf_counter()
        result = await semantic.type_text(element_id, text, ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        element_label = _get_element_label(ctx.deps.tool_context.element_index, element_id)
        text_preview = text[:20] + "..." if len(text) > 20 else text
        _log_tool_header_if_needed(ctx.deps.tool_tracker)
        logger.info(
            _format_tool_log(
                "type_text",
                result.ok,
                duration_ms,
                element_id=element_id,
                element_label=element_label,
                extra=f'text="{text_preview}"' if result.ok else result.message,
                feedback=_compact_feedback(result.message, f"Typed into {element_id}") if result.ok else None,
            )
        )
        logger.debug(
            "tool=type_text step=%s ok=%s element_id=%s text_len=%s duration_ms=%s full_message=%s",
            ctx.deps.step,
            result.ok,
            element_id,
            len(text),
            duration_ms,
            result.message,
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
        """Drag one element onto another. Use for reordering lists, moving cards, adjusting sliders, etc. Use element IDs from the page snapshot."""
        start = time.perf_counter()
        result = await semantic.drag_and_drop(source_id, target_id, ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        source_label = _get_element_label(ctx.deps.tool_context.element_index, source_id)
        target_label = _get_element_label(ctx.deps.tool_context.element_index, target_id)
        _log_tool_header_if_needed(ctx.deps.tool_tracker)
        logger.info(
            _format_tool_log(
                "drag_and_drop",
                result.ok,
                duration_ms,
                extra=f'{source_label or source_id} -> {target_label or target_id}' if result.ok else result.message,
                feedback=_compact_feedback(result.message, f"Dragged {source_id} -> {target_id}") if result.ok else None,
            )
        )
        logger.debug(
            "tool=drag_and_drop step=%s ok=%s source_id=%s target_id=%s duration_ms=%s full_message=%s",
            ctx.deps.step,
            result.ok,
            source_id,
            target_id,
            duration_ms,
            result.message,
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

    @agent.tool(name="draw")
    async def draw(ctx: RunContext[WorkerDeps], element_id: str, path: list[list[float]]) -> ToolExecutionResult:
        """Draw a freeform path on a canvas or drawing surface by moving the mouse through a series of coordinate points with the button held. Points are [x, y] pairs relative to the element's top-left corner. Use element_id from the page snapshot."""
        start = time.perf_counter()
        result = await semantic.draw(element_id, path, ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        element_label = _get_element_label(ctx.deps.tool_context.element_index, element_id)
        _log_tool_header_if_needed(ctx.deps.tool_tracker)
        logger.info(
            _format_tool_log(
                "draw",
                result.ok,
                duration_ms,
                element_id=element_id,
                element_label=element_label,
                extra=f"points={len(path)}" if result.ok else result.message,
                feedback=_compact_feedback(result.message, f"Drew path with {len(path)} points on {element_id}") if result.ok else None,
            )
        )
        logger.debug(
            "tool=draw step=%s ok=%s element_id=%s points=%s duration_ms=%s full_message=%s",
            ctx.deps.step,
            result.ok,
            element_id,
            len(path),
            duration_ms,
            result.message,
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="draw",
            ok=result.ok,
            duration_ms=duration_ms,
            element_id=element_id,
            points_count=len(path),
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="wait")
    async def wait(ctx: RunContext[WorkerDeps], milliseconds: int) -> ToolExecutionResult:
        """Pause execution. Use when the page needs time to load, animate, or settle. Capped at 10 000 ms."""
        start = time.perf_counter()
        result = await semantic.wait(milliseconds, ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        _log_tool_header_if_needed(ctx.deps.tool_tracker)
        logger.info(
            _format_tool_log(
                "wait",
                result.ok,
                duration_ms,
                extra=f"{milliseconds}ms",
            )
        )
        logger.debug(
            "tool=wait step=%s ok=%s requested_ms=%s duration_ms=%s full_message=%s",
            ctx.deps.step,
            result.ok,
            milliseconds,
            duration_ms,
            result.message,
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="wait",
            ok=result.ok,
            duration_ms=duration_ms,
            requested_ms=milliseconds,
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="inspect_element")
    async def inspect_element(ctx: RunContext[WorkerDeps], element_id: str) -> ToolExecutionResult:
        """Read an element's full text content and all HTML attributes. Use when the snapshot shows truncated text or you need attribute values like data-*, aria-*, etc. Use element_id from the page snapshot."""
        start = time.perf_counter()
        result = await semantic.inspect_element(element_id, ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        element_label = _get_element_label(ctx.deps.tool_context.element_index, element_id)
        _log_tool_header_if_needed(ctx.deps.tool_tracker)
        logger.info(
            _format_tool_log(
                "inspect_element",
                result.ok,
                duration_ms,
                element_id=element_id,
                element_label=element_label,
                extra=None if result.ok else result.message,
            )
        )
        logger.debug(
            "tool=inspect_element step=%s ok=%s element_id=%s duration_ms=%s full_message=%s",
            ctx.deps.step,
            result.ok,
            element_id,
            duration_ms,
            result.message,
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="inspect_element",
            ok=result.ok,
            duration_ms=duration_ms,
            element_id=element_id,
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="search_page_attributes")
    async def search_page_attributes(ctx: RunContext[WorkerDeps], query: str) -> ToolExecutionResult:
        """Search every element on the page for attributes whose name or value contains the query string. Use to find hidden data embedded in element attributes anywhere on the page."""
        start = time.perf_counter()
        result = await semantic.search_page_attributes(query, ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        _log_tool_header_if_needed(ctx.deps.tool_tracker)
        query_preview = query[:30] + "..." if len(query) > 30 else query
        logger.info(
            _format_tool_log(
                "search_page_attributes",
                result.ok,
                duration_ms,
                extra=f'query="{query_preview}"',
            )
        )
        logger.debug(
            "tool=search_page_attributes step=%s ok=%s query=%s duration_ms=%s full_message=%s",
            ctx.deps.step,
            result.ok,
            query,
            duration_ms,
            result.message,
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="search_page_attributes",
            ok=result.ok,
            duration_ms=duration_ms,
            query=query,
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="scroll")
    async def scroll(
        ctx: RunContext[WorkerDeps],
        delta_x: int = 0,
        delta_y: int = 0,
        element_id: str | None = None,
    ) -> ToolExecutionResult:
        """Scroll the viewport by a pixel offset, optionally anchored to a target element. Use element_id from the page snapshot."""
        start = time.perf_counter()
        result = await semantic.scroll(
            delta_x,
            delta_y,
            ctx.deps.tool_context,
            element_id=element_id,
        )
        duration_ms = int((time.perf_counter() - start) * 1000)
        direction_parts = []
        if delta_x != 0:
            direction_parts.append(f"dx={delta_x}")
        if delta_y != 0:
            direction_parts.append(f"dy={delta_y}")
        direction = " ".join(direction_parts) if direction_parts else "no movement"
        _log_tool_header_if_needed(ctx.deps.tool_tracker)
        logger.info(
            _format_tool_log(
                "scroll",
                result.ok,
                duration_ms,
                element_id=element_id,
                extra=direction if result.ok else result.message,
                feedback=_compact_feedback(result.message, f"Scrolled dx={delta_x} dy={delta_y}") if result.ok else None,
            )
        )
        logger.debug(
            "tool=scroll step=%s ok=%s delta_x=%s delta_y=%s element_id=%s duration_ms=%s full_message=%s",
            ctx.deps.step,
            result.ok,
            delta_x,
            delta_y,
            element_id,
            duration_ms,
            result.message,
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="scroll",
            ok=result.ok,
            duration_ms=duration_ms,
            delta_x=delta_x,
            delta_y=delta_y,
            element_id=element_id,
        )
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="switch_to_iframe")
    async def switch_to_iframe(ctx: RunContext[WorkerDeps], iframe_id: str) -> ToolExecutionResult:
        """Switch into an iframe to interact with its elements. Required before clicking or typing inside an iframe. Use element_id from the page snapshot."""
        start = time.perf_counter()
        result = await semantic.switch_to_iframe(iframe_id, ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        element_label = _get_element_label(ctx.deps.tool_context.element_index, iframe_id)
        _log_tool_header_if_needed(ctx.deps.tool_tracker)
        logger.info(
            _format_tool_log(
                "switch_to_iframe",
                result.ok,
                duration_ms,
                element_id=iframe_id,
                element_label=element_label,
                extra=None if result.ok else result.message,
            )
        )
        logger.debug(
            "tool=switch_to_iframe step=%s ok=%s iframe_id=%s duration_ms=%s full_message=%s",
            ctx.deps.step,
            result.ok,
            iframe_id,
            duration_ms,
            result.message,
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
        """Leave the current iframe and return to the top-level page."""
        start = time.perf_counter()
        result = await semantic.switch_to_main_frame(ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        _log_tool_header_if_needed(ctx.deps.tool_tracker)
        logger.info(
            _format_tool_log(
                "switch_to_main_frame",
                result.ok,
                duration_ms,
                extra=None if result.ok else result.message,
            )
        )
        logger.debug(
            "tool=switch_to_main_frame step=%s ok=%s duration_ms=%s full_message=%s",
            ctx.deps.step,
            result.ok,
            duration_ms,
            result.message,
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
        """Navigate to a URL. Use for opening new pages, not for following links already on the page."""
        start = time.perf_counter()
        result = await semantic.navigate_to(url, ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        url_preview = url[:50] + "..." if len(url) > 50 else url
        _log_tool_header_if_needed(ctx.deps.tool_tracker)
        logger.info(
            _format_tool_log(
                "navigate_to",
                result.ok,
                duration_ms,
                extra=url_preview if result.ok else result.message,
                feedback=_compact_feedback(result.message, f"Navigated to {url}") if result.ok else None,
            )
        )
        logger.debug(
            "tool=navigate_to step=%s ok=%s url=%s duration_ms=%s full_message=%s",
            ctx.deps.step,
            result.ok,
            url,
            duration_ms,
            result.message,
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
        """Capture a full-page screenshot for visual inspection."""
        start = time.perf_counter()
        result = await semantic.take_screenshot(ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        _log_tool_header_if_needed(ctx.deps.tool_tracker)
        logger.info(
            _format_tool_log(
                "take_screenshot",
                result.ok,
                duration_ms,
                extra=None if result.ok else result.message,
            )
        )
        logger.debug(
            "tool=take_screenshot step=%s ok=%s duration_ms=%s full_message=%s",
            ctx.deps.step,
            result.ok,
            duration_ms,
            result.message,
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
        """Run arbitrary JavaScript in the page. Use only as a last resort when no other tool fits."""
        start = time.perf_counter()
        result = await semantic.execute_js(code, ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        code_preview = code[:30].replace("\n", " ") + "..." if len(code) > 30 else code.replace("\n", " ")
        _log_tool_header_if_needed(ctx.deps.tool_tracker)
        logger.info(
            _format_tool_log(
                "execute_js",
                result.ok,
                duration_ms,
                extra=f'"{code_preview}"' if result.ok else result.message,
            )
        )
        logger.debug(
            "tool=execute_js step=%s ok=%s code_len=%s duration_ms=%s full_message=%s",
            ctx.deps.step,
            result.ok,
            len(code),
            duration_ms,
            result.message,
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
        """Press a keyboard shortcut. Examples: ["Enter"] to submit, ["Control", "C"] to copy, ["Escape"] to dismiss."""
        start = time.perf_counter()
        result = await semantic.press_key_combination(keys, ctx.deps.tool_context)
        duration_ms = int((time.perf_counter() - start) * 1000)
        keys_str = "+".join(keys)
        _log_tool_header_if_needed(ctx.deps.tool_tracker)
        logger.info(
            _format_tool_log(
                "press_key_combination",
                result.ok,
                duration_ms,
                extra=keys_str if result.ok else result.message,
                feedback=_compact_feedback(result.message, f"Pressed {keys_str}") if result.ok else None,
            )
        )
        logger.debug(
            "tool=press_key_combination step=%s ok=%s keys=%s duration_ms=%s full_message=%s",
            ctx.deps.step,
            result.ok,
            keys_str,
            duration_ms,
            result.message,
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
        _setup_logging(
            self.agent_config.log_dir,
            level=self.agent_config.log_level,
            color=self.agent_config.color_logs,
        )
        metrics = MetricsRecorder(
            log_dir=self.agent_config.log_dir,
            run_id=run_id,
            enabled=self.agent_config.metrics_enabled,
        )
        page_saver = PageSaver(self.agent_config.log_dir, run_id) if self.agent_config.save_pages else None
        run_started = time.perf_counter()
        total_input_tokens = 0
        total_output_tokens = 0
        total_cost_usd: float | None = None
        metrics.emit(
            "run_start",
            target_url=self.agent_config.target_url,
            goal=self.agent_config.goal,
            max_steps=self.agent_config.max_steps,
            model=self.llm_config.model,
        )
        logger.info(
            "Run start run_id=%s url=%s max_steps=%s model=%s worker_model=%s filter_model=%s oracle_model=%s",
            run_id,
            self.agent_config.target_url,
            self.agent_config.max_steps,
            self.llm_config.model,
            self.llm_config.worker_model or self.llm_config.model,
            self.llm_config.filter_model or self.llm_config.model,
            self.llm_config.oracle_model or self.llm_config.model,
        )

        model = _build_model(self.llm_config)
        model_settings = _model_settings(self.llm_config)

        worker_model = (
            _build_model(self.llm_config, model_override=self.llm_config.worker_model)
            if self.llm_config.worker_model
            else model
        )
        filter_model = (
            _build_model(self.llm_config, model_override=self.llm_config.filter_model)
            if self.llm_config.filter_model
            else model
        )
        oracle_model = (
            _build_model(self.llm_config, model_override=self.llm_config.oracle_model)
            if self.llm_config.oracle_model
            else model
        )

        orchestrator = build_orchestrator_agent(model, model_settings=model_settings)
        snapshot_filter = build_snapshot_filter_agent(filter_model, model_settings=model_settings)
        oracle_agent = build_oracle_agent(oracle_model, model_settings=model_settings)
        browser_worker = build_browser_worker_agent(worker_model, model_settings=model_settings)

        session = await launch_browser(self.browser_config)
        try:
            await session.page.goto(self.agent_config.target_url)
            prev_snapshot = None
            stop_reason: str | None = None
            for step in range(self.agent_config.max_steps):
                self.state.step = step + 1
                step_started = time.perf_counter()
                # Visual separation between steps
                logger.info("")
                logger.info(_STEP_SEPARATOR)
                logger.info(f"Step {self.state.step} start")
                try:
                    await session.page.wait_for_load_state("domcontentloaded")
                    await session.page.wait_for_load_state("networkidle", timeout=3000)
                except Exception:
                    pass
                # ── Handler extraction ──
                handler_map: dict[str, dict[str, str]] | None = None
                handlers_count = 0
                if self.agent_config.handlers_enabled:
                    handler_started = time.perf_counter()
                    handler_map = await extract_handlers(session.page)
                    handler_duration_ms = int((time.perf_counter() - handler_started) * 1000)
                    handlers_count = len(handler_map) if handler_map else 0
                    metrics.emit(
                        "handler_extraction",
                        step=self.state.step,
                        duration_ms=handler_duration_ms,
                        handlers=handlers_count,
                    )

                snapshot_started = time.perf_counter()
                snapshot = await capture_snapshot(
                    session.page, session.cdp_session, handler_map=handler_map,
                )
                snapshot_duration_ms = int((time.perf_counter() - snapshot_started) * 1000)

                if handler_map:
                    await cleanup_handler_attributes(session.page)

                metrics.emit(
                    "snapshot",
                    step=self.state.step,
                    duration_ms=snapshot_duration_ms,
                    url=snapshot.url,
                    title=snapshot.title,
                    elements=len(snapshot.elements),
                    handlers=handlers_count,
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
                    f"  snapshot: {snapshot_duration_ms}ms elements={len(snapshot.elements)}"
                    f" handlers={handlers_count} url={snapshot.url}"
                )
                prev_priority_ids = (
                    self.state.last_filter_output.priority_element_ids
                    if self.state.last_filter_output
                    else None
                )
                diff_text, _diff_ids = _snapshot_diff(
                    prev_snapshot,
                    snapshot,
                    priority_ids=prev_priority_ids,
                    changed_limit=self.agent_config.diff_changed_limit,
                    raw_text_limit=self.agent_config.raw_text_limit_diff,
                    raw_text_detail_limit=self.agent_config.raw_text_diff_detail_limit,
                    raw_text_scan_cap=self.agent_config.raw_text_scan_cap,
                    raw_text_line_max_len=self.agent_config.raw_text_line_max_len,
                    raw_text_dedupe_prefix_len=self.agent_config.raw_text_dedupe_prefix_len,
                    raw_text_dedupe_suffix_len=self.agent_config.raw_text_dedupe_suffix_len,
                )
                page_fingerprint = _page_fingerprint(
                    snapshot,
                    raw_text_limit=self.agent_config.raw_text_limit_fingerprint,
                )
                logger.debug("page_fingerprint=%s", page_fingerprint)
                logger.debug("diff_text:\n%s", diff_text)
                if self.state.last_page_fingerprint == page_fingerprint:
                    self.state.no_progress_steps += 1
                else:
                    self.state.no_progress_steps = 0
                self.state.last_page_fingerprint = page_fingerprint

                if page_saver:
                    await page_saver.capture_page(
                        session.page,
                        self.state.step,
                        snapshot.url or "",
                        snapshot.title or "",
                        page_fingerprint,
                    )

                if self.state.no_progress_steps >= self.agent_config.unchanged_abort_threshold:
                    stop_reason = "unchanged_fingerprint_abort"
                    logger.warning(
                        f"  abort: unchanged_fingerprint count={self.state.no_progress_steps} "
                        f"threshold={self.agent_config.unchanged_abort_threshold}"
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

                priority_ids: list[str] = []

                # Compute full tree text once — used by both Oracle and Filter
                full_tree_text = format_snapshot_for_llm(
                    snapshot,
                    max_elements=self.agent_config.max_elements,
                )

                # ── Oracle (dual trigger: periodic + stuck) ──
                oracle_hint = ""
                should_call_oracle = (
                    (self.agent_config.oracle_interval > 0 and self.state.step % self.agent_config.oracle_interval == 0)
                    or self.state.no_progress_steps >= self.agent_config.stuck_threshold
                )
                if should_call_oracle and self.state.step_trace:
                    trace_text = _format_step_trace(self.state.step_trace)
                    tool_list = ", ".join(sorted(DEFAULT_WORKER_TOOLS))
                    oracle_prompt = (
                        f"Overall goal: {self.agent_config.goal}\n\n"
                        f"Current step: {self.state.step}\n"
                        f"No-progress steps: {self.state.no_progress_steps}\n\n"
                        f"Execution trace:\n{trace_text}\n\n"
                        f"Worker tools: {tool_list}\n\n"
                        f"Page snapshot (full interactive element tree):\n{full_tree_text}\n"
                    )
                    logger.debug(
                        "oracle prompt step=%s chars=%s:\n%s",
                        self.state.step,
                        len(oracle_prompt),
                        oracle_prompt,
                    )
                    oracle_started = time.perf_counter()
                    try:
                        oracle_result = await oracle_agent.run(oracle_prompt)
                        oracle_duration_ms = int((time.perf_counter() - oracle_started) * 1000)
                        oracle_usage = usage_stats_from_result(oracle_result)
                        oracle_cost = cost_stats_from_result(
                            oracle_result, self.llm_config.oracle_model or self.llm_config.model
                        )
                        total_input_tokens += oracle_usage.input_tokens
                        total_output_tokens += oracle_usage.output_tokens
                        if oracle_cost:
                            total_cost_usd = (total_cost_usd or 0.0) + oracle_cost.cost_usd
                        metrics.emit(
                            "agent_call",
                            step=self.state.step,
                            agent="oracle",
                            duration_ms=oracle_duration_ms,
                            input_tokens=oracle_usage.input_tokens,
                            output_tokens=oracle_usage.output_tokens,
                            requests=oracle_usage.requests,
                            tool_calls=oracle_usage.tool_calls,
                            cost_usd=(oracle_cost.cost_usd if oracle_cost else None),
                            upstream_inference_cost_usd=(
                                oracle_cost.upstream_inference_cost_usd if oracle_cost else None
                            ),
                        )
                        advice = oracle_result.output
                        avoid_str = ", ".join(advice.avoid) if advice.avoid else "None"
                        logger.info(
                            f"  oracle: {oracle_duration_ms}ms all_clear={advice.all_clear} diagnosis={advice.diagnosis[:80]}"
                        )
                        logger.debug(
                            "oracle output step=%s all_clear=%s diagnosis=%s recommendation=%s avoid=%s",
                            self.state.step,
                            advice.all_clear,
                            advice.diagnosis,
                            advice.recommendation,
                            advice.avoid,
                        )
                        if not advice.all_clear:
                            oracle_hint = (
                                f"\n\nORACLE DIRECTIVE:\n"
                                f"Diagnosis: {advice.diagnosis}\n"
                                f"Recommendation: {advice.recommendation}\n"
                                f"Avoid: {avoid_str}"
                            )
                            # Invalidate filter cache so it re-runs with Oracle context
                            self.state.last_filter_fingerprint = None
                            logger.info(f"    recommendation: {advice.recommendation[:120]}")
                            if advice.avoid:
                                logger.info(f"    avoid: {avoid_str[:120]}")
                    except Exception:
                        oracle_duration_ms = int((time.perf_counter() - oracle_started) * 1000)
                        logger.warning("Oracle advisor failed", exc_info=True)

                # ── Filter (tree pruner) ──
                filter_output = self.state.last_filter_output
                if self.state.last_filter_fingerprint != page_fingerprint or filter_output is None:
                    logger.debug("full_tree_text (filter input):\n%s", full_tree_text)
                    raw_lines = _select_raw_text_lines(
                        list(snapshot.raw_text),
                        limit=self.agent_config.raw_text_limit_prompt,
                        scan_cap=self.agent_config.raw_text_scan_cap,
                        max_len=self.agent_config.raw_text_line_max_len,
                        dedupe_prefix_len=self.agent_config.raw_text_dedupe_prefix_len,
                        dedupe_suffix_len=self.agent_config.raw_text_dedupe_suffix_len,
                    )
                    raw_text_block = "\n".join(raw_lines) if raw_lines else "None."
                    last_summary = self.state.last_summary or "None."
                    last_worker_goal = self.state.last_worker_goal or "None."
                    filter_prompt = (
                        f"Overall goal: {self.agent_config.goal}\n\n"
                        f"Last worker goal: {last_worker_goal}\n"
                        f"Last step summary: {last_summary}\n\n"
                        f"Diff since prior snapshot:\n{diff_text}\n\n"
                        + (f"Oracle advice:\n{oracle_hint}\n\n" if oracle_hint else "")
                        + f"Page snapshot (full interactive element tree):\n{full_tree_text}\n\n"
                        f"Page text lines:\n{raw_text_block}\n"
                    )

                    logger.debug(
                        "snapshot_filter prompt step=%s chars=%s:\n%s",
                        self.state.step,
                        len(filter_prompt),
                        filter_prompt,
                    )
                    filter_started = time.perf_counter()
                    filter_result = await snapshot_filter.run(filter_prompt)
                    filter_duration_ms = int((time.perf_counter() - filter_started) * 1000)
                    filter_usage = usage_stats_from_result(filter_result)
                    filter_cost = cost_stats_from_result(filter_result, self.llm_config.filter_model or self.llm_config.model)
                    total_input_tokens += filter_usage.input_tokens
                    total_output_tokens += filter_usage.output_tokens
                    if filter_cost:
                        total_cost_usd = (total_cost_usd or 0.0) + filter_cost.cost_usd
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
                        f"  filter: {filter_duration_ms}ms "
                        f"useful_lines={len(filter_output.useful_text_lines or [])} "
                        f"priority_ids={len(priority_ids)} total_elements={len(snapshot.elements)}"
                    )
                    logger.debug(
                        "filter output step=%s useful_text_lines=%s notes=%s priority_element_ids=%s",
                        self.state.step,
                        filter_output.useful_text_lines,
                        filter_output.notes,
                        filter_output.priority_element_ids,
                    )

                # ── Build pruned snapshot ──
                useful_lines = filter_output.useful_text_lines if filter_output else []
                useful_block = "\n".join(useful_lines) if useful_lines else "None."
                priority_ids = list(filter_output.priority_element_ids if filter_output else [])
                kept_ids = set(priority_ids)
                pruned_elements = [el for el in snapshot.elements if el.stable_id in kept_ids]
                pruned_snapshot = PageSnapshot(
                    url=snapshot.url,
                    title=snapshot.title,
                    elements=pruned_elements,
                    raw_text=snapshot.raw_text,
                    viewport_width=snapshot.viewport_width,
                    viewport_height=snapshot.viewport_height,
                )

                # ── Orchestrator ──
                snapshot_text_orchestrator = format_snapshot_for_llm(
                    pruned_snapshot,
                    max_elements=self.agent_config.max_elements,
                )
                memory_text = _format_memory(self.state.memory, limit=self.agent_config.memory_steps)
                tool_list = ", ".join(sorted(DEFAULT_WORKER_TOOLS))
                orchestrator_prompt = (
                    f"Overall goal: {self.agent_config.goal}\n\n"
                    f"Filtered useful lines:\n{useful_block}\n\n"
                    f"Diff since prior snapshot:\n{diff_text}\n\n"
                    f"Memory (recent):\n{memory_text}\n\n"
                    f"Worker tools: {tool_list}\n\n"
                    f"Page snapshot:\n{snapshot_text_orchestrator}\n"
                    f"{oracle_hint}"
                )
                logger.debug(
                    "orchestrator prompt step=%s chars=%s:\n%s",
                    self.state.step,
                    len(orchestrator_prompt),
                    orchestrator_prompt,
                )
                orchestrator_started = time.perf_counter()
                decision_result = await orchestrator.run(orchestrator_prompt)
                orchestrator_duration_ms = int((time.perf_counter() - orchestrator_started) * 1000)
                orchestrator_usage = usage_stats_from_result(decision_result)
                orchestrator_cost = cost_stats_from_result(decision_result, self.llm_config.model)
                total_input_tokens += orchestrator_usage.input_tokens
                total_output_tokens += orchestrator_usage.output_tokens
                if orchestrator_cost:
                    total_cost_usd = (total_cost_usd or 0.0) + orchestrator_cost.cost_usd
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
                    f"  orchestrator: {orchestrator_duration_ms}ms worker={decision.worker} done={decision.done}"
                )
                if decision.worker_goal:
                    logger.info(f"    goal: {decision.worker_goal}")
                if decision.rationale:
                    logger.info(f"    rationale: {decision.rationale}")
                logger.debug(
                    "orchestrator output step=%s worker=%s worker_goal=%s done=%s rationale=%s allowed_tools=%s",
                    self.state.step,
                    decision.worker,
                    decision.worker_goal,
                    decision.done,
                    decision.rationale,
                    getattr(decision, "allowed_tools", None),
                )
                if decision.done:
                    logger.info(f"  orchestrator done: {decision.rationale or 'task complete'}")
                    metrics.emit(
                        "step_end",
                        step=self.state.step,
                        done=True,
                        duration_ms=int((time.perf_counter() - step_started) * 1000),
                    )
                    break

                # ── Worker ──
                snapshot_text_worker = format_snapshot_for_llm(
                    pruned_snapshot,
                    max_elements=self.agent_config.max_elements,
                )
                tool_tracker = ToolCallTracker()
                deps = WorkerDeps(
                    tool_context=tool_context,
                    metrics=metrics,
                    step=self.state.step,
                    tool_tracker=tool_tracker,
                    allowed_tools=DEFAULT_WORKER_TOOLS,
                )
                prev_url = getattr(session.page, "url", "") or ""
                worker_prompt = (
                    STEP_PROMPT.format(goal=decision.worker_goal)
                    + "\n\n"
                    + f"Page context:\n{useful_block}\n\n"
                    + f"Page snapshot:\n{snapshot_text_worker}\n"
                )
                logger.debug(
                    "worker prompt step=%s chars=%s:\n%s",
                    self.state.step,
                    len(worker_prompt),
                    worker_prompt,
                )
                worker_started = time.perf_counter()
                with browser_worker.sequential_tool_calls():
                    worker_result = await browser_worker.run(worker_prompt, deps=deps)
                worker_duration_ms = int((time.perf_counter() - worker_started) * 1000)
                worker_usage = usage_stats_from_result(worker_result)
                worker_cost = cost_stats_from_result(worker_result, self.llm_config.worker_model or self.llm_config.model)
                total_input_tokens += worker_usage.input_tokens
                total_output_tokens += worker_usage.output_tokens
                if worker_cost:
                    total_cost_usd = (total_cost_usd or 0.0) + worker_cost.cost_usd
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
                logger.debug("memory step=%s entries=%s", self.state.step, self.state.memory)

                # ── Populate step trace ──
                self.state.step_trace.append({
                    "step": self.state.step,
                    "url": getattr(session.page, "url", "") or "",
                    "goal": decision.worker_goal,
                    "summary": step_output.summary,
                    "diff_summary": diff_text.split("\n")[0] if diff_text else "",
                    "url_changed": prev_url != (getattr(session.page, "url", "") or ""),
                })
                logger.debug("step_trace step=%s entries=%s", self.state.step, self.state.step_trace)

                logger.info(f"  worker: {worker_duration_ms}ms done={step_output.done}")
                if step_output.summary:
                    logger.info(f"    summary: {step_output.summary}")
                logger.debug(
                    "worker output step=%s done=%s summary=%s",
                    self.state.step,
                    step_output.done,
                    step_output.summary,
                )
                metrics.emit(
                    "step_end",
                    step=self.state.step,
                    done=False,
                    worker_done=bool(step_output.done),
                    duration_ms=int((time.perf_counter() - step_started) * 1000),
                )
                if step_output.done:
                    logger.info("    worker done: delegated goal complete; continuing until orchestrator done=true")
                step_duration_ms = int((time.perf_counter() - step_started) * 1000)
                logger.info(f"Step {self.state.step} end {step_duration_ms}ms")
                prev_snapshot = snapshot
        finally:
            interrupted = isinstance(
                sys.exc_info()[1], (asyncio.CancelledError, KeyboardInterrupt)
            )
            if interrupted:
                print("\nShutting down gracefully... (press Ctrl+C again to force-kill)")

            # Allow a second Ctrl+C to force-kill during cleanup
            original_sigint = signal.getsignal(signal.SIGINT)

            def _force_kill(signum: int, frame: Any) -> None:
                print("\nForce-killing...")
                os._exit(1)

            try:
                signal.signal(signal.SIGINT, _force_kill)
            except (OSError, ValueError):
                pass

            try:
                await close_browser(session)
            except Exception:
                logger.warning("Error closing browser", exc_info=True)

            run_duration_ms = int((time.perf_counter() - run_started) * 1000)
            effective_stop_reason = "interrupted" if interrupted else stop_reason

            try:
                metrics.emit("run_end", duration_ms=run_duration_ms, interrupted=interrupted)
            except Exception:
                pass

            try:
                write_run_summary(
                    log_dir=self.agent_config.log_dir,
                    run_id=run_id,
                    summary={
                        "duration_ms": run_duration_ms,
                        "steps": self.state.step,
                        "last_summary": self.state.last_summary,
                        "stop_reason": effective_stop_reason,
                        "input_tokens": total_input_tokens,
                        "output_tokens": total_output_tokens,
                        "total_tokens": total_input_tokens + total_output_tokens,
                        "cost_usd": total_cost_usd,
                    },
                )
            except Exception:
                logger.warning("Failed to write run summary", exc_info=True)

            try:
                logger.info(
                    "Run end run_id=%s duration_ms=%s steps=%s total_tokens=%s cost_usd=%s%s",
                    run_id,
                    run_duration_ms,
                    self.state.step,
                    total_input_tokens + total_output_tokens,
                    total_cost_usd,
                    " (interrupted)" if interrupted else "",
                )
            except Exception:
                pass

            try:
                metrics.close()
            except Exception:
                pass

            _teardown_logging()

            try:
                signal.signal(signal.SIGINT, original_sigint)
            except (OSError, ValueError):
                pass


async def run_agent(agent_config: AgentConfig, llm_config: LLMConfig, browser_config: BrowserConfig) -> None:
    agent = BrowserAgent(agent_config, llm_config, browser_config)
    await agent.run()


def run_agent_sync(agent_config: AgentConfig, llm_config: LLMConfig, browser_config: BrowserConfig) -> None:
    try:
        asyncio.run(run_agent(agent_config, llm_config, browser_config))
    except KeyboardInterrupt:
        pass  # Cleanup already handled in BrowserAgent.run() finally block
