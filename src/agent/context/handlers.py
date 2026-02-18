"""Extract JavaScript event handler source code from DOM elements.

Walks all DOM elements via a single page.evaluate() call, extracting handler
source from inline handlers and framework internals (React/Vue/Angular).
Elements with handlers are stamped with data-agent-hid for correlation with
the CDP DOM snapshot.
"""

from __future__ import annotations

import logging

from playwright.async_api import Page

logger = logging.getLogger(__name__)

_MAX_HANDLER_LENGTH = 120
_MAX_HANDLERS_PER_ELEMENT = 3

# Priority order for handler events (higher index = lower priority).
_EVENT_PRIORITY = [
    "click",
    "submit",
    "change",
    "input",
    "keydown",
    "keyup",
    "keypress",
    "focus",
    "blur",
    "mousedown",
    "mouseup",
    "mouseover",
    "dragstart",
    "dragover",
    "drop",
    "dragenter",
    "mouseenter",
    "mouseleave",
]

_EXTRACT_HANDLERS_JS = """
(() => {
  const MAX_LEN = """ + str(_MAX_HANDLER_LENGTH) + """;
  const INLINE_EVENTS = [
    'onclick', 'onchange', 'oninput', 'onsubmit', 'onkeydown', 'onkeyup',
    'onkeypress', 'onfocus', 'onblur', 'onmousedown', 'onmouseup', 'onmouseover',
    'ondblclick', 'oncontextmenu',
    'ondragstart', 'ondragover', 'ondrop', 'ondragenter',
    'onmouseenter', 'onmouseleave',
  ];
  const REACT_EVENTS = [
    'onClick', 'onChange', 'onInput', 'onSubmit', 'onKeyDown', 'onKeyUp',
    'onKeyPress', 'onFocus', 'onBlur', 'onMouseDown', 'onMouseUp', 'onMouseOver',
    'onDoubleClick', 'onContextMenu',
    'onDragStart', 'onDragOver', 'onDrop', 'onDragEnter',
    'onMouseEnter', 'onMouseLeave',
  ];

  function truncate(src) {
    if (!src) return null;
    let s = String(src).trim();
    // Collapse whitespace
    s = s.replace(/\\s+/g, ' ');
    if (s.length > MAX_LEN) s = s.substring(0, MAX_LEN) + '...';
    return s;
  }

  function handlerSummary(fn) {
    if (!fn || typeof fn !== 'function') return null;
    let src;
    try { src = fn.toString(); } catch(e) { return null; }
    if (!src) return null;
    return truncate(src);
  }

  const result = {};
  let hid = 0;
  const allElements = document.querySelectorAll('*');

  for (const el of allElements) {
    const handlers = {};

    // 1. Inline handlers (onclick, onchange, etc.)
    for (const attr of INLINE_EVENTS) {
      const fn = el[attr];
      if (fn && typeof fn === 'function') {
        const eventName = attr.substring(2); // 'onclick' -> 'click'
        const summary = handlerSummary(fn);
        if (summary) handlers[eventName] = summary;
      }
    }

    // 2. React props (__reactProps$* or __reactFiber$*)
    try {
      const keys = Object.keys(el);
      for (const key of keys) {
        if (key.startsWith('__reactProps$') || key.startsWith('__reactProps')) {
          const props = el[key];
          if (props && typeof props === 'object') {
            for (const rEvent of REACT_EVENTS) {
              if (props[rEvent] && typeof props[rEvent] === 'function') {
                const eventName = rEvent.substring(2).toLowerCase(); // 'onClick' -> 'click'
                if (!handlers[eventName]) {
                  const summary = handlerSummary(props[rEvent]);
                  if (summary) handlers[eventName] = summary;
                }
              }
            }
          }
        }
      }
    } catch(e) {}

    // 3. Vue 3 (vnode props)
    try {
      const vueComp = el.__vueParentComponent;
      if (vueComp && vueComp.vnode && vueComp.vnode.props) {
        const vueProps = vueComp.vnode.props;
        for (const key of Object.keys(vueProps)) {
          if (key.startsWith('on') && typeof vueProps[key] === 'function') {
            const eventName = key.substring(2).toLowerCase();
            if (!handlers[eventName]) {
              const summary = handlerSummary(vueProps[key]);
              if (summary) handlers[eventName] = summary;
            }
          }
        }
      }
    } catch(e) {}

    // 4. Vue 2 ($listeners)
    try {
      const vue2 = el.__vue__;
      if (vue2 && vue2.$listeners) {
        for (const [eventName, fn] of Object.entries(vue2.$listeners)) {
          if (typeof fn === 'function' && !handlers[eventName]) {
            const summary = handlerSummary(fn);
            if (summary) handlers[eventName] = summary;
          }
        }
      }
    } catch(e) {}

    // 5. Angular (__ngContext__)
    try {
      const ngCtx = el.__ngContext__;
      if (ngCtx && Array.isArray(ngCtx)) {
        for (const item of ngCtx) {
          if (item && typeof item === 'object' && item.constructor &&
              item.constructor.name && item.constructor.name !== 'Object') {
            // Look for method-like properties on component instances
            const proto = Object.getPrototypeOf(item);
            if (proto) {
              const methodNames = Object.getOwnPropertyNames(proto)
                .filter(n => n !== 'constructor' && typeof proto[n] === 'function');
              for (const name of methodNames) {
                const lower = name.toLowerCase();
                if (lower.includes('click') || lower.includes('submit') ||
                    lower.includes('change') || lower.includes('handle') ||
                    lower.includes('toggle')) {
                  const eventGuess = lower.includes('click') ? 'click' :
                                     lower.includes('submit') ? 'submit' :
                                     lower.includes('change') ? 'change' : 'handler';
                  if (!handlers[eventGuess]) {
                    const summary = handlerSummary(proto[name]);
                    if (summary) handlers[eventGuess] = summary;
                  }
                }
              }
            }
          }
        }
      }
    } catch(e) {}

    if (Object.keys(handlers).length > 0) {
      const id = String(hid);
      el.setAttribute('data-agent-hid', id);
      result[id] = handlers;
      hid++;
    }
  }

  return result;
})()
"""

_CLEANUP_HANDLERS_JS = """
(() => {
  const els = document.querySelectorAll('[data-agent-hid]');
  for (const el of els) {
    el.removeAttribute('data-agent-hid');
  }
})()
"""


async def extract_handlers(page: Page) -> dict[str, dict[str, str]]:
    """Extract JS event handlers from all DOM elements.

    Returns ``{hid: {event_name: truncated_source}}`` and stamps each element
    that has handlers with a ``data-agent-hid`` attribute for snapshot
    correlation.
    """
    try:
        result = await page.evaluate(_EXTRACT_HANDLERS_JS)
        if isinstance(result, dict):
            return result
        return {}
    except Exception:
        logger.debug("Handler extraction failed", exc_info=True)
        return {}


async def cleanup_handler_attributes(page: Page) -> None:
    """Remove ``data-agent-hid`` attributes stamped by extract_handlers()."""
    try:
        await page.evaluate(_CLEANUP_HANDLERS_JS)
    except Exception:
        logger.debug("Handler attribute cleanup failed", exc_info=True)


def prioritize_handlers(handlers: dict[str, str]) -> dict[str, str]:
    """Keep the top N handlers sorted by event priority.

    Returns at most ``_MAX_HANDLERS_PER_ELEMENT`` entries.
    """
    if not handlers:
        return {}

    priority_map = {name: idx for idx, name in enumerate(_EVENT_PRIORITY)}
    max_priority = len(_EVENT_PRIORITY)

    sorted_events = sorted(
        handlers.items(),
        key=lambda item: priority_map.get(item[0], max_priority),
    )
    return dict(sorted_events[:_MAX_HANDLERS_PER_ELEMENT])


def format_handlers_for_llm(handlers: dict[str, str]) -> str:
    """Format handler info into a compact LLM-readable string.

    Example output: ``[click:handleSubmit(){fetch('/api/...; change:validate()]``
    """
    if not handlers:
        return ""

    parts: list[str] = []
    for event, source in handlers.items():
        # Truncate source for display — keep it short
        display = source
        if len(display) > 60:
            display = display[:57] + "..."
        parts.append(f"{event}:{display}")

    return "[" + "; ".join(parts) + "]"
