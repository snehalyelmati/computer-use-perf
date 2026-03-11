"""History processor that compacts old tool-return content to save tokens."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ToolReturnPart,
)

logger = logging.getLogger(__name__)

_ERROR_MAX_LEN = 120


def make_tool_return_compactor(
    keep_recent: int = 3,
) -> Callable[[list[ModelMessage]], list[ModelMessage]]:
    """Create a history processor that compacts old tool-return content.

    Within a single agent run, keeps the last *keep_recent* tool-return
    round-trips fully intact and replaces older ``ToolReturnPart.content``
    with ``"ok"`` or ``"error: <reason>"``.

    Assistant tool-call messages (``ModelResponse``) are never modified.
    """

    def _compact(messages: list[ModelMessage]) -> list[ModelMessage]:
        if keep_recent <= 0:
            return messages

        # Indices of ModelRequests that carry at least one ToolReturnPart.
        tool_return_indices: list[int] = []
        for i, msg in enumerate(messages):
            if isinstance(msg, ModelRequest) and any(
                isinstance(p, ToolReturnPart) for p in msg.parts
            ):
                tool_return_indices.append(i)

        if len(tool_return_indices) <= keep_recent:
            return messages  # nothing to compact

        to_compact = tool_return_indices[:-keep_recent]
        compacted = 0

        for idx in to_compact:
            msg = messages[idx]
            assert isinstance(msg, ModelRequest)
            for part in msg.parts:
                if isinstance(part, ToolReturnPart):
                    summary = _summarize_content(part.content)
                    if summary != part.content:
                        part.content = summary
                        compacted += 1

        if compacted:
            logger.debug(
                "Compacted %d tool returns (%d of %d rounds kept intact)",
                compacted,
                keep_recent,
                len(tool_return_indices),
            )

        return messages

    return _compact


def _summarize_content(content: Any) -> str:
    """Produce a compact summary of tool-return content."""

    # Already compact (idempotency).
    if isinstance(content, str) and (content == "ok" or content.startswith("error: ")):
        return content

    # Pydantic BaseModel with ok/message (ToolExecutionResult).
    if hasattr(content, "ok") and hasattr(content, "message"):
        if content.ok:
            return "ok"
        return f"error: {str(content.message)[:_ERROR_MAX_LEN]}"

    # Dict with "ok" key (serialised ToolExecutionResult).
    if isinstance(content, dict) and "ok" in content:
        if content["ok"]:
            return "ok"
        return f"error: {str(content.get('message', 'unknown'))[:_ERROR_MAX_LEN]}"

    # Short string — already cheap, keep as-is.
    if isinstance(content, str) and len(content) <= 60:
        return content

    # Long string without ok/error structure — truncate rather than lose info.
    if isinstance(content, str):
        return content[:60] + "..."

    # Unknown type fallback.
    return "ok"
