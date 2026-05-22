"""Run extensible BrowserGym benchmarks through AgentLab and write reports."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import os
import pickle
import re
import shlex
import subprocess
import sys
import warnings
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.agentlab.computer_use_agent import BENCHMARK_DEFAULT_MODEL


DEFAULT_RESULTS_DIR = ROOT / "logs" / "agentlab" / "studies"
DEFAULT_AGENT_LOG_DIR = ROOT / "logs" / "agentlab"
DEFAULT_MINIWOB_PATH = (
    ROOT / ".benchmarks" / "miniwob-plusplus" / "miniwob" / "html" / "miniwob"
)
DEFAULT_PROVIDER = "openrouter"
DEFAULT_MAX_STEPS = 20
DEFAULT_ENV_MAX_STEPS = 10
DEFAULT_MAX_ELEMENTS = 80
DEFAULT_N_JOBS = 1
DEFAULT_MAX_WORKER_TOOL_CALLS = 10
DEFAULT_WORKER_CONTEXT_STEPS = 3
DEFAULT_ORACLE_INTERVAL = 5
DEFAULT_STUCK_THRESHOLD = 3
DEFAULT_UNCHANGED_ABORT_THRESHOLD = 8
TASK_SET_DIR = ROOT / "benchmarks" / "agentlab" / "task_sets"
SUPPORTED_BENCHMARKS = (
    "miniwob",
    "webarena",
    "webarena_lite",
    "webarena_verified",
    "webarena_tiny",
)
WEB_ARENA_BENCHMARKS = {
    "webarena",
    "webarena_lite",
    "webarena_verified",
    "webarena_tiny",
}
WEB_ARENA_REQUIRED_ENV = (
    "WA_SHOPPING",
    "WA_SHOPPING_ADMIN",
    "WA_REDDIT",
    "WA_GITLAB",
    "WA_WIKIPEDIA",
    "WA_MAP",
    "WA_HOMEPAGE",
)
MINIWOB_VERIFY_FIVE_TASKS = (
    "miniwob.click-button",
    "miniwob.enter-text",
    "miniwob.click-checkboxes",
    "miniwob.form-sequence",
    "miniwob.scroll-text",
)
ITERATION_PROFILE_DEFAULTS: dict[str, dict[str, int]] = {
    "full": {
        "max_worker_tool_calls": DEFAULT_MAX_WORKER_TOOL_CALLS,
        "worker_context_steps": DEFAULT_WORKER_CONTEXT_STEPS,
        "stuck_threshold": DEFAULT_STUCK_THRESHOLD,
        "unchanged_abort_threshold": DEFAULT_UNCHANGED_ABORT_THRESHOLD,
        "oracle_interval": DEFAULT_ORACLE_INTERVAL,
        "env_max_steps": DEFAULT_ENV_MAX_STEPS,
    },
    "balanced": {
        "max_worker_tool_calls": 6,
        "worker_context_steps": 2,
        "stuck_threshold": 2,
        "unchanged_abort_threshold": 4,
        "oracle_interval": 0,
        "env_max_steps": DEFAULT_ENV_MAX_STEPS,
    },
    "cheap": {
        "max_worker_tool_calls": 4,
        "worker_context_steps": 1,
        "stuck_threshold": 1,
        "unchanged_abort_threshold": 2,
        "oracle_interval": 0,
        "env_max_steps": 5,
    },
}
TASK_SET_CAPS: dict[str, int] = {
    "env_max_steps": 5,
    "max_worker_tool_calls": 4,
    "worker_context_steps": 1,
    "oracle_interval": 0,
    "stuck_threshold": 1,
    "unchanged_abort_threshold": 2,
}
DEFAULT_FULL_REPEATS = {
    "miniwob": 5,
    "webarena": 1,
    "webarena_lite": 1,
    "webarena_verified": 1,
    "webarena_tiny": 1,
}
_RESOURCE_TRACKER_WARNING_FILTER = "ignore:resource_tracker:UserWarning"
_STEP_FILE_RE = re.compile(r"step_(\d+)\.pkl\.gz$")


class BenchmarkConfigurationError(ValueError):
    """Raised when a benchmark cannot be configured safely."""


@dataclass(frozen=True)
class BenchmarkSelection:
    """Resolved benchmark preset before BrowserGym objects are imported."""

    benchmark: str
    preset: str
    browsergym_key: str
    tasks: tuple[str, ...] | None
    n_repeats: int
    is_full_preset: bool
    task_set: str | None = None


@dataclass(frozen=True)
class IterationOptions:
    """Resolved runtime caps for one benchmark run."""

    profile: str
    max_steps: int
    env_max_steps: int
    max_worker_tool_calls: int
    worker_context_steps: int
    oracle_interval: int
    stuck_threshold: int
    unchanged_abort_threshold: int


@dataclass(frozen=True)
class ReportContext:
    """Configuration metadata saved with generated benchmark reports."""

    benchmark: str
    preset: str
    provider: str
    model: str
    worker_model: str | None
    filter_model: str | None
    oracle_model: str | None
    unified: bool
    max_steps: int
    env_max_steps: int
    max_elements: int
    n_repeats: int
    task_count: int
    n_jobs: int
    agent_logs_dir: str
    command: str
    env_info: dict[str, Any]
    iteration_profile: str = "full"
    task_set: str | None = None
    max_worker_tool_calls: int = DEFAULT_MAX_WORKER_TOOL_CALLS
    worker_context_steps: int = DEFAULT_WORKER_CONTEXT_STEPS
    oracle_interval: int = DEFAULT_ORACLE_INTERVAL
    stuck_threshold: int = DEFAULT_STUCK_THRESHOLD
    unchanged_abort_threshold: int = DEFAULT_UNCHANGED_ABORT_THRESHOLD


@dataclass
class EpisodeResult:
    """Normalized one-row AgentLab episode result."""

    task_name: str
    task_seed: str | None
    reward: float | None
    terminated: bool | None
    truncated: bool | None
    error_message: str | None
    error_key: str | None
    exp_dir: str | None
    native_agent_log_dir: str | None
    n_steps: int | None

    @property
    def has_error(self) -> bool:
        return bool(self.error_message)

    @property
    def completed(self) -> bool:
        return self.has_error or self.terminated is True or self.truncated is True

    @property
    def failed(self) -> bool:
        if self.has_error or self.truncated is True or not self.completed:
            return True
        return self.reward is None or self.reward <= 0


def _suppress_resource_tracker_shutdown_noise() -> None:
    """Silence multiprocessing's resource_tracker semaphore warning from AgentLab cleanup."""
    existing = os.environ.get("PYTHONWARNINGS", "")
    filters = [item for item in existing.split(",") if item]
    if _RESOURCE_TRACKER_WARNING_FILTER not in filters:
        filters.append(_RESOURCE_TRACKER_WARNING_FILTER)
        os.environ["PYTHONWARNINGS"] = ",".join(filters)
    warnings.filterwarnings(
        "ignore",
        message=r"resource_tracker: There appear to be .* leaked semaphore objects",
        category=UserWarning,
    )


def _default_miniwob_url(local_path: Path = DEFAULT_MINIWOB_PATH) -> str:
    return f"{local_path.resolve().as_uri()}/"


def _file_url_to_path(url: str) -> Path | None:
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None
    return Path(unquote(parsed.path))


def validate_miniwob_setup(
    miniwob_url: str | None = None,
    *,
    env: Mapping[str, str] = os.environ,
    local_path: Path = DEFAULT_MINIWOB_PATH,
) -> tuple[str, dict[str, Any]]:
    """Resolve MiniWoB URL and fail before AgentLab creates a study if setup is missing."""
    configured_url = miniwob_url or env.get("MINIWOB_URL")
    if configured_url:
        file_path = _file_url_to_path(configured_url)
        if file_path is not None and not file_path.is_dir():
            raise BenchmarkConfigurationError(
                "MiniWoB setup is invalid: MINIWOB_URL points to a missing directory "
                f"({file_path}). Set MINIWOB_URL to the MiniWoB++ "
                "miniwob/html/miniwob directory or clone it under .benchmarks/."
            )
        source = "argument" if miniwob_url else "environment"
        return configured_url, {"miniwob_url": configured_url, "source": source}

    if local_path.is_dir():
        resolved_url = _default_miniwob_url(local_path)
        return resolved_url, {
            "miniwob_url": resolved_url,
            "source": "repo-local",
            "local_path": str(local_path),
        }

    raise BenchmarkConfigurationError(
        "MiniWoB setup is missing: set MINIWOB_URL or clone miniwob-plusplus to "
        f"{ROOT / '.benchmarks' / 'miniwob-plusplus'} so BrowserGym can load "
        "miniwob/html/miniwob."
    )


def validate_webarena_setup(
    benchmark: str,
    *,
    env: Mapping[str, str] = os.environ,
) -> dict[str, Any]:
    """Validate WebArena service variables before AgentLab creates a study."""
    missing = [
        name
        for name in WEB_ARENA_REQUIRED_ENV
        if not env.get(name) or env.get(name, "").strip().lower() == "todo"
    ]
    if missing:
        missing_list = ", ".join(missing)
        required_list = ", ".join(WEB_ARENA_REQUIRED_ENV)
        raise BenchmarkConfigurationError(
            f"{benchmark} setup is missing required WebArena environment variables: "
            f"{missing_list}. Set the self-hosted service URLs first. Required: "
            f"{required_list}."
        )
    return {
        "required_env_set": {name: True for name in WEB_ARENA_REQUIRED_ENV},
        "wa_full_reset_set": bool(env.get("WA_FULL_RESET")),
    }


def check_benchmark_environment(
    selection: BenchmarkSelection,
    *,
    miniwob_url: str | None = None,
    env: Mapping[str, str] = os.environ,
    local_miniwob_path: Path = DEFAULT_MINIWOB_PATH,
) -> dict[str, Any]:
    """Validate benchmark setup before creating any AgentLab study directory."""
    if selection.benchmark == "miniwob":
        resolved_url, info = validate_miniwob_setup(
            miniwob_url, env=env, local_path=local_miniwob_path
        )
        return {"type": "miniwob", **info, "resolved_miniwob_url": resolved_url}
    if selection.benchmark in WEB_ARENA_BENCHMARKS:
        return {"type": "webarena", **validate_webarena_setup(selection.benchmark, env=env)}
    raise BenchmarkConfigurationError(f"Unsupported benchmark: {selection.benchmark}")


def load_task_set(benchmark: str, name: str) -> tuple[str, ...]:
    """Load a checked-in benchmark task-set manifest."""
    safe_name = name.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", safe_name):
        raise BenchmarkConfigurationError(f"Invalid task set name: {name!r}.")
    path = TASK_SET_DIR / f"{safe_name}.json"
    if not path.exists():
        available = ", ".join(sorted(p.stem for p in TASK_SET_DIR.glob("*.json")))
        raise BenchmarkConfigurationError(
            f"Unknown task set {name!r}. Available: {available or 'none'}."
        )
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise BenchmarkConfigurationError(f"Task set {name!r} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise BenchmarkConfigurationError(f"Task set {name!r} must contain a JSON object.")
    manifest_benchmark = str(payload.get("benchmark") or "").lower()
    if manifest_benchmark and manifest_benchmark != benchmark.lower():
        raise BenchmarkConfigurationError(
            f"Task set {name!r} is for {manifest_benchmark}, not {benchmark}."
        )
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not all(isinstance(item, str) and item for item in tasks):
        raise BenchmarkConfigurationError(f"Task set {name!r} must define a non-empty string tasks list.")
    return tuple(dict.fromkeys(tasks))


def expand_preset(
    benchmark: str,
    preset: str | None = None,
    tasks: Sequence[str] | None = None,
    task_set: str | None = None,
    n_repeats: int | None = None,
) -> BenchmarkSelection:
    """Resolve CLI benchmark/preset/task options into a BrowserGym benchmark selection."""
    benchmark_key = benchmark.lower()
    if benchmark_key not in SUPPORTED_BENCHMARKS:
        raise BenchmarkConfigurationError(
            f"Unsupported benchmark {benchmark!r}. Supported: {', '.join(SUPPORTED_BENCHMARKS)}."
        )

    if task_set and tasks:
        raise BenchmarkConfigurationError("--task-set cannot be combined with --task.")
    task_tuple = load_task_set(benchmark_key, task_set) if task_set else tuple(tasks or ())
    preset_key = (preset or (None if task_set else ("custom" if task_tuple else None)) or "").lower()
    if not preset_key:
        preset_key = "verify-five" if benchmark_key == "miniwob" else "full"

    if preset_key == "custom":
        if not task_tuple:
            raise BenchmarkConfigurationError("Custom task mode requires at least one --task.")
        repeats = int(n_repeats if n_repeats is not None else 1)
        return BenchmarkSelection(
            benchmark=benchmark_key,
            preset="custom",
            browsergym_key=benchmark_key,
            tasks=task_tuple,
            n_repeats=repeats,
            is_full_preset=False,
            task_set=task_set,
        )

    if task_set:
        if preset is not None:
            raise BenchmarkConfigurationError("--task-set can only be used with --preset custom or without --preset.")
        repeats = int(n_repeats if n_repeats is not None else 1)
        return BenchmarkSelection(
            benchmark=benchmark_key,
            preset=task_set,
            browsergym_key=benchmark_key,
            tasks=task_tuple,
            n_repeats=repeats,
            is_full_preset=False,
            task_set=task_set,
        )

    if task_tuple:
        raise BenchmarkConfigurationError(
            "--task can only be used with --preset custom or without an explicit preset."
        )

    if preset_key == "verify-five":
        if benchmark_key != "miniwob":
            raise BenchmarkConfigurationError("--preset verify-five is only available for MiniWoB.")
        repeats = int(n_repeats if n_repeats is not None else 1)
        return BenchmarkSelection(
            benchmark="miniwob",
            preset="verify-five",
            browsergym_key="miniwob",
            tasks=MINIWOB_VERIFY_FIVE_TASKS,
            n_repeats=repeats,
            is_full_preset=False,
        )

    if preset_key == "full":
        repeats = int(n_repeats if n_repeats is not None else DEFAULT_FULL_REPEATS[benchmark_key])
        return BenchmarkSelection(
            benchmark=benchmark_key,
            preset="full",
            browsergym_key=benchmark_key,
            tasks=None,
            n_repeats=repeats,
            is_full_preset=True,
        )

    raise BenchmarkConfigurationError(
        f"Unsupported preset {preset!r} for {benchmark_key}. Use full, custom"
        + (", or verify-five." if benchmark_key == "miniwob" else ".")
    )


def build_browsergym_benchmark(selection: BenchmarkSelection, *, env_max_steps: int) -> Any:
    """Instantiate and configure a BrowserGym benchmark lazily."""
    try:
        from browsergym.experiments.benchmark.configs import DEFAULT_BENCHMARKS
    except ImportError as exc:  # pragma: no cover - depends on optional deps
        raise RuntimeError(
            "BrowserGym benchmark dependencies are missing. Run `uv sync --extra agentlab`."
        ) from exc

    benchmark = DEFAULT_BENCHMARKS[selection.browsergym_key](n_repeats=selection.n_repeats)
    if selection.tasks is not None:
        benchmark = benchmark.subset_from_list(
            list(selection.tasks),
            benchmark_name_suffix=selection.preset.replace("-", "_"),
        )
    for env_args in benchmark.env_args_list:
        env_args.max_steps = int(env_max_steps)
    return benchmark


def resolve_iteration_options(
    *,
    profile: str,
    task_set: str | None = None,
    max_steps: int | None = None,
    env_max_steps: int | None = None,
    max_worker_tool_calls: int | None = None,
    worker_context_steps: int | None = None,
    oracle_interval: int | None = None,
    stuck_threshold: int | None = None,
    unchanged_abort_threshold: int | None = None,
) -> IterationOptions:
    """Resolve iteration-profile defaults, task-set caps, and explicit overrides."""
    if profile not in ITERATION_PROFILE_DEFAULTS:
        raise BenchmarkConfigurationError(
            f"Unsupported iteration profile {profile!r}. Use full, balanced, or cheap."
        )
    defaults = dict(ITERATION_PROFILE_DEFAULTS[profile])
    if task_set:
        for key, value in TASK_SET_CAPS.items():
            defaults[key] = min(defaults.get(key, value), value)

    return IterationOptions(
        profile=profile,
        max_steps=int(max_steps if max_steps is not None else DEFAULT_MAX_STEPS),
        env_max_steps=int(env_max_steps if env_max_steps is not None else defaults["env_max_steps"]),
        max_worker_tool_calls=int(
            max_worker_tool_calls
            if max_worker_tool_calls is not None
            else defaults["max_worker_tool_calls"]
        ),
        worker_context_steps=int(
            worker_context_steps
            if worker_context_steps is not None
            else defaults["worker_context_steps"]
        ),
        oracle_interval=int(
            oracle_interval if oracle_interval is not None else defaults["oracle_interval"]
        ),
        stuck_threshold=int(
            stuck_threshold if stuck_threshold is not None else defaults["stuck_threshold"]
        ),
        unchanged_abort_threshold=int(
            unchanged_abort_threshold
            if unchanged_abort_threshold is not None
            else defaults["unchanged_abort_threshold"]
        ),
    )


def _clean_cell(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    return text


def _first_value(row: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    for key in keys:
        value = _clean_cell(row.get(key))
        if value is not None:
            return value
    return None


def _parse_float(value: Any) -> float | None:
    text = _clean_cell(value)
    if text is None:
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    if math.isnan(parsed):
        return None
    return parsed


def _score_reward(episode: EpisodeResult) -> float:
    """Return the reward value used in benchmark scores; missing rewards count as zero."""
    return float(episode.reward) if episode.reward is not None else 0.0


def _parse_int(value: Any) -> int | None:
    text = _clean_cell(value)
    if text is None:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _parse_bool(value: Any) -> bool | None:
    text = _clean_cell(value)
    if text is None:
        return None
    if text.lower() in {"true", "1", "yes", "y"}:
        return True
    if text.lower() in {"false", "0", "no", "n"}:
        return False
    return None


def _normalize_path_text(value: str | None) -> str | None:
    if value is None:
        return None
    match = re.fullmatch(r"(?:PosixPath|WindowsPath|Path)\('(.+)'\)", value)
    if match:
        return match.group(1)
    return value


def map_error_key(error_message: str | None) -> str | None:
    """Map a raw AgentLab error message to a compact grouping key."""
    if not error_message:
        return None
    match = re.search(
        r"\b([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Interrupt|Timeout))\b",
        error_message,
    )
    if match:
        return match.group(1)
    first_line = error_message.strip().splitlines()[0]
    return first_line[:80]


def _step_number(path: Path) -> int:
    match = _STEP_FILE_RE.match(path.name)
    return int(match.group(1)) if match else -1


def _extract_native_agent_log_dir(exp_dir: str | None) -> str | None:
    if not exp_dir:
        return None
    exp_path = Path(exp_dir)
    if not exp_path.is_dir():
        return None
    step_files = sorted(exp_path.glob("step_*.pkl.gz"), key=_step_number, reverse=True)
    for step_file in step_files:
        try:
            with gzip.open(step_file, "rb") as handle:
                step_info = pickle.load(handle)
        except Exception:
            continue
        agent_info = getattr(step_info, "agent_info", None)
        if isinstance(agent_info, dict):
            extra_info = agent_info.get("extra_info")
            log_dir = _clean_cell(agent_info.get("log_dir"))
        else:
            extra_info = getattr(agent_info, "extra_info", None)
            get_value = getattr(agent_info, "get", None)
            log_dir = _clean_cell(get_value("log_dir")) if callable(get_value) else None
        if isinstance(extra_info, dict):
            extra_log_dir = _clean_cell(extra_info.get("log_dir"))
            if extra_log_dir:
                return extra_log_dir
        if log_dir:
            return log_dir
    return None


def _find_result_csv(study_dir: Path) -> Path:
    preferred = study_dir / "result_df.csv"
    if preferred.exists():
        return preferred
    candidates = sorted(
        study_dir.glob("result_df*.csv"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if candidates:
        return candidates[0]
    raise FileNotFoundError(f"No AgentLab result_df CSV found in {study_dir}")


def load_episode_results(
    study_dir: Path,
    *,
    result_csv: Path | None = None,
) -> tuple[list[EpisodeResult], list[str]]:
    """Load AgentLab result CSV rows into normalized episode records."""
    parse_gaps: list[str] = []
    csv_path = result_csv or _find_result_csv(study_dir)
    episodes: list[EpisodeResult] = []
    with csv_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for index, row in enumerate(reader, start=1):
            task_name = _first_value(row, ("env.task_name", "task_name", "task"))
            if task_name is None:
                task_name = "unknown"
                parse_gaps.append(f"Row {index} in {csv_path.name} is missing env.task_name.")
            exp_dir = _normalize_path_text(_first_value(row, ("exp_dir", "experiment_dir")))
            native_log_dir = _normalize_path_text(
                _first_value(
                    row,
                    (
                        "native_agent_log_dir",
                        "agent_log_dir",
                        "extra_info.log_dir",
                        "agent_info.extra_info.log_dir",
                    ),
                )
            )
            if native_log_dir is None:
                native_log_dir = _extract_native_agent_log_dir(exp_dir)
            error_message = _first_value(row, ("err_msg", "error_msg", "error", "exception"))
            raw_reward = _first_value(row, ("cum_reward", "reward"))
            reward = _parse_float(raw_reward)
            if raw_reward is None:
                parse_gaps.append(
                    f"Row {index} in {csv_path.name} is missing cum_reward; counting reward as 0."
                )
            episodes.append(
                EpisodeResult(
                    task_name=task_name,
                    task_seed=_first_value(row, ("env.task_seed", "task_seed", "seed")),
                    reward=reward,
                    terminated=_parse_bool(row.get("terminated")),
                    truncated=_parse_bool(row.get("truncated")),
                    error_message=error_message,
                    error_key=map_error_key(error_message),
                    exp_dir=exp_dir,
                    native_agent_log_dir=native_log_dir,
                    n_steps=_parse_int(_first_value(row, ("n_steps", "steps"))),
                )
            )
    return episodes, parse_gaps


def _mean(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _std_err(values: Sequence[float]) -> float | None:
    if not values:
        return None
    mean_value = sum(values) / len(values)
    if all(value in {0.0, 1.0} for value in values):
        return math.sqrt(mean_value * (1 - mean_value) / len(values))
    if len(values) == 1:
        return 0.0
    variance = sum((value - mean_value) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance) / math.sqrt(len(values))


def _round_metric(value: float | None, digits: int = 6) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _agent_log_paths(log_dir: str | None) -> tuple[str | None, str | None]:
    if not log_dir:
        return None, None
    base = Path(log_dir)
    return str(base / "agent.log"), str(base / "agent_debug.log")


def _episode_to_csv_row(episode: EpisodeResult) -> dict[str, Any]:
    return {
        "task_name": episode.task_name,
        "task_seed": episode.task_seed,
        "reward": episode.reward,
        "completed": episode.completed,
        "terminated": episode.terminated,
        "truncated": episode.truncated,
        "error": episode.has_error,
        "error_key": episode.error_key,
        "n_steps": episode.n_steps,
        "exp_dir": episode.exp_dir,
        "native_agent_log_dir": episode.native_agent_log_dir,
    }


def _failed_entry(episode: EpisodeResult) -> dict[str, Any]:
    agent_log, agent_debug_log = _agent_log_paths(episode.native_agent_log_dir)
    return {
        "task_name": episode.task_name,
        "task_seed": episode.task_seed,
        "reward": _score_reward(episode),
        "error_key": episode.error_key,
        "error_message": episode.error_message,
        "terminated": episode.terminated,
        "truncated": episode.truncated,
        "completed": episode.completed,
        "exp_dir": episode.exp_dir,
        "native_agent_log_dir": episode.native_agent_log_dir,
        "agent_log": agent_log,
        "agent_debug_log": agent_debug_log,
    }


def _resource_tracker_warning_found(study_dir: Path) -> bool:
    for path in study_dir.rglob("*"):
        if not path.is_file() or path.suffix != ".log":
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        if "resource_tracker" in text and "semaphore" in text:
            return True
    return False


def _missing_log_warnings(failed_entries: Sequence[dict[str, Any]]) -> list[str]:
    warnings_list: list[str] = []
    for entry in failed_entries:
        label = f"{entry['task_name']} seed={entry.get('task_seed') or 'unknown'}"
        log_dir = entry.get("native_agent_log_dir")
        if not log_dir:
            warnings_list.append(f"No native agent log directory found for {label}.")
            continue
        for key in ("agent_log", "agent_debug_log"):
            path = entry.get(key)
            if path and not Path(path).exists():
                warnings_list.append(f"Missing {Path(path).name} for {label}: {path}.")
    return warnings_list


def _per_task_aggregate(episodes: Sequence[EpisodeResult]) -> list[dict[str, Any]]:
    grouped: dict[str, list[EpisodeResult]] = defaultdict(list)
    for episode in episodes:
        grouped[episode.task_name].append(episode)
    aggregates: list[dict[str, Any]] = []
    for task_name, task_episodes in grouped.items():
        rewards = [_score_reward(episode) for episode in task_episodes]
        aggregates.append(
            {
                "task_name": task_name,
                "episodes": len(task_episodes),
                "mean_reward": _round_metric(_mean(rewards)),
                "min_reward": _round_metric(min(rewards) if rewards else None),
                "max_reward": _round_metric(max(rewards) if rewards else None),
                "errors": sum(1 for episode in task_episodes if episode.has_error),
                "completed_count": sum(1 for episode in task_episodes if episode.completed),
            }
        )
    return sorted(
        aggregates,
        key=lambda item: (
            float("inf") if item["mean_reward"] is None else item["mean_reward"],
            item["task_name"],
        ),
    )


def build_report(
    study_dir: Path,
    context: ReportContext,
    *,
    episodes: Sequence[EpisodeResult],
    parse_gaps: Sequence[str],
    generated_at: str | None = None,
    git_commit: str | None = None,
) -> dict[str, Any]:
    """Build the canonical machine-readable benchmark report."""
    rewards = [_score_reward(episode) for episode in episodes]
    avg_reward = _mean(rewards)
    std_err = _std_err(rewards)
    failed_entries = [_failed_entry(episode) for episode in episodes if episode.failed]
    warnings_payload = {
        "resource_tracker_warning_found": _resource_tracker_warning_found(study_dir),
        "missing_logs": _missing_log_warnings(failed_entries),
        "parse_gaps": list(parse_gaps),
    }
    return {
        "study_dir": str(study_dir),
        "agent_logs_dir": context.agent_logs_dir,
        "benchmark": context.benchmark,
        "preset": context.preset,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit or _git_commit(),
        "command": context.command,
        "config": {
            "provider": context.provider,
            "model": context.model,
            "role_model_overrides": {
                "worker": context.worker_model,
                "filter": context.filter_model,
                "oracle": context.oracle_model,
            },
            "unified": context.unified,
            "max_steps": context.max_steps,
            "env_max_steps": context.env_max_steps,
            "max_elements": context.max_elements,
            "iteration_profile": context.iteration_profile,
            "task_set": context.task_set,
            "max_worker_tool_calls": context.max_worker_tool_calls,
            "worker_context_steps": context.worker_context_steps,
            "oracle_interval": context.oracle_interval,
            "stuck_threshold": context.stuck_threshold,
            "unchanged_abort_threshold": context.unchanged_abort_threshold,
            "repeats": context.n_repeats,
            "task_count": context.task_count,
            "jobs": context.n_jobs,
        },
        "aggregate": {
            "avg_reward": _round_metric(avg_reward),
            "std_err": _round_metric(std_err),
            "score_percent": _round_metric(avg_reward * 100 if avg_reward is not None else None),
            "completed_count": sum(1 for episode in episodes if episode.completed),
            "error_count": sum(1 for episode in episodes if episode.has_error),
            "truncated_count": sum(1 for episode in episodes if episode.truncated is True),
            "incomplete_count": sum(1 for episode in episodes if not episode.completed),
            "episode_count": len(episodes),
        },
        "per_task": _per_task_aggregate(episodes),
        "failed_tasks": failed_entries,
        "warnings": warnings_payload,
        "reproducibility": {
            "env": context.env_info,
        },
    }


def _git_commit() -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=ROOT,
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except Exception:
        return "unknown"


def _fmt_float(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.3f}".rstrip("0").rstrip(".")


def _md_escape(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")


def _failed_markdown(failed_entries: Sequence[dict[str, Any]]) -> str:
    if not failed_entries:
        return "No failed tasks.\n"
    lines = ["# Failed Tasks", ""]
    for entry in failed_entries:
        title = f"{entry['task_name']}"
        if entry.get("task_seed"):
            title += f" seed {entry['task_seed']}"
        lines.extend(
            [
                f"## {title}",
                f"- Reward: {_fmt_float(entry.get('reward'))}",
                f"- Error: {_md_escape(entry.get('error_key') or entry.get('error_message') or 'none')}",
                f"- Truncated: {entry.get('truncated')}",
                f"- Completed: {entry.get('completed')}",
                f"- Experiment: `{entry.get('exp_dir') or 'unknown'}`",
                f"- Agent log: `{entry.get('agent_log') or 'unavailable'}`",
                f"- Agent debug log: `{entry.get('agent_debug_log') or 'unavailable'}`",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _report_markdown(report: dict[str, Any]) -> str:
    aggregate = report["aggregate"]
    config = report["config"]
    lines = [
        "# Benchmark Report",
        "",
        "## Command",
        "",
        "```bash",
        report["command"],
        "```",
        "",
        "## Aggregate",
        "",
        f"- Score: {_fmt_float(aggregate['score_percent'])}%",
        f"- Average reward: {_fmt_float(aggregate['avg_reward'])} +/- {_fmt_float(aggregate['std_err'])}",
        f"- Episodes: {aggregate['episode_count']}",
        f"- Completed: {aggregate['completed_count']}",
        f"- Errors: {aggregate['error_count']}",
        f"- Truncated: {aggregate['truncated_count']}",
        f"- Incomplete: {aggregate['incomplete_count']}",
        "",
        "## Per Task",
        "",
        "| Task | Episodes | Mean Reward | Min | Max | Completed | Errors |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in report["per_task"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    _md_escape(item["task_name"]),
                    str(item["episodes"]),
                    _fmt_float(item["mean_reward"]),
                    _fmt_float(item["min_reward"]),
                    _fmt_float(item["max_reward"]),
                    str(item["completed_count"]),
                    str(item["errors"]),
                ]
            )
            + " |"
        )
    lines.extend(["", "## Failed Tasks", ""])
    failed_text = _failed_markdown(report["failed_tasks"]).splitlines()
    lines.extend(failed_text[2:] if failed_text[:1] == ["# Failed Tasks"] else failed_text)
    lines.extend(
        [
            "",
            "## Reproducibility",
            "",
            "```text",
            f"git_commit={report['git_commit']}",
            f"benchmark={report['benchmark']}",
            f"preset={report['preset']}",
            f"provider={config['provider']}",
            f"model={config['model']}",
            f"unified={config['unified']}",
            f"max_steps={config['max_steps']}",
            f"env_max_steps={config['env_max_steps']}",
            f"max_elements={config['max_elements']}",
            f"iteration_profile={config['iteration_profile']}",
            f"task_set={config['task_set']}",
            f"max_worker_tool_calls={config['max_worker_tool_calls']}",
            f"worker_context_steps={config['worker_context_steps']}",
            f"oracle_interval={config['oracle_interval']}",
            f"stuck_threshold={config['stuck_threshold']}",
            f"unchanged_abort_threshold={config['unchanged_abort_threshold']}",
            f"repeats={config['repeats']}",
            f"jobs={config['jobs']}",
            f"env={json.dumps(report['reproducibility']['env'], sort_keys=True)}",
            "```",
            "",
            "## Warnings",
            "",
            f"- resource_tracker semaphore warning found: {report['warnings']['resource_tracker_warning_found']}",
        ]
    )
    for warning in report["warnings"]["missing_logs"]:
        lines.append(f"- missing log: {_md_escape(warning)}")
    for warning in report["warnings"]["parse_gaps"]:
        lines.append(f"- parse gap: {_md_escape(warning)}")
    return "\n".join(lines).rstrip() + "\n"


def write_report_artifacts(
    study_dir: Path,
    report: dict[str, Any],
    episodes: Sequence[EpisodeResult],
) -> None:
    """Write benchmark JSON, Markdown, normalized CSV, and failed-task notes."""
    study_dir.mkdir(parents=True, exist_ok=True)
    (study_dir / "benchmark_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    (study_dir / "benchmark_report.md").write_text(_report_markdown(report))
    (study_dir / "failed_tasks.md").write_text(_failed_markdown(report["failed_tasks"]))

    csv_path = study_dir / "per_task_results.csv"
    fieldnames = [
        "task_name",
        "task_seed",
        "reward",
        "completed",
        "terminated",
        "truncated",
        "error",
        "error_key",
        "n_steps",
        "exp_dir",
        "native_agent_log_dir",
    ]
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for episode in episodes:
            writer.writerow(_episode_to_csv_row(episode))


def generate_report_artifacts(
    study_dir: Path,
    context: ReportContext,
    *,
    result_csv: Path | None = None,
    generated_at: str | None = None,
    git_commit: str | None = None,
) -> dict[str, Any]:
    """Load AgentLab results and write the benchmark report artifact set."""
    episodes, parse_gaps = load_episode_results(study_dir, result_csv=result_csv)
    report = build_report(
        study_dir,
        context,
        episodes=episodes,
        parse_gaps=parse_gaps,
        generated_at=generated_at,
        git_commit=git_commit,
    )
    write_report_artifacts(study_dir, report, episodes)
    return report


def export_leaderboard_draft(report: dict[str, Any], output_path: Path) -> None:
    """Write a local draft leaderboard artifact without submitting anything."""
    draft = {
        "status": "draft_not_submitted",
        "not_submitted": True,
        "benchmark": report["benchmark"],
        "preset": report["preset"],
        "score_percent": report["aggregate"]["score_percent"],
        "avg_reward": report["aggregate"]["avg_reward"],
        "std_err": report["aggregate"]["std_err"],
        "episode_count": report["aggregate"]["episode_count"],
        "study_dir": report["study_dir"],
        "generated_at": report["generated_at"],
        "git_commit": report["git_commit"],
        "agent": {
            "provider": report["config"]["provider"],
            "model": report["config"]["model"],
            "unified": report["config"]["unified"],
        },
    }
    output_path.write_text(json.dumps(draft, indent=2, sort_keys=True) + "\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run BrowserGym benchmarks through AgentLab with reporting."
    )
    parser.add_argument("--benchmark", choices=SUPPORTED_BENCHMARKS, default="miniwob")
    parser.add_argument(
        "--preset",
        default=None,
        help="Preset to run: verify-five, full, or custom. Defaults to verify-five for MiniWoB and full otherwise.",
    )
    parser.add_argument(
        "--task",
        action="append",
        dest="tasks",
        default=None,
        help="BrowserGym task name to include. Use with --preset custom; repeatable.",
    )
    parser.add_argument(
        "--task-set",
        default=None,
        help="Checked-in task-set manifest name under benchmarks/agentlab/task_sets/.",
    )
    parser.add_argument(
        "--iteration-profile",
        choices=("full", "balanced", "cheap"),
        default="full",
        help="Runtime cap profile for iteration cost control.",
    )
    parser.add_argument(
        "--n-repeats",
        type=int,
        default=None,
        help="Override preset repeat count.",
    )
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--env-max-steps", type=int, default=None)
    parser.add_argument("--max-elements", type=int, default=DEFAULT_MAX_ELEMENTS)
    parser.add_argument("--max-worker-tool-calls", type=int, default=None)
    parser.add_argument("--worker-context-steps", type=int, default=None)
    parser.add_argument("--oracle-interval", type=int, default=None)
    parser.add_argument("--stuck-threshold", type=int, default=None)
    parser.add_argument("--unchanged-abort-threshold", type=int, default=None)
    parser.add_argument("--provider", default=DEFAULT_PROVIDER)
    parser.add_argument("--model", default=BENCHMARK_DEFAULT_MODEL)
    parser.add_argument("--worker-model", default=None)
    parser.add_argument("--filter-model", default=None)
    parser.add_argument("--oracle-model", default=None)
    parser.add_argument(
        "--split-pipeline",
        action="store_true",
        help="Use the split filter/orchestrator/worker pipeline instead of unified mode.",
    )
    parser.add_argument("--n-jobs", type=int, default=DEFAULT_N_JOBS)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--log-dir", default=str(DEFAULT_AGENT_LOG_DIR))
    parser.add_argument(
        "--miniwob-url",
        default=None,
        help="MiniWoB base URL. Defaults to MINIWOB_URL or repo-local .benchmarks checkout.",
    )
    parser.add_argument(
        "--export-leaderboard-json",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="Write a draft leaderboard JSON artifact after a full preset run. Nothing is submitted.",
    )
    parser.add_argument(
        "--no-force-exit",
        action="store_true",
        help=(
            "Testing/debug only: return normally after report generation instead of "
            "forcing process exit. Real AgentLab runs may leave cleanup resources "
            "that keep Python alive."
        ),
    )
    return parser


def _configure_environment(results_dir: Path, env_info: Mapping[str, Any]) -> None:
    os.environ["AGENTLAB_EXP_ROOT"] = str(results_dir.resolve())
    miniwob_url = env_info.get("resolved_miniwob_url")
    if miniwob_url:
        os.environ["MINIWOB_URL"] = str(miniwob_url)


def _study_suffix(selection: BenchmarkSelection) -> str:
    if selection.preset == "full":
        return "full"
    return selection.preset.replace("-", "_")


def select_parallel_backend(selection: BenchmarkSelection, n_jobs: int) -> str:
    """Choose an AgentLab backend that preserves benchmark dependency constraints."""
    if int(n_jobs) <= 1:
        return "sequential"
    if selection.benchmark in WEB_ARENA_BENCHMARKS:
        return "ray"
    return "joblib"


def _make_command(argv: Sequence[str]) -> str:
    return shlex.join([sys.executable, *argv])


def main(argv: Sequence[str] | None = None) -> None:
    _suppress_resource_tracker_shutdown_noise()
    parser = _build_parser()
    args = parser.parse_args(argv)
    cli_argv = list(argv) if argv is not None else sys.argv

    try:
        selection = expand_preset(
            args.benchmark,
            args.preset,
            tasks=args.tasks,
            task_set=args.task_set,
            n_repeats=args.n_repeats,
        )
        iteration = resolve_iteration_options(
            profile=args.iteration_profile,
            task_set=selection.task_set,
            max_steps=args.max_steps,
            env_max_steps=args.env_max_steps,
            max_worker_tool_calls=args.max_worker_tool_calls,
            worker_context_steps=args.worker_context_steps,
            oracle_interval=args.oracle_interval,
            stuck_threshold=args.stuck_threshold,
            unchanged_abort_threshold=args.unchanged_abort_threshold,
        )
        if args.export_leaderboard_json is not None and not selection.is_full_preset:
            raise BenchmarkConfigurationError(
                "--export-leaderboard-json is only allowed for --preset full runs."
            )
        env_info = check_benchmark_environment(selection, miniwob_url=args.miniwob_url)
    except BenchmarkConfigurationError as exc:
        parser.error(str(exc))
        return

    _configure_environment(args.results_dir, env_info)

    from agentlab.experiments.study import make_study

    from benchmarks.agentlab import ComputerUseAgentArgs

    benchmark = build_browsergym_benchmark(selection, env_max_steps=iteration.env_max_steps)
    agent_args = ComputerUseAgentArgs(
        provider=args.provider,
        model=args.model,
        worker_model=args.worker_model,
        filter_model=args.filter_model,
        oracle_model=args.oracle_model,
        max_steps=int(iteration.max_steps),
        max_elements=int(args.max_elements),
        max_worker_tool_calls=int(iteration.max_worker_tool_calls),
        worker_context_steps=int(iteration.worker_context_steps),
        oracle_interval=int(iteration.oracle_interval),
        stuck_threshold=int(iteration.stuck_threshold),
        unchanged_abort_threshold=int(iteration.unchanged_abort_threshold),
        log_dir=args.log_dir,
        unified=not args.split_pipeline,
    )
    study = make_study(
        agent_args=agent_args,
        benchmark=benchmark,
        suffix=_study_suffix(selection),
        comment=f"{selection.benchmark}:{selection.preset} benchmark for the computer-use agent.",
    )
    parallel_backend = select_parallel_backend(selection, int(args.n_jobs))
    study.run(
        n_jobs=int(args.n_jobs),
        parallel_backend=parallel_backend,
        n_relaunch=1,
        exp_root=Path(os.environ["AGENTLAB_EXP_ROOT"]),
    )
    study.get_results()

    context = ReportContext(
        benchmark=selection.benchmark,
        preset=selection.preset,
        provider=args.provider,
        model=args.model,
        worker_model=args.worker_model,
        filter_model=args.filter_model,
        oracle_model=args.oracle_model,
        unified=not args.split_pipeline,
        max_steps=int(iteration.max_steps),
        env_max_steps=int(iteration.env_max_steps),
        max_elements=int(args.max_elements),
        iteration_profile=iteration.profile,
        task_set=selection.task_set,
        max_worker_tool_calls=int(iteration.max_worker_tool_calls),
        worker_context_steps=int(iteration.worker_context_steps),
        oracle_interval=int(iteration.oracle_interval),
        stuck_threshold=int(iteration.stuck_threshold),
        unchanged_abort_threshold=int(iteration.unchanged_abort_threshold),
        n_repeats=selection.n_repeats,
        task_count=len({env_args.task_name for env_args in benchmark.env_args_list}),
        n_jobs=int(args.n_jobs),
        agent_logs_dir=args.log_dir,
        command=_make_command(cli_argv),
        env_info=env_info,
    )
    report = generate_report_artifacts(Path(study.dir), context)

    if args.export_leaderboard_json is not None:
        output_path = (
            Path(study.dir) / "leaderboard_draft.not_submitted.json"
            if args.export_leaderboard_json == ""
            else Path(args.export_leaderboard_json)
        )
        export_leaderboard_draft(report, output_path)
        print(f"Draft leaderboard JSON (not submitted): {output_path}")

    print(f"Benchmark: {selection.benchmark}:{selection.preset}")
    if env_info.get("resolved_miniwob_url"):
        print(f"MiniWoB URL: {env_info['resolved_miniwob_url']}")
    print(f"AgentLab study dir: {study.dir}")
    print(f"Agent logs dir: {args.log_dir}")
    print(f"Benchmark report: {Path(study.dir) / 'benchmark_report.md'}")

    if not args.no_force_exit:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)


if __name__ == "__main__":
    main()
