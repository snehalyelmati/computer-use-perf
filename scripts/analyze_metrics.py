from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
from typing import Any, Iterable


def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    if q <= 0:
        return float(sorted_values[0])
    if q >= 1:
        return float(sorted_values[-1])
    pos = q * (len(sorted_values) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(sorted_values[lo])
    weight = pos - lo
    return float(sorted_values[lo] * (1.0 - weight) + sorted_values[hi] * weight)


def _summary(values: Iterable[float]) -> dict[str, float]:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return {"count": 0.0, "avg": 0.0, "p50": 0.0, "p95": 0.0}
    avg = sum(vals) / len(vals)
    return {
        "count": float(len(vals)),
        "avg": avg,
        "p50": _quantile(vals, 0.5),
        "p95": _quantile(vals, 0.95),
    }


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze agent metrics.jsonl timings")
    parser.add_argument("path", help="Path to metrics.jsonl (e.g. logs/latest/metrics.jsonl)")
    args = parser.parse_args()

    path = Path(args.path)
    if not path.exists():
        raise SystemExit(f"File not found: {path}")

    records = _load_jsonl(path)

    by_event: dict[str, list[float]] = defaultdict(list)
    cdp_by_name: dict[str, list[float]] = defaultdict(list)
    agent_by_name: dict[str, list[float]] = defaultdict(list)

    step_snapshot: dict[int, float] = {}
    step_agents: dict[int, dict[str, float]] = defaultdict(dict)
    step_tool_total: dict[int, float] = defaultdict(float)
    step_total: dict[int, float] = {}

    for rec in records:
        event = str(rec.get("event") or "")
        step = rec.get("step")
        duration_ms = rec.get("duration_ms")
        if isinstance(duration_ms, (int, float)):
            by_event[event].append(float(duration_ms))

        if event == "cdp_call":
            name = str(rec.get("name") or "unknown")
            if isinstance(duration_ms, (int, float)):
                cdp_by_name[name].append(float(duration_ms))

        if event == "agent_call":
            name = str(rec.get("agent") or "unknown")
            if isinstance(duration_ms, (int, float)):
                agent_by_name[name].append(float(duration_ms))
                if isinstance(step, int):
                    step_agents[int(step)][name] = float(duration_ms)

        if event == "snapshot" and isinstance(step, int) and isinstance(duration_ms, (int, float)):
            step_snapshot[int(step)] = float(duration_ms)

        if event == "tool_call" and isinstance(step, int) and isinstance(duration_ms, (int, float)):
            step_tool_total[int(step)] += float(duration_ms)

        if event == "step_end" and isinstance(step, int) and isinstance(duration_ms, (int, float)):
            step_total[int(step)] = float(duration_ms)

    print(f"Loaded {len(records)} records from {path}")
    print()

    def print_table(title: str, rows: list[tuple[str, dict[str, float]]]) -> None:
        print(title)
        if not rows:
            print("  (none)")
            print()
            return
        for name, stats in rows:
            print(
                f"  {name:32s} count={int(stats['count']):4d} avg={stats['avg']:7.1f}ms"
                f" p50={stats['p50']:7.1f}ms p95={stats['p95']:7.1f}ms"
            )
        print()

    print_table(
        "Event timings (duration_ms):",
        sorted(((name, _summary(vals)) for name, vals in by_event.items()), key=lambda r: (-r[1]["p95"], r[0])),
    )
    print_table(
        "CDP call timings:",
        sorted(((name, _summary(vals)) for name, vals in cdp_by_name.items()), key=lambda r: (-r[1]["p95"], r[0])),
    )
    print_table(
        "Agent call timings:",
        sorted(((name, _summary(vals)) for name, vals in agent_by_name.items()), key=lambda r: (-r[1]["p95"], r[0])),
    )

    steps = sorted(set(step_snapshot.keys()) | set(step_agents.keys()) | set(step_tool_total.keys()) | set(step_total.keys()))
    print("Per-step breakdown:")
    if not steps:
        print("  (none)")
        return 0
    for step in steps[:50]:
        snap = step_snapshot.get(step, 0.0)
        agents = step_agents.get(step, {})
        filt = agents.get("snapshot_filter", 0.0)
        orch = agents.get("orchestrator", 0.0)
        work = agents.get("browser_worker", 0.0)
        tools = step_tool_total.get(step, 0.0)
        total = step_total.get(step, 0.0)
        print(
            f"  step={step:3d} total={total:7.1f}ms snapshot={snap:7.1f}ms"
            f" filter={filt:7.1f}ms orch={orch:7.1f}ms worker={work:7.1f}ms tools={tools:7.1f}ms"
        )
    if len(steps) > 50:
        print(f"  ... ({len(steps) - 50} more steps omitted)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
