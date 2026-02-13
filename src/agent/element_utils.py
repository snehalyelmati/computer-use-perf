from playwright.async_api import Page

async def extract_elements(page: Page) -> tuple[list, list]:
    """Extract interactive elements with indices and return element handles.

    Returns:
        tuple: (metadata_list, element_handles) where indices match between both
    """
    selector = ', '.join([
        'button', 'input', 'textarea', 'select', 'a[href]', 'canvas',
        'audio', 'video',
        '[onclick]', '[contenteditable]', '[tabindex]:not([tabindex="-1"])',
        '[role="button"]', '[role="radio"]', '[role="checkbox"]',
        '[role="tab"]', '[role="switch"]', '[role="menuitem"]',
        '[role="option"]', '[role="link"]', '[role="slider"]',
        '[draggable="true"]', '[ondrop]', '[ondragover]'
    ])
    selector_handles = await page.query_selector_all(selector)

    # Second pass: find all elements with cursor:pointer computed style
    # Skip containers that wrap interactive children — the child is already captured
    cursor_handles = await page.evaluate_handle('''() => {
        const all = document.querySelectorAll('*');
        const results = [];
        for (const el of all) {
            if (window.getComputedStyle(el).cursor === 'pointer') {
                const interactiveChild = el.querySelector('button, input, textarea, select, a[href], [role="button"], [role="link"], [role="checkbox"], [role="radio"]');
                if (interactiveChild) continue;
                results.push(el);
            }
        }
        return results;
    }''')
    cursor_count = await cursor_handles.evaluate('els => els.length')
    cursor_list = []
    for i in range(cursor_count):
        handle = await cursor_handles.evaluate_handle(f'els => els[{i}]')
        cursor_list.append(handle.as_element())

    # Third pass: find scrollable containers (overflow: auto/scroll with hidden content)
    scroll_handles = await page.evaluate_handle('''() => {
        const results = [];
        for (const el of document.querySelectorAll('*')) {
            const style = window.getComputedStyle(el);
            const ov = style.overflowY || style.overflow;
            if ((ov === 'auto' || ov === 'scroll') && el.scrollHeight > el.clientHeight + 10) {
                results.push(el);
            }
        }
        return results;
    }''')
    scroll_count = await scroll_handles.evaluate('els => els.length')
    scroll_list = []
    for i in range(scroll_count):
        handle = await scroll_handles.evaluate_handle(f'els => els[{i}]')
        scroll_list.append(handle.as_element())

    # Dedup: combine all lists, skip duplicates
    seen = set()
    handles = []
    for handle in selector_handles + cursor_list + scroll_list:
        if handle is None:
            continue
        uid = await handle.evaluate('el => el.uniqueId || (el.uniqueId = Math.random().toString(36))')
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
            metadata = await handle.evaluate('''el => {
                const tag = el.tagName.toLowerCase();
                const type = el.type || '';

                // Fix: Proper text extraction with whitespace handling
                const innerText = (el.innerText || '').trim();
                const text = (innerText || el.value || el.placeholder || el.getAttribute('aria-label') || el.title || '').trim();

                const role = el.getAttribute('role') || '';
                const state = el.getAttribute('data-state') || '';
                const disabled = el.disabled || el.getAttribute('aria-disabled') === 'true';
                const href = el.getAttribute('href') || '';

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
                    bbox: {
                        x: Math.round(rect.x + rect.width / 2),
                        y: Math.round(rect.y + rect.height / 2)
                    }
                };
            }''')

            # Skip elements with no useful content for the LLM
            has_content = (metadata['text'] or metadata['value'] or metadata['name'] or
                           metadata['dataValue'] or metadata['href'])
            is_core_interactive = metadata['tag'] in ('inp', 'txt', 'sel', 'canvas', 'scroll', 'audio', 'video')
            has_semantic_role = metadata['role'] in ('button', 'radio', 'checkbox', 'tab', 'switch', 'menuitem', 'option', 'link', 'slider')
            if not has_content and not is_core_interactive and not has_semantic_role:
                continue

            # Assign sequential index that matches position in visible_handles
            metadata['index'] = len(elements)
            elements.append(metadata)
            visible_handles.append(handle)

        except Exception:
            # Element may have been removed from DOM
            continue

    return elements, visible_handles

def format_element_summary(elements: list, max_elements: int = None) -> str:
    """Format elements with rich annotations (state, checked, disabled, value, dataValue, name).

    Used by both overview and action LLMs for consistent element representation.

    Args:
        elements: List of element metadata dicts from extract_elements()
        max_elements: If set, truncate to this many elements and append count of remaining
    """
    subset = elements[:max_elements] if max_elements else elements
    el_strs = []

    for el in subset:
        # Use role if available, otherwise tag
        tag = el.get('role') or el['tag']

        # Build state string with new metadata
        state = ""
        if el.get('state'):
            state = f" [{el['state']}]"
        if el.get('disabled'):
            state += " [disabled]"
        if el.get('checked'):
            state += " [checked]"
        if el.get('selected'):
            state += " [selected]"

        # Media state for audio/video elements
        if el.get('tag') in ('audio', 'video'):
            if el.get('mediaLoop'):
                state += " [loop]"
            if el.get('mediaPlaying'):
                remaining = el.get('mediaDuration', 0) - el.get('mediaCurrentTime', 0)
                state += f" [playing, {int(remaining)}s remaining]"
            else:
                state += " [paused]"

        text = el["text"] if el["text"] else el["type"] or "?"

        # Show current value for inputs and radio/checkbox (helps LLM know state and identify correct options)
        value_info = ""
        if el.get('value') and (el['tag'] == 'inp' or el.get('role') in ('radio', 'checkbox')):
            value_info = f" value=\"{el['value']}\""

        # Show data-value/data-code if present (might contain answer)
        if el.get('dataValue'):
            value_info += f" data=\"{el['dataValue']}\""

        # Show name/id for form field identification
        name_info = ""
        if el.get('name'):
            name_info = f" ({el['name']})"

        # Include href for links
        href = el.get('href', '')
        if href and href != '#':
            el_strs.append(f"[{el['index']}] {tag} \"{text}\"{name_info} -> {href}{state}")
        else:
            el_strs.append(f"[{el['index']}] {tag} \"{text}\"{name_info}{value_info}{state}")

    if max_elements and len(elements) > max_elements:
        el_strs.append(f"... and {len(elements) - max_elements} more elements")

    return "\n".join(el_strs)


def format_elements_by_proximity(elements: list, last_pos: tuple = None,
                                  proximity_threshold: int = 200) -> str:
    """Format elements separated into Nearby and Other sections.

    Args:
        elements: List of element metadata dicts with bbox
        last_pos: (x, y) center of last interacted element, or None
        proximity_threshold: Max distance in pixels to be considered "nearby"
    """
    if not last_pos or not elements:
        return format_element_summary(elements)

    def distance(el):
        bbox = el.get('bbox')
        if not bbox:
            return float('inf')
        return ((bbox['x'] - last_pos[0])**2 + (bbox['y'] - last_pos[1])**2) ** 0.5

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


def format_context(overview: str, elements: list) -> str:
    """Format the analysis and elements for the action LLM.

    Note: Elements now have sequential indices (0, 1, 2...) that match
    the element handles list, so we show them all without reordering.
    """

    parts = []

    # Overview from analysis
    parts.append("=== PAGE ANALYSIS ===")
    parts.append(overview)

    # Elements - show all with enriched info (role, state)
    parts.append("\n=== INTERACTIVE ELEMENTS ===")
    parts.append(format_element_summary(elements))

    return "\n".join(parts)
