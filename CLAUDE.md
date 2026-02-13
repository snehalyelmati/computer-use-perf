# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A fast computer use agent — the goal is to use computers better than humans. Python 3.14, managed with [uv](https://docs.astral.sh/uv/).

## Commands

- `uv sync` — install/update dependencies
- `uv run main.py` — run the agent with default settings
- `uv run main.py --model <name>` — run with specific model (e.g., `moonshotai/kimi-k2-instruct-0905`, `qwen/qwen3-32b`)
- `uv run main.py --provider <name>` — LLM provider (`groq`, `cerebras`)
- `uv run main.py --reasoning <level>` — set reasoning effort (`none`, `low`, `medium`, `high`) for models that support it
- `uv run main.py --action-model <name>` — override the action model
- `uv run main.py --url <url>` — run against a specific URL
- `uv add <package>` — add a dependency

## Dependencies

- **groq** — LLM inference via Groq API (key in `.envrc` as `GROQ_API_KEY`)
- **cerebras-cloud-sdk** — LLM inference via Cerebras API (key in `.envrc` as `CEREBRAS_API_KEY`)

## Code Guidelines

- **DO NOT HARDCODE** values, selectors, keywords, or patterns specific to particular websites/challenges
- The agent must be **general-purpose** and work on any website without site-specific logic
- Let the LLM figure out what to click/type based on context, not hardcoded rules
- Keep element selection generic - pass all elements to LLM and let it decide
- If filtering is needed, base it on element type (input, button) not text content

## Architecture

- Entry point: `main.py`
- Core modules live in `src/agent/`
  - `src/agent/providers.py` — provider-to-model mapping (`PROVIDER_MODELS`). Adding a new provider means adding an entry here.
- Runtime artifacts live in `logs/<YYYY-MM-DD>/` with timestamped files per run
  - `agent_HHMMSS.log` / `agent_verbose_HHMMSS.log` — per-run logs
  - `agent.log` / `agent_verbose.log` — symlinks to latest run
