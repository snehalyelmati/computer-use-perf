## Observability

This agent emits both human-readable logs and machine-readable metrics.

### Files

Each run writes to its own `logs/<run_id>/` subdirectory. A `logs/latest` symlink points to the most recent run. Old directories are pruned at startup (default: keep last 10; configure with `--max-log-runs`).

- `logs/latest/agent.log`: main runtime log (INFO by default; add `--log-level DEBUG` for per-tool timing lines)
- `logs/latest/agent_debug.log`: DEBUG log with prompts, structured outputs, diffs, memory, and traces
- `logs/latest/metrics.jsonl`: structured JSONL events (disable with `--no-metrics`)
- `logs/latest/run_summary.json`: final rollup for the run (duration, total tokens, total cost, etc.)
- `logs/latest/pages/`: optional saved HTML snapshots and `manifest.jsonl` when `--save-pages` is enabled

AgentLab benchmark studies also write report artifacts under `logs/agentlab/studies/<study>/`:

- `benchmark_report.json`
- `benchmark_report.md`
- `per_task_results.csv`
- `failed_tasks.md`

Treat `logs/agentlab/studies/<study>/` as the durable benchmark artifact location. Native per-episode log directories under `logs/agentlab/<run_id>/` follow the runtime log-retention policy and may be pruned independently, so `benchmark_report.md` can contain missing native-log warnings even when the AgentLab study result remains valid.

Each AgentLab step also carries an `AgentInfo` payload. The numeric `stats` token/cost fields are per-step deltas so AgentLab aggregation does not double-count cumulative totals. The cumulative native run totals are preserved under `extra_info.cumulative_usage`, and `extra_info.validation` records the latest external BrowserGym validation signal when available.

### Metrics events (`logs/latest/metrics.jsonl`)

Each line is a standalone JSON object with common fields:

- `ts`: UTC timestamp (ISO 8601)
- `run_id`: unique id for the run
- `event`: event name

Event types:

- `run_start`: `target_url`, `goal`, `max_steps`, `model`
- `snapshot`: `step`, `duration_ms`, `url`, `title`, `elements`, `handlers`
- `handler_extraction`: `step`, `duration_ms`, `handlers` (number of elements with JS event handlers found)
- `cdp_call`: `step`, `name`, `duration_ms` plus lightweight size hints (e.g. `dom_total_nodes`, `dom_strings`, `ax_nodes`, `frames`)
- `agent_call`: `step`, `agent` (`snapshot_filter`, `oracle`, `orchestrator`, `browser_worker`, or `unified`), `duration_ms`, token fields (`input_tokens`, `output_tokens`, `requests`, `tool_calls`), and cost fields when available (`cost_usd`, `upstream_inference_cost_usd`)
- `tool_call`: `step`, `tool`, `ok`, `duration_ms` plus safe tool metadata (e.g. `element_id`, pointer/slider geometry, `text_len`, `code_len`, `query_len`, `limit`)
- `step_end`: `step`, `done`, `duration_ms` (and `worker_done` when available)
- `run_end`: `duration_ms`

### Tokens and cost

- Token usage comes from PydanticAI `RunUsage`.
- Cost is extracted from OpenRouter response metadata when OpenRouter provides it; if not present, cost fields are `null`/omitted.
- In AgentLab reports, use `AgentInfo.stats` for per-step accounting and `AgentInfo.extra_info.cumulative_usage` for native cumulative totals.

### Quick inspection

- Total run summary: `cat logs/latest/run_summary.json`
- Count events by type: `jq -r '.event' logs/latest/metrics.jsonl | sort | uniq -c`
- Sum durations (rough): `jq -s 'map(select(.duration_ms != null) | .duration_ms) | add' logs/latest/metrics.jsonl`
- Timing breakdown: `uv run python scripts/analyze_metrics.py logs/latest/metrics.jsonl`
