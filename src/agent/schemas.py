from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class OracleResponse(BaseModel):
    status: Literal["OK", "WARN", "REDIRECT", "OVERRIDE", "WRONG_GOAL"] = "OK"
    reason: str | None = None
    correct_goal: str | None = None
    next_directive: str | None = None
    avoid: str | None = None
    evidence: str | None = None
    explore: str | None = None


class OverviewResponse(BaseModel):
    goal: str = Field(min_length=1)
    task: str | None = None
    data: str | None = None
    progress: str | None = None
    next: str = Field(min_length=1)


class ActionItem(BaseModel):
    a: str = Field(min_length=1)
    n: int | None = None
    v: str | None = None
    t: int | None = None


class ActionResponse(BaseModel):
    actions: list[ActionItem] = Field(default_factory=list)


class LearningResponse(BaseModel):
    learning: str = Field(min_length=1)
