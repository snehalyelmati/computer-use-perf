import asyncio
import base64
import codecs
import random
import re
import urllib.parse
from playwright.async_api import Page
from .config import ACTION_DELAY

async def execute(page: Page, action: dict, handles: list) -> str:
    """Execute action on page using stored element handles.

    Args:
        page: Playwright page
        action: Action dict from LLM (e.g., {"a": "click", "n": 0})
        handles: List of ElementHandles from extract_elements()
    """

    action_type = action.get("a", "error")
    index = action.get("n", 0)
    value = action.get("v", "")

    if action_type in ("done", "error"):
        return action_type

    try:
        if action_type == "click":
            if index < len(handles):
                try:
                    await handles[index].dispatch_event("click")
                except Exception:
                    await handles[index].click(force=True, timeout=2000)
                await asyncio.sleep(ACTION_DELAY)  # Allow page to react
                return f"clicked [{index}]"
            return f"[{index}] not found (only {len(handles)} elements)"

        elif action_type == "type":
            if index < len(handles):
                await handles[index].fill(str(value), force=True, timeout=2000)
                await asyncio.sleep(ACTION_DELAY)  # Allow form to register
                return f"typed '{value}'"
            return f"[{index}] not found (only {len(handles)} elements)"

        elif action_type == "drag":
            target = action.get("t", 0)
            target_text = str(action.get("v", ""))
            if index < len(handles):
                # Find drop target: by element index or by text content
                dst = None
                if target_text:
                    dst = await page.evaluate_handle('''(text) => {
                        const all = document.querySelectorAll('*');
                        for (const el of all) {
                            if (el.children.length === 0 && el.textContent.trim() === text) return el;
                        }
                        return null;
                    }''', target_text)
                    if await dst.evaluate('el => el === null'):
                        dst = None
                if dst is None and target < len(handles):
                    dst = handles[target]
                if dst is None:
                    return f"drop target not found"
                # Use JS DataTransfer events to bypass overlays
                await page.evaluate('''([src, dst]) => {
                    const dt = new DataTransfer();
                    src.dispatchEvent(new DragEvent('dragstart', {bubbles: true, dataTransfer: dt}));
                    dst.dispatchEvent(new DragEvent('dragenter', {bubbles: true, dataTransfer: dt}));
                    dst.dispatchEvent(new DragEvent('dragover', {bubbles: true, dataTransfer: dt}));
                    dst.dispatchEvent(new DragEvent('drop', {bubbles: true, dataTransfer: dt}));
                    src.dispatchEvent(new DragEvent('dragend', {bubbles: true, dataTransfer: dt}));
                }''', [handles[index], dst])
                await asyncio.sleep(ACTION_DELAY)
                label = target_text or f"[{target}]"
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
                return f"hovered [{index}]"
            return f"[{index}] not found (only {len(handles)} elements)"

        elif action_type == "draw":
            if index < len(handles):
                # Dispatch mouse events directly on the canvas element via JS
                # to bypass any overlay that would intercept page.mouse events.
                # Uses button/buttons props and rAF timing for reliable registration.
                await page.evaluate('''(canvas) => {
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
                }''', handles[index])
                await asyncio.sleep(ACTION_DELAY)
                return f"drew stroke on canvas [{index}]"
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
            if "n" in action and index < len(handles):
                await handles[index].evaluate(f"el => el.scrollBy(0, {amount})")
                return f"scrolled [{index}] {amount}px"
            await page.evaluate(f"window.scrollBy(0, {amount})")
            return f"scrolled {amount}px"

        elif action_type == "watch":
            text = str(value)
            result = await page.evaluate('''(text) => {
                return new Promise((resolve) => {
                    const skip = new Set(['SCRIPT','STYLE','NOSCRIPT','META','LINK','HEAD']);
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
                            setTimeout(() => { el.click(); resolve("found"); }, 50);
                        }
                    });
                    observer.observe(document.body, {childList: true, subtree: true, attributes: true, characterData: true});
                });
            }''', text)
            if result == "found":
                return f"watched and clicked '{text}'"
            return f"watch timeout: '{text}' not found"

        elif action_type == "decode":
            v = str(value)
            if not v:
                return "decode error: no value provided"
            results = []
            # Base64
            try:
                decoded = base64.b64decode(v, validate=True).decode('utf-8')
                if decoded.isprintable() and len(decoded) >= 1:
                    results.append(f"base64='{decoded}'")
            except Exception:
                pass
            # Hex
            try:
                cleaned = v.replace(' ', '').replace('0x', '').replace(',', '')
                decoded = bytes.fromhex(cleaned).decode('utf-8')
                if decoded.isprintable() and len(decoded) >= 1:
                    results.append(f"hex='{decoded}'")
            except Exception:
                pass
            # ROT13
            try:
                decoded = codecs.decode(v, 'rot_13')
                if decoded != v:
                    results.append(f"rot13='{decoded}'")
            except Exception:
                pass
            # URL encoding
            try:
                decoded = urllib.parse.unquote(v)
                if decoded != v:
                    results.append(f"url='{decoded}'")
            except Exception:
                pass
            # Reverse
            reversed_v = v[::-1]
            if reversed_v != v:
                results.append(f"reverse='{reversed_v}'")
            # Binary (space-separated)
            try:
                if re.match(r'^[01]{7,8}(\s+[01]{7,8})+$', v.strip()):
                    decoded = ''.join(chr(int(b, 2)) for b in v.strip().split())
                    if decoded.isprintable():
                        results.append(f"binary='{decoded}'")
            except Exception:
                pass
            if results:
                return "decoded: " + " | ".join(results)
            return f"decode: no valid decoding found for '{v}'"

        elif action_type == "wait":
            match = re.search(r'[\d.]+', str(value)) if value else None
            seconds = min(float(match.group()) if match else 1, 10)
            await asyncio.sleep(seconds)
            return "waited"

    except Exception as e:
        return f"error: {e}"

    return f"unknown: {action_type}"


async def _verify_action(page: Page, action: dict, handles: list, result: str) -> str | None:
    """Verify an action succeeded. Returns None if OK, or an error string."""
    action_type = action.get("a", "")
    index = action.get("n", 0)
    value = action.get("v", "")

    try:
        if action_type == "type" and index < len(handles):
            actual = await handles[index].input_value(timeout=1000)
            if actual.lower() != str(value).lower():
                return f"verify failed: expected '{value}', got '{actual}'"

        elif action_type == "click" and index < len(handles):
            # Check handle is still attached (page didn't navigate unexpectedly)
            try:
                await handles[index].is_visible()
            except Exception:
                return f"verify failed: element [{index}] detached after click"

        elif action_type == "drag" and index < len(handles):
            # Check if source element moved (parent changed or detached)
            try:
                src_moved = await handles[index].evaluate('''el => {
                    return !el.isConnected || el.offsetParent === null
                        || el.style.display === 'none' || el.style.visibility === 'hidden';
                }''')
                if not src_moved:
                    # Source still in place — auto-retry drag once
                    target = action.get("t", 0)
                    target_text = str(value or "")
                    dst = None
                    if target_text:
                        dst = await page.evaluate_handle('''(text) => {
                            const all = document.querySelectorAll('*');
                            for (const el of all) {
                                if (el.children.length === 0 && el.textContent.trim() === text) return el;
                            }
                            return null;
                        }''', target_text)
                        if await dst.evaluate('el => el === null'):
                            dst = None
                    if dst is None and target < len(handles):
                        dst = handles[target]
                    if dst is not None:
                        await page.evaluate('''([src, dst]) => {
                            const dt = new DataTransfer();
                            src.dispatchEvent(new DragEvent('dragstart', {bubbles: true, dataTransfer: dt}));
                            dst.dispatchEvent(new DragEvent('dragenter', {bubbles: true, dataTransfer: dt}));
                            dst.dispatchEvent(new DragEvent('dragover', {bubbles: true, dataTransfer: dt}));
                            dst.dispatchEvent(new DragEvent('drop', {bubbles: true, dataTransfer: dt}));
                            src.dispatchEvent(new DragEvent('dragend', {bubbles: true, dataTransfer: dt}));
                        }''', [handles[index], dst])
                        await asyncio.sleep(ACTION_DELAY)
            except Exception:
                pass  # Element detached = drag succeeded
    except Exception:
        pass  # Verification errors are non-fatal

    return None


async def execute_batch(page: Page, actions: list[dict], handles: list) -> list[tuple[dict, str]]:
    """Execute a batch of actions sequentially with verification.

    Stops on error or verification failure so the main loop can re-observe.

    Returns:
        List of (action_dict, result_string) tuples for executed actions.
    """
    results = []

    for action in actions:
        action_type = action.get("a", "error")

        # Stop on terminal actions
        if action_type in ("done", "error"):
            results.append((action, action_type))
            break

        result = await execute(page, action, handles)
        results.append((action, result))

        # Stop batch on execution error
        if result.startswith("error") or "not found" in result:
            break

        # Verify action succeeded
        verify_err = await _verify_action(page, action, handles, result)
        if verify_err:
            results[-1] = (action, verify_err)
            break

    return results
