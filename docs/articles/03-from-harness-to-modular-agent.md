# From Harness To Modular Agent

The current architecture did not appear at the start. It came after the original harness became too complex to reason about.

The first harness combined page extraction, planning, action selection, execution, memory, stuck detection, and recovery in one loop. That was fine for fast iteration, but the responsibilities started to conflict.

## Why The Harness Became Hard To Maintain

The harness had to answer too many questions at once:

- What matters on the page?
- Which text or attributes should be shown to the model?
- Is the agent making progress?
- What should the next browser objective be?
- Which exact element should be used?
- Did the last action actually work?
- Is the model looping or fabricating values?

Adding more prompt rules helped temporarily, but it made the system harder to debug. A failure could come from extraction, planning, action translation, tool execution, or stale memory.

## The Rewrite

The modular rewrite split the system into explicit stages:

- Snapshot capture extracts page context.
- Filter prunes obvious filler while keeping plausible targets.
- Oracle diagnoses loops or wrong approaches.
- Orchestrator chooses the next small goal.
- Worker executes browser tools.
- Metrics record what happened.

This made the architecture slower per step than a single direct call, but it created control points. Each stage could be inspected, timed, and improved separately.

## Stable IDs Became The Interface

The most important interface change was replacing raw selectors and brittle indexes with stable `el_...` IDs.

The LLM does not need CSS selectors. It needs a stable reference to a meaningful element in the snapshot. The tool layer owns the mapping from stable ID to CDP/backend/frame information.

This reduces the chance that the model invents selectors or acts on stale index positions.

## Separation Of Concerns

The current default path uses Orchestrator and Worker separately. The Orchestrator decides what outcome should happen next. The Worker executes that outcome using tools.

The project also has `--unified`, which lets one tool-equipped agent plan and execute after Filter/Oracle preprocessing. This exists because the Orchestrator -> Worker handoff has its own cost: context can be lost when everything is compressed into a single worker goal.

## What The Rewrite Bought

The modular architecture made several things easier:

- Debugging over-pruning versus poor planning.
- Calling Oracle only when useful.
- Caching filter outputs by fingerprint.
- Measuring token and cost per agent role.
- Testing unified versus split planning/execution.
- Keeping powerful tools out of the default worker set.

## The Main Lesson

Multi-agent architecture should not be added because it sounds sophisticated. In this project, it was a response to concrete bottlenecks in the original harness. The value was not more agents; the value was explicit responsibility boundaries.
