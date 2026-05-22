# Archived Benchmark Results

The project was developed against an external browser-agent challenge site. That site may no longer be reliably available, so these results should be read as historical development evidence rather than a currently reproducible public benchmark.

The raw generated table is kept in `results.md`. It is produced by `scripts/generate_results.py`, which scans run logs, preserves history in `logs/results_history.json`, and regenerates the Markdown table.

## What The Benchmark Was Useful For

The benchmark forced the agent through a wide range of browser interaction patterns:

- Click and form tasks.
- Hidden DOM and visible-code tasks.
- Delayed reveal and transient content.
- Hover-dependent UI.
- Drag/drop and drawing tasks.
- Keyboard sequences.
- Shadow DOM and iframe cases.
- Service worker and WebSocket behavior.
- Disabled-to-enabled transitions.

This made it useful as a failure-mode generator even when individual site bugs required benchmark-specific investigation.

## How Results Are Recorded

Each run summary records:

- Run ID.
- Git commit.
- Provider and model.
- Per-role models when configured.
- Duration and active duration.
- Token usage.
- Cost when available.
- Step count and stop reason.

`scripts/generate_results.py` also parses snapshot URLs to estimate the furthest challenge step reached and uses `logs/full_challenge_map.json` when available to label the challenge type where a run stopped.

## Interpreting The Table

When reading `results.md`, treat it as a development timeline:

- Early runs reflect the original and transitional harness behavior.
- Middle runs reflect the modular architecture and core reliability work.
- Later runs focus more on provider routing, cost reporting, token compaction, and per-step overhead.

The table is most useful for comparing regressions and improvements across commits, not for making broad model benchmark claims.

## Important Caveats

- The external benchmark may be unavailable.
- Historical runs may include interrupted runs.
- Some runs include benchmark-specific recovery logic.
- Results depend on model/provider availability and pricing at the time of the run.
- Some costs are estimated from local pricing maps when provider-side cost metadata is unavailable.

## Benchmark-Specific Fixes

The repository contains documented investigations for benchmark-site issues, including:

- React state leak on back-to-back math puzzle steps.
- Nested iframe frame ID mismatch.
- Recursive iframe off-by-one behavior.
- Final-step code reveal behavior.
- Retina/HiDPI viewport classification issues.

These are valuable debugging case studies, but they should not be presented as general-purpose browser-agent features.

## Current Reproducibility Path

The archived external benchmark results remain historical, but the repository now has a reproducible BrowserGym/AgentLab path. Start with the MiniWoB verification preset documented in `docs/agentlab-benchmarks.md`; it runs under BrowserGym validation and writes `benchmark_report.json`, `benchmark_report.md`, `per_task_results.csv`, and `failed_tasks.md`.

The next reproducibility work is to broaden BrowserGym coverage beyond the verified MiniWoB subset and keep full-suite runs separate from custom local regression subsets.

## Recorded BrowserGym Runs

- `2026-05-21`: MiniWoB full suite, one repeat, `125` episodes, `58.4%` score, `$38.80` logged cost. See `docs/benchmark-results/miniwob-full-run-2026-05-21.md`.
