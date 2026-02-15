from playwright.async_api import Page

from . import config
from .text_budget import select_lines_for_budget


async def extract_elements(page: Page) -> tuple[list, list]:
    """Extract interactive elements with indices and return element handles.

    Returns:
        tuple: (metadata_list, element_handles) where indices match between both
    """
    selector = ", ".join(
        [
            "button",
            "input",
            "textarea",
            "select",
            "a[href]",
            "canvas",
            "audio",
            "video",
            "[onclick]",
            "[contenteditable]",
            '[tabindex]:not([tabindex="-1"])',
            '[role="button"]',
            '[role="radio"]',
            '[role="checkbox"]',
            '[role="tab"]',
            '[role="switch"]',
            '[role="menuitem"]',
            '[role="option"]',
            '[role="link"]',
            '[role="slider"]',
            '[draggable="true"]',
            "[ondrop]",
            "[ondragover]",
        ]
    )
    if config.USE_LENIENT_ELEMENT_EXTRACTION:
        handles = await page.query_selector_all("*")
    else:
        selector_handles = await page.query_selector_all(selector)

        # Second pass: find all elements with cursor indicating direct manipulation.
        # Skip containers that wrap interactive children - the child is already captured
        cursor_handles = await page.evaluate_handle("""() => {
            const all = document.querySelectorAll('*');
            const results = [];
            const cursors = new Set(['pointer', 'grab', 'grabbing', 'move']);
            for (const el of all) {
                const cur = (window.getComputedStyle(el).cursor || '').toLowerCase();
                if (cursors.has(cur)) {
                    const interactiveChild = el.querySelector('button, input, textarea, select, a[href], [role="button"], [role="link"], [role="checkbox"], [role="radio"]');
                    if (interactiveChild) continue;
                    results.push(el);
                }
            }
            return results;
        }""")
        cursor_count = await cursor_handles.evaluate("els => els.length")
        cursor_list = []
        for i in range(cursor_count):
            handle = await cursor_handles.evaluate_handle(f"els => els[{i}]")
            cursor_list.append(handle.as_element())

        # Third pass: find scrollable containers (overflow: auto/scroll with hidden content)
        scroll_handles = await page.evaluate_handle("""() => {
            const results = [];
            for (const el of document.querySelectorAll('*')) {
                const style = window.getComputedStyle(el);
                const ov = style.overflowY || style.overflow;
                if ((ov === 'auto' || ov === 'scroll') && el.scrollHeight > el.clientHeight + 10) {
                    results.push(el);
                }
            }
            return results;
        }""")
        scroll_count = await scroll_handles.evaluate("els => els.length")
        scroll_list = []
        for i in range(scroll_count):
            handle = await scroll_handles.evaluate_handle(f"els => els[{i}]")
            scroll_list.append(handle.as_element())

        # Fourth pass: find drag/drop related elements via properties (not just attributes).
        # This catches frameworks that bind handlers via DOM properties or listeners.
        dragdrop_handles = await page.evaluate_handle(
            """() => {
            const results = [];
            const all = document.querySelectorAll('*');
        const dropRoles = new Set([
            'listbox', 'grid', 'tree', 'treegrid', 'table', 'rowgroup', 'tabpanel',
            'gridcell', 'row', 'cell', 'listitem'
        ]);
            for (const el of all) {
                try {
                    const draggable = el.draggable === true;
                    const droppable = !!el.ondrop || !!el.ondragover || !!el.ondragenter || !!el.ondragleave || !!el.ondragend;
                    const hasDropzoneAttr = !!el.getAttribute && !!el.getAttribute('dropzone');
                    const ariaDropeffect = el.getAttribute && el.getAttribute('aria-dropeffect');
                const role = (el.getAttribute && el.getAttribute('role')) || '';
                const hasDropRole = role && dropRoles.has(role.toLowerCase());
                    if (draggable || droppable || hasDropzoneAttr || hasDropRole || (ariaDropeffect && ariaDropeffect !== 'none')) {
                        results.push(el);
                    }
                } catch (e) {
                    // ignore
                }
            }
            return results;
        }"""
        )
        dragdrop_count = await dragdrop_handles.evaluate("els => els.length")
        dragdrop_list = []
        for i in range(dragdrop_count):
            handle = await dragdrop_handles.evaluate_handle(f"els => els[{i}]")
            dragdrop_list.append(handle.as_element())

        # Fifth pass: include elements with event/role/ARIA hints that are often interactive.
        hint_handles = await page.evaluate_handle(
            """() => {
            const results = [];
            const all = document.querySelectorAll('*');
            const allowedRoles = new Set([
                'button', 'link', 'checkbox', 'radio', 'tab', 'switch', 'menuitem', 'option',
                'listbox', 'grid', 'tree', 'treegrid', 'table', 'rowgroup', 'tabpanel',
                'textbox', 'combobox', 'list', 'listitem', 'row', 'cell', 'gridcell'
            ]);
            for (const el of all) {
                try {
                    if (!el.getAttribute) continue;
                    const role = (el.getAttribute('role') || '').toLowerCase();
                    const hasRole = role && allowedRoles.has(role);
                    const ariaHint = Array.from(el.attributes || []).some(attr => attr.name.startsWith('aria-'));
                    const hasTabindex = el.hasAttribute('tabindex');
                    const hasOnEvent = !!el.onclick || !!el.onmousedown || !!el.onmouseup || !!el.onkeydown || !!el.onkeyup || !!el.onkeypress;
                    if (hasRole || ariaHint || hasTabindex || hasOnEvent) {
                        results.push(el);
                    }
                } catch (e) {
                    // ignore
                }
            }
            return results;
        }"""
        )
        hint_count = await hint_handles.evaluate("els => els.length")
        hint_list = []
        for i in range(hint_count):
            handle = await hint_handles.evaluate_handle(f"els => els[{i}]")
            hint_list.append(handle.as_element())

        # Dedup: combine all lists, skip duplicates
        seen = set()
        handles = []
        for handle in selector_handles + cursor_list + scroll_list + dragdrop_list + hint_list:
            if handle is None:
                continue
            uid = await handle.evaluate(
                "el => el.uniqueId || (el.uniqueId = Math.random().toString(36))"
            )
            if uid not in seen:
                seen.add(uid)
                handles.append(handle)

    elements = []
    visible_handles = []

    for handle in handles:
        try:
            if not await handle.is_visible():
                continue

            # Extract metadata from element including role, state, and values
            metadata = await handle.evaluate("""el => {
                const tag = el.tagName.toLowerCase();
                const type = el.type || '';

                // Fix: Proper text extraction with whitespace handling
                const innerText = (el.innerText || '').trim();
                const text = (innerText || el.value || el.placeholder || el.getAttribute('aria-label') || el.title || '').trim();

                const role = el.getAttribute('role') || '';
                const state = el.getAttribute('data-state') || '';
                const disabled = el.disabled || el.getAttribute('aria-disabled') === 'true';
                const href = el.getAttribute('href') || '';

                // Drag/drop metadata
                const draggable = (el.draggable === true) || (el.getAttribute('draggable') === 'true');
                const ariaDropeffect = el.getAttribute('aria-dropeffect');
                const dropRoles = ['listbox', 'grid', 'tree', 'treegrid', 'table', 'rowgroup', 'tabpanel', 'gridcell', 'row', 'cell', 'listitem'];
                const roleLower = role.toLowerCase();
                const hasDropRole = !!roleLower && dropRoles.includes(roleLower);
                const droppable = !!el.ondrop || !!el.ondragover || !!el.ondragenter || !!el.ondragleave || !!el.ondragend
                    || el.hasAttribute('ondrop') || el.hasAttribute('ondragover') || el.hasAttribute('ondragenter') || el.hasAttribute('ondragleave')
                    || !!el.getAttribute('dropzone')
                    || (ariaDropeffect && ariaDropeffect !== 'none')
                    || hasDropRole;

                // Additional metadata for better decision making
                const value = el.value || '';
                const checked = el.checked || el.getAttribute('aria-checked') === 'true';
                const selected = el.getAttribute('aria-selected') === 'true';
                const name = el.name || el.id || '';
                // Capture ALL data-* attributes dynamically
                const dataAttrs = [];
                for (const attr of el.attributes) {
                    if (attr.name.startsWith('data-') && attr.name !== 'data-state' && attr.value) {
                        dataAttrs.push(attr.name + '=' + attr.value);
                    }
                }
                const dataValue = dataAttrs.join('; ');

                let abbr = tag;
                if (tag === 'button') abbr = 'btn';
                else if (tag === 'input') abbr = 'inp';
                else if (tag === 'textarea') abbr = 'txt';
                else if (tag === 'select') abbr = 'sel';
                else if (tag === 'a') abbr = 'link';
                else if (tag === 'canvas') abbr = 'canvas';
                else if (tag === 'audio') abbr = 'audio';
                else if (tag === 'video') abbr = 'video';
                else if (role === 'tab') abbr = 'tab';
                else if (role === 'switch') abbr = 'switch';

                // Media element metadata
                let mediaPlaying = false;
                let mediaDuration = 0;
                let mediaCurrentTime = 0;
                let mediaLoop = false;
                if (tag === 'audio' || tag === 'video') {
                    mediaPlaying = !el.paused && !el.ended;
                    mediaDuration = el.duration || 0;
                    mediaCurrentTime = el.currentTime || 0;
                    mediaLoop = el.loop || false;
                }

                // Detect scrollable container
                const ov = window.getComputedStyle(el).overflowY || window.getComputedStyle(el).overflow;
                const isScrollable = (ov === 'auto' || ov === 'scroll') && el.scrollHeight > el.clientHeight + 10;
                if (isScrollable && !['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON', 'A'].includes(el.tagName)) {
                    abbr = 'scroll';
                }

                const rect = el.getBoundingClientRect();
                return {
                    tag: abbr, text: text, type: type, role: role,
                    state: state, disabled: disabled, href: href,
                    value: value, checked: checked, selected: selected,
                    name: name, dataValue: dataValue,
                    mediaPlaying: mediaPlaying, mediaDuration: mediaDuration,
                    mediaCurrentTime: mediaCurrentTime, mediaLoop: mediaLoop,
                    draggable: draggable,
                    droppable: droppable,
                    bbox: {
                        x: Math.round(rect.x + rect.width / 2),
                        y: Math.round(rect.y + rect.height / 2)
                    }
                };
            }""")

            # Skip elements with no useful content for the LLM
            has_content = (
                metadata["text"]
                or metadata["value"]
                or metadata["name"]
                or metadata["dataValue"]
                or metadata["href"]
            )
            is_core_interactive = metadata["tag"] in (
                "inp",
                "txt",
                "sel",
                "canvas",
                "scroll",
                "audio",
                "video",
            )
            has_semantic_role = metadata["role"] in (
                "button",
                "radio",
                "checkbox",
                "tab",
                "switch",
                "menuitem",
                "option",
                "link",
                "slider",
            )
            is_drag_related = bool(
                metadata.get("draggable") or metadata.get("droppable")
            )
            if (
                not has_content
                and not is_core_interactive
                and not has_semantic_role
                and not is_drag_related
            ):
                if not config.USE_LENIENT_ELEMENT_EXTRACTION:
                    continue
                if metadata.get("bbox"):
                    bbox = metadata["bbox"]
                    if not isinstance(bbox, dict):
                        continue
                    if bbox.get("x") is None or bbox.get("y") is None:
                        continue
                else:
                    continue

            # Assign sequential index that matches position in visible_handles
            metadata["index"] = len(elements)
            elements.append(metadata)
            visible_handles.append(handle)

        except Exception:
            # Element may have been removed from DOM
            continue

    return elements, visible_handles


def format_element_summary(elements: list, max_elements: int | None = None) -> str:
    """Format elements with rich annotations (state, checked, disabled, value, dataValue, name).

    Used by both overview and action LLMs for consistent element representation.

    Args:
        elements: List of element metadata dicts from extract_elements()
        max_elements: If set, truncate to this many elements and append count of remaining
    """
    if max_elements is None:
        subset = elements
    elif max_elements <= 0:
        subset = []
    else:
        subset = elements[:max_elements]
    el_strs = []

    for el in subset:
        # Use role if available, otherwise tag
        tag = el.get("role") or el["tag"]

        # Build state string with new metadata
        state = ""
        if el.get("state"):
            state = f" [{el['state']}]"
        if el.get("disabled"):
            state += " [disabled]"
        if el.get("checked"):
            state += " [checked]"
        if el.get("selected"):
            state += " [selected]"
        if el.get("draggable"):
            state += " [draggable]"
        if el.get("droppable"):
            state += " [droppable]"

        # Media state for audio/video elements
        if el.get("tag") in ("audio", "video"):
            if el.get("mediaLoop"):
                state += " [loop]"
            if el.get("mediaPlaying"):
                remaining = el.get("mediaDuration", 0) - el.get("mediaCurrentTime", 0)
                state += f" [playing, {int(remaining)}s remaining]"
            else:
                state += " [paused]"

        text = el["text"] if el["text"] else el["type"] or "?"

        # Show current value for inputs and radio/checkbox (helps LLM know state and identify correct options)
        value_info = ""
        if el.get("value") and (
            el["tag"] == "inp" or el.get("role") in ("radio", "checkbox")
        ):
            value_info = f' value="{el["value"]}"'

        # Show data-value/data-code if present (might contain answer)
        if el.get("dataValue"):
            value_info += f' data="{el["dataValue"]}"'

        # Show name/id for form field identification
        name_info = ""
        if el.get("name"):
            name_info = f" ({el['name']})"

        # Include href for links
        href = el.get("href", "")
        if href and href != "#":
            el_strs.append(
                f'[{el["index"]}] {tag} "{text}"{name_info} -> {href}{state}'
            )
        else:
            el_strs.append(
                f'[{el["index"]}] {tag} "{text}"{name_info}{value_info}{state}'
            )

    if max_elements and len(elements) > max_elements:
        el_strs.append(f"... and {len(elements) - max_elements} more elements")

    return "\n".join(el_strs)


def format_elements_by_proximity(
    elements: list,
    last_pos: tuple[int, int] | None = None,
    proximity_threshold: int = 200,
) -> str:
    """Format elements separated into Nearby and Other sections.

    Args:
        elements: List of element metadata dicts with bbox
        last_pos: (x, y) center of last interacted element, or None
        proximity_threshold: Max distance in pixels to be considered "nearby"
    """
    if not last_pos or not elements:
        return format_element_summary(elements)

    def distance(el):
        bbox = el.get("bbox")
        if not bbox:
            return float("inf")
        return ((bbox["x"] - last_pos[0]) ** 2 + (bbox["y"] - last_pos[1]) ** 2) ** 0.5

    nearby = []
    other = []
    for el in elements:
        if distance(el) <= proximity_threshold:
            nearby.append(el)
        else:
            other.append(el)

    # Sort nearby by distance (closest first)
    nearby.sort(key=distance)

    parts = []
    if nearby:
        parts.append("=== NEARBY ELEMENTS (from last action) ===")
        parts.append(format_element_summary(nearby))
    if other:
        parts.append("\n=== OTHER ELEMENTS ===")
        parts.append(format_element_summary(other))

    return "\n".join(parts)


def format_context(
    goal: str,
    objective: str | None,
    data: str | None,
    task: str | None,
    next_intent: str | None,
    elements: list,
) -> str:
    """Format the analysis and elements for the action LLM.

    Args:
        goal: The current goal from overview
        data: Discovered data values (may be None)
        task: TASK DSL text to translate (may be None)
        next_intent: Natural language intent (optional; not executed)
        elements: List of element metadata dicts
    """
    parts = []
    parts.append("=== PAGE ANALYSIS ===")
    parts.append(f"GOAL: {goal}")
    if objective:
        parts.append(f"OBJECTIVE: {objective}")
    if data:
        parts.append(f"DATA: {data}")
    if task:
        parts.append("TASK:")
        parts.append(task)
    if next_intent:
        parts.append(f"NEXT (intent only): {next_intent}")
    parts.append("\n=== INTERACTIVE ELEMENTS ===")

    # Budget the element list for the Action model.
    full = format_element_summary(elements)

    def _score_el_line(line: str) -> int:
        s = (line or "").lower()
        score = 0
        if " inp " in s or " txt " in s or " sel " in s or "textbox" in s:
            score += 50
        if " btn " in s or " button " in s or "role=button" in s:
            score += 25
        if ' data="' in s or ' value="' in s:
            score += 30
        if "[draggable]" in s or "[droppable]" in s:
            score += 50
        if " link " in s:
            score += 20
        if " scroll " in s:
            score += 10
        if s.strip():
            score += 5
        if "disabled" in s:
            score += 5
        return score

    kept = select_lines_for_budget(
        full.splitlines(),
        max_chars=config.ELEMENT_SUMMARY_BUDGET_CHARS,
        score_fn=_score_el_line,
    )
    if kept:
        parts.append("\n".join(kept))
    elif config.ELEMENT_SUMMARY_BUDGET_CHARS <= 0:
        parts.append("[elements omitted due to budget]")
    else:
        parts.append(full)
    return "\n".join(parts)
