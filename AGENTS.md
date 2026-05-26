# AGENTS.md

Build/Test/Lint:
- Install deps: `uv sync`
- Install benchmark deps: `uv sync --extra agentlab`
- Run agent: `uv run main.py --url <target> --task TASK.md`
- Run tests: `uv run pytest -q`

Common Commands:
- `uv run main.py --url <target> --task TASK.md` - run the agent
- `uv run main.py --url <target> --task TASK.md --oracle-interval 5 --max-tokens 2048` - run with explicit defaults
- `uv run main.py --url <target> --task TASK.md --worker-model <model> --filter-model <model> --oracle-model <model>` - per-role models
- `uv run --extra agentlab python benchmarks/agentlab/run_browsergym_benchmark.py --benchmark miniwob --preset verify-five --n-repeats 1 --max-steps 20 --env-max-steps 10 --max-elements 80` - run MiniWoB verification benchmark
- `uv run --extra agentlab python benchmarks/agentlab/run_browsergym_benchmark.py --benchmark miniwob --task-set terminal-readback --iteration-profile cheap --n-repeats 1` - run a cheap targeted BrowserGym subset
- `uv add <package>` - add a dependency

Observability:
- Each run writes to `logs/<run_id>/`; `logs/latest` symlink points to the most recent run. Old runs pruned at startup (default: keep 10; `--max-log-runs`).
- Logs: `logs/latest/agent.log`
- Metrics: `logs/latest/metrics.jsonl` (disable via `--no-metrics`)
- Run summary: `logs/latest/run_summary.json`
- AgentLab studies: `logs/agentlab/studies/<study>/` with `benchmark_report.json`, `benchmark_report.md`, `per_task_results.csv`, and `failed_tasks.md`

Results Tracking:
- `logs/` is gitignored, so meaningful BrowserGym/AgentLab benchmark runs need a version-controlled note under `docs/benchmark-results/` with the study path, command, config, score, cost/tokens, runtime, and primary artifacts.
- Keep `docs/benchmark-results/README.md` updated with an index entry for each recorded BrowserGym/AgentLab run.
- When runtime behavior, CLI flags, benchmark workflow, report schemas, AgentInfo fields, or Mermaid-documented architecture changes, update the relevant docs in the same change. At minimum check `README.md`, `docs/architecture.md`, `docs/agentlab-benchmarks.md`, `docs/observability.md`, and `docs/source-map.md`.

Dependencies:
- `pydantic-ai` - agent orchestration + structured output
- `openai` - OpenAI-compatible client for OpenRouter
- `playwright` - browser automation
- Optional benchmark deps: `agentlab`, `browsergym`, `browsergym-miniwob`, `browsergym-webarena`

Architecture:
- Root entrypoint in `main.py`; core modules live under `src/agent/`.
- Uses PydanticAI for orchestration and structured outputs.
- Uses OpenRouter (OpenAI-compatible), Cerebras, or Groq for LLM access.
- Uses CDP for context extraction and Playwright for action execution.
- Default pipeline: **Handler extraction** (optional JS introspection) → **Filter** (conservative tree pruner) → **Oracle** (periodic + stuck health check) → **Orchestrator** (goal planner using element IDs) → **Worker** (browser executor using the delegated goal, compact recent state, useful text, and pruned snapshot). Unified mode skips the Orchestrator/Worker split after Oracle/Filter and uses a single tool-equipped agent.
- Benchmark path: `benchmarks/agentlab/computer_use_agent.py` adapts the runtime to BrowserGym-owned pages; `benchmarks/agentlab/run_browsergym_benchmark.py` selects BrowserGym benchmarks and writes report artifacts.
- Handler extraction runs a single `page.evaluate()` before snapshot capture; stamps elements with `data-agent-hid` for correlation, cleaned up after snapshot. Disable with `--no-handlers`.
- Oracle advice + diff are fed into the filter; filter cache is invalidated when Oracle intervenes with `all_clear=false`.
- No database or server components.

Agent Responsibilities:
- **Filter**: Receives full snapshot tree + diff + Oracle advice. Conservatively removes only obvious filler elements. Cached when page fingerprint is unchanged.
- **Oracle**: Reviews the execution trace (step history with URLs, goals, outcomes, diff stats). Fires periodically (every N steps) and when stuck. Issues directives the orchestrator must follow.
- **Orchestrator**: Plans the next sub-goal using stable element IDs from the pruned snapshot. Follows Oracle directives when present.
- **Worker**: Executes the delegated goal using semantic tools. Receives compact recent state, useful text, and the pruned snapshot.

Verification:
- When possible, write a local debug script (e.g. `debug_<feature>.py`) to verify changes against a minimal test page before running the full agent end-to-end.

Code Guidelines:
- DO NOT hardcode values, selectors, keywords, or patterns specific to particular websites/challenges.
- The agent must be general-purpose and work on any website without site-specific logic.
- Let the LLM decide what to click/type based on context, not hardcoded rules.
- Keep element selection generic: pass stable element IDs to the LLM and let it decide.
- Never pass raw CSS/XPath selectors to the LLM.
- When significant changes are made, especially to runtime control flow, validation/completion policy, CLI flags, benchmark workflow, logs/reports, or public data fields, update docs and Mermaid diagrams to match the current behavior.

Code Style:
- Prefer small, pure helper functions and explicit types in signatures.
- Use stdlib imports first, then third-party (pydantic_ai, openai, playwright).
- Avoid magic strings; centralize prompts/constants near top of file.
- Error handling should log context and keep the loop resilient.

Tool Docstrings:
- Follow the pattern: What it does. When to use it. Constraints. — action-first, one sentence each.
- Tools that accept `element_id` must include: "Use element_id from the page snapshot."
- Describe behavior, not internals — don't mention CDP, snapshot dicts, or interactivity filters.

Snapshot Scope:
- `capture_snapshot` includes interactive elements plus selected structural/text context such as labels, table rows/cells, paragraphs, code/pre, canvas, and SVG hints.
- All HTML attributes are stored in `ElementSnapshot.attributes`, but only 9 are shown to the LLM in `format_snapshot_for_llm()`.
- Handler introspection (`src/agent/context/handlers.py`): extracts JS event handler source from inline handlers and framework internals (React/Vue/Angular). Handler hints appear as `[click:fn(); change:fn()]` in the LLM snapshot.

Browser Interaction Principles:
- DOM-first: use DOM methods (`.click()`, `.focus()`, `.innerText`) — they work through any visual layer (overlays, modals, z-index stacking).
- CDP coordinates only when required: only use `Input.dispatchMouseEvent` for actions needing screen positions (e.g., drag-and-drop, pointer drag, resize, draw). Never gate click/type/read on visibility checks.
- Minimize CDP round-trips: combine operations into single `_call_on_node` calls.
- No `onTop` gates for DOM operations: don't check `elementFromPoint()` before DOM interactions.
