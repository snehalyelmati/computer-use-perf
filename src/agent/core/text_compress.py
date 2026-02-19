"""Deterministic compression for text-line blobs passed to LLM prompts."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass


_ACTION_VERBS: tuple[str, ...] = (
    "click",
    "select",
    "reveal",
    "times",
    "submit",
    "enter",
    "press",
)

_STABLE_ID_RE = re.compile(r"\bel_[0-9a-f]{6,}\b", re.IGNORECASE)
_SIX_CHAR_CODE_RE = re.compile(r"\b[A-Z0-9]{6}\b")
_WHITESPACE_RE = re.compile(r"\s+")
_DIGITS_RE = re.compile(r"\d+")
_PUNCT_RUN_RE = re.compile(r"([:=/@#_\-])\1+")


def _normalize_line(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", (value or "").strip())


def _shape_signature(value: str) -> str:
    lowered = _normalize_line(value).lower()
    lowered = _DIGITS_RE.sub("#", lowered)
    lowered = _PUNCT_RUN_RE.sub(r"\1", lowered)
    return lowered


def _is_instruction_like(value: str) -> bool:
    lowered = value.lower()
    return any(token in lowered for token in _ACTION_VERBS)


def _is_value_or_error_like(value: str) -> bool:
    if "❌" in value or "✅" in value:
        return True
    if _STABLE_ID_RE.search(value):
        return True
    if _SIX_CHAR_CODE_RE.search(value):
        return True
    return False


@dataclass(frozen=True)
class _LineItem:
    idx: int
    text: str
    bucket_rank: int


def compress_text_lines(
    lines: Sequence[str],
    *,
    max_lines: int,
    max_chars: int,
    per_signature: int = 1,
) -> list[str]:
    """Compress text lines deterministically to reduce prompt tokens.

    Keeps a diverse subset of instruction/value/error lines and de-duplicates templated
    content by a shape signature. Does not use site-specific keywords.
    """
    if not lines or max_lines <= 0 or max_chars <= 0:
        return []

    per_signature = max(1, int(per_signature))
    max_lines = max(1, int(max_lines))
    max_chars = max(50, int(max_chars))

    seen_counts: dict[str, int] = {}
    items: list[_LineItem] = []

    for idx, raw in enumerate(lines):
        normalized = _normalize_line(str(raw))
        if len(normalized) < 3:
            continue

        sig = _shape_signature(normalized)
        count = seen_counts.get(sig, 0)
        if count >= per_signature:
            continue
        seen_counts[sig] = count + 1

        if _is_value_or_error_like(normalized):
            bucket_rank = 0
        elif _is_instruction_like(normalized):
            bucket_rank = 1
        else:
            bucket_rank = 2
        items.append(_LineItem(idx=idx, text=normalized, bucket_rank=bucket_rank))

    items.sort(key=lambda item: (item.bucket_rank, item.idx))

    out: list[str] = []
    total_chars = 0
    for item in items:
        if len(out) >= max_lines:
            break
        added = len(item.text) + (1 if out else 0)
        if total_chars + added > max_chars:
            continue
        out.append(item.text)
        total_chars += added

    if not out and items:
        out = [items[0].text[:max_chars]]

    return out

