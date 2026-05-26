"""Pydantic models for tool-calling and structured outputs."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


_ELEMENT_ID_RE = re.compile(r"\bel_[a-f0-9]{12}(?:[-_]\d+)?\b")
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+")


class Action(BaseModel):
    """Single semantic action emitted by the LLM."""

    name: str = Field(..., description="Semantic tool name")
    element_id: str | None = Field(None, description="Stable element id")
    text: str | None = Field(None, description="Text payload for typing")
    source_id: str | None = Field(None, description="Drag source element id")
    target_id: str | None = Field(None, description="Drag target element id")
    keys: list[str] | None = Field(None, description="Key combination")


class ToolExecutionResult(BaseModel):
    """Structured result returned by a semantic tool."""

    ok: bool = Field(..., description="Whether the tool succeeded")
    message: str = Field(..., description="Human-readable tool outcome")
    facts: dict[str, Any] = Field(
        default_factory=dict,
        description="Compact machine-readable facts about observable state changes",
    )


class StepOutput(BaseModel):
    """Structured output returned by the agent after a step."""

    done: bool = Field(
        False,
        description=(
            "Set true when the delegated worker goal for this step is complete. "
            "This does not necessarily mean the overall run goal is complete."
        ),
    )
    summary: str = Field(..., description="Concise summary of what happened in this step")


class UnifiedStepOutput(BaseModel):
    """Structured output returned by the unified agent after a step."""

    done: bool = Field(
        False,
        description="Set true ONLY when the overall run goal is fully complete.",
    )
    step_goal: str = Field(
        ...,
        description="Short sub-goal you attempted this step (used for trace/Oracle).",
    )
    summary: str = Field(
        ...,
        description="Concise summary of what happened in this step.",
    )
    rationale: str = Field(
        "",
        description="Brief why these actions were chosen.",
    )
    completion_evidence: str | None = Field(
        None,
        description=(
            "Observable evidence that the overall goal is complete; required when done=true."
        ),
    )


class OrchestratorDecision(BaseModel):
    """Structured output returned by the orchestrator to delegate work."""

    done: bool = Field(False, description="Set true when the overall goal is complete")
    worker: Literal["browser"] = Field("browser", description="Which worker agent should act next")
    worker_goal: str = Field(..., description="Concrete goal for the worker to execute next")
    rationale: str | None = Field(
        None,
        description="Optional brief rationale for why this next goal was chosen",
    )
    completion_evidence: str | None = Field(
        None,
        description=(
            "Observable evidence that the overall goal is complete; required when done=true."
        ),
    )


class OracleAdvice(BaseModel):
    """Structured output from the Oracle advisor."""

    all_clear: bool = Field(False, description="Set true when progress is healthy and no intervention is needed")
    diagnosis: str = Field(..., description="Why the agent is stuck — identify the pattern of failure")
    recommendation: str = Field(..., description="What the orchestrator should do differently")
    avoid: list[str] = Field(default_factory=list, description="Specific approaches or elements to stop trying")


class SnapshotFilterOutput(BaseModel):
    """Structured output returned by the snapshot filter stage."""

    useful_text_lines: list[str] = Field(
        default_factory=list,
        description="High-signal page text lines (one line per item, no numbering).",
    )
    priority_element_ids: list[str] = Field(
        default_factory=list,
        description="Shortlist of stable element ids likely to matter for the goal.",
    )
    notes: str | None = Field(
        None,
        description="Optional short notes about what matters on the page right now.",
    )

    @field_validator("useful_text_lines", mode="before")
    @classmethod
    def _coerce_useful_text_lines(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [
                _LIST_MARKER_RE.sub("", line).strip()
                for line in value.splitlines()
                if line.strip()
            ]
        return value

    @field_validator("priority_element_ids", mode="before")
    @classmethod
    def _coerce_priority_element_ids(cls, value: Any) -> Any:
        if isinstance(value, str):
            return list(dict.fromkeys(_ELEMENT_ID_RE.findall(value)))
        return value
