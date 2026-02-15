"""Semantic tool definitions for the agent."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolResult:
    """Result of executing a semantic tool."""

    ok: bool
    message: str


async def click_element(element_id: str) -> ToolResult:
    raise NotImplementedError


async def type_text(element_id: str, text: str) -> ToolResult:
    raise NotImplementedError


async def drag_and_drop(source_id: str, target_id: str) -> ToolResult:
    raise NotImplementedError


async def select_all() -> ToolResult:
    raise NotImplementedError


async def copy_selection() -> ToolResult:
    raise NotImplementedError


async def paste() -> ToolResult:
    raise NotImplementedError


async def read_element_text(element_id: str) -> ToolResult:
    raise NotImplementedError


async def switch_to_iframe(iframe_id: str) -> ToolResult:
    raise NotImplementedError


async def switch_to_main_frame() -> ToolResult:
    raise NotImplementedError


async def navigate_to(url: str) -> ToolResult:
    raise NotImplementedError


async def take_screenshot() -> ToolResult:
    raise NotImplementedError


async def execute_js(code: str) -> ToolResult:
    raise NotImplementedError


async def press_key_combination(keys: list[str]) -> ToolResult:
    raise NotImplementedError
