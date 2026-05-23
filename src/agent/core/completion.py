"""Completion and recovery policy for browser-agent steps."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


ValidationStatus = Literal["unknown", "success", "failure", "neutral"]
CompletionAction = Literal[
    "continue",
    "recover_once",
    "stop_success",
    "stop_failure",
    "blocked",
]


@dataclass(frozen=True)
class ValidationSignal:
    """External validation signal from an authoritative harness."""

    source: str
    status: ValidationStatus = "unknown"
    terminal: bool = False
    reward: float | None = None
    evidence: tuple[str, ...] = ()
    reason: str | None = None

    @property
    def positive(self) -> bool:
        return self.status == "success" or (
            self.reward is not None and self.reward > 0
        )

    @property
    def negative(self) -> bool:
        return self.status == "failure" or (
            self.reward is not None and self.reward <= 0
        )

    @property
    def zero_non_terminal(self) -> bool:
        return (
            not self.terminal
            and self.reward is not None
            and self.reward <= 0
            and self.status != "success"
        )


@dataclass(frozen=True)
class CompletionDecision:
    """Policy decision for one completed internal step."""

    action: CompletionAction
    stop_reason: str | None = None
    reason: str = ""
    accepted_done: bool = False
    recovery_directive: str | None = None
    evidence: tuple[str, ...] = ()

    @property
    def terminal(self) -> bool:
        return self.action in {"stop_success", "stop_failure", "blocked"}


@dataclass(frozen=True)
class CompletionInputs:
    """Facts used by the pure completion policy."""

    validation: ValidationSignal | None = None
    model_done: bool = False
    completion_evidence: str | None = None
    successful_tools: int = 0
    failed_tools: int = 0
    progress_fingerprint: str | None = None
    previous_progress_fingerprint: str | None = None
    same_worker_goal: bool = False
    same_tool_family: bool = False
    dom_changed: bool = False
    value_changed: bool = False
    url_changed: bool = False
    tool_limit_hit: bool = False
    consecutive_tool_limit_steps: int = 0
    recovery_attempted: bool = False
    no_progress_steps: int = 0
    tool_results: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def observable_evidence(self) -> bool:
        return self.dom_changed or self.value_changed or self.url_changed

    @property
    def same_progress_fingerprint(self) -> bool:
        return (
            self.progress_fingerprint is not None
            and self.previous_progress_fingerprint is not None
            and self.progress_fingerprint == self.previous_progress_fingerprint
        )


def decide_completion(inputs: CompletionInputs) -> CompletionDecision:
    """Decide whether to continue, recover once, stop, or block."""

    validation = inputs.validation
    if validation and validation.terminal:
        evidence = validation.evidence
        if validation.positive:
            return CompletionDecision(
                action="stop_success",
                stop_reason="done",
                reason=validation.reason or "external validation reported success",
                accepted_done=True,
                evidence=evidence,
            )
        return CompletionDecision(
            action="stop_failure",
            stop_reason="validation_terminal_failure",
            reason=validation.reason or "external validation reported terminal failure",
            evidence=evidence,
        )

    if inputs.consecutive_tool_limit_steps >= 2:
        return CompletionDecision(
            action="blocked",
            stop_reason="blocked_tool_limit",
            reason="consecutive steps hit the worker tool-call limit",
        )

    evidence_text = (inputs.completion_evidence or "").strip()
    has_model_evidence = bool(evidence_text)
    if inputs.model_done:
        if validation and validation.source == "browsergym" and not validation.terminal:
            return _recover_or_block(
                inputs,
                blocked_reason="blocked_done_without_validation",
                recovery_reason=(
                    "model proposed done=true before BrowserGym reported terminal success"
                ),
            )
        if has_model_evidence and (
            inputs.successful_tools > 0 or inputs.observable_evidence
        ):
            return CompletionDecision(
                action="stop_success",
                stop_reason="done",
                reason="model completion was backed by observable evidence",
                accepted_done=True,
                evidence=(evidence_text,),
            )
        return _recover_or_block(
            inputs,
            blocked_reason="blocked_done_without_evidence",
            recovery_reason=(
                "model proposed done=true without successful tools or observable completion evidence"
            ),
        )

    no_progress = (
        inputs.no_progress_steps > 0
        or inputs.same_progress_fingerprint
        or (validation.zero_non_terminal if validation else False)
    )
    repeated_strategy = inputs.same_worker_goal or inputs.same_tool_family
    if no_progress and repeated_strategy and not inputs.observable_evidence:
        return _recover_or_block(
            inputs,
            blocked_reason="blocked_no_progress",
            recovery_reason="same page state and same strategy produced no observable progress",
        )

    if inputs.tool_limit_hit and inputs.recovery_attempted and not inputs.observable_evidence:
        return CompletionDecision(
            action="blocked",
            stop_reason="blocked_tool_limit",
            reason="recovery attempt hit the tool-call limit without new evidence",
        )

    return CompletionDecision(action="continue", reason="step can continue")


def _recover_or_block(
    inputs: CompletionInputs,
    *,
    blocked_reason: str,
    recovery_reason: str,
) -> CompletionDecision:
    if inputs.recovery_attempted:
        return CompletionDecision(
            action="blocked",
            stop_reason=blocked_reason,
            reason=f"recovery attempt did not produce new evidence: {recovery_reason}",
        )
    return CompletionDecision(
        action="recover_once",
        reason=recovery_reason,
        recovery_directive=(
            "Make one materially different attempt. Do not repeat the same tool family, "
            "target element, or done=true claim unless external validation or observable "
            "page evidence confirms completion."
        ),
    )
