# MiniWoB Full Suite Failure Analysis - 2026-05-26

This note summarizes the remaining zero-reward failures from the `2026-05-26_15-41-13` one-repeat MiniWoB full-suite run.

## Summary

- Score improved from `58.4%` on `2026-05-21` to `86.4%`.
- Zero-reward failures dropped from `52` to `17`.
- Truncated episodes dropped from `20` to `4`.
- Major improvements landed in email inbox controls, drag/drop, copy/paste, terminal readback, social media controls, focus tasks, sliders, color wheels, and rich text editing.

## Remaining Failure Families

| Family | Tasks | What failed |
| --- | --- | --- |
| SVG / coordinate geometry | `circle-center`, `find-midpoint` | The agent identified plausible SVG coordinates, but coordinate-frame conversion or exact target placement produced zero reward. |
| Menus and stateful widgets | `click-menu`, `click-pie`, `daily-calendar` | Multi-level menus and dynamically revealed controls still caused missed selections, repeated clicks, or env-step truncation. |
| Drag, resize, and canvas | `drag-cube`, `drag-shapes`, `drag-shapes-2`, `resize-textarea` | Spatial pointer operations were attempted but did not reliably satisfy MiniWoB's target geometry or resize validation. |
| Visual reasoning gaps | `count-sides`, `number-checkboxes`, `text-transform`, `tic-tac-toe` | Some tasks required interpreting visual layouts or game state that was not represented well enough in the worker context. |
| Validation/action mismatch | `enter-date`, `search-engine` | The runtime believed the action path was complete, but BrowserGym returned zero reward after submission/navigation. |
| Stuck or truncated loops | `email-inbox-nl-turk`, `hot-cold` | The internal runtime stopped for no-progress/tool-limit reasons while BrowserGym continued until env-step truncation. |

## Failed Episodes

| Task | Seed | Steps | Truncated | Notes |
| --- | ---: | ---: | --- | --- |
| `miniwob.circle-center` | 2 | 1 | false | Repeated center clicks and submit attempts hit the worker tool-call limit; same coordinate-frame issue seen in earlier targeted run. |
| `miniwob.click-menu` | 2 | 1 | false | Navigated the intended hierarchy `Amandi > Olga > Ashlee`, but BrowserGym did not award success. |
| `miniwob.click-pie` | 8 | 2 | false | Expanded the pie menu but failed to locate/click item `9` reliably after dynamic reveal. |
| `miniwob.count-sides` | 3 | 1 | false | Tool calls exhausted while trying to inspect/read the shape answer. |
| `miniwob.daily-calendar` | 28 | 10 | true | Internal runtime hit a tool-limit stop and BrowserGym truncated after ten env steps. |
| `miniwob.drag-cube` | 33 | 10 | true | Internal runtime hit a tool-limit stop on a spatial drag task and BrowserGym truncated. |
| `miniwob.drag-shapes` | 30 | 1 | false | Dragged the wrong shape set or positions, then submitted for negative reward. |
| `miniwob.drag-shapes-2` | 14 | 3 | false | Multiple drag and pointer-drag attempts did not place shapes correctly before submit. |
| `miniwob.email-inbox-nl-turk` | 14 | 10 | true | Inbox navigation/reply flow stalled and BrowserGym truncated. |
| `miniwob.enter-date` | 8 | 1 | false | Entered `07/07/2017` and submitted, but MiniWoB returned negative reward. |
| `miniwob.find-midpoint` | 7 | 1 | false | Computed midpoint `(86.5, 61)` and submitted, but click landed outside the accepted target. |
| `miniwob.hot-cold` | 21 | 10 | true | Internal runtime stopped for no progress and BrowserGym truncated. |
| `miniwob.number-checkboxes` | 25 | 1 | false | Began drawing the target number with checkboxes but did not complete the full pattern. |
| `miniwob.resize-textarea` | 25 | 1 | false | Multiple pointer-drag/resize attempts exhausted the tool-call limit without satisfying validation. |
| `miniwob.search-engine` | 3 | 1 | false | Agent believed it clicked the eighth result, but BrowserGym returned zero reward. |
| `miniwob.text-transform` | 1 | 4 | false | Eventually typed `w P s`, but the transformed text expected by the task was not captured or entered correctly. |
| `miniwob.tic-tac-toe` | 5 | 1 | false | Played several moves and lost or drew, receiving negative reward. |

## Priority Backlog

1. Make coordinate tools explicit about element-local, SVG-local, and viewport coordinate frames.
2. Improve visual summaries for SVG/canvas/game-state tasks before asking the model to act.
3. Add stronger no-progress recovery for menu/stateful-widget tasks before BrowserGym env truncation.
4. Preserve more structured context for dynamic controls that reveal options after the first click.
5. Audit validation mismatch cases where tool feedback suggests success but BrowserGym returns zero reward.
