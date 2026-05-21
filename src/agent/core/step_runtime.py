"""Single-step runtime for externally owned browser pages."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import logging
import re
import sys
import time
from collections import defaultdict
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.usage import UsageLimits

from src.agent.browser.external import ExternalBrowserSession
from src.agent.capture.page_saver import PageSaver
from src.agent.config import AgentConfig, LLMConfig
from src.agent.context.handlers import cleanup_handler_attributes, extract_handlers
from src.agent.context.scroll_containers import (
    cleanup_scroll_container_attributes,
    extract_scroll_containers,
)
from src.agent.context.snapshot import (
    PageSnapshot,
    build_element_index,
    capture_snapshot,
    format_snapshot_for_llm,
)
from src.agent.core import agent as runtime
from src.agent.core.pruning import (
    extract_instruction_phrases,
    extract_stable_ids,
    match_phrases_to_elements,
)
from src.agent.core.text_compress import compress_text_lines
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
    UnifiedStepOutput,
)
from src.agent.prompts.system import STEP_PROMPT
from src.agent.tools import semantic

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeStepResult:
    """Structured result from one internal agent step."""

    step: int
    done: bool
    summary: str
    stop_reason: str | None
    worker_goal: str | None
    tool_calls: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    duration_ms: int
    log_dir: str | None
    trace: list[dict[str, Any]]

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class _RuntimeAgents:
    orchestrator: Any
    snapshot_filter: Any
    oracle: Any
    browser_worker: Any
    unified: Any | None


class BrowserAgentStepRuntime:
    """Runs the existing agent pipeline one step at a time on an external page."""

    def __init__(self, agent_config: AgentConfig, llm_config: LLMConfig) -> None:
        self.agent_config = agent_config
        self.llm_config = llm_config
        self.state = runtime.AgentState()
        self._run_id: str | None = None
        self._run_dir: str | None = None
        self._metrics: MetricsRecorder | None = None
        self._page_saver: PageSaver | None = None
        self._run_started: float | None = None
        self._git_commit = "unknown"
        self._agents: _RuntimeAgents | None = None
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_cost_usd: float | None = None
        self._prev_snapshot: PageSnapshot | None = None
        self._closed = False

    @property
    def run_dir(self) -> str | None:
        return self._run_dir

    def _ensure_started(self, goal: str, current_url: str) -> None:
        if self._agents is not None:
            return
        if not goal:
            raise ValueError("goal is required")

        self.agent_config = replace(
            self.agent_config,
            goal=goal,
            target_url=self.agent_config.target_url or current_url,
        )
        self._run_id = new_run_id()
        self._run_dir = prepare_run_dir(
            self.agent_config.log_dir,
            self._run_id,
            max_log_runs=self.agent_config.max_log_runs,
        )
        runtime._setup_logging(
            self._run_dir,
            level=self.agent_config.log_level,
            color=self.agent_config.color_logs,
        )
        self._metrics = MetricsRecorder(
            log_dir=self._run_dir,
            run_id=self._run_id,
            enabled=self.agent_config.metrics_enabled,
        )
        self._page_saver = PageSaver(self._run_dir, self._run_id) if self.agent_config.save_pages else None
        self._run_started = time.perf_counter()
        self._git_commit = get_git_commit()

        model = runtime._build_model(self.llm_config)
        model_settings = runtime._model_settings(self.llm_config)
        worker_model = (
            runtime._build_model(self.llm_config, model_override=self.llm_config.worker_model)
            if self.llm_config.worker_model
            else model
        )
        filter_model = (
            runtime._build_model(self.llm_config, model_override=self.llm_config.filter_model)
            if self.llm_config.filter_model
            else model
        )
        oracle_model = (
            runtime._build_model(self.llm_config, model_override=self.llm_config.oracle_model)
            if self.llm_config.oracle_model
            else model
        )
        keep_recent = self.agent_config.keep_recent_tool_rounds
        unified_agent: Agent[Any, UnifiedStepOutput] | None = None
        if self.agent_config.unified:
            unified_agent = runtime.build_unified_agent(
                worker_model,
                model_settings=model_settings,
                keep_recent_tool_rounds=keep_recent,
            )
        self._agents = _RuntimeAgents(
            orchestrator=runtime.build_orchestrator_agent(model, model_settings=model_settings),
            snapshot_filter=runtime.build_snapshot_filter_agent(
                filter_model, model_settings=model_settings
            ),
            oracle=runtime.build_oracle_agent(oracle_model, model_settings=model_settings),
            browser_worker=runtime.build_browser_worker_agent(
                worker_model,
                model_settings=model_settings,
                keep_recent_tool_rounds=keep_recent,
            ),
            unified=unified_agent,
        )

        self._emit(
            "run_start",
            target_url=self.agent_config.target_url,
            goal=self.agent_config.goal,
            max_steps=self.agent_config.max_steps,
            model=self.llm_config.model,
            worker_model=self.llm_config.worker_model,
            filter_model=self.llm_config.filter_model,
            oracle_model=self.llm_config.oracle_model,
            provider=self.llm_config.provider,
            git_commit=self._git_commit,
            harness="agentlab_browsergym",
            unified=self.agent_config.unified,
        )
        logger.info(
            "External run start run_id=%s url=%s max_steps=%s model=%s",
            self._run_id,
            self.agent_config.target_url,
            self.agent_config.max_steps,
            self.llm_config.model,
        )

    def _emit(self, event: str, **fields: Any) -> None:
        if self._metrics is not None:
            self._metrics.emit(event, **fields)

    def _record_agent_call(
        self,
        *,
        result: Any,
        step: int,
        agent: str,
        duration_ms: int,
        model_name: str,
    ) -> None:
        usage = usage_stats_from_result(result)
        cost = cost_stats_from_result(result, model_name)
        self._total_input_tokens += usage.input_tokens
        self._total_output_tokens += usage.output_tokens
        if cost:
            self._total_cost_usd = (self._total_cost_usd or 0.0) + cost.cost_usd
        self._emit(
            "agent_call",
            step=step,
            agent=agent,
            duration_ms=duration_ms,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            requests=usage.requests,
            tool_calls=usage.tool_calls,
            cost_usd=(cost.cost_usd if cost else None),
            upstream_inference_cost_usd=(
                cost.upstream_inference_cost_usd if cost else None
            ),
        )

    async def close(self, *, stop_reason: str | None = None, interrupted: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        run_duration_ms = int((time.perf_counter() - (self._run_started or time.perf_counter())) * 1000)
        effective_stop_reason = "interrupted" if interrupted else stop_reason
        try:
            self._emit("run_end", duration_ms=run_duration_ms, interrupted=interrupted)
            self._write_run_summary(
                stop_reason=effective_stop_reason,
                duration_ms=run_duration_ms,
            )
        finally:
            if self._metrics is not None:
                self._metrics.close()
            runtime._teardown_logging()

    def _write_run_summary(
        self, *, stop_reason: str | None = None, duration_ms: int | None = None
    ) -> None:
        if not self._run_dir or not self._run_id:
            return
        effective_duration_ms = duration_ms
        if effective_duration_ms is None and self._run_started is not None:
            effective_duration_ms = int((time.perf_counter() - self._run_started) * 1000)
        try:
            write_run_summary(
                log_dir=self._run_dir,
                run_id=self._run_id,
                summary={
                    "git_commit": self._git_commit,
                    "provider": self.llm_config.provider,
                    "model": self.llm_config.model,
                    "worker_model": self.llm_config.worker_model,
                    "filter_model": self.llm_config.filter_model,
                    "oracle_model": self.llm_config.oracle_model,
                    "duration_ms": effective_duration_ms,
                    "steps": self.state.step,
                    "last_summary": self.state.last_summary,
                    "stop_reason": stop_reason,
                    "input_tokens": self._total_input_tokens,
                    "output_tokens": self._total_output_tokens,
                    "total_tokens": self._total_input_tokens + self._total_output_tokens,
                    "cost_usd": self._total_cost_usd,
                    "harness": "agentlab_browsergym",
                    "unified": self.agent_config.unified,
                },
            )
        except Exception:
            logger.warning("Failed to write external run summary", exc_info=True)

    async def run_one_step(
        self, session: ExternalBrowserSession, *, goal: str | None = None
    ) -> RuntimeStepResult:
        page_url = getattr(session.page, "url", "") or ""
        self._ensure_started(goal or self.agent_config.goal or "", page_url)
        assert self._agents is not None

        if self.state.step >= self.agent_config.max_steps:
            self._write_run_summary(stop_reason="max_steps")
            return RuntimeStepResult(
                step=self.state.step,
                done=True,
                summary="Internal max_steps reached.",
                stop_reason="max_steps",
                worker_goal=self.state.last_worker_goal,
                tool_calls="",
                input_tokens=self._total_input_tokens,
                output_tokens=self._total_output_tokens,
                cost_usd=self._total_cost_usd,
                duration_ms=0,
                log_dir=self._run_dir,
                trace=list(self.state.step_trace),
            )

        self.state.step += 1
        step_started = time.perf_counter()
        stop_reason: str | None = None
        logger.info("")
        logger.info(runtime._STEP_SEPARATOR)
        logger.info("Step %s start (external browser)", self.state.step)

        try:
            await session.page.wait_for_load_state("domcontentloaded")
            await session.page.wait_for_load_state(
                "networkidle", timeout=self.agent_config.networkidle_timeout_ms
            )
        except Exception:
            pass

        snapshot, snapshot_duration_ms, handlers_count = await self._capture_step_snapshot(
            session
        )
        self._emit(
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
                self._emit(
                    "cdp_call",
                    step=self.state.step,
                    name=name,
                    duration_ms=int(duration_ms),
                    **(snapshot.diagnostics.size_hints or {}),
                )
        logger.info(
            "  snapshot: %sms elements=%s handlers=%s url=%s",
            snapshot_duration_ms,
            len(snapshot.elements),
            handlers_count,
            snapshot.url,
        )

        prev_priority_ids = (
            self.state.last_filter_output.priority_element_ids
            if self.state.last_filter_output
            else None
        )
        diff_text, _diff_ids, newly_enabled_ids = runtime._snapshot_diff(
            self._prev_snapshot,
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
        page_fingerprint = runtime._page_fingerprint(
            snapshot,
            raw_text_limit=self.agent_config.raw_text_limit_fingerprint,
        )
        self._update_progress(snapshot, page_fingerprint)

        if self._page_saver:
            await self._page_saver.capture_page(
                session.page,
                self.state.step,
                snapshot.url or "",
                snapshot.title or "",
                page_fingerprint,
            )

        if self.state.no_progress_steps >= self.agent_config.unchanged_abort_threshold:
            stop_reason = "unchanged_fingerprint_abort"
            self._emit(
                "step_end",
                step=self.state.step,
                done=True,
                stop_reason=stop_reason,
                duration_ms=int((time.perf_counter() - step_started) * 1000),
            )
            self._prev_snapshot = snapshot
            return self._result(
                step_started=step_started,
                done=True,
                summary="Internal unchanged-fingerprint abort.",
                stop_reason=stop_reason,
                worker_goal=self.state.last_worker_goal,
                tool_calls="",
            )

        element_index = build_element_index(snapshot)
        tool_timing = semantic.ToolTimingConfig(
            settle_ms=self.agent_config.settle_ms,
            draw_settle_ms=self.agent_config.draw_settle_ms,
            draw_point_interval_ms=self.agent_config.draw_point_interval_ms,
            drag_phase_interval_ms=self.agent_config.drag_phase_interval_ms,
        )
        tool_context = semantic.build_tool_context(
            session,
            element_index,
            active_frame_id=self.state.active_frame_id,
            timing=tool_timing,
        )

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
        step_deadline = (
            asyncio.get_running_loop().time() + self.agent_config.step_timeout_seconds
        )

        oracle_hint, advice = await self._maybe_run_oracle(
            full_tree_text=full_tree_text,
            step_deadline=step_deadline,
        )
        avoid_ids: set[str] = set()
        if advice:
            for entry in advice.avoid or []:
                avoid_ids |= extract_stable_ids(entry)
        oracle_intervened = bool(
            self.agent_config.widen_on_oracle and advice and not advice.all_clear
        )

        filter_output, priority_ids = await self._run_filter(
            snapshot=snapshot,
            element_index=element_index,
            full_tree_text=full_tree_text,
            diff_text=diff_text,
            oracle_hint=oracle_hint,
            avoid_ids=avoid_ids,
            page_fingerprint=page_fingerprint,
            step_deadline=step_deadline,
        )
        useful_lines = filter_output.useful_text_lines if filter_output else []
        useful_block = "\n".join(useful_lines) if useful_lines else "None."
        pruned_snapshot = self._build_pruned_snapshot(
            snapshot=snapshot,
            element_index=element_index,
            filter_output=filter_output,
            oracle_hint=oracle_hint,
            avoid_ids=avoid_ids,
            oracle_intervened=oracle_intervened,
            newly_enabled_ids=newly_enabled_ids,
        )
        priority_ids = runtime._filter_ids_ordered(
            tuple(priority_ids),
            valid_ids=set(element_index.elements.keys()),
            avoid_ids=avoid_ids,
        )

        if self.agent_config.unified:
            result = await self._run_unified_step(
                session=session,
                snapshot=pruned_snapshot,
                tool_context=tool_context,
                useful_lines=list(useful_lines),
                useful_block=useful_block,
                diff_text=diff_text,
                oracle_hint=oracle_hint,
                priority_ids=priority_ids,
                step_deadline=step_deadline,
                step_started=step_started,
            )
            self._prev_snapshot = snapshot
            return result

        result = await self._run_orchestrator_worker_step(
            session=session,
            snapshot=pruned_snapshot,
            tool_context=tool_context,
            useful_lines=list(useful_lines),
            useful_block=useful_block,
            diff_text=diff_text,
            oracle_hint=oracle_hint,
            priority_ids=priority_ids,
            step_deadline=step_deadline,
            step_started=step_started,
        )
        self._prev_snapshot = snapshot
        return result

    async def _capture_step_snapshot(
        self, session: ExternalBrowserSession
    ) -> tuple[PageSnapshot, int, int]:
        handler_map: dict[str, dict[str, str]] | None = None
        handlers_count = 0
        if self.agent_config.handlers_enabled:
            handler_started = time.perf_counter()
            handler_map = await extract_handlers(session.page)
            handlers_count = len(handler_map) if handler_map else 0
            self._emit(
                "handler_extraction",
                step=self.state.step,
                duration_ms=int((time.perf_counter() - handler_started) * 1000),
                handlers=handlers_count,
            )

        scroll_marked = 0
        if self.agent_config.scroll_containers_enabled:
            scroll_started = time.perf_counter()
            scroll_marked = await extract_scroll_containers(session.page)
            self._emit(
                "scroll_container_marking",
                step=self.state.step,
                duration_ms=int((time.perf_counter() - scroll_started) * 1000),
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

        if await runtime._fix_stale_puzzle_state(
            session.page, snapshot.raw_text, self.state.last_step_was_puzzle
        ):
            snapshot = await self._recapture_snapshot(session, handler_map)
        if await runtime._fix_recursive_iframe_bug(session.page, snapshot.raw_text):
            snapshot = await self._recapture_snapshot(session, handler_map)
        if await runtime._fix_final_step_code_bug(session.page, snapshot.raw_text):
            snapshot = await self._recapture_snapshot(session, handler_map)
        return snapshot, snapshot_duration_ms, handlers_count

    async def _recapture_snapshot(
        self,
        session: ExternalBrowserSession,
        handler_map: dict[str, dict[str, str]] | None,
    ) -> PageSnapshot:
        return await capture_snapshot(
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

    def _update_progress(self, snapshot: PageSnapshot, page_fingerprint: str) -> None:
        if self.agent_config.progress_fingerprint_enabled:
            progress_fingerprint = runtime._progress_fingerprint(
                snapshot,
                max_elements=self.agent_config.progress_fingerprint_max_elements,
                raw_text_lines=self.agent_config.progress_fingerprint_raw_lines,
                raw_text_chars=self.agent_config.progress_fingerprint_raw_chars,
                raw_text_scan_cap=self.agent_config.raw_text_scan_cap,
                raw_text_line_max_len=self.agent_config.raw_text_line_max_len,
                raw_text_dedupe_prefix_len=self.agent_config.raw_text_dedupe_prefix_len,
                raw_text_dedupe_suffix_len=self.agent_config.raw_text_dedupe_suffix_len,
            )
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

    async def _maybe_run_oracle(
        self, *, full_tree_text: str, step_deadline: float
    ) -> tuple[str, OracleAdvice | None]:
        assert self._agents is not None
        oracle_hint = ""
        advice: OracleAdvice | None = None
        should_call_oracle = (
            (
                self.agent_config.oracle_interval > 0
                and self.state.step % self.agent_config.oracle_interval == 0
            )
            or self.state.no_progress_steps >= self.agent_config.stuck_threshold
            or self.state.consecutive_tool_limit_steps >= 2
        )
        if not should_call_oracle or not self.state.step_trace:
            return oracle_hint, advice

        trace_text = runtime._format_step_trace(
            self.state.step_trace, window=self.agent_config.oracle_trace_window
        )
        tool_list = ", ".join(sorted(runtime.DEFAULT_WORKER_TOOLS))
        tool_constraint = (
            "Only recommend actions using these exact tools. Do not suggest "
            "inspecting elements, reading page content, taking screenshots, "
            "executing JavaScript, or any action not in this list."
        )
        oracle_prompt = (
            f"Overall goal: {self.agent_config.goal}\n\n"
            f"Current step: {self.state.step}\n"
            f"No-progress steps: {self.state.no_progress_steps}\n"
            f"Consecutive tool-limit-hit steps: {self.state.consecutive_tool_limit_steps}\n\n"
            f"Execution trace:\n{trace_text}\n\n"
            f"Worker tools: {tool_list}\n{tool_constraint}\n\n"
            f"Page snapshot (full interactive element tree):\n{full_tree_text}\n"
        )
        oracle_started = time.perf_counter()
        try:
            oracle_result = await runtime._with_deadline(
                self._agents.oracle.run(oracle_prompt), step_deadline
            )
        except TimeoutError:
            logger.warning("Oracle timed out (step %s deadline)", self.state.step)
            return oracle_hint, advice
        except Exception:
            logger.warning("Oracle advisor failed", exc_info=True)
            return oracle_hint, advice

        oracle_duration_ms = int((time.perf_counter() - oracle_started) * 1000)
        self._record_agent_call(
            result=oracle_result,
            step=self.state.step,
            agent="oracle",
            duration_ms=oracle_duration_ms,
            model_name=self.llm_config.oracle_model or self.llm_config.model,
        )
        advice = oracle_result.output
        if not advice.all_clear:
            avoid_str = ", ".join(advice.avoid) if advice.avoid else "None"
            oracle_hint = (
                "\n\nORACLE DIRECTIVE:\n"
                f"Diagnosis: {advice.diagnosis}\n"
                f"Recommendation: {advice.recommendation}\n"
                f"Avoid: {avoid_str}"
            )
            self.state.last_filter_fingerprint = None
            self.state.no_progress_steps = max(
                0, self.state.no_progress_steps - self.agent_config.stuck_threshold
            )
        logger.info(
            "  oracle: %sms all_clear=%s diagnosis=%s",
            oracle_duration_ms,
            advice.all_clear,
            advice.diagnosis[:80],
        )
        return oracle_hint, advice

    async def _run_filter(
        self,
        *,
        snapshot: PageSnapshot,
        element_index: Any,
        full_tree_text: str,
        diff_text: str,
        oracle_hint: str,
        avoid_ids: set[str],
        page_fingerprint: str,
        step_deadline: float,
    ) -> tuple[SnapshotFilterOutput, list[str]]:
        assert self._agents is not None
        filter_output = self.state.last_filter_output
        filter_duration_ms = 0
        if self.state.last_filter_fingerprint != page_fingerprint or filter_output is None:
            raw_lines = runtime._select_raw_text_lines(
                list(snapshot.raw_text),
                limit=self.agent_config.raw_text_limit_prompt,
                scan_cap=self.agent_config.raw_text_scan_cap,
                max_len=self.agent_config.raw_text_line_max_len,
                dedupe_prefix_len=self.agent_config.raw_text_dedupe_prefix_len,
                dedupe_suffix_len=self.agent_config.raw_text_dedupe_suffix_len,
            )
            raw_lines = compress_text_lines(raw_lines, max_lines=60, max_chars=8000)
            raw_text_block = "\n".join(raw_lines) if raw_lines else "None."
            filter_prompt = (
                f"Overall goal: {self.agent_config.goal}\n\n"
                f"Last worker goal: {self.state.last_worker_goal or 'None.'}\n"
                f"Last step summary: {self.state.last_summary or 'None.'}\n\n"
                f"Diff since prior snapshot:\n{diff_text}\n\n"
                + (f"Oracle advice:\n{oracle_hint}\n\n" if oracle_hint else "")
                + f"Page snapshot (full interactive element tree):\n{full_tree_text}\n\n"
                f"Page text lines:\n{raw_text_block}\n"
            )
            filter_started = time.perf_counter()
            try:
                filter_result = await runtime._with_deadline(
                    self._agents.snapshot_filter.run(filter_prompt), step_deadline
                )
                filter_duration_ms = int((time.perf_counter() - filter_started) * 1000)
                self._record_agent_call(
                    result=filter_result,
                    step=self.state.step,
                    agent="snapshot_filter",
                    duration_ms=filter_duration_ms,
                    model_name=self.llm_config.filter_model or self.llm_config.model,
                )
                filter_output = filter_result.output
            except Exception:
                filter_duration_ms = int((time.perf_counter() - filter_started) * 1000)
                logger.warning(
                    "Filter failed (step %s), using unfiltered snapshot",
                    self.state.step,
                    exc_info=True,
                )
                self._emit(
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
        compressed_useful_lines = compress_text_lines(
            list(filter_output.useful_text_lines or []),
            max_lines=30,
            max_chars=4000,
        )
        priority_ids = runtime._filter_ids_ordered(
            tuple(filter_output.priority_element_ids or ()),
            valid_ids=valid_ids,
            avoid_ids=avoid_ids,
        )
        phrases = extract_instruction_phrases(
            compressed_useful_lines, oracle_hint=oracle_hint or None
        )
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
            "  filter: %sms useful_lines=%s priority_ids=%s total_elements=%s",
            filter_duration_ms,
            len(filter_output.useful_text_lines or []),
            len(priority_ids),
            len(snapshot.elements),
        )
        return filter_output, priority_ids

    def _build_pruned_snapshot(
        self,
        *,
        snapshot: PageSnapshot,
        element_index: Any,
        filter_output: SnapshotFilterOutput,
        oracle_hint: str,
        avoid_ids: set[str],
        oracle_intervened: bool,
        newly_enabled_ids: list[str],
    ) -> PageSnapshot:
        valid_ids = set(element_index.elements.keys())
        useful_lines = compress_text_lines(
            list(filter_output.useful_text_lines or []),
            max_lines=30,
            max_chars=4000,
        )
        priority_ids = runtime._filter_ids_ordered(
            tuple(filter_output.priority_element_ids or ()),
            valid_ids=valid_ids,
            avoid_ids=avoid_ids,
        )
        phrases = extract_instruction_phrases(useful_lines, oracle_hint=oracle_hint or None)
        anchored_ids = match_phrases_to_elements(phrases, snapshot.elements, max_matches=15)
        for sid in anchored_ids:
            if sid in valid_ids and sid not in avoid_ids and sid not in priority_ids:
                priority_ids.append(sid)

        kept_ids = set(priority_ids) - avoid_ids
        for sid in newly_enabled_ids:
            if sid in valid_ids and sid not in avoid_ids:
                kept_ids.add(sid)
        if oracle_intervened:
            kept_ids = set(valid_ids) - avoid_ids

        container_prefixes: set[tuple[tuple[int, str, str], ...]] = set()
        if kept_ids and not oracle_intervened:
            for element in snapshot.elements:
                if element.stable_id in kept_ids and element.parent_chain:
                    for prefix in runtime._container_prefixes(element.parent_chain):
                        container_prefixes.add(prefix)
        if container_prefixes:
            chain_index: dict[tuple, list[str]] = defaultdict(list)
            for element in snapshot.elements:
                if (
                    element.stable_id in kept_ids
                    or element.stable_id in avoid_ids
                    or not element.parent_chain
                ):
                    continue
                for depth in range(1, len(element.parent_chain) + 1):
                    chain_index[element.parent_chain[:depth]].append(element.stable_id)
            added = 0
            for prefix in container_prefixes:
                for sid in chain_index.get(prefix, []):
                    if sid not in avoid_ids and sid not in kept_ids:
                        kept_ids.add(sid)
                        added += 1
                        if added >= runtime._CONTAINER_EXPANSION_LIMIT:
                            break
                if added >= runtime._CONTAINER_EXPANSION_LIMIT:
                    break
        kept_ids -= avoid_ids
        if not kept_ids:
            kept_ids = set(valid_ids) - avoid_ids
        pruned_elements = [
            el
            for el in snapshot.elements
            if el.stable_id in kept_ids and el.stable_id not in avoid_ids
        ]
        return PageSnapshot(
            url=snapshot.url,
            title=snapshot.title,
            elements=pruned_elements,
            raw_text=snapshot.raw_text,
            viewport_width=snapshot.viewport_width,
            viewport_height=snapshot.viewport_height,
        )

    async def _run_unified_step(
        self,
        *,
        session: ExternalBrowserSession,
        snapshot: PageSnapshot,
        tool_context: semantic.ToolContext,
        useful_lines: list[str],
        useful_block: str,
        diff_text: str,
        oracle_hint: str,
        priority_ids: list[str],
        step_deadline: float,
        step_started: float,
    ) -> RuntimeStepResult:
        assert self._agents is not None and self._agents.unified is not None
        snapshot_text = format_snapshot_for_llm(
            snapshot,
            max_elements=self.agent_config.max_elements,
            query=((" ".join(useful_lines) + " " + (self.agent_config.goal or "")).strip())[:600]
            or None,
            priority_ids=priority_ids,
            active_frame_id=self.state.active_frame_id,
            class_sanitize_mode=self.agent_config.class_sanitize_mode,
            class_sanitize_max_tokens=self.agent_config.class_sanitize_max_tokens,
            class_sanitize_max_chars=self.agent_config.class_sanitize_max_chars,
            class_sanitize_fallback_tokens=self.agent_config.class_sanitize_fallback_tokens,
            attr_value_max_len=self.agent_config.snapshot_attr_value_max_len,
        )
        prompt = (
            f"Overall goal: {self.agent_config.goal}\n\n"
            f"Memory (recent):\n{runtime._format_memory(self.state.memory, limit=self.agent_config.memory_steps)}\n\n"
            f"Filtered useful lines:\n{useful_block}\n\n"
            f"Diff since prior snapshot:\n{diff_text}\n\n"
            f"Page snapshot:\n{snapshot_text}\n"
            f"{oracle_hint}"
        )
        tool_tracker = runtime.ToolCallTracker()
        deps = runtime.WorkerDeps(
            tool_context=tool_context,
            metrics=self._metrics or MetricsRecorder(log_dir="/tmp", run_id="disabled", enabled=False),
            step=self.state.step,
            tool_tracker=tool_tracker,
            allowed_tools=runtime.DEFAULT_WORKER_TOOLS,
        )
        prev_url = getattr(session.page, "url", "") or ""
        step_output: UnifiedStepOutput
        duration_ms = 0
        try:
            with self._agents.unified.sequential_tool_calls():
                started = time.perf_counter()
                unified_result = await runtime._with_deadline(
                    self._agents.unified.run(
                        prompt,
                        deps=deps,
                        usage_limits=UsageLimits(
                            request_limit=self.agent_config.max_worker_tool_calls
                        ),
                    ),
                    step_deadline,
                )
                duration_ms = int((time.perf_counter() - started) * 1000)
        except UsageLimitExceeded:
            self.state.consecutive_tool_limit_steps += 1
            step_output = UnifiedStepOutput(
                done=False,
                step_goal="Attempt progress toward the overall goal",
                summary=f"Tool call limit reached ({self.agent_config.max_worker_tool_calls})",
                rationale="",
            )
        except TimeoutError:
            step_output = UnifiedStepOutput(
                done=False,
                step_goal="Attempt progress toward the overall goal",
                summary="Unified timed out (step deadline exceeded)",
                rationale="",
            )
        except Exception:
            err_name = type(sys.exc_info()[1]).__name__
            logger.warning("Unified LLM error (step %s): %s", self.state.step, err_name, exc_info=True)
            step_output = UnifiedStepOutput(
                done=False,
                step_goal="Attempt progress toward the overall goal",
                summary=f"Unified LLM error: {err_name}",
                rationale="",
            )
        else:
            self.state.consecutive_tool_limit_steps = 0
            self._record_agent_call(
                result=unified_result,
                step=self.state.step,
                agent="unified",
                duration_ms=duration_ms,
                model_name=self.llm_config.worker_model or self.llm_config.model,
            )
            step_output = unified_result.output

        if step_output.done and tool_tracker.success_count == 0:
            prefix = "[no successful tools]" if tool_tracker.failure_count > 0 else "[no tools executed]"
            step_output = step_output.model_copy(
                update={"done": False, "summary": f"{prefix} {step_output.summary}"}
            )
        self._finish_tool_step(
            step_output=step_output,
            worker_goal=step_output.step_goal,
            tool_context=tool_context,
            tool_tracker=tool_tracker,
            diff_text=diff_text,
            prev_url=prev_url,
        )
        self._emit(
            "step_end",
            step=self.state.step,
            done=bool(step_output.done),
            duration_ms=int((time.perf_counter() - step_started) * 1000),
            **({"stop_reason": "done"} if step_output.done else {}),
        )
        return self._result(
            step_started=step_started,
            done=bool(step_output.done),
            summary=step_output.summary,
            stop_reason="done" if step_output.done else None,
            worker_goal=step_output.step_goal,
            tool_calls=tool_tracker.calls_summary(),
        )

    async def _run_orchestrator_worker_step(
        self,
        *,
        session: ExternalBrowserSession,
        snapshot: PageSnapshot,
        tool_context: semantic.ToolContext,
        useful_lines: list[str],
        useful_block: str,
        diff_text: str,
        oracle_hint: str,
        priority_ids: list[str],
        step_deadline: float,
        step_started: float,
    ) -> RuntimeStepResult:
        decision = await self._run_orchestrator(
            snapshot=snapshot,
            useful_lines=useful_lines,
            useful_block=useful_block,
            diff_text=diff_text,
            oracle_hint=oracle_hint,
            priority_ids=priority_ids,
            step_deadline=step_deadline,
        )
        if decision is None:
            summary = "Orchestrator LLM error, step skipped."
            self.state.memory.append(f"[step {self.state.step}] {summary}")
            self._emit(
                "step_end",
                step=self.state.step,
                done=False,
                skipped=True,
                duration_ms=int((time.perf_counter() - step_started) * 1000),
            )
            return self._result(
                step_started=step_started,
                done=False,
                summary=summary,
                stop_reason=None,
                worker_goal=self.state.last_worker_goal,
                tool_calls="",
            )
        self.state.last_worker_goal = decision.worker_goal
        if decision.done:
            self._emit(
                "step_end",
                step=self.state.step,
                done=True,
                stop_reason="done",
                duration_ms=int((time.perf_counter() - step_started) * 1000),
            )
            return self._result(
                step_started=step_started,
                done=True,
                summary=decision.rationale or "Orchestrator marked task complete.",
                stop_reason="done",
                worker_goal=decision.worker_goal,
                tool_calls="",
            )

        worker_result = await self._run_worker(
            session=session,
            snapshot=snapshot,
            tool_context=tool_context,
            useful_block=useful_block,
            diff_text=diff_text,
            decision=decision,
            priority_ids=priority_ids,
            step_deadline=step_deadline,
        )
        self._emit(
            "step_end",
            step=self.state.step,
            done=False,
            worker_done=bool(worker_result.done),
            duration_ms=int((time.perf_counter() - step_started) * 1000),
        )
        return self._result(
            step_started=step_started,
            done=bool(worker_result.done),
            summary=worker_result.summary,
            stop_reason=None,
            worker_goal=decision.worker_goal,
            tool_calls=self.state.step_trace[-1].get("tool_calls", "") if self.state.step_trace else "",
        )

    async def _run_orchestrator(
        self,
        *,
        snapshot: PageSnapshot,
        useful_lines: list[str],
        useful_block: str,
        diff_text: str,
        oracle_hint: str,
        priority_ids: list[str],
        step_deadline: float,
    ) -> OrchestratorDecision | None:
        assert self._agents is not None
        snapshot_text = format_snapshot_for_llm(
            snapshot,
            max_elements=self.agent_config.max_elements,
            query=((" ".join(useful_lines) + " " + (self.state.last_worker_goal or "")).strip())[:600]
            or None,
            priority_ids=priority_ids,
            active_frame_id=self.state.active_frame_id,
            class_sanitize_mode=self.agent_config.class_sanitize_mode,
            class_sanitize_max_tokens=self.agent_config.class_sanitize_max_tokens,
            class_sanitize_max_chars=self.agent_config.class_sanitize_max_chars,
            class_sanitize_fallback_tokens=self.agent_config.class_sanitize_fallback_tokens,
            attr_value_max_len=self.agent_config.snapshot_attr_value_max_len,
        )
        tool_list = ", ".join(sorted(runtime.DEFAULT_WORKER_TOOLS))
        prompt = (
            f"Overall goal: {self.agent_config.goal}\n\n"
            f"Filtered useful lines:\n{useful_block}\n\n"
            f"Diff since prior snapshot:\n{diff_text}\n\n"
            f"Memory (recent):\n{runtime._format_memory(self.state.memory, limit=self.agent_config.memory_steps)}\n\n"
            f"Worker tools: {tool_list}\n"
            "Only set goals achievable with these exact tools. Do not suggest inspecting elements, reading page content, "
            "taking screenshots, executing JavaScript, or any action not in this list.\n\n"
            f"Page snapshot:\n{snapshot_text}\n"
            f"{oracle_hint}"
        )
        started = time.perf_counter()
        for attempt in range(2):
            try:
                result = await runtime._with_deadline(
                    self._agents.orchestrator.run(prompt), step_deadline
                )
                duration_ms = int((time.perf_counter() - started) * 1000)
                self._record_agent_call(
                    result=result,
                    step=self.state.step,
                    agent="orchestrator",
                    duration_ms=duration_ms,
                    model_name=self.llm_config.model,
                )
                decision = result.output
                logger.info(
                    "  orchestrator: %sms worker=%s done=%s",
                    duration_ms,
                    decision.worker,
                    decision.done,
                )
                return decision
            except TimeoutError:
                logger.warning("Orchestrator timed out (step %s deadline)", self.state.step)
                return None
            except Exception:
                if attempt == 0:
                    logger.warning("Orchestrator failed, retrying once", exc_info=True)
                    continue
                logger.warning("Orchestrator failed twice", exc_info=True)
                self._emit(
                    "agent_call",
                    step=self.state.step,
                    agent="orchestrator",
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    error=True,
                )
                return None
        return None

    async def _run_worker(
        self,
        *,
        session: ExternalBrowserSession,
        snapshot: PageSnapshot,
        tool_context: semantic.ToolContext,
        useful_block: str,
        diff_text: str,
        decision: OrchestratorDecision,
        priority_ids: list[str],
        step_deadline: float,
    ) -> StepOutput:
        assert self._agents is not None
        snapshot_text = format_snapshot_for_llm(
            snapshot,
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
        worker_context = ""
        if self.state.memory and self.agent_config.worker_context_steps > 0:
            recent = self.state.memory[-self.agent_config.worker_context_steps :]
            worker_context = "\n\nRecent steps:\n" + "\n".join(f"- {m}" for m in recent)
        prompt = (
            STEP_PROMPT.format(goal=decision.worker_goal)
            + worker_context
            + "\n\n"
            + f"Page context:\n{useful_block}\n\n"
            + f"Page snapshot:\n{snapshot_text}\n"
        )
        tool_tracker = runtime.ToolCallTracker()
        deps = runtime.WorkerDeps(
            tool_context=tool_context,
            metrics=self._metrics or MetricsRecorder(log_dir="/tmp", run_id="disabled", enabled=False),
            step=self.state.step,
            tool_tracker=tool_tracker,
            allowed_tools=runtime.DEFAULT_WORKER_TOOLS,
        )
        prev_url = getattr(session.page, "url", "") or ""
        step_output: StepOutput
        started = time.perf_counter()
        try:
            with self._agents.browser_worker.sequential_tool_calls():
                result = await runtime._with_deadline(
                    self._agents.browser_worker.run(
                        prompt,
                        deps=deps,
                        usage_limits=UsageLimits(
                            request_limit=self.agent_config.max_worker_tool_calls
                        ),
                    ),
                    step_deadline,
                )
        except UsageLimitExceeded:
            self.state.consecutive_tool_limit_steps += 1
            step_output = StepOutput(
                done=False,
                summary=f"Tool call limit reached ({self.agent_config.max_worker_tool_calls})",
            )
        except TimeoutError:
            step_output = StepOutput(
                done=False,
                summary="Worker timed out (step deadline exceeded)",
            )
        except Exception:
            err_name = type(sys.exc_info()[1]).__name__
            logger.warning("Worker LLM error (step %s): %s", self.state.step, err_name, exc_info=True)
            self._emit(
                "agent_call",
                step=self.state.step,
                agent="browser_worker",
                duration_ms=int((time.perf_counter() - started) * 1000),
                error=True,
            )
            step_output = StepOutput(done=False, summary=f"Worker LLM error: {err_name}")
        else:
            self.state.consecutive_tool_limit_steps = 0
            self._record_agent_call(
                result=result,
                step=self.state.step,
                agent="browser_worker",
                duration_ms=int((time.perf_counter() - started) * 1000),
                model_name=self.llm_config.worker_model or self.llm_config.model,
            )
            step_output = result.output

        if step_output.done and tool_tracker.success_count == 0:
            step_output = step_output.model_copy(
                update={"done": False, "summary": f"[no successful tools] {step_output.summary}"}
            )
        self._finish_tool_step(
            step_output=step_output,
            worker_goal=decision.worker_goal,
            tool_context=tool_context,
            tool_tracker=tool_tracker,
            diff_text=diff_text,
            prev_url=prev_url,
        )
        logger.info("  worker: done=%s summary=%s", step_output.done, step_output.summary)
        return step_output

    def _finish_tool_step(
        self,
        *,
        step_output: StepOutput | UnifiedStepOutput,
        worker_goal: str,
        tool_context: semantic.ToolContext,
        tool_tracker: runtime.ToolCallTracker,
        diff_text: str,
        prev_url: str,
    ) -> None:
        self.state.active_frame_id = tool_context.active_frame_id
        tool_status = f"{tool_tracker.success_count} ok, {tool_tracker.failure_count} failed"
        memory_entry = f"[step {self.state.step}, {tool_status}] {step_output.summary}"
        self.state.last_summary = memory_entry
        self.state.memory.append(memory_entry)
        self.state.last_tool = tool_context.last_tool
        self.state.last_element_id = tool_context.last_element_id
        self.state.last_worker_goal = worker_goal
        self.state.last_step_was_puzzle = bool(
            re.search(r"(?i)puzzle|math.*solve|solve.*puzzle", step_output.summary)
        )
        self.state.step_trace.append(
            {
                "step": self.state.step,
                "url": getattr(tool_context.page, "url", "") or "",
                "goal": worker_goal,
                "summary": step_output.summary,
                "diff_summary": diff_text.split("\n")[0] if diff_text else "",
                "url_changed": prev_url != (getattr(tool_context.page, "url", "") or ""),
                "tool_calls": tool_tracker.calls_summary(),
                "tool_limit_hit": step_output.summary.startswith("Tool call limit reached"),
            }
        )

    def _result(
        self,
        *,
        step_started: float,
        done: bool,
        summary: str,
        stop_reason: str | None,
        worker_goal: str | None,
        tool_calls: str,
    ) -> RuntimeStepResult:
        duration_ms = int((time.perf_counter() - step_started) * 1000)
        logger.info("Step %s end %sms", self.state.step, duration_ms)
        self._write_run_summary(stop_reason=stop_reason)
        return RuntimeStepResult(
            step=self.state.step,
            done=done,
            summary=summary,
            stop_reason=stop_reason,
            worker_goal=worker_goal,
            tool_calls=tool_calls,
            input_tokens=self._total_input_tokens,
            output_tokens=self._total_output_tokens,
            cost_usd=self._total_cost_usd,
            duration_ms=duration_ms,
            log_dir=self._run_dir,
            trace=list(self.state.step_trace),
        )
