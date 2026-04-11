"""Verify the tool-return compaction history processor."""

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from src.agent.core.history import make_tool_return_compactor
from src.agent.models.actions import ToolExecutionResult


def _make_round(
    tool_name: str, ok: bool, message: str, call_id: str | None = None,
) -> tuple[ModelResponse, ModelRequest]:
    """Create a (ModelResponse, ModelRequest) pair representing one tool-call round."""
    cid = call_id or f"call_{tool_name}"
    response = ModelResponse(
        parts=[ToolCallPart(tool_name=tool_name, args="{}", tool_call_id=cid)],
    )
    request = ModelRequest(
        parts=[ToolReturnPart(
            tool_name=tool_name,
            content=ToolExecutionResult(ok=ok, message=message),
            tool_call_id=cid,
        )],
    )
    return response, request


def test_basic_compaction():
    """With 5 tool-return rounds and keep_recent=2, first 3 should be compacted."""
    compact = make_tool_return_compactor(keep_recent=2)

    messages: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content="Do something")])]

    # Round 1: ok, verbose
    r1_resp, r1_req = _make_round("click_element", True, "Clicked el_abc\nDOM changes:\n  + lots of text here " * 10, "c1")
    messages.extend([r1_resp, r1_req])

    # Round 2: error
    r2_resp, r2_req = _make_round("type_text", False, "Type failed: element not focusable", "c2")
    messages.extend([r2_resp, r2_req])

    # Round 3: ok, verbose
    r3_resp, r3_req = _make_round("click_element", True, "Clicked el_def\nDOM changes:\n  + more data " * 20, "c3")
    messages.extend([r3_resp, r3_req])

    # Round 4 (recent): ok, verbose
    r4_resp, r4_req = _make_round("scroll", True, "Scrolled dx=0 dy=600\nDOM changes:\n  + content", "c4")
    messages.extend([r4_resp, r4_req])

    # Round 5 (recent): ok, verbose
    r5_resp, r5_req = _make_round("click_element", True, "Clicked el_submit\nDOM changes:\n  + Accepted!", "c5")
    messages.extend([r5_resp, r5_req])

    result = compact(messages)

    # Rounds 1, 2, 3 compacted; 4, 5 intact
    assert result[2].parts[0].content == "ok", f"Round 1 should be 'ok', got: {result[2].parts[0].content!r}"
    assert result[4].parts[0].content.startswith("error: "), f"Round 2 should start with 'error: ', got: {result[4].parts[0].content!r}"
    assert "not focusable" in result[4].parts[0].content, "Error reason should be preserved"
    assert result[6].parts[0].content == "ok", f"Round 3 should be 'ok', got: {result[6].parts[0].content!r}"

    # Recent rounds untouched
    assert hasattr(result[8].parts[0].content, "ok"), "Round 4 should retain ToolExecutionResult"
    assert hasattr(result[10].parts[0].content, "ok"), "Round 5 should retain ToolExecutionResult"

    print("PASS: basic_compaction")


def test_no_op_when_fewer_rounds():
    """If fewer rounds than keep_recent, nothing is compacted."""
    compact = make_tool_return_compactor(keep_recent=5)

    messages: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content="hello")])]
    r1_resp, r1_req = _make_round("click", True, "Clicked el_abc\nDOM changes:\n  big data", "c1")
    messages.extend([r1_resp, r1_req])

    result = compact(messages)
    assert hasattr(result[2].parts[0].content, "ok"), "Should not be compacted"
    print("PASS: no_op_when_fewer_rounds")


def test_idempotency():
    """Running the compactor twice produces the same result."""
    compact = make_tool_return_compactor(keep_recent=1)

    messages: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content="hello")])]
    r1_resp, r1_req = _make_round("a", True, "Verbose result " * 50, "c1")
    r2_resp, r2_req = _make_round("b", True, "Another verbose result " * 50, "c2")
    messages.extend([r1_resp, r1_req, r2_resp, r2_req])

    compact(messages)
    assert messages[2].parts[0].content == "ok"

    # Run again — should be idempotent
    compact(messages)
    assert messages[2].parts[0].content == "ok"

    print("PASS: idempotency")


def test_disabled_when_zero():
    """keep_recent=0 disables compaction entirely."""
    compact = make_tool_return_compactor(keep_recent=0)

    messages: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content="hello")])]
    r1_resp, r1_req = _make_round("a", True, "Verbose " * 100, "c1")
    r2_resp, r2_req = _make_round("b", True, "Also verbose " * 100, "c2")
    messages.extend([r1_resp, r1_req, r2_resp, r2_req])

    result = compact(messages)
    assert hasattr(result[2].parts[0].content, "ok"), "Should not be compacted when disabled"
    assert hasattr(result[4].parts[0].content, "ok"), "Should not be compacted when disabled"

    print("PASS: disabled_when_zero")


def test_error_truncation():
    """Long error messages are truncated to 120 chars."""
    compact = make_tool_return_compactor(keep_recent=1)

    long_error = "x" * 500
    messages: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content="hello")])]
    r1_resp, r1_req = _make_round("a", False, long_error, "c1")
    r2_resp, r2_req = _make_round("b", True, "ok result", "c2")
    messages.extend([r1_resp, r1_req, r2_resp, r2_req])

    compact(messages)
    content = messages[2].parts[0].content
    assert content.startswith("error: "), f"Should start with 'error: ', got: {content!r}"
    assert len(content) <= 128, f"Should be truncated, got len={len(content)}"

    print("PASS: error_truncation")


def test_short_strings_kept():
    """Short string content (PydanticAI internals) is kept as-is."""
    compact = make_tool_return_compactor(keep_recent=1)

    messages: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content="hello")])]
    # Simulate a PydanticAI internal short string return
    r1_resp = ModelResponse(
        parts=[ToolCallPart(tool_name="final_result", args="{}", tool_call_id="c1")],
    )
    r1_req = ModelRequest(
        parts=[ToolReturnPart(tool_name="final_result", content="Final result processed.", tool_call_id="c1")],
    )
    r2_resp, r2_req = _make_round("b", True, "ok result", "c2")
    messages.extend([r1_resp, r1_req, r2_resp, r2_req])

    compact(messages)
    assert messages[2].parts[0].content == "Final result processed.", "Short strings should be kept"

    print("PASS: short_strings_kept")


if __name__ == "__main__":
    test_basic_compaction()
    test_no_op_when_fewer_rounds()
    test_idempotency()
    test_disabled_when_zero()
    test_error_truncation()
    test_short_strings_kept()
    print("\nAll tests passed!")
