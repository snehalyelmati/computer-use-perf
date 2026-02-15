# AGENTS.md

Build/Test/Lint:
- Install deps: `uv sync`
- Run agent: `uv run main.py` (flags TBD)
- Run tests: `uv run pytest -q`

Architecture:
- Root entrypoint in `main.py`; core modules live under `src/agent/`.
- Uses PydanticAI for orchestration and structured outputs.
- Uses OpenRouter (OpenAI-compatible) for LLM access.
- Uses CDP for context extraction and Playwright for action execution.
- No database or server components.

Common Commands:
- `uv run main.py` - run with default settings
- `uv add <package>` - add a dependency

Dependencies:
- `pydantic-ai` - agent orchestration + structured output
- `openai` - OpenAI-compatible client for OpenRouter
- `playwright` - browser automation

Code Style:
- Keep agent general-purpose; do not hardcode site-specific selectors or logic.
- Prefer small, pure helper functions and explicit types in signatures.
- Use stdlib imports first, then third-party (pydantic_ai, openai, playwright).
- Avoid magic strings; centralize prompts/constants near top of file.
- Error handling should log context and keep the loop resilient.

Repository Rules:
- Follow `CLAUDE.md`:
  - DO NOT hardcode challenge/site-specific values, selectors, keywords, or patterns.
  - Keep element selection generic; pass elements to the LLM and let it decide.
  - Use stable element IDs from DOM snapshots; never pass raw selectors to the LLM.
