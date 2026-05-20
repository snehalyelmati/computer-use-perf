# Documentation Source Map

This document records where the main documentation claims should be verified. It is intended to keep README, docs, and engineering writeups grounded in code and commit history.

## Current Runtime

- Entrypoint and CLI flags: `main.py`.
- Main loop: `src/agent/core/agent.py`, especially `BrowserAgent.run()`.
- Runtime configuration: `src/agent/config.py`.
- Browser launch and cleanup: `src/agent/browser/session.py`.

Verified facts:

- `--url` and `--task` are required by the CLI.
- The default runtime uses Orchestrator and Worker agents after snapshot filtering.
- `--unified` uses a single tool-equipped agent after Oracle/Filter preprocessing.
- Supported providers in config are OpenRouter, Cerebras, and Groq.

## Snapshot And Context Extraction

- CDP snapshot capture: `src/agent/context/snapshot.py`.
- Handler extraction: `src/agent/context/handlers.py`.
- Scroll container marking: `src/agent/context/scroll_containers.py`.

Verified facts:

- Snapshot capture calls `DOMSnapshot.captureSnapshot`, `Accessibility.getFullAXTree`, and `Page.getFrameTree`.
- Stable IDs are generated with SHA-256-derived `el_...` IDs.
- Handler extraction supports inline handlers and framework internals for React, Vue, and Angular.
- Elements with detected handlers can be upgraded to interactive snapshot entries.

## Agents And Structured Outputs

- Agent builders: `src/agent/core/agent.py`.
- Prompt text: `src/agent/prompts/system.py`.
- Pydantic output models: `src/agent/models/actions.py`.

Verified facts:

- `SnapshotFilterOutput` returns useful text lines and priority element IDs.
- `OracleAdvice` returns `all_clear`, `diagnosis`, `recommendation`, and `avoid`.
- `OrchestratorDecision` returns `done`, `worker`, `worker_goal`, and optional rationale.
- `StepOutput` means the delegated worker goal is complete, not necessarily the whole run.
- `UnifiedStepOutput.done` means the overall goal is complete.

## Tools

- Tool registration and default tool gate: `src/agent/core/agent.py`.
- Tool implementations: `src/agent/tools/semantic.py`.

Verified default worker tools:

- `click_element`
- `hover_element`
- `type_text`
- `drag_and_drop`
- `draw`
- `scroll`
- `wait`
- `watch_for_text`
- `switch_to_iframe`
- `switch_to_main_frame`
- `press_key_combination`

Tools implemented but not in the default worker tool set include `inspect_element`, `search_page_attributes`, `navigate_to`, `take_screenshot`, and `execute_js`.

## Observability

- Metrics and run directories: `src/agent/metrics.py`.
- Page capture: `src/agent/capture/page_saver.py`.
- Metrics analysis scripts: `scripts/analyze_metrics.py`, `scripts/analyze_last_run.py`, `scripts/visualize_api_calls.py`.
- Result generation: `scripts/generate_results.py`.

Verified facts:

- Each run writes to `logs/<run_id>/` and updates `logs/latest`.
- Metrics are JSONL events written to `metrics.jsonl`.
- Run summaries include provider, models, duration, retry wait, step count, stop reason, tokens, and cost.
- Page HTML capture is optional via `--save-pages`.

## Historical Commit Sources

Pre-modular harness commits:

- Initial observe-act harness: `f5bd651`.
- State-hash stuck detection and observability: `7a4222d`.
- Challenge-level memory and enriched elements: `59b23d2`.
- Hidden content and data attribute extraction: `2558f20`.
- Action expansion for hover, drag, key, wait, draw, watch: `2b201c8`, `f3bdb02`, `6524c25`, `f2dcaaa`.
- Stuck recovery and anti-fabrication prompts: `7734796`, `b56b756`.
- GOAL/TASK separation and media/text diff support: `e9b894c`.
- Oracle wrong-goal detection: `9c292b8`.

Modular architecture commits:

- Restructure agent scaffold: `76ce01b`.
- Hashed element IDs and overlay-aware tools: `c121db8`.
- Metrics outputs: `3e3b1e8`.
- DOM-first tools: `4312336`.
- MutationObserver feedback: `bbacb91`.
- Tree snapshot: `029f296`.
- Separation of concerns refactor: `e99e2ab`.
- Handler introspection: `cc3e919`.
- Page replay capture: `0b07dcc`.
- Unified mode: `1f59c30`.
- Result tracker: `e8a1040`.
- Dynamic element IDs from mutation feedback: `367d8f9`.
- Tool-return history compaction: `802664d`.

Benchmark-specific commits:

- Challenge map documentation and scripts: `e2cd5ae`.
- Nested iframe frame ID fix: `a322fbf`.
- Back-to-back puzzle state leak fix: `9035161`.
- Recursive iframe challenge workaround: `1495563`.
- Final-step finish navigation workaround: `3ba7238`, `8f331da`.

## Existing Docs To Keep In Sync

- `docs/observability.md`
- `docs/architecture-options.md`
- `docs/challenge-map.md`
- `docs/qwen3-prompting-guide.md`
- `results.md`
