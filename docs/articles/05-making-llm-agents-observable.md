# Making LLM Agents Observable

LLM agents are difficult to debug if all you have is the final failure. Browser agents are worse because the page, model output, tool calls, and DOM state all change over time.

This project treats observability as part of the agent architecture.

## What Needs To Be Observed

For each run, the useful questions are:

- What page state did the agent see?
- What did each agent role receive and return?
- Which tool calls were made?
- Did the DOM actually change after an action?
- Was the agent stuck, interrupted, done, or timed out?
- How many tokens and dollars did each stage cost?
- Which model and provider were used?

Without those answers, it is hard to know whether to fix prompts, tools, extraction, or architecture.

## Run Artifacts

Each run writes to `logs/<run_id>/`, and `logs/latest` points to the newest run.

The main artifacts are:

- `agent.log` for readable progress.
- `agent_debug.log` for prompts, outputs, diffs, memory, and traces.
- `metrics.jsonl` for structured events.
- `run_summary.json` for final duration, stop reason, tokens, cost, provider, and models.
- Optional saved page HTML when `--save-pages` is enabled.

## Metrics As A Debugging Tool

The metrics stream records snapshots, CDP calls, agent calls, tool calls, step ends, and run ends.

This makes it possible to answer questions like:

- Did snapshot capture get slower?
- Did the Filter consume most tokens?
- Did the Worker hit the tool-call limit?
- Did retries dominate wall-clock time?
- Did a provider stop returning cost metadata?

## Page Capture And Replay

When `--save-pages` is enabled, the runtime saves page HTML for unique fingerprints. This gives an offline artifact for investigating what the agent saw at a step.

It is not a perfect replay of dynamic browser state, but it is much better than relying on memory or screenshots alone.

## Result History

`scripts/generate_results.py` creates `results.md` from run logs and persists history so old runs survive log pruning.

That table is not just presentation. It helps track regressions across commits, providers, models, costs, and stop reasons.

## The Main Lesson

Agent observability should be built early. Otherwise every failure turns into guesswork.

For this project, logs and metrics became the difference between "the model failed" and "the worker repeated the same click until the tool-call limit, Oracle did not trigger soon enough, and the newly enabled button was pruned out."
