from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, Field, field_validator


class OracleResponse(BaseModel):
    status: Literal["OK", "WARN", "REDIRECT", "OVERRIDE", "WRONG_GOAL"] = "OK"
    reason: str | None = None
    correct_goal: str | None = None
    next_directive: str | None = None
    avoid: str | None = None
    evidence: str | None = None
    explore: str | None = None

    _STATUS_ALIASES: ClassVar[dict[str, str]] = {
        "SUBMISSION_NO_EFFECT": "WARN", "WAIT": "OK", "STUCK": "OVERRIDE",
        "RETRY": "WARN", "ERROR": "WARN", "FAIL": "WARN", "FAILURE": "WARN",
        "PROGRESS": "OK", "SUCCESS": "OK", "DONE": "OK",
    }
    _VALID_STATUSES: ClassVar[set[str]] = {"OK", "WARN", "REDIRECT", "OVERRIDE", "WRONG_GOAL"}

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, v):
        if isinstance(v, str):
            v = v.upper().strip()
            if v in cls._VALID_STATUSES:
                return v
            return cls._STATUS_ALIASES.get(v, "WARN")
        return v

    @field_validator("avoid", mode="before")
    @classmethod
    def coerce_avoid_list(cls, v):
        if isinstance(v, list):
            return ", ".join(str(item) for item in v)
        return v


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
