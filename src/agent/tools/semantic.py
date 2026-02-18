"""Semantic tool definitions for the agent."""

from __future__ import annotations

import asyncio
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

_DEFAULT_SETTLE_MS = 200
_WAIT_BUFFER_MS = 500

_OBSERVER_INJECT_JS = """
(() => {
  if (window.__mutObs) {
    try { window.__mutObs.observer.disconnect(); } catch(e) {}
  }
  const data = {
    addedText: [],
    removedText: [],
    addedElements: [],
    removedElements: [],
    attrChanges: [],
  };
  const IGNORED_TAGS = new Set(['SCRIPT','STYLE','NOSCRIPT','LINK','META']);
  const TRACKED_ATTRS = new Set([
    'aria-expanded','aria-checked','aria-selected','aria-hidden',
    'aria-disabled','disabled','checked','selected','open','hidden',
    'value','href','src'
  ]);

  function textOf(node) {
    if (node.nodeType === 3) {
      const t = (node.textContent || '').trim();
      return t.length > 0 && t.length < 500 ? t : null;
    }
    if (node.nodeType === 1) {
      const t = (node.innerText || node.textContent || '').trim();
      return t.length > 0 && t.length < 500 ? t : null;
    }
    return null;
  }

  function tagOf(node) {
    if (node.nodeType === 1) {
      const tag = node.tagName || '';
      const role = node.getAttribute && node.getAttribute('role') || '';
      return [tag.toLowerCase(), role].filter(Boolean).join(' ');
    }
    return '';
  }

  const observer = new MutationObserver((mutations) => {
    for (const m of mutations) {
      if (m.type === 'childList') {
        for (const node of m.addedNodes) {
          if (node.nodeType === 1 && IGNORED_TAGS.has(node.tagName)) continue;
          const text = textOf(node);
          if (text && data.addedText.length < 20)
            data.addedText.push(text.slice(0, 250));
          if (node.nodeType === 1 && data.addedElements.length < 10)
            data.addedElements.push(tagOf(node));
        }
        for (const node of m.removedNodes) {
          if (node.nodeType === 1 && IGNORED_TAGS.has(node.tagName)) continue;
          const text = textOf(node);
          if (text && data.removedText.length < 10)
            data.removedText.push(text.slice(0, 250));
          if (node.nodeType === 1 && data.removedElements.length < 10)
            data.removedElements.push(tagOf(node));
        }
      } else if (m.type === 'attributes') {
        const attr = m.attributeName;
        if (!TRACKED_ATTRS.has(attr)) continue;
        const newVal = m.target.getAttribute(attr);
        const oldVal = m.oldValue;
        if (newVal !== oldVal && data.attrChanges.length < 15) {
          const tag = (m.target.tagName || '').toLowerCase();
          data.attrChanges.push({tag, attr, old: oldVal, new: newVal});
        }
      } else if (m.type === 'characterData') {
        const text = (m.target.textContent || '').trim();
        if (text && data.addedText.length < 20)
          data.addedText.push(text.slice(0, 250));
      }
    }
  });

  observer.observe(document.body || document.documentElement, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeOldValue: true,
    characterData: true,
    characterDataOldValue: true,
    attributeFilter: Array.from(TRACKED_ATTRS),
  });

  window.__mutObs = { observer, data, startUrl: location.href };
})();
"""

_OBSERVER_COLLECT_JS = """
(() => {
  if (!window.__mutObs) return null;
  const { observer, data, startUrl } = window.__mutObs;
  observer.disconnect();
  delete window.__mutObs;

  const seen = new Set();
  const uniqueAdded = [];
  for (const t of data.addedText) {
    const key = t.toLowerCase().trim();
    if (!seen.has(key) && key.length > 0) {
      seen.add(key);
      uniqueAdded.push(t);
    }
  }

  return {
    addedText: uniqueAdded,
    removedText: data.removedText,
    addedElements: data.addedElements,
    removedElements: data.removedElements,
    attrChanges: data.attrChanges,
    currentUrl: location.href,
    startUrl: startUrl,
    title: document.title || '',
  };
})();
"""


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

_SCROLL_PAGE_JS = """
([dx, dy]) => {
    const before = { x: window.scrollX, y: window.scrollY };
    window.scrollBy(dx, dy);
    const after = { x: window.scrollX, y: window.scrollY };
    return { before, after, targetTag: 'window' };
}
""".strip()

_SCROLL_ELEMENT_JS = """
([dx, dy]) => {
    function canScroll(el) {
        const style = getComputedStyle(el);
        const oy = style.overflowY;
        const ox = style.overflowX;
        const canY = dy !== 0 && (oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight + 1;
        const canX = dx !== 0 && (ox === 'auto' || ox === 'scroll') && el.scrollWidth > el.clientWidth + 1;
        return canY || canX;
    }
    let target = this;
    while (target && target !== document.documentElement) {
        if (canScroll(target)) break;
        target = target.parentElement;
    }
    const root = document.scrollingElement || document.documentElement;
    if (!target || target === document.documentElement) {
        target = root;
    }
    const useWindow = target === root;
    const before = {
        x: useWindow ? window.scrollX : target.scrollLeft,
        y: useWindow ? window.scrollY : target.scrollTop,
    };
    if (useWindow) {
        window.scrollBy(dx, dy);
    } else {
        target.scrollBy(dx, dy);
    }
    const after = {
        x: useWindow ? window.scrollX : target.scrollLeft,
        y: useWindow ? window.scrollY : target.scrollTop,
    };
    const tag = useWindow ? 'window' : (target.tagName || 'element');
    return { before, after, targetTag: tag };
}
""".strip()

async def _insert_text(session: CDPSession, text: str) -> bool:
    try:
        await session.send("Input.insertText", {"text": text})
    except Exception:
        return False
    return True

async def _dom_focus(backend_node_id: int, session: CDPSession) -> bool:
    """Focus an element via DOM, bypassing visual obscuration."""
    result = await _call_on_node(
        backend_node_id,
        session,
        """
        function () {
            this.scrollIntoView({block: 'center', inline: 'center'});
            this.focus();
            if (this.select) this.select();
            return document.activeElement === this;
        }
        """,
    )
    if not result:
        return False
    return bool(result.get("result", {}).get("value"))

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


async def _inject_observer(session: CDPSession) -> bool:
    """Inject a MutationObserver into the page. Returns True on success."""
    try:
        await session.send(
            "Runtime.evaluate",
            {"expression": _OBSERVER_INJECT_JS, "returnByValue": True},
        )
        return True
    except Exception:
        return False


async def _collect_mutations(
    session: CDPSession, settle_ms: int = _DEFAULT_SETTLE_MS
) -> dict | None:
    """Wait for DOM mutations to settle, then collect and disconnect the observer."""
    await asyncio.sleep(settle_ms / 1000.0)
    try:
        result = await session.send(
            "Runtime.evaluate",
            {"expression": _OBSERVER_COLLECT_JS, "returnByValue": True},
        )
        value = result.get("result", {}).get("value")
        if isinstance(value, dict):
            return value
        return None
    except Exception:
        return None


def _format_verification(mutations: dict | None, base_message: str) -> str:
    """Append a concise DOM-change summary to the base tool result message."""
    if mutations is None:
        return base_message

    parts: list[str] = [base_message]

    # URL change
    start_url = mutations.get("startUrl", "")
    current_url = mutations.get("currentUrl", "")
    if start_url and current_url and start_url != current_url:
        parts.append(f"Page navigated to: {current_url}")

    # Attribute changes
    attr_changes = mutations.get("attrChanges", [])
    if attr_changes:
        attr_lines = []
        for change in attr_changes:
            tag = change.get("tag", "?")
            attr = change.get("attr", "?")
            old = change.get("old") or "null"
            new = change.get("new") or "null"
            attr_lines.append(f"{tag}[{attr}]: {old} -> {new}")
        parts.append("Attribute changes: " + "; ".join(attr_lines))

    # New text
    added_text = mutations.get("addedText", [])
    if added_text:
        items = [t[:250] for t in added_text]
        parts.append("New text appeared: " + " | ".join(items))

    # Removed text
    removed_text = mutations.get("removedText", [])
    if removed_text:
        items = [t[:250] for t in removed_text]
        parts.append("Text removed: " + " | ".join(items))

    # New elements (only if no text to avoid redundancy)
    added_elements = mutations.get("addedElements", [])
    if added_elements and not added_text:
        parts.append(
            f"{len(added_elements)} element(s) added: "
            + ", ".join(added_elements)
        )

    # Removed elements (only if no text to avoid redundancy)
    removed_elements = mutations.get("removedElements", [])
    if removed_elements and not removed_text:
        parts.append(
            f"{len(removed_elements)} element(s) removed: "
            + ", ".join(removed_elements)
        )

    if len(parts) == 1:
        parts.append("No visible DOM changes detected")

    return ". ".join(parts)

def _format_wait_message(wait_ms: int, mutations: dict | None) -> str:
    wait_seconds = wait_ms / 1000.0
    parts = [f"Waited {wait_ms}ms"]

    if not mutations:
        parts.append(f"No changes during wait of {wait_seconds:.1f} seconds.")
        return "\n".join(parts)

    detail_lines: list[str] = []
    start_url = mutations.get("startUrl", "")
    current_url = mutations.get("currentUrl", "")
    if start_url and current_url and start_url != current_url:
        detail_lines.append(f"Page navigated to: {current_url}")

    attr_changes = mutations.get("attrChanges", [])
    if attr_changes:
        attr_lines = []
        for change in attr_changes:
            tag = change.get("tag", "?")
            attr = change.get("attr", "?")
            old = change.get("old") or "null"
            new = change.get("new") or "null"
            attr_lines.append(f"{tag}[{attr}]: {old} -> {new}")
        detail_lines.append("Attribute changes: " + "; ".join(attr_lines))

    added_text = mutations.get("addedText", [])
    if added_text:
        items = [t[:250] for t in added_text]
        detail_lines.append("New text appeared: " + " | ".join(items))

    removed_text = mutations.get("removedText", [])
    if removed_text:
        items = [t[:250] for t in removed_text]
        detail_lines.append("Text removed: " + " | ".join(items))

    added_elements = mutations.get("addedElements", [])
    if added_elements:
        items = [t[:80] for t in added_elements]
        detail_lines.append("Elements added: " + " | ".join(items))

    removed_elements = mutations.get("removedElements", [])
    if removed_elements:
        items = [t[:80] for t in removed_elements]
        detail_lines.append("Elements removed: " + " | ".join(items))

    if not detail_lines:
        parts.append(f"No changes during wait of {wait_seconds:.1f} seconds.")
        return "\n".join(parts)

    parts.append(f"Changes during wait of {wait_seconds:.1f} seconds:")
    parts.extend(detail_lines)
    return "\n".join(parts)


async def _read_input_value(
    backend_node_id: int, session: CDPSession
) -> str | None:
    """Read the current value of an input/textarea element."""
    result = await _call_on_node(
        backend_node_id,
        session,
        """
        function () {
            return this.value !== undefined ? this.value : (this.textContent || '');
        }
        """,
    )
    if not result:
        return None
    return result.get("result", {}).get("value")




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
    await _inject_observer(session)
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
        await _collect_mutations(session, settle_ms=50)
        return ToolResult(ok=False, message="Click failed")
    mutations = await _collect_mutations(session)
    message = _format_verification(mutations, f"Clicked {element_id}")
    return ToolResult(ok=True, message=message)


async def hover_element(element_id: str, context: ToolContext, *, duration_ms: int = 1000) -> ToolResult:
    context.last_tool = "hover_element"
    context.last_element_id = element_id
    element = _resolve_element(element_id, context)
    if not element or not element.backend_node_id:
        return ToolResult(ok=False, message=f"Unknown element id: {element_id}")
    frame_error = _active_frame_error(element, context)
    if frame_error:
        return frame_error
    session = await _session_for_element(element, context)
    await _inject_observer(session)

    # CDP coordinate hover — triggers CSS :hover pseudo-class
    info = await _viewport_info(element.backend_node_id, session)
    if info:
        value = info.get("result", {}).get("value")
        if value:
            try:
                await session.send(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseMoved", "x": value["x"], "y": value["y"]},
                )
            except Exception:
                pass  # Fall through to DOM events

    # DOM synthetic events — triggers JS mouseover/mouseenter handlers
    await _call_on_node(
        element.backend_node_id,
        session,
        """
        function () {
            this.scrollIntoView({block: 'center', inline: 'center'});
            const opts = {bubbles: true, cancelable: true};
            this.dispatchEvent(new MouseEvent('mouseenter', {...opts, bubbles: false}));
            this.dispatchEvent(new MouseEvent('mouseover', opts));
            return true;
        }
        """,
    )

    # Hold hover for the requested duration
    clamped = max(100, min(duration_ms, 5000))
    await asyncio.sleep(clamped / 1000)

    # Complete the hover cycle — fire mouseleave/mouseout so JS handlers that
    # accumulate hover duration (e.g. time-gated reveals) get the leave event.
    await _call_on_node(
        element.backend_node_id,
        session,
        """
        function () {
            const opts = {bubbles: true, cancelable: true};
            this.dispatchEvent(new MouseEvent('mouseleave', {...opts, bubbles: false}));
            this.dispatchEvent(new MouseEvent('mouseout', opts));
            return true;
        }
        """,
    )

    # Also move CDP cursor away so CSS :hover pseudo-class clears
    if info:
        value = info.get("result", {}).get("value")
        if value:
            try:
                await session.send(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseMoved", "x": 0, "y": 0},
                )
            except Exception:
                pass

    mutations = await _collect_mutations(session)
    message = _format_verification(mutations, f"Hovered {element_id} for {clamped}ms")
    return ToolResult(ok=True, message=message)


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
    await _inject_observer(session)
    if not await _dom_focus(element.backend_node_id, session):
        await _collect_mutations(session, settle_ms=50)
        return ToolResult(ok=False, message="Type failed: element not focusable")
    if not await _insert_text(session, text):
        await _collect_mutations(session, settle_ms=50)
        return ToolResult(ok=False, message="Type failed")
    mutations = await _collect_mutations(session)
    current_value = await _read_input_value(element.backend_node_id, session)
    base_msg = f"Typed into {element_id}"
    if current_value is not None:
        display = current_value[:250] + "..." if len(current_value) > 250 else current_value
        base_msg = f"Typed into {element_id}. Current value: \"{display}\""
    message = _format_verification(mutations, base_msg)
    return ToolResult(ok=True, message=message)


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

    await _inject_observer(session)

    base_msg = f"Dragged {source_id} -> {target_id}"

    # CDP coordinate-based drag — only when both elements are on top (no overlays).
    # CDP mouse events can't complete HTML5 DnD through overlays (browser's drag
    # state machine doesn't handle mouseReleased as a drop when overlay intercepts).
    if source_value.get("onTop") and target_value.get("onTop"):
        try:
            sx, sy = source_value["x"], source_value["y"]
            tx, ty = target_value["x"], target_value["y"]
            await session.send(
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": sx, "y": sy, "button": "left"},
            )
            await session.send(
                "Input.dispatchMouseEvent",
                {"type": "mousePressed", "x": sx, "y": sy, "button": "left", "clickCount": 1},
            )
            await asyncio.sleep(0.05)
            await session.send(
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": sx + 10, "y": sy + 10, "button": "left", "buttons": 1},
            )
            await asyncio.sleep(0.05)
            await session.send(
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": tx, "y": ty, "button": "left", "buttons": 1},
            )
            await asyncio.sleep(0.05)
            await session.send(
                "Input.dispatchMouseEvent",
                {"type": "mouseReleased", "x": tx, "y": ty, "button": "left", "clickCount": 1},
            )
            mutations = await _collect_mutations(session)
            message = _format_verification(mutations, base_msg)
            return ToolResult(ok=True, message=message)
        except Exception:
            pass  # Fall through to DOM fallback

    # DOM fallback — split-phase DragEvent dispatch for overlay bypass.
    # Dispatches directly on DOM nodes (bypasses visual layering).
    # Split into two phases with async gap so framework state (React setState)
    # can flush between dragstart and drop.
    target_object_id = await _resolve_object_id(target.backend_node_id, session)
    if not target_object_id:
        await _collect_mutations(session, settle_ms=50)
        return ToolResult(ok=False, message="Drag failed: cannot resolve target")

    phase1 = await _call_on_node(
        source.backend_node_id,
        session,
        """
        function () {
            this.scrollIntoView({block: 'center', inline: 'center'});
            const dt = new DataTransfer();
            this.dispatchEvent(new DragEvent('dragstart', {bubbles: true, cancelable: true, dataTransfer: dt}));
            return true;
        }
        """,
    )
    if not phase1:
        await _collect_mutations(session, settle_ms=50)
        return ToolResult(ok=False, message="Drag failed: cannot initiate drag")

    await asyncio.sleep(0.1)

    source_object_id = await _resolve_object_id(source.backend_node_id, session)
    result = await _call_on_node(
        target.backend_node_id,
        session,
        """
        function (sourceEl) {
            this.scrollIntoView({block: 'center', inline: 'center'});
            const dt = new DataTransfer();
            const opts = {bubbles: true, cancelable: true, dataTransfer: dt};
            this.dispatchEvent(new DragEvent('dragenter', opts));
            this.dispatchEvent(new DragEvent('dragover', opts));
            this.dispatchEvent(new DragEvent('drop', opts));
            if (sourceEl) sourceEl.dispatchEvent(new DragEvent('dragend', opts));
            return true;
        }
        """,
        [{"objectId": source_object_id or target_object_id}],
    )
    if not result:
        await _collect_mutations(session, settle_ms=50)
        return ToolResult(ok=False, message="Drag failed: drop not accepted")
    mutations = await _collect_mutations(session)
    return ToolResult(ok=True, message=_format_verification(mutations, base_msg))


async def draw(
    element_id: str,
    path: list[list[float]],
    context: ToolContext,
) -> ToolResult:
    context.last_tool = "draw"
    context.last_element_id = element_id
    element = _resolve_element(element_id, context)
    if not element or not element.backend_node_id:
        return ToolResult(ok=False, message=f"Unknown element id: {element_id}")
    frame_error = _active_frame_error(element, context)
    if frame_error:
        return frame_error
    if len(path) < 2:
        return ToolResult(ok=False, message="Draw requires at least 2 points")

    session = await _session_for_element(element, context)
    info = await _viewport_info(element.backend_node_id, session)
    if not info:
        return ToolResult(ok=False, message="Draw failed: cannot locate element")
    value = info.get("result", {}).get("value")
    if not value:
        return ToolResult(ok=False, message="Draw failed: cannot locate element")

    # Element top-left corner in viewport coordinates
    el_left = value["x"] - value["width"] / 2
    el_top = value["y"] - value["height"] / 2

    await _inject_observer(session)

    base_msg = f"Drew path with {len(path)} points on {element_id}"

    # CDP coordinate-based draw — only when element is on top (no overlays).
    if value.get("onTop"):
        start_x = el_left + path[0][0]
        start_y = el_top + path[0][1]
        try:
            await session.send(
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": start_x, "y": start_y, "button": "left"},
            )
            await session.send(
                "Input.dispatchMouseEvent",
                {"type": "mousePressed", "x": start_x, "y": start_y, "button": "left", "clickCount": 1},
            )
            for point in path[1:]:
                px = el_left + point[0]
                py = el_top + point[1]
                await session.send(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseMoved", "x": px, "y": py, "button": "left", "buttons": 1},
                )
                await asyncio.sleep(0.02)
            last_x = el_left + path[-1][0]
            last_y = el_top + path[-1][1]
            await session.send(
                "Input.dispatchMouseEvent",
                {"type": "mouseReleased", "x": last_x, "y": last_y, "button": "left", "clickCount": 1},
            )
            mutations = await _collect_mutations(session)
            return ToolResult(ok=True, message=_format_verification(mutations, base_msg))
        except Exception:
            pass  # Fall through to DOM fallback

    # DOM fallback — dispatch synthetic MouseEvents directly on the element,
    # bypassing any overlay that intercepts CDP coordinate-based events.
    result = await _call_on_node(
        element.backend_node_id,
        session,
        """
        function (path) {
            this.scrollIntoView({block: 'center', inline: 'center'});
            const rect = this.getBoundingClientRect();
            const opts = (x, y, buttons) => ({
                bubbles: true, cancelable: true, button: 0, buttons: buttons,
                clientX: rect.left + x, clientY: rect.top + y,
                offsetX: x, offsetY: y,
            });
            const first = path[0];
            this.dispatchEvent(new MouseEvent('mousedown', opts(first[0], first[1], 1)));
            for (let i = 1; i < path.length; i++) {
                this.dispatchEvent(new MouseEvent('mousemove', opts(path[i][0], path[i][1], 1)));
            }
            const last = path[path.length - 1];
            this.dispatchEvent(new MouseEvent('mouseup', opts(last[0], last[1], 0)));
            return true;
        }
        """,
        [{"value": path}],
    )
    if not result:
        await _collect_mutations(session, settle_ms=50)
        return ToolResult(ok=False, message="Draw failed: DOM event dispatch error")

    mutations = await _collect_mutations(session)
    return ToolResult(ok=True, message=_format_verification(mutations, base_msg))


async def wait(milliseconds: int, context: ToolContext) -> ToolResult:
    context.last_tool = "wait"
    context.last_element_id = None
    clamped = max(0, min(milliseconds, 10_000))
    buffered = min(clamped + _WAIT_BUFFER_MS, 10_000)
    injected = await _inject_observer(context.cdp_session)
    await asyncio.sleep(buffered / 1000)
    mutations = await _collect_mutations(context.cdp_session) if injected else None
    return ToolResult(ok=True, message=_format_wait_message(buffered, mutations))


def _truncate_attr(value: str, max_len: int = 200) -> str:
    if len(value) > max_len:
        return value[:max_len] + "..."
    return value


async def inspect_element(element_id: str, context: ToolContext) -> ToolResult:
    """Read an element's full text content and all HTML attributes."""
    context.last_tool = "inspect_element"
    context.last_element_id = element_id
    element = _resolve_element(element_id, context)
    if not element:
        return ToolResult(ok=False, message=f"Unknown element id: {element_id}")
    frame_error = _active_frame_error(element, context)
    if frame_error:
        return frame_error

    # Read text via CDP if possible
    text_value = ""
    if element.backend_node_id:
        session = await _session_for_element(element, context)
        result = await _call_on_node(
            element.backend_node_id,
            session,
            """
            function () {
                return this.innerText || this.textContent || '';
            }
            """,
        )
        if result:
            text_value = result.get("result", {}).get("value") or ""

    # Read attributes from snapshot
    attrs = element.attributes or {}
    attr_parts = [f'{k}="{_truncate_attr(v)}"' for k, v in attrs.items()]
    attr_str = " ".join(attr_parts) if attr_parts else "none"

    parts = []
    parts.append(f"text: {text_value}" if text_value else "text: (empty)")
    parts.append(f"attributes: {attr_str}")
    return ToolResult(ok=True, message="\n".join(parts))


async def search_page_attributes(query: str, context: ToolContext) -> ToolResult:
    """Search every element on the page for attributes whose name or value contains the query string."""
    context.last_tool = "search_page_attributes"
    context.last_element_id = None
    if not query or len(query) < 2:
        return ToolResult(ok=False, message="Query must be at least 2 characters")
    try:
        results = await context.page.evaluate(
            """(query) => {
                const matches = [];
                const q = query.toLowerCase();
                for (const el of document.querySelectorAll('*')) {
                    for (const attr of el.attributes) {
                        if (attr.value.toLowerCase().includes(q) ||
                            attr.name.toLowerCase().includes(q)) {
                            const text = (el.innerText || '').slice(0, 100).trim();
                            const attrs = {};
                            for (const a of el.attributes) {
                                attrs[a.name] = a.value.length > 200
                                    ? a.value.slice(0, 200) + '...' : a.value;
                            }
                            matches.push({
                                tag: el.tagName.toLowerCase(),
                                attrs: attrs,
                                text: text
                            });
                            break;
                        }
                    }
                    if (matches.length >= 10) break;
                }
                return matches;
            }""",
            query,
        )
    except Exception as exc:
        return ToolResult(ok=False, message=f"Search failed: {exc}")
    if not results:
        return ToolResult(ok=True, message="No matching elements found.")
    lines = []
    for match in results:
        attrs_str = " ".join(f'{k}="{v}"' for k, v in match["attrs"].items())
        text_hint = f' text="{match["text"]}"' if match.get("text") else ""
        lines.append(f"<{match['tag']} {attrs_str}>{text_hint}")
    return ToolResult(ok=True, message="\n".join(lines))

async def scroll(
    delta_x: int,
    delta_y: int,
    context: ToolContext,
    *,
    element_id: str | None = None,
) -> ToolResult:
    """Scroll the page by a pixel offset, optionally anchored to a target element."""
    context.last_tool = "scroll"
    context.last_element_id = element_id
    element = _resolve_element(element_id, context) if element_id else None
    frame_error = _active_frame_error(element, context) if element_id else None
    if frame_error:
        return frame_error
    session = context.cdp_session
    if element_id and element and element.frame_id:
        session = await _session_for_element(element, context)
    try:
        if element_id and element and element.backend_node_id:
            result = await _call_on_node(
                element.backend_node_id,
                session,
                _SCROLL_ELEMENT_JS,
                args=[{"value": [delta_x, delta_y]}],
            )
            if not result:
                return ToolResult(ok=False, message="Scroll failed: element not found")
            result = result.get("result", {}).get("value")
        else:
            result = await context.page.evaluate(_SCROLL_PAGE_JS, [delta_x, delta_y])
    except Exception as exc:
        return ToolResult(ok=False, message=f"Scroll failed: {exc}")
    if not result:
        return ToolResult(ok=True, message=f"Scrolled dx={delta_x} dy={delta_y}")
    before = result.get("before", {})
    after = result.get("after", {})
    target = result.get("targetTag")
    dx = round(after.get("x", 0) - before.get("x", 0))
    dy = round(after.get("y", 0) - before.get("y", 0))
    msg = f"Scrolled dx={delta_x} dy={delta_y}"
    if target:
        msg += f" on {target}"
    msg += (
        f". Scroll position changed by ({dx}, {dy})px,"
        f" now at ({round(after.get('x', 0))}, {round(after.get('y', 0))})"
    )
    if dx == 0 and dy == 0:
        msg += ". WARNING: scroll position did not change (may be at boundary)"
    return ToolResult(ok=True, message=msg)


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
    try:
        final_url = context.page.url
        title = await context.page.title()
        parts = [f"Navigated to {final_url}"]
        if title:
            parts.append(f"Page title: \"{title}\"")
        if final_url != url:
            parts.append(f"(redirected from {url})")
        return ToolResult(ok=True, message=". ".join(parts))
    except Exception:
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
    session = context.cdp_session
    if context.active_frame_id and context.active_frame_id in context.frame_sessions:
        session = context.frame_sessions[context.active_frame_id]
    await _inject_observer(session)
    try:
        await context.page.keyboard.press("+".join(keys))
    except Exception as exc:  # pragma: no cover - runtime safety
        await _collect_mutations(session, settle_ms=50)
        return ToolResult(ok=False, message=f"Key press failed: {exc}")
    mutations = await _collect_mutations(session)
    base_msg = f"Pressed {'+'.join(keys)}"
    message = _format_verification(mutations, base_msg)
    return ToolResult(ok=True, message=message)
