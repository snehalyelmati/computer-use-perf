# Benchmark Lessons And Results

This project was developed against an external browser-agent challenge site. That site may no longer be reliably available, so the benchmark results are now best understood as archived development history.

That is still useful. The benchmark acted as a failure-mode generator.

## What The Benchmark Tested

The challenge site exposed many browser-agent edge cases:

- Hidden values.
- Visible codes.
- Delayed reveals.
- Hover tasks.
- Drag/drop tasks.
- Drawing tasks.
- Keyboard sequences.
- Shadow DOM.
- Iframes and recursive iframes.
- Service worker and WebSocket behavior.

Each category forced a different part of the agent to improve.

## What The Results Table Shows

`results.md` is generated from run logs. It records run ID, commit, provider, model, per-role model overrides, challenge progress, duration, tokens, cost, and stop reason.

The best way to read it is as a timeline:

- Early runs show the cost of missing context and brittle actions.
- Middle runs show gains from stable IDs, DOM-first tools, MutationObserver feedback, and modular architecture.
- Later runs show cost, latency, provider routing, and token-compaction work.

It should not be read as a clean model leaderboard. The benchmark was external, the site may be unavailable, and some runs were interrupted.

## General Lessons

The reusable lessons are:

- Stable element IDs are safer than indexes or selectors.
- DOM-first tools are more reliable for many web interactions.
- Mutation feedback helps the model react to dynamic pages.
- Conservative pruning is safer than aggressive pruning.
- Oracle-style diagnosis can catch loops and wrong goals.
- Per-run metrics are necessary to understand token and cost behavior.

## Benchmark-Specific Discoveries

Some discoveries were specific to the challenge site:

- A React state leak affected back-to-back math puzzle steps.
- A recursive iframe challenge had off-by-one behavior.
- The final-step code reveal path needed a benchmark-specific finish navigation workaround.

These are good debugging stories, but they should be presented separately from the general agent design.

## What I Would Add Next

The most useful follow-up is a local mini-benchmark checked into the repository. It should not try to recreate the full external benchmark. It should cover representative hard cases:

- Hidden DOM value.
- Dynamic reveal.
- Disabled-to-enabled button.
- Iframe interaction.
- Hover reveal.
- Newly inserted interactive element.

That would make the project easier to evaluate even if the original benchmark never comes back.
