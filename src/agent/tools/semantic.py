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
  };
  const TEXT_CAP = 20;
  const INTERACTIVE_CAP = 10;
  const charDataNodes = new Map();  // node -> index in addedText
  const IGNORED_TAGS = new Set(['SCRIPT','STYLE','NOSCRIPT','LINK','META']);
  const TRACKED_ATTRS = new Set([
    'aria-expanded','aria-checked','aria-selected','aria-hidden',
    'aria-disabled','disabled','checked','selected','open','hidden',
    'value','href','src'
  ]);
  const INTERACTIVE_TAGS_SET = new Set(['A','BUTTON','INPUT','SELECT','TEXTAREA','OPTION','IFRAME']);
  const INTERACTIVE_ROLES_SET = new Set([
    'button','checkbox','combobox','link','menuitem','option',
    'radio','slider','spinbutton','switch','tab','textbox'
  ]);

  function isInteractive(el) {
    if (INTERACTIVE_TAGS_SET.has(el.tagName)) return true;
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
    });
  }

  delete window.__mutObs;

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
            stable_id = build_stable_id_from_backend(frame_id, int(backend_node_id))
            resolved.append({
                "stable_id": stable_id,
                "backend_node_id": int(backend_node_id),
                "tag": item.get("tag", ""),
                "role": item.get("role", ""),
                "text": item.get("text", ""),
                "name": item.get("name", ""),
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
            bounding_box=None,
            attributes={},
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

    # New interactive elements with IDs  (+ interactive)
    for item in resolved_items:
        sid = item.get("stable_id", "?")
        tag = item.get("tag", "?")
        label = item.get("role") or item.get("name") or item.get("text") or ""
        if label:
            lines.append(f'  + interactive {sid}: {tag} "{label}"')
        else:
            lines.append(f"  + interactive {sid}: {tag}")

    # Removed text  (-)
    for item in mutations.get("removedText", []):
        lines.append(f"  - {_fmt_text_item(item)}")

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


async def hover_element(element_id: str, context: ToolContext, *, duration_ms: int = 2000) -> ToolResult:
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

    mutations = await _collect_mutations_with_ids(
        session, context, context.timing.settle_ms,
        frame_id=element.frame_id, frame_url=element.frame_url,
    )
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
    mutations = await _collect_mutations_with_ids(
        session, context, context.timing.settle_ms,
        frame_id=element.frame_id, frame_url=element.frame_url,
    )
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
        return ToolResult(ok=True, message=_format_verification(mutations, base_msg))

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
    function find() {
        const all = document.querySelectorAll('*');
        for (const el of all) {
            if (SKIP.has(el.tagName)) continue;
            if (el.children.length === 0) {
                const t = (el.textContent || '').trim();
                if (t.includes(text)) return el;
            }
        }
        return null;
    }
    const immediate = find();
    if (immediate) {
        immediate.click();
        return resolve('found');
    }
    const observer = new MutationObserver(() => {
        const el = find();
        if (el) {
            observer.disconnect();
            clearTimeout(timer);
            setTimeout(() => { el.click(); resolve('found'); }, 50);
        }
    });
    observer.observe(document.body, {
        childList: true, subtree: true,
        attributes: true, characterData: true
    });
    const timer = setTimeout(() => {
        observer.disconnect();
        resolve('timeout');
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

    if result == "found":
        mutations = await _collect_mutations_with_ids(
            session, context, context.timing.settle_ms,
            frame_id=context.active_frame_id,
        )
        base_msg = f"Watched and clicked '{text}'"
        return ToolResult(ok=True, message=_format_verification(mutations, base_msg))

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
        return ToolResult(ok=False, message=f"Unknown element id: {iframe_id}")

    tag = (element.node_name or "").upper() or "UNKNOWN"
    if tag != "IFRAME":
        return ToolResult(ok=False, message=f"Element {iframe_id} is a {tag}, not an IFRAME")

    if not element.frame_id:
        return ToolResult(ok=False, message="Iframe has no frame id (frame not ready)")

    prev_active = context.active_frame_id
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
    try:
        await context.page.keyboard.press("+".join(keys))
    except Exception as exc:  # pragma: no cover - runtime safety
        await _collect_mutations(session, settle_ms=50)
        return ToolResult(ok=False, message=f"Key press failed: {exc}")
    mutations = await _collect_mutations_with_ids(
        session, context, context.timing.settle_ms,
        frame_id=context.active_frame_id,
    )
    base_msg = f"Pressed {'+'.join(keys)}"
    message = _format_verification(mutations, base_msg)
    return ToolResult(ok=True, message=message)
