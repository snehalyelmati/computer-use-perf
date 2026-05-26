# MiniWoB Full Suite Run - 2026-05-26

This records the one-repeat full MiniWoB run started at study timestamp `2026-05-26_15-41-13`.

## Storage Status

The run artifacts are stored locally under:

`logs/agentlab/studies/2026-05-26_15-41-13_computer-use-agent-on-miniwob-full/`

That directory contains the expected AgentLab and runner outputs:

- `benchmark_report.md`
- `benchmark_report.json`
- `per_task_results.csv`
- `failed_tasks.md`
- `result_df.csv`
- `summary_df.csv`
- `error_report.md`
- `study.pkl.gz`

Important caveat: `logs/` is gitignored, so the raw artifacts are not version-controlled. This file is the version-controlled index entry for the run. The native per-episode `logs/agentlab/<run_id>/` directories may be pruned independently; missing native logs in report warnings do not invalidate the study artifacts.

## Command

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

The report records the resolved Python invocation as:

```bash
/Users/snehalyelmati/Documents/computer-use-perf/.venv/bin/python3 benchmarks/agentlab/run_browsergym_benchmark.py --benchmark miniwob --preset full --n-repeats 1 --max-steps 20 --env-max-steps 10 --max-elements 80 --n-jobs 1
```

## Configuration

- Benchmark: `miniwob:full`
- Repeats: `1`
- Tasks / episodes: `125`
- Provider: `openrouter`
- Model: `z-ai/glm-4.7:nitro`
- Pipeline: unified mode
- Max steps: `20`
- Env max steps: `10`
- Max elements: `80`
- Max worker tool calls: `10`
- Worker context steps: `3`
- Oracle interval: `5`
- Git commit: `368ce18`

## Results

- Score: `86.4%`
- Average reward: `0.864 +/- 0.031`
- Completed episodes: `125 / 125`
- Reward-positive successes: `108`
- Zero-reward failures: `17`
- Errors: `0`
- Truncated episodes: `4`
- Incomplete episodes: `0`

A remaining-failures review is recorded in [miniwob-full-run-2026-05-26-failure-analysis.md](miniwob-full-run-2026-05-26-failure-analysis.md).

## Cost And Runtime

Cost and token totals were aggregated from the per-episode `summary_info.json` files in the study directory.

- Total cost: `$7.14561305`
- Average cost per episode: `$0.05716490`
- Total tokens: `3,378,433`
- BrowserGym episode steps: `187`
- Internal computer-use steps: `300`
- Agent elapsed time: `2,027.89s` (`33.8 min`)

## Failed Tasks

| Task | Seed | Reward | Steps | Truncated |
| --- | ---: | ---: | ---: | --- |
| `miniwob.circle-center` | 2 | 0 | 1 | false |
| `miniwob.click-menu` | 2 | 0 | 1 | false |
| `miniwob.click-pie` | 8 | 0 | 2 | false |
| `miniwob.count-sides` | 3 | 0 | 1 | false |
| `miniwob.daily-calendar` | 28 | 0 | 10 | true |
| `miniwob.drag-cube` | 33 | 0 | 10 | true |
| `miniwob.drag-shapes` | 30 | 0 | 1 | false |
| `miniwob.drag-shapes-2` | 14 | 0 | 3 | false |
| `miniwob.email-inbox-nl-turk` | 14 | 0 | 10 | true |
| `miniwob.enter-date` | 8 | 0 | 1 | false |
| `miniwob.find-midpoint` | 7 | 0 | 1 | false |
| `miniwob.hot-cold` | 21 | 0 | 10 | true |
| `miniwob.number-checkboxes` | 25 | 0 | 1 | false |
| `miniwob.resize-textarea` | 25 | 0 | 1 | false |
| `miniwob.search-engine` | 3 | 0 | 1 | false |
| `miniwob.text-transform` | 1 | 0 | 4 | false |
| `miniwob.tic-tac-toe` | 5 | 0 | 1 | false |

## Primary Artifacts

- `logs/agentlab/studies/2026-05-26_15-41-13_computer-use-agent-on-miniwob-full/benchmark_report.md`
- `logs/agentlab/studies/2026-05-26_15-41-13_computer-use-agent-on-miniwob-full/benchmark_report.json`
- `logs/agentlab/studies/2026-05-26_15-41-13_computer-use-agent-on-miniwob-full/per_task_results.csv`
- `logs/agentlab/studies/2026-05-26_15-41-13_computer-use-agent-on-miniwob-full/failed_tasks.md`
- `logs/agentlab/studies/2026-05-26_15-41-13_computer-use-agent-on-miniwob-full/result_df.csv`
