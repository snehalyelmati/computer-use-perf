# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A fast computer use agent — the goal is to use computers better than humans. Python 3.14, managed with [uv](https://docs.astral.sh/uv/).

## Commands

- `uv sync` — install/update dependencies
- `uv run main.py` — run the agent
- `uv add <package>` — add a dependency

## Dependencies

- **groq** — LLM inference via Groq API (key in `.envrc` as `GROQ_API_KEY`)

## Code Guidelines

- **DO NOT HARDCODE** values, selectors, keywords, or patterns specific to particular websites/challenges
- The agent must be **general-purpose** and work on any website without site-specific logic
- Let the LLM figure out what to click/type based on context, not hardcoded rules
- Keep element selection generic - pass all elements to LLM and let it decide
- If filtering is needed, base it on element type (input, button) not text content

## Architecture

- Entry point: `main.py`
- Core modules live in `src/agent/`
- Runtime artifacts live in `logs/`
