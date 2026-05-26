"""Semantic tool definitions for the agent."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import logging
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

from playwright.async_api import CDPSession, Page

from src.agent.browser.session import BrowserSession
from src.agent.context.snapshot import ElementSnapshot, ElementIndex, build_stable_id_from_backend


@dataclass(frozen=True)
class ToolResult:
    """Result of executing a semantic tool."""

    ok: bool
    message: str


@dataclass(frozen=True)
class ToolTimingConfig:
    """Timing parameters for tool actions (milliseconds)."""

    settle_ms: int = 100
    draw_settle_ms: int = 400
    draw_point_interval_ms: int = 20
    drag_phase_interval_ms: int = 50


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
    timing: ToolTimingConfig = field(default_factory=ToolTimingConfig)


def build_tool_context(
    session: BrowserSession,
    element_index: ElementIndex,
    *,
    active_frame_id: str | None = None,
    timing: ToolTimingConfig | None = None,
) -> ToolContext:
    """Build a tool context tied to the browser session lifecycle."""

    return ToolContext(
        page=session.page,
        cdp_session=session.cdp_session,
        element_index=element_index,
        frame_sessions=session.frame_sessions,
        active_frame_id=active_frame_id,
        timing=timing or ToolTimingConfig(),
    )

_WAIT_BUFFER_MS = 200

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
    interactiveNodes: [],
    formStart: [],
  };
  const TEXT_CAP = 20;
  const INTERACTIVE_CAP = 10;
  const charDataNodes = new Map();  // node -> index in addedText
  const IGNORED_TAGS = new Set(['SCRIPT','STYLE','NOSCRIPT','LINK','META']);
  const TRACKED_ATTRS = new Set([
    'aria-expanded','aria-checked','aria-selected','aria-hidden',
    'aria-disabled','disabled','checked','selected','open','hidden',
    'value','href','src','class','style'
  ]);
  const INTERACTIVE_TAGS_SET = new Set(['A','BUTTON','INPUT','SELECT','TEXTAREA','OPTION','IFRAME']);
  const INTERACTIVE_ROLES_SET = new Set([
    'button','checkbox','combobox','link','menuitem','option',
    'radio','slider','spinbutton','switch','tab','textbox'
  ]);
  const SVG_INTERACTIVE_TAGS_SET = new Set([
    'CIRCLE','ELLIPSE','G','LINE','PATH','POLYGON','POLYLINE','RECT','TEXT','TSPAN'
  ]);
  const SVG_ATTRS = [
    'class','id','fill','stroke','x','y','x1','y1','x2','y2','cx','cy','r','rx','ry',
    'width','height','data-index'
  ];

  function isInteractive(el) {
    if (INTERACTIVE_TAGS_SET.has(el.tagName)) return true;
    if ((el.ownerSVGElement || el.tagName === 'SVG') && SVG_INTERACTIVE_TAGS_SET.has(el.tagName)) return true;
    const role = el.getAttribute('role');
    if (role && INTERACTIVE_ROLES_SET.has(role.toLowerCase())) return true;
    if (el.hasAttribute('contenteditable')) return true;
    if (el.hasAttribute('tabindex')) return true;
    if (el.hasAttribute('onclick')) return true;
    if (el.getAttribute('draggable') === 'true') return true;
    return false;
  }

  function collectInteractive(node) {
    if (node.nodeType !== 1) return;
    if (data.interactiveNodes.length >= INTERACTIVE_CAP) return;
    if (isInteractive(node)) {
      data.interactiveNodes.push(node);
    }
    // Check direct children (one level deep) — when a parent container is
    // appended, only the container appears in addedNodes, not its children.
    if (node.children) {
      for (const child of node.children) {
        if (data.interactiveNodes.length >= INTERACTIVE_CAP) break;
        if (isInteractive(child)) {
          data.interactiveNodes.push(child);
        }
      }
    }
  }

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

  function attrsOf(el) {
    const attrs = {};
    if (!el.getAttribute) return attrs;
    for (const key of SVG_ATTRS) {
      const value = el.getAttribute(key);
      if (value !== null && value !== '') attrs[key] = value;
    }
    return attrs;
  }

  function formValues() {
    const controls = Array.from(document.querySelectorAll('input, textarea, select'));
    return controls.slice(0, 60).map((el, index) => {
      const tag = (el.tagName || '').toLowerCase();
      const key = el.id || el.name || el.getAttribute('aria-label') ||
        el.getAttribute('placeholder') || `${tag}:${index}`;
      return {
        key,
        tag,
        value: el.value !== undefined ? String(el.value) : '',
        checked: el.checked === undefined ? null : Boolean(el.checked),
      };
    });
  }

  data.formStart = formValues();

  const callback = (mutations) => {
    for (const m of mutations) {
      if (m.type === 'childList') {
        for (const node of m.addedNodes) {
          if (node.nodeType === 1 && IGNORED_TAGS.has(node.tagName)) continue;
          const text = textOf(node);
          if (text && data.addedText.length < TEXT_CAP) {
            const tag = node.nodeType === 1 ? (node.tagName || '').toLowerCase() : '';
            data.addedText.push({t: text.slice(0, 250), tag});
          }
          if (node.nodeType === 1 && data.addedElements.length < 10)
            data.addedElements.push(tagOf(node));
          collectInteractive(node);
        }
        for (const node of m.removedNodes) {
          if (node.nodeType === 1 && IGNORED_TAGS.has(node.tagName)) continue;
          const text = textOf(node);
          if (text && data.removedText.length < 10) {
            const tag = node.nodeType === 1 ? (node.tagName || '').toLowerCase() : '';
            data.removedText.push({t: text.slice(0, 250), tag});
          }
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
        if (text) {
          const parentTag = (m.target.parentElement?.tagName || '').toLowerCase();
          const existing = charDataNodes.get(m.target);
          if (existing !== undefined) {
            data.addedText[existing] = {t: text.slice(0, 250), tag: parentTag};
          } else if (data.addedText.length < TEXT_CAP) {
            charDataNodes.set(m.target, data.addedText.length);
            data.addedText.push({t: text.slice(0, 250), tag: parentTag});
          }
        }
      }
    }
  };

  const observer = new MutationObserver(callback);

  observer.observe(document.body || document.documentElement, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeOldValue: true,
    characterData: true,
    characterDataOldValue: true,
    attributeFilter: Array.from(TRACKED_ATTRS),
  });

  window.__mutObs = { observer, data, callback, startUrl: location.href };
})();
"""

_OBSERVER_COLLECT_JS = """
(() => {
  if (!window.__mutObs) return null;
  const { observer, data, callback, startUrl } = window.__mutObs;
  const SVG_ATTRS = [
    'class','id','fill','stroke','x','y','x1','y1','x2','y2','cx','cy','r','rx','ry',
    'width','height','data-index'
  ];
  function attrsOf(el) {
    const attrs = {};
    if (!el.getAttribute) return attrs;
    for (const key of SVG_ATTRS) {
      const value = el.getAttribute(key);
      if (value !== null && value !== '') attrs[key] = value;
    }
    return attrs;
  }
  function formValues() {
    const controls = Array.from(document.querySelectorAll('input, textarea, select'));
    return controls.slice(0, 60).map((el, index) => {
      const tag = (el.tagName || '').toLowerCase();
      const key = el.id || el.name || el.getAttribute('aria-label') ||
        el.getAttribute('placeholder') || `${tag}:${index}`;
      return {
        key,
        tag,
        value: el.value !== undefined ? String(el.value) : '',
        checked: el.checked === undefined ? null : Boolean(el.checked),
      };
    });
  }
  const pending = observer.takeRecords();
  if (pending.length > 0) callback(pending);
  observer.disconnect();

  // Stamp interactive nodes with temporary markers for CDP resolution
  const newInteractive = [];
  for (let i = 0; i < data.interactiveNodes.length; i++) {
    const el = data.interactiveNodes[i];
    if (!document.contains(el)) continue;
    const marker = '__mut_' + i;
    el.setAttribute('data-agent-mut-id', marker);
    const text = (el.innerText || el.textContent || '').trim().slice(0, 250);
    const role = el.getAttribute('role') || '';
    const name = el.getAttribute('aria-label') || el.getAttribute('name') || '';
    newInteractive.push({
      marker: marker,
      tag: (el.tagName || '').toLowerCase(),
      role: role,
      text: text,
      name: name,
      attrs: attrsOf(el),
    });
  }

  delete window.__mutObs;

  const formEnd = formValues();
  const startByKey = new Map();
  for (const item of data.formStart || []) {
    startByKey.set(`${item.tag}|${item.key}`, item);
  }
  const formChanges = [];
  for (const item of formEnd) {
    if (formChanges.length >= 12) break;
    const previous = startByKey.get(`${item.tag}|${item.key}`);
    if (!previous) continue;
    if (previous.value !== item.value) {
      formChanges.push({
        tag: item.tag,
        key: item.key,
        attr: 'value',
        old: previous.value,
        new: item.value,
      });
    }
    if (previous.checked !== item.checked) {
      formChanges.push({
        tag: item.tag,
        key: item.key,
        attr: 'checked',
        old: previous.checked,
        new: item.checked,
      });
    }
  }

  const seen = new Set();
  const uniqueAdded = [];
  for (const item of data.addedText) {
    const text = typeof item === 'string' ? item : item.t;
    const key = text.toLowerCase().trim();
    if (!seen.has(key) && key.length > 0) {
      seen.add(key);
      uniqueAdded.push(item);
    }
  }

  return {
    addedText: uniqueAdded,
    removedText: data.removedText,
    addedElements: data.addedElements,
    removedElements: data.removedElements,
    attrChanges: data.attrChanges,
    formChanges: formChanges,
    newInteractive: newInteractive,
    currentUrl: location.href,
    startUrl: startUrl,
    title: document.title || '',
  };
})();
"""


def _resolve_element(element_id: str, context: ToolContext) -> ElementSnapshot | None:
    element = context.element_index.elements.get(element_id)
    if element is None and not element_id.startswith("el_"):
        corrected = f"el_{element_id}"
        element = context.element_index.elements.get(corrected)
        if element is not None:
            logger.warning("Auto-corrected element id %r -> %r", element_id, corrected)
    return element


def _unknown_element_message(element_id: str) -> str:
    return (
        f"Unknown element id: {element_id}. The page likely changed after a prior tool call; "
        "stop this step and wait for a fresh snapshot before using another element id."
    )


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
        result = await session.send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": function_body,
                "arguments": args or [],
                "returnByValue": True,
            },
        )
        if result.get("exceptionDetails"):
            return None
        return result
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


async def _element_rect_info(backend_node_id: int, session: CDPSession) -> dict[str, Any] | None:
    return await _call_on_node(
        backend_node_id,
        session,
        """
        function () {
            const before = this.getBoundingClientRect();
            this.scrollIntoView({block: 'center', inline: 'center'});
            const rect = this.getBoundingClientRect();
            return {
                left: rect.left,
                top: rect.top,
                width: rect.width,
                height: rect.height,
                beforeLeft: before.left,
                beforeTop: before.top,
                beforeWidth: before.width,
                beforeHeight: before.height,
            };
        }
        """,
    )

_SCROLL_PAGE_JS = """
([dx, dy]) => {
    const before = { x: window.scrollX, y: window.scrollY };
    window.scrollBy(dx, dy);
    const after = { x: window.scrollX, y: window.scrollY };
    return { before, after, targetTag: 'window' };
}
""".strip()

_SCROLL_ELEMENT_JS = """
function ([dx, dy]) {
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


async def _type_text_with_keyboard(page: Any, text: str) -> tuple[bool, str | None]:
    keyboard = getattr(page, "keyboard", None)
    type_method = getattr(keyboard, "type", None)
    if not callable(type_method):
        return False, "keyboard typing unavailable"
    try:
        result = type_method(text)
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:
        return False, str(exc)
    return True, None


async def _set_text_value(backend_node_id: int, session: CDPSession, text: str) -> bool:
    """Set input-like values through native setters and dispatch form events."""
    result = await _call_on_node(
        backend_node_id,
        session,
        """
        function (value) {
            const tag = (this.tagName || '').toUpperCase();
            if (!('value' in this) || !['INPUT', 'TEXTAREA'].includes(tag)) {
                return false;
            }
            const proto = tag === 'TEXTAREA'
                ? window.HTMLTextAreaElement.prototype
                : window.HTMLInputElement.prototype;
            const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
            if (descriptor && descriptor.set) {
                descriptor.set.call(this, value);
            } else {
                this.value = value;
            }
            this.dispatchEvent(new Event('input', { bubbles: true }));
            this.dispatchEvent(new Event('change', { bubbles: true }));
            return this.value === value;
        }
        """,
        [{"value": text}],
    )
    return bool(result and result.get("result", {}).get("value"))

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


async def _dispatch_pointer_drag_local(
    backend_node_id: int,
    session: CDPSession,
    *,
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    steps: int,
    allow_outside: bool = False,
) -> tuple[bool, dict[str, float] | None, str | None]:
    info = await _element_rect_info(backend_node_id, session)
    value = (info or {}).get("result", {}).get("value")
    if not value:
        return False, None, "cannot locate element"
    width = float(value.get("width") or 0)
    height = float(value.get("height") or 0)
    if width <= 0 or height <= 0:
        return False, None, "element has no visible area"
    left = float(value.get("left") or 0)
    top = float(value.get("top") or 0)
    sx = max(0.0, min(float(start_x), width))
    sy = max(0.0, min(float(start_y), height))
    if allow_outside:
        ex = float(end_x)
        ey = float(end_y)
    else:
        ex = max(0.0, min(float(end_x), width))
        ey = max(0.0, min(float(end_y), height))
    px0 = left + sx
    py0 = top + sy
    px1 = left + ex
    py1 = top + ey
    try:
        await session.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": px0, "y": py0, "button": "none"})
        await session.send(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": px0, "y": py0, "button": "left", "clickCount": 1},
        )
        count = max(1, min(int(steps), 50))
        for idx in range(1, count + 1):
            t = idx / count
            await session.send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseMoved",
                    "x": px0 + (px1 - px0) * t,
                    "y": py0 + (py1 - py0) * t,
                    "button": "left",
                    "buttons": 1,
                },
            )
        await session.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": px1, "y": py1, "button": "left", "clickCount": 1},
        )
    except Exception as exc:
        return False, None, str(exc)
    return True, {
        "start_local_x": sx,
        "start_local_y": sy,
        "end_local_x": ex,
        "end_local_y": ey,
        "start_viewport_x": px0,
        "start_viewport_y": py0,
        "end_viewport_x": px1,
        "end_viewport_y": py1,
        "width": width,
        "height": height,
    }, None


async def _dispatch_pointer_drag_between_nodes(
    source_backend_node_id: int,
    target_backend_node_id: int,
    session: CDPSession,
    *,
    steps: int,
) -> tuple[bool, dict[str, float] | None, str | None]:
    target_object_id = await _resolve_object_id(target_backend_node_id, session)
    if not target_object_id:
        return False, None, "cannot resolve target"
    pair_info = await _call_on_node(
        source_backend_node_id,
        session,
        """
        function (targetEl) {
            this.scrollIntoView({block: 'center', inline: 'center'});
            const sourceRect = this.getBoundingClientRect();
            const targetRect = targetEl.getBoundingClientRect();
            return {
                source: {
                    left: sourceRect.left,
                    top: sourceRect.top,
                    width: sourceRect.width,
                    height: sourceRect.height,
                },
                target: {
                    left: targetRect.left,
                    top: targetRect.top,
                    width: targetRect.width,
                    height: targetRect.height,
                },
            };
        }
        """,
        [{"objectId": target_object_id}],
    )
    value = (pair_info or {}).get("result", {}).get("value") or {}
    source = value.get("source")
    target = value.get("target")
    if not isinstance(source, dict) or not isinstance(target, dict):
        return False, None, "cannot locate source or target"
    source_width = float(source.get("width") or 0)
    source_height = float(source.get("height") or 0)
    target_width = float(target.get("width") or 0)
    target_height = float(target.get("height") or 0)
    if source_width <= 0 or source_height <= 0 or target_width <= 0 or target_height <= 0:
        return False, None, "source or target has no visible area"
    source_left = float(source.get("left") or 0)
    source_top = float(source.get("top") or 0)
    target_left = float(target.get("left") or 0)
    target_top = float(target.get("top") or 0)
    target_center_x = target_left + target_width / 2
    target_center_y = target_top + target_height / 2
    return await _dispatch_pointer_drag_local(
        source_backend_node_id,
        session,
        start_x=source_width / 2,
        start_y=source_height / 2,
        end_x=target_center_x - source_left,
        end_y=target_center_y - source_top,
        steps=steps,
        allow_outside=True,
    )


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

async def _session_for_active_frame(context: ToolContext) -> CDPSession:
    """Return the CDP session for the active frame (or the main session)."""
    if not context.active_frame_id:
        return context.cdp_session
    if context.active_frame_id in context.frame_sessions:
        return context.frame_sessions[context.active_frame_id]
    iframe_element: ElementSnapshot | None = None
    for el in context.element_index.elements.values():
        if (el.node_name or "").upper() == "IFRAME" and el.frame_id == context.active_frame_id:
            iframe_element = el
            break
    if not iframe_element:
        return context.cdp_session
    try:
        session = await _session_for_element(iframe_element, context)
    except Exception:
        return context.cdp_session
    if context.active_frame_id in context.frame_sessions:
        return context.frame_sessions[context.active_frame_id]
    return session

def _main_frame_id_from_index(context: ToolContext) -> str | None:
    """Best-effort guess of the main frame id based on the current snapshot."""
    page_url = getattr(context.page, "url", "") or ""
    child_frame_ids = {
        el.frame_id
        for el in context.element_index.elements.values()
        if el.frame_id and (el.node_name or "").upper() == "IFRAME"
    }

    def _pick(exclude: set[str]) -> str | None:
        by_url: dict[str, int] = {}
        by_total: dict[str, int] = {}
        for el in context.element_index.elements.values():
            fid = el.frame_id
            if not fid or fid in exclude:
                continue
            by_total[fid] = by_total.get(fid, 0) + 1
            if el.frame_url and el.frame_url == page_url:
                by_url[fid] = by_url.get(fid, 0) + 1
        if by_url:
            return sorted(by_url.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        if by_total:
            return sorted(by_total.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        return None

    return _pick(child_frame_ids) or _pick(set())

def _iframe_id_for_frame(frame_id: str | None, context: ToolContext) -> str | None:
    if not frame_id:
        return None
    for el in context.element_index.elements.values():
        if (el.node_name or "").upper() == "IFRAME" and el.frame_id == frame_id:
            return el.stable_id
    return None

def _hostname(url: str | None) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    return parsed.hostname or ""

def _active_frame_error(element: ElementSnapshot | None, context: ToolContext) -> ToolResult | None:
    if not context.active_frame_id or not element:
        return None
    if element.frame_id == context.active_frame_id:
        return None

    main_frame_id = _main_frame_id_from_index(context)
    active_iframe_id = _iframe_id_for_frame(context.active_frame_id, context)
    active_label = active_iframe_id or context.active_frame_id
    element_id = element.stable_id

    if element.frame_id is None:
        return ToolResult(
            ok=False,
            message=(
                f"Element {element_id} is in the main frame, but you are in iframe {active_label}. "
                "Call switch_to_main_frame() first."
            ),
        )

    if element.frame_id == main_frame_id or (
        main_frame_id is None and element.frame_url == getattr(context.page, "url", "")
    ):
        return ToolResult(
            ok=False,
            message=(
                f"Element {element_id} is in the main frame, but you are in iframe {active_label}. "
                "Call switch_to_main_frame() first."
            ),
        )

    target_iframe_id = _iframe_id_for_frame(element.frame_id, context)
    target_label = target_iframe_id or _hostname(element.frame_url) or (element.frame_id or "unknown")
    return ToolResult(
        ok=False,
        message=(
            f"Element {element_id} is in iframe {target_label}, but you are in iframe {active_label}. "
            f"Call switch_to_main_frame(), then switch_to_iframe({target_label})."
        ),
    )


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
    session: CDPSession, settle_ms: int = 100
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


async def _resolve_new_interactive(
    session: CDPSession, mutations: dict, frame_id: str | None
) -> None:
    """Resolve stable element IDs for newly added interactive elements via CDP.

    Enriches mutations dict in-place with 'resolvedInteractive' list.
    Best-effort: exceptions skip individual elements.
    """
    new_items = mutations.get("newInteractive")
    if not new_items:
        return
    resolved: list[dict[str, Any]] = []
    for item in new_items:
        marker = item.get("marker")
        if not marker:
            continue
        try:
            result = await session.send(
                "Runtime.evaluate",
                {
                    "expression": f'document.querySelector("[data-agent-mut-id=\\"{marker}\\"]")',
                    "returnByValue": False,
                },
            )
            object_id = result.get("result", {}).get("objectId")
            if not object_id:
                continue
            desc = await session.send(
                "DOM.describeNode",
                {"objectId": object_id},
            )
            backend_node_id = desc.get("node", {}).get("backendNodeId")
            if not backend_node_id:
                continue
            rect_value: dict[str, Any] = {}
            try:
                rect = await session.send(
                    "Runtime.callFunctionOn",
                    {
                        "objectId": object_id,
                        "functionDeclaration": (
                            "function () { const r = this.getBoundingClientRect(); "
                            "return {x: r.left, y: r.top, w: r.width, h: r.height}; }"
                        ),
                        "returnByValue": True,
                    },
                )
                rect_value = rect.get("result", {}).get("value") or {}
            except Exception:
                rect_value = {}
            stable_id = build_stable_id_from_backend(frame_id, int(backend_node_id))
            resolved.append({
                "stable_id": stable_id,
                "backend_node_id": int(backend_node_id),
                "tag": item.get("tag", ""),
                "role": item.get("role", ""),
                "text": item.get("text", ""),
                "name": item.get("name", ""),
                "attrs": item.get("attrs") if isinstance(item.get("attrs"), dict) else {},
                "bbox": rect_value,
            })
            # Clean up marker attribute
            await session.send(
                "Runtime.evaluate",
                {
                    "expression": (
                        f'document.querySelector("[data-agent-mut-id=\\"{marker}\\"]")'
                        f'?.removeAttribute("data-agent-mut-id")'
                    ),
                    "returnByValue": True,
                },
            )
        except Exception:
            continue
    if resolved:
        mutations["resolvedInteractive"] = resolved


def _bbox_from_resolved_item(item: dict[str, Any]) -> tuple[float, float, float, float] | None:
    bbox = item.get("bbox")
    if not isinstance(bbox, dict) or not {"x", "y", "w", "h"} <= set(bbox):
        return None
    try:
        return (
            float(bbox["x"]),
            float(bbox["y"]),
            float(bbox["w"]),
            float(bbox["h"]),
        )
    except (TypeError, ValueError):
        return None


def _is_click_driven_visual_surface(element: ElementSnapshot) -> bool:
    tag = (element.node_name or "").upper()
    handlers = {name.lower() for name in (element.handlers or {})}
    visual_tag = tag in {"CANVAS", "SVG", "G", "PATH", "LINE", "RECT", "CIRCLE", "ELLIPSE", "POLYGON", "POLYLINE"}
    drag_handlers = handlers & {"mousedown", "mousemove", "mouseup", "pointerdown", "pointermove", "pointerup", "dragstart"}
    return visual_tag and "click" in handlers and not drag_handlers


def _is_clickable_svg_leaf(element: ElementSnapshot) -> bool:
    tag = (element.node_name or "").upper()
    handlers = {name.lower() for name in (element.handlers or {})}
    return tag in {"CIRCLE", "ELLIPSE", "LINE", "PATH", "POLYGON", "POLYLINE", "RECT", "TEXT", "TSPAN"} and "click" in handlers


def _register_new_elements(
    resolved: list[dict], context: ToolContext, frame_id: str | None, frame_url: str | None
) -> None:
    """Register resolved interactive elements in the element index for immediate use."""
    for item in resolved:
        stable_id = item["stable_id"]
        if stable_id in context.element_index.elements:
            continue
        context.element_index.elements[stable_id] = ElementSnapshot(
            stable_id=stable_id,
            backend_node_id=item["backend_node_id"],
            node_name=item.get("tag", "").upper() or None,
            role=item.get("role") or None,
            name=item.get("name") or None,
            text=item.get("text") or None,
            bounding_box=_bbox_from_resolved_item(item),
            attributes=item.get("attrs") if isinstance(item.get("attrs"), dict) else {},
            frame_id=frame_id,
            frame_url=frame_url,
            frame_name=None,
            interactive_reason="mutation_detected",
            interactive_confidence=0.8,
        )


async def _collect_mutations_with_ids(
    session: CDPSession,
    context: ToolContext,
    settle_ms: int,
    *,
    frame_id: str | None,
    frame_url: str | None = None,
) -> dict | None:
    """Collect mutations and resolve stable IDs for new interactive elements."""
    mutations = await _collect_mutations(session, settle_ms)
    if mutations is None:
        return None
    await _resolve_new_interactive(session, mutations, frame_id)
    resolved = mutations.get("resolvedInteractive")
    if resolved:
        _register_new_elements(resolved, context, frame_id, frame_url)
    return mutations


def _fmt_text_item(item: Any) -> str:
    """Format a text item (string or {t, tag} dict) for diff output."""
    if isinstance(item, dict):
        text = (item.get("t") or "")[:250]
        tag = item.get("tag") or ""
        return f'"{text}" ({tag})' if tag else f'"{text}"'
    return f'"{str(item)[:250]}"'


_BOOL_ATTR_LABELS: dict[str, tuple[str, str]] = {
    # attr_name: (present_label, absent_label)
    "disabled": ("disabled", "enabled"),
    "hidden": ("hidden", "visible"),
    "checked": ("checked", "unchecked"),
    "selected": ("selected", "unselected"),
    "open": ("open", "closed"),
    "aria-disabled": ("disabled", "enabled"),
    "aria-hidden": ("hidden", "visible"),
    "aria-checked": ("checked", "unchecked"),
    "aria-selected": ("selected", "unselected"),
    "aria-expanded": ("expanded", "collapsed"),
}

def _fmt_attr_val(attr: str, raw_val: str | None, *, is_present: bool) -> str:
    """Format an attribute value for mutation feedback.

    For boolean HTML attributes (disabled, hidden, checked, etc.),
    returns semantic labels like 'disabled'/'enabled'.
    For string-valued attributes, returns the value as-is.
    """
    if raw_val is not None and raw_val != "":
        return raw_val
    labels = _BOOL_ATTR_LABELS.get(attr)
    if labels:
        return labels[0] if is_present else labels[1]
    return "set" if is_present else "removed"

def _fmt_form_val(raw_val: Any) -> str:
    if raw_val is None:
        return "unset"
    if raw_val == "":
        return '""'
    return str(raw_val)

def _build_change_lines(mutations: dict) -> list[str]:
    """Build diff-style change lines from mutation data."""
    lines: list[str] = []

    # URL change
    start_url = mutations.get("startUrl", "")
    current_url = mutations.get("currentUrl", "")
    if start_url and current_url and start_url != current_url:
        lines.append(f"  navigated to: {current_url}")

    # Build a set of (text_lower, tag) covered by resolved interactive elements
    # so we can suppress duplicate addedText entries for those.
    resolved_items = mutations.get("resolvedInteractive", [])
    resolved_text_keys: set[tuple[str, str]] = set()
    for item in resolved_items:
        text = (item.get("text") or "").strip().lower()
        tag = (item.get("tag") or "").strip().lower()
        if text:
            resolved_text_keys.add((text, tag))

    # Added text  (+) — skip entries that have a resolved interactive line
    for item in mutations.get("addedText", []):
        if isinstance(item, dict):
            t = (item.get("t") or "").strip().lower()
            tg = (item.get("tag") or "").strip().lower()
        else:
            t = str(item).strip().lower()
            tg = ""
        if (t, tg) in resolved_text_keys:
            continue
        lines.append(f"  + {_fmt_text_item(item)}")

    # Attribute changes  (~)
    for change in mutations.get("attrChanges", []):
        tag = change.get("tag", "?")
        attr = change.get("attr", "?")
        raw_old = change.get("old")
        raw_new = change.get("new")
        old = _fmt_attr_val(attr, raw_old, is_present=raw_old is not None)
        new = _fmt_attr_val(attr, raw_new, is_present=raw_new is not None)
        lines.append(f"  ~ {tag}[{attr}]: {old} -> {new}")

    # Form property changes are not always reflected as DOM attributes.
    for change in mutations.get("formChanges", []):
        tag = change.get("tag", "?")
        key = str(change.get("key") or "").strip()
        attr = change.get("attr", "?")
        old = _fmt_form_val(change.get("old"))
        new = _fmt_form_val(change.get("new"))
        key_hint = f" {key}" if key else ""
        lines.append(f"  ~ {tag}{key_hint}[{attr}]: {old} -> {new}")

    # Visual updates can add SVG/canvas children with no text.
    for tag in mutations.get("addedElements", [])[:8]:
        if tag:
            lines.append(f"  + element <{tag}>")

    # New interactive elements with IDs  (+ interactive)
    for item in resolved_items:
        sid = item.get("stable_id", "?")
        tag = item.get("tag", "?")
        attrs = item.get("attrs") if isinstance(item.get("attrs"), dict) else {}
        attr_bits = [
            f'{key}="{attrs[key]}"'
            for key in ("data-index", "id", "class", "x", "y", "cx", "cy", "width", "height")
            if attrs.get(key)
        ]
        attr_hint = f" ({' '.join(attr_bits)})" if attr_bits else ""
        label = item.get("role") or item.get("name") or item.get("text") or ""
        item_bbox = _bbox_from_resolved_item(item)
        bbox_hint = ""
        if item_bbox is not None:
            x, y, w, h = item_bbox
            bbox_hint = (
                f" bbox={int(round(x))},"
                f"{int(round(y))},"
                f"{int(round(w))},"
                f"{int(round(h))}"
            )
        if label:
            lines.append(f'  + interactive {sid}: {tag} "{label}"{attr_hint}{bbox_hint}')
        else:
            lines.append(f"  + interactive {sid}: {tag}{attr_hint}{bbox_hint}")

    # Removed text  (-)
    for item in mutations.get("removedText", []):
        lines.append(f"  - {_fmt_text_item(item)}")

    for tag in mutations.get("removedElements", [])[:8]:
        if tag:
            lines.append(f"  - element <{tag}>")

    return lines


def _format_verification(mutations: dict | None, base_message: str) -> str:
    """Append a multi-line diff-style DOM-change summary to the base tool result message."""
    if mutations is None:
        return base_message

    change_lines = _build_change_lines(mutations)

    if change_lines:
        return base_message + "\nDOM changes:\n" + "\n".join(change_lines)
    else:
        return base_message + "\nNo DOM changes."

def _format_wait_message(wait_ms: int, mutations: dict | None) -> str:
    base = f"Waited {wait_ms}ms."

    if not mutations:
        return base + "\nNo DOM changes."

    change_lines = _build_change_lines(mutations)

    if change_lines:
        return base + "\nDOM changes:\n" + "\n".join(change_lines)
    else:
        return base + "\nNo DOM changes."


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




_CLICK_BY_TEXT_JS = """
(args) => {
    const [label, tagName] = args;
    const candidates = tagName
        ? document.querySelectorAll(tagName)
        : document.querySelectorAll('button, a, [role="button"], input[type="submit"]');
    for (const el of candidates) {
        if (el.textContent.trim().includes(label)) {
            el.scrollIntoView({block: 'center', inline: 'center'});
            el.click();
            return true;
        }
    }
    return false;
}
"""


async def _select_option_element(
    element: ElementSnapshot,
    session: CDPSession,
    context: ToolContext,
) -> ToolResult:
    result = await _call_on_node(
        int(element.backend_node_id or 0),
        session,
        """
        function () {
            const option = this;
            const select = option.closest && option.closest('select');
            if (!select) {
                return { ok: false, message: 'Option has no parent select' };
            }
            option.scrollIntoView({block: 'center', inline: 'center'});
            const options = Array.from(select.options || []);
            const index = options.indexOf(option);
            if (index < 0) {
                return { ok: false, message: 'Option is not in parent select' };
            }
            select.selectedIndex = index;
            option.selected = true;
            if (option.value !== undefined) {
                select.value = option.value;
            }
            select.dispatchEvent(new Event('input', { bubbles: true }));
            select.dispatchEvent(new Event('change', { bubbles: true }));
            return {
                ok: true,
                value: select.value,
                text: (option.textContent || '').trim(),
                selectedIndex: select.selectedIndex
            };
        }
        """,
    )
    details = (result or {}).get("result", {}).get("value")
    if not isinstance(details, dict) or not details.get("ok"):
        await _collect_mutations(session, settle_ms=50)
        message = "Select option failed"
        if isinstance(details, dict) and details.get("message"):
            message = str(details["message"])
        return ToolResult(ok=False, message=message)

    mutations = await _collect_mutations_with_ids(
        session,
        context,
        context.timing.settle_ms,
        frame_id=element.frame_id,
        frame_url=element.frame_url,
    )
    selected_text = str(details.get("text") or "")
    selected_value = str(details.get("value") or "")
    selected_index = details.get("selectedIndex")
    base = (
        f"Selected option {element.stable_id}"
        f' text="{selected_text}" value="{selected_value}" index={selected_index}'
    )
    return ToolResult(ok=True, message=_format_verification(mutations, base))


async def click_element(element_id: str, context: ToolContext) -> ToolResult:
    context.last_tool = "click_element"
    context.last_element_id = element_id
    element = _resolve_element(element_id, context)
    if not element or not element.backend_node_id:
        return ToolResult(ok=False, message=_unknown_element_message(element_id))
    frame_error = _active_frame_error(element, context)
    if frame_error:
        return frame_error
    session = await _session_for_element(element, context)
    await _inject_observer(session)
    if (element.node_name or "").upper() == "OPTION":
        return await _select_option_element(element, session, context)
    result = await _call_on_node(
        element.backend_node_id,
        session,
        """
        function () {
            this.scrollIntoView({block: 'center', inline: 'center'});
            const tag = (this.tagName || '').toUpperCase();
            const isSvg = !!this.ownerSVGElement || tag === 'SVG';
            const href = tag === 'A' ? (this.getAttribute('href') || '') : '';
            if (!isSvg && typeof this.click === 'function') {
                if (tag === 'A' && (href === '#' || href.startsWith('#'))) {
                    const rect = this.getBoundingClientRect();
                    const clientX = rect.left + rect.width / 2;
                    const clientY = rect.top + rect.height / 2;
                    const opts = {
                        bubbles: true,
                        cancelable: true,
                        view: window,
                        button: 0,
                        buttons: 1,
                        clientX,
                        clientY,
                        screenX: window.screenX + clientX,
                        screenY: window.screenY + clientY,
                    };
                    this.dispatchEvent(new MouseEvent('mouseover', opts));
                    this.dispatchEvent(new MouseEvent('mousemove', opts));
                    this.dispatchEvent(new MouseEvent('mousedown', opts));
                    this.dispatchEvent(new MouseEvent('mouseup', {...opts, buttons: 0}));
                    this.dispatchEvent(new MouseEvent('click', {...opts, buttons: 0}));
                    return true;
                }
                this.click();
                return true;
            }
            const rect = this.getBoundingClientRect();
            const clientX = rect.left + rect.width / 2;
            const clientY = rect.top + rect.height / 2;
            const opts = {
                bubbles: true,
                cancelable: true,
                view: window,
                button: 0,
                buttons: 1,
                clientX,
                clientY,
                screenX: window.screenX + clientX,
                screenY: window.screenY + clientY,
            };
            this.dispatchEvent(new MouseEvent('mouseover', opts));
            this.dispatchEvent(new MouseEvent('mousemove', opts));
            this.dispatchEvent(new MouseEvent('mousedown', opts));
            this.dispatchEvent(new MouseEvent('mouseup', {...opts, buttons: 0}));
            this.dispatchEvent(new MouseEvent('click', {...opts, buttons: 0}));
            return true;
        }
        """,
    )
    if not result:
        # Stale node — try text-based re-lookup in the live DOM
        label = element.name or (element.text or "").strip()
        tag = (element.node_name or "").lower() or None
        if label:
            try:
                re_clicked = await context.page.evaluate(
                    _CLICK_BY_TEXT_JS, [label, tag]
                )
            except Exception:
                re_clicked = False
            if re_clicked:
                mutations = await _collect_mutations_with_ids(
                    session, context, context.timing.settle_ms,
                    frame_id=element.frame_id, frame_url=element.frame_url,
                )
                message = _format_verification(
                    mutations, f"Re-found and clicked element with text '{label}'"
                )
                return ToolResult(ok=True, message=message)
        await _collect_mutations(session, settle_ms=50)
        return ToolResult(ok=False, message="Click failed")
    mutations = await _collect_mutations_with_ids(
        session, context, context.timing.settle_ms,
        frame_id=element.frame_id, frame_url=element.frame_url,
    )
    message = _format_verification(mutations, f"Clicked {element_id}")
    return ToolResult(ok=True, message=message)


async def click_at(
    element_id: str,
    x: float,
    y: float,
    context: ToolContext,
) -> ToolResult:
    context.last_tool = "click_at"
    context.last_element_id = element_id
    element = _resolve_element(element_id, context)
    if not element or not element.backend_node_id:
        return ToolResult(ok=False, message=_unknown_element_message(element_id))
    frame_error = _active_frame_error(element, context)
    if frame_error:
        return frame_error
    session = await _session_for_element(element, context)
    await _inject_observer(session)

    if _is_clickable_svg_leaf(element):
        result = await _call_on_node(
            element.backend_node_id,
            session,
            """
            function () {
                this.scrollIntoView({block: 'center', inline: 'center'});
                const rect = this.getBoundingClientRect();
                const clientX = rect.left + rect.width / 2;
                const clientY = rect.top + rect.height / 2;
                const opts = {
                    bubbles: true,
                    cancelable: true,
                    view: window,
                    button: 0,
                    buttons: 1,
                    clientX,
                    clientY,
                    screenX: window.screenX + clientX,
                    screenY: window.screenY + clientY,
                };
                this.dispatchEvent(new MouseEvent('mouseover', opts));
                this.dispatchEvent(new MouseEvent('mousemove', opts));
                this.dispatchEvent(new MouseEvent('mousedown', opts));
                this.dispatchEvent(new MouseEvent('mouseup', {...opts, buttons: 0}));
                this.dispatchEvent(new MouseEvent('click', {...opts, buttons: 0}));
                return true;
            }
            """,
        )
        if not result:
            await _collect_mutations(session, settle_ms=50)
            return ToolResult(ok=False, message="Click-at failed: SVG element is no longer available")
        mutations = await _collect_mutations_with_ids(
            session, context, context.timing.settle_ms,
            frame_id=element.frame_id, frame_url=element.frame_url,
        )
        base_msg = f"Clicked {element_id} SVG element"
        return ToolResult(ok=True, message=_format_verification(mutations, base_msg))

    info = await _element_rect_info(element.backend_node_id, session)
    value = (info or {}).get("result", {}).get("value")
    if not value:
        await _collect_mutations(session, settle_ms=50)
        return ToolResult(ok=False, message="Click-at failed: cannot locate element")

    width = float(value.get("width") or 0)
    height = float(value.get("height") or 0)
    if width <= 0 or height <= 0:
        await _collect_mutations(session, settle_ms=50)
        return ToolResult(ok=False, message="Click-at failed: element has no visible area")

    left = float(value.get("left") or 0)
    top = float(value.get("top") or 0)
    before_left = float(value.get("beforeLeft", left) or 0)
    before_top = float(value.get("beforeTop", top) or 0)
    before_width = float(value.get("beforeWidth", width) or 0)
    before_height = float(value.get("beforeHeight", height) or 0)
    raw_x = float(x)
    raw_y = float(y)
    if 0 <= raw_x <= width and 0 <= raw_y <= height:
        local_x = raw_x
        local_y = raw_y
        page_x = left + local_x
        page_y = top + local_y
        coord_mode = "relative"
    elif (
        before_width > 0
        and before_height > 0
        and before_left <= raw_x <= before_left + before_width
        and before_top <= raw_y <= before_top + before_height
    ):
        local_x = raw_x - before_left
        local_y = raw_y - before_top
        page_x = left + local_x
        page_y = top + local_y
        coord_mode = "viewport"
    else:
        local_x = max(0.0, min(raw_x, width))
        local_y = max(0.0, min(raw_y, height))
        page_x = left + local_x
        page_y = top + local_y
        coord_mode = "relative"
    try:
        await session.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": page_x, "y": page_y, "button": "none"},
        )
        await session.send(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": page_x, "y": page_y, "button": "left", "clickCount": 1},
        )
        await session.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": page_x, "y": page_y, "button": "left", "clickCount": 1},
        )
    except Exception as exc:
        await _collect_mutations(session, settle_ms=50)
        return ToolResult(ok=False, message=f"Click-at failed: {exc}")

    mutations = await _collect_mutations_with_ids(
        session, context, context.timing.settle_ms,
        frame_id=element.frame_id, frame_url=element.frame_url,
    )
    base_msg = f"Clicked {element_id} at ({local_x:.1f}, {local_y:.1f}) {coord_mode}"
    return ToolResult(ok=True, message=_format_verification(mutations, base_msg))


async def hover_element(element_id: str, context: ToolContext, *, duration_ms: int = 2000) -> ToolResult:
    context.last_tool = "hover_element"
    context.last_element_id = element_id
    element = _resolve_element(element_id, context)
    if not element or not element.backend_node_id:
        return ToolResult(ok=False, message=_unknown_element_message(element_id))
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

    mutations = await _collect_mutations_with_ids(
        session, context, context.timing.settle_ms,
        frame_id=element.frame_id, frame_url=element.frame_url,
    )
    message = _format_verification(mutations, f"Hovered {element_id} for {clamped}ms")
    return ToolResult(ok=True, message=message)


async def focus_element(element_id: str, context: ToolContext) -> ToolResult:
    context.last_tool = "focus_element"
    context.last_element_id = element_id
    element = _resolve_element(element_id, context)
    if not element or not element.backend_node_id:
        return ToolResult(ok=False, message=_unknown_element_message(element_id))
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
            const before = document.activeElement;
            if (typeof this.focus === 'function') this.focus();
            const after = document.activeElement;
            const label = (after && [
                after.tagName || '',
                after.id ? '#' + after.id : '',
                after.getAttribute && after.getAttribute('name') ? `[name="${after.getAttribute('name')}"]` : ''
            ].join('')) || '';
            return {
                active: after === this,
                changed: before !== after,
                activeLabel: label,
            };
        }
        """,
    )
    if not result:
        await _collect_mutations(session, settle_ms=50)
        return ToolResult(ok=False, message="Focus failed")
    value = result.get("result", {}).get("value") or {}
    mutations = await _collect_mutations_with_ids(
        session, context, context.timing.settle_ms,
        frame_id=element.frame_id, frame_url=element.frame_url,
    )
    active = bool(value.get("active"))
    changed = bool(value.get("changed"))
    label = value.get("activeLabel") or "unknown"
    base_msg = f"Focused {element_id}. Focus changed: {str(changed).lower()}. Active element: {label}"
    if not active:
        base_msg = f"Focus failed: active element is {label}, not {element_id}"
    return ToolResult(ok=active, message=_format_verification(mutations, base_msg))


async def type_text(element_id: str, text: str, context: ToolContext) -> ToolResult:
    context.last_tool = "type_text"
    context.last_element_id = element_id
    element = _resolve_element(element_id, context)
    if not element or not element.backend_node_id:
        return ToolResult(ok=False, message=_unknown_element_message(element_id))
    frame_error = _active_frame_error(element, context)
    if frame_error:
        return frame_error
    session = await _session_for_element(element, context)
    await _inject_observer(session)
    try:
        previous_value = await _read_input_value(element.backend_node_id, session)
    except Exception:
        logger.debug("Failed to read live input value before typing", exc_info=True)
        previous_value = (element.attributes or {}).get("value")
    if not await _dom_focus(element.backend_node_id, session):
        await _collect_mutations(session, settle_ms=50)
        return ToolResult(ok=False, message="Type failed: element not focusable")
    typed_with_keyboard, keyboard_error = await _type_text_with_keyboard(context.page, text)
    if not typed_with_keyboard and not await _insert_text(session, text):
        await _collect_mutations(session, settle_ms=50)
        reason = f": {keyboard_error}" if keyboard_error else ""
        return ToolResult(ok=False, message=f"Type failed{reason}")
    typing_settle_ms = max(context.timing.settle_ms, 600)
    mutations = await _collect_mutations_with_ids(
        session, context, typing_settle_ms,
        frame_id=element.frame_id, frame_url=element.frame_url,
    )
    current_value = await _read_input_value(element.backend_node_id, session)
    used_value_fallback = False
    if current_value is not None and current_value != text:
        if await _set_text_value(element.backend_node_id, session, text):
            used_value_fallback = True
            fallback_mutations = await _collect_mutations_with_ids(
                session, context, typing_settle_ms,
                frame_id=element.frame_id, frame_url=element.frame_url,
            )
            if fallback_mutations:
                mutations = fallback_mutations
            current_value = await _read_input_value(element.backend_node_id, session)
    base_msg = f"Typed into {element_id}"
    if current_value is not None:
        display = current_value[:250] + "..." if len(current_value) > 250 else current_value
        base_msg = f"Typed into {element_id}. Current value: \"{display}\""
        if previous_value is not None:
            prev_display = previous_value[:250] + "..." if len(previous_value) > 250 else previous_value
            base_msg += f'. Previous value: "{prev_display}"'
        if used_value_fallback:
            base_msg += " (set value via form events after keyboard input did not stick)"
    elif typed_with_keyboard:
        base_msg += " using keyboard events"
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
    await _inject_observer(session)

    base_msg = f"Dragged {source_id} -> {target_id}"

    # DOM-based DragEvent dispatch — operates directly on DOM nodes, not coordinates.
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

    await asyncio.sleep(context.timing.drag_phase_interval_ms * 2 / 1000)

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
    mutations = await _collect_mutations_with_ids(
        session, context, context.timing.settle_ms,
        frame_id=source.frame_id, frame_url=source.frame_url,
    )
    if not _build_change_lines(mutations or {}):
        await _inject_observer(session)
        ok, coords, error = await _dispatch_pointer_drag_between_nodes(
            source.backend_node_id,
            target.backend_node_id,
            session,
            steps=18,
        )
        if not ok or coords is None:
            return ToolResult(
                ok=False,
                message=(
                    f"{base_msg}\nNo observable change followed DOM drag/drop; "
                    f"pointer fallback failed: {error or 'unknown error'}"
                ),
            )
        fallback_mutations = await _collect_mutations_with_ids(
            session, context, context.timing.settle_ms,
            frame_id=source.frame_id, frame_url=source.frame_url,
        )
        fallback_msg = (
            f"{base_msg} using pointer fallback from viewport "
            f"({coords['start_viewport_x']:.1f}, {coords['start_viewport_y']:.1f}) -> "
            f"({coords['end_viewport_x']:.1f}, {coords['end_viewport_y']:.1f})"
        )
        return ToolResult(ok=True, message=_format_verification(fallback_mutations, fallback_msg))
    return ToolResult(ok=True, message=_format_verification(mutations, base_msg))


async def pointer_drag(
    element_id: str,
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    context: ToolContext,
    *,
    steps: int = 12,
) -> ToolResult:
    context.last_tool = "pointer_drag"
    context.last_element_id = element_id
    element = _resolve_element(element_id, context)
    if not element or not element.backend_node_id:
        return ToolResult(ok=False, message=_unknown_element_message(element_id))
    frame_error = _active_frame_error(element, context)
    if frame_error:
        return frame_error
    session = await _session_for_element(element, context)
    await _inject_observer(session)
    ok, coords, error = await _dispatch_pointer_drag_local(
        element.backend_node_id,
        session,
        start_x=start_x,
        start_y=start_y,
        end_x=end_x,
        end_y=end_y,
        steps=steps,
        allow_outside=True,
    )
    if not ok or coords is None:
        await _collect_mutations(session, settle_ms=50)
        return ToolResult(ok=False, message=f"Pointer drag failed: {error or 'unknown error'}")
    mutations = await _collect_mutations_with_ids(
        session, context, context.timing.settle_ms,
        frame_id=element.frame_id, frame_url=element.frame_url,
    )
    base_msg = (
        f"Pointer dragged {element_id} from local "
        f"({coords['start_local_x']:.1f}, {coords['start_local_y']:.1f}) "
        f"to ({coords['end_local_x']:.1f}, {coords['end_local_y']:.1f}); viewport "
        f"({coords['start_viewport_x']:.1f}, {coords['start_viewport_y']:.1f}) -> "
        f"({coords['end_viewport_x']:.1f}, {coords['end_viewport_y']:.1f})"
    )
    return ToolResult(ok=True, message=_format_verification(mutations, base_msg))


async def set_slider_value(element_id: str, value: float, context: ToolContext) -> ToolResult:
    context.last_tool = "set_slider_value"
    context.last_element_id = element_id
    element = _resolve_element(element_id, context)
    if not element or not element.backend_node_id:
        return ToolResult(ok=False, message=_unknown_element_message(element_id))
    frame_error = _active_frame_error(element, context)
    if frame_error:
        return frame_error
    session = await _session_for_element(element, context)
    await _inject_observer(session)
    desired = float(value)
    direct = await _call_on_node(
        element.backend_node_id,
        session,
        """
        function (desired) {
            const desiredNumber = Number(desired);
            const desiredString = String(desired);
            const closeEnough = (value) => {
                if (value === undefined || value === null || value === '') return false;
                const numberValue = Number(value);
                if (Number.isFinite(numberValue) && Number.isFinite(desiredNumber)) {
                    return Math.abs(numberValue - desiredNumber) < 1e-6;
                }
                return String(value) === desiredString;
            };
            const jq = window.jQuery || window.$;
            const sliderRoot = (
                this.closest && (
                    this.closest('.ui-slider') ||
                    this.closest('[role="slider"]')
                )
            ) || this;
            const initializedJQuerySlider = (el) => {
                if (!jq || !el) return false;
                try {
                    return typeof jq(el).slider === 'function' && (
                        !!jq(el).data('ui-slider') ||
                        !!jq(el).data('slider') ||
                        el.classList.contains('ui-slider')
                    );
                } catch (_e) {
                    return false;
                }
            };
            const readState = (target) => {
                let raw = '';
                let min = null;
                let max = null;
                let method = '';
                if (initializedJQuerySlider(sliderRoot)) {
                    try {
                        raw = jq(sliderRoot).slider('value');
                        min = jq(sliderRoot).slider('option', 'min');
                        max = jq(sliderRoot).slider('option', 'max');
                        method = 'jquery-ui';
                    } catch (_e) {}
                }
                if ((raw === '' || raw === undefined || raw === null) && 'value' in target) {
                    raw = target.value;
                    min = target.min || min;
                    max = target.max || max;
                    method = method || 'native-value';
                }
                if ((raw === '' || raw === undefined || raw === null) && target.getAttribute) {
                    raw = target.getAttribute('aria-valuenow') || sliderRoot.getAttribute('aria-valuenow') || '';
                    min = target.getAttribute('aria-valuemin') || sliderRoot.getAttribute('aria-valuemin') || min;
                    max = target.getAttribute('aria-valuemax') || sliderRoot.getAttribute('aria-valuemax') || max;
                    method = method || 'aria';
                }
                return {
                    value: raw === undefined || raw === null ? '' : String(raw),
                    min: min === undefined || min === null || min === '' ? null : Number(min),
                    max: max === undefined || max === null || max === '' ? null : Number(max),
                    method,
                };
            };
            sliderRoot.scrollIntoView({block: 'center', inline: 'center'});
            const before = readState(this);
            let ok = false;
            let method = '';
            if (initializedJQuerySlider(sliderRoot)) {
                try {
                    jq(sliderRoot).slider('value', desiredNumber);
                    jq(sliderRoot).trigger('slidechange');
                    sliderRoot.dispatchEvent(new Event('input', { bubbles: true }));
                    sliderRoot.dispatchEvent(new Event('change', { bubbles: true }));
                    method = 'jquery-ui';
                } catch (_e) {}
            }
            if (!method && 'value' in this) {
                const tag = (this.tagName || '').toUpperCase();
                const proto = tag === 'INPUT' ? window.HTMLInputElement.prototype : null;
                const descriptor = proto ? Object.getOwnPropertyDescriptor(proto, 'value') : null;
                if (descriptor && descriptor.set) descriptor.set.call(this, String(desired));
                else this.value = String(desired);
                this.dispatchEvent(new Event('input', { bubbles: true }));
                this.dispatchEvent(new Event('change', { bubbles: true }));
                method = 'native-value';
            }
            if (!method && (this.getAttribute('role') === 'slider' || sliderRoot.getAttribute('role') === 'slider')) {
                const ariaTarget = sliderRoot.getAttribute('role') === 'slider' ? sliderRoot : this;
                ariaTarget.setAttribute('aria-valuenow', String(desired));
                ariaTarget.dispatchEvent(new Event('input', { bubbles: true }));
                ariaTarget.dispatchEvent(new Event('change', { bubbles: true }));
                method = 'aria';
            }
            const after = readState(this);
            ok = closeEnough(after.value);
            return {
                ok,
                before: before.value,
                after: after.value,
                min: after.min ?? before.min,
                max: after.max ?? before.max,
                method: method || after.method || before.method,
                root: sliderRoot === this ? 'self' : 'ancestor',
            };
        }
        """,
        [{"value": desired}],
    )
    direct_value = (direct or {}).get("result", {}).get("value") or {}
    mutations = await _collect_mutations_with_ids(
        session, context, context.timing.settle_ms,
        frame_id=element.frame_id, frame_url=element.frame_url,
    )
    if direct_value.get("ok") or str(direct_value.get("after")) == str(desired):
        base_msg = (
            f"Set slider {element_id} to {desired:g}. "
            f"Previous value: \"{direct_value.get('before', '')}\". "
            f"Current value: \"{direct_value.get('after', '')}\""
        )
        return ToolResult(ok=True, message=_format_verification(mutations, base_msg))

    attrs = element.attributes or {}
    min_value = float(direct_value.get("min") or attrs.get("aria-valuemin") or attrs.get("min") or 0)
    max_value = float(direct_value.get("max") or attrs.get("aria-valuemax") or attrs.get("max") or 100)
    if max_value <= min_value:
        max_value = min_value + 100
    ratio = max(0.0, min((desired - min_value) / (max_value - min_value), 1.0))
    info = await _element_rect_info(element.backend_node_id, session)
    rect = (info or {}).get("result", {}).get("value") or {}
    width = float(rect.get("width") or 0)
    height = float(rect.get("height") or 0)
    if width <= 0 or height <= 0:
        return ToolResult(ok=False, message="Set slider failed: element has no visible area")
    ok, coords, error = await _dispatch_pointer_drag_local(
        element.backend_node_id,
        session,
        start_x=width / 2,
        start_y=height / 2,
        end_x=ratio * width,
        end_y=height / 2,
        steps=16,
    )
    if not ok:
        return ToolResult(ok=False, message=f"Set slider failed: {error or 'pointer drag failed'}")
    pointer_mutations = await _collect_mutations_with_ids(
        session, context, context.timing.settle_ms,
        frame_id=element.frame_id, frame_url=element.frame_url,
    )
    if pointer_mutations:
        mutations = pointer_mutations
    reread = await _call_on_node(
        element.backend_node_id,
        session,
        """
        function () {
            return this.value !== undefined ? String(this.value) : (this.getAttribute('aria-valuenow') || '');
        }
        """,
    )
    current_after = (reread or {}).get("result", {}).get("value")
    if current_after is None:
        current_after = direct_value.get("after", "")
    base_msg = (
        f"Set slider {element_id} toward {desired:g} using pointer ratio {ratio:.3f}. "
        f"Previous value: \"{direct_value.get('before', '')}\". "
        f"Current value: \"{current_after}\""
    )
    if coords:
        base_msg += f". Drag ended at local ({coords['end_local_x']:.1f}, {coords['end_local_y']:.1f})"
    try:
        verified = abs(float(current_after) - desired) < 1e-6
    except (TypeError, ValueError):
        verified = str(current_after) == str(desired)
    if not verified:
        return ToolResult(ok=False, message=_format_verification(mutations, f"Set slider failed verification. {base_msg}"))
    return ToolResult(ok=True, message=_format_verification(mutations, base_msg))


def _dimension_changed_in_direction(
    before: float,
    after: float,
    delta: float,
    *,
    tolerance: float = 0.5,
) -> bool:
    if abs(float(delta)) <= tolerance:
        return True
    if float(delta) < 0:
        return float(after) < float(before) - tolerance
    return float(after) > float(before) + tolerance


def _resize_change_verified(
    *,
    before_width: float,
    before_height: float,
    after_width: float,
    after_height: float,
    delta_width: float,
    delta_height: float,
) -> bool:
    if abs(float(delta_width)) <= 0.5 and abs(float(delta_height)) <= 0.5:
        return False
    return _dimension_changed_in_direction(
        before_width, after_width, delta_width
    ) and _dimension_changed_in_direction(before_height, after_height, delta_height)


def _resize_dimensions_message(
    *,
    before_width: float,
    before_height: float,
    after_width: float,
    after_height: float,
) -> str:
    return (
        f"dimensions {before_width:.1f}x{before_height:.1f} -> "
        f"{after_width:.1f}x{after_height:.1f}"
    )


async def _read_element_rect(
    backend_node_id: int,
    session: CDPSession,
) -> dict[str, float] | None:
    info = await _element_rect_info(backend_node_id, session)
    rect = (info or {}).get("result", {}).get("value") or {}
    try:
        width = float(rect.get("width") or 0)
        height = float(rect.get("height") or 0)
        left = float(rect.get("left") or 0)
        top = float(rect.get("top") or 0)
    except (TypeError, ValueError):
        return None
    return {"left": left, "top": top, "width": width, "height": height}


def _is_unqualified_text_selection_control(element: ElementSnapshot, text: str | None) -> bool:
    if (text or "").strip():
        return False
    tag = (element.node_name or "").upper()
    attrs = element.attributes or {}
    input_type = (attrs.get("type") or "").lower()
    if tag == "BUTTON" or tag == "SELECT" or tag == "OPTION":
        return True
    return tag == "INPUT" and input_type in {"button", "checkbox", "color", "file", "radio", "reset", "submit"}


async def resize_element(
    element_id: str,
    delta_width: float,
    delta_height: float,
    context: ToolContext,
) -> ToolResult:
    context.last_tool = "resize_element"
    context.last_element_id = element_id
    element = _resolve_element(element_id, context)
    if not element or not element.backend_node_id:
        return ToolResult(ok=False, message=_unknown_element_message(element_id))
    frame_error = _active_frame_error(element, context)
    if frame_error:
        return frame_error
    session = await _session_for_element(element, context)
    await _inject_observer(session)
    rect = await _read_element_rect(element.backend_node_id, session)
    width = float((rect or {}).get("width") or 0)
    height = float((rect or {}).get("height") or 0)
    if width <= 0 or height <= 0:
        return ToolResult(ok=False, message="Resize failed: element has no visible area")
    ok, coords, error = await _dispatch_pointer_drag_local(
        element.backend_node_id,
        session,
        start_x=width - 2,
        start_y=height - 2,
        end_x=width + float(delta_width),
        end_y=height + float(delta_height),
        steps=12,
        allow_outside=True,
    )
    if not ok:
        await _collect_mutations(session, settle_ms=50)
        return ToolResult(ok=False, message=f"Resize failed: {error or 'pointer drag failed'}")
    mutations = await _collect_mutations_with_ids(
        session, context, context.timing.settle_ms,
        frame_id=element.frame_id, frame_url=element.frame_url,
    )
    after_rect = await _read_element_rect(element.backend_node_id, session)
    after_width = float((after_rect or {}).get("width") or 0)
    after_height = float((after_rect or {}).get("height") or 0)
    dimensions_msg = _resize_dimensions_message(
        before_width=width,
        before_height=height,
        after_width=after_width,
        after_height=after_height,
    )
    base_msg = f"Resized {element_id} by ({delta_width:g}, {delta_height:g}); {dimensions_msg}"
    if coords:
        base_msg += f" using handle drag to local ({coords['end_local_x']:.1f}, {coords['end_local_y']:.1f})"
    if _resize_change_verified(
        before_width=width,
        before_height=height,
        after_width=after_width,
        after_height=after_height,
        delta_width=delta_width,
        delta_height=delta_height,
    ):
        return ToolResult(ok=True, message=_format_verification(mutations, base_msg))

    await _inject_observer(session)
    fallback = await _call_on_node(
        element.backend_node_id,
        session,
        """
        function (deltaWidth, deltaHeight) {
            const dw = Number(deltaWidth) || 0;
            const dh = Number(deltaHeight) || 0;
            this.scrollIntoView({block: 'center', inline: 'center'});
            const wrapper = this.closest && this.closest('.ui-wrapper, .ui-resizable');
            const base = wrapper && wrapper !== this ? wrapper : this;
            const before = base.getBoundingClientRect();
            const targetWidth = Math.max(1, before.width + dw);
            const targetHeight = Math.max(1, before.height + dh);
            const px = (value) => `${Math.max(1, Math.round(value))}px`;
            const targets = wrapper && wrapper !== this ? [wrapper, this] : [this];
            for (const target of targets) {
                if (dw !== 0) target.style.width = px(targetWidth);
                if (dh !== 0) target.style.height = px(targetHeight);
            }
            const jq = window.jQuery || window.$;
            if (jq) {
                try {
                    if (dw !== 0) jq(this).width(targetWidth);
                    if (dh !== 0) jq(this).height(targetHeight);
                    jq(this).trigger('resize').trigger('resizestop');
                    if (wrapper && wrapper !== this) {
                        if (dw !== 0) jq(wrapper).width(targetWidth);
                        if (dh !== 0) jq(wrapper).height(targetHeight);
                        jq(wrapper).trigger('resize').trigger('resizestop');
                    }
                } catch (_e) {}
            }
            this.dispatchEvent(new Event('input', {bubbles: true}));
            this.dispatchEvent(new Event('change', {bubbles: true}));
            this.dispatchEvent(new Event('resize', {bubbles: true}));
            window.dispatchEvent(new Event('resize'));
            const after = base.getBoundingClientRect();
            return {
                beforeWidth: before.width,
                beforeHeight: before.height,
                afterWidth: after.width,
                afterHeight: after.height,
                targetWidth,
                targetHeight,
                wrapperTag: wrapper && wrapper !== this ? (wrapper.tagName || '').toLowerCase() : '',
            };
        }
        """,
        [{"value": float(delta_width)}, {"value": float(delta_height)}],
    )
    fallback_value = (fallback or {}).get("result", {}).get("value") or {}
    fallback_mutations = await _collect_mutations_with_ids(
        session, context, context.timing.settle_ms,
        frame_id=element.frame_id, frame_url=element.frame_url,
    )
    fallback_after_width = float(fallback_value.get("afterWidth") or after_width)
    fallback_after_height = float(fallback_value.get("afterHeight") or after_height)
    fallback_msg = (
        f"Resize fallback set inline dimensions for {element_id}; "
        + _resize_dimensions_message(
            before_width=width,
            before_height=height,
            after_width=fallback_after_width,
            after_height=fallback_after_height,
        )
    )
    if fallback_value.get("wrapperTag"):
        fallback_msg += f" via {fallback_value['wrapperTag']} wrapper"
    if _resize_change_verified(
        before_width=width,
        before_height=height,
        after_width=fallback_after_width,
        after_height=fallback_after_height,
        delta_width=delta_width,
        delta_height=delta_height,
    ):
        return ToolResult(ok=True, message=_format_verification(fallback_mutations, fallback_msg))

    failed_msg = (
        "Resize failed verification: requested dimensions did not change in the requested direction. "
        f"Pointer attempt: {dimensions_msg}. "
        + _resize_dimensions_message(
            before_width=width,
            before_height=height,
            after_width=fallback_after_width,
            after_height=fallback_after_height,
        )
    )
    return ToolResult(ok=False, message=_format_verification(fallback_mutations or mutations, failed_msg))


async def draw(
    element_id: str,
    path: list[list[float]],
    context: ToolContext,
) -> ToolResult:
    context.last_tool = "draw"
    context.last_element_id = element_id
    element = _resolve_element(element_id, context)
    if not element or not element.backend_node_id:
        return ToolResult(ok=False, message=_unknown_element_message(element_id))
    frame_error = _active_frame_error(element, context)
    if frame_error:
        return frame_error
    if len(path) < 2:
        return ToolResult(ok=False, message="Draw requires at least 2 points")

    session = await _session_for_element(element, context)
    await _inject_observer(session)

    base_msg = f"Drew path with {len(path)} points on {element_id}"

    # DOM-first — dispatch synthetic MouseEvents directly on the element.
    # Works through overlays and computes accurate clientX/clientY from
    # a fresh getBoundingClientRect() in the same synchronous JS call.
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
    if result:
        mutations = await _collect_mutations_with_ids(
            session, context, context.timing.draw_settle_ms,
            frame_id=element.frame_id, frame_url=element.frame_url,
        )
        if _build_change_lines(mutations or {}):
            return ToolResult(ok=True, message=_format_verification(mutations, base_msg))
        if _is_click_driven_visual_surface(element):
            return ToolResult(
                ok=False,
                message=(
                    f"{base_msg}\nNo observable drawing change followed. "
                    "This visual target exposes a click handler; use click_at(element_id, x, y) "
                    "for a single target point instead of draw."
                ),
            )
        base_msg = f"{base_msg} using pointer fallback after synthetic events made no observable change"

    # CDP coordinate fallback — for apps that ignore synthetic DOM events
    # but respond to real input events (e.g. some canvas libraries).
    info = await _viewport_info(element.backend_node_id, session)
    if not info:
        await _collect_mutations(session, settle_ms=50)
        return ToolResult(ok=False, message="Draw failed: cannot locate element")
    value = info.get("result", {}).get("value")
    if not value:
        await _collect_mutations(session, settle_ms=50)
        return ToolResult(ok=False, message="Draw failed: cannot locate element")

    el_left = value["x"] - value["width"] / 2
    el_top = value["y"] - value["height"] / 2
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
            await asyncio.sleep(context.timing.draw_point_interval_ms / 1000)
        last_x = el_left + path[-1][0]
        last_y = el_top + path[-1][1]
        await session.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": last_x, "y": last_y, "button": "left", "clickCount": 1},
        )
        mutations = await _collect_mutations_with_ids(
            session, context, context.timing.draw_settle_ms,
            frame_id=element.frame_id, frame_url=element.frame_url,
        )
        return ToolResult(ok=True, message=_format_verification(mutations, base_msg))
    except Exception:
        await _collect_mutations(session, settle_ms=50)
        return ToolResult(ok=False, message="Draw failed")


async def wait(milliseconds: int, context: ToolContext) -> ToolResult:
    context.last_tool = "wait"
    context.last_element_id = None
    session = await _session_for_active_frame(context)
    clamped = max(0, min(milliseconds, 10_000))
    buffered = min(clamped + _WAIT_BUFFER_MS, 10_000)
    injected = await _inject_observer(session)
    await asyncio.sleep(buffered / 1000)
    mutations = await _collect_mutations_with_ids(
        session, context, context.timing.settle_ms,
        frame_id=context.active_frame_id,
    ) if injected else None
    return ToolResult(ok=True, message=_format_wait_message(buffered, mutations))


_WATCH_MAX_TIMEOUT_MS = 10_000

_WATCH_FOR_TEXT_JS = """
([text, timeoutMs]) => new Promise(resolve => {
    const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','META','LINK','HEAD']);
    const CLICKABLE = 'button,a,input,select,textarea,option,summary,[role="button"],[role="option"],[onclick],[tabindex],svg text,svg rect,svg circle,svg path,svg line';
    function norm(value) {
        return (value || '').replace(/\\s+/g, ' ').trim();
    }
    const wanted = norm(text);
    function clickableTarget(el) {
        let cur = el;
        while (cur && cur !== document.body) {
            if (cur.matches && cur.matches(CLICKABLE)) return cur;
            cur = cur.parentElement;
        }
        return null;
    }
    function find() {
        const all = document.querySelectorAll('*');
        for (const el of all) {
            if (SKIP.has(el.tagName)) continue;
            const t = norm(el.textContent);
            if (!t.includes(wanted)) continue;
            const target = clickableTarget(el);
            if (target) return target;
        }
        return null;
    }
    const immediate = find();
    if (immediate) {
        immediate.click();
        return resolve({status: 'found', tag: immediate.tagName || '', text: norm(immediate.textContent)});
    }
    const observer = new MutationObserver(() => {
        const el = find();
        if (el) {
            observer.disconnect();
            clearTimeout(timer);
            setTimeout(() => {
                el.click();
                resolve({status: 'found', tag: el.tagName || '', text: norm(el.textContent)});
            }, 50);
        }
    });
    observer.observe(document.body, {
        childList: true, subtree: true,
        attributes: true, characterData: true
    });
    const timer = setTimeout(() => {
        observer.disconnect();
        resolve({status: 'timeout'});
    }, timeoutMs);
})
"""


async def watch_for_text(
    text: str, context: ToolContext, *, timeout_ms: int = 10_000
) -> ToolResult:
    context.last_tool = "watch_for_text"
    context.last_element_id = None

    if not text or not text.strip():
        return ToolResult(ok=False, message="Text to watch for cannot be empty")

    clamped = max(500, min(timeout_ms, _WATCH_MAX_TIMEOUT_MS))

    session = await _session_for_active_frame(context)
    await _inject_observer(session)

    try:
        expression = f"({_WATCH_FOR_TEXT_JS})({json.dumps([text.strip(), clamped])})"
        evaluated = await session.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
            },
        )
        result = evaluated.get("result", {}).get("value")
    except Exception as exc:
        await _collect_mutations(session, settle_ms=50)
        return ToolResult(ok=False, message=f"Watch failed: {exc}")

    if isinstance(result, dict) and result.get("status") == "found":
        mutations = await _collect_mutations_with_ids(
            session, context, context.timing.settle_ms,
            frame_id=context.active_frame_id,
        )
        clicked_tag = str(result.get("tag") or "element").lower()
        clicked_text = str(result.get("text") or text)
        base_msg = f"Watched and clicked text containing '{text}' on {clicked_tag}"
        message = _format_verification(mutations, base_msg)
        if not _build_change_lines(mutations or {}):
            return ToolResult(
                ok=True,
                message=(
                    f"Found matching text '{clicked_text}' and clicked it, but no observable "
                    "page change followed. Do not repeat this watch; if the relevant field already has the "
                    "desired value, proceed, otherwise stop this step and wait for a fresh snapshot or use a stable element ID."
                ),
            )
        return ToolResult(ok=True, message=message)

    mutations = await _collect_mutations(session, settle_ms=50)
    timeout_msg = f"Watch timeout: '{text}' not found within {clamped}ms"
    return ToolResult(
        ok=False,
        message=_format_verification(mutations, timeout_msg),
    )


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
        return ToolResult(ok=False, message=_unknown_element_message(element_id))
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


async def select_text(
    element_id: str,
    context: ToolContext,
    *,
    text: str | None = None,
    occurrence: int = 1,
) -> ToolResult:
    context.last_tool = "select_text"
    context.last_element_id = element_id
    element = _resolve_element(element_id, context)
    if not element or not element.backend_node_id:
        return ToolResult(ok=False, message=_unknown_element_message(element_id))
    frame_error = _active_frame_error(element, context)
    if frame_error:
        return frame_error
    if _is_unqualified_text_selection_control(element, text):
        return ToolResult(
            ok=False,
            message=(
                "Select text refused: target is a control, not a text container. "
                "Use a visible text/paragraph element, or provide exact text that exists inside this element."
            ),
        )
    session = await _session_for_element(element, context)
    await _inject_observer(session)
    result = await _call_on_node(
        element.backend_node_id,
        session,
        """
        function (targetText, occurrence) {
            this.scrollIntoView({block: 'center', inline: 'center'});
            const selection = window.getSelection();
            if (!selection) return {ok: false, selected: '', reason: 'selection unavailable'};
            selection.removeAllRanges();
            const wanted = (targetText || '').trim();
            if (!wanted) {
                const range = document.createRange();
                range.selectNodeContents(this);
                selection.addRange(range);
                return {ok: true, selected: selection.toString()};
            }
            const targetOccurrence = Math.max(1, Number(occurrence) || 1);
            const walker = document.createTreeWalker(this, NodeFilter.SHOW_TEXT);
            let node;
            let seen = 0;
            while ((node = walker.nextNode())) {
                const value = node.nodeValue || '';
                const idx = value.indexOf(wanted);
                if (idx < 0) continue;
                seen += 1;
                if (seen !== targetOccurrence) continue;
                const range = document.createRange();
                const start = idx;
                const end = idx + wanted.length;
                range.setStart(node, start);
                range.setEnd(node, end);
                selection.addRange(range);
                return {ok: true, selected: selection.toString()};
            }
            return {ok: false, selected: '', reason: 'text not found'};
        }
        """,
        [{"value": text or ""}, {"value": int(occurrence)}],
    )
    value = (result or {}).get("result", {}).get("value") or {}
    mutations = await _collect_mutations_with_ids(
        session, context, context.timing.settle_ms,
        frame_id=element.frame_id, frame_url=element.frame_url,
    )
    selected = str(value.get("selected") or "")
    if not value.get("ok"):
        return ToolResult(ok=False, message=f"Select text failed: {value.get('reason') or 'unknown error'}")
    display = selected[:250] + "..." if len(selected) > 250 else selected
    return ToolResult(ok=True, message=_format_verification(mutations, f'Selected text: "{display}"'))


async def apply_format(command: str, context: ToolContext) -> ToolResult:
    context.last_tool = "apply_format"
    context.last_element_id = None
    normalized = command.strip().lower()
    allowed = {
        "bold": "bold",
        "italic": "italic",
        "underline": "underline",
        "strike": "strikeThrough",
        "strikethrough": "strikeThrough",
        "ordered_list": "insertOrderedList",
        "unordered_list": "insertUnorderedList",
    }
    browser_command = allowed.get(normalized)
    if not browser_command:
        return ToolResult(ok=False, message=f"Unsupported format command: {command}")
    session = await _session_for_active_frame(context)
    await _inject_observer(session)
    try:
        evaluated = await session.send(
            "Runtime.evaluate",
            {
                "expression": (
                    "(() => { const before = String(window.getSelection?.() || ''); "
                    f"const ok = document.execCommand({json.dumps(browser_command)}, false, null); "
                    "const after = String(window.getSelection?.() || ''); "
                    "return {ok, before, after}; })()"
                ),
                "returnByValue": True,
            },
        )
    except Exception as exc:
        await _collect_mutations(session, settle_ms=50)
        return ToolResult(ok=False, message=f"Apply format failed: {exc}")
    value = evaluated.get("result", {}).get("value") or {}
    mutations = await _collect_mutations_with_ids(
        session, context, context.timing.settle_ms,
        frame_id=context.active_frame_id,
    )
    ok = bool(value.get("ok"))
    selected = str(value.get("after") or value.get("before") or "")
    return ToolResult(
        ok=ok,
        message=_format_verification(mutations, f"Applied format {browser_command}. Selection: \"{selected[:120]}\""),
    )


async def transfer_text(source_id: str, target_id: str, context: ToolContext) -> ToolResult:
    context.last_tool = "transfer_text"
    context.last_element_id = target_id
    source = _resolve_element(source_id, context)
    target = _resolve_element(target_id, context)
    if not source or not source.backend_node_id:
        return ToolResult(ok=False, message=f"Unknown source id: {source_id}")
    if not target or not target.backend_node_id:
        return ToolResult(ok=False, message=f"Unknown target id: {target_id}")
    if source.frame_id != target.frame_id:
        return ToolResult(ok=False, message="Transfer failed: source and target are in different frames")
    if context.active_frame_id and source.frame_id != context.active_frame_id:
        return ToolResult(ok=False, message="Transfer failed: source is not in the active frame")
    session = await _session_for_element(source, context)
    await _inject_observer(session)
    source_result = await _call_on_node(
        source.backend_node_id,
        session,
        """
        function () {
            const type = (this.getAttribute && this.getAttribute('type') || '').toLowerCase();
            if (type === 'password') return {ok: false, text: '', reason: 'password source omitted'};
            if ('value' in this) return {ok: true, text: String(this.value || '')};
            return {ok: true, text: String(this.innerText || this.textContent || '')};
        }
        """,
    )
    source_value = (source_result or {}).get("result", {}).get("value") or {}
    if not source_value.get("ok"):
        return ToolResult(ok=False, message=f"Transfer failed: {source_value.get('reason') or 'source text unavailable'}")
    text_value = str(source_value.get("text") or "")
    if not await _set_text_value(target.backend_node_id, session, text_value):
        if not await _dom_focus(target.backend_node_id, session) or not await _insert_text(session, text_value):
            await _collect_mutations(session, settle_ms=50)
            return ToolResult(ok=False, message="Transfer failed: target did not accept text")
    mutations = await _collect_mutations_with_ids(
        session, context, max(context.timing.settle_ms, 300),
        frame_id=target.frame_id, frame_url=target.frame_url,
    )
    current_value = await _read_input_value(target.backend_node_id, session)
    target_type = (target.attributes or {}).get("type", "").lower()
    display = "[password value omitted]" if target_type == "password" else (current_value if current_value is not None else text_value)[:250]
    return ToolResult(
        ok=True,
        message=_format_verification(
            mutations,
            f'Transferred text from {source_id} to {target_id}. Current value: "{display}"',
        ),
    )


async def read_live_text(context: ToolContext, element_id: str | None = None) -> ToolResult:
    context.last_tool = "read_live_text"
    context.last_element_id = element_id
    session = await _session_for_active_frame(context)
    if element_id:
        element = _resolve_element(element_id, context)
        if not element or not element.backend_node_id:
            return ToolResult(ok=False, message=_unknown_element_message(element_id))
        frame_error = _active_frame_error(element, context)
        if frame_error:
            return frame_error
        session = await _session_for_element(element, context)
        result = await _call_on_node(
            element.backend_node_id,
            session,
            """
            function () {
                const type = (this.getAttribute && this.getAttribute('type') || '').toLowerCase();
                if (type === 'password') return [{label: 'target', tag: this.tagName || '', text: '[password value omitted]'}];
                const text = 'value' in this ? String(this.value || '') : String(this.innerText || this.textContent || '');
                return [{label: 'target', tag: this.tagName || '', text}];
            }
            """,
        )
        value = (result or {}).get("result", {}).get("value") or []
    else:
        evaluated = await session.send(
            "Runtime.evaluate",
            {
                "expression": """
                (() => {
                  const selectors = [
                    'textarea', 'input', '[contenteditable]', '[role="textbox"]',
                    '[role="log"]', '[aria-live]', 'pre', 'code', 'output', 'section', 'main'
                  ];
                  const seen = new Set();
                  const out = [];
                  function add(label, el) {
                    if (!el || seen.has(el)) return;
                    seen.add(el);
                    const tag = el.tagName || '';
                    const type = (el.getAttribute && el.getAttribute('type') || '').toLowerCase();
                    if (type === 'password') return;
                    const text = ('value' in el ? String(el.value || '') : String(el.innerText || el.textContent || '')).trim();
                    if (!text) return;
                    out.push({label, tag, text: text.slice(0, 2000)});
                  }
                  add('active', document.activeElement);
                  for (const selector of selectors) {
                    for (const el of document.querySelectorAll(selector)) {
                      if (out.length >= 8) break;
                      add(selector, el);
                    }
                    if (out.length >= 8) break;
                  }
                  if (out.length === 0) add('body', document.body);
                  return out;
                })()
                """,
                "returnByValue": True,
            },
        )
        value = evaluated.get("result", {}).get("value") or []
    lines = []
    for item in value[:8]:
        text_value = " ".join(str(item.get("text") or "").split())
        if not text_value:
            continue
        if len(text_value) > 500:
            text_value = text_value[:500].rstrip() + "..."
        lines.append(f"{item.get('label') or 'text'} <{str(item.get('tag') or '').lower()}>: {text_value}")
    message = "\n".join(lines) if lines else "No live text found."
    return ToolResult(ok=True, message=message)


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
    await _inject_observer(session)
    try:
        if element_id and element and element.backend_node_id:
            result = await _call_on_node(
                element.backend_node_id,
                session,
                _SCROLL_ELEMENT_JS,
                args=[{"value": [delta_x, delta_y]}],
            )
            if not result:
                await _collect_mutations(session, settle_ms=50)
                return ToolResult(ok=False, message="Scroll failed: element not found")
            result = result.get("result", {}).get("value")
        else:
            result = await context.page.evaluate(_SCROLL_PAGE_JS, [delta_x, delta_y])
    except Exception as exc:
        await _collect_mutations(session, settle_ms=50)
        return ToolResult(ok=False, message=f"Scroll failed: {exc}")
    _scroll_frame_id = element.frame_id if element else context.active_frame_id
    _scroll_frame_url = element.frame_url if element else None
    mutations = await _collect_mutations_with_ids(
        session, context, context.timing.settle_ms,
        frame_id=_scroll_frame_id, frame_url=_scroll_frame_url,
    )
    if not result:
        msg = f"Scrolled dx={delta_x} dy={delta_y}"
        if element_id:
            msg += ". WARNING: could not confirm element scroll position changed"
        return ToolResult(ok=True, message=_format_verification(mutations, msg))
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
    return ToolResult(ok=True, message=_format_verification(mutations, msg))


async def switch_to_iframe(iframe_id: str, context: ToolContext) -> ToolResult:
    context.last_tool = "switch_to_iframe"
    context.last_element_id = iframe_id
    element = _resolve_element(iframe_id, context)
    if not element:
        return ToolResult(ok=False, message=_unknown_element_message(iframe_id))

    tag = (element.node_name or "").upper() or "UNKNOWN"
    if tag != "IFRAME":
        return ToolResult(ok=False, message=f"Element {iframe_id} is a {tag}, not an IFRAME")

    if not element.frame_id:
        return ToolResult(ok=False, message="Iframe has no frame id (frame not ready)")

    prev_active = context.active_frame_id
    if prev_active == element.frame_id:
        frame_label = element.frame_name or element.frame_url or ""
        suffix = f" ({frame_label})" if frame_label else ""
        return ToolResult(ok=True, message=f"Already in iframe {iframe_id}{suffix}")
    try:
        session = await _session_for_element(element, context)
    except Exception:
        context.active_frame_id = prev_active
        return ToolResult(
            ok=False,
            message=f"Failed to attach to iframe session for {iframe_id}. Try waiting and switching again.",
        )

    if session is context.cdp_session and element.frame_id not in context.frame_sessions:
        context.active_frame_id = prev_active
        return ToolResult(
            ok=False,
            message=f"Failed to attach to iframe session for {iframe_id}. Try waiting and switching again.",
        )

    context.active_frame_id = element.frame_id
    elements_in_frame = sum(
        1
        for el in context.element_index.elements.values()
        if el.frame_id == element.frame_id and el.stable_id != iframe_id
    )
    frame_label = element.frame_name or element.frame_url or ""
    if frame_label:
        message = (
            f"Active frame set to iframe {iframe_id} ({frame_label}). "
            f"Elements in this frame: {elements_in_frame}. "
            "Use switch_to_main_frame() to interact with the main page."
        )
    else:
        message = (
            f"Active frame set to iframe {iframe_id}. "
            f"Elements in this frame: {elements_in_frame}. "
            "Use switch_to_main_frame() to interact with the main page."
        )
    return ToolResult(ok=True, message=message)


async def switch_to_main_frame(context: ToolContext) -> ToolResult:
    context.last_tool = "switch_to_main_frame"
    context.last_element_id = None
    if context.active_frame_id is None:
        return ToolResult(ok=True, message="Already in main frame")
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
    session = await _session_for_active_frame(context)
    await _inject_observer(session)
    try:
        await session.send(
            "Runtime.evaluate",
            {
                "expression": code,
                "awaitPromise": True,
            },
        )
    except Exception as exc:  # pragma: no cover - runtime safety
        await _collect_mutations(session, settle_ms=50)
        return ToolResult(ok=False, message=f"Execute JS failed: {exc}")
    mutations = await _collect_mutations_with_ids(
        session, context, context.timing.settle_ms,
        frame_id=context.active_frame_id,
    )
    return ToolResult(ok=True, message=_format_verification(mutations, "Executed script"))


async def press_key_combination(keys: list[str], context: ToolContext) -> ToolResult:
    context.last_tool = "press_key_combination"
    context.last_element_id = None
    session = await _session_for_active_frame(context)
    await _inject_observer(session)
    async def _active_value() -> dict[str, Any]:
        try:
            evaluated = await session.send(
                "Runtime.evaluate",
                {
                    "expression": (
                        "(() => { const el = document.activeElement; if (!el) return {}; "
                        "const type = (el.getAttribute && el.getAttribute('type') || '').toLowerCase(); "
                        "const value = type === 'password' ? '[password value omitted]' : "
                        "('value' in el ? String(el.value || '') : String(el.innerText || el.textContent || '')); "
                        "return {tag: el.tagName || '', id: el.id || '', value}; })()"
                    ),
                    "returnByValue": True,
                },
            )
            value = evaluated.get("result", {}).get("value")
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}
    before_active = await _active_value()
    try:
        await context.page.keyboard.press("+".join(keys))
    except Exception as exc:  # pragma: no cover - runtime safety
        await _collect_mutations(session, settle_ms=50)
        return ToolResult(ok=False, message=f"Key press failed: {exc}")
    mutations = await _collect_mutations_with_ids(
        session, context, context.timing.settle_ms,
        frame_id=context.active_frame_id,
    )
    after_active = await _active_value()
    base_msg = f"Pressed {'+'.join(keys)}"
    if before_active or after_active:
        base_msg += (
            f". Active before: {before_active.get('tag', '')}#{before_active.get('id', '')}; "
            f"after: {after_active.get('tag', '')}#{after_active.get('id', '')}"
        )
        before_value = str(before_active.get("value") or "")
        after_value = str(after_active.get("value") or "")
        if before_value or after_value:
            base_msg += f'. Previous value: "{before_value[:120]}". Current value: "{after_value[:120]}"'
    message = _format_verification(mutations, base_msg)
    return ToolResult(ok=True, message=message)
