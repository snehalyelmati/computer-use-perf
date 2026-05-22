from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.core.agent import ToolCallTracker


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
