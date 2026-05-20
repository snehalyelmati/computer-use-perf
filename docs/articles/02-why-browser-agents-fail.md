# Why Browser Agents Fail

Browser agents fail because websites are not clean APIs. They are dynamic applications with hidden state, framework event handlers, overlays, iframes, transient DOM updates, and user-interface patterns that were not designed for LLM control.

This project became interesting when those failures stopped being one-off bugs and started becoming architecture requirements.

## The Agent Cannot See The Right Thing

The first failure mode is missing context.

The useful value might be in a hidden element, a `data-*` attribute, an ARIA label, a short text node, or a non-interactive paragraph. A snapshot that only includes visible buttons and inputs can miss the actual task instruction.

The mitigation in this project was to move toward richer CDP snapshots, raw text extraction, selected non-interactive signals, and filtered useful text lines.

## The Agent Clicks The Wrong Thing

Labels are not enough. Pages can contain multiple buttons named Submit, Continue, Open, Close, or Click Here. Some are ads. Some are unrelated controls. Some are decoys.

The current snapshot includes tree structure, frame grouping, attributes, and handler hints to help distinguish similar elements. The Orchestrator is also instructed to reference stable element IDs rather than relying only on labels.

## The Browser State Changes Underneath It

DOM nodes appear and disappear. Buttons become enabled. Text changes. Iframes load late. A target from the last snapshot can become stale by the time the tool executes.

The project handles this with snapshot diffs, progress fingerprints, mutation feedback, stale-node fallbacks, and fresh snapshots when the worker exits early.

## Visual Automation Is Brittle

Coordinate-first automation sounds natural because humans use a screen, but many web failures are caused by visual layers. An element can be visually obscured while still being the correct DOM target. A visibility check can prevent an action that would have worked.

This is why the project moved toward DOM-first tools for click, type, and read-style interactions. Coordinates still matter for inherently spatial actions like drawing and some hover behavior, but they should not gate ordinary DOM actions.

## The Model Repeats Itself

LLMs often retry the same action with slight variations. If there is no explicit memory, tool feedback, or stuck detection, a browser agent can burn many calls doing nothing.

Mitigations in this project include recent memory, step traces, tool-call limits, Oracle diagnosis, progress fingerprints, and done-gates.

## The Agent Declares Victory Too Early

Success text can be unrelated. A page can show a solved state from stale React state. A worker can complete its delegated sub-goal while the overall goal is still incomplete.

The current architecture distinguishes worker sub-goal completion from whole-run completion. It also gates worker done claims when no tool call succeeded.

## The Main Lesson

Browser-agent reliability is not one feature. It is a loop:

- Better context.
- Stable references.
- Safer tools.
- Action feedback.
- Memory and trace.
- Diagnostics.
- Metrics.

Each piece reduces a different class of failure.
