"""Metrics and structured event logging utilities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import shutil
from pathlib import Path
from typing import Any, Iterable, Sequence
import uuid

from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.run import AgentRunResult
from pydantic_ai.usage import RunUsage

from src.agent.config import MODEL_PRICES


_RUN_DIR_RE = re.compile(r"^[0-9a-f]{32}$")


def new_run_id() -> str:
    return uuid.uuid4().hex


def prune_old_runs(base_log_dir: str | Path, *, keep: int) -> None:
    """Delete oldest per-run log directories beyond *keep* count.

    Only directories whose names match the 32-hex-char UUID pattern produced by
    ``new_run_id()`` are considered.  Non-matching entries are never touched.
    """
    base = Path(base_log_dir)
    if not base.is_dir():
        return
    run_dirs = [d for d in base.iterdir() if d.is_dir() and _RUN_DIR_RE.match(d.name)]
    # Sort newest first by mtime
    run_dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    for old in run_dirs[keep:]:
        shutil.rmtree(old, ignore_errors=True)


def _update_latest_symlink(base_log_dir: str | Path, run_id: str) -> None:
    """Create or replace a ``latest`` symlink pointing to *run_id*."""
    link = Path(base_log_dir) / "latest"
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(run_id)
    except OSError:
        pass  # Silently skip on platforms that don't support symlinks


def prepare_run_dir(
    base_log_dir: str | Path, run_id: str, *, max_log_runs: int
) -> str:
    """Create ``<base_log_dir>/<run_id>/``, prune old runs, update symlink.

    Returns the per-run directory path as a string.
    """
    prune_old_runs(base_log_dir, keep=max(max_log_runs - 1, 0))
    run_dir = Path(base_log_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    _update_latest_symlink(base_log_dir, run_id)
    return str(run_dir)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class UsageStats:
    requests: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    input_audio_tokens: int
    cache_audio_read_tokens: int
    output_audio_tokens: int

    @classmethod
    def from_run_usage(cls, usage: RunUsage) -> UsageStats:
        return cls(
            requests=int(getattr(usage, "requests", 0) or 0),
            tool_calls=int(getattr(usage, "tool_calls", 0) or 0),
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_write_tokens=int(getattr(usage, "cache_write_tokens", 0) or 0),
            cache_read_tokens=int(getattr(usage, "cache_read_tokens", 0) or 0),
            input_audio_tokens=int(getattr(usage, "input_audio_tokens", 0) or 0),
            cache_audio_read_tokens=int(getattr(usage, "cache_audio_read_tokens", 0) or 0),
            output_audio_tokens=int(getattr(usage, "output_audio_tokens", 0) or 0),
        )

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class CostStats:
    cost_usd: float
    upstream_inference_cost_usd: float | None = None


def _safe_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def extract_openrouter_cost(messages: Sequence[ModelMessage]) -> CostStats | None:
    """Extract OpenRouter cost stats from provider_details in model responses.

    Requires OpenRouter to include cost fields in provider_details. If no cost fields
    are present, returns None.
    """

    total_cost = 0.0
    total_upstream = 0.0
    seen: set[str] = set()
    found_cost = False
    found_upstream = False

    for message in messages:
        if not isinstance(message, ModelResponse):
            continue
        response_id = message.provider_response_id or ""
        if response_id and response_id in seen:
            continue
        if response_id:
            seen.add(response_id)

        details = message.provider_details or {}
        if cost := _safe_float(details.get("cost")):
            total_cost += cost
            found_cost = True
        if upstream := _safe_float(details.get("upstream_inference_cost")):
            total_upstream += upstream
            found_upstream = True

    if not found_cost and not found_upstream:
        return None
    return CostStats(
        cost_usd=total_cost if found_cost else 0.0,
        upstream_inference_cost_usd=total_upstream if found_upstream else None,
    )


def usage_stats_from_result(result: AgentRunResult[Any]) -> UsageStats:
    return UsageStats.from_run_usage(result.usage())


def compute_cost_from_usage(model: str, usage: UsageStats) -> CostStats | None:
    """Compute cost from token counts using the local price map."""
    pricing = MODEL_PRICES.get(model)
    if not pricing:
        return None

    cache_write_rate = pricing.cache_write_per_mtok if pricing.cache_write_per_mtok is not None else pricing.input_per_mtok
    cache_read_rate = pricing.cache_read_per_mtok if pricing.cache_read_per_mtok is not None else pricing.input_per_mtok

    uncached_input = max(usage.input_tokens - usage.cache_write_tokens - usage.cache_read_tokens, 0)

    input_cost = (
        uncached_input * pricing.input_per_mtok
        + usage.cache_write_tokens * cache_write_rate
        + usage.cache_read_tokens * cache_read_rate
    )
    output_cost = usage.output_tokens * pricing.output_per_mtok
    cost = (input_cost + output_cost) / 1_000_000
    return CostStats(cost_usd=cost)


def cost_stats_from_result(result: AgentRunResult[Any], model: str) -> CostStats | None:
    cost = extract_openrouter_cost(result.new_messages())
    if cost:
        return cost
    usage = usage_stats_from_result(result)
    return compute_cost_from_usage(model, usage)


class MetricsRecorder:
    """Writes structured metrics events as JSONL."""

    def __init__(
        self,
        *,
        log_dir: str,
        run_id: str,
        enabled: bool = True,
        filename: str = "metrics.jsonl",
    ) -> None:
        self.enabled = enabled
        self.run_id = run_id
        self.path = Path(log_dir) / filename
        self._fh = None
        if self.enabled:
            Path(log_dir).mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a", encoding="utf-8")

    def emit(self, event: str, **fields: Any) -> None:
        if not self.enabled or self._fh is None:
            return
        record = {"ts": utc_now_iso(), "run_id": self.run_id, "event": event, **fields}
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()

    def emit_many(self, records: Iterable[dict[str, Any]]) -> None:
        for record in records:
            event = str(record.get("event") or "event")
            fields = {k: v for k, v in record.items() if k != "event"}
            self.emit(event, **fields)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def write_run_summary(
    *,
    log_dir: str,
    run_id: str,
    summary: dict[str, Any],
    filename: str = "run_summary.json",
) -> Path:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    path = Path(log_dir) / filename
    payload = {"ts": utc_now_iso(), "run_id": run_id, **summary}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path

