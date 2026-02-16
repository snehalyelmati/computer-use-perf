# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A general-purpose browser agent. Python 3.14, managed with [uv](https://docs.astral.sh/uv/).

## Commands

- `uv sync` — install/update dependencies
- `uv run main.py --url <target> --goal "<task>"` — run the agent
- `uv add <package>` — add a dependency

## Observability

- Logs: `logs/agent.log`
- Metrics: `logs/metrics.jsonl` (disable with `--no-metrics`)
- Run summary: `logs/run_summary.json`

## Dependencies

- **pydantic-ai** — agent orchestration + structured outputs
- **openai** — OpenAI-compatible client for OpenRouter
- **playwright** — browser automation

## Code Guidelines

- **DO NOT HARDCODE** values, selectors, keywords, or patterns specific to particular websites/challenges
- The agent must be **general-purpose** and work on any website without site-specific logic
- Let the LLM decide what to click/type based on context, not hardcoded rules
- Keep element selection generic: pass stable element IDs to the LLM and let it decide
- Never pass raw CSS/XPath selectors to the LLM
- When significant changes are made, update docs and Mermaid diagrams to match the current behavior

## Browser Interaction Principles

- **DOM-first**: Use DOM methods (`.click()`, `.focus()`, `.innerText`) for element interactions — they work through any visual layer (overlays, modals, z-index stacking)
- **CDP coordinates only when required**: Only use `Input.dispatchMouseEvent` (coordinate-based) for actions that genuinely need screen positions (e.g., drag-and-drop). Never gate click/type/read on visibility checks.
- **Minimize CDP round-trips**: Combine operations into single `_call_on_node` calls instead of chaining multiple CDP commands
- **No `onTop` gates for DOM operations**: Don't check `document.elementFromPoint()` or `onTop` before DOM interactions — they bypass visual layering by design

## Architecture

- Entry point: `main.py`
- Core modules live in `src/agent/`
- Uses PydanticAI for orchestration and OpenRouter for LLM access
- Uses CDP for context extraction and Playwright for action execution
