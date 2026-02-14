# Stage 1: Challenge Mode (Benchmark-First)

Stage 1 optimizes for the benchmark website flow: solve one challenge at a time, advance on URL change, and emit run statistics (time, tokens, token cost).

This stage intentionally keeps orchestration deterministic in code (no LLM orchestrator). The LLM focuses on understanding the current page and producing the next atomic browser actions.

## Definitions

- `GOAL` (fixed, immutable): The top-level instruction provided by the system/caller. The agent must not change this.
- `OBJECTIVE` (page-specific): A one-line restatement of what the current page requires to be considered solved. This can change each page and is derived from page text/state.
- `NEXT` (intent): Human-readable description of what should happen next. Used for logging and supervision.
- `TASK` (execution): A strict, index-based DSL that is translated into executable actions.

`TASK` DSL (one step per line):

- `click <index>`
- `type <index> <data_key>`
- `scroll page <pixels>` / `scroll <index> <pixels>`
- `wait <seconds>`
- `key <keyspec>`
- `watch <substring_or_data_key>`

In Challenge Mode:

- The agent always solves the *current* page's challenge.
- A URL change indicates the next challenge.
- Per-challenge memory resets on URL change.

## What Stage 1 Implements

- Challenge Mode run loop (URL change => challenge boundary)
- Fixed per-challenge `GOAL`
- Per-step `OBJECTIVE` extracted by the Overview LLM
- Per-step `TASK` DSL produced by the Overview LLM (translated by the Action LLM, then executed)
- Oracle supervision (does not change the fixed `GOAL`; provides guidance to the planner)
- Run statistics:
  - Total runtime + per-challenge runtime
  - Token usage by call type/model
  - Token cost (uses dummy pricing table; update later)

## Computation Policy

- The planner should not attempt complex computation/decoding.
- If a page explicitly asks for arithmetic and shows an expression, it should be recorded as `expr=<expression>` in `DATA`.
- The runner computes `answer=<result>` and makes it available for subsequent steps.

## Runtime Artifacts

All outputs go to `logs/<YYYY-MM-DD>/`:

- `agent_<HHMMSS>.log` / `agent_verbose_<HHMMSS>.log`
- `agent.log` / `agent_verbose.log` (symlinks to latest)
- `run_stats.json` (machine-readable)
- `run_stats.md` (human summary)

## Setup

1. Install dependencies:

   `uv sync`

2. Configure API keys (see `.envrc`):

   - `GROQ_API_KEY` for Groq
   - `CEREBRAS_API_KEY` for Cerebras

## Run

Default (Cerebras):

`uv run main.py --provider cerebras --url https://serene-frangipane-7fd25b.netlify.app`

Example (Groq):

`uv run main.py --provider groq --url https://serene-frangipane-7fd25b.netlify.app`

Optional limits:

- `--max-steps 500` (default)
- `--max-challenges 30`

## Pricing Table (Dummy)

Token cost is computed using per-model pricing specs in `src/agent/providers.py`.

- Values are placeholders for now.
- Pricing is per 1M tokens (input, output, cached input).
- Update `TokenPricingPer1M` values in `src/agent/providers.py` when real pricing is available.

## Next Stages (Planned)

- Stage 2: General Mode
  - Navigation is normal; URL change must NOT reset memory
  - Completion is explicit ("done" state) instead of implicit URL change
- Stage 3: Optional LLM Orchestrator
  - Run-level mission management (budgets, subtask routing) while retaining deterministic safety rails
