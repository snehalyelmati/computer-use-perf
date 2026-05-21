# AgentLab And BrowserGym Benchmarks

This repository can run under AgentLab with BrowserGym owning the browser and task validation. The adapter lives in `benchmarks/agentlab/` and keeps the normal agent runtime responsible for perception, planning, tool execution, logs, and metrics.

## Runtime Shape

```mermaid
flowchart LR
    Study[AgentLab Study] --> Env[BrowserGym Env]
    Env -->|raw obs includes page| Adapter[ComputerUseAgentLabAgent]
    Adapter --> Bridge[Sync Playwright Bridge]
    Bridge --> Runtime[Single-Step Agent Runtime]
    Runtime --> Snapshot[CDP Snapshot + Handler Hints]
    Snapshot --> Agents[Oracle + Filter + Orchestrator + Worker]
    Agents --> Tools[DOM-First Semantic Tools]
    Tools -->|mutate live page| Env
    Adapter -->|returns noop()| Env
    Env --> Validate[BrowserGym Task Validation]
```

The important inversion is browser ownership:

- AgentLab/BrowserGym creates the environment, browser context, page, observations, and validation.
- The adapter requests `use_raw_page_output=True`, stores `obs["page"]`, and removes it before observations are pickled.
- `BrowserAgentStepRuntime` runs one internal step against the live page.
- The adapter returns `noop()` after the internal tool calls have already changed the page.
- The benchmark snapshot includes compact SVG graphics summaries and bounding boxes when labels are not enough; the worker can use `click_at` for coordinate targets and `draw` for path targets.
- BrowserGym remains the source of truth for task success. Internal `done=True` is logged in `AgentInfo` but does not replace BrowserGym termination.

## Installation

Use Python 3.12. The project metadata is pinned to `>=3.12,<3.13` so it is compatible with AgentLab.

```bash
uv sync --extra agentlab
uv run playwright install chromium
git clone https://github.com/Farama-Foundation/miniwob-plusplus.git .benchmarks/miniwob-plusplus
git -C .benchmarks/miniwob-plusplus reset --hard 7fd85d71a4b60325c6585396ec4f48377d049838
```

MiniWoB also requires the MiniWoB++ static site at BrowserGym's pinned commit and `MINIWOB_URL` pointing to it. WebArena requires the self-hosted services and the standard `WA_*` environment variables.

For the local smoke runner, the default MiniWoB URL is:

```bash
export MINIWOB_URL="file://$PWD/.benchmarks/miniwob-plusplus/miniwob/html/miniwob/"
```

## AgentLab Configuration

Import the adapter from a module path visible on `PYTHONPATH`:

```python
from agentlab.experiments.study import make_study

from benchmarks.agentlab import ComputerUseAgentArgs

agent_args = ComputerUseAgentArgs(
    provider="openrouter",
    model="z-ai/glm-4.7:nitro",
    unified=True,
    max_steps=20,
    max_elements=80,
    log_dir="logs/agentlab",
)

study = make_study(
    benchmark="miniwob",
    agent_args=[agent_args],
    comment="computer-use MiniWoB smoke",
)
study.run(n_jobs=1)
```

Start with single-task smoke tests before broad runs:

- `miniwob.click-button`
- `miniwob.enter-text`
- one form task
- one scroll or hover task

This repo includes a two-task smoke runner:

```bash
AGENTLAB_EXP_ROOT="$PWD/logs/agentlab/studies" \
MINIWOB_URL="file://$PWD/.benchmarks/miniwob-plusplus/miniwob/html/miniwob/" \
uv run --extra agentlab python benchmarks/agentlab/run_miniwob_smoke.py
```

Benchmark runs default to unified mode with `z-ai/glm-4.7:nitro`. Pass `--split-pipeline` only when comparing against the older filter/orchestrator/worker pipeline.

For WebArena, first run one self-hosted task, then a small dependency-safe subset, then WebArena Lite or WebArena-Verified, and only then a full run.

## Outputs

AgentLab saves its normal experiment artifacts under its experiment root. This adapter also writes this agent's native logs under `logs/agentlab/<run_id>/`:

- `agent.log`
- `agent_debug.log`
- `metrics.jsonl`
- `run_summary.json`
- optional `pages/` captures when enabled

Each `AgentInfo` includes a compact markdown summary, numeric stats for AgentLab aggregation, and `extra_info` with the internal trace, tool call summary, log directory, tokens, cost, and internal stop reason.
