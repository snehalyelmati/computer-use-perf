"""CDP-based DOM snapshot extraction."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Iterable, Sequence

from playwright.async_api import CDPSession, Page

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


@dataclass
class PageSnapshot:
    """Structured representation of the page for LLM context."""

    url: str
    title: str | None
    elements: Sequence[ElementSnapshot]
    raw_text: Sequence[str]

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

def _is_interactive(
    node_name: str | None,
    role: str | None,
    attributes: dict[str, str],
    cursor: str | None,
) -> bool:
    if role and role.lower() in INTERACTIVE_ROLES:
        return True
    if node_name and node_name.upper() in INTERACTIVE_TAGS:
        return True
    if "contenteditable" in attributes:
        return True
    if "tabindex" in attributes:
        return True
    if "onclick" in attributes:
        return True
    if "href" in attributes and (node_name or "").upper() == "A":
        return True
    if cursor == "pointer":
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

def unique_stable_id(stable_id: str, counts: dict[str, int]) -> str:
    count = counts.get(stable_id, 0)
    counts[stable_id] = count + 1
    if count == 0:
        return stable_id
    return f"{stable_id}-{count + 1}"

async def capture_snapshot(page: Page, cdp_session: CDPSession) -> PageSnapshot:
    """Capture a DOM + accessibility snapshot using CDP."""

    dom_snapshot = await cdp_session.send(
        "DOMSnapshot.captureSnapshot",
        {
            "computedStyles": ["cursor"],
            "includeDOMRects": True,
            "includePaintOrder": True,
        },
    )
    ax_tree = await cdp_session.send("Accessibility.getFullAXTree")
    frame_tree = await cdp_session.send("Page.getFrameTree")

    ax_lookup = _ax_lookup(ax_tree.get("nodes", []))
    frame_lookup = _frame_tree_lookup(frame_tree.get("frameTree", {}))

    elements: list[ElementSnapshot] = []
    raw_text: list[str] = []
    strings = dom_snapshot.get("strings", [])
    stable_id_counts: dict[str, int] = {}

    documents = dom_snapshot.get("documents", [])
    for document in documents:
        nodes = document.get("nodes", {})
        node_names = nodes.get("nodeName", [])
        node_values = nodes.get("nodeValue", [])
        text_values = nodes.get("textValue", [])
        backend_node_ids = nodes.get("backendNodeId", [])
        attributes = nodes.get("attributes", [])
        computed_styles = nodes.get("computedStyles", [])
        content_document_indices = nodes.get("contentDocumentIndex", [])
        frame_id = document.get("frameId")
        document_frame_meta = frame_lookup.get(frame_id or "", {})
        document_frame_url = document_frame_meta.get("url") or None
        document_frame_name = document_frame_meta.get("name") or None

        bounds_map = _layout_bounds(document.get("layout", {}))

        node_count = len(node_names)
        for index in range(node_count):
            node_name = _decode_string(node_names[index], strings)
            node_value = _decode_string(node_values[index], strings) if index < len(node_values) else ""
            text_value = _decode_string(text_values[index], strings) if index < len(text_values) else ""
            text = _normalize_text(text_value or node_value)
            if node_name == "#text" and text:
                raw_text.append(text)

            node_attributes = {}
            if index < len(attributes):
                node_attributes = attribute_map(attributes[index], strings)

            cursor = None
            if index < len(computed_styles):
                style_values = computed_styles[index]
                if style_values:
                    cursor = _decode_string(style_values[0], strings)

            element_frame_id = frame_id
            if index < len(content_document_indices):
                child_document_index = content_document_indices[index]
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

            if not _is_interactive(node_name, role, node_attributes, cursor):
                continue

            payload = _stable_id_payload(
                node_name,
                role,
                name,
                text,
                node_attributes,
                frame_id,
                frame_url,
            )
            stable_id = unique_stable_id(build_stable_id(payload), stable_id_counts)
            elements.append(
                ElementSnapshot(
                    stable_id=stable_id,
                    backend_node_id=backend_node_id,
                    node_name=node_name,
                    role=role,
                    name=name,
                    text=text,
                    bounding_box=bounds_map.get(index),
                    attributes=node_attributes,
                    frame_id=element_frame_id,
                    frame_url=frame_url,
                    frame_name=frame_name,
                )
            )

    title = await page.title()
    return PageSnapshot(url=page.url, title=title, elements=elements, raw_text=raw_text)
