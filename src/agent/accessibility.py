"""Accessibility tree operations for element extraction via CDP."""

from playwright.async_api import Page

from . import config
from .text_budget import select_lines_for_budget

# Roles we consider interactive (lowercase for comparison)
INTERACTIVE_ROLES = {
    "button",
    "link",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "textbox",
    "searchbox",
    "combobox",
    "listbox",
    "spinbutton",
    "checkbox",
    "radio",
    "switch",
    "slider",
    "tab",
    "option",
    "treeitem",
    "canvas",
    "disclosuretriangle",  # <details>/<summary> elements
}


def filter_interactive_nodes(ax_nodes: list[dict]) -> list[dict]:
    """Filter to interactive elements only."""
    nodes = []
    for node in ax_nodes:
        if node.get("ignored"):
            continue
        role = node.get("role", {}).get("value", "").lower()
        if role not in INTERACTIVE_ROLES:
            continue
        backend_id = node.get("backendDOMNodeId")
        if not backend_id:
            continue
        nodes.append(node)
    return nodes


def _str_to_bool(val):
    """Convert a11y string property to bool. Returns None for non-bool strings."""
    if val is True or val == "true":
        return True
    if val is False or val == "false":
        return False
    if val == "mixed":  # For tri-state checkboxes
        return "mixed"
    return None


def _role_to_abbr(role: str) -> str:
    """Map a11y role to display abbreviation matching existing format."""
    mapping = {
        "button": "btn",
        "link": "link",
        "textbox": "inp",
        "searchbox": "inp",
        "combobox": "sel",
        "listbox": "sel",
        "checkbox": "checkbox",
        "radio": "radio",
        "switch": "switch",
        "slider": "slider",
        "tab": "tab",
        "canvas": "canvas",
        "option": "option",
        "menuitem": "menu",
        "menuitemcheckbox": "menu",
        "menuitemradio": "menu",
        "treeitem": "tree",
        "spinbutton": "inp",
        "disclosuretriangle": "details",  # <details>/<summary>
    }
    return mapping.get(role.lower(), role if role else "?")


def _build_ancestor_labels(ax_nodes: list[dict], interactive_node_ids: set) -> dict:
    """Build a map of nodeId -> ancestor container label for tree display.

    Returns dict mapping interactive nodeId to its nearest named container.
    """
    # Build node lookup and parent map
    node_map = {n.get("nodeId"): n for n in ax_nodes}

    # Container roles we want to show in hierarchy
    CONTAINER_ROLES = {
        "form",
        "dialog",
        "group",
        "region",
        "list",
        "listbox",
        "menu",
        "menubar",
        "navigation",
        "main",
        "complementary",
        "contentinfo",
        "banner",
        "radiogroup",
        "tablist",
        "toolbar",
    }

    ancestor_labels = {}

    for node_id in interactive_node_ids:
        # Walk up to find nearest named container
        current = node_map.get(node_id)
        ancestors = []
        while current:
            parent_id = current.get("parentId")
            if not parent_id:
                break
            parent = node_map.get(parent_id)
            if parent:
                role = parent.get("role", {}).get("value", "").lower()
                name = parent.get("name", {}).get("value", "")
                if role in CONTAINER_ROLES and name:
                    ancestors.append(f"{role}:{name}")
                elif role in CONTAINER_ROLES:
                    ancestors.append(role)
            current = parent

        # Store the path (reversed so outermost first)
        if ancestors:
            ancestor_labels[node_id] = " > ".join(reversed(ancestors))

    return ancestor_labels


def format_full_a11y_tree(ax_nodes: list[dict], interactive_indices: set[int]) -> str:
    """Format full a11y tree for LLM in ASCII tree format, sorted by importance.

    Priority (highest first):
    1. Potential codes (short alphanumeric strings)
    2. Page structure (containers)
    3. Other static text
    """
    import re

    # Roles to skip entirely (noise)
    SKIP_ROLES = {"none", "generic", "rootwebarea", "InlineTextBox", "LineBreak"}

    # Container roles (show as structure)
    CONTAINER_ROLES = {
        "form",
        "dialog",
        "group",
        "region",
        "list",
        "listbox",
        "menu",
        "menubar",
        "navigation",
        "main",
        "complementary",
        "contentinfo",
        "banner",
        "radiogroup",
        "tablist",
        "toolbar",
        "article",
        "section",
    }

    code_candidates = []
    containers = []
    other_text = []

    for node in ax_nodes:
        if node.get("ignored"):
            continue

        role = node.get("role", {}).get("value", "").lower()
        name = node.get("name", {}).get("value", "") or ""
        node_id = node.get("nodeId")

        if role in SKIP_ROLES:
            continue

        # Skip interactive elements (shown separately with indices)
        if node_id in interactive_indices:
            continue

        # Containers with names
        if role in CONTAINER_ROLES and name:
            containers.append(f"{role}: {name}")
            continue

        # Static text
        if role == "statictext" and name.strip():
            text = name.strip()
            # Potential codes (4-10 alphanumeric chars)
            if re.match(r"^[A-Z0-9]{4,10}$", text):
                code_candidates.append(text)
            elif len(text) > 2:
                other_text.append(text)

    # Build markdown tree output
    lines = []

    if code_candidates:
        lines.append("### Potential Codes")
        for code in code_candidates:
            lines.append(f"- **{code}**")

    if containers:
        lines.append("### Page Structure")
        for c in containers:
            lines.append(f"- {c}")

    if other_text:
        lines.append("### Page Text")
        seen = set()
        for t in other_text:
            if t not in seen:
                seen.add(t)
                lines.append(f"- {t}")

    def _score(line: str) -> int:
        s = line.strip()
        if s.startswith("###"):
            return 100
        if s.startswith("- **"):
            return 80
        return 10

    kept = select_lines_for_budget(
        lines,
        max_chars=config.A11Y_TREE_BUDGET_CHARS,
        score_fn=_score,
    )
    return "\n".join(kept)


async def extract_from_accessibility_tree(page: Page) -> tuple[list[dict], list, str]:
    """Fetch a11y tree, filter interactive nodes, and resolve to ElementHandles.

    Uses a single CDP session for efficiency.

    Returns:
        tuple: (metadata_list, element_handles, a11y_tree_text) where indices match between both
    """
    cdp = await page.context.new_cdp_session(page)
    metadata_list = []
    handles = []
    a11y_tree_text = ""

    try:
        # Fetch full accessibility tree
        result = await cdp.send("Accessibility.getFullAXTree")
        ax_nodes = result.get("nodes", [])

        # Filter to interactive nodes
        interactive_nodes = filter_interactive_nodes(ax_nodes)

        # Build ancestor labels for tree display
        interactive_node_ids = {
            node_id
            for n in interactive_nodes
            for node_id in [n.get("nodeId")]
            if node_id is not None
        }
        ancestor_labels = _build_ancestor_labels(ax_nodes, interactive_node_ids)

        for i, node in enumerate(interactive_nodes):
            backend_id = node.get("backendDOMNodeId")
            unique_id = f"a11y-{i}"

            # Resolve and inject ID
            try:
                result = await cdp.send(
                    "DOM.resolveNode", {"backendNodeId": backend_id}
                )
                object_id = result.get("object", {}).get("objectId")
                if not object_id:
                    continue

                await cdp.send(
                    "Runtime.callFunctionOn",
                    {
                        "objectId": object_id,
                        "functionDeclaration": f"function() {{ this.setAttribute('data-a11y-id', '{unique_id}'); }}",
                    },
                )
            except Exception:
                continue

            # Query back to get ElementHandle
            handle = await page.query_selector(f'[data-a11y-id="{unique_id}"]')
            if not handle or not await handle.is_visible():
                continue

            # Get bbox
            bbox = await handle.bounding_box()
            if not bbox or bbox["width"] <= 0 or bbox["height"] <= 0:
                continue

            # Extract metadata from a11y node
            # NOTE: A11y properties are strings ('true'/'false') not booleans!
            props = {
                p["name"]: p.get("value", {}).get("value")
                for p in node.get("properties", [])
            }

            # Get href, data attrs, name, and type from DOM (not in a11y tree)
            href = ""
            data_value = ""
            name = ""
            input_type = ""
            try:
                dom_info = await handle.evaluate("""el => {
                    const dataAttrs = [];
                    for (const attr of el.attributes) {
                        if (attr.name.startsWith('data-') && attr.name !== 'data-a11y-id' && attr.value) {
                            dataAttrs.push(attr.name + '=' + attr.value);
                        }
                    }
                    return {
                        href: el.getAttribute('href') || '',
                        dataValue: dataAttrs.join('; '),
                        name: el.name || el.id || '',
                        type: el.type || ''
                    };
                }""")
                href = dom_info.get("href", "")
                data_value = dom_info.get("dataValue", "")
                name = dom_info.get("name", "")
                input_type = dom_info.get("type", "")
            except Exception:
                pass

            role_value = node.get("role", {}).get("value", "")
            node_id = node.get("nodeId")
            metadata = {
                "index": len(metadata_list),
                "tag": _role_to_abbr(role_value),
                "role": role_value,
                "type": input_type,
                "text": node.get("name", {}).get("value", "") or "",
                "value": node.get("value", {}).get("value", "") or "",
                "disabled": _str_to_bool(props.get("disabled")) or False,
                "checked": _str_to_bool(props.get("checked")),
                "selected": _str_to_bool(props.get("selected")) or False,
                "expanded": _str_to_bool(props.get("expanded")),
                "required": _str_to_bool(props.get("required")) or False,
                "readonly": _str_to_bool(props.get("readonly")) or False,
                "focusable": _str_to_bool(props.get("focusable")) or False,
                "state": "",  # Legacy field, kept for compatibility
                "name": name,
                "dataValue": data_value,
                "href": href,
                "container": ancestor_labels.get(node_id, ""),
                "bbox": {
                    "x": int(bbox["x"] + bbox["width"] / 2),
                    "y": int(bbox["y"] + bbox["height"] / 2),
                },
            }
            metadata_list.append(metadata)
            handles.append(handle)

        # Format full tree for LLM (after we know which nodes got indices)
        indexed_node_ids = {
            node_id
            for n in interactive_nodes
            for node_id in [n.get("nodeId")]
            if node_id is not None
        }
        a11y_tree_text = format_full_a11y_tree(ax_nodes, indexed_node_ids)
    finally:
        await cdp.detach()

    return metadata_list, handles, a11y_tree_text
