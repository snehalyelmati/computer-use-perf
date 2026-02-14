from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class OracleResponse(BaseModel):
    status: Literal["OK", "WARN", "OVERRIDE", "WRONG_GOAL"] = "OK"
    reason: str | None = None
    next_directive: str | None = None
    avoid: str | None = None

    _STATUS_ALIASES: ClassVar[dict[str, str]] = {
        "SUBMISSION_NO_EFFECT": "WARN",
        "WAIT": "OK",
        "STUCK": "OVERRIDE",
        "RETRY": "WARN",
        "ERROR": "WARN",
        "FAIL": "WARN",
        "FAILURE": "WARN",
        "PROGRESS": "OK",
        "SUCCESS": "OK",
        "DONE": "OK",
        # Backwards compatibility / common variants.
        "REDIRECT": "WARN",
        "WRONG_OBJECTIVE": "WRONG_GOAL",
        "RESET": "WRONG_GOAL",
        "REASSESS": "WRONG_GOAL",
    }
    _VALID_STATUSES: ClassVar[set[str]] = {
        "OK",
        "WARN",
        "OVERRIDE",
        "WRONG_GOAL",
    }

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
    # Page-specific objective derived from page text/state.
    objective: str = Field(min_length=1)
    # Optional extra plan text (kept for debugging/backward compatibility).
    task: str | None = None
    data: str | None = None
    progress: str | None = None
    next: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _coerce_objective(cls, v: Any):
        # Backwards compat: older prompts return {"goal": ...}; treat it as objective.
        if isinstance(v, dict) and not v.get("objective") and v.get("goal"):
            v = dict(v)
            v["objective"] = v.get("goal")
        return v

    @field_validator("data", mode="before")
    @classmethod
    def _coerce_data(cls, v: Any):
        if v is None:
            return None
        if isinstance(v, dict):
            parts: list[str] = []
            for k, val in v.items():
                if val is None:
                    continue
                sval = str(val).strip()
                if not sval:
                    continue
                parts.append(f"{k}={sval}")
            return " ".join(parts) if parts else None
        if isinstance(v, (list, tuple)):
            s = " ".join(str(item).strip() for item in v if str(item).strip())
            return s or None
        if not isinstance(v, str):
            return str(v)
        s = v.strip()
        return s or None


class ActionItem(BaseModel):
    a: str = Field(min_length=1)
    n: int | None = None
    v: str | None = None
    t: int | None = None


class ActionResponse(BaseModel):
    actions: list[ActionItem] = Field(default_factory=list)


class LearningResponse(BaseModel):
    learning: str = Field(min_length=1)
