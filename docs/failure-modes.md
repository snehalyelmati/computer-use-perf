# Browser-Agent Failure Modes

This document summarizes failure modes observed while building the agent and how the current code attempts to mitigate them.

## Missing Context

Symptoms:

- The agent cannot see a code, value, label, or instruction that exists in the page.
- The model clicks through visible UI even though the answer is already present in hidden text or attributes.
- The model has no awareness of non-interactive content that explains what to do.

Mitigations:

- CDP snapshot capture instead of only HTML parsing.
- Raw text extraction and selected non-interactive structural signals.
- Generic `data-*` and ARIA attribute handling.
- Filtered useful text lines passed to Orchestrator and Worker.
- Descendant-text previews for unlabeled clickable containers.

Tradeoff:

- More context improves recall but increases tokens and noise. The Filter and text compression try to preserve high-signal content without flooding every prompt.

## Unstable Element References

Symptoms:

- The model chooses an element by index, but the index changes after a page update.
- A stale node reference breaks a click after the DOM mutates.
- Similar labels make it unclear which element was intended.

Mitigations:

- Stable `el_...` IDs in snapshots.
- Semantic tools take element IDs, not CSS or XPath selectors.
- Stale-node text fallback in `click_element`.
- Tree-structured snapshot formatting and handler hints for disambiguation.

Tradeoff:

- Stable IDs still depend on snapshot/backend information. When the DOM changes, a fresh snapshot may be required.

## Overlay And Visibility Problems

Symptoms:

- Coordinate-based clicks fail because an overlay or modal is visually on top.
- Visibility checks report that an element is blocked even though DOM activation would work.
- z-index and layout layers create false negatives.

Mitigations:

- DOM-first click, type, drag, and draw behavior where possible.
- Tools scroll elements into view automatically.
- CDP coordinates are reserved for interactions that need spatial input, such as hover CSS state and drawing paths.

Tradeoff:

- Some custom widgets only respond to trusted pointer events or coordinate sequences. The tool layer uses fallbacks where practical.

## Dynamic DOM And Missing Feedback

Symptoms:

- A click reveals a new button, but the model cannot target it until the next snapshot.
- A value appears temporarily and disappears before the next step.
- The model repeats actions because it cannot tell whether the page changed.

Mitigations:

- MutationObserver feedback after mutating tools.
- Diff-style feedback with added, changed, and removed content.
- `watch_for_text` for transient DOM content.
- Resolution of newly added interactive elements into stable IDs when possible.

Tradeoff:

- Mutation feedback can be noisy, so the code compacts log output and limits captured text.

## Repetition And Stuck Loops

Symptoms:

- The worker clicks the same element repeatedly.
- The agent keeps scrolling even when the scroll position does not change.
- Multiple steps hit the tool-call limit without progress.

Mitigations:

- Progress fingerprints and unchanged abort threshold.
- Worker tool-call limits.
- Done-gates that override unsupported success claims.
- Step trace with tool-call history.
- Oracle triggers for periodic health checks, stuck states, and repeated tool-limit loops.
- Recent step summaries passed to the Worker.

Tradeoff:

- Some legitimate tasks need repeated actions. The Oracle and memory are meant to distinguish progress from loops, but this is still imperfect.

## Premature Success

Symptoms:

- The model declares the task done because it sees success-like text unrelated to the actual goal.
- The Worker marks a sub-goal complete even when no tool call succeeded.

Mitigations:

- Structured outputs distinguish worker sub-goal completion from whole-run completion.
- Worker done-gate requires successful tool execution for `StepOutput.done`.
- Unified done-gate only allows no-tool success when no failed tools were attempted.
- Orchestrator controls whole-run completion in the default path.

Tradeoff:

- The Orchestrator can still declare overall completion based on context. More deterministic validation remains a future improvement.

## Iframes And Frame State

Symptoms:

- The model targets an element in a different frame from the active tool context.
- Nested iframe metadata is decoded incorrectly.
- Mutation feedback is collected from the wrong frame.

Mitigations:

- Snapshot frame metadata and active-frame grouping.
- `switch_to_iframe` and `switch_to_main_frame` tools.
- Frame-aware CDP session selection for tools and mutation feedback.
- Tests and debug scripts for iframe feedback.

Tradeoff:

- Cross-frame interaction remains more complex than single-frame DOM interaction, especially when iframes are dynamic.

## Disabled-To-Enabled Blindness

Symptoms:

- A button becomes enabled after typing or waiting, but the agent does not notice.
- Attribute display makes disabled/enabled transitions ambiguous.

Mitigations:

- Disabled state included in snapshot diff keys.
- Newly enabled elements are automatically included in the pruned snapshot.
- Boolean attributes are formatted semantically in mutation feedback.

Tradeoff:

- Newly enabled controls may be included even when they are not relevant, but this is safer than hiding a newly actionable target.

## Token And Cost Growth

Symptoms:

- Multi-tool steps resend large histories repeatedly.
- Full tool returns create triangular token growth inside one agent step.
- Debugging model/provider changes is difficult without cost visibility.

Mitigations:

- Tool-return history compaction keeps recent rounds intact and compacts older ones.
- Per-run token and cost tracking.
- Provider/model fields in `run_summary.json` and `metrics.jsonl`.
- Local pricing fallback for providers where server-side cost is unavailable.

Tradeoff:

- Compaction can remove details that might have helped the model. The current default keeps the last few tool rounds intact.

## Benchmark-Specific Site Bugs

Symptoms observed on the external benchmark site:

- Back-to-back math puzzle state leaked across steps.
- Recursive iframe challenge had off-by-one behavior.
- Final-step code reveal did not produce a valid code path.

Mitigations:

- Stale puzzle state recovery.
- Recursive iframe benchmark workaround.
- Final-step finish navigation workaround.

Important distinction:

- These are not general browser-agent techniques. They are archived benchmark discoveries and should be described separately from the reusable architecture.
