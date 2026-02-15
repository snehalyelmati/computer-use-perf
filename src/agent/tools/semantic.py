"""Semantic tool definitions for the agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import CDPSession, Page

from src.agent.browser.session import BrowserSession
from src.agent.context.snapshot import ElementSnapshot, ElementIndex


@dataclass(frozen=True)
class ToolResult:
    """Result of executing a semantic tool."""

    ok: bool
    message: str


@dataclass
class ToolContext:
    """Execution context for semantic tools."""

    page: Page
    cdp_session: CDPSession
    element_index: ElementIndex
    frame_sessions: dict[str, CDPSession] = field(default_factory=dict)
    active_frame_id: str | None = None
    last_tool: str | None = None
    last_element_id: str | None = None


def build_tool_context(
    session: BrowserSession,
    element_index: ElementIndex,
    *,
    active_frame_id: str | None = None,
) -> ToolContext:
    """Build a tool context tied to the browser session lifecycle."""

    return ToolContext(
        page=session.page,
        cdp_session=session.cdp_session,
        element_index=element_index,
        frame_sessions=session.frame_sessions,
        active_frame_id=active_frame_id,
    )

def _resolve_element(element_id: str, context: ToolContext) -> ElementSnapshot | None:
    return context.element_index.elements.get(element_id)

async def _resolve_object_id(backend_node_id: int, session: CDPSession) -> str | None:
    try:
        resolved = await session.send(
            "DOM.resolveNode",
            {"backendNodeId": backend_node_id},
        )
    except Exception:
        return None
    object_id = resolved.get("object", {}).get("objectId")
    if not object_id:
        return None
    return str(object_id)

async def _call_on_node(
    backend_node_id: int,
    session: CDPSession,
    function_body: str,
    args: list[dict[str, Any]] | None = None,
):
    try:
        object_id = await _resolve_object_id(backend_node_id, session)
        if not object_id:
            return None
        return await session.send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": function_body,
                "arguments": args or [],
                "returnByValue": True,
            },
        )
    except Exception:
        return None

async def _viewport_info(backend_node_id: int, session: CDPSession) -> dict[str, Any] | None:
    return await _call_on_node(
        backend_node_id,
        session,
        """
        function () {
            this.scrollIntoView({block: 'center', inline: 'center'});
            const rect = this.getBoundingClientRect();
            const x = rect.left + rect.width / 2;
            const y = rect.top + rect.height / 2;
            const hit = document.elementFromPoint(x, y);
            const onTop = !!(hit && (this.contains(hit) || hit.contains(this)));
            return {x, y, width: rect.width, height: rect.height, onTop};
        }
        """,
    )

async def _dispatch_click(session: CDPSession, x: float, y: float) -> bool:
    try:
        await session.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": x, "y": y, "button": "left"},
        )
        await session.send(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
        )
        await session.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
        )
    except Exception:
        return False
    return True

async def _insert_text(session: CDPSession, text: str) -> bool:
    try:
        await session.send("Input.insertText", {"text": text})
    except Exception:
        return False
    return True

def _frame_tree_paths(frame_tree: dict[str, Any]) -> dict[str, list[int]]:
    paths: dict[str, list[int]] = {}

    def _walk(node: dict[str, Any], path: list[int]) -> None:
        frame = node.get("frame", {})
        frame_id = frame.get("id")
        if frame_id:
            paths[frame_id] = path
        for idx, child in enumerate(node.get("childFrames", []) or []):
            _walk(child, [*path, idx])

    _walk(frame_tree, [])
    return paths

def _playwright_frame_by_path(frame: Any, path: list[int]):
    current = frame
    for index in path:
        children = current.child_frames
        if index >= len(children):
            return None
        current = children[index]
    return current

async def _session_for_element(element: ElementSnapshot | None, context: ToolContext) -> CDPSession:
    if not element or not element.frame_id:
        return context.cdp_session
    if element.frame_id in context.frame_sessions:
        session = context.frame_sessions[element.frame_id]
        try:
            await session.send("Runtime.evaluate", {"expression": "1"})
            return session
        except Exception:
            try:
                await session.detach()
            except Exception:
                pass
            context.frame_sessions.pop(element.frame_id, None)
    if not element.backend_node_id:
        return context.cdp_session
    try:
        tree = await context.cdp_session.send("Page.getFrameTree")
    except Exception:
        tree = {}
    frame_paths = _frame_tree_paths(tree.get("frameTree", {})) if tree else {}
    if element.frame_id in frame_paths:
        path = frame_paths[element.frame_id]
        matched = _playwright_frame_by_path(context.page.main_frame, path)
        if matched is not None:
            session = await context.page.context.new_cdp_session(matched)
            context.frame_sessions[element.frame_id] = session
            return session
    frame_url = element.frame_url
    frame_name = element.frame_name
    for frame in context.page.frames:
        if frame_url and frame.url == frame_url:
            session = await context.page.context.new_cdp_session(frame)
            context.frame_sessions[element.frame_id] = session
            return session
        if frame_name and frame.name == frame_name:
            session = await context.page.context.new_cdp_session(frame)
            context.frame_sessions[element.frame_id] = session
            return session
    return context.cdp_session

def _active_frame_error(element: ElementSnapshot | None, context: ToolContext) -> ToolResult | None:
    if context.active_frame_id and element and element.frame_id != context.active_frame_id:
        return ToolResult(ok=False, message="Element is not in the active frame")
    return None


async def click_element(element_id: str, context: ToolContext) -> ToolResult:
    context.last_tool = "click_element"
    context.last_element_id = element_id
    element = _resolve_element(element_id, context)
    if not element or not element.backend_node_id:
        return ToolResult(ok=False, message=f"Unknown element id: {element_id}")
    frame_error = _active_frame_error(element, context)
    if frame_error:
        return frame_error
    session = await _session_for_element(element, context)
    info = await _viewport_info(element.backend_node_id, session)
    if not info:
        return ToolResult(ok=False, message="Click failed")
    value = info.get("result", {}).get("value") if isinstance(info, dict) else None
    if value and value.get("onTop"):
        if not await _dispatch_click(session, value["x"], value["y"]):
            return ToolResult(ok=False, message="Click failed")
    else:
        result = await _call_on_node(
            element.backend_node_id,
            session,
            """
            function () {
                this.scrollIntoView({block: 'center', inline: 'center'});
                this.click();
                return true;
            }
            """,
        )
        if not result:
            return ToolResult(ok=False, message="Click failed")
    return ToolResult(ok=True, message=f"Clicked {element_id}")


async def type_text(element_id: str, text: str, context: ToolContext) -> ToolResult:
    context.last_tool = "type_text"
    context.last_element_id = element_id
    element = _resolve_element(element_id, context)
    if not element or not element.backend_node_id:
        return ToolResult(ok=False, message=f"Unknown element id: {element_id}")
    frame_error = _active_frame_error(element, context)
    if frame_error:
        return frame_error
    session = await _session_for_element(element, context)
    info = await _viewport_info(element.backend_node_id, session)
    if not info:
        return ToolResult(ok=False, message="Type failed")
    value = info.get("result", {}).get("value") if isinstance(info, dict) else None
    if value and not value.get("onTop"):
        return ToolResult(ok=False, message="Type failed: element obscured")
    if value and not await _dispatch_click(session, value["x"], value["y"]):
        return ToolResult(ok=False, message="Type failed")
    result = await _call_on_node(
        element.backend_node_id,
        session,
        """
        function () {
            this.scrollIntoView({block: 'center', inline: 'center'});
            if (this.select) {
                this.select();
            }
            return true;
        }
        """,
    )
    if not result:
        return ToolResult(ok=False, message="Type failed")
    if not await _insert_text(session, text):
        return ToolResult(ok=False, message="Type failed")
    return ToolResult(ok=True, message=f"Typed into {element_id}")


async def drag_and_drop(source_id: str, target_id: str, context: ToolContext) -> ToolResult:
    context.last_tool = "drag_and_drop"
    context.last_element_id = None
    source = _resolve_element(source_id, context)
    target = _resolve_element(target_id, context)
    if not source or not source.backend_node_id:
        return ToolResult(ok=False, message=f"Unknown source id: {source_id}")
    if not target or not target.backend_node_id:
        return ToolResult(ok=False, message=f"Unknown target id: {target_id}")
    if context.active_frame_id and (
        source.frame_id != context.active_frame_id or target.frame_id != context.active_frame_id
    ):
        return ToolResult(ok=False, message="Drag elements are not in the active frame")
    if source.frame_id != target.frame_id:
        return ToolResult(ok=False, message="Drag elements are in different frames")
    session = await _session_for_element(source, context)
    source_info = await _viewport_info(source.backend_node_id, session)
    if not source_info:
        return ToolResult(ok=False, message="Drag failed")
    target_info = await _viewport_info(target.backend_node_id, session)
    if not target_info:
        return ToolResult(ok=False, message="Drag failed")
    source_value = source_info.get("result", {}).get("value")
    target_value = target_info.get("result", {}).get("value")
    if not source_value or not target_value:
        return ToolResult(ok=False, message="Drag failed")
    if not source_value.get("onTop") or not target_value.get("onTop"):
        return ToolResult(ok=False, message="Drag failed: element obscured")
    source_x = source_value["x"]
    source_y = source_value["y"]
    target_x = target_value["x"]
    target_y = target_value["y"]
    try:
        await session.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": source_x, "y": source_y, "button": "left"},
        )
        await session.send(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": source_x, "y": source_y, "button": "left", "clickCount": 1},
        )
        await session.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": target_x, "y": target_y, "button": "left", "buttons": 1},
        )
        await session.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": target_x, "y": target_y, "button": "left", "clickCount": 1},
        )
    except Exception as exc:  # pragma: no cover - runtime safety
        return ToolResult(ok=False, message=f"Drag failed: {exc}")
    return ToolResult(ok=True, message=f"Dragged {source_id} -> {target_id}")


async def select_all(context: ToolContext) -> ToolResult:
    context.last_tool = "select_all"
    context.last_element_id = None
    try:
        await context.page.keyboard.press("ControlOrMeta+A")
    except Exception as exc:  # pragma: no cover - runtime safety
        return ToolResult(ok=False, message=f"Select all failed: {exc}")
    return ToolResult(ok=True, message="Selected all")


async def copy_selection(context: ToolContext) -> ToolResult:
    context.last_tool = "copy_selection"
    context.last_element_id = None
    try:
        await context.page.keyboard.press("ControlOrMeta+C")
    except Exception as exc:  # pragma: no cover - runtime safety
        return ToolResult(ok=False, message=f"Copy failed: {exc}")
    return ToolResult(ok=True, message="Copied selection")


async def paste(context: ToolContext) -> ToolResult:
    context.last_tool = "paste"
    context.last_element_id = None
    try:
        await context.page.keyboard.press("ControlOrMeta+V")
    except Exception as exc:  # pragma: no cover - runtime safety
        return ToolResult(ok=False, message=f"Paste failed: {exc}")
    return ToolResult(ok=True, message="Pasted")


async def read_element_text(element_id: str, context: ToolContext) -> ToolResult:
    context.last_tool = "read_element_text"
    context.last_element_id = element_id
    element = _resolve_element(element_id, context)
    if not element or not element.backend_node_id:
        return ToolResult(ok=False, message=f"Unknown element id: {element_id}")
    frame_error = _active_frame_error(element, context)
    if frame_error:
        return frame_error
    session = await _session_for_element(element, context)
    info = await _viewport_info(element.backend_node_id, session)
    if not info:
        return ToolResult(ok=False, message="Read text failed")
    value = info.get("result", {}).get("value") if isinstance(info, dict) else None
    if value and not value.get("onTop"):
        return ToolResult(ok=False, message="Read text failed: element obscured")
    result = await _call_on_node(
        element.backend_node_id,
        session,
        """
        function () {
            return this.innerText || this.textContent || '';
        }
        """,
    )
    if not result:
        return ToolResult(ok=False, message="Read text failed")
    value = result.get("result", {}).get("value")
    return ToolResult(ok=True, message=value or "")


async def switch_to_iframe(iframe_id: str, context: ToolContext) -> ToolResult:
    context.last_tool = "switch_to_iframe"
    context.last_element_id = iframe_id
    element = _resolve_element(iframe_id, context)
    if not element:
        return ToolResult(ok=False, message=f"Unknown iframe id: {iframe_id}")
    if not element.frame_id:
        return ToolResult(ok=False, message="Iframe has no frame id")
    context.active_frame_id = element.frame_id
    return ToolResult(ok=True, message=f"Switched to iframe {iframe_id}")


async def switch_to_main_frame(context: ToolContext) -> ToolResult:
    context.last_tool = "switch_to_main_frame"
    context.last_element_id = None
    context.active_frame_id = None
    return ToolResult(ok=True, message="Switched to main frame")


async def navigate_to(url: str, context: ToolContext) -> ToolResult:
    context.last_tool = "navigate_to"
    context.last_element_id = None
    try:
        await context.page.goto(url)
    except Exception as exc:  # pragma: no cover - runtime safety
        return ToolResult(ok=False, message=f"Navigation failed: {exc}")
    return ToolResult(ok=True, message=f"Navigated to {url}")


async def take_screenshot(context: ToolContext) -> ToolResult:
    context.last_tool = "take_screenshot"
    context.last_element_id = None
    try:
        await context.page.screenshot(full_page=True)
    except Exception as exc:  # pragma: no cover - runtime safety
        return ToolResult(ok=False, message=f"Screenshot failed: {exc}")
    return ToolResult(ok=True, message="Screenshot captured")


async def execute_js(code: str, context: ToolContext) -> ToolResult:
    context.last_tool = "execute_js"
    context.last_element_id = None
    try:
        await context.page.evaluate(code)
    except Exception as exc:  # pragma: no cover - runtime safety
        return ToolResult(ok=False, message=f"Execute JS failed: {exc}")
    return ToolResult(ok=True, message="Executed script")


async def press_key_combination(keys: list[str], context: ToolContext) -> ToolResult:
    context.last_tool = "press_key_combination"
    context.last_element_id = None
    try:
        await context.page.keyboard.press("+".join(keys))
    except Exception as exc:  # pragma: no cover - runtime safety
        return ToolResult(ok=False, message=f"Key press failed: {exc}")
    return ToolResult(ok=True, message=f"Pressed {'+'.join(keys)}")
