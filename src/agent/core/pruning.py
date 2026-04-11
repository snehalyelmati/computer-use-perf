"""Deterministic pruning helpers (instruction-anchored keep and container expansion)."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence

from src.agent.context.snapshot import ElementSnapshot, element_text_blob


_STABLE_ID_RE = re.compile(r"\bel_[0-9a-f]{6,}\b", re.IGNORECASE)
_QUOTED_PHRASE_RE = re.compile(r"\"([^\"]{3,60})\"")
_ACTION_PHRASE_RE = re.compile(r"\b(click|press|select)\b\s+(.+)", re.IGNORECASE)
_TRIM_PUNCT_RE = re.compile(r"^[\s\W]+|[\s\W]+$")


def extract_stable_ids(text: str) -> set[str]:
    return {m.group(0) for m in _STABLE_ID_RE.finditer(text or "")}


def _normalize_phrase(value: str) -> str:
    return " ".join((value or "").strip().split()).lower()


def extract_instruction_phrases(
    useful_lines: Sequence[str],
    *,
    oracle_hint: str | None = None,
    max_phrases: int = 25,
) -> list[str]:
    """Extract likely UI label phrases from instruction text (deterministic)."""
    phrases: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        normalized = _normalize_phrase(raw)
        if len(normalized) < 3:
            return
        if normalized in seen:
            return
        seen.add(normalized)
        phrases.append(normalized)

    for line in useful_lines:
        for match in _QUOTED_PHRASE_RE.finditer(line or ""):
            add(match.group(1))
        m = _ACTION_PHRASE_RE.search(line or "")
        if m:
            remainder = m.group(2) or ""
            remainder = _TRIM_PUNCT_RE.sub("", remainder)
            tokens = remainder.split()
            if len(tokens) >= 2:
                add(" ".join(tokens[:4]))

    if oracle_hint:
        for match in _QUOTED_PHRASE_RE.finditer(oracle_hint):
            add(match.group(1))

    return phrases[: max(0, int(max_phrases))]


def _role_weight(role: str | None) -> float:
    r = (role or "").strip().lower()
    if r in {"button", "textbox"}:
        return 3.0
    if r in {"radio", "checkbox"}:
        return 2.0
    if r == "link":
        return 1.5
    return 1.0


def match_phrases_to_elements(
    phrases: Sequence[str],
    elements: Iterable[ElementSnapshot],
    *,
    max_matches: int = 15,
) -> list[str]:
    """Match extracted phrases to interactive elements (deterministic)."""
    normalized_phrases = [_normalize_phrase(p) for p in phrases if _normalize_phrase(p)]
    if not normalized_phrases or max_matches <= 0:
        return []

    scored: dict[str, float] = {}
    for element in elements:
        blob = _normalize_phrase(element_text_blob(element))
        if not blob:
            continue
        for phrase in normalized_phrases:
            if len(phrase) < 4:
                continue
            if phrase in blob:
                base = _role_weight(element.role)
                conf = float(element.interactive_confidence or 0.6)
                bonus = min(1.0, 0.25 * max(0, len(phrase.split()) - 1))
                handler_bonus = 0.0
                if element.handlers and ("click" in element.handlers):
                    handler_bonus = 0.5
                elif (element.attributes or {}).get("onclick"):
                    handler_bonus = 0.5
                elif (element.interactive_reason or "") == "detected_handler":
                    handler_bonus = 0.5
                area_bonus = 0.0
                if element.area and element.area > 0:
                    area_bonus = max(0.0, min(1.0, (math.log10(float(element.area)) - 4.0) / 2.0))
                score = base * max(0.1, min(conf, 1.0)) + bonus + handler_bonus + area_bonus
                prev = scored.get(element.stable_id, 0.0)
                if score > prev:
                    scored[element.stable_id] = score

    ranked = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))
    return [sid for sid, _ in ranked[: int(max_matches)]]
