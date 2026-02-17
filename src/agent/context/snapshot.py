"""CDP-based DOM snapshot extraction."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
import time
from urllib.parse import urlparse
from typing import Any, Iterable, Sequence

from playwright.async_api import CDPSession, Page

from src.agent.context.handlers import format_handlers_for_llm, prioritize_handlers

INTERACTIVE_ROLES = {
    "button",
    "checkbox",
    "combobox",
    "link",
    "menuitem",
    "option",
    "radio",
    "slider",
    "spinbutton",
    "switch",
    "tab",
    "textbox",
}

INTERACTIVE_TAGS = {
    "A",
    "BUTTON",
    "INPUT",
    "SELECT",
    "TEXTAREA",
    "OPTION",
    "IFRAME",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass
class ElementSnapshot:
    """Stable element reference for LLM-facing tools."""

    stable_id: str
    backend_node_id: int | None
    node_name: str | None
    role: str | None
    name: str | None
    text: str | None
    bounding_box: tuple[float, float, float, float] | None
    attributes: dict[str, str]
    frame_id: str | None
    frame_url: str | None
    frame_name: str | None
    interactive_reason: str | None = None
    interactive_confidence: float | None = None
    in_viewport: bool | None = None
    area: float | None = None
    parent_chain: tuple[tuple[int, str, str], ...] | None = None
    handlers: dict[str, str] | None = None  # {event_name: truncated_source}


@dataclass(frozen=True)
class SnapshotDiagnostics:
    """Timing and size hints for snapshot capture."""

    durations_ms: dict[str, int]
    size_hints: dict[str, int]


@dataclass
class PageSnapshot:
    """Structured representation of the page for LLM context."""

    url: str
    title: str | None
    elements: Sequence[ElementSnapshot]
    raw_text: Sequence[str]
    viewport_width: int | None = None
    viewport_height: int | None = None
    diagnostics: SnapshotDiagnostics | None = None

@dataclass(frozen=True)
class ElementIndex:
    """Lookup table for stable element ids."""

    elements: dict[str, ElementSnapshot]

def build_element_index(snapshot: PageSnapshot) -> ElementIndex:
    return ElementIndex(elements={element.stable_id: element for element in snapshot.elements})

def build_stable_id(parts: dict[str, Any]) -> str:
    payload = json.dumps(parts, sort_keys=True, ensure_ascii=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"el_{digest[:12]}"

def build_stable_id_from_backend(frame_id: str | None, backend_node_id: int) -> str:
    payload = json.dumps({"frame": frame_id or "", "backend_node_id": int(backend_node_id)}, sort_keys=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"el_{digest[:12]}"

def _tokenize(text: str) -> set[str]:
    tokens = {match.group(0) for match in _TOKEN_RE.finditer(text.lower())}
    return {token for token in tokens if len(token) >= 2}

def _normalize_text(value: str | None) -> str | None:
    if not value:
        return None
    normalized = " ".join(value.split())
    return normalized or None

def _decode_string(value: Any, strings: Sequence[str]) -> str:
    if isinstance(value, int):
        if 0 <= value < len(strings):
            return strings[value]
        return ""
    if isinstance(value, str):
        return value
    return ""

def _decode_string_list(values: Iterable[Any], strings: Sequence[str]) -> list[str]:
    return [_decode_string(value, strings) for value in values]

def attribute_map(raw_attributes: Iterable[Any], strings: Sequence[str]) -> dict[str, str]:
    decoded = _decode_string_list(raw_attributes, strings)
    return {
        decoded[index].lower(): decoded[index + 1]
        for index in range(0, len(decoded) - 1, 2)
    }

def _interactive_reason(
    node_name: str | None,
    role: str | None,
    attributes: dict[str, str],
    cursor: str | None,
) -> tuple[bool, str | None, float]:
    if role and role.lower() in INTERACTIVE_ROLES:
        return True, "role", 1.0
    if node_name and node_name.upper() in INTERACTIVE_TAGS:
        reason = "native_tag"
        confidence = 0.95
        if (node_name or "").upper() == "A" and "href" in attributes:
            reason = "href"
            confidence = 0.98
        return True, reason, confidence
    if "contenteditable" in attributes:
        return True, "contenteditable", 0.9
    if "tabindex" in attributes:
        return True, "tabindex", 0.75
    if "onclick" in attributes:
        return True, "onclick", 0.65
    if "href" in attributes and (node_name or "").upper() == "A":
        return True, "href", 0.98
    if cursor == "pointer":
        return True, "cursor_pointer", 0.35
    return False, None, 0.0

def _should_include_non_interactive(
    node_name: str | None,
    attributes: dict[str, str],
) -> bool:
    if (node_name or "").upper() == "META":
        return True
    for key, value in attributes.items():
        key_lower = key.lower()
        value_lower = (value or "").lower()
        if key_lower.startswith("data-") or key_lower.startswith("aria-"):
            if value_lower or key_lower in {"aria-hidden", "aria-label", "aria-describedby"}:
                return True
        if key_lower in {"hidden", "content", "name", "property", "http-equiv"}:
            return True
        if key_lower == "style" and any(token in value_lower for token in ("display:none", "visibility:hidden")):
            return True
    return False

def _frame_tree_lookup(frame_tree: dict[str, Any]) -> dict[str, dict[str, str]]:
    lookup: dict[str, dict[str, str]] = {}

    def _walk(tree: dict[str, Any]) -> None:
        frame = tree.get("frame", {})
        frame_id = frame.get("id")
        if frame_id:
            lookup[frame_id] = {
                "url": frame.get("url", ""),
                "name": frame.get("name", ""),
            }
        for child in tree.get("childFrames", []) or []:
            _walk(child)

    _walk(frame_tree)
    return lookup

def _layout_bounds(layout: dict[str, Any]) -> dict[int, tuple[float, float, float, float]]:
    if not layout:
        return {}
    node_indices = layout.get("nodeIndex", [])
    bounds = layout.get("bounds", [])
    bounds_map: dict[int, tuple[float, float, float, float]] = {}
    if bounds and isinstance(bounds[0], list):
        for idx, node_index in enumerate(node_indices):
            if idx < len(bounds) and len(bounds[idx]) == 4:
                bounds_map[node_index] = tuple(bounds[idx])
    else:
        stride = 4
        for idx, node_index in enumerate(node_indices):
            start = idx * stride
            if start + 3 < len(bounds):
                bounds_map[node_index] = tuple(bounds[start : start + 4])
    return bounds_map

def _ax_lookup(ax_nodes: Sequence[dict[str, Any]]) -> dict[int, dict[str, str]]:
    lookup: dict[int, dict[str, str]] = {}
    for node in ax_nodes:
        backend_id = node.get("backendDOMNodeId")
        if not backend_id:
            continue
        role_value = node.get("role", {}).get("value")
        name_value = node.get("name", {}).get("value")
        if role_value or name_value:
            lookup[int(backend_id)] = {
                "role": role_value or "",
                "name": name_value or "",
            }
    return lookup

def _stable_id_payload(
    node_name: str | None,
    role: str | None,
    name: str | None,
    text: str | None,
    attributes: dict[str, str],
    frame_id: str | None,
    frame_url: str | None,
) -> dict[str, Any]:
    prioritized_attrs = {
        key: attributes.get(key)
        for key in [
            "id",
            "name",
            "aria-label",
            "aria-labelledby",
            "placeholder",
            "type",
            "title",
            "alt",
            "href",
            "value",
        ]
        if attributes.get(key)
    }
    return {
        "node": node_name or "",
        "role": role or "",
        "name": _normalize_text(name) or "",
        "text": _normalize_text(text) or "",
        "attrs": prioritized_attrs,
        "frame": frame_id or "",
        "frame_url": frame_url or "",
    }

def element_text_blob(element: ElementSnapshot) -> str:
    parts: list[str] = []
    for value in [element.role, element.name, element.text, element.node_name]:
        if value:
            parts.append(str(value))
    attrs = element.attributes or {}
    for key in [
        "id",
        "name",
        "type",
        "placeholder",
        "aria-label",
        "title",
        "alt",
        "href",
        "value",
    ]:
        if value := attrs.get(key):
            parts.append(str(value))
    return " ".join(parts)

def _hostname(url: str | None) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    return parsed.hostname or ""

def rank_elements(
    elements: Sequence[ElementSnapshot],
    *,
    query: str,
    page_url: str,
) -> list[ElementSnapshot]:
    query_tokens = _tokenize(query)
    if not query_tokens:
        return list(elements)

    token_freq: dict[str, int] = {}
    for element in elements:
        for token in _tokenize(element_text_blob(element)):
            token_freq[token] = token_freq.get(token, 0) + 1

    page_host = _hostname(page_url)
    scored: list[tuple[float, str, ElementSnapshot]] = []
    max_score = 0.0
    n = max(1, len(elements))
    for element in elements:
        blob_tokens = _tokenize(element_text_blob(element))
        overlap_tokens = query_tokens & blob_tokens
        score = 0.0
        for token in overlap_tokens:
            freq = token_freq.get(token, 1)
            score += math.log((n + 1.0) / (freq + 1.0)) + 1.0

        confidence = float(element.interactive_confidence or 0.6)
        score *= max(0.1, min(confidence, 1.0))

        if element.in_viewport is False:
            score *= 0.7
        if element.area is not None and element.area > 0:
            # Prefer reasonable-sized targets; avoid tiny icons without hard-dropping them.
            size_factor = min(1.25, max(0.6, math.log10(max(1.0, float(element.area))) / 3.0))
            score *= size_factor

        node_name = (element.node_name or "").upper()
        if node_name == "IFRAME":
            score *= 0.6

        frame_host = _hostname(element.frame_url)
        if page_host and frame_host and frame_host != page_host:
            score *= 0.7

        max_score = max(max_score, score)
        scored.append((score, element.stable_id, element))

    if max_score <= 0.0:
        return list(elements)
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in scored]

def search_elements(
    elements: Sequence[ElementSnapshot],
    *,
    query: str,
    limit: int,
    page_url: str,
) -> list[ElementSnapshot]:
    ranked = rank_elements(elements, query=query, page_url=page_url)
    if limit <= 0:
        return []
    return list(ranked)[:limit]

def unique_stable_id(stable_id: str, counts: dict[str, int]) -> str:
    count = counts.get(stable_id, 0)
    counts[stable_id] = count + 1
    if count == 0:
        return stable_id
    return f"{stable_id}-{count + 1}"

async def capture_snapshot(
    page: Page,
    cdp_session: CDPSession,
    handler_map: dict[str, dict[str, str]] | None = None,
) -> PageSnapshot:
    """Capture a DOM + accessibility snapshot using CDP."""

    durations_ms: dict[str, int] = {}
    size_hints: dict[str, int] = {}

    started = time.perf_counter()
    dom_snapshot = await cdp_session.send(
        "DOMSnapshot.captureSnapshot",
        {
            "computedStyles": ["cursor"],
            "includeDOMRects": True,
            "includePaintOrder": True,
        },
    )
    durations_ms["DOMSnapshot.captureSnapshot"] = int((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    ax_tree = await cdp_session.send("Accessibility.getFullAXTree")
    durations_ms["Accessibility.getFullAXTree"] = int((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    frame_tree = await cdp_session.send("Page.getFrameTree")
    durations_ms["Page.getFrameTree"] = int((time.perf_counter() - started) * 1000)

    ax_lookup = _ax_lookup(ax_tree.get("nodes", []))
    frame_lookup = _frame_tree_lookup(frame_tree.get("frameTree", {}))

    elements: list[ElementSnapshot] = []
    raw_text: list[str] = []
    strings = dom_snapshot.get("strings", [])
    stable_id_counts: dict[str, int] = {}
    size_hints["dom_strings"] = int(len(strings) if isinstance(strings, list) else 0)
    documents = dom_snapshot.get("documents", [])
    size_hints["dom_documents"] = int(len(documents) if isinstance(documents, list) else 0)
    size_hints["ax_nodes"] = int(len(ax_tree.get("nodes", []) or []))
    size_hints["frames"] = int(len(frame_lookup))

    viewport = page.viewport_size or {}
    viewport_width = viewport.get("width") if isinstance(viewport, dict) else None
    viewport_height = viewport.get("height") if isinstance(viewport, dict) else None

    for document in documents:
        nodes = document.get("nodes", {})
        node_names = nodes.get("nodeName", [])
        node_values = nodes.get("nodeValue", [])
        text_values = nodes.get("textValue", [])
        backend_node_ids = nodes.get("backendNodeId", [])
        attributes = nodes.get("attributes", [])
        computed_styles = nodes.get("computedStyles", [])
        parent_indices = nodes.get("parentIndex", [])
        content_document_indices = nodes.get("contentDocumentIndex", [])
        frame_id = document.get("frameId")
        document_frame_meta = frame_lookup.get(frame_id or "", {})
        document_frame_url = document_frame_meta.get("url") or None
        document_frame_name = document_frame_meta.get("name") or None

        bounds_map = _layout_bounds(document.get("layout", {}))

        node_count = len(node_names)
        size_hints["dom_total_nodes"] = size_hints.get("dom_total_nodes", 0) + int(node_count)
        last_text_parent_index: int | None = None
        for index in range(node_count):
            node_name = _decode_string(node_names[index], strings)
            node_value = _decode_string(node_values[index], strings) if index < len(node_values) else ""
            if isinstance(text_values, dict):
                text_value_raw = text_values.get(index, "")
            else:
                text_value_raw = text_values[index] if index < len(text_values) else ""
            text_value = _decode_string(text_value_raw, strings)
            text = _normalize_text(text_value or node_value)
            if node_name == "#text" and text:
                parent_index = None
                if isinstance(parent_indices, list) and index < len(parent_indices):
                    parent_index = parent_indices[index]
                if parent_index is not None and parent_index == last_text_parent_index and raw_text:
                    raw_text[-1] = f"{raw_text[-1]} {text}".strip()
                else:
                    raw_text.append(text)
                last_text_parent_index = parent_index
            else:
                last_text_parent_index = None

            node_attributes = {}
            if index < len(attributes):
                node_attributes = attribute_map(attributes[index], strings)

            cursor = None
            if index < len(computed_styles):
                style_values = computed_styles[index]
                if style_values:
                    cursor = _decode_string(style_values[0], strings)

            element_frame_id = frame_id
            if isinstance(content_document_indices, dict):
                child_document_index = content_document_indices.get(index)
            else:
                child_document_index = (
                    content_document_indices[index] if index < len(content_document_indices) else None
                )
            if isinstance(child_document_index, int) and 0 <= child_document_index < len(documents):
                element_frame_id = documents[child_document_index].get("frameId") or frame_id

            frame_meta = frame_lookup.get(element_frame_id or "", {})
            frame_url = frame_meta.get("url") or document_frame_url
            frame_name = frame_meta.get("name") or document_frame_name

            backend_node_id = None
            if index < len(backend_node_ids):
                backend_node_id = int(backend_node_ids[index])

            ax_info = ax_lookup.get(backend_node_id or -1, {})
            role = ax_info.get("role") or None
            name = _normalize_text(ax_info.get("name") or None)

            is_interactive, interactive_reason, interactive_confidence = _interactive_reason(
                node_name, role, node_attributes, cursor
            )
            if not is_interactive and not _should_include_non_interactive(
                node_name, node_attributes,
            ):
                continue

            stable_id_base: str
            if backend_node_id is not None:
                stable_id_base = build_stable_id_from_backend(element_frame_id, backend_node_id)
            else:
                payload = _stable_id_payload(
                    node_name,
                    role,
                    name,
                    text,
                    node_attributes,
                    frame_id,
                    frame_url,
                )
                stable_id_base = build_stable_id(payload)
            stable_id = unique_stable_id(stable_id_base, stable_id_counts)

            # Build parent chain for tree structure
            parent_chain: tuple[tuple[int, str, str], ...] | None = None
            if parent_indices:
                chain: list[tuple[int, str, str]] = []
                pi = parent_indices[index] if index < len(parent_indices) else -1
                while isinstance(pi, int) and 0 <= pi < node_count:
                    p_name = _decode_string(node_names[pi], strings) if pi < len(node_names) else ""
                    if p_name and p_name.upper() in {"HTML", "#DOCUMENT"}:
                        break
                    p_attrs = attribute_map(attributes[pi], strings) if pi < len(attributes) else {}
                    p_label = p_attrs.get("id") or p_attrs.get("class", "").split()[0] if p_attrs.get("class") else ""
                    if p_name and not p_name.startswith("#"):
                        chain.append((pi, p_name.lower(), p_label))
                    next_pi = parent_indices[pi] if pi < len(parent_indices) else -1
                    if next_pi == pi:
                        break
                    pi = next_pi
                if chain:
                    parent_chain = tuple(reversed(chain))

            # Look up handler data via data-agent-hid marker attribute
            element_handlers: dict[str, str] | None = None
            if handler_map:
                hid = node_attributes.get("data-agent-hid")
                if hid and hid in handler_map:
                    element_handlers = prioritize_handlers(handler_map[hid])
                # Strip the marker so it doesn't leak into attributes / [+attrs] hint
                node_attributes.pop("data-agent-hid", None)

            bbox = bounds_map.get(index)
            in_viewport = _in_viewport(bbox, viewport_width=viewport_width, viewport_height=viewport_height)
            area = None
            if bbox:
                _, _, w, h = bbox
                if w and h and w > 0 and h > 0:
                    area = float(w * h)
            elements.append(
                ElementSnapshot(
                    stable_id=stable_id,
                    backend_node_id=backend_node_id,
                    node_name=node_name,
                    role=role,
                    name=name,
                    text=text,
                    bounding_box=bbox,
                    attributes=node_attributes,
                    frame_id=element_frame_id,
                    frame_url=frame_url,
                    frame_name=frame_name,
                    interactive_reason=interactive_reason if is_interactive else "non_interactive_hint",
                    interactive_confidence=float(interactive_confidence) if is_interactive else 0.25,
                    in_viewport=in_viewport,
                    area=area,
                    parent_chain=parent_chain,
                    handlers=element_handlers if is_interactive else None,
                )
            )

    started = time.perf_counter()
    title = await page.title()
    durations_ms["page.title"] = int((time.perf_counter() - started) * 1000)

    diagnostics = SnapshotDiagnostics(durations_ms=durations_ms, size_hints=size_hints)
    return PageSnapshot(
        url=page.url,
        title=title,
        elements=elements,
        raw_text=raw_text,
        viewport_width=(int(viewport_width) if isinstance(viewport_width, int) else None),
        viewport_height=(int(viewport_height) if isinstance(viewport_height, int) else None),
        diagnostics=diagnostics,
    )


def format_snapshot_for_llm(
    snapshot: PageSnapshot,
    *,
    max_elements: int = 200,
    query: str | None = None,
    priority_ids: Sequence[str] | None = None,
) -> str:
    """Format a snapshot into a compact, LLM-friendly text representation."""

    lines: list[str] = []
    title = snapshot.title or ""
    lines.append(f"URL: {snapshot.url}")
    if title:
        lines.append(f"Title: {title}")
    lines.append("")
    lines.append(f"Interactive elements (showing up to {max_elements}):")

    elements = list(snapshot.elements)
    if priority_ids:
        index = {element.stable_id: element for element in elements}
        prioritized = [index[stable_id] for stable_id in priority_ids if stable_id in index]
        remaining = [element for element in elements if element.stable_id not in set(priority_ids)]
        elements = prioritized + remaining
    if query:
        # Keep user-provided priority IDs at the top, then rank the remainder.
        if priority_ids:
            keep = {stable_id for stable_id in priority_ids}
            head = [element for element in elements if element.stable_id in keep]
            tail = [element for element in elements if element.stable_id not in keep]
            tail = rank_elements(tail, query=query, page_url=snapshot.url)
            elements = head + tail
        else:
            elements = rank_elements(elements, query=query, page_url=snapshot.url)
        lines.append("Elements are sorted by predicted relevance to the goal.")

    elements = elements[:max_elements]

    # --- Build element label ---
    _IMPORTANT_ATTR_KEYS = [
        "id",
        "name",
        "type",
        "placeholder",
        "aria-label",
        "aria-describedby",
        "aria-details",
        "aria-labelledby",
        "aria-hidden",
        "aria-value",
        "aria-valuetext",
        "aria-valuenow",
        "aria-valuemin",
        "aria-valuemax",
        "title",
        "alt",
        "href",
        "value",
        "class",
        "role",
        "content",
        "property",
        "http-equiv",
    ]
    _SHOWN_ATTRS = set(_IMPORTANT_ATTR_KEYS)
    _SEMANTIC_TAGS = frozenset({
        "form", "section", "main", "nav", "aside", "article",
        "dialog", "header", "footer", "ul", "ol", "table", "fieldset",
    })

    def _label_key(el: ElementSnapshot) -> tuple[str, str, str, str]:
        def norm(v: str | None) -> str:
            return " ".join((v or "").lower().split())
        return (norm(el.role), norm(el.name), norm(el.text), norm(el.node_name))

    label_counts: dict[tuple[str, str, str, str], int] = {}
    for el in elements:
        key = _label_key(el)
        label_counts[key] = label_counts.get(key, 0) + 1

    def _element_label(element: ElementSnapshot) -> str:
        role = (element.role or "").strip()
        name = (element.name or "").strip()
        text = (element.text or "").strip()
        tag = (element.node_name or "").strip()
        attrs = element.attributes or {}
        important_attrs = {
            k: attrs.get(k) for k in _IMPORTANT_ATTR_KEYS if attrs.get(k)
        }
        for k, v in attrs.items():
            if k.startswith("data-") and v and k not in important_attrs:
                important_attrs[k] = v
        attr_str = (
            " ".join(f'{k}="{v}"' for k, v in important_attrs.items())
            if important_attrs else ""
        )
        label_parts = [part for part in [role, name, text, tag] if part]
        label = " | ".join(label_parts) if label_parts else "element"
        if attr_str:
            label = f"{label} ({attr_str})"
        # Hints
        hints_parts: list[str] = []
        if element.frame_name or element.frame_url:
            fn = element.frame_name or ""
            fu = element.frame_url or ""
            hints_parts.append(f"[frame: {fn} {fu}]".strip())
        if element.interactive_reason and (element.interactive_confidence or 0.0) < 0.55:
            hints_parts.append(f"reason={element.interactive_reason}")
        if label_counts.get(_label_key(element), 0) > 1 and element.bounding_box:
            x, y, w, h = element.bounding_box
            hints_parts.append(f"bbox={int(round(x))},{int(round(y))},{int(round(w))},{int(round(h))}")
        if element.in_viewport is False:
            hints_parts.append("offscreen")
        has_extra_aria = any(
            k.startswith("aria-") and k not in _SHOWN_ATTRS
            for k in attrs
        )
        if has_extra_aria:
            hints_parts.append("[+attrs]")
        hints = (" " + " ".join(hints_parts)) if hints_parts else ""
        handler_str = ""
        if element.handlers:
            handler_str = " " + format_handlers_for_llm(element.handlers)
        return f"- {element.stable_id}: {label}{hints}{handler_str}"

    # --- Try tree output ---
    has_chains = any(el.parent_chain for el in elements)
    if not has_chains:
        # Fallback: flat list (no parent chain data available)
        for element in elements:
            lines.append(_element_label(element))
        return "\n".join(lines).strip()

    # Build a tree from parent chains.
    # Tree node: dict with "tag", "label", "children" (ordered dict by node_idx),
    #   "elements" (list of ElementSnapshot in DOM order)
    root: dict = {"tag": "", "label": "", "children": {}, "elements": [], "idx": -1}

    for element in elements:
        chain = element.parent_chain or ()
        node = root
        for node_idx, tag, label in chain:
            if node_idx not in node["children"]:
                node["children"][node_idx] = {
                    "tag": tag, "label": label, "children": {}, "elements": [], "idx": node_idx,
                }
            node = node["children"][node_idx]
        node["elements"].append(element)

    # Prune: a container is "meaningful" if it has id/class, is semantic, or is a
    # branching point (multiple child subtrees with interactive elements).
    def _is_meaningful(node: dict) -> bool:
        if node["label"]:
            return True
        if node["tag"] in _SEMANTIC_TAGS:
            return True
        child_count = len(node["children"]) + len(node["elements"])
        if child_count > 1:
            return True
        return False

    def _container_label(node: dict) -> str:
        tag = node["tag"] or "div"
        label = node["label"]
        if label:
            # Determine if label is an id or class
            return f"<{tag} {label}>"
        return f"<{tag}>"

    def _walk_tree(node: dict, depth: int, out: list[str]) -> None:
        # Sort children by node index (DOM order)
        sorted_children = sorted(node["children"].values(), key=lambda n: n["idx"])
        # Interleave containers and elements by index
        items: list[tuple[int, dict | ElementSnapshot]] = []
        for child in sorted_children:
            items.append((child["idx"], child))
        # Elements don't have a node index from parent_chain, but they appear
        # after all children in DOM order. Use a high index.
        for el in node["elements"]:
            el_idx = el.parent_chain[-1][0] + 1 if el.parent_chain else 999999
            items.append((el_idx, el))

        for _, item in sorted(items, key=lambda x: x[0]):
            if isinstance(item, dict):
                # Container node
                if _is_meaningful(item):
                    indent = "  " * depth
                    out.append(f"{indent}{_container_label(item)}")
                    _walk_tree(item, depth + 1, out)
                else:
                    # Skip this level, promote children
                    _walk_tree(item, depth, out)
            else:
                # Interactive element
                indent = "  " * depth
                out.append(f"{indent}{_element_label(item)}")

    tree_lines: list[str] = []
    _walk_tree(root, 0, tree_lines)
    lines.extend(tree_lines)
    return "\n".join(lines).strip()


def _in_viewport(
    bbox: tuple[float, float, float, float] | None,
    *,
    viewport_width: int | None,
    viewport_height: int | None,
) -> bool | None:
    if not bbox or not viewport_width or not viewport_height:
        return None
    x, y, w, h = bbox
    if w <= 0 or h <= 0:
        return False
    left = x
    top = y
    right = x + w
    bottom = y + h
    return not (right < 0 or bottom < 0 or left > viewport_width or top > viewport_height)
