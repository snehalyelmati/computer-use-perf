# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A general-purpose browser agent. Python 3.14, managed with [uv](https://docs.astral.sh/uv/).

## Commands

- `uv sync` — install/update dependencies
- `uv run main.py --url <target> --goal "<task>"` — run the agent
- `uv add <package>` — add a dependency

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

## Architecture

- Entry point: `main.py`
- Core modules live in `src/agent/`
- Uses PydanticAI for orchestration and OpenRouter for LLM access
- Uses CDP for context extraction and Playwright for action execution
