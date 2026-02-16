# AGENTS.md

Build/Test/Lint:
- Install deps: `uv sync`
- Run agent: `uv run main.py --url <target> --goal "<task>"`
- Run tests: `uv run pytest -q`

Common Commands:
- `uv run main.py --url <target> --goal "<task>"` - run the agent
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
- Uses OpenRouter (OpenAI-compatible) for LLM access.
- Uses CDP for context extraction and Playwright for action execution.
- No database or server components.

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

Browser Interaction Principles:
- DOM-first: use DOM methods (`.click()`, `.focus()`, `.innerText`) — they work through any visual layer (overlays, modals, z-index stacking).
- CDP coordinates only when required: only use `Input.dispatchMouseEvent` for actions needing screen positions (e.g., drag-and-drop). Never gate click/type/read on visibility checks.
- Minimize CDP round-trips: combine operations into single `_call_on_node` calls.
- No `onTop` gates for DOM operations: don't check `elementFromPoint()` before DOM interactions.
