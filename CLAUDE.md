# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A general-purpose browser agent. Python 3.12, managed with [uv](https://docs.astral.sh/uv/).

## Commands

- `uv sync` — install/update dependencies
- `uv sync --extra agentlab` — install optional AgentLab/BrowserGym benchmark dependencies
- `uv run main.py --url <target> --task TASK.md` — run the agent
- `uv run --extra agentlab python benchmarks/agentlab/run_browsergym_benchmark.py --benchmark miniwob --preset verify-five --n-repeats 1 --max-steps 20 --env-max-steps 10 --max-elements 80` — run the MiniWoB verification benchmark
- `uv run --extra agentlab python benchmarks/agentlab/run_browsergym_benchmark.py --benchmark miniwob --task-set terminal-readback --iteration-profile cheap --n-repeats 1` — run a cheap targeted BrowserGym subset
- `uv add <package>` — add a dependency

## Observability

Each run writes to its own `logs/<run_id>/` subdirectory. A `logs/latest` symlink points to the most recent run. Old run directories are pruned at startup (keep last 10 by default; configure with `--max-log-runs`).

- Logs: `logs/latest/agent.log`
- Debug log: `logs/latest/agent_debug.log` (always captures full DEBUG output)
- Metrics: `logs/latest/metrics.jsonl` (disable with `--no-metrics`)
- Run summary: `logs/latest/run_summary.json`
- Page captures: `logs/latest/pages/*.html` (enable with `--save-pages`)
- Page manifest: `logs/latest/pages/manifest.jsonl`
- AgentLab study reports: `logs/agentlab/studies/<study>/benchmark_report.json`, `benchmark_report.md`, `per_task_results.csv`, and `failed_tasks.md`

## Dependencies

- **pydantic-ai** — agent orchestration + structured outputs
- **openai** — OpenAI-compatible client for OpenRouter
- **playwright** — browser automation
- **agentlab/browsergym** — optional benchmark orchestration, task setup, validation, and result aggregation

## Verification

- When possible, write a local debug script (e.g. `debug_<feature>.py`) to verify changes against a minimal test page before running the full agent end-to-end

## Documentation

- When runtime behavior, CLI flags, benchmark workflow, report schemas, AgentInfo fields, or Mermaid-documented architecture changes, update the relevant docs in the same change. At minimum check `README.md`, `docs/architecture.md`, `docs/agentlab-benchmarks.md`, `docs/observability.md`, and `docs/source-map.md`.

## Code Guidelines

- **DO NOT HARDCODE** values, selectors, keywords, or patterns specific to particular websites/challenges
- The agent must be **general-purpose** and work on any website without site-specific logic
- Let the LLM decide what to click/type based on context, not hardcoded rules
- Keep element selection generic: pass stable element IDs to the LLM and let it decide
- Never pass raw CSS/XPath selectors to the LLM
- When significant changes are made, especially to runtime control flow, validation/completion policy, CLI flags, benchmark workflow, logs/reports, or public data fields, update docs and Mermaid diagrams to match the current behavior

## Browser Interaction Principles

- **DOM-first**: Use DOM methods (`.click()`, `.focus()`, `.innerText`) for element interactions — they work through any visual layer (overlays, modals, z-index stacking)
- **CDP coordinates only when required**: Only use `Input.dispatchMouseEvent` (coordinate-based) for actions that genuinely need screen positions (e.g., drag-and-drop, pointer drag, resize, draw). Never gate click/type/read on visibility checks.
- **Minimize CDP round-trips**: Combine operations into single `_call_on_node` calls instead of chaining multiple CDP commands
- **No `onTop` gates for DOM operations**: Don't check `document.elementFromPoint()` or `onTop` before DOM interactions — they bypass visual layering by design

## Tool Docstrings

- Follow the pattern: **What it does. When to use it. Constraints.** — action-first, one sentence each
- Tools that accept `element_id` must include: "Use element_id from the page snapshot."
- Describe behavior, not internals — don't mention CDP, snapshot dicts, or interactivity filters

## Snapshot Scope

- `capture_snapshot` includes interactive elements plus selected structural/text context such as labels, table rows/cells, paragraphs, code/pre, canvas, and SVG hints.
- All HTML attributes are stored in `ElementSnapshot.attributes`, but only 9 are shown to the LLM in `format_snapshot_for_llm()`
- **Handler introspection** (`src/agent/context/handlers.py`): a pre-snapshot `page.evaluate()` extracts JS event handler source from inline handlers and framework internals (React/Vue/Angular), stamps elements with `data-agent-hid`, and the snapshot correlates them. Handler hints appear as `[click:fn(); change:fn()]` in the LLM snapshot. Disable with `--no-handlers`.

## Results Tracking

- After a meaningful agent run, regenerate archived challenge results: `uv run python scripts/generate_results.py`
- Review `results.md` diff before committing to track progress/regressions
- For BrowserGym benchmarks, use `benchmarks/agentlab/run_browsergym_benchmark.py`; missing `cum_reward` rows are scored as zero and reported in `warnings.parse_gaps`
- `AgentInfo.stats` token/cost fields are per-step deltas; cumulative native run totals live under `extra_info.cumulative_usage`.

## Architecture

- Entry point: `main.py`
- Core modules live in `src/agent/`
- Uses PydanticAI for orchestration and OpenRouter/Cerebras/Groq provider access
- Uses CDP for context extraction and Playwright for action execution
- Default pipeline: **Handler extraction** (optional JS introspection) → **Filter** (conservative tree pruner) → **Oracle** (periodic + stuck health check) → **Orchestrator** (goal planner using element IDs) → **Worker** (browser executor, sees only goal + pruned snapshot)
- Unified mode skips the Orchestrator/Worker split after Oracle/Filter and uses a single tool-equipped agent
- AgentLab path: `benchmarks/agentlab/computer_use_agent.py` runs the runtime inside BrowserGym-owned pages; `benchmarks/agentlab/run_browsergym_benchmark.py` selects MiniWoB/WebArena benchmarks and writes reports
- Handler extraction runs a single `page.evaluate()` before snapshot capture; stamps elements with `data-agent-hid` for correlation, cleaned up after snapshot
- Oracle advice + diff are fed into the filter; filter cache is invalidated when Oracle intervenes
- Per-role model support: `--worker-model`, `--filter-model`, `--oracle-model`
- LLM resilience: `ResilientModel` wraps each model with per-category retries (429/5xx/network); step-level try/except provides graceful degradation (filter falls back to full snapshot, orchestrator retries once then skips, worker records failure). See `docs/llm-resilience.md`.
