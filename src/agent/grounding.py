from __future__ import annotations

import re
from dataclasses import dataclass

from .text_budget import extract_candidate_values


_DATA_PAIR_RE = re.compile(r"\b([A-Za-z0-9_-]{1,32})=([^\s,;]+)")


@dataclass(frozen=True)
class GroundingResult:
    data: str | None
    dropped_pairs: list[str]


def parse_data_pairs(data: str | None) -> list[tuple[str, str]]:
    """Parse Overview DATA into (key, value) pairs.

    Expected format is compact key=value entries separated by spaces/commas/semicolons.
    """
    if not data:
        return []
    s = data.strip()
    if not s:
        return []

    pairs: list[tuple[str, str]] = []
    for m in _DATA_PAIR_RE.finditer(s):
        k = m.group(1).strip()
        v = m.group(2).strip().strip("\"'")
        if k and v:
            pairs.append((k, v))
    return pairs


def format_data_pairs(pairs: list[tuple[str, str]]) -> str | None:
    if not pairs:
        return None
    # Keep compact and deterministic ordering.
    parts = [f"{k}={v}" for k, v in pairs]
    return " ".join(parts)


def update_observed_values(observed: set[str], *sources: object) -> None:
    """Update observed value set from mixed text sources."""
    for src in sources:
        if src is None:
            continue
        if isinstance(src, str):
            observed.update(extract_candidate_values(src))
            continue
        if isinstance(src, (list, tuple)):
            for item in src:
                if isinstance(item, str):
                    observed.update(extract_candidate_values(item))
                else:
                    observed.update(extract_candidate_values(str(item)))
            continue
        observed.update(extract_candidate_values(str(src)))


def ground_data_to_observed(
    data: str | None, observed_values: set[str]
) -> GroundingResult:
    """Drop DATA pairs whose values were never observed."""
    if not data:
        return GroundingResult(data=None, dropped_pairs=[])

    pairs = parse_data_pairs(data)
    if not pairs:
        # No parseable pairs; leave as-is (but normalize whitespace).
        normalized = data.strip() or None
        return GroundingResult(data=normalized, dropped_pairs=[])

    kept: list[tuple[str, str]] = []
    dropped: list[str] = []
    for k, v in pairs:
        if v in observed_values:
            kept.append((k, v))
        else:
            dropped.append(f"{k}=<value>")

    return GroundingResult(data=format_data_pairs(kept), dropped_pairs=dropped)


_SINGLE_QUOTED_RE = re.compile(r"'[^']*'")
_DOUBLE_QUOTED_RE = re.compile(r"\"[^\"]*\"")


def scrub_values(text: str) -> str:
    """Remove literal values from free-form text.

    Intended for memory blocks like failed attempts.
    """
    if not text:
        return text
    s = _SINGLE_QUOTED_RE.sub("'<value>'", text)
    s = _DOUBLE_QUOTED_RE.sub('"<value>"', s)
    # Normalize key=value values.
    s = re.sub(r"\b([A-Za-z0-9_-]{1,32})=([^\s,;]+)", r"\1=<value>", s)
    return s
