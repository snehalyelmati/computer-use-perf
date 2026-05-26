"""Extract JavaScript event handler source code from DOM elements.

Walks all DOM elements via a single page.evaluate() call, extracting handler
source from inline handlers and framework internals (React/Vue/Angular).
Elements with handlers are stamped with data-agent-hid for correlation with
the CDP DOM snapshot.
"""

from __future__ import annotations

import logging
import re

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
    "scroll",
    "wheel",
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
    'onscroll', 'onwheel',
    'ondblclick', 'oncontextmenu',
    'ondragstart', 'ondragover', 'ondrop', 'ondragenter',
    'onmouseenter', 'onmouseleave',
  ];
  const REACT_EVENTS = [
    'onClick', 'onChange', 'onInput', 'onSubmit', 'onKeyDown', 'onKeyUp',
    'onKeyPress', 'onFocus', 'onBlur', 'onMouseDown', 'onMouseUp', 'onMouseOver',
    'onScroll', 'onWheel',
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
    const t = truncate(src);
    const name = fn.name;
    // If name is descriptive and not already the start of the source, prefix it
    if (name && name.length > 2 && !/^[_$][a-zA-Z]?$/.test(name)
        && !t.startsWith('function ' + name)) {
      return truncate(name + '() ' + src);
    }
    return t;
  }

  function isOpaque(s) {
    if (!s) return true;
    if (/^function\\s*[_$a-zA-Z]{0,2}\\s*\\(/.test(s) && s.length < 40) return true;
    if (s.includes('[native code]')) return true;
    return false;
  }

  function namedScore(s) {
    if (!s) return 0;
    const m = s.match(/^([A-Za-z_$][\\w$]{2,})\\(\\)/);
    if (m) return m[1].length;
    const fm = s.match(/^function\\s+([A-Za-z_$][\\w$]{2,})\\b/);
    return fm ? fm[1].length : 0;
  }

  function bestHandler(existing, candidate) {
    if (!candidate) return existing;
    if (!existing) return candidate;
    if (isOpaque(existing) && !isOpaque(candidate)) return candidate;
    if (!isOpaque(existing) && isOpaque(candidate)) return existing;
    // Both non-opaque: prefer the one with a better named function score;
    // on tie, prefer the shorter (more concise) handler.
    const es = namedScore(existing), cs = namedScore(candidate);
    if (cs > es) return candidate;
    if (es > cs) return existing;
    return candidate.length <= existing.length ? candidate : existing;
  }

  function eventNameFromKey(key) {
    if (!key) return null;
    if (key === '__on') return null;
    const m = String(key).match(/^_+on([A-Za-z][\\w-]*)$/);
    return m ? m[1].toLowerCase() : null;
  }

  function addListenerLikeValue(handlers, eventName, value) {
    if (!eventName || !value) return;
    if (typeof value === 'function') {
      const summary = handlerSummary(value) || 'listener';
      handlers[eventName] = bestHandler(handlers[eventName], summary);
      return;
    }
    if (Array.isArray(value)) {
      for (const item of value) addListenerLikeValue(handlers, eventName, item);
      return;
    }
    if (typeof value === 'object') {
      const nestedEvent = value.type || eventName;
      const candidates = [value.value, value.listener, value.handler, value.callback];
      for (const candidate of candidates) {
        if (typeof candidate === 'function') {
          const summary = handlerSummary(candidate) || 'listener';
          handlers[nestedEvent] = bestHandler(handlers[nestedEvent], summary);
          return;
        }
      }
    }
  }

  function addPointerAffordance(handlers, el) {
    try {
      const nativeTags = new Set(['A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'OPTION', 'IFRAME']);
      const role = (el.getAttribute('role') || '').toLowerCase();
      const nativeRoles = new Set(['button', 'checkbox', 'combobox', 'link', 'menuitem', 'option', 'radio', 'slider', 'spinbutton', 'switch', 'tab', 'textbox']);
      if (nativeTags.has(el.tagName) || nativeRoles.has(role)) return;

      const style = window.getComputedStyle(el);
      if (!style || style.cursor !== 'pointer') return;
      const rects = el.getClientRects();
      if (!rects || rects.length === 0) return;
      const rect = rects[0];
      if (!rect || rect.width <= 0 || rect.height <= 0) return;
      const label = (el.getAttribute('aria-label') || el.getAttribute('title') || el.innerText || el.textContent || '').trim();
      const structuralHint = (el.id || el.className || el.getAttribute('role') || el.getAttribute('data-action') || '').toString().trim();
      if (!label && !structuralHint) return;
      handlers.click = bestHandler(handlers.click, 'cursor:pointer');
    } catch(e) {}
  }

  const result = {};
  let hid = 0;
  const allElements = document.querySelectorAll('*');

  for (const el of allElements) {
    const handlers = {};

    // 1. React props + fiber (checked first — most descriptive on React pages)
    try {
      const keys = Array.from(new Set(Object.keys(el).concat(Object.getOwnPropertyNames(el))));
      for (const key of keys) {
        if (key.startsWith('__reactProps$') || key.startsWith('__reactProps')) {
          const props = el[key];
          if (props && typeof props === 'object') {
            for (const rEvent of REACT_EVENTS) {
              if (props[rEvent] && typeof props[rEvent] === 'function') {
                const eventName = rEvent.substring(2).toLowerCase(); // 'onClick' -> 'click'
                const summary = handlerSummary(props[rEvent]);
                if (summary) handlers[eventName] = bestHandler(handlers[eventName], summary);
              }
            }
          }
        }
        if (key.startsWith('__reactFiber$') || key.startsWith('__reactInternalInstance$')) {
          try {
            const fiber = el[key];
            if (fiber && fiber.memoizedProps) {
              for (const rEvent of REACT_EVENTS) {
                if (fiber.memoizedProps[rEvent] && typeof fiber.memoizedProps[rEvent] === 'function') {
                  const eventName = rEvent.substring(2).toLowerCase();
                  const summary = handlerSummary(fiber.memoizedProps[rEvent]);
                  if (summary) handlers[eventName] = bestHandler(handlers[eventName], summary);
                }
              }
            }
          } catch(e) {}
        }
      }
    } catch(e) {}

    // 2. Vue 3 (vnode props)
    try {
      const vueComp = el.__vueParentComponent;
      if (vueComp && vueComp.vnode && vueComp.vnode.props) {
        const vueProps = vueComp.vnode.props;
        for (const key of Object.keys(vueProps)) {
          if (key.startsWith('on') && typeof vueProps[key] === 'function') {
            const eventName = key.substring(2).toLowerCase();
            const summary = handlerSummary(vueProps[key]);
            if (summary) handlers[eventName] = bestHandler(handlers[eventName], summary);
          }
        }
      }
    } catch(e) {}

    // 3. Vue 2 ($listeners)
    try {
      const vue2 = el.__vue__;
      if (vue2 && vue2.$listeners) {
        for (const [eventName, fn] of Object.entries(vue2.$listeners)) {
          if (typeof fn === 'function') {
            const summary = handlerSummary(fn);
            if (summary) handlers[eventName] = bestHandler(handlers[eventName], summary);
          }
        }
      }
    } catch(e) {}

    // 4. Angular (__ngContext__)
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
                  const summary = handlerSummary(proto[name]);
                  if (summary) handlers[eventGuess] = bestHandler(handlers[eventGuess], summary);
                }
              }
            }
          }
        }
      }
    } catch(e) {}

    // 5. Generic listener-like expandos used by libraries such as D3.
    try {
      const keys = Array.from(new Set(Object.keys(el).concat(Object.getOwnPropertyNames(el))));
      for (const key of keys) {
        if (key === '__on' && Array.isArray(el[key])) {
          for (const item of el[key]) {
            if (item && typeof item === 'object') {
              addListenerLikeValue(handlers, item.type || 'handler', item);
            }
          }
          continue;
        }
        const eventName = eventNameFromKey(key);
        if (eventName) addListenerLikeValue(handlers, eventName, el[key]);
      }
    } catch(e) {}

    // 6. Inline handlers (fallback for non-framework pages)
    for (const attr of INLINE_EVENTS) {
      const fn = el[attr];
      if (fn && typeof fn === 'function') {
        const eventName = attr.substring(2); // 'onclick' -> 'click'
        const summary = handlerSummary(fn);
        if (summary) handlers[eventName] = bestHandler(handlers[eventName], summary);
      }
    }

    // 7. Last-resort clickable affordance. This catches DOM elements that are
    // intentionally clickable but do not expose listener source to page JS.
    addPointerAffordance(handlers, el);

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


def _infer_intents(source: str) -> list[str]:
    lowered = source.lower()
    patterns: list[tuple[str, str]] = [
        (r"\bsubmit\b", "submit"),
        (r"\bsearch\b", "search"),
        (r"\bclose\b|\bdismiss\b|\bcancel\b", "close"),
        (r"\bopen\b|\bshow\b|\breveal\b|\bexpand\b", "show"),
        (r"\bhide\b|\bcollapse\b", "hide"),
        (r"\btoggle\b", "toggle"),
        (r"\bnext\b|\bcontinue\b|\badvance\b", "next"),
        (r"\bprev\b|\bback\b", "back"),
        (r"\baccept\b|\bagree\b|\bconfirm\b|\bok\b", "confirm"),
        (r"\bdecline\b|\breject\b", "decline"),
        (r"\bscroll\b", "scroll"),
        (r"\bselect\b|\bchoose\b", "select"),
        (r"\bplay\b|\bpause\b", "media"),
        (r"\bdrag\b|\bdrop\b", "drag"),
        (r"\bdownload\b", "download"),
        (r"\bupload\b", "upload"),
        (r"\bzoom\b", "zoom"),
        (r"\badd\b|\bremove\b|\bdelete\b", "edit"),
    ]
    intents: list[str] = []
    for pattern, label in patterns:
        if re.search(pattern, lowered):
            intents.append(label)
    return intents


def _extract_handler_name(source: str) -> str | None:
    name_match = re.search(r"^([A-Za-z_$][\w$]{2,})\(\)", source)
    if name_match:
        return name_match.group(1)
    fn_match = re.search(r"\bfunction\s+([A-Za-z_$][\w$]{2,})\b", source)
    if fn_match:
        return fn_match.group(1)
    return None


def format_handlers_for_llm(handlers: dict[str, str]) -> str:
    """Format handler info into a compact LLM-readable string.

    Example output: ``[click:handleSubmit(){fetch('/api/...; change:validate()]``
    """
    if not handlers:
        return ""

    parts: list[str] = []
    for event, source in handlers.items():
        intents = _infer_intents(source)
        if intents:
            display = "|".join(intents[:2])
        else:
            name = _extract_handler_name(source)
            display = name or "handler"
        parts.append(f"{event}:{display}")

    return "[" + "; ".join(parts) + "]"
