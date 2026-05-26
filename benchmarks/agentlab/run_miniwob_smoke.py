"""Run a two-task MiniWoB smoke study through AgentLab."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
import warnings


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_RESULTS_DIR = ROOT / "logs" / "agentlab" / "studies"
DEFAULT_MINIWOB_URL = (
    ROOT / ".benchmarks" / "miniwob-plusplus" / "miniwob" / "html" / "miniwob"
)
DEFAULT_TASKS = ("miniwob.click-button", "miniwob.enter-text")
BENCHMARK_DEFAULT_MODEL = "z-ai/glm-4.7:nitro"
_RESOURCE_TRACKER_WARNING_FILTER = "ignore:resource_tracker:UserWarning"


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


def _default_miniwob_url() -> str:
    return f"file://{DEFAULT_MINIWOB_URL.resolve()}/"


def _configure_environment(results_dir: Path, miniwob_url: str | None) -> None:
    os.environ.setdefault("AGENTLAB_EXP_ROOT", str(results_dir.resolve()))
    if miniwob_url:
        os.environ.setdefault("MINIWOB_URL", miniwob_url)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the AgentLab MiniWoB smoke benchmark")
    parser.add_argument(
        "--task",
        action="append",
        dest="tasks",
        default=None,
        help="MiniWoB task name to include, e.g. miniwob.click-button. Repeatable.",
    )
    parser.add_argument("--provider", default="openrouter", help="LLM provider for the agent")
    parser.add_argument(
        "--model",
        default=BENCHMARK_DEFAULT_MODEL,
        help="Model override for all agent roles",
    )
    parser.add_argument("--worker-model", default=None, help="Worker model override")
    parser.add_argument("--filter-model", default=None, help="Filter model override")
    parser.add_argument("--oracle-model", default=None, help="Oracle model override")
    parser.add_argument("--max-steps", type=int, default=20, help="Internal agent max steps")
    parser.add_argument("--env-max-steps", type=int, default=10, help="BrowserGym max steps")
    parser.add_argument("--max-elements", type=int, default=80, help="Max elements shown to LLM")
    parser.add_argument("--n-jobs", type=int, default=1, help="AgentLab parallel jobs")
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=DEFAULT_RESULTS_DIR,
        help="AgentLab study output directory",
    )
    parser.add_argument(
        "--miniwob-url",
        default=None,
        help="MiniWoB base URL. Defaults to MINIWOB_URL or repo-local .benchmarks checkout.",
    )
    parser.add_argument(
        "--log-dir",
        default=str(ROOT / "logs" / "agentlab"),
        help="Native agent log directory",
    )
    parser.add_argument(
        "--split-pipeline",
        action="store_true",
        help="Use the split filter/orchestrator/worker pipeline instead of unified mode.",
    )
    parser.add_argument(
        "--no-force-exit",
        action="store_true",
        help=(
            "Testing/debug only: return normally after the study instead of forcing "
            "process exit. Real AgentLab runs may leave cleanup resources that keep "
            "Python alive."
        ),
    )
    return parser


def main() -> None:
    _suppress_resource_tracker_shutdown_noise()
    args = _build_parser().parse_args()
    tasks = tuple(args.tasks or DEFAULT_TASKS)
    miniwob_url = args.miniwob_url or os.environ.get("MINIWOB_URL") or _default_miniwob_url()
    _configure_environment(args.results_dir, miniwob_url)

    # AgentLab creates its default results directory at import time, so import it
    # only after AGENTLAB_EXP_ROOT has been set.
    from agentlab.experiments.study import make_study
    from browsergym.experiments.benchmark.configs import DEFAULT_BENCHMARKS

    from benchmarks.agentlab import ComputerUseAgentArgs

    benchmark = DEFAULT_BENCHMARKS["miniwob"](n_repeats=1).subset_from_list(
        list(tasks),
        benchmark_name_suffix="smoke",
    )
    for env_args in benchmark.env_args_list:
        env_args.max_steps = int(args.env_max_steps)

    agent_args = ComputerUseAgentArgs(
        provider=args.provider,
        model=args.model,
        worker_model=args.worker_model,
        filter_model=args.filter_model,
        oracle_model=args.oracle_model,
        max_steps=int(args.max_steps),
        max_elements=int(args.max_elements),
        log_dir=args.log_dir,
        unified=not args.split_pipeline,
    )
    study = make_study(
        agent_args=agent_args,
        benchmark=benchmark,
        suffix="smoke",
        comment="MiniWoB smoke benchmark for Zip.",
    )
    study.run(
        n_jobs=int(args.n_jobs),
        parallel_backend="sequential",
        n_relaunch=1,
        exp_root=Path(os.environ["AGENTLAB_EXP_ROOT"]),
    )
    print(f"MiniWoB URL: {os.environ['MINIWOB_URL']}")
    print(f"AgentLab study dir: {study.dir}")
    print(f"Agent logs dir: {args.log_dir}")
    if not args.no_force_exit:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)


if __name__ == "__main__":
    main()
