# Building A Browser Agent From Scratch

The first version of this project was intentionally simple: parse the page, ask an LLM what is happening, ask another LLM what to do, and execute the action with Playwright.

That version was not elegant, but it was useful. It exposed the real problems quickly.

## The First Harness

The initial harness had a few pieces:

- BeautifulSoup for structured HTML parsing.
- An Overview agent that summarized the page and task.
- An Action LLM that converted the summary into browser actions.
- Async Playwright for browser execution.
- Basic stuck detection to stop wasting API calls.

The split between Overview and Action was useful because it separated page understanding from action execution. The Overview model could focus on the broader task, while the Action model could focus on concrete operations.

## What Worked Early

The simple harness was enough to prove that an LLM could operate a browser when given structured page context and a small action language.

It also made iteration fast. Each failure could become a prompt change, extraction change, or new action handler. The harness quickly gained better logging, memory, state hashes, action verification, and richer element metadata.

## The First Problems

The early failures were not exotic. They were basic browser-agent problems:

- The agent could not see hidden values or data attributes.
- Element indexes changed after page updates.
- The model repeated actions that had already failed.
- The model guessed values that were not actually on the page.
- Clicks and drags failed when overlays or custom event handlers were involved.
- Some tasks needed hover, wait, drag, draw, or keyboard actions, not only click and type.

These problems pushed the harness beyond a toy implementation.

## Hardening The Harness

The project added progressively more structure:

- Challenge-level memory.
- Rich element annotations.
- Hidden content and `data-*` extraction.
- Text and element diffs.
- Action batching.
- Post-action verification.
- Stuck recovery and repetition detection.
- GOAL/TASK separation.
- Anti-fabrication rules.

This stage was valuable because it created a catalog of failure modes. The later modular architecture came from these failures, not from an abstract desire to use multiple agents.

## The Main Lesson

The simple version was the right first version. It gave fast feedback and forced the project to discover what mattered.

But a browser agent stops being simple as soon as it needs to be reliable. Context extraction, action execution, state tracking, verification, and recovery are separate problems. Treating them as one prompt eventually became the bottleneck.
