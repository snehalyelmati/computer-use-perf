# DOM-First Browser Automation

One of the clearest lessons from this project is that browser agents should not treat the screenshot as the only source of truth.

Humans interact visually, but automation can often be more reliable when it works directly with DOM nodes.

## The Problem With Coordinate-First Actions

Coordinate-first actions are fragile:

- An overlay can intercept a click.
- A modal can visually obscure the target.
- z-index stacking can confuse visibility checks.
- HiDPI displays can create coordinate-space mismatches.
- Layout shifts can move targets between snapshot and action.

For some tasks, coordinates are necessary. Drawing on a canvas is spatial. Hovering may need pointer movement to trigger CSS `:hover`. But ordinary click and type actions often work better through DOM methods.

## What DOM-First Means Here

In this project, DOM-first means the semantic tool resolves a stable element ID to a DOM/backend node and performs the action through the node when possible.

Examples:

- `click_element` scrolls the node into view and calls `.click()`.
- `type_text` focuses the node and inserts text.
- `drag_and_drop` uses DOM drag events with framework-friendly timing.
- `draw` tries DOM synthetic mouse events before falling back where needed.

The model does not see raw selectors. It sees stable element IDs from the snapshot.

## Why This Helped

DOM-first actions reduced false failures where the browser automation layer thought an element was not actionable even though activating the DOM node was enough.

It also made the tool API more semantic. The model asks to click `el_...`; the tool decides how to execute that reliably.

## Feedback Matters

DOM-first execution is not enough by itself. The model needs to know whether the action worked.

Most mutating tools inject a MutationObserver before acting and collect feedback after a short settle period. The feedback tells the model whether text was added, attributes changed, elements disappeared, or navigation occurred.

This turns tools into sensors as well as actuators.

## When Coordinates Still Matter

Coordinates are still useful for:

- Canvas/SVG drawing.
- Pointer movement that triggers CSS hover state.
- Drag/drop implementations that ignore synthetic DOM events.

The point is not to ban CDP coordinates. The point is to avoid using visual hit-testing as a gate for every interaction.

## The Main Lesson

For browser agents, the best abstraction is not "move the mouse like a human." It is "perform the semantic browser action and return evidence of what changed." DOM-first tools support that abstraction better than raw coordinate automation for many tasks.
