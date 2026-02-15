"""Pydantic models for tool-calling and structured outputs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


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


class StepOutput(BaseModel):
    """Structured output returned by the agent after a step."""

    done: bool = Field(False, description="Set true when the overall goal is complete")
    summary: str = Field(..., description="Concise summary of what happened in this step")
    next_goal: str | None = Field(
        None,
        description="Optional next sub-goal the agent will pursue on the next step",
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
