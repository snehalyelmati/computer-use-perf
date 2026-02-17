# computer-use-perf

General-purpose browser agent (scaffold).

## Structure
- Entry point: `main.py`
- Core modules: `src/agent/`

```mermaid
flowchart LR
    subgraph LLM[LLM Infrastructure]
        OR[OpenRouter / Cerebras]
        FILT[Filter — Snapshot Pruner]
        ORAC[Oracle — Execution Auditor]
        ORCH[Orchestrator — Goal Planner]
        WORK[Worker — Browser Executor]
        OR --> FILT
        OR --> ORAC
        OR --> ORCH
        OR --> WORK
    end

    subgraph Browser[Execution Environment]
        HE[Handler Extraction]
        CDP[CDP Snapshot]
        PW[Playwright Actions]
        HE -->|"data-agent-hid stamps"| CDP
        CDP --> PW
    end

    HE -->|Handler Map| CDP
    CDP -->|Full Snapshot + handlers| FILT
    FILT -->|Pruned Snapshot + useful text| ORCH
    ORAC -->|Directive when off-track| ORCH
    ORCH -->|Delegated Goal| WORK
    FILT -->|Pruned Snapshot| WORK
    WORK -->|"Semantic Tool Calls (stable ids)"| PW
```

## Setup
- `uv sync`
- Set `OPENROUTER_API_KEY` for OpenRouter access

## Run
- `uv run main.py --url <target> --task TASK.md [--headless] [--max-elements 60] [--stuck-threshold 3] [--unchanged-abort-threshold 5] [--oracle-interval 5] [--max-tokens 2048] [--log-level INFO] [--no-metrics] [--no-handlers]`

`TASK.md` should contain the full task instructions as plain markdown text.

### Outputs
- Logs: `logs/agent.log`
- Metrics (JSONL): `logs/metrics.jsonl` (timings, token usage, and OpenRouter cost when available)
- Run summary: `logs/run_summary.json`
- Details: `docs/observability.md`
- Analyze timings: `uv run python scripts/analyze_metrics.py logs/metrics.jsonl`

## Architecture

This project is a general-purpose browser agent built around a clear separation of concerns:

- **PydanticAI** handles orchestration, memory, and structured outputs.
- **OpenRouter** provides a single OpenAI-compatible gateway to multiple model providers.
- **CDP** captures rich DOM context for the LLM.
- **Playwright** executes actions reliably in the browser.

### Agent Framework (PydanticAI)

- Agents are defined with `Agent(model, tools=...)` and return structured outputs.
- Pydantic models describe the agent outputs and tool payloads.
- Multi-agent orchestration is handled manually via code-driven delegation.

### LLM Infrastructure (OpenRouter)

- OpenRouter is used as an OpenAI-compatible endpoint.
- Models (Groq, Cerebras, OpenAI, etc.) are selected via model names.
- Structured outputs are enforced via JSON schema when supported.
- Default model: `moonshotai/kimi-k2-0905:exacto` (see `src/agent/config.py`).

### Execution Environment

- **CDP (Chrome DevTools Protocol)** is used for context extraction:
  - DOM, accessibility, layout, and element metadata for LLM context.
  - Low-latency, high-fidelity snapshots.
- **Playwright** is used for action execution:
  - Robust actions with built-in waiting and retries.
  - Browser lifecycle and session management.
- **CDP + Playwright via CDPSession** keeps context extraction and actions aligned.

```mermaid
sequenceDiagram
    participant HE as Handler Extraction
    participant CDP as CDP Snapshotter
    participant Filter as Filter (Pruner)
    participant Oracle as Oracle (Auditor)
    participant Orchestrator as Orchestrator
    participant Worker as Worker
    participant PW as Playwright Executor

    CDP->>CDP: wait_for_load_state(networkidle)
    HE->>HE: page.evaluate (stamp data-agent-hid)
    HE->>CDP: handler map
    CDP->>CDP: capture snapshot (correlate handlers)
    HE->>HE: cleanup data-agent-hid
    CDP->>Filter: Full snapshot + handlers + raw text + diff
    Filter-->>Orchestrator: Pruned snapshot + useful text
    Oracle-->>Orchestrator: Directive (when off-track)
    Orchestrator->>Worker: Goal + pruned snapshot
    Worker->>PW: Semantic tool calls (stable ids)
    PW-->>Worker: Tool results + DOM change feedback
    Worker-->>Orchestrator: Step summary + done?
    Worker-->>Oracle: Step trace entry
```

## Tooling Principles

### Semantic Tools (Preferred)

The agent uses semantic tools that reference stable element IDs:

- `click_element(element_id: str)`
- `type_text(element_id: str, text: str)`
- `drag_and_drop(source_id: str, target_id: str)`
- `scroll(delta_x: int, delta_y: int, element_id: str | None = None)`
- `wait(milliseconds: int)` (capped at 10s)
- `switch_to_iframe(iframe_id: str)`, `switch_to_main_frame()`
- `press_key_combination(keys: list[str])`

### Available but not in default worker set

- `find_elements(query: str, limit: int = 8)` — search for elements by text, label, or role
- `inspect_element(element_id: str)` — returns text content + all HTML attributes
- `search_page_attributes(query: str)` — searches all DOM elements for matching attributes
- `take_screenshot()`, `execute_js(code: str)`

### Reference-Based, Not Selectors

- The LLM never sees raw CSS/XPath selectors.
- Each snapshot produces a mapping: `stable_id -> backend node id + frame metadata` (internal only).
- Tool calls only accept stable element IDs.

### Optional Escape Hatches

- `execute_js(code: str)`
- `press_key_combination(keys: list[str])`

## Recommended Agent Loop

1. **Wait for page settlement** (`domcontentloaded` + `networkidle`) to handle SPA transitions.
2. **Extract JS handlers** via `page.evaluate()` — stamps elements with `data-agent-hid`, returns handler map. Disabled with `--no-handlers`.
3. **Extract context via CDP** into a structured snapshot with full element tree. Handler map is correlated via `data-agent-hid`, then marker attributes are cleaned up.
4. **Oracle health check** (periodic every N steps + when stuck): reviews the execution trace and issues directives. Invalidates filter cache when intervention is needed.
5. **Filter (tree pruner)**: receives full snapshot + diff + Oracle advice. Conservatively removes only obvious filler elements; keeps everything plausibly useful. Cached when the page fingerprint is unchanged.
6. **Build pruned snapshot**: only filter-kept elements survive. Orchestrator and worker never see pruned elements.
7. **Orchestrator**: plans the next sub-goal using stable element IDs from the pruned snapshot. Follows Oracle directives when present.
8. **Worker**: executes the goal using semantic tools against the pruned snapshot. Receives only the goal + snapshot (no memory, no progress info).
9. **Update step trace + memory + stop criteria** (`done`, `max_steps`, or unchanged fingerprint abort).
10. **Repeat** until the overall goal is complete.

## Guardrails

- Avoid hardcoding site-specific selectors or strings.
- Pass stable element IDs, never raw selectors, to the LLM.
- Keep tools generic and reusable across websites.
- The Oracle fires periodically (default: every 5 steps) and when stuck (default: 3 unchanged steps). It reviews the full execution trace and issues directives that the orchestrator must follow.
- When the Oracle fires with `all_clear=false`, the filter cache is invalidated to force re-evaluation with Oracle context.
- If the page fingerprint is unchanged for multiple steps (default: 5), the agent aborts early with a clear stop reason.
- LLM output is capped at `max_tokens` (default: 2048) with `frequency_penalty` (0.3) to prevent degenerate repetition.

## Roadmap

### Phase 1: Scaffolding (Done)

- Create modular package layout under `src/agent/`.
- Replace legacy docs with updated architecture guidance.
- Define basic config and entrypoint.

### Phase 2: Context & Tooling (Done)

- Implement CDP snapshot capture for DOM + accessibility.
- Create hashed stable element-id mapping per snapshot (CDP node IDs stay internal).
- Wire semantic tools to Playwright/CDP actions with overlay-aware input.
- Support iframe switching and navigation APIs with frame-aware CDP sessions.

### Phase 3: Agent Loop (Done)

- Add an orchestrator agent that delegates to worker agents.
- Implement a browser worker agent with tool-calling support.
- Build an orchestration loop with memory and stop criteria.

### Phase 4: Quality & Reliability

- Add tests for snapshot extraction, id mapping, and tools.
- Add logging and tracing for agent decisions.
- Create replay fixtures for debugging.
