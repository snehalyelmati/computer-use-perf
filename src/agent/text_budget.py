from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence


_CODE_RE = re.compile(r"\b[A-Z0-9]{4,16}\b")
_CODE_LOOSE_RE = re.compile(
    r"\b(?=[A-Za-z0-9_-]{4,32}\b)(?=.*[0-9])(?=.*[A-Za-z])[A-Za-z0-9_-]+\b"
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_KEY_VALUE_RE = re.compile(r"\b[A-Za-z0-9_-]{1,32}=([^\s,;]+)")


def score_line_default(line: str) -> int:
    """Heuristic score for keeping a line in LLM context.

    Higher score means more likely to contain instructions, errors, progress, or values.
    """
    if not line:
        return 0

    s = line.strip()
    if not s:
        return 0

    low = s.lower()
    score = 0

    # Strong signals: explicit errors / invalidations.
    if any(
        w in low
        for w in (
            "error",
            "invalid",
            "incorrect",
            "wrong",
            "failed",
            "try again",
            "warning",
        )
    ):
        score += 50

    # Progress indicators.
    if re.search(
        r"\b(step\s*\d+|\d+\s*/\s*\d+|\d+\s+of\s+\d+|progress\s*[:\s]+\d+%?)\b", low
    ):
        score += 30

    # Value-ish content.
    if _KEY_VALUE_RE.search(s):
        score += 30
    if _CODE_RE.search(s):
        score += 25
    if _EMAIL_RE.search(s):
        score += 20

    # Imperatives / task-y language.
    if any(
        w in low
        for w in (
            "click",
            "type",
            "enter",
            "select",
            "choose",
            "drag",
            "drop",
            "submit",
            "press",
            "hover",
            "scroll",
        )
    ):
        score += 10

    return score


def select_lines_for_budget(
    lines: Sequence[str] | None,
    *,
    max_chars: int,
    score_fn: Callable[[str], int] = score_line_default,
    dedupe: bool = True,
) -> list[str]:
    """Select whole lines up to a character budget.

    Strategy:
    - Score all lines.
    - Pick highest-score lines first.
    - Emit in original order for readability.
    - Never truncate a line; only include or exclude.
    """
    if not lines:
        return []

    if max_chars <= 0:
        return []

    indexed: list[tuple[int, str]] = [
        (i, (line or "").strip()) for i, line in enumerate(lines)
    ]
    indexed = [(i, line) for i, line in indexed if line]
    if not indexed:
        return []

    if dedupe:
        seen = set()
        deduped: list[tuple[int, str]] = []
        for i, line in indexed:
            key = re.sub(r"\s+", " ", line)
            if key in seen:
                continue
            seen.add(key)
            deduped.append((i, line))
        indexed = deduped

    scored: list[tuple[int, int, str]] = []
    for i, line in indexed:
        scored.append((score_fn(line), i, line))
    scored.sort(key=lambda t: (t[0], -t[1]), reverse=True)

    chosen: list[tuple[int, str]] = []
    used = 0
    for score, i, line in scored:
        cost = len(line) + 1  # newline
        if used + cost > max_chars:
            continue
        chosen.append((i, line))
        used += cost
        if used >= max_chars:
            break

    chosen.sort(key=lambda t: t[0])
    return [line for _, line in chosen]


def format_lines_block(
    heading: str,
    lines: Sequence[str] | None,
    *,
    max_chars: int,
) -> str:
    selected = select_lines_for_budget(lines, max_chars=max_chars)
    if not selected:
        return f"{heading}: none"
    return f"{heading}:\n" + "\n".join(selected)


def extract_candidate_values(text: str) -> set[str]:
    """Extract likely values from arbitrary text.

    Used for grounding only; keep conservative to avoid capturing lots of noise.
    """
    if not text:
        return set()
    found: set[str] = set()

    for m in _KEY_VALUE_RE.finditer(text):
        val = m.group(1).strip().strip("\"'")
        if val:
            found.add(val)

    for m in _CODE_RE.finditer(text):
        found.add(m.group(0))

    for m in _CODE_LOOSE_RE.finditer(text):
        found.add(m.group(0))

    for m in _EMAIL_RE.finditer(text):
        found.add(m.group(0))

    return found


def flatten_text_sources(sources: Iterable[object]) -> list[str]:
    """Best-effort flattening of mixed inputs into a list of strings."""
    out: list[str] = []
    for src in sources:
        if src is None:
            continue
        if isinstance(src, str):
            s = src.strip()
            if s:
                out.append(s)
            continue
        if isinstance(src, (list, tuple)):
            for item in src:
                if isinstance(item, str):
                    s = item.strip()
                    if s:
                        out.append(s)
        else:
            s = str(src).strip()
            if s:
                out.append(s)
    return out
