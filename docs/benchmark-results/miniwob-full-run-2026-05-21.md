# MiniWoB++ Full Suite Run - 2026-05-21

This records the one-repeat full MiniWoB++ run for Zip started at study timestamp `2026-05-21_22-05-01`. BrowserGym refers to this benchmark with the key `miniwob`.

## Storage Status

The run artifacts are stored locally under the original study path:

`logs/agentlab/studies/2026-05-21_22-05-01_computer-use-agent-on-miniwob-full/`

That directory contains the expected AgentLab and runner outputs:

- `benchmark_report.md`
- `benchmark_report.json`
- `per_task_results.csv`
- `failed_tasks.md`
- `result_df.csv`
- `summary_df.csv`
- `error_report.md`
- `study.pkl.gz`

Important caveat: `logs/` is gitignored, so the raw artifacts are not version-controlled. This file is the version-controlled index entry for the run. Keep the raw study directory intact when comparing or auditing the result.

The retained path includes the earlier `computer-use-agent` working name because it was created before the Zip rename.

## Command

```bash
uv run --extra agentlab python benchmarks/agentlab/run_browsergym_benchmark.py \
  --benchmark miniwob \
  --preset full \
  --n-repeats 1
```

The report also captured this local absolute Python invocation from the original checkout:

```bash
/Users/snehalyelmati/Documents/computer-use-perf/.venv/bin/python3 benchmarks/agentlab/run_browsergym_benchmark.py --benchmark miniwob --preset full --n-repeats 1
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
- Git commit: `3dbd436`

## Results

- Score: `58.4%`
- Average reward: `0.584 +/- 0.044`
- Completed episodes: `125 / 125`
- Reward-positive successes: `73`
- Zero-reward failures: `52`
- Errors: `0`
- Truncated episodes: `20`
- Incomplete episodes: `0`

An all-cases review and failure backlog is recorded in [miniwob-full-run-2026-05-21-failure-analysis.md](miniwob-full-run-2026-05-21-failure-analysis.md).

## Cost And Runtime

Cost and token totals were aggregated from `result_df.csv`.

- Total cost: `$38.80079007`
- Average cost per episode: `$0.31040632056`
- Total tokens: `18,303,706`
- Input tokens: `14,830,263`
- Output tokens: `3,473,443`
- BrowserGym episode steps: `347`
- Internal Zip steps: `1,333`
- Agent elapsed time: `6,080.66s` (`101.3 min`)
- Approximate wall-clock runtime: `1h52m`

Cost was concentrated in failed and truncated tasks:

- Successful tasks: `73`, `$3.14709066`, `1,568,871` tokens
- Failed tasks: `52`, `$35.65369941`, `16,734,835` tokens
- Truncated tasks: `20`, `$33.52879315`, `15,620,900` tokens

## Top Cost Drivers

| Task | Seed | Reward | Steps | Cost | Tokens | Truncated |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `miniwob.email-inbox-important` | 28 | 0 | 10 | `$3.22165` | 1,392,592 | true |
| `miniwob.email-inbox-star-reply` | 6 | 0 | 10 | `$3.2212041` | 1,410,650 | true |
| `miniwob.email-inbox-delete` | 17 | 0 | 10 | `$2.54015175` | 1,077,297 | true |
| `miniwob.email-inbox-noscroll` | 0 | 0 | 10 | `$2.47731295` | 1,275,004 | true |
| `miniwob.email-inbox` | 15 | 0 | 10 | `$2.42258021` | 1,236,452 | true |

## Primary Artifacts

- `logs/agentlab/studies/2026-05-21_22-05-01_computer-use-agent-on-miniwob-full/benchmark_report.md`
- `logs/agentlab/studies/2026-05-21_22-05-01_computer-use-agent-on-miniwob-full/benchmark_report.json`
- `logs/agentlab/studies/2026-05-21_22-05-01_computer-use-agent-on-miniwob-full/per_task_results.csv`
- `logs/agentlab/studies/2026-05-21_22-05-01_computer-use-agent-on-miniwob-full/failed_tasks.md`
- `logs/agentlab/studies/2026-05-21_22-05-01_computer-use-agent-on-miniwob-full/result_df.csv`
