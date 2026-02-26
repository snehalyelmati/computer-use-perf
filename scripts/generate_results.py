#!/usr/bin/env python3
"""Generate results.md from all run logs.

Maintains a persistent history in logs/results_history.json so that results
survive log pruning.  Each invocation scans logs/ for new run directories,
merges them into the history file, then regenerates results.md from the
full history.

Usage:
    uv run scripts/generate_results.py
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "results.md"
HISTORY_PATH = LOGS_DIR / "results_history.json"
CHALLENGE_MAP_PATH = LOGS_DIR / "full_challenge_map.json"

_RUN_DIR_RE = re.compile(r"^[0-9a-f]{32}$")
_STEP_RE = re.compile(r"/step(\d+)")
_VERSION_RE = re.compile(r"version=(\d+)")


def _load_challenge_map() -> dict[str, dict[str, dict]]:
    """Load full_challenge_map.json → {version: {step: info}}."""
    if not CHALLENGE_MAP_PATH.exists():
        return {}
    with open(CHALLENGE_MAP_PATH) as f:
        return json.load(f)


def _load_history() -> dict[str, dict]:
    """Load existing results history keyed by run_id."""
    if not HISTORY_PATH.exists():
        return {}
    with open(HISTORY_PATH) as f:
        entries = json.load(f)
    return {e["run_id"]: e for e in entries}


def _save_history(history: dict[str, dict]) -> None:
    """Persist the full history to disk."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    entries = sorted(history.values(), key=lambda e: e.get("ts", ""), reverse=True)
    HISTORY_PATH.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _parse_run(run_dir: Path) -> dict | None:
    """Parse a single run directory into a result dict."""
    summary_path = run_dir / "run_summary.json"
    metrics_path = run_dir / "metrics.jsonl"

    if not summary_path.exists():
        return None

    with open(summary_path) as f:
        summary = json.load(f)

    # Prefer active_duration_ms (excludes retry waits); fall back to duration_ms
    active_ms = summary.get("active_duration_ms") or summary.get("duration_ms", 0)
    result: dict = {
        "run_id": run_dir.name,
        "ts": summary.get("ts", ""),
        "active_duration_ms": active_ms,
        "steps": summary.get("steps", 0),
        "stop_reason": summary.get("stop_reason", "—"),
        "cost_usd": summary.get("cost_usd"),
        "total_tokens": summary.get("total_tokens"),
        "git_commit": summary.get("git_commit"),
        "provider": summary.get("provider"),
        "model": summary.get("model"),
    }

    # Parse metrics.jsonl for run_start info and max challenge step
    max_challenge_step = 0
    version: str | None = None

    if metrics_path.exists():
        with open(metrics_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if event.get("event") == "run_start":
                    # Backfill from metrics if not in summary (older runs)
                    if not result["model"]:
                        result["model"] = event.get("model")
                    if not result["provider"]:
                        result["provider"] = event.get("provider")
                    if not result["git_commit"]:
                        result["git_commit"] = event.get("git_commit")

                elif event.get("event") == "snapshot":
                    url = event.get("url", "")
                    step_match = _STEP_RE.search(url)
                    if step_match:
                        max_challenge_step = max(
                            max_challenge_step, int(step_match.group(1))
                        )
                    ver_match = _VERSION_RE.search(url)
                    if ver_match:
                        version = ver_match.group(1)

    result["max_challenge_step"] = max_challenge_step
    result["version"] = version

    return result


# ── Formatting helpers ────────────────────────────────────────────────


def _format_duration(ms: int) -> str:
    """Format milliseconds as m:ss."""
    total_seconds = ms // 1000
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f"{minutes}:{seconds:02d}"


def _format_cost(cost: float | None) -> str:
    if cost is None:
        return "—"
    return f"${cost:.4f}"


def _format_tokens(tokens: int | None) -> str:
    if tokens is None:
        return "—"
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.0f}K"
    return str(tokens)


def _challenge_label(
    challenge_map: dict, version: str | None, step: int
) -> str:
    """Look up the challenge type for a given version+step."""
    if not version or not challenge_map:
        return "—"
    ver_data = challenge_map.get(version, {})
    step_data = ver_data.get(str(step), {})
    return step_data.get("challengeType", "—")


def _steps_reached_label(max_step: int, total: int = 30) -> str:
    """Format challenge progress as 'N/30'."""
    if max_step == 0:
        return "0/30"
    return f"{max_step}/{total}"


# ── Main generation ──────────────────────────────────────────────────


def generate() -> str:
    """Merge new runs into history and generate the results markdown."""
    challenge_map = _load_challenge_map()
    history = _load_history()

    # Scan log dirs for any runs not yet in history
    new_count = 0
    for entry in LOGS_DIR.iterdir():
        if not entry.is_dir() or not _RUN_DIR_RE.match(entry.name):
            continue
        if entry.name in history:
            continue
        result = _parse_run(entry)
        if result:
            history[result["run_id"]] = result
            new_count += 1

    # Persist updated history
    _save_history(history)

    # Sort by timestamp, newest first
    runs = sorted(history.values(), key=lambda r: r.get("ts", ""), reverse=True)

    # Build markdown
    lines: list[str] = []
    lines.append("# Agent Run Results\n")
    lines.append(
        f"*Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*\n"
    )
    lines.append(f"**Total runs:** {len(runs)}\n")

    # Summary table
    lines.append("## Runs\n")
    lines.append(
        "| Date | Run ID | Commit | Provider | Model | Steps Reached | Stuck On | Duration | Tokens | Cost | Stop Reason |"
    )
    lines.append(
        "|------|--------|--------|----------|-------|---------------|----------|----------|--------|------|-------------|"
    )

    for r in runs:
        # Parse date
        ts = r.get("ts", "")
        try:
            dt = datetime.fromisoformat(ts)
            date_str = dt.strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            date_str = "—"

        run_id_short = r["run_id"][:8]
        commit = r.get("git_commit") or "—"
        provider = r.get("provider") or "—"
        model = r.get("model") or "—"
        model_short = model.split("/")[-1] if "/" in model else model

        max_step = r.get("max_challenge_step", 0)
        steps_reached = _steps_reached_label(max_step)

        version = r.get("version")
        if max_step > 0 and max_step < 30:
            stuck_on = _challenge_label(challenge_map, version, max_step + 1)
        elif max_step >= 30:
            stuck_on = "completed"
        else:
            stuck_on = "—"

        duration = _format_duration(r.get("active_duration_ms", 0))
        tokens = _format_tokens(r.get("total_tokens"))
        cost = _format_cost(r.get("cost_usd"))
        stop_reason = r.get("stop_reason") or "—"

        lines.append(
            f"| {date_str} | `{run_id_short}` | {commit} | {provider} | {model_short} | {steps_reached} | {stuck_on} | {duration} | {tokens} | {cost} | {stop_reason} |"
        )

    return "\n".join(lines) + "\n"


def main() -> None:
    content = generate()
    OUTPUT_PATH.write_text(content, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH} ({len(content)} bytes, {content.count(chr(10))} lines)")


if __name__ == "__main__":
    main()
