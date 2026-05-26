# AgentLab And BrowserGym Benchmarks

Zip can run under AgentLab with BrowserGym owning the browser and task validation. The adapter lives in `benchmarks/agentlab/` and keeps the normal agent runtime responsible for perception, planning, tool execution, logs, and metrics.

## Runtime Shape

```mermaid
flowchart LR
    Study[AgentLab Study] --> Env[BrowserGym Env]
    Env -->|raw obs includes page| Adapter[Zip Adapter]
    Adapter --> Bridge[Sync Playwright Bridge]
    Bridge --> Runtime[Single-Step Agent Runtime]
    Env -->|reward/termination| Adapter
    Runtime --> Snapshot[CDP Snapshot + Handler Hints]
    Snapshot --> Agents[Oracle + Filter + Orchestrator + Worker]
    Agents --> Tools[DOM-First Semantic Tools]
    Tools -->|mutate live page| Env
    Adapter -->|returns noop()| Env
    Env --> Validate[BrowserGym Task Validation]
    Validate --> Results[AgentLab result_df.csv]
    Results --> Reports[benchmark_report.json + Markdown + per-task CSV]
```

The important inversion is browser ownership:

- AgentLab/BrowserGym creates the environment, browser context, page, observations, and validation.
- The adapter requests `use_raw_page_output=True`, stores `obs["page"]`, and removes it before observations are pickled.
- `BrowserAgentStepRuntime` runs one internal step against the live page.
- The adapter returns `noop()` after the internal tool calls have already changed the page.
- The benchmark snapshot includes compact SVG graphics, non-interactive text/structure, context hints, widget values, and bounding boxes when labels are not enough; the worker can use coordinate, pointer-drag, slider, selection, formatting, and live-text tools for MiniWoB++-style widgets.
- BrowserGym remains the source of truth for task success. BrowserGym reward/termination is passed into the runtime as external validation; terminal positive validation stops success, and terminal zero/negative validation stops failure.
- Internal `done=True` is treated as a proposal. While BrowserGym validation is non-terminal, it triggers recovery instead of latching the runtime as done; terminal BrowserGym success is the authoritative success signal.

## Installation

Use Python 3.12. The project metadata is pinned to `>=3.12,<3.13` so it is compatible with AgentLab.

```bash
uv sync --extra agentlab
uv run playwright install chromium
git clone https://github.com/Farama-Foundation/miniwob-plusplus.git .benchmarks/miniwob-plusplus
git -C .benchmarks/miniwob-plusplus reset --hard 7fd85d71a4b60325c6585396ec4f48377d049838
```

MiniWoB++ (BrowserGym benchmark key: `miniwob`) requires the MiniWoB++ static site at BrowserGym's pinned commit and `MINIWOB_URL` pointing to it. WebArena requires the self-hosted services and the standard `WA_*` environment variables.

For the local smoke runner, the default MiniWoB++ URL is:

```bash
export MINIWOB_URL="file://$PWD/.benchmarks/miniwob-plusplus/miniwob/html/miniwob/"
```

## Benchmark Runner

Use the generic runner for current BrowserGym/AgentLab benchmark work:

```bash
uv run --extra agentlab python benchmarks/agentlab/run_browsergym_benchmark.py \
  --benchmark miniwob \
  --preset verify-five \
  --n-repeats 1 \
  --max-steps 20 \
  --env-max-steps 10 \
  --max-elements 80
```

For a one-repeat MiniWoB++ full-suite comparison run, use:

```bash
uv run --extra agentlab python benchmarks/agentlab/run_browsergym_benchmark.py \
  --benchmark miniwob \
  --preset full \
  --n-repeats 1 \
  --max-steps 20 \
  --env-max-steps 10 \
  --max-elements 80 \
  --n-jobs 1
```

Supported benchmarks are `miniwob`, `webarena`, `webarena_lite`, `webarena_verified`, and `webarena_tiny`.

Presets:

- `miniwob:verify-five`: `miniwob.click-button`, `miniwob.enter-text`, `miniwob.click-checkboxes`, `miniwob.form-sequence`, and `miniwob.scroll-text`.
- `miniwob:full`: BrowserGym's default MiniWoB++ suite with `n_repeats=5` unless overridden.
- `webarena_tiny:full`: BrowserGym's `webarena_tiny` benchmark.
- `webarena:full`, `webarena_lite:full`, `webarena_verified:full`: BrowserGym defaults for those suites.
- `custom`: pass one or more `--task` values.

Iteration profiles:

- `full`: comparable defaults, including `max_worker_tool_calls=10`, `worker_context_steps=3`, `stuck_threshold=3`, `unchanged_abort_threshold=8`, `oracle_interval=5`, and `env_max_steps=10`.
- `balanced`: cheaper iteration defaults: `max_worker_tool_calls=6`, `worker_context_steps=2`, `stuck_threshold=2`, `unchanged_abort_threshold=4`, `oracle_interval=0`.
- `cheap`: smallest local loop defaults: `max_worker_tool_calls=4`, `worker_context_steps=1`, `stuck_threshold=1`, `unchanged_abort_threshold=2`, `oracle_interval=0`, and `env_max_steps=5`.

Task sets:

- Checked-in manifests live under `benchmarks/agentlab/task_sets/`.
- Use `--task-set <name>` for targeted local regression subsets such as `terminal-readback`, `email-icon-controls`, `social-icon-controls`, `drag-draw-slider`, and `noninteractive-text`.
- Task-set runs apply low caps by default unless the corresponding CLI flag is explicitly provided.

The runner defaults to unified mode, OpenRouter, `z-ai/glm-4.7:nitro`, `--iteration-profile full`, `--max-steps 20`, `--env-max-steps 10`, `--max-elements 80`, and sequential execution. Non-WebArena parallel runs use AgentLab's `joblib` backend; WebArena variants use `ray` when `--n-jobs > 1` so BrowserGym task dependencies are honored. Pass `--split-pipeline` only when comparing against the older filter/orchestrator/worker pipeline.

Before creating a study, the runner validates benchmark setup:

- MiniWoB++ requires `MINIWOB_URL` or the repo-local `.benchmarks/miniwob-plusplus/miniwob/html/miniwob` checkout.
- WebArena variants require the standard self-hosted `WA_*` URL variables.

## AgentLab Configuration

Import the adapter from a module path visible on `PYTHONPATH`. The `ComputerUseAgentArgs` class name is a legacy internal name for Zip's AgentLab adapter.

```python
from agentlab.experiments.study import make_study

from benchmarks.agentlab import ComputerUseAgentArgs

agent_args = ComputerUseAgentArgs(
    provider="openrouter",
    model="z-ai/glm-4.7:nitro",
    unified=True,
    max_steps=20,
    max_elements=80,
    log_dir="logs/agentlab",
)

study = make_study(
    benchmark="miniwob",
    agent_args=[agent_args],
    comment="Zip MiniWoB++ smoke",
)
study.run(n_jobs=1)
```

Start with single-task smoke tests before broad runs:

- `miniwob.click-button`
- `miniwob.enter-text`
- one form task
- one scroll or hover task

This repo still includes the older two-task MiniWoB++ smoke runner:

```bash
AGENTLAB_EXP_ROOT="$PWD/logs/agentlab/studies" \
MINIWOB_URL="file://$PWD/.benchmarks/miniwob-plusplus/miniwob/html/miniwob/" \
uv run --extra agentlab python benchmarks/agentlab/run_miniwob_smoke.py
```

For WebArena, first run one self-hosted task, then a small dependency-safe subset, then WebArena Lite or WebArena-Verified, and only then a full run.

## Outputs

AgentLab saves its normal experiment artifacts under its experiment root. This adapter also writes this agent's native logs under `logs/agentlab/<run_id>/`:

- `agent.log`
- `agent_debug.log`
- `metrics.jsonl`
- `run_summary.json`
- optional `pages/` captures when enabled

Each `AgentInfo` includes a compact markdown summary, numeric stats for AgentLab aggregation, and `extra_info` with the internal trace, tool call summary, log directory, validation signal, step token/cost usage, cumulative token/cost usage, and internal stop reason. AgentLab `stats` token/cost fields are per-step deltas only; cumulative totals live under `extra_info.cumulative_usage` so AgentLab aggregation does not double-count them.

The generic runner writes these additional files inside each AgentLab study directory:

- `benchmark_report.json`: canonical machine-readable report with config, aggregate score, per-task aggregates, failed episodes, warnings, git commit, and environment metadata.
- `benchmark_report.md`: human-readable summary with the command, aggregate score, per-task table, failed-task notes, and reproducibility block.
- `per_task_results.csv`: normalized per-task/per-seed rows derived from AgentLab's `result_df.csv`.
- `failed_tasks.md`: only failed, errored, truncated, incomplete, or zero-reward episodes, with paths to the AgentLab experiment and native `agent.log`/`agent_debug.log` when available.

Score reporting treats missing `cum_reward` values in AgentLab result rows as `0.0`, so incomplete experiments stay in the denominator. The report records those cases under `warnings.parse_gaps`.

`--export-leaderboard-json` writes a local draft artifact for full preset runs only. It is explicitly marked as not submitted; leaderboard submission remains manual.

For clean leaderboard-style comparisons, run a `full` preset from a clean git commit and keep the generated `benchmark_report.json`, `per_task_results.csv`, `result_df.csv`, and draft leaderboard JSON together. Custom task subsets are useful for local regression checks, but they should not be compared as full-suite leaderboard results unless the preset definition explicitly names that exact suite.
