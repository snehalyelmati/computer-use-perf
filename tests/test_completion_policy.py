from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.core.completion import CompletionInputs, ValidationSignal, decide_completion


def test_external_terminal_success_stops() -> None:
    decision = decide_completion(
        CompletionInputs(
            validation=ValidationSignal(
                source="browsergym",
                status="success",
                terminal=True,
                reward=1.0,
                evidence=("reward=1",),
            )
        )
    )

    assert decision.action == "stop_success"
    assert decision.stop_reason == "done"
    assert decision.accepted_done is True


def test_external_terminal_failure_stops() -> None:
    decision = decide_completion(
        CompletionInputs(
            validation=ValidationSignal(
                source="browsergym",
                status="failure",
                terminal=True,
                reward=0.0,
            )
        )
    )

    assert decision.action == "stop_failure"
    assert decision.stop_reason == "validation_terminal_failure"


def test_non_terminal_zero_validation_gets_one_recovery() -> None:
    decision = decide_completion(
        CompletionInputs(
            validation=ValidationSignal(
                source="browsergym",
                status="neutral",
                terminal=False,
                reward=0.0,
            ),
            same_worker_goal=True,
        )
    )

    assert decision.action == "recover_once"


def test_repeated_no_progress_after_recovery_blocks() -> None:
    decision = decide_completion(
        CompletionInputs(
            same_worker_goal=True,
            no_progress_steps=1,
            recovery_attempted=True,
        )
    )

    assert decision.action == "blocked"
    assert decision.stop_reason == "blocked_no_progress"


def test_done_without_evidence_is_rejected() -> None:
    decision = decide_completion(CompletionInputs(model_done=True, successful_tools=0))

    assert decision.action == "recover_once"
    assert decision.accepted_done is False


def test_done_with_evidence_and_successful_tool_stops() -> None:
    decision = decide_completion(
        CompletionInputs(
            model_done=True,
            completion_evidence="Page shows submitted confirmation.",
            successful_tools=1,
        )
    )

    assert decision.action == "stop_success"
    assert decision.stop_reason == "done"


def test_browsergym_nonterminal_done_with_evidence_recovers() -> None:
    decision = decide_completion(
        CompletionInputs(
            validation=ValidationSignal(
                source="browsergym",
                status="neutral",
                terminal=False,
                reward=0.0,
            ),
            model_done=True,
            completion_evidence="The form appears submitted.",
            successful_tools=1,
            value_changed=True,
        )
    )

    assert decision.action == "recover_once"
    assert decision.accepted_done is False


def test_consecutive_tool_limit_steps_block() -> None:
    decision = decide_completion(CompletionInputs(consecutive_tool_limit_steps=2))

    assert decision.action == "blocked"
    assert decision.stop_reason == "blocked_tool_limit"
