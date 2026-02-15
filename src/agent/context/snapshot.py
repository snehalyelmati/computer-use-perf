"""CDP-based DOM snapshot extraction (scaffold)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass
class ElementSnapshot:
    """Stable element reference for LLM-facing tools."""

    element_id: str
    role: str | None
    name: str | None
    text: str | None
    bounding_box: tuple[float, float, float, float] | None


@dataclass
class PageSnapshot:
    """Structured representation of the page for LLM context."""

    url: str
    title: str | None
    elements: Sequence[ElementSnapshot]
    raw_text: Sequence[str]


async def capture_snapshot() -> PageSnapshot:
    """Placeholder for CDP snapshot extraction."""

    raise NotImplementedError("CDP snapshot capture not yet implemented")
