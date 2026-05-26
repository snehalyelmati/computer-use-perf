# Benchmark Results

Zip's primary reproducible benchmark path is BrowserGym through AgentLab. BrowserGym owns task setup, browser lifecycle, rewards, termination, and validation; Zip runs its normal context extraction, agent loop, semantic tools, metrics, and reporting inside the live BrowserGym page.

## Current Headline Result

| Date | Benchmark | Episodes | Score | Errors | Model | Cost | Notes |
| --- | --- | ---: | ---: | ---: | --- | ---: | --- |
| 2026-05-26 | MiniWoB++ full suite, one repeat | 125 | 86.4% | 0 | `openrouter` / `z-ai/glm-4.7:nitro` | $7.15 | [summary](miniwob-full-run-2026-05-26.md), [failure analysis](miniwob-full-run-2026-05-26-failure-analysis.md) |
| 2026-05-21 | MiniWoB++ full suite, one repeat | 125 | 58.4% | 0 | `openrouter` / `z-ai/glm-4.7:nitro` | $38.80 | [summary](miniwob-full-run-2026-05-21.md), [failure analysis](miniwob-full-run-2026-05-21-failure-analysis.md) |

The 2026-05-26 run is the current comparison point and the basis for future BrowserGym leaderboard submission work. Treat it as a recorded one-repeat run, not a final multi-seed leaderboard claim.

## Reproducible BrowserGym Command

BrowserGym refers to MiniWoB++ with the benchmark key `miniwob`.

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

The generic runner writes these artifacts inside each AgentLab study directory:

- `benchmark_report.json`
- `benchmark_report.md`
- `per_task_results.csv`
- `failed_tasks.md`
- AgentLab's `result_df.csv`, `summary_df.csv`, `error_report.md`, and `study.pkl.gz`

Because `logs/` is gitignored, version-controlled run notes in this directory are the durable index for recorded benchmark runs.

## Interpreting BrowserGym Results

Use the BrowserGym/AgentLab reports for standard comparisons:

- BrowserGym reward and termination are the source of truth for success.
- Missing `cum_reward` rows are counted as zero reward by the report generator.
- Scores depend on model/provider behavior, runtime caps, and benchmark setup.
- Custom task-set runs are useful for local regression work but should not be compared as full-suite results.
- Full-suite leaderboard-style runs should keep `benchmark_report.json`, `per_task_results.csv`, `result_df.csv`, and any exported leaderboard JSON together.

## Archived External Challenge History

Before the BrowserGym path existed, Zip was iterated against an external browser-agent challenge site. That site may no longer be reliably available, so those results are archived as development history rather than standard benchmark evidence.

The raw generated table is kept in [`external-challenge-results.md`](external-challenge-results.md). It is produced by `scripts/generate_results.py`, which scans run logs, preserves history in `logs/results_history.json`, and regenerates the Markdown table.

That external benchmark was still useful because it forced the agent through a wide range of browser interaction patterns:

- Click and form tasks.
- Hidden DOM and visible-code tasks.
- Delayed reveal and transient content.
- Hover-dependent UI.
- Drag/drop and drawing tasks.
- Keyboard sequences.
- Shadow DOM and iframe cases.
- Service worker and WebSocket behavior.
- Disabled-to-enabled transitions.

Read `external-challenge-results.md` as an engineering timeline: useful for understanding regressions and design pressure, not for making current model benchmark claims.
