from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.agentlab.run_browsergym_benchmark import (
    DEFAULT_FULL_REPEATS,
    DEFAULT_MAX_WORKER_TOOL_CALLS,
    DEFAULT_ORACLE_INTERVAL,
    DEFAULT_UNCHANGED_ABORT_THRESHOLD,
    DEFAULT_WORKER_CONTEXT_STEPS,
    MINIWOB_VERIFY_FIVE_TASKS,
    BenchmarkConfigurationError,
    ReportContext,
    check_benchmark_environment,
    expand_preset,
    generate_report_artifacts,
    load_task_set,
    resolve_iteration_options,
    select_parallel_backend,
    validate_miniwob_setup,
)


def test_miniwob_verify_five_preset_expands_requested_tasks() -> None:
    selection = expand_preset("miniwob", "verify-five")

    assert selection.browsergym_key == "miniwob"
    assert selection.tasks == MINIWOB_VERIFY_FIVE_TASKS
    assert selection.n_repeats == 1
    assert selection.is_full_preset is False


def test_full_miniwob_preset_does_not_subset_tasks() -> None:
    selection = expand_preset("miniwob", "full")

    assert selection.browsergym_key == "miniwob"
    assert selection.tasks is None
    assert selection.n_repeats == DEFAULT_FULL_REPEATS["miniwob"]
    assert selection.is_full_preset is True


def test_webarena_tiny_full_resolves_browsergym_key() -> None:
    selection = expand_preset("webarena_tiny", "full")

    assert selection.browsergym_key == "webarena_tiny"
    assert selection.tasks is None
    assert selection.n_repeats == 1


def test_custom_task_mode_requires_task() -> None:
    with pytest.raises(BenchmarkConfigurationError, match="requires at least one"):
        expand_preset("miniwob", "custom")


def test_iteration_profile_full_preserves_current_defaults() -> None:
    options = resolve_iteration_options(profile="full")

    assert options.max_worker_tool_calls == DEFAULT_MAX_WORKER_TOOL_CALLS
    assert options.worker_context_steps == DEFAULT_WORKER_CONTEXT_STEPS
    assert options.oracle_interval == DEFAULT_ORACLE_INTERVAL
    assert options.unchanged_abort_threshold == DEFAULT_UNCHANGED_ABORT_THRESHOLD


def test_iteration_profile_balanced_and_cheap_apply_defaults() -> None:
    balanced = resolve_iteration_options(profile="balanced")
    cheap = resolve_iteration_options(profile="cheap")

    assert balanced.max_worker_tool_calls == 6
    assert balanced.worker_context_steps == 2
    assert balanced.stuck_threshold == 2
    assert balanced.unchanged_abort_threshold == 4
    assert balanced.oracle_interval == 0
    assert cheap.max_worker_tool_calls == 4
    assert cheap.worker_context_steps == 1
    assert cheap.stuck_threshold == 1
    assert cheap.unchanged_abort_threshold == 2
    assert cheap.oracle_interval == 0
    assert cheap.env_max_steps == 5


def test_explicit_iteration_overrides_beat_profile_and_task_set_caps() -> None:
    options = resolve_iteration_options(
        profile="cheap",
        task_set="terminal-readback",
        env_max_steps=9,
        max_worker_tool_calls=7,
        worker_context_steps=3,
        oracle_interval=5,
    )

    assert options.env_max_steps == 9
    assert options.max_worker_tool_calls == 7
    assert options.worker_context_steps == 3
    assert options.oracle_interval == 5


def test_task_set_expands_checked_in_manifest_tasks() -> None:
    tasks = load_task_set("miniwob", "email-icon-controls")
    selection = expand_preset("miniwob", task_set="email-icon-controls")

    assert "miniwob.email-inbox" in tasks
    assert selection.tasks == tasks
    assert selection.task_set == "email-icon-controls"
    assert selection.n_repeats == 1


def test_miniwob_environment_check_fails_without_url_or_local_checkout(tmp_path: Path) -> None:
    missing_local = tmp_path / "missing-miniwob"

    with pytest.raises(BenchmarkConfigurationError, match="MiniWoB setup is missing"):
        validate_miniwob_setup(env={}, local_path=missing_local)


def test_miniwob_environment_check_uses_repo_local_checkout(tmp_path: Path) -> None:
    local_miniwob = tmp_path / "miniwob"
    local_miniwob.mkdir()

    url, info = validate_miniwob_setup(env={}, local_path=local_miniwob)

    assert url == f"{local_miniwob.resolve().as_uri()}/"
    assert info["source"] == "repo-local"


def test_webarena_environment_check_fails_before_study_creation() -> None:
    selection = expand_preset("webarena", "full")

    with pytest.raises(BenchmarkConfigurationError, match="WA_SHOPPING"):
        check_benchmark_environment(selection, env={})


def test_webarena_parallel_uses_dependency_aware_backend() -> None:
    selection = expand_preset("webarena_tiny", "full")

    assert select_parallel_backend(selection, n_jobs=1) == "sequential"
    assert select_parallel_backend(selection, n_jobs=2) == "ray"


def test_miniwob_parallel_uses_joblib_backend() -> None:
    selection = expand_preset("miniwob", "full")

    assert select_parallel_backend(selection, n_jobs=2) == "joblib"


def test_report_generation_writes_stable_artifacts(tmp_path: Path) -> None:
    study_dir = tmp_path / "study"
    study_dir.mkdir()
    success_exp = study_dir / "exp-success"
    failed_exp = study_dir / "exp-failed"
    success_exp.mkdir()
    failed_exp.mkdir()
    native_log_dir = tmp_path / "agent-run"
    native_log_dir.mkdir()
    (native_log_dir / "agent.log").write_text("info\n")
    (native_log_dir / "agent_debug.log").write_text("debug\n")
    result_csv = study_dir / "result_df.csv"
    result_csv.write_text(
        "\n".join(
            [
                "env.task_name,env.task_seed,cum_reward,err_msg,terminated,truncated,n_steps,exp_dir,native_agent_log_dir",
                f"miniwob.click-button,1,1,,True,False,2,{success_exp},{native_log_dir}",
                f"miniwob.enter-text,2,0,,False,True,10,{failed_exp},{native_log_dir}",
                f"miniwob.click-button,3,0,TimeoutError: model timed out,False,False,4,{failed_exp},",
            ]
        )
        + "\n"
    )
    (study_dir / "summary_df.csv").write_text("avg_reward,std_err\n0.333,0.272\n")
    context = ReportContext(
        benchmark="miniwob",
        preset="verify-five",
        provider="openrouter",
        model="z-ai/glm-4.7:nitro",
        worker_model=None,
        filter_model=None,
        oracle_model=None,
        unified=True,
        max_steps=20,
        env_max_steps=10,
        max_elements=80,
        n_repeats=1,
        task_count=2,
        n_jobs=1,
        agent_logs_dir="logs/agentlab",
        command="python benchmarks/agentlab/run_browsergym_benchmark.py --benchmark miniwob",
        env_info={"type": "miniwob", "miniwob_url": "file:///tmp/miniwob/"},
    )

    report = generate_report_artifacts(
        study_dir,
        context,
        generated_at="2026-01-01T00:00:00+00:00",
        git_commit="abc1234",
    )

    assert report["aggregate"]["episode_count"] == 3
    assert report["aggregate"]["avg_reward"] == pytest.approx(0.333333)
    assert report["aggregate"]["score_percent"] == pytest.approx(33.333333)
    assert report["aggregate"]["completed_count"] == 3
    assert report["aggregate"]["error_count"] == 1
    assert len(report["failed_tasks"]) == 2
    assert report["failed_tasks"][1]["error_key"] == "TimeoutError"
    assert (study_dir / "benchmark_report.json").exists()
    assert (study_dir / "benchmark_report.md").exists()
    assert (study_dir / "per_task_results.csv").exists()
    assert (study_dir / "failed_tasks.md").exists()

    saved = json.loads((study_dir / "benchmark_report.json").read_text())
    assert saved["git_commit"] == "abc1234"
    assert saved["config"]["provider"] == "openrouter"
    assert "miniwob.enter-text" in (study_dir / "failed_tasks.md").read_text()


def test_report_counts_missing_rewards_as_zero(tmp_path: Path) -> None:
    study_dir = tmp_path / "study"
    study_dir.mkdir()
    result_csv = study_dir / "result_df.csv"
    result_csv.write_text(
        "\n".join(
            [
                "env.task_name,env.task_seed,cum_reward,err_msg,terminated,truncated,n_steps,exp_dir,native_agent_log_dir",
                "miniwob.click-button,1,1,,True,False,1,,",
                "miniwob.enter-text,2,,,False,False,,,",
            ]
        )
        + "\n"
    )
    context = ReportContext(
        benchmark="miniwob",
        preset="custom",
        provider="openrouter",
        model="z-ai/glm-4.7:nitro",
        worker_model=None,
        filter_model=None,
        oracle_model=None,
        unified=True,
        max_steps=20,
        env_max_steps=10,
        max_elements=80,
        n_repeats=1,
        task_count=2,
        n_jobs=1,
        agent_logs_dir="logs/agentlab",
        command="python benchmarks/agentlab/run_browsergym_benchmark.py --benchmark miniwob",
        env_info={"type": "miniwob"},
    )

    report = generate_report_artifacts(
        study_dir,
        context,
        generated_at="2026-01-01T00:00:00+00:00",
        git_commit="abc1234",
    )

    assert report["aggregate"]["episode_count"] == 2
    assert report["aggregate"]["avg_reward"] == pytest.approx(0.5)
    assert report["aggregate"]["score_percent"] == pytest.approx(50.0)
    assert report["aggregate"]["incomplete_count"] == 1
    assert report["failed_tasks"][0]["task_name"] == "miniwob.enter-text"
    assert report["failed_tasks"][0]["reward"] == 0.0
    assert report["warnings"]["parse_gaps"] == [
        "Row 2 in result_df.csv is missing cum_reward; counting reward as 0."
    ]
