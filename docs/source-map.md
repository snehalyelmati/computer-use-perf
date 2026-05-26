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
- Snapshot entries can include context hints for nearby labels/tables/text and widget hints for values, drag handlers, and geometry.

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
- `click_at`
- `focus_element`
- `hover_element`
- `type_text`
- `transfer_text`
- `drag_and_drop`
- `pointer_drag`
- `set_slider_value`
- `resize_element`
- `draw`
- `select_text`
- `apply_format`
- `read_live_text`
- `scroll`
- `wait`
- `watch_for_text`
- `switch_to_iframe`
- `switch_to_main_frame`
- `press_key_combination`

Tools implemented but not in the default worker tool set include `find_elements`, `inspect_element`, `search_page_attributes`, `navigate_to`, `take_screenshot`, and `execute_js`.

## Observability

- Metrics and run directories: `src/agent/metrics.py`.
- Page capture: `src/agent/capture/page_saver.py`.
- Metrics analysis scripts: `scripts/analyze_metrics.py`, `scripts/analyze_last_run.py`.
- Result generation: `scripts/generate_results.py`.
- BrowserGym benchmark reporting: `benchmarks/agentlab/run_browsergym_benchmark.py`.

Verified facts:

- Each run writes to `logs/<run_id>/` and updates `logs/latest`.
- Metrics are JSONL events written to `metrics.jsonl`.
- Run summaries include provider, models, duration, retry wait, step count, stop reason, tokens, and cost.
- Page HTML capture is optional via `--save-pages`.

## AgentLab Benchmarks

- Adapter: `benchmarks/agentlab/computer_use_agent.py`.
- Generic runner and report generation: `benchmarks/agentlab/run_browsergym_benchmark.py`.
- Legacy MiniWoB smoke runner: `benchmarks/agentlab/run_miniwob_smoke.py`.
- Regression tests: `tests/test_agentlab_adapter.py`, `tests/test_browsergym_benchmark_runner.py`.

Verified facts:

- BrowserGym owns task setup, browser lifecycle, rewards, termination, and validation.
- The adapter requests `use_raw_page_output=True`, stores the raw Playwright page, passes BrowserGym reward/termination into the runtime as external validation, and returns `noop()` after the internal agent mutates the live page.
- The generic runner supports MiniWoB, WebArena, WebArena Lite, WebArena Verified, and WebArena Tiny.
- MiniWoB `verify-five` is a five-task verification subset; MiniWoB `full` uses BrowserGym's default suite with `n_repeats=5` unless overridden.
- Iteration profiles are `full`, `balanced`, and `cheap`; checked-in task-set manifests live under `benchmarks/agentlab/task_sets/`.
- WebArena variants use AgentLab's `ray` backend when `--n-jobs > 1` so BrowserGym task dependencies are preserved.
- Benchmark reports count missing `cum_reward` rows as zero reward and include those gaps in `warnings.parse_gaps`.
- Report artifacts are `benchmark_report.json`, `benchmark_report.md`, `per_task_results.csv`, and `failed_tasks.md`.
- AgentLab `AgentInfo.stats` token/cost values are per-step deltas; cumulative totals live in `extra_info.cumulative_usage`.
- Version-controlled BrowserGym result notes live under `docs/benchmark-results/`; the latest recorded one-repeat MiniWoB full-suite note is `docs/benchmark-results/miniwob-full-run-2026-05-26.md`.

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

AgentLab and BrowserGym commits:

- Task file support replacing direct goal CLI usage: `1ce84c4`.
- AgentLab BrowserGym adapter and Python 3.12 compatibility: `3a41cf5`.
- MiniWoB smoke benchmark runner and BrowserGym sync bridge: `75159be`.
- MiniWoB benchmark defaults, option selection, and unified no-action done gating: `55d29be`.
- MiniWoB visual handling, `click_at`, benchmark shutdown/logging hardening, and resource tracker suppression: `343a670`.
- MiniWoB benchmark reliability improvements: `9efe402`.
- MiniWoB interaction handling improvements: `11f2268`.
- Text selection and resize validation improvements: `368ce18`.

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
