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
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from pydantic_ai import Agent, RunContext, ToolDefinition
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits
from pydantic_ai.models.cerebras import CerebrasModel
from pydantic_ai.models.groq import GroqModel
from pydantic_ai.models.openrouter import OpenRouterModel

from src.agent.core.resilient_model import ResilientModel
from src.agent.core.pruning import (
    extract_instruction_phrases,
    extract_stable_ids,
    match_phrases_to_elements,
)
from src.agent.core.history import make_tool_return_compactor
from src.agent.core.text_compress import compress_text_lines

from src.agent.browser.session import close_browser, launch_browser
from src.agent.capture.page_saver import PageSaver
from src.agent.config import AgentConfig, BrowserConfig, LLMConfig
from src.agent.context.handlers import cleanup_handler_attributes, extract_handlers
from src.agent.context.scroll_containers import (
    cleanup_scroll_container_attributes,
    extract_scroll_containers,
)
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
    get_git_commit,
    new_run_id,
    prepare_run_dir,
    usage_stats_from_result,
    write_run_summary,
)
from src.agent.models.actions import (
    OracleAdvice,
    OrchestratorDecision,
    SnapshotFilterOutput,
    StepOutput,
    ToolExecutionResult,
    UnifiedStepOutput,
)
from src.agent.prompts.system import (
    FILTER_PROMPT,
    ORACLE_PROMPT,
    ORCHESTRATOR_PROMPT,
    STEP_PROMPT,
    SYSTEM_PROMPT,
    UNIFIED_PROMPT,
)
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

    Parses the newline-delimited diff format::

        Base message.
        DOM changes:
          + "text" (tag)
          ~ tag[attr]: old -> new
          - "text" (tag)
    """
    lines = message.split("\n")
    if len(lines) <= 1:
        return None

    parts: list[str] = []

    # Parse diff lines (everything after the first line)
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped or stripped == "DOM changes:":
            continue
        if stripped == "No DOM changes.":
            parts.append("no DOM changes")
        elif stripped.startswith("+ "):
            parts.append(f"text+: {stripped[2:]}")
        elif stripped.startswith("~ "):
            parts.append(f"attr: {stripped[2:]}")
        elif stripped.startswith("- "):
            parts.append(f"text-: {stripped[2:]}")
        elif stripped.startswith("navigated to: "):
            parts.append(f"nav→{stripped[14:]}")

    # Extract scroll/value info from the base message first line
    first_line = lines[0]
    idx = first_line.find(". ", max(0, len(base_prefix) - 5)) if base_prefix else first_line.find(". ")
    if idx != -1:
        for seg in first_line[idx + 2:].split(". "):
            seg = seg.strip()
            if not seg:
                continue
            if seg.startswith("Scroll position changed by "):
                seg = seg.replace("Scroll position changed by ", "moved ")
            elif seg.startswith("Current value: "):
                seg = seg.replace("Current value: ", "val=")
            elif seg.startswith("WARNING: scroll position did not change"):
                seg = "AT BOUNDARY"
            elif seg.startswith("Page title: "):
                seg = seg.replace("Page title: ", "title: ")
            else:
                continue
            parts.insert(0, seg)

    if not parts:
        return None
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


_SEMANTIC_CONTAINER_TAGS: frozenset[str] = frozenset({
    "form",
    "section",
    "main",
    "nav",
    "aside",
    "article",
    "dialog",
    "header",
    "footer",
    "ul",
    "ol",
    "table",
    "fieldset",
})
_CONTAINER_EXPANSION_LIMIT = 80


def _container_prefixes(parent_chain: tuple[tuple[int, str, str], ...]) -> list[tuple[tuple[int, str, str], ...]]:
    """Pick up to two meaningful container prefixes from a parent chain.

    The deepest meaningful container can be too narrow (e.g., a flex row) and miss
    siblings that live at a higher level in the same task card. Returning an
    additional ancestor prefix helps preserve nearby controls without hardcoding.
    """
    prefixes: list[tuple[tuple[int, str, str], ...]] = []
    for idx in range(len(parent_chain) - 1, -1, -1):
        _, tag, label = parent_chain[idx]
        if label or tag in _SEMANTIC_CONTAINER_TAGS:
            prefixes.append(parent_chain[: idx + 1])
            if len(prefixes) >= 2:
                break
    return prefixes


def _filter_ids_ordered(
    ids: list[str] | tuple[str, ...] | None,
    *,
    valid_ids: set[str],
    avoid_ids: set[str],
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for sid in ids or []:
        if sid in seen:
            continue
        seen.add(sid)
        if sid not in valid_ids:
            continue
        if sid in avoid_ids:
            continue
        out.append(sid)
    return out


@dataclass
class AgentState:
    step: int = 0
    active_frame_id: str | None = None
    memory: list[str] = field(default_factory=list)
    last_summary: str | None = None
    last_page_fingerprint: str | None = None
    last_progress_fingerprint: str | None = None
    no_progress_steps: int = 0
    last_tool: str | None = None
    last_element_id: str | None = None
    last_filter_fingerprint: str | None = None
    last_filter_output: SnapshotFilterOutput | None = None
    last_worker_goal: str | None = None
    step_trace: list[dict[str, Any]] = field(default_factory=list)
    last_step_was_puzzle: bool = False
    consecutive_tool_limit_steps: int = 0


@dataclass
class ToolCallTracker:
    """Track tool calls within a step for logging purposes."""
    first_tool_logged: bool = False
    success_count: int = 0
    failure_count: int = 0
    calls: list[tuple[str, str | None]] = field(default_factory=list)

    def record(self, ok: bool, *, tool_name: str = "", element_id: str | None = None) -> None:
        if ok:
            self.success_count += 1
        else:
            self.failure_count += 1
        self.calls.append((tool_name, element_id))

    def calls_summary(self) -> str:
        """Compact summary of tool calls, collapsing consecutive duplicates."""
        if not self.calls:
            return ""
        parts: list[str] = []
        prev: tuple[str, str | None] | None = None
        count = 0
        for call in self.calls:
            if call == prev:
                count += 1
            else:
                if prev is not None:
                    label = prev[0]
                    if prev[1]:
                        label += f"({prev[1]})"
                    parts.append(f"{label}x{count}" if count > 1 else label)
                prev = call
                count = 1
        if prev is not None:
            label = prev[0]
            if prev[1]:
                label += f"({prev[1]})"
            parts.append(f"{label}x{count}" if count > 1 else label)
        return ", ".join(parts)


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
    "watch_for_text",
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
        model = ResilientModel(model)
    return model


def _model_settings(config: LLMConfig) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "timeout": float(config.timeout_seconds),
        "parallel_tool_calls": False,
        "max_tokens": config.max_tokens,
        "temperature": 0.7,
        "top_p": 0.8,
    }
    if config.provider == "openrouter":
        settings["openrouter_usage"] = {"include": True}
        settings["openrouter_provider"] = {
            "order": ["cerebras", "sambanova", "groq", "baseten", "fireworks", "google-ai-studio", "google-vertex", "together", "xai"],
            "only": ["cerebras", "sambanova", "groq", "baseten", "fireworks", "google-ai-studio", "google-vertex", "together", "xai"],
        }
        if config.reasoning_effort and config.reasoning_effort != "none":
            settings["openrouter_reasoning"] = {"effort": config.reasoning_effort}
    return settings


async def _with_deadline(coro: Any, deadline: float) -> Any:
    """Run *coro* with a deadline; raises TimeoutError if deadline has passed or is reached."""
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError("step deadline exceeded")
    return await asyncio.wait_for(coro, timeout=remaining)


def _format_memory(memory: list[str], *, limit: int = 10) -> str:
    if not memory:
        return "None."
    recent = memory[-limit:]
    # Collapse consecutive entries with identical content after the step number
    collapsed: list[tuple[str, int]] = []  # (entry, repeat_count)
    for entry in recent:
        # Strip "[step N, " prefix to get comparable content
        comparable = re.sub(r"^\[step \d+, ", "[step _, ", entry)
        if collapsed and collapsed[-1][0] == comparable:
            collapsed[-1] = (comparable, collapsed[-1][1] + 1)
        else:
            collapsed.append((comparable, 1))
    lines: list[str] = []
    for idx, (entry, count) in enumerate(collapsed):
        if count > 1:
            lines.append(f"{idx + 1}. {entry} (x{count})")
        else:
            lines.append(f"{idx + 1}. {entry}")
    return "\n".join(lines)

def _format_step_trace(trace: list[dict[str, Any]], *, window: int = 0) -> str:
    if not trace:
        return "No steps yet."
    if window > 0 and len(trace) > window:
        visible = trace[-window:]
        header = f"(showing last {window} of {len(trace)} steps)\n"
    else:
        visible = trace
        header = ""
    lines: list[str] = []
    for entry in visible:
        url_changed = "yes" if entry.get("url_changed") else "no"
        lines.append(
            f"Step {entry['step']}: [{entry.get('url', '')}] goal={entry.get('goal', '')}"
        )
        lines.append(
            f"  Result: {entry.get('summary', '')}"
        )
        if entry.get("tool_calls"):
            limit_tag = " [LIMIT HIT]" if entry.get("tool_limit_hit") else ""
            lines.append(
                f"  Tools: {entry['tool_calls']}{limit_tag}"
            )
        lines.append(
            f"  Diff: {entry.get('diff_summary', '')} | url_changed={url_changed}"
        )
    return header + "\n".join(lines)


# ── React state leak workaround for back-to-back math puzzle steps ──
#
# The challenge site has a React bug: the math puzzle component uses useState
# but is not keyed by step number. When React Router navigates from one puzzle
# step to the next, React reuses the component instance with stale state
# (solved=true, old code). The Solve button and number input are gone, making
# the step unsolvable.
#
# Detection: after snapshot capture, if the previous step was a puzzle AND the
# current snapshot shows both a math equation AND "Puzzle solved" text, the
# component has leaked state.
#
# Recovery: pushState to a non-puzzle step (forces React to unmount the puzzle
# component), then pushState back (mounts a fresh puzzle with clean state).
# The caller must re-capture the snapshot after recovery.
#
# Affected: every site version has exactly one adjacent puzzle pair
# (v1/v5: 19→20, v2/v4: 18→19, v3: 17→18).

_PUZZLE_EQUATION_RE = re.compile(r"\d+\s*\+\s*\d+\s*=\s*\?")
_FINAL_STEP_RE = re.compile(r"Step\s+(\d+)\s+of\s+(\d+)", re.IGNORECASE)


async def _fix_stale_puzzle_state(
    page,
    raw_text: Sequence[str],
    last_step_was_puzzle: bool,
) -> bool:
    """Detect and fix React state leak on back-to-back math puzzle steps.

    Returns True if stale state was detected and fixed (caller must re-capture
    the snapshot).
    """
    if not last_step_was_puzzle:
        return False

    joined = " ".join(raw_text)
    has_equation = bool(_PUZZLE_EQUATION_RE.search(joined))
    has_solved = "puzzle solved" in joined.lower()

    if not (has_equation and has_solved):
        return False

    # Parse step number and version from the current URL
    url = page.url
    step_match = re.search(r"/step(\d+)\?version=(\d+)", url)
    if not step_match:
        return False

    step_num = int(step_match.group(1))
    version = step_match.group(2)

    # pushState to a step 2 back (guaranteed non-puzzle since the adjacent
    # puzzle pair is always exactly 2 consecutive steps)
    detour = max(1, step_num - 2)
    detour_url = f"/step{detour}?version={version}"
    target_url = f"/step{step_num}?version={version}"

    await page.evaluate(
        f"window.history.pushState({{}}, '', '{detour_url}');"
        f"window.dispatchEvent(new PopStateEvent('popstate', {{ state: {{}} }}));"
    )
    await page.wait_for_timeout(500)

    await page.evaluate(
        f"window.history.pushState({{}}, '', '{target_url}');"
        f"window.dispatchEvent(new PopStateEvent('popstate', {{ state: {{}} }}));"
    )
    await page.wait_for_timeout(1500)

    return True


# ── Recursive Iframe Challenge off-by-one fix ──────────────────────────
#
# The challenge site's "Recursive Iframe Challenge" has a bug in the React
# component (Hv).  The "Enter Level" buttons increment a level counter `u`
# from 0 up to `numLevels - 1`, but the "Extract Code" button's onClick
# guard checks `u < y` (currentLevel < numLevels) and bails out early.
# Because `u` maxes out at `y - 1`, the guard always fires and the button
# is effectively dead — the challenge is unsolvable as-is.
#
# Fix: bypass the guard entirely by calling `onComplete(proof)` directly
# on the Hv component's fiber.  We build the same proof object that the
# Extract Code onClick's internal `w()` would construct, with currentLevel
# set to numLevels (what it should be if the off-by-one didn't exist).
# onComplete returns the code string, which we dispatch into the component's
# display state hook so it renders on the page for the agent to read.
#
# Detection:
#   Python pre-filter: "recursive iframe challenge" in snapshot raw_text
#   JS confirmation:   Extract Code button exists AND fiber has onComplete
#                      in memoizedProps (only the Hv component has this)


async def _fix_recursive_iframe_bug(page, raw_text: Sequence[str]) -> bool:
    """Detect and fix the off-by-one bug in the Recursive Iframe Challenge.

    The Extract Code button's onClick guard requires currentLevel >= numLevels,
    but the Enter Level buttons only increment the counter to numLevels - 1.
    We call onComplete(proof) directly, bypassing the guard, and dispatch
    the resulting code string into the display state hook.

    Returns True if the bug was detected and fixed (caller must re-capture
    the snapshot).
    """
    joined = " ".join(raw_text).lower()

    # Cheap pre-filter: must be on the Recursive Iframe Challenge page.
    if "recursive iframe challenge" not in joined:
        return False

    patched = await page.evaluate("""(() => {
        // Find the "Extract Code" button
        const btns = [...document.querySelectorAll('button')];
        const extractBtn = btns.find(b =>
            b.textContent.trim().toLowerCase().includes('extract code')
        );
        if (!extractBtn) return 'no_button';

        // Walk up the React fiber tree to find the Hv component
        // (the one with onComplete in memoizedProps)
        const fiberKey = Object.keys(extractBtn).find(k =>
            k.startsWith('__reactFiber$')
        );
        if (!fiberKey) return 'no_fiber';

        let fiber = extractBtn[fiberKey];
        let hvFiber = null;
        while (fiber) {
            if (fiber.memoizedProps &&
                typeof fiber.memoizedProps.onComplete === 'function') {
                hvFiber = fiber;
                break;
            }
            fiber = fiber.return;
        }
        if (!hvFiber) return 'no_onComplete';

        // Read props
        const onComplete = hvFiber.memoizedProps.onComplete;
        const config = hvFiber.memoizedProps.config || {};
        const numLevels = (config.metadata && config.metadata.numLevels) || 3;
        const stepNum = hvFiber.memoizedProps.stepNum;

        // Read hook chain:
        //   hook 0 = currentLevel (useState, number)
        //   hook 1 = code display state (useState, null initially)
        //   hook 2 = levelClickTimes (useRef, {current: {...}})
        const hook0 = hvFiber.memoizedState;
        if (!hook0 || typeof hook0.memoizedState !== 'number') return 'no_level_hook';
        const currentLevel = hook0.memoizedState;

        const hook1 = hook0.next;
        if (!hook1 || !hook1.queue || typeof hook1.queue.dispatch !== 'function')
            return 'no_code_hook';

        const hook2 = hook1.next;
        const clickTimes = (hook2 && hook2.memoizedState && hook2.memoizedState.current)
            ? { ...hook2.memoizedState.current }
            : {};

        // Fill in the missing click time for the current (stuck) level
        if (!(currentLevel in clickTimes)) {
            clickTimes[currentLevel] = Date.now();
        }

        // Build the proof — same structure as the Extract Code onClick's
        // internal builder:
        //   { type, timestamp, data: { method, numLevels, currentLevel,
        //     levelClickTimes, stepNum } }
        // Set currentLevel = numLevels to satisfy validation in bd().
        const proof = {
            type: "recursive_iframe",
            timestamp: Date.now(),
            data: {
                method: "recursive_iframe",
                numLevels: numLevels,
                currentLevel: numLevels,
                levelClickTimes: clickTimes,
                stepNum: stepNum,
            },
        };

        // Call onComplete(proof) — returns the code string (or null on
        // validation failure inside bd())
        const code = onComplete(proof);
        if (!code) return 'onComplete_returned_null';

        // Dispatch the code into hook 1 (the display state) so it renders
        // on the page for the agent to read from the next snapshot
        hook1.queue.dispatch(code);

        return 'patched_' + code;
    })()""")

    if not isinstance(patched, str) or not patched.startswith("patched_"):
        return False

    # Wait for React re-render after dispatching code to display state
    await page.wait_for_timeout(500)
    return True


# ── Final-step code-reveal off-by-one fix ────────────────────────────
#
# The challenge site's markChallengeComplete(stepNum, proof) returns
# codes.get(stepNum + 1), but only 30 codes exist (indexed 1–30).
# On the final step (step 30), codes.get(31) is undefined → null,
# so the "Reveal Code" button never displays a code.
#
# The same off-by-one affects validateCode(stepNum): it checks
# codes.get(stepNum + 1), so code submission on the last step always
# returns false regardless of what is entered.
#
# However, markChallengeComplete still calls
# completedChallenges.add(stepNum) before returning null, so the
# challenge IS marked complete in the session after clicking
# "Reveal Code".
#
# The /finish page is a static congratulations page with no server-
# side validation — navigating there directly works once the final
# challenge is marked complete (or even without that).
#
# Detection (pure Python):
#   1. "Step N of N" in raw text where both numbers match (final step)
#   2. URL contains /step{N}
#
# Recovery: pushState to /finish + popstate (same technique as the
# stale puzzle fix).


async def _fix_final_step_code_bug(
    page, raw_text: Sequence[str],
) -> bool:
    """Detect the final-step code-reveal bug and navigate to /finish.

    The site's markChallengeComplete returns codes.get(stepNum + 1), but
    only 30 codes exist, so step 30 gets codes.get(31) = undefined.
    Since /finish has no server-side validation, we navigate there as
    soon as the final step is detected — no progress guard needed.

    Returns True if navigated (caller must re-capture the snapshot).
    """
    joined = " ".join(raw_text)
    m = _FINAL_STEP_RE.search(joined)
    if not m:
        return False

    current_step = m.group(1)
    total_steps = m.group(2)
    if current_step != total_steps:
        return False

    # Confirm URL matches /step{N}
    url = page.url
    if f"/step{current_step}" not in url:
        return False

    # Navigate to /finish via pushState + popstate (triggers React router)
    await page.evaluate(
        "window.history.pushState({}, '', '/finish');"
        "window.dispatchEvent(new PopStateEvent('popstate'));"
    )
    await page.wait_for_timeout(1500)
    return True


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

def _element_progress_line(element: ElementSnapshot) -> str:
    """Semantic element identity for progress detection (ignores stable_id)."""
    attrs = element.attributes or {}
    important_attrs: list[str] = []
    for key in ["id", "name", "type", "placeholder", "aria-label", "title", "alt", "href", "value", "disabled"]:
        if key in attrs:
            value = attrs.get(key)
            if value:
                important_attrs.append(f"{key}={_normalize_label(str(value))}")
            else:
                important_attrs.append(key)
    parts = [
        _normalize_label(element.role),
        _normalize_label(element.name),
        _normalize_label(element.text or element.descendant_text),
        _normalize_label(element.node_name),
        "|".join(sorted(set(important_attrs))),
    ]
    return "\t".join(parts)


def _progress_fingerprint(
    snapshot: Any,
    *,
    max_elements: int = 120,
    raw_text_lines: int = 60,
    raw_text_chars: int = 8000,
    raw_text_scan_cap: int = 20000,
    raw_text_line_max_len: int = 800,
    raw_text_dedupe_prefix_len: int = 240,
    raw_text_dedupe_suffix_len: int = 120,
) -> str:
    """Fingerprint page progress while ignoring stable-id churn."""
    elements = list(getattr(snapshot, "elements", []) or [])
    semantic_lines = [_element_progress_line(el) for el in elements[: max(0, int(max_elements))]]
    semantic_lines.sort()

    raw_lines = _select_raw_text_lines(
        list(getattr(snapshot, "raw_text", []) or []),
        limit=int(raw_text_lines),
        scan_cap=raw_text_scan_cap,
        max_len=raw_text_line_max_len,
        dedupe_prefix_len=raw_text_dedupe_prefix_len,
        dedupe_suffix_len=raw_text_dedupe_suffix_len,
    )
    raw_lines = compress_text_lines(raw_lines, max_lines=int(raw_text_lines), max_chars=int(raw_text_chars))

    lines: list[str] = [getattr(snapshot, "url", "") or "", getattr(snapshot, "title", "") or ""]
    lines.extend(semantic_lines)
    if raw_lines:
        lines.append("RAW_TEXT:")
        lines.extend(raw_lines)
    material = "\n".join(lines)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


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

def _is_disabled(element) -> bool:
    attrs = getattr(element, "attributes", None) or {}
    if "disabled" in attrs:
        return True
    return attrs.get("aria-disabled", "").lower() == "true"

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
    disabled_hint = ""
    if _is_disabled(element):
        disabled_hint = " [disabled]"
    hints = f"{bbox_hint}{(' ' + frame_hint) if frame_hint else ''}{reason_hint}{viewport_hint}{disabled_hint}"
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
) -> tuple[str, list[str], list[str]]:
    if not prev:
        return "First snapshot (no prior snapshot to diff).", [], []
    prev_map = {el.stable_id: el for el in list(getattr(prev, "elements", []) or [])}
    curr_map = {el.stable_id: el for el in list(getattr(curr, "elements", []) or [])}
    new_ids = sorted([sid for sid in curr_map.keys() if sid not in prev_map])
    removed_ids = sorted([sid for sid in prev_map.keys() if sid not in curr_map])
    changed: list[str] = []
    for sid in sorted(set(prev_map.keys()) & set(curr_map.keys())):
        a = prev_map[sid]
        b = curr_map[sid]
        a_key = (_normalize_label(a.role), _normalize_label(a.name), _normalize_label(a.text), _normalize_label(a.node_name), _is_disabled(a))
        b_key = (_normalize_label(b.role), _normalize_label(b.name), _normalize_label(b.text), _normalize_label(b.node_name), _is_disabled(b))
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
        brief = _format_element_brief(curr_map[sid])
        prev_dis = _is_disabled(prev_map[sid])
        curr_dis = _is_disabled(curr_map[sid])
        if prev_dis and not curr_dis:
            brief += " [was disabled, NOW ENABLED]"
        elif not prev_dis and curr_dis:
            brief += " [was enabled, now DISABLED]"
        lines.append(f"~ {brief}")
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
    newly_enabled: list[str] = []
    for sid in sorted(set(prev_map.keys()) & set(curr_map.keys())):
        if _is_disabled(prev_map[sid]) and not _is_disabled(curr_map[sid]):
            newly_enabled.append(sid)
    return "\n".join(lines), detail_ids, newly_enabled


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


def register_browser_tools(agent: Agent[WorkerDeps, Any]) -> None:
    @agent.tool(name="click_element")
    async def click_element(ctx: RunContext[WorkerDeps], element_id: str) -> ToolExecutionResult:
        """Click on an element to activate it, follow a link, or toggle a control. Automatically scrolls the element into view — no need to scroll first. Use element_id from the page snapshot."""
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
        if ctx.deps.tool_tracker:
            ctx.deps.tool_tracker.record(result.ok, tool_name="click_element", element_id=element_id)
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="hover_element")
    async def hover_element(ctx: RunContext[WorkerDeps], element_id: str, duration_ms: int = 2000) -> ToolExecutionResult:
        """Hover over an element for a duration to trigger hover-dependent behavior. Automatically scrolls the element into view — no need to scroll first. Use for revealing tooltips, dropdown menus, or hidden content triggered by mouse hover. Default hold time is 2 seconds; increase duration_ms for content that requires longer hover (up to 5 000 ms). Use element_id from the page snapshot."""
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
        if ctx.deps.tool_tracker:
            ctx.deps.tool_tracker.record(result.ok, tool_name="hover_element", element_id=element_id)
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
        if ctx.deps.tool_tracker:
            ctx.deps.tool_tracker.record(True, tool_name="find_elements")
        return ToolExecutionResult(ok=True, message=message)

    @agent.tool(name="type_text")
    async def type_text(ctx: RunContext[WorkerDeps], element_id: str, text: str) -> ToolExecutionResult:
        """Type text into an input or editable field — clears any existing content first and focuses automatically (no separate click needed). Automatically scrolls the element into view — no need to scroll first. Use for any element that accepts keyboard input, including fields with placeholder hints like 'click to type' or 'enter value'. Use element_id from the page snapshot."""
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
        if ctx.deps.tool_tracker:
            ctx.deps.tool_tracker.record(result.ok, tool_name="type_text", element_id=element_id)
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="drag_and_drop")
    async def drag_and_drop(ctx: RunContext[WorkerDeps], source_id: str, target_id: str) -> ToolExecutionResult:
        """Drag one element onto another. Automatically scrolls elements into view — no need to scroll first. Use for reordering lists, moving cards, adjusting sliders, etc. Use element IDs from the page snapshot."""
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
        if ctx.deps.tool_tracker:
            ctx.deps.tool_tracker.record(result.ok, tool_name="drag_and_drop", element_id=source_id)
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="draw")
    async def draw(ctx: RunContext[WorkerDeps], element_id: str, path: list[list[float]]) -> ToolExecutionResult:
        """Draw a freeform path on a canvas or drawing surface by moving the mouse through a series of coordinate points with the button held. Automatically scrolls the element into view — no need to scroll first. Points are [x, y] pairs relative to the element's top-left corner. Use element_id from the page snapshot."""
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
        if ctx.deps.tool_tracker:
            ctx.deps.tool_tracker.record(result.ok, tool_name="draw", element_id=element_id)
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
        if ctx.deps.tool_tracker:
            ctx.deps.tool_tracker.record(result.ok, tool_name="wait")
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="watch_for_text")
    async def watch_for_text(
        ctx: RunContext[WorkerDeps], text: str, timeout_ms: int = 10000
    ) -> ToolExecutionResult:
        """Watch for literal text to appear on the page, then click its element.
Pass the exact text to match (case-sensitive substring).
Use ONLY for transient elements that appear after a delay — buttons, toasts, or labels that are not yet in the snapshot.
When tool feedback reports new text appeared (e.g. "New text appeared: ..."), watch for the dynamic content itself, not surrounding labels or prefixes. For example, if a page says "your value will appear here: " and feedback later shows a button with new text, watch for the button text, not the label.
Do NOT fabricate expected text — only watch for text you have actually seen in tool feedback or page instructions.
NOT for finding text already visible in the snapshot — use click_element for those. Max timeout 10 000 ms."""
        start = time.perf_counter()
        result = await semantic.watch_for_text(
            text, ctx.deps.tool_context, timeout_ms=timeout_ms
        )
        duration_ms = int((time.perf_counter() - start) * 1000)
        _log_tool_header_if_needed(ctx.deps.tool_tracker)
        text_preview = text[:30] + "..." if len(text) > 30 else text
        logger.info(
            _format_tool_log(
                "watch_for_text",
                result.ok,
                duration_ms,
                extra=f'"{text_preview}"',
            )
        )
        logger.debug(
            "tool=watch_for_text step=%s ok=%s text=%s timeout_ms=%s duration_ms=%s full_message=%s",
            ctx.deps.step,
            result.ok,
            text,
            timeout_ms,
            duration_ms,
            result.message,
        )
        ctx.deps.metrics.emit(
            "tool_call",
            step=ctx.deps.step,
            tool="watch_for_text",
            ok=result.ok,
            duration_ms=duration_ms,
            text=text,
            timeout_ms=timeout_ms,
        )
        if ctx.deps.tool_tracker:
            ctx.deps.tool_tracker.record(result.ok, tool_name="watch_for_text")
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
        if ctx.deps.tool_tracker:
            ctx.deps.tool_tracker.record(result.ok, tool_name="inspect_element", element_id=element_id)
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
        if ctx.deps.tool_tracker:
            ctx.deps.tool_tracker.record(result.ok, tool_name="search_page_attributes")
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
        if ctx.deps.tool_tracker:
            ctx.deps.tool_tracker.record(result.ok, tool_name="scroll", element_id=element_id)
        return ToolExecutionResult(ok=result.ok, message=result.message)

    @agent.tool(name="switch_to_iframe")
    async def switch_to_iframe(ctx: RunContext[WorkerDeps], iframe_id: str) -> ToolExecutionResult:
        """Set the ACTIVE FRAME to an iframe. Use when you need wait/watch_for_text/execute_js/press_key_combination to target that iframe or when a frame error tells you to switch. Use element_id from the page snapshot."""
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
        if ctx.deps.tool_tracker:
            ctx.deps.tool_tracker.record(result.ok, tool_name="switch_to_iframe", element_id=iframe_id)
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
        if ctx.deps.tool_tracker:
            ctx.deps.tool_tracker.record(result.ok, tool_name="switch_to_main_frame")
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
        if ctx.deps.tool_tracker:
            ctx.deps.tool_tracker.record(result.ok, tool_name="navigate_to")
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
        if ctx.deps.tool_tracker:
            ctx.deps.tool_tracker.record(result.ok, tool_name="take_screenshot")
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
        if ctx.deps.tool_tracker:
            ctx.deps.tool_tracker.record(result.ok, tool_name="execute_js")
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
        if ctx.deps.tool_tracker:
            ctx.deps.tool_tracker.record(result.ok, tool_name="press_key_combination")
        return ToolExecutionResult(ok=result.ok, message=result.message)

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


def _build_history_processors(keep_recent_tool_rounds: int) -> list:
    processors: list = []
    if keep_recent_tool_rounds > 0:
        processors.append(make_tool_return_compactor(keep_recent=keep_recent_tool_rounds))
    return processors


def build_browser_worker_agent(
    model: Model, *, model_settings: dict[str, Any], keep_recent_tool_rounds: int = 3,
) -> Agent[WorkerDeps, StepOutput]:
    agent: Agent[WorkerDeps, StepOutput] = Agent(
        model,
        deps_type=WorkerDeps,
        output_type=StepOutput,
        system_prompt=SYSTEM_PROMPT,
        model_settings=model_settings,
        prepare_tools=_filter_tools,
        prepare_output_tools=_normalize_strict,
        retries=1,
        history_processors=_build_history_processors(keep_recent_tool_rounds),
    )
    register_browser_tools(agent)
    return agent


def build_unified_agent(
    model: Model, *, model_settings: dict[str, Any], keep_recent_tool_rounds: int = 3,
) -> Agent[WorkerDeps, UnifiedStepOutput]:
    agent: Agent[WorkerDeps, UnifiedStepOutput] = Agent(
        model,
        deps_type=WorkerDeps,
        output_type=UnifiedStepOutput,
        system_prompt=(SYSTEM_PROMPT, UNIFIED_PROMPT),
        model_settings=model_settings,
        prepare_tools=_filter_tools,
        prepare_output_tools=_normalize_strict,
        retries=1,
        history_processors=_build_history_processors(keep_recent_tool_rounds),
    )
    register_browser_tools(agent)
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
        run_dir = prepare_run_dir(
            self.agent_config.log_dir,
            run_id,
            max_log_runs=self.agent_config.max_log_runs,
        )
        _setup_logging(
            run_dir,
            level=self.agent_config.log_level,
            color=self.agent_config.color_logs,
        )
        metrics = MetricsRecorder(
            log_dir=run_dir,
            run_id=run_id,
            enabled=self.agent_config.metrics_enabled,
        )
        page_saver = PageSaver(run_dir, run_id) if self.agent_config.save_pages else None
        run_started = time.perf_counter()
        total_input_tokens = 0
        total_output_tokens = 0
        total_cost_usd: float | None = None
        git_commit = get_git_commit()
        metrics.emit(
            "run_start",
            target_url=self.agent_config.target_url,
            goal=self.agent_config.goal,
            max_steps=self.agent_config.max_steps,
            model=self.llm_config.model,
            worker_model=self.llm_config.worker_model,
            filter_model=self.llm_config.filter_model,
            oracle_model=self.llm_config.oracle_model,
            provider=self.llm_config.provider,
            git_commit=git_commit,
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

        _seen: set[int] = set()
        resilient_models: list[ResilientModel] = []
        for m in [model, worker_model, filter_model, oracle_model]:
            if isinstance(m, ResilientModel) and id(m) not in _seen:
                _seen.add(id(m))
                resilient_models.append(m)

        orchestrator = build_orchestrator_agent(model, model_settings=model_settings)
        snapshot_filter = build_snapshot_filter_agent(filter_model, model_settings=model_settings)
        oracle_agent = build_oracle_agent(oracle_model, model_settings=model_settings)
        keep_recent = self.agent_config.keep_recent_tool_rounds
        browser_worker = build_browser_worker_agent(
            worker_model, model_settings=model_settings, keep_recent_tool_rounds=keep_recent,
        )
        unified_agent: Agent[WorkerDeps, UnifiedStepOutput] | None = None
        if self.agent_config.unified:
            unified_agent = build_unified_agent(
                worker_model, model_settings=model_settings, keep_recent_tool_rounds=keep_recent,
            )

        session = await launch_browser(self.browser_config)
        try:
            await session.page.goto(self.agent_config.target_url)
            prev_snapshot = None
            stop_reason: str | None = None
            tool_timing = semantic.ToolTimingConfig(
                settle_ms=self.agent_config.settle_ms,
                draw_settle_ms=self.agent_config.draw_settle_ms,
                draw_point_interval_ms=self.agent_config.draw_point_interval_ms,
                drag_phase_interval_ms=self.agent_config.drag_phase_interval_ms,
            )
            for step in range(self.agent_config.max_steps):
                self.state.step = step + 1
                step_started = time.perf_counter()
                # Visual separation between steps
                logger.info("")
                logger.info(_STEP_SEPARATOR)
                logger.info(f"Step {self.state.step} start")
                try:
                    await session.page.wait_for_load_state("domcontentloaded")
                    await session.page.wait_for_load_state(
                        "networkidle", timeout=self.agent_config.networkidle_timeout_ms
                    )
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

                scroll_marked = 0
                if self.agent_config.scroll_containers_enabled:
                    scroll_started = time.perf_counter()
                    scroll_marked = await extract_scroll_containers(session.page)
                    scroll_duration_ms = int((time.perf_counter() - scroll_started) * 1000)
                    metrics.emit(
                        "scroll_container_marking",
                        step=self.state.step,
                        duration_ms=scroll_duration_ms,
                        marked=scroll_marked,
                    )

                snapshot_started = time.perf_counter()
                snapshot = await capture_snapshot(
                    session.page,
                    session.cdp_session,
                    handler_map=handler_map,
                    desc_text_preview_enabled=self.agent_config.desc_text_preview_enabled,
                    desc_text_preview_max_chars=self.agent_config.desc_text_preview_max_chars,
                    desc_text_preview_max_nodes=self.agent_config.desc_text_preview_max_nodes,
                    class_sanitize_mode=self.agent_config.class_sanitize_mode,
                    class_sanitize_max_tokens=self.agent_config.class_sanitize_max_tokens,
                    class_sanitize_max_chars=self.agent_config.class_sanitize_max_chars,
                    class_sanitize_fallback_tokens=self.agent_config.class_sanitize_fallback_tokens,
                )
                snapshot_duration_ms = int((time.perf_counter() - snapshot_started) * 1000)

                if handler_map:
                    await cleanup_handler_attributes(session.page)
                if scroll_marked:
                    await cleanup_scroll_container_attributes(session.page)

                # ── React state leak fix (back-to-back math puzzle) ──
                if await _fix_stale_puzzle_state(
                    session.page, snapshot.raw_text, self.state.last_step_was_puzzle
                ):
                    logger.info("  stale puzzle state detected — fixed via pushState detour")
                    snapshot = await capture_snapshot(
                        session.page,
                        session.cdp_session,
                        handler_map=handler_map,
                        desc_text_preview_enabled=self.agent_config.desc_text_preview_enabled,
                        desc_text_preview_max_chars=self.agent_config.desc_text_preview_max_chars,
                        desc_text_preview_max_nodes=self.agent_config.desc_text_preview_max_nodes,
                        class_sanitize_mode=self.agent_config.class_sanitize_mode,
                        class_sanitize_max_tokens=self.agent_config.class_sanitize_max_tokens,
                        class_sanitize_max_chars=self.agent_config.class_sanitize_max_chars,
                        class_sanitize_fallback_tokens=self.agent_config.class_sanitize_fallback_tokens,
                    )

                # ── Recursive iframe challenge off-by-one fix ──
                if await _fix_recursive_iframe_bug(session.page, snapshot.raw_text):
                    logger.info("  recursive iframe bug detected — fixed via React state patch")
                    snapshot = await capture_snapshot(
                        session.page,
                        session.cdp_session,
                        handler_map=handler_map,
                        desc_text_preview_enabled=self.agent_config.desc_text_preview_enabled,
                        desc_text_preview_max_chars=self.agent_config.desc_text_preview_max_chars,
                        desc_text_preview_max_nodes=self.agent_config.desc_text_preview_max_nodes,
                        class_sanitize_mode=self.agent_config.class_sanitize_mode,
                        class_sanitize_max_tokens=self.agent_config.class_sanitize_max_tokens,
                        class_sanitize_max_chars=self.agent_config.class_sanitize_max_chars,
                        class_sanitize_fallback_tokens=self.agent_config.class_sanitize_fallback_tokens,
                    )

                # ── Final-step code-reveal bug fix ──
                if await _fix_final_step_code_bug(
                    session.page, snapshot.raw_text,
                ):
                    logger.info("  final step code bug detected — navigated to /finish")
                    snapshot = await capture_snapshot(
                        session.page,
                        session.cdp_session,
                        handler_map=handler_map,
                        desc_text_preview_enabled=self.agent_config.desc_text_preview_enabled,
                        desc_text_preview_max_chars=self.agent_config.desc_text_preview_max_chars,
                        desc_text_preview_max_nodes=self.agent_config.desc_text_preview_max_nodes,
                        class_sanitize_mode=self.agent_config.class_sanitize_mode,
                        class_sanitize_max_tokens=self.agent_config.class_sanitize_max_tokens,
                        class_sanitize_max_chars=self.agent_config.class_sanitize_max_chars,
                        class_sanitize_fallback_tokens=self.agent_config.class_sanitize_fallback_tokens,
                    )

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
                diff_text, _diff_ids, newly_enabled_ids = _snapshot_diff(
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
                if self.agent_config.progress_fingerprint_enabled:
                    progress_fingerprint = _progress_fingerprint(
                        snapshot,
                        max_elements=self.agent_config.progress_fingerprint_max_elements,
                        raw_text_lines=self.agent_config.progress_fingerprint_raw_lines,
                        raw_text_chars=self.agent_config.progress_fingerprint_raw_chars,
                        raw_text_scan_cap=self.agent_config.raw_text_scan_cap,
                        raw_text_line_max_len=self.agent_config.raw_text_line_max_len,
                        raw_text_dedupe_prefix_len=self.agent_config.raw_text_dedupe_prefix_len,
                        raw_text_dedupe_suffix_len=self.agent_config.raw_text_dedupe_suffix_len,
                    )
                    logger.debug("progress_fingerprint=%s", progress_fingerprint)
                    if self.state.last_progress_fingerprint == progress_fingerprint:
                        self.state.no_progress_steps += 1
                    else:
                        self.state.no_progress_steps = 0
                    self.state.last_progress_fingerprint = progress_fingerprint
                else:
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
                    fp_label = (
                        "progress_fingerprint"
                        if self.agent_config.progress_fingerprint_enabled
                        else "page_fingerprint"
                    )
                    logger.warning(
                        f"  abort: unchanged_{fp_label} count={self.state.no_progress_steps} "
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
                    timing=tool_timing,
                )

                priority_ids: list[str] = []

                # Compute full tree text once — used by both Oracle and Filter
                full_tree_text = format_snapshot_for_llm(
                    snapshot,
                    max_elements=self.agent_config.max_elements,
                    active_frame_id=self.state.active_frame_id,
                    class_sanitize_mode=self.agent_config.class_sanitize_mode,
                    class_sanitize_max_tokens=self.agent_config.class_sanitize_max_tokens,
                    class_sanitize_max_chars=self.agent_config.class_sanitize_max_chars,
                    class_sanitize_fallback_tokens=self.agent_config.class_sanitize_fallback_tokens,
                    attr_value_max_len=self.agent_config.snapshot_attr_value_max_len,
                )

                # Step-level deadline for LLM calls
                _step_deadline = asyncio.get_running_loop().time() + self.agent_config.step_timeout_seconds
                _step_timed_out = False

                def _is_step_timed_out() -> bool:
                    """Log + record if the step deadline was exceeded. Returns True to skip."""
                    if not _step_timed_out:
                        return False
                    logger.warning("Step %s timed out, skipping remaining LLM calls", self.state.step)
                    self.state.memory.append(f"[step {self.state.step}] Step timed out")
                    metrics.emit(
                        "step_end",
                        step=self.state.step,
                        done=False,
                        timeout=True,
                        duration_ms=int((time.perf_counter() - step_started) * 1000),
                    )
                    logger.info(f"Step {self.state.step} end (timed out)")
                    return True

                # ── Oracle (triple trigger: periodic + stuck + tool-limit loop) ──
                oracle_hint = ""
                advice: OracleAdvice | None = None
                should_call_oracle = (
                    (self.agent_config.oracle_interval > 0 and self.state.step % self.agent_config.oracle_interval == 0)
                    or self.state.no_progress_steps >= self.agent_config.stuck_threshold
                    or self.state.consecutive_tool_limit_steps >= 2
                )
                if should_call_oracle and self.state.step_trace:
                    trace_text = _format_step_trace(self.state.step_trace, window=self.agent_config.oracle_trace_window)
                    tool_list = ", ".join(sorted(DEFAULT_WORKER_TOOLS))
                    tool_constraint = "Only recommend actions using these exact tools. Do not suggest inspecting elements, reading page content, taking screenshots, executing JavaScript, or any action not in this list."
                    oracle_prompt = (
                        f"Overall goal: {self.agent_config.goal}\n\n"
                        f"Current step: {self.state.step}\n"
                        f"No-progress steps: {self.state.no_progress_steps}\n"
                        f"Consecutive tool-limit-hit steps: {self.state.consecutive_tool_limit_steps}\n\n"
                        f"Execution trace:\n{trace_text}\n\n"
                        f"Worker tools: {tool_list}\n{tool_constraint}\n\n"
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
                        oracle_result = await _with_deadline(oracle_agent.run(oracle_prompt), _step_deadline)
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
                            # Give oracle-guided steps a fair window before abort
                            self.state.no_progress_steps = max(0, self.state.no_progress_steps - self.agent_config.stuck_threshold)
                            logger.info(f"    recommendation: {advice.recommendation[:120]}")
                            if advice.avoid:
                                logger.info(f"    avoid: {avoid_str[:120]}")
                    except (asyncio.CancelledError, KeyboardInterrupt):
                        raise
                    except TimeoutError:
                        oracle_duration_ms = int((time.perf_counter() - oracle_started) * 1000)
                        _step_timed_out = True
                        logger.warning("Oracle timed out (step %s deadline)", self.state.step)
                    except Exception:
                        oracle_duration_ms = int((time.perf_counter() - oracle_started) * 1000)
                        logger.warning("Oracle advisor failed", exc_info=True)

                oracle_intervened = bool(
                    self.agent_config.widen_on_oracle and advice and not advice.all_clear
                )
                avoid_ids: set[str] = set()
                if advice:
                    for entry in advice.avoid or []:
                        avoid_ids |= extract_stable_ids(entry)

                # ── Filter (tree pruner) ──
                if _is_step_timed_out():
                    prev_snapshot = snapshot
                    continue
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
                    raw_lines = compress_text_lines(raw_lines, max_lines=60, max_chars=8000)
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
                    try:
                        filter_result = await _with_deadline(snapshot_filter.run(filter_prompt), _step_deadline)
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
                    except (asyncio.CancelledError, KeyboardInterrupt):
                        raise
                    except Exception as _filter_exc:
                        filter_duration_ms = int((time.perf_counter() - filter_started) * 1000)
                        if isinstance(_filter_exc, TimeoutError):
                            _step_timed_out = True
                            logger.warning("Filter timed out (step %s deadline)", self.state.step)
                        else:
                            logger.warning(
                                "Filter failed (step %s), using unfiltered snapshot",
                                self.state.step,
                                exc_info=True,
                            )
                            metrics.emit(
                                "agent_call",
                                step=self.state.step,
                                agent="snapshot_filter",
                                duration_ms=filter_duration_ms,
                                error=True,
                            )
                        filter_output = SnapshotFilterOutput(
                            useful_text_lines=[],
                            priority_element_ids=[],
                            notes="Filter unavailable; using full snapshot.",
                        )
                        self.state.last_filter_fingerprint = None
                    valid_ids = set(element_index.elements.keys())
                    priority_ids.extend(
                        _filter_ids_ordered(
                            tuple(filter_output.priority_element_ids or ()),
                            valid_ids=valid_ids,
                            avoid_ids=avoid_ids,
                        )
                    )
                    compressed_useful_lines = compress_text_lines(
                        list(filter_output.useful_text_lines or []),
                        max_lines=30,
                        max_chars=4000,
                    )
                    phrases = extract_instruction_phrases(compressed_useful_lines, oracle_hint=oracle_hint or None)
                    anchored_ids = match_phrases_to_elements(phrases, snapshot.elements, max_matches=15)
                    for sid in anchored_ids:
                        if sid in valid_ids and sid not in avoid_ids and sid not in priority_ids:
                            priority_ids.append(sid)
                    filter_output = SnapshotFilterOutput(
                        useful_text_lines=compressed_useful_lines,
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
                valid_ids = set(element_index.elements.keys())
                if filter_output:
                    compressed_useful_lines = compress_text_lines(
                        list(filter_output.useful_text_lines or []),
                        max_lines=30,
                        max_chars=4000,
                    )
                    priority_ids = _filter_ids_ordered(
                        tuple(filter_output.priority_element_ids or ()),
                        valid_ids=valid_ids,
                        avoid_ids=avoid_ids,
                    )
                    phrases = extract_instruction_phrases(compressed_useful_lines, oracle_hint=oracle_hint or None)
                    anchored_ids = match_phrases_to_elements(phrases, snapshot.elements, max_matches=15)
                    for sid in anchored_ids:
                        if sid in valid_ids and sid not in avoid_ids and sid not in priority_ids:
                            priority_ids.append(sid)
                    filter_output = SnapshotFilterOutput(
                        useful_text_lines=compressed_useful_lines,
                        priority_element_ids=priority_ids,
                        notes=filter_output.notes,
                    )
                    self.state.last_filter_output = filter_output
                useful_lines = filter_output.useful_text_lines if filter_output else []
                useful_block = "\n".join(useful_lines) if useful_lines else "None."
                priority_ids = list(filter_output.priority_element_ids if filter_output else [])
                if priority_ids:
                    priority_ids = _filter_ids_ordered(tuple(priority_ids), valid_ids=valid_ids, avoid_ids=avoid_ids)

                kept_ids = set(priority_ids) - avoid_ids
                # Guardrail: always show elements that just became enabled
                if newly_enabled_ids:
                    added_enabled = 0
                    for sid in newly_enabled_ids:
                        if sid in valid_ids and sid not in avoid_ids and sid not in kept_ids:
                            kept_ids.add(sid)
                            added_enabled += 1
                    if added_enabled:
                        logger.debug(
                            "auto-included %d newly-enabled elements: %s",
                            added_enabled,
                            [s for s in newly_enabled_ids if s in kept_ids],
                        )
                if oracle_intervened:
                    kept_ids = set(valid_ids) - avoid_ids
                    logger.debug(
                        "oracle_intervened: widening kept_ids=%s total_elements=%s avoided=%s",
                        len(kept_ids),
                        len(valid_ids),
                        len(avoid_ids),
                    )
                container_prefixes: set[tuple[tuple[int, str, str], ...]] = set()
                if kept_ids and not oracle_intervened:
                    for element in snapshot.elements:
                        if element.stable_id in kept_ids and element.parent_chain:
                            for prefix in _container_prefixes(element.parent_chain):
                                container_prefixes.add(prefix)
                if container_prefixes:
                    # Build index: chain prefix tuple -> list of element stable_ids
                    chain_index: dict[tuple, list[str]] = defaultdict(list)
                    for element in snapshot.elements:
                        if element.stable_id in kept_ids or element.stable_id in avoid_ids or not element.parent_chain:
                            continue
                        for depth in range(1, len(element.parent_chain) + 1):
                            chain_index[element.parent_chain[:depth]].append(element.stable_id)
                    added = 0
                    for prefix in container_prefixes:
                        for sid in chain_index.get(prefix, []):
                            if sid in avoid_ids:
                                continue
                            if sid not in kept_ids:
                                kept_ids.add(sid)
                                added += 1
                                if added >= _CONTAINER_EXPANSION_LIMIT:
                                    break
                        if added >= _CONTAINER_EXPANSION_LIMIT:
                            break
                    if added:
                        logger.debug("expanded pruned snapshot by %s elements via container prefixes", added)
                kept_ids -= avoid_ids
                if not kept_ids:
                    kept_ids = set(valid_ids) - avoid_ids
                    logger.debug("kept_ids empty after avoid; falling back to keep all non-avoided (%s)", len(kept_ids))
                pruned_elements = [
                    el for el in snapshot.elements if el.stable_id in kept_ids and el.stable_id not in avoid_ids
                ]
                pruned_snapshot = PageSnapshot(
                    url=snapshot.url,
                    title=snapshot.title,
                    elements=pruned_elements,
                    raw_text=snapshot.raw_text,
                    viewport_width=snapshot.viewport_width,
                    viewport_height=snapshot.viewport_height,
                )

                # ── Unified (single agent: plan + tools) ──
                if self.agent_config.unified:
                    if _is_step_timed_out():
                        prev_snapshot = snapshot
                        continue
                    if unified_agent is None:
                        raise RuntimeError("unified_agent is not initialized")

                    unified_query = ((" ".join(useful_lines) + " " + (self.agent_config.goal or "")).strip())[:600]
                    snapshot_text_unified = format_snapshot_for_llm(
                        pruned_snapshot,
                        max_elements=self.agent_config.max_elements,
                        query=unified_query or None,
                        priority_ids=priority_ids,
                        active_frame_id=self.state.active_frame_id,
                        class_sanitize_mode=self.agent_config.class_sanitize_mode,
                        class_sanitize_max_tokens=self.agent_config.class_sanitize_max_tokens,
                        class_sanitize_max_chars=self.agent_config.class_sanitize_max_chars,
                        class_sanitize_fallback_tokens=self.agent_config.class_sanitize_fallback_tokens,
                        attr_value_max_len=self.agent_config.snapshot_attr_value_max_len,
                    )
                    memory_text = _format_memory(self.state.memory, limit=self.agent_config.memory_steps)
                    unified_prompt = (
                        f"Overall goal: {self.agent_config.goal}\n\n"
                        f"Memory (recent):\n{memory_text}\n\n"
                        f"Filtered useful lines:\n{useful_block}\n\n"
                        f"Diff since prior snapshot:\n{diff_text}\n\n"
                        f"Page snapshot:\n{snapshot_text_unified}\n"
                        f"{oracle_hint}"
                    )
                    logger.debug(
                        "unified prompt step=%s chars=%s:\n%s",
                        self.state.step,
                        len(unified_prompt),
                        unified_prompt,
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
                    unified_usage_limits = UsageLimits(request_limit=self.agent_config.max_worker_tool_calls)
                    unified_started = time.perf_counter()
                    try:
                        with unified_agent.sequential_tool_calls():
                            unified_result = await _with_deadline(
                                unified_agent.run(
                                    unified_prompt, deps=deps, usage_limits=unified_usage_limits
                                ),
                                _step_deadline,
                            )
                    except UsageLimitExceeded:
                        unified_duration_ms = int((time.perf_counter() - unified_started) * 1000)
                        self.state.consecutive_tool_limit_steps += 1
                        logger.warning(
                            "  unified: tool call limit reached (%s), ending step (consecutive=%s)",
                            self.agent_config.max_worker_tool_calls,
                            self.state.consecutive_tool_limit_steps,
                        )
                        step_output = UnifiedStepOutput(
                            done=False,
                            step_goal="Attempt progress toward the overall goal",
                            summary=f"Tool call limit reached ({self.agent_config.max_worker_tool_calls})",
                            rationale="",
                        )
                    except TimeoutError:
                        unified_duration_ms = int((time.perf_counter() - unified_started) * 1000)
                        logger.warning("Unified timed out (step %s deadline)", self.state.step)
                        step_output = UnifiedStepOutput(
                            done=False,
                            step_goal="Attempt progress toward the overall goal",
                            summary="Unified timed out (step deadline exceeded)",
                            rationale="",
                        )
                    except (asyncio.CancelledError, KeyboardInterrupt):
                        raise
                    except Exception:
                        unified_duration_ms = int((time.perf_counter() - unified_started) * 1000)
                        err_name = type(sys.exc_info()[1]).__name__
                        logger.warning(
                            "Unified LLM error (step %s): %s", self.state.step, err_name, exc_info=True
                        )
                        metrics.emit(
                            "agent_call",
                            step=self.state.step,
                            agent="unified",
                            duration_ms=unified_duration_ms,
                            error=True,
                        )
                        step_output = UnifiedStepOutput(
                            done=False,
                            step_goal="Attempt progress toward the overall goal",
                            summary=f"Unified LLM error: {err_name}",
                            rationale="",
                        )
                    else:
                        self.state.consecutive_tool_limit_steps = 0
                        unified_duration_ms = int((time.perf_counter() - unified_started) * 1000)
                        unified_usage = usage_stats_from_result(unified_result)
                        unified_cost = cost_stats_from_result(
                            unified_result, self.llm_config.worker_model or self.llm_config.model
                        )
                        total_input_tokens += unified_usage.input_tokens
                        total_output_tokens += unified_usage.output_tokens
                        if unified_cost:
                            total_cost_usd = (total_cost_usd or 0.0) + unified_cost.cost_usd
                        metrics.emit(
                            "agent_call",
                            step=self.state.step,
                            agent="unified",
                            duration_ms=unified_duration_ms,
                            input_tokens=unified_usage.input_tokens,
                            output_tokens=unified_usage.output_tokens,
                            requests=unified_usage.requests,
                            tool_calls=unified_usage.tool_calls,
                            cost_usd=(unified_cost.cost_usd if unified_cost else None),
                            upstream_inference_cost_usd=(
                                unified_cost.upstream_inference_cost_usd if unified_cost else None
                            ),
                        )
                        step_output = unified_result.output

                    # Done-gate: override done=True when tool calls were attempted and none succeeded
                    if step_output.done and tool_tracker.success_count == 0 and tool_tracker.failure_count > 0:
                        logger.info("    done-gate: overriding done=True (no successful tool calls)")
                        step_output = step_output.model_copy(
                            update={"done": False, "summary": f"[no successful tools] {step_output.summary}"}
                        )

                    self.state.active_frame_id = tool_context.active_frame_id
                    tool_status = f"{tool_tracker.success_count} ok, {tool_tracker.failure_count} failed"
                    memory_entry = f"[step {self.state.step}, {tool_status}] {step_output.summary}"
                    self.state.last_summary = memory_entry
                    self.state.memory.append(memory_entry)
                    self.state.last_tool = tool_context.last_tool
                    self.state.last_element_id = tool_context.last_element_id
                    self.state.last_worker_goal = step_output.step_goal
                    self.state.last_step_was_puzzle = bool(
                        re.search(r"(?i)puzzle|math.*solve|solve.*puzzle", step_output.summary)
                    )
                    logger.debug("memory step=%s entries=%s", self.state.step, self.state.memory)

                    # ── Populate step trace ──
                    tool_limit_hit = step_output.summary.startswith("Tool call limit reached")
                    self.state.step_trace.append({
                        "step": self.state.step,
                        "url": getattr(session.page, "url", "") or "",
                        "goal": step_output.step_goal,
                        "summary": step_output.summary,
                        "diff_summary": diff_text.split("\n")[0] if diff_text else "",
                        "url_changed": prev_url != (getattr(session.page, "url", "") or ""),
                        "tool_calls": tool_tracker.calls_summary(),
                        "tool_limit_hit": tool_limit_hit,
                    })
                    logger.debug("step_trace step=%s entries=%s", self.state.step, self.state.step_trace)

                    logger.info(f"  unified: {unified_duration_ms}ms done={step_output.done}")
                    if step_output.step_goal:
                        logger.info(f"    goal: {step_output.step_goal}")
                    if step_output.summary:
                        logger.info(f"    summary: {step_output.summary}")
                    if step_output.rationale:
                        logger.info(f"    rationale: {step_output.rationale}")
                    logger.debug(
                        "unified output step=%s done=%s step_goal=%s summary=%s rationale=%s",
                        self.state.step,
                        step_output.done,
                        step_output.step_goal,
                        step_output.summary,
                        step_output.rationale,
                    )

                    metrics.emit(
                        "step_end",
                        step=self.state.step,
                        done=bool(step_output.done),
                        duration_ms=int((time.perf_counter() - step_started) * 1000),
                        **({"stop_reason": "done"} if step_output.done else {}),
                    )
                    step_duration_ms = int((time.perf_counter() - step_started) * 1000)
                    logger.info(f"Step {self.state.step} end {step_duration_ms}ms")
                    prev_snapshot = snapshot
                    if step_output.done:
                        stop_reason = "done"
                        break
                    continue

                # ── Orchestrator ──
                if _is_step_timed_out():
                    prev_snapshot = snapshot
                    continue
                prev_goal = self.state.last_worker_goal or ""
                orchestrator_query = ((" ".join(useful_lines) + " " + prev_goal).strip())[:600]
                snapshot_text_orchestrator = format_snapshot_for_llm(
                    pruned_snapshot,
                    max_elements=self.agent_config.max_elements,
                    query=orchestrator_query or None,
                    priority_ids=priority_ids,
                    active_frame_id=self.state.active_frame_id,
                    class_sanitize_mode=self.agent_config.class_sanitize_mode,
                    class_sanitize_max_tokens=self.agent_config.class_sanitize_max_tokens,
                    class_sanitize_max_chars=self.agent_config.class_sanitize_max_chars,
                    class_sanitize_fallback_tokens=self.agent_config.class_sanitize_fallback_tokens,
                    attr_value_max_len=self.agent_config.snapshot_attr_value_max_len,
                )
                memory_text = _format_memory(self.state.memory, limit=self.agent_config.memory_steps)
                tool_list = ", ".join(sorted(DEFAULT_WORKER_TOOLS))
                tool_constraint = "Only set goals achievable with these exact tools. Do not suggest inspecting elements, reading page content, taking screenshots, executing JavaScript, or any action not in this list."
                orchestrator_prompt = (
                    f"Overall goal: {self.agent_config.goal}\n\n"
                    f"Filtered useful lines:\n{useful_block}\n\n"
                    f"Diff since prior snapshot:\n{diff_text}\n\n"
                    f"Memory (recent):\n{memory_text}\n\n"
                    f"Worker tools: {tool_list}\n{tool_constraint}\n\n"
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
                decision = None
                for orchestrator_attempt in range(2):
                    try:
                        decision_result = await _with_deadline(orchestrator.run(orchestrator_prompt), _step_deadline)
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
                        break
                    except (asyncio.CancelledError, KeyboardInterrupt):
                        raise
                    except TimeoutError:
                        orchestrator_duration_ms = int((time.perf_counter() - orchestrator_started) * 1000)
                        _step_timed_out = True
                        logger.warning("Orchestrator timed out (step %s deadline)", self.state.step)
                        break  # don't retry on timeout
                    except Exception:
                        orchestrator_duration_ms = int((time.perf_counter() - orchestrator_started) * 1000)
                        if orchestrator_attempt == 0:
                            logger.warning("Orchestrator failed, retrying once", exc_info=True)
                            continue
                        logger.warning(
                            "Orchestrator failed twice (step %s), skipping step",
                            self.state.step,
                            exc_info=True,
                        )
                        metrics.emit(
                            "agent_call",
                            step=self.state.step,
                            agent="orchestrator",
                            duration_ms=orchestrator_duration_ms,
                            error=True,
                        )

                if decision is None:
                    self.state.memory.append(
                        f"[step {self.state.step}] Orchestrator LLM error, step skipped"
                    )
                    metrics.emit(
                        "step_end",
                        step=self.state.step,
                        done=False,
                        skipped=True,
                        duration_ms=int((time.perf_counter() - step_started) * 1000),
                    )
                    logger.info(f"Step {self.state.step} end (skipped — orchestrator error)")
                    prev_snapshot = snapshot
                    continue
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
                    stop_reason = "done"
                    logger.info(f"  orchestrator done: {decision.rationale or 'task complete'}")
                    metrics.emit(
                        "step_end",
                        step=self.state.step,
                        done=True,
                        stop_reason=stop_reason,
                        duration_ms=int((time.perf_counter() - step_started) * 1000),
                    )
                    break

                # ── Worker ──
                if _is_step_timed_out():
                    prev_snapshot = snapshot
                    continue
                snapshot_text_worker = format_snapshot_for_llm(
                    pruned_snapshot,
                    max_elements=self.agent_config.max_elements,
                    query=(decision.worker_goal or "")[:600] or None,
                    priority_ids=priority_ids,
                    active_frame_id=self.state.active_frame_id,
                    class_sanitize_mode=self.agent_config.class_sanitize_mode,
                    class_sanitize_max_tokens=self.agent_config.class_sanitize_max_tokens,
                    class_sanitize_max_chars=self.agent_config.class_sanitize_max_chars,
                    class_sanitize_fallback_tokens=self.agent_config.class_sanitize_fallback_tokens,
                    attr_value_max_len=self.agent_config.snapshot_attr_value_max_len,
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
                # Build worker cross-step context
                worker_context = ""
                if self.state.memory and self.agent_config.worker_context_steps > 0:
                    recent = self.state.memory[-self.agent_config.worker_context_steps:]
                    worker_context = "\n\nRecent steps:\n" + "\n".join(f"- {m}" for m in recent)
                worker_prompt = (
                    STEP_PROMPT.format(goal=decision.worker_goal)
                    + worker_context
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
                worker_usage_limits = UsageLimits(request_limit=self.agent_config.max_worker_tool_calls)
                worker_started = time.perf_counter()
                try:
                    with browser_worker.sequential_tool_calls():
                        worker_result = await _with_deadline(
                            browser_worker.run(worker_prompt, deps=deps, usage_limits=worker_usage_limits),
                            _step_deadline,
                        )
                except UsageLimitExceeded:
                    worker_duration_ms = int((time.perf_counter() - worker_started) * 1000)
                    self.state.consecutive_tool_limit_steps += 1
                    logger.warning(
                        "  worker: tool call limit reached (%s), ending step (consecutive=%s)",
                        self.agent_config.max_worker_tool_calls,
                        self.state.consecutive_tool_limit_steps,
                    )
                    step_output = StepOutput(
                        done=False,
                        summary=f"Tool call limit reached ({self.agent_config.max_worker_tool_calls})",
                    )
                except TimeoutError:
                    worker_duration_ms = int((time.perf_counter() - worker_started) * 1000)
                    logger.warning("Worker timed out (step %s deadline)", self.state.step)
                    step_output = StepOutput(
                        done=False,
                        summary="Worker timed out (step deadline exceeded)",
                    )
                except (asyncio.CancelledError, KeyboardInterrupt):
                    raise
                except Exception:
                    worker_duration_ms = int((time.perf_counter() - worker_started) * 1000)
                    err_name = type(sys.exc_info()[1]).__name__
                    logger.warning(
                        "Worker LLM error (step %s): %s", self.state.step, err_name, exc_info=True
                    )
                    metrics.emit(
                        "agent_call",
                        step=self.state.step,
                        agent="browser_worker",
                        duration_ms=worker_duration_ms,
                        error=True,
                    )
                    step_output = StepOutput(
                        done=False,
                        summary=f"Worker LLM error: {err_name}",
                    )
                else:
                    self.state.consecutive_tool_limit_steps = 0
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

                # Done-gate: override done=True when no tool calls succeeded
                if step_output.done and tool_tracker.success_count == 0:
                    logger.info("    done-gate: overriding done=True (no successful tool calls)")
                    step_output = step_output.model_copy(
                        update={"done": False, "summary": f"[no successful tools] {step_output.summary}"}
                    )
                self.state.active_frame_id = tool_context.active_frame_id
                tool_status = f"{tool_tracker.success_count} ok, {tool_tracker.failure_count} failed"
                memory_entry = f"[step {self.state.step}, {tool_status}] {step_output.summary}"
                self.state.last_summary = memory_entry
                self.state.memory.append(memory_entry)
                self.state.last_tool = tool_context.last_tool
                self.state.last_element_id = tool_context.last_element_id
                self.state.last_step_was_puzzle = bool(
                    re.search(r"(?i)puzzle|math.*solve|solve.*puzzle", step_output.summary)
                )
                logger.debug("memory step=%s entries=%s", self.state.step, self.state.memory)

                # ── Populate step trace ──
                tool_limit_hit = step_output.summary.startswith("Tool call limit reached")
                self.state.step_trace.append({
                    "step": self.state.step,
                    "url": getattr(session.page, "url", "") or "",
                    "goal": decision.worker_goal,
                    "summary": step_output.summary,
                    "diff_summary": diff_text.split("\n")[0] if diff_text else "",
                    "url_changed": prev_url != (getattr(session.page, "url", "") or ""),
                    "tool_calls": tool_tracker.calls_summary(),
                    "tool_limit_hit": tool_limit_hit,
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
            else:
                stop_reason = stop_reason or "max_steps"
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception:
            logger.error(
                "Step %s failed with unhandled error", self.state.step, exc_info=True
            )
            self.state.last_summary = (
                f"Step {self.state.step} crashed: {type(sys.exc_info()[1]).__name__}"
            )
            stop_reason = "error"
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
            retry_wait_ms = int(sum(m.total_retry_wait_seconds for m in resilient_models) * 1000)
            active_duration_ms = run_duration_ms - retry_wait_ms
            effective_stop_reason = "interrupted" if interrupted else stop_reason

            try:
                metrics.emit("run_end", duration_ms=run_duration_ms, interrupted=interrupted)
            except Exception:
                pass

            try:
                write_run_summary(
                    log_dir=run_dir,
                    run_id=run_id,
                    summary={
                        "git_commit": git_commit,
                        "provider": self.llm_config.provider,
                        "model": self.llm_config.model,
                        "worker_model": self.llm_config.worker_model,
                        "filter_model": self.llm_config.filter_model,
                        "oracle_model": self.llm_config.oracle_model,
                        "duration_ms": run_duration_ms,
                        "retry_wait_ms": retry_wait_ms,
                        "active_duration_ms": active_duration_ms,
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
                    "Run end run_id=%s duration_ms=%s retry_wait_ms=%s active_duration_ms=%s steps=%s total_tokens=%s cost_usd=%s%s",
                    run_id,
                    run_duration_ms,
                    retry_wait_ms,
                    active_duration_ms,
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
