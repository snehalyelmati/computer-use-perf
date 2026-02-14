# AGENTS.md

Build/Test/Lint:
- Install deps: `uv sync`
- Run agent: `uv run main.py` (see common flags below)
- Run tests: `uv run pytest -q`

Architecture:
- Root entrypoint in `main.py`; core modules under `src/agent/`.
- Uses Playwright for browser automation and LLM providers (Groq, Cerebras) for decisions.
- No database or server components.
- Runtime artifacts live in `logs/<YYYY-MM-DD>/` with per-run files; `logs/agent*.log` are symlinks to latest.

Common Commands:
- `uv run main.py` - run with default settings
- `uv run main.py --url <url>` - run against a specific URL
- `uv run main.py --model <name>` - run with a specific model
- `uv run main.py --provider <name>` - LLM provider (`groq`, `cerebras`)
- `uv run main.py --reasoning <level>` - reasoning effort (`none`, `low`, `medium`, `high`) for supported models
- `uv run main.py --action-model <name>` - override the action model
- `uv add <package>` - add a dependency

Dependencies:
- `groq` - Groq API (`GROQ_API_KEY` in `.envrc`)
- `cerebras-cloud-sdk` - Cerebras API (`CEREBRAS_API_KEY` in `.envrc`)

Code Style:
- Keep agent general-purpose; do not hardcode site-specific selectors or logic.
- Prefer small, pure helper functions and explicit types in signatures.
- Use stdlib imports first, then third-party (bs4, groq, playwright).
- Avoid magic strings; centralize prompts/constants near top of file.
- Error handling should log context via `log()` and keep loop resilient.

Repository Rules:
- Follow `CLAUDE.md`:
  - DO NOT hardcode challenge/site-specific values, selectors, keywords, or patterns.
  - Keep element selection generic; pass elements to the LLM and let it decide.
  - If filtering is needed, base it on element type/state, not text content.
