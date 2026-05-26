from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.core.agent import ToolCallTracker, _repeat_guard_result


def test_same_no_change_tool_signature_blocks_after_two_attempts() -> None:
    tracker = ToolCallTracker()
    signature = ("press_key_combination", ("Enter",))
    facts = {"state_changed": False, "dom_changed": False}

    tracker.record(
        True,
        tool_name="press_key_combination",
        signature=signature,
        message="Pressed Enter\nNo DOM changes.",
        facts=facts,
    )
    tracker.record(
        True,
        tool_name="press_key_combination",
        signature=signature,
        message="Pressed Enter\nNo DOM changes.",
        facts=facts,
    )

    message = tracker.repeated_no_change_message("press_key_combination", signature)

    assert message is not None
    assert "Blocked repeated press_key_combination" in message


def test_repeat_guard_stays_blocked_after_block_record() -> None:
    tracker = ToolCallTracker()
    signature = ("press_key_combination", ("Enter",))
    facts = {"state_changed": False, "dom_changed": False}

    tracker.record(
        True,
        tool_name="press_key_combination",
        signature=signature,
        message="Pressed Enter\nNo DOM changes.",
        facts=facts,
    )
    tracker.record(
        True,
        tool_name="press_key_combination",
        signature=signature,
        message="Pressed Enter\nNo DOM changes.",
        facts=facts,
    )

    first_block = _repeat_guard_result(tracker, "press_key_combination", signature)
    second_block = _repeat_guard_result(tracker, "press_key_combination", signature)

    assert first_block is not None
    assert second_block is not None
    assert first_block.ok is False
    assert second_block.ok is False
    assert tracker.blocked_repeats == 2


def test_changed_tool_signature_does_not_block() -> None:
    tracker = ToolCallTracker()
    signature = ("type_text", "el_1", "value")
    tracker.record(
        True,
        tool_name="type_text",
        element_id="el_1",
        signature=signature,
        message='Typed into el_1. Current value: "value". Previous value: ""',
        facts={"value_changed": True},
    )
    tracker.record(
        True,
        tool_name="type_text",
        element_id="el_1",
        signature=signature,
        message='Typed into el_1. Current value: "value". Previous value: "value"',
        facts={"value_changed": False},
    )

    assert tracker.repeated_no_change_message("type_text", signature) is None


def test_intervening_state_change_resets_no_change_guard() -> None:
    tracker = ToolCallTracker()
    signature = ("press_key_combination", ("Enter",))

    tracker.record(
        True,
        tool_name="press_key_combination",
        signature=signature,
        message="Pressed Enter\nNo DOM changes.",
        facts={"state_changed": False},
    )
    tracker.record(
        True,
        tool_name="type_text",
        signature=("type_text", "el_1", "abc"),
        message='Typed into el_1. Current value: "abc". Previous value: ""',
        facts={"value_changed": True},
    )
    tracker.record(
        True,
        tool_name="press_key_combination",
        signature=signature,
        message="Pressed Enter\nNo DOM changes.",
        facts={"state_changed": False},
    )

    assert tracker.repeated_no_change_message("press_key_combination", signature) is None


def test_different_no_change_messages_do_not_block_readback() -> None:
    tracker = ToolCallTracker()
    signature = ("read_live_text", "")
    facts = {"state_changed": False}

    tracker.record(True, tool_name="read_live_text", signature=signature, message="output: first", facts=facts)
    tracker.record(True, tool_name="read_live_text", signature=signature, message="output: second", facts=facts)

    assert tracker.repeated_no_change_message("read_live_text", signature) is None
