# Zip

Zip is a lightweight, modular browser-use agent for LLM-driven web automation. It runs against arbitrary target URLs, extracts structured browser context, exposes stable element IDs to the model, executes DOM-first browser actions, and records inspectable logs, metrics, and benchmark artifacts for every run.

The project is built as a real browser-agent runtime rather than a one-off benchmark script. The original external challenge site was useful for finding failure modes; the current reproducible benchmark path uses BrowserGym and AgentLab, with MiniWoB++ as the primary recorded result.

## Current Benchmark Result

The latest recorded standard benchmark run is a one-repeat BrowserGym/AgentLab MiniWoB++ full-suite run:

| Benchmark | Episodes | Score | Errors | Model | Cost |
| --- | ---: | ---: | ---: | --- | ---: |
| MiniWoB++ full suite | 125 | 86.4% | 0 | `openrouter` / `z-ai/glm-4.7:nitro` | $7.15 |

Run configuration: unified pipeline, `max_steps=20`, `env_max_steps=10`, `max_elements=80`, `max_worker_tool_calls=10`, `worker_context_steps=3`, `oracle_interval=5`, and sequential execution. The run is documented in [`docs/benchmark-results/miniwob-full-run-2026-05-26.md`](docs/benchmark-results/miniwob-full-run-2026-05-26.md), with failure analysis in [`docs/benchmark-results/miniwob-full-run-2026-05-26-failure-analysis.md`](docs/benchmark-results/miniwob-full-run-2026-05-26-failure-analysis.md).

This is the benchmark track intended for clean comparison and future BrowserGym leaderboard submission. Historical results from the older external challenge site are archived separately as development history.

## What Zip Does

Zip combines browser instrumentation, agent orchestration, semantic tools, and observability:

- Runs against a target URL and task file with `uv run main.py --url <target-url> --task TASK.md`.
- Captures browser context with CDP DOM snapshots, accessibility trees, frame data, handler hints, scroll-container state, text context, widget values, and diffs.
- Gives the LLM stable `el_...` element IDs instead of raw CSS or XPath selectors.
- Uses DOM-first tools for ordinary click, focus, type, read, and form interactions; coordinate tools are reserved for spatial actions such as draw, drag, resize, and SVG/canvas targeting.
- Supports split Oracle -> Filter -> Orchestrator -> Worker mode and a unified single-agent execution mode.
- Records per-run logs, debug traces, JSONL metrics, cost/tokens, page captures, and benchmark reports.
- Adapts the same runtime to BrowserGym pages through AgentLab while leaving BrowserGym in charge of validation and reward.

## Why Browser-Use Agents Are Hard

Browser automation fails in ways that are easy to miss from screenshots or simple DOM dumps:

- Hidden DOM and `data-*` attributes can contain task-critical values.
- Interactive elements can be unlabeled, dynamically inserted, or hidden inside iframes.
- Visual visibility checks can fail because overlays, modals, or z-index layers obscure targets.
- Element labels can be decoys, stale, or unrelated to the current task.
- Buttons and controls can change state without obvious text changes.
- LLM agents can repeat failed actions, declare success too early, or exhaust tool calls.
- Tool feedback can grow large enough to create token and cost blowups.

Zip treats these as runtime and systems problems: context extraction, action semantics, feedback loops, completion policy, state tracking, benchmark validation, and observability.

## Architecture

`main.py` builds runtime configuration and calls `BrowserAgent.run()` in `src/agent/core/agent.py`.

Runtime loop:

1. Launch the browser and open the target URL.
2. Wait for page settlement, extract handler hints, mark scroll containers, capture a CDP snapshot, and compute diffs.
3. Ask the Oracle for guidance when review is due or the agent appears stuck.
4. Filter the snapshot to keep prompt context compact and high-signal.
5. Plan and execute the next step through split Orchestrator -> Worker mode or unified mode.
6. Record tool feedback, memory, metrics, and summaries, then repeat until the task is complete or a stop condition fires.

| Stage | Role |
| --- | --- |
| Context | Browser/session setup, handler extraction, scroll marking, CDP snapshot capture, accessibility/frame data, and diffing. |
| Guidance | Oracle diagnosis and conservative filtering to reduce context without dropping necessary controls. |
| Action | Split Orchestrator -> Worker execution or unified tool-equipped execution. |
| Feedback | Mutation/value/focus/URL feedback, compact history, metrics, cost/tokens, and run summaries. |
| Validation | Native completion policy for direct runs; BrowserGym reward/termination for benchmark runs. |

Key modules:

- `src/agent/context/snapshot.py`: CDP snapshot capture, accessibility/frame correlation, stable element IDs, and context hints.
- `src/agent/context/handlers.py`: inline, React, Vue, and Angular handler introspection.
- `src/agent/tools/semantic.py`: DOM-first and spatial browser tools exposed to the worker.
- `src/agent/core/agent.py`: full browser runtime loop.
- `src/agent/core/step_runtime.py`: reusable single-step runtime for externally owned BrowserGym pages.
- `benchmarks/agentlab/computer_use_agent.py`: AgentLab adapter around the normal Zip runtime.
- `benchmarks/agentlab/run_browsergym_benchmark.py`: MiniWoB/WebArena runner and report generator.

## Default Tool Set

The default worker tool set is intentionally constrained:

- `click_element(element_id)`
- `click_at(element_id, x, y)`
- `focus_element(element_id)`
- `hover_element(element_id, duration_ms=2000)`
- `type_text(element_id, text)`
- `transfer_text(source_id, target_id)`
- `drag_and_drop(source_id, target_id)`
- `pointer_drag(element_id, start_x, start_y, end_x, end_y, steps=12)`
- `set_slider_value(element_id, value)`
- `resize_element(element_id, delta_width, delta_height)`
- `draw(element_id, path)`
- `select_text(element_id, text=None, occurrence=1)`
- `apply_format(command)`
- `read_live_text(element_id=None)`
- `scroll(delta_x, delta_y, element_id=None)`
- `wait(milliseconds)`
- `watch_for_text(text, timeout_ms=10000)`
- `switch_to_iframe(iframe_id)`
- `switch_to_main_frame()`
- `press_key_combination(keys)`

Additional inspection and escape-hatch tools exist in the tool layer, but they are not part of the default worker set.

## Quick Start

Install dependencies:

```bash
uv sync
```

Set an API key for the provider you want to use:

```bash
export OPENROUTER_API_KEY=...
```

Run Zip against a target URL and task file:

```bash
uv run main.py --url <target-url> --task TASK.md
```

Useful variants:

```bash
uv run main.py --url <target-url> --task TASK.md --headless
uv run main.py --url <target-url> --task TASK.md --unified
uv run main.py --url <target-url> --task TASK.md --provider groq
uv run main.py --url <target-url> --task TASK.md --worker-model <model> --filter-model <model> --oracle-model <model>
uv run main.py --url <target-url> --task TASK.md --save-pages
```

Run tests:

```bash
uv run pytest -q
```

## BrowserGym / AgentLab Benchmarks

Install optional benchmark dependencies:

```bash
uv sync --extra agentlab
uv run playwright install chromium
```

Point BrowserGym at MiniWoB++ assets:

```bash
mkdir -p .benchmarks
git clone https://github.com/Farama-Foundation/miniwob-plusplus .benchmarks/miniwob-plusplus
# Or export MINIWOB_URL=file:///path/to/miniwob-plusplus/miniwob/html/miniwob/
```

Run the MiniWoB verification subset:

```bash
uv run --extra agentlab python benchmarks/agentlab/run_browsergym_benchmark.py \
  --benchmark miniwob \
  --preset verify-five \
  --n-repeats 1 \
  --max-steps 20 \
  --env-max-steps 10 \
  --max-elements 80
```

Run the one-repeat MiniWoB full-suite configuration used for the latest recorded comparison:

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

Run a cheap targeted regression subset:

```bash
uv run --extra agentlab python benchmarks/agentlab/run_browsergym_benchmark.py \
  --benchmark miniwob \
  --task-set terminal-readback \
  --iteration-profile cheap \
  --n-repeats 1
```

The generic runner supports MiniWoB, WebArena, WebArena Lite, WebArena Verified, and WebArena Tiny. It writes AgentLab studies under `logs/agentlab/studies/` and emits `benchmark_report.json`, `benchmark_report.md`, `per_task_results.csv`, and `failed_tasks.md` for comparison.

## Outputs

Each native run writes to `logs/<run_id>/`; `logs/latest` points to the most recent run.

- `logs/latest/agent.log`: human-readable runtime log.
- `logs/latest/agent_debug.log`: verbose debug log with prompts, structured outputs, diffs, memory, and traces.
- `logs/latest/metrics.jsonl`: structured events for snapshots, agent calls, tools, tokens, cost, and timings.
- `logs/latest/run_summary.json`: final rollup with stop reason, duration, tokens, cost, provider, and models.
- `logs/latest/pages/`: optional saved HTML snapshots when `--save-pages` is enabled.

AgentLab benchmark runs additionally write report artifacts in each study directory. `AgentInfo.stats` token/cost fields are per-step deltas; cumulative totals are stored under `extra_info.cumulative_usage`.

Analyze timing metrics:

```bash
uv run python scripts/analyze_metrics.py logs/latest/metrics.jsonl
```

## Repository Layout

- `main.py`: CLI entrypoint for direct browser runs.
- `src/agent/`: Zip runtime, context extraction, tools, metrics, prompts, and models.
- `benchmarks/agentlab/`: BrowserGym/AgentLab adapter, benchmark runner, smoke runner, and task-set manifests.
- `tests/`: unit and integration tests for snapshots, tools, completion policy, metrics, AgentLab adapter behavior, and benchmark reports.
- `scripts/analyze_metrics.py`: timing and metrics inspection.
- `scripts/generate_results.py`: archived external-challenge result table generator.
- `scripts/debug_*.py`: development diagnostics from earlier benchmark and runtime investigations.
- `docs/`: architecture notes, observability notes, benchmark results, failure-mode writeups, and long-form engineering articles.

## Historical External Benchmark

Before the BrowserGym path existed, Zip was iterated against an external browser-agent challenge site. That site may no longer be reliably available, so its results are not presented as the current public benchmark. They remain useful as development evidence because the site exposed hidden DOM, hover, iframe, shadow DOM, drawing, drag/drop, WebSocket, service worker, delayed-reveal, and completion-policy failures.

The archived table lives at [`docs/benchmark-results/external-challenge-results.md`](docs/benchmark-results/external-challenge-results.md). Interpret it as an engineering timeline, not a standard benchmark claim. If maintaining old challenge logs, regenerate it with:

```bash
uv run python scripts/generate_results.py
```

## Design Principles

- Prefer DOM-first actions for click, type, focus, and read interactions.
- Use coordinate or pointer tools only when the task is inherently spatial.
- Pass stable element IDs to the LLM, never raw CSS or XPath selectors.
- Keep pruning conservative because over-pruning can make a task impossible.
- Treat tool feedback as part of the agent loop, not just logging.
- Keep benchmark-specific recovery separate from general browser-agent architecture.
- Measure tokens, cost, timing, stop reasons, and tool behavior for every run.

## Limitations

- Zip is experimental and can still repeat bad actions, over-trust page text, or fail on complex application state.
- BrowserGym results depend on model/provider behavior, pricing, and benchmark setup.
- The latest MiniWoB++ result is one repeat, not a multi-seed leaderboard submission.
- Some recovery code for the older external benchmark remains in the direct runtime and should be isolated if Zip becomes a reusable package.
- The default worker tool set intentionally excludes powerful escape hatches such as arbitrary JavaScript execution.

## Roadmap

- Prepare clean MiniWoB++ artifacts for official BrowserGym leaderboard submission.
- Repeat full-suite runs to distinguish regressions from seed/model variance.
- Broaden BrowserGym coverage into WebArena-family runs.
- Isolate older benchmark-specific recovery code from the reusable runtime.
- Build a simple run viewer for logs, metrics, page captures, and step traces.
- Improve failure classification and task-completion validation.
