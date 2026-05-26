# Scripts

This directory contains local utilities used while developing and validating Zip.

## Maintained Utilities

- `analyze_metrics.py`: summarize timing/cost metrics from a run's `metrics.jsonl`.
- `analyze_last_run.py`: inspect the latest native run logs.
- `generate_results.py`: regenerate the archived external-challenge results table under `docs/benchmark-results/`.
- `serve_pages.py`: serve saved page captures for local inspection.

## Diagnostic Scripts

Files named `debug_*.py` and `prove_state_leak.py` are retained as diagnostic artifacts from runtime and benchmark investigations. Many are tied to the archived external challenge site and are not part of the main Zip run path or the current BrowserGym benchmark workflow.
