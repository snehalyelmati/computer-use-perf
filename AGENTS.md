# AGENTS.md

Build/Test/Lint:
- Install deps: `uv sync`
- Run agent: `uv run main.py [base_url]`
- Single test: none found (no test runner in repo)

Architecture:
- Root entrypoint in `main.py`; core modules under `src/agent/`.
- Uses Playwright for browser automation and Groq LLM for decisions.
- No database or server components; logs to `logs/agent.log`.

Code Style:
- Keep agent general-purpose; do not hardcode site-specific selectors or logic.
- Prefer small, pure helper functions and explicit types in signatures.
- Use stdlib imports first, then third-party (bs4, groq, playwright).
- Avoid magic strings; centralize prompts/constants near top of file.
- Error handling should log context via `log()` and keep loop resilient.

Repository Rules:
- Follow `CLAUDE.md` (general-purpose agent, no site-specific logic; generic element selection).
