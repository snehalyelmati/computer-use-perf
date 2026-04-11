# LLM Resilience

The browser agent has two layers of protection against transient LLM failures:

1. **Request-level retries** (`ResilientModel`) — automatic retry with per-category backoff
2. **Step-level degradation** — try/except around each agent call with graceful fallbacks

## Request-Level Retries

`ResilientModel` wraps each pydantic-ai `Model` with nested retry loops:

- **Inner loop**: catches `ModelHTTPError`, retries per status-code category
- **Outer loop**: catches `ModelAPIError` (network/timeout), retries independently

Network errors reset the HTTP retry counter (nested design).

### Retry Policy Table

| Error Category | Status Codes | Max Retries | Delays (seconds) |
|---|---|---|---|
| Rate limit | 429 | 3 | 5, 15, 30 (or `Retry-After` header, capped at 60s) |
| Server error | 500, 502, 503, 504 | 3 | 2, 4, 8 |
| Network/timeout | N/A (connection failures) | 2 | 2, 4 |
| Bad request | 400 | 1 | 1 |
| Auth/other | 401, 403, etc. | 0 | — (immediate raise) |

### Retry-After Header

For 429 responses, `ResilientModel` extracts the `Retry-After` header from the chained exception cause (the underlying `APIStatusError` from the OpenAI SDK). The value is capped at 60 seconds. If the header is missing or unparseable, the policy's default delays are used.

## Step-Level Degradation

Each agent call in the step pipeline is wrapped in a try/except with a specific fallback:

| Agent | On Failure | Behavior |
|---|---|---|
| Oracle | Skip advice | Proceed without oracle hint; advisory-only, no impact on correctness |
| Filter | Fall back to full snapshot | Orchestrator and Worker see all elements (more context, not less) |
| Orchestrator | Retry once, then skip step | One retry covers transient blips; if both fail, skip step rather than crash |
| Worker | Record error summary | Step continues to the done-gate with an error summary |

## Step Timeout

Each step has a deadline (default: 300s, configure with `--step-timeout`). The deadline is enforced per-LLM-call using `asyncio.wait_for()`. If the deadline is exceeded:

- The timed-out LLM call raises `TimeoutError`
- The existing try/except handler for that agent catches it
- A `_step_timed_out` flag is set
- Before each subsequent LLM section, the flag is checked and the step is skipped if set
- Memory records the timeout and the step ends

## PydanticAI Validation Retries

PydanticAI agents are configured with `retries=1`, which means if the LLM output fails Pydantic validation (e.g. bad JSON structure), PydanticAI automatically retries once. This is independent of `ResilientModel`'s HTTP/network retries.

## CLI Arguments

| Argument | Default | Description |
|---|---|---|
| `--timeout` | 60 | Timeout in seconds for each LLM HTTP request |
| `--max-retries` | 2 | Max retries for transient LLM errors; 0 disables the resilient wrapper |
| `--step-timeout` | 300 | Timeout in seconds for an entire step's LLM pipeline |

## Metrics Events

Resilience events are captured in `metrics.jsonl`:

- `agent_call` with `error=True` — an agent call failed and was handled by the fallback
- `step_end` with `timeout=True` — step was skipped due to timeout
- `step_end` with `skipped=True` — step was skipped due to orchestrator failure

## Architecture

```
┌─────────────────────────────────────────────┐
│              ResilientModel                  │
│  ┌─────────────────────────────────────────┐│
│  │ Outer loop: network/timeout retries     ││
│  │  ┌───────────────────────────────────┐  ││
│  │  │ Inner loop: HTTP status retries   │  ││
│  │  │  429 → RATE_LIMIT_POLICY          │  ││
│  │  │  5xx → SERVER_ERROR_POLICY        │  ││
│  │  │  400 → BAD_REQUEST_POLICY         │  ││
│  │  └───────────────────────────────────┘  ││
│  └─────────────────────────────────────────┘│
└─────────────────────────────────────────────┘
         ↓ wraps each Model instance

Step pipeline:
  Oracle  → try/except (advisory, skip on failure)
  Filter  → try/except (fall back to full snapshot)
  Orchestrator → retry once, then skip step
  Worker  → try/except (record error summary)

Each .run() call uses asyncio.wait_for(coro, remaining_deadline)
```
