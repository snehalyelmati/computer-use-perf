"""Pydantic models for tool-calling and structured outputs."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Action(BaseModel):
    """Single semantic action emitted by the LLM."""

    name: str = Field(..., description="Semantic tool name")
    element_id: str | None = Field(None, description="Stable element id")
    text: str | None = Field(None, description="Text payload for typing")
    source_id: str | None = Field(None, description="Drag source element id")
    target_id: str | None = Field(None, description="Drag target element id")
    keys: list[str] | None = Field(None, description="Key combination")
