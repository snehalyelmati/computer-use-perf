"""Analyze the latest agent run from logs to diagnose stuck/inefficiency patterns.

Reads local logs (no browser, no network) and emits a concise report:
- repeated goals/actions (normalized by stable-id removal)
- Oracle interventions
- candidate "missed" interactive elements whose labels match instruction phrases
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.core.pruning import extract_instruction_phrases
from src.agent.core.text_compress import compress_text_lines


_STABLE_ID_RE = re.compile(r"\bel_[0-9a-f]{6,}\b", re.IGNORECASE)


def _normalize(text: str) -> str:
    text = _STABLE_ID_RE.sub("el_*", text)
    text = " ".join((text or "").split())
    return text.strip()


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _find_run_segment(text: str, run_id: str) -> str:
    start_pat = re.compile(rf"Run start run_id={re.escape(run_id)}\b")
    end_pat = re.compile(rf"Run end run_id={re.escape(run_id)}\b")
    start = start_pat.search(text)
    if not start:
        return ""
    end = end_pat.search(text, start.end())
    if not end:
        return text[start.start() :]
    return text[start.start() : end.end()]


def _extract_goals(agent_log_segment: str) -> list[str]:
    goals: list[str] = []
    for line in agent_log_segment.splitlines():
        if "goal:" in line:
            # "INFO agent:     goal: ..."
            _, _, rest = line.partition("goal:")
            rest = rest.strip()
            if rest:
                goals.append(rest)
    return goals


def _extract_tool_actions(agent_log_segment: str) -> list[str]:
    actions: list[str] = []
    for line in agent_log_segment.splitlines():
        if "✓ " in line:
            actions.append(line.split("✓", 1)[1].strip())
    return actions


def _extract_oracle(agent_log_segment: str) -> list[str]:
    out: list[str] = []
    for line in agent_log_segment.splitlines():
        if "oracle:" in line:
            out.append(line.strip())
    return out


def _top_repeats(items: Iterable[str], *, limit: int = 10) -> list[tuple[int, str]]:
    counts: dict[str, int] = {}
    for raw in items:
        key = _normalize(raw)
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
    ranked = sorted(((c, k) for k, c in counts.items()), key=lambda t: (-t[0], t[1]))
    return ranked[:limit]


@dataclass(frozen=True)
class DebugSignals:
    useful_lines: list[str]
    chosen_ids: set[str]
    element_lines: dict[str, str]  # stable_id -> rendered label line


def _parse_py_list_repr(value: str) -> list[str]:
    try:
        parsed = ast.literal_eval(value)
    except Exception:
        return []
    if isinstance(parsed, list):
        return [str(x) for x in parsed if isinstance(x, (str, int, float))]
    return []


def _extract_debug_signals(debug_segment: str) -> DebugSignals:
    useful_lines: list[str] = []
    chosen_ids: set[str] = set()
    element_lines: dict[str, str] = {}

    # 1) Filter useful lines
    for line in debug_segment.splitlines():
        if "filter output step=" in line and "useful_text_lines=" in line:
            # "... useful_text_lines=[...]" Python list repr
            m = re.search(r"useful_text_lines=(\[[^\]]*\])", line)
            if not m:
                continue
            useful_lines.extend(_parse_py_list_repr(m.group(1)))

    # 2) Chosen ids from orchestrator output and worker goals
    for line in debug_segment.splitlines():
        if "orchestrator output step=" in line and "worker_goal=" in line:
            chosen_ids.update(_STABLE_ID_RE.findall(line))
        if "Goal:" in line:
            chosen_ids.update(_STABLE_ID_RE.findall(line))

    # 3) All element label lines shown in snapshots (from orchestrator/worker prompts)
    for line in debug_segment.splitlines():
        if line.lstrip().startswith("- el_"):
            m = re.match(r"\s*-\s*(el_[0-9a-f]{6,})\s*:\s*(.+)$", line.strip(), re.IGNORECASE)
            if not m:
                continue
            sid = m.group(1)
            element_lines[sid] = line.strip()

    return DebugSignals(useful_lines=useful_lines, chosen_ids=chosen_ids, element_lines=element_lines)


def _missed_elements(signals: DebugSignals) -> list[str]:
    # Derive phrases deterministically from useful lines to avoid site-specific heuristics.
    useful = compress_text_lines(signals.useful_lines, max_lines=30, max_chars=4000)
    phrases = extract_instruction_phrases(useful, oracle_hint=None)
    phrases = [p for p in phrases if len(p) >= 4]
    if not phrases:
        return []

    missed: list[tuple[int, str]] = []
    for sid, label in signals.element_lines.items():
        if sid in signals.chosen_ids:
            continue
        blob = label.lower()
        hits = sum(1 for phrase in phrases if phrase in blob)
        if hits:
            missed.append((hits, label))
    missed.sort(key=lambda t: (-t[0], t[1]))
    return [item[1] for item in missed[:15]]


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze last run logs for stuck/inefficiency patterns")
    parser.add_argument("--log-dir", default="logs", help="Log directory (default: logs)")
    parser.add_argument("--run-id", default=None, help="Run id to analyze (default: from run_summary.json)")
    args = parser.parse_args()

    log_dir = Path(args.log_dir)
    run_summary_path = log_dir / "run_summary.json"
    if not run_summary_path.exists():
        raise SystemExit(f"Missing {run_summary_path}")
    try:
        run_summary = json.loads(_read_text(run_summary_path))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Failed to parse {run_summary_path}: {exc}") from exc
    run_id = str(args.run_id or run_summary.get("run_id") or "").strip()
    if not run_id:
        raise SystemExit("No run_id provided and run_summary.json missing run_id")

    agent_log_path = log_dir / "agent.log"
    agent_debug_path = log_dir / "agent_debug.log"
    if not agent_log_path.exists():
        raise SystemExit(f"Missing {agent_log_path}")
    agent_log = _read_text(agent_log_path)
    agent_segment = _find_run_segment(agent_log, run_id)
    if not agent_segment:
        raise SystemExit(f"Run id {run_id} not found in {agent_log_path}")

    print(f"run_id={run_id} steps={run_summary.get('steps')} stop_reason={run_summary.get('stop_reason')}")
    if run_summary.get("last_summary"):
        print(f"last_summary={run_summary.get('last_summary')}")
    print("")

    goals = _extract_goals(agent_segment)
    actions = _extract_tool_actions(agent_segment)
    oracle_lines = _extract_oracle(agent_segment)

    print("Top repeated goals (stable-ids normalized):")
    for count, item in _top_repeats(goals, limit=8):
        if count > 1:
            print(f"  x{count}: {item}")
    print("")

    print("Top repeated actions (stable-ids normalized):")
    for count, item in _top_repeats(actions, limit=8):
        if count > 1:
            print(f"  x{count}: {item}")
    print("")

    if oracle_lines:
        print("Oracle interventions:")
        for line in oracle_lines:
            print(f"  {line}")
        print("")

    if agent_debug_path.exists():
        debug_segment = _find_run_segment(_read_text(agent_debug_path), run_id)
        if debug_segment:
            signals = _extract_debug_signals(debug_segment)
            missed = _missed_elements(signals)
            if missed:
                print("Candidate missed elements (match instruction phrases but not chosen):")
                for line in missed:
                    print(f"  {line}")
                print("")
            else:
                print("No missed-element candidates found from debug snapshot text.")
        else:
            print("Run id not found in agent_debug.log (skipping missed-element scan).")
    else:
        print("Missing agent_debug.log (skipping debug scan).")


if __name__ == "__main__":
    main()
