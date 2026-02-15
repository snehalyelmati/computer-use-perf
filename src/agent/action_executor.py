import asyncio
import random
import re
import time
from playwright.async_api import Page
from .config import ACTION_DELAY


async def execute(
    page: Page, action: dict, handles: list, elements: list[dict] | None = None
) -> str:
    """Execute action on page using stored element handles.

    Args:
        page: Playwright page
        action: Action dict from LLM (e.g., {"a": "click", "n": 0})
        handles: List of ElementHandles from extract_elements()
        elements: List of element dicts for context in result messages
    """

    action_type = action.get("a", "error")
    index = action.get("n", 0)
    value = action.get("v", "")

    # Get element label for context in results
    def _el_label(idx):
        if elements and 0 <= idx < len(elements):
            text = (elements[idx].get("text") or "").strip()
            return f'"{text}"' if text else ""
        return ""

    if action_type in ("done", "error"):
        return action_type

    async def _is_disabled(idx: int) -> bool:
        if idx >= len(handles):
            return True
        try:
            return bool(
                await handles[idx].evaluate(
                    "el => !!el.disabled || el.getAttribute('aria-disabled') === 'true'"
                )
            )
        except Exception:
            return False

    try:
        if action_type == "click":
            if index < len(handles):
                if await _is_disabled(index):
                    label = _el_label(index)
                    return f"verify failed: element [{index}] {label} disabled".strip()
                try:
                    await handles[index].dispatch_event("click")
                except Exception:
                    await handles[index].click(force=True, timeout=2000)
                await asyncio.sleep(ACTION_DELAY)  # Allow page to react
                label = _el_label(index)
                return f"clicked [{index}] {label}".strip()
            return f"[{index}] not found (only {len(handles)} elements)"

        elif action_type == "type":
            if index < len(handles):
                await handles[index].fill(str(value), force=True, timeout=2000)
                await asyncio.sleep(ACTION_DELAY)  # Allow form to register
                label = _el_label(index)
                return f"typed '{value}' into [{index}] {label}".strip()
            return f"[{index}] not found (only {len(handles)} elements)"

        elif action_type == "drag":
            target = action.get("t")
            target_text = str(action.get("v", "") or "").strip()
            if target_text:
                target_text = target_text.strip().strip('"').strip("'")

            # Recovery: some models emit the destination index as v="12".
            if target is None and target_text.strip().isdigit():
                target = int(target_text.strip())
                target_text = ""

            if index < len(handles):
                # Find drop target: by element index or by text content
                dst = None
                if target_text:
                    dst = await page.evaluate_handle(
                        """(text) => {
                        const want = (text || '').trim();
                        if (!want) return null;
                        const skip = new Set(['SCRIPT','STYLE','NOSCRIPT','META','LINK','HEAD']);
                        const exact = [];
                        const partial = [];

                        const all = document.querySelectorAll('*');
                        for (const el of all) {
                            try {
                                if (skip.has(el.tagName)) continue;
                                const style = window.getComputedStyle(el);
                                if (!style) continue;
                                if (style.display === 'none' || style.visibility === 'hidden') continue;
                                if (style.pointerEvents === 'none') continue;
                                const opacity = parseFloat(style.opacity || '1');
                                if (!isNaN(opacity) && opacity <= 0.01) continue;

                                const rect = el.getBoundingClientRect();
                                if (!rect || rect.width < 2 || rect.height < 2) continue;

                                const txt = (el.innerText || el.textContent || '').trim();
                                if (!txt) continue;
                                if (txt === want) exact.push(el);
                                else if (txt.includes(want)) partial.push(el);
                            } catch (e) {
                                // ignore
                            }
                        }

                        const pickBest = (arr) => {
                            let best = null;
                            let bestArea = Infinity;
                            for (const el of arr) {
                                const r = el.getBoundingClientRect();
                                const area = (r.width || 0) * (r.height || 0);
                                if (area > 0 && area < bestArea) {
                                    bestArea = area;
                                    best = el;
                                }
                            }
                            return best;
                        };

                        return pickBest(exact) || pickBest(partial) || null;
                    }""",
                        target_text,
                    )
                    if await dst.evaluate("el => el === null"):
                        dst = None
                    else:
                        dst = dst.as_element()

                if dst is None and isinstance(target, int) and target < len(handles):
                    dst = handles[target]
                if dst is None:
                    return f"drop target not found"

                # Use JS DataTransfer events to bypass overlays.
                try:
                    await page.evaluate(
                        """([src, dst]) => {
                        const dt = new DataTransfer();
                        src.dispatchEvent(new DragEvent('dragstart', {bubbles: true, dataTransfer: dt}));
                        dst.dispatchEvent(new DragEvent('dragenter', {bubbles: true, dataTransfer: dt}));
                        dst.dispatchEvent(new DragEvent('dragover', {bubbles: true, dataTransfer: dt}));
                        dst.dispatchEvent(new DragEvent('drop', {bubbles: true, dataTransfer: dt}));
                        src.dispatchEvent(new DragEvent('dragend', {bubbles: true, dataTransfer: dt}));
                    }""",
                        [handles[index], dst],
                    )
                except Exception:
                    # Fallback: native pointer-based drag.
                    await handles[index].drag_to(dst, timeout=2000)
                await asyncio.sleep(ACTION_DELAY)
                label = target_text or (f"[{target}]" if target is not None else "?")
                return f"dragged [{index}] to {label}"
            return f"[{index}] not found (only {len(handles)} elements)"

        elif action_type == "hover":
            if index < len(handles):
                try:
                    await handles[index].hover(timeout=2000)
                except Exception:
                    await handles[index].dispatch_event("mouseenter")
                    await handles[index].dispatch_event("mouseover")
                await asyncio.sleep(ACTION_DELAY)  # Allow hover effects to appear
                label = _el_label(index)
                return f"hovered [{index}] {label}".strip()
            return f"[{index}] not found (only {len(handles)} elements)"

        elif action_type == "draw":
            if index < len(handles):
                # Dispatch mouse events directly on the canvas element via JS
                # to bypass any overlay that would intercept page.mouse events.
                # Uses button/buttons props and rAF timing for reliable registration.
                await page.evaluate(
                    """(canvas) => {
                    return new Promise(resolve => {
                        const rect = canvas.getBoundingClientRect();
                        const m = 10;
                        const rand = (lo, hi) => lo + Math.random() * (hi - lo);
                        const x1 = rand(m, rect.width - m);
                        const y1 = rand(m, rect.height - m);
                        const x2 = rand(m, rect.width - m);
                        const y2 = rand(m, rect.height - m);
                        const fire = (type, x, y, btns) => canvas.dispatchEvent(new MouseEvent(type, {
                            clientX: rect.left + x, clientY: rect.top + y,
                            offsetX: x, offsetY: y,
                            button: 0, buttons: btns,
                            bubbles: true, cancelable: true
                        }));
                        fire('mousedown', x1, y1, 1);
                        const steps = 5;
                        let i = 1;
                        const next = () => {
                            if (i <= steps) {
                                const t = i / steps;
                                fire('mousemove', x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, 1);
                                i++;
                                requestAnimationFrame(next);
                            } else {
                                fire('mouseup', x2, y2, 0);
                                resolve();
                            }
                        };
                        requestAnimationFrame(next);
                    });
                }""",
                    handles[index],
                )
                await asyncio.sleep(ACTION_DELAY)
                label = _el_label(index)
                return f"drew stroke on [{index}] {label}".strip()
            return f"[{index}] not found (only {len(handles)} elements)"

        elif action_type == "key":
            await page.keyboard.press(str(value))
            await asyncio.sleep(ACTION_DELAY)
            return f"pressed '{value}'"

        elif action_type == "scroll":
            try:
                amount = int(value)
            except (ValueError, TypeError):
                amount = 500

            async def _page_scroll(delta: int) -> str:
                # If we are at the top, avoid a no-op "scroll up".
                try:
                    y = await page.evaluate("window.scrollY")
                except Exception:
                    y = 0
                if delta < 0 and (y is None or float(y) <= 1.0):
                    delta = abs(delta)
                await page.evaluate(f"window.scrollBy(0, {delta})")
                return f"scrolled page {delta}px"

            if "n" in action and index < len(handles):
                # Only scroll element containers that are actually scrollable.
                try:
                    is_scrollable = await handles[index].evaluate(
                        "el => el && el.scrollHeight > el.clientHeight + 10"
                    )
                except Exception:
                    is_scrollable = False

                if is_scrollable:
                    try:
                        top = await handles[index].evaluate("el => el.scrollTop")
                    except Exception:
                        top = 0
                    delta = amount
                    if delta < 0 and (top is None or float(top) <= 1.0):
                        delta = abs(delta)
                    await handles[index].evaluate(f"el => el.scrollBy(0, {delta})")
                    label = _el_label(index)
                    return f"scrolled [{index}] {label} {delta}px".strip()

                # Fallback: scroll the page when the indexed element isn't scrollable.
                return await _page_scroll(amount)

            return await _page_scroll(amount)

        elif action_type == "watch":
            text = str(value)
            result = await page.evaluate(
                """(text) => {
                return new Promise((resolve) => {
                    const skip = new Set(['SCRIPT','STYLE','NOSCRIPT','META','LINK','HEAD']);
                    const isDisabled = (el) => {
                        try {
                            return !!el.disabled || el.getAttribute('aria-disabled') === 'true';
                        } catch (e) {
                            return false;
                        }
                    };
                    // Check if element already exists
                    const find = () => {
                        const all = document.querySelectorAll('*');
                        for (const el of all) {
                            if (!skip.has(el.tagName) && el.children.length === 0 && el.textContent.trim().includes(text)) {
                                return el;
                            }
                        }
                        return null;
                    };
                    const existing = find();
                    if (existing) {
                        if (isDisabled(existing)) {
                            resolve("disabled");
                            return;
                        }
                        existing.click();
                        resolve("found");
                        return;
                    }
                    // Watch for it with MutationObserver
                    const timeout = setTimeout(() => { observer.disconnect(); resolve("timeout"); }, 10000);
                    const observer = new MutationObserver(() => {
                        const el = find();
                        if (el) {
                            observer.disconnect();
                            clearTimeout(timeout);
                            if (isDisabled(el)) {
                                resolve("disabled");
                                return;
                            }
                            setTimeout(() => { el.click(); resolve("found"); }, 50);
                        }
                    });
                    observer.observe(document.body, {childList: true, subtree: true, attributes: true, characterData: true});
                });
            }""",
                text,
            )
            if result == "found":
                return f"watched and clicked '{text}'"
            if result == "disabled":
                return f"verify failed: watched element '{text}' disabled"
            return f"watch timeout: '{text}' not found"

        elif action_type == "wait":
            match = re.search(r"[\d.]+", str(value)) if value else None
            seconds = min(float(match.group()) if match else 1, 10)
            # Add a small buffer to account for UI timers/animations.
            seconds = min(seconds + 0.5, 10)
            await asyncio.sleep(seconds)
            return "waited"

    except Exception as e:
        return f"error: {e}"

    return f"unknown: {action_type}"


async def _verify_action(
    page: Page, action: dict, handles: list, result: str
) -> str | None:
    """Verify an action succeeded. Returns None if OK, or an error string."""
    action_type = action.get("a", "")
    index = action.get("n", 0)
    value = action.get("v", "")

    try:
        if action_type == "type" and index < len(handles):
            actual = await handles[index].input_value(timeout=1000)
            if actual.lower() != str(value).lower():
                val_str = str(value)
                if (
                    len(actual) < len(val_str)
                    and val_str.lower().startswith(actual.lower())
                    and len(actual) > 0
                ):
                    return (
                        f"verify failed: field only accepted {len(actual)} chars "
                        f"(maxlength?) - expected '{value}', got '{actual}'"
                    )
                return f"verify failed: expected '{value}', got '{actual}'"

        elif action_type == "click" and index < len(handles):
            # Check handle is still attached (page didn't navigate unexpectedly)
            try:
                await handles[index].is_visible()
            except Exception:
                return f"verify failed: element [{index}] detached after click"

        elif action_type == "drag" and index < len(handles):
            # Drag success is app-specific; avoid aggressive auto-retries.
            try:
                await handles[index].is_visible()
            except Exception:
                pass
    except Exception:
        pass  # Verification errors are non-fatal

    return None


async def execute_batch(
    page: Page, actions: list[dict], handles: list, elements: list[dict] | None = None
) -> list[tuple[dict, str]]:
    """Execute a batch of actions sequentially with verification.

    Stops on error or verification failure so the main loop can re-observe.

    Returns:
        List of (action_dict, result_string) tuples for executed actions.
    """
    results = []

    async def _wait_for_url_change(from_url: str, timeout_s: float) -> bool:
        if page.url != from_url:
            return True
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            if page.url != from_url:
                return True
        return page.url != from_url

    for i, action in enumerate(actions):
        action_type = action.get("a", "error")

        pre_action_url = page.url

        # Stop on terminal actions
        if action_type in ("done", "error"):
            results.append((action, action_type))
            break

        result = await execute(page, action, handles, elements)
        results.append((action, result))

        # Stop batch on execution error
        low = (result or "").lower()
        if (
            low.startswith("error")
            or "not found" in low
            or low.startswith("verify failed")
        ):
            break

        # If a click/key triggered navigation, stop executing further actions.
        if action_type in ("click", "key"):
            if page.url != pre_action_url:
                results[-1] = (action, f"{result} (navigated)")
                break
            if i < len(actions) - 1:
                if await _wait_for_url_change(pre_action_url, timeout_s=0.35):
                    results[-1] = (action, f"{result} (navigated)")
                    break

        # Verify action succeeded
        verify_err = await _verify_action(page, action, handles, result)
        if verify_err:
            results[-1] = (action, verify_err)
            break

    return results
