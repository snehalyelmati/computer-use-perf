# AGENTS.md

Build/Test/Lint:
- Install deps: `uv sync`
- Run agent: `uv run main.py --url <target> --goal "<task>"`
- Run tests: `uv run pytest -q`

Common Commands:
- `uv run main.py --url <target> --goal "<task>"` - run the agent
- `uv run main.py --url <target> --goal "<task>" --oracle-interval 5 --max-tokens 2048` - run with explicit defaults
- `uv run main.py --url <target> --goal "<task>" --worker-model <model> --filter-model <model> --oracle-model <model>` - per-role models
- `uv add <package>` - add a dependency

Observability:
- Logs: `logs/agent.log`
- Metrics: `logs/metrics.jsonl` (disable via `--no-metrics`)
- Run summary: `logs/run_summary.json`

Dependencies:
- `pydantic-ai` - agent orchestration + structured output
- `openai` - OpenAI-compatible client for OpenRouter
- `playwright` - browser automation

Architecture:
- Root entrypoint in `main.py`; core modules live under `src/agent/`.
- Uses PydanticAI for orchestration and structured outputs.
- Uses OpenRouter (OpenAI-compatible) or Cerebras for LLM access.
- Uses CDP for context extraction and Playwright for action execution.
- Pipeline: **Handler extraction** (optional JS introspection) → **Filter** (conservative tree pruner) → **Oracle** (periodic + stuck health check) → **Orchestrator** (goal planner using element IDs) → **Worker** (browser executor, sees only goal + pruned snapshot).
- Handler extraction runs a single `page.evaluate()` before snapshot capture; stamps elements with `data-agent-hid` for correlation, cleaned up after snapshot. Disable with `--no-handlers`.
- Oracle advice + diff are fed into the filter; filter cache is invalidated when Oracle intervenes with `all_clear=false`.
- No database or server components.

Agent Responsibilities:
- **Filter**: Receives full snapshot tree + diff + Oracle advice. Conservatively removes only obvious filler elements. Cached when page fingerprint is unchanged.
- **Oracle**: Reviews the execution trace (step history with URLs, goals, outcomes, diff stats). Fires periodically (every N steps) and when stuck. Issues directives the orchestrator must follow.
- **Orchestrator**: Plans the next sub-goal using stable element IDs from the pruned snapshot. Follows Oracle directives when present.
- **Worker**: Executes the goal using semantic tools. Receives only the goal + pruned snapshot (no memory, no progress info).

Verification:
- When possible, write a local debug script (e.g. `debug_<feature>.py`) to verify changes against a minimal test page before running the full agent end-to-end.

Code Guidelines:
- DO NOT hardcode values, selectors, keywords, or patterns specific to particular websites/challenges.
- The agent must be general-purpose and work on any website without site-specific logic.
- Let the LLM decide what to click/type based on context, not hardcoded rules.
- Keep element selection generic: pass stable element IDs to the LLM and let it decide.
- Never pass raw CSS/XPath selectors to the LLM.
- When significant changes are made, update docs and Mermaid diagrams to match the current behavior.

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
- `capture_snapshot` only includes interactive elements — non-interactive elements need live DOM search via `page.evaluate()`.
- All HTML attributes are stored in `ElementSnapshot.attributes`, but only 9 are shown to the LLM in `format_snapshot_for_llm()`.
- Handler introspection (`src/agent/context/handlers.py`): extracts JS event handler source from inline handlers and framework internals (React/Vue/Angular). Handler hints appear as `[click:fn(); change:fn()]` in the LLM snapshot.

Browser Interaction Principles:
- DOM-first: use DOM methods (`.click()`, `.focus()`, `.innerText`) — they work through any visual layer (overlays, modals, z-index stacking).
- CDP coordinates only when required: only use `Input.dispatchMouseEvent` for actions needing screen positions (e.g., drag-and-drop, draw). Never gate click/type/read on visibility checks.
- Minimize CDP round-trips: combine operations into single `_call_on_node` calls.
- No `onTop` gates for DOM operations: don't check `elementFromPoint()` before DOM interactions.
