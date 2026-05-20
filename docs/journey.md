# Project Journey

This project did not start as the current modular browser agent. It started as a small custom harness and was repeatedly rewritten around concrete failures discovered while running browser tasks.

## Phase 1: A Simple Harness

The first version was a general-purpose browser automation agent built from a few direct pieces:

- BeautifulSoup for structured HTML parsing.
- An Overview agent to understand the page and task.
- An Action LLM to choose browser actions.
- Async Playwright for browser control.
- Basic stuck detection to avoid wasting API calls.

That harness was useful because it made iteration fast. The early goal was not clean architecture; it was to expose where LLM browser automation actually failed.

## Phase 2: Hardening The Harness

The first failures were mostly about context and state.

Early changes added:

- State-hash stuck detection.
- Richer element metadata such as role, state, disabled status, values, names, and data attributes.
- Hidden content and `data-*` extraction.
- Challenge-level memory and summaries.
- Structured GOAL/DATA/PROGRESS/NEXT outputs.
- Element diffs and text diffs between steps.
- Better logging with timing, state changes, and full LLM outputs.

The harness also grew more browser actions: hover, drag/drop, keyboard shortcuts, wait, canvas drawing, and a MutationObserver-based watch action for transient content.

This phase produced an important lesson: browser agents need more than click and type. Many web tasks depend on dynamic state, hover behavior, drag events, canvas strokes, hidden data, or short-lived DOM updates.

## Phase 3: Harness Limits

As the harness improved, its complexity became the next problem.

The same loop was responsible for too many concerns:

- Extracting enough context without overwhelming the model.
- Deciding the next goal.
- Translating that goal into browser actions.
- Recovering from loops and wrong goals.
- Preventing value fabrication.
- Tracking progress, costs, and failures.

Prompt rules accumulated quickly. Action batching helped in some cases but made verification and failure recovery harder. Element indexes and text labels were not reliable enough as long-term references. This pushed the project toward a rewrite.

## Phase 4: Modular Rewrite

The modular branch reorganized the project around explicit responsibilities.

The current code lives under `src/agent/`, with `main.py` as the CLI entrypoint. The main runtime is `BrowserAgent.run()` in `src/agent/core/agent.py`.

Key changes in the rewrite:

- Stable `el_...` element IDs derived from snapshot/backend information.
- CDP-based snapshots instead of only HTML parsing.
- Semantic tools that receive element IDs rather than selectors.
- A Worker agent for browser execution.
- An Orchestrator agent for setting small worker goals.
- A Filter agent for conservative snapshot pruning.
- An Oracle agent for periodic and stuck-state diagnosis.
- Structured metrics and per-run logs.

The modular architecture was not added for aesthetics. It was added because the harness had revealed separate problems that needed separate control points.

## Phase 5: Reliability Engineering

After the modular rewrite, much of the work moved into reliability details.

The project added:

- DOM-first click/type/read behavior to avoid false failures caused by overlays.
- MutationObserver feedback after actions.
- Diff-style tool feedback with added, changed, and removed DOM content.
- Handler hints from inline and framework event handlers.
- Iframe-aware tool context and frame switching.
- Scroll-container detection and element-targeted scrolling.
- Done-gates and worker tool-call limits.
- Resilient LLM retries and step-level timeouts.
- Per-role model configuration for worker, filter, and Oracle.
- Token/cost accounting and provider-specific pricing fallbacks.
- Tool-return history compaction to reduce triangular token growth.

The main lesson from this phase is that agent reliability is not only about prompts. It is also about giving the model stable references, bounded tools, accurate feedback, and enough run artifacts to debug failure after the fact.

## Phase 6: Benchmark Investigation

During development, the agent was tested against an external browser-agent challenge site. That site appears to no longer be reliably available, so the results are now archival.

The benchmark was still useful because it forced a wide range of browser failures:

- Hidden DOM and visible-code tasks.
- Hover and drag/drop interactions.
- Shadow DOM and iframe challenges.
- Dynamic reveal patterns.
- Service worker and WebSocket behavior.
- Disabled-to-enabled transitions.
- Benchmark-specific React state bugs.

Some fixes in the code are benchmark-specific, such as stale puzzle state recovery, recursive iframe challenge recovery, and final-step finish navigation. These should be documented as benchmark discoveries, not as general-purpose browser-agent design.

## What Worked

- Stable element IDs made tool calls safer than raw labels or selectors.
- CDP snapshots gave richer page structure than plain HTML parsing.
- DOM-first tools avoided many overlay and visibility failures.
- Mutation feedback let the model react to page changes immediately.
- Conservative pruning was safer than aggressive pruning.
- Oracle intervention helped identify loops and wrong approaches.
- Per-run metrics made token, cost, and latency regressions visible.

## What Did Not Work Well

- Raw full-page context quickly became noisy and expensive.
- Element indexes were too brittle as durable references.
- Strict grounding could drop valid actions when useful values were present but not formatted exactly as expected.
- Coordinate-first actions were fragile except for genuinely spatial tasks.
- Repeating failed actions without fresh state wasted many steps.
- Unbounded tool-return history caused large token growth inside multi-tool steps.

## Current State

The current project is an experimental browser-agent runtime, not a polished product package. It can run against arbitrary URLs and tasks, records detailed run artifacts, and includes archived benchmark results from development. The next presentation step is to separate the reusable agent design from benchmark-specific recovery code and add a local demo target so the repo remains reproducible without the original benchmark site.
