## Observability

This agent emits both human-readable logs and machine-readable metrics.

### Files

- `logs/agent.log`: main runtime log (INFO by default; add `--log-level DEBUG` for per-tool timing lines)
- `logs/metrics.jsonl`: structured JSONL events (disable with `--no-metrics`)
- `logs/run_summary.json`: final rollup for the run (duration, total tokens, total cost, etc.)

### Metrics events (`logs/metrics.jsonl`)

Each line is a standalone JSON object with common fields:

- `ts`: UTC timestamp (ISO 8601)
- `run_id`: unique id for the run
- `event`: event name

Event types:

- `run_start`: `target_url`, `goal`, `max_steps`, `model`
- `snapshot`: `step`, `duration_ms`, `url`, `title`, `elements`, `handlers`
- `handler_extraction`: `step`, `duration_ms`, `handlers` (number of elements with JS event handlers found)
- `cdp_call`: `step`, `name`, `duration_ms` plus lightweight size hints (e.g. `dom_total_nodes`, `dom_strings`, `ax_nodes`, `frames`)
- `agent_call`: `step`, `agent` (`snapshot_filter`, `oracle`, `orchestrator`, or `browser_worker`), `duration_ms`, token fields (`input_tokens`, `output_tokens`, `requests`, `tool_calls`), and cost fields when available (`cost_usd`, `upstream_inference_cost_usd`)
- `tool_call`: `step`, `tool`, `ok`, `duration_ms` plus safe tool metadata (e.g. `element_id`, `text_len`, `code_len`, `query_len`, `limit`)
- `step_end`: `step`, `done`, `duration_ms` (and `worker_done` when available)
- `run_end`: `duration_ms`

### Tokens and cost

- Token usage comes from PydanticAI `RunUsage`.
- Cost is extracted from OpenRouter response metadata when OpenRouter provides it; if not present, cost fields are `null`/omitted.

### Quick inspection

- Total run summary: `cat logs/run_summary.json`
- Count events by type: `jq -r '.event' logs/metrics.jsonl | sort | uniq -c`
- Sum durations (rough): `jq -s 'map(select(.duration_ms != null) | .duration_ms) | add' logs/metrics.jsonl`
- Timing breakdown: `uv run python scripts/analyze_metrics.py logs/metrics.jsonl`
