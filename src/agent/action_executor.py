import asyncio
import re
from playwright.async_api import Page

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
                await asyncio.sleep(0.5)  # Allow page to react
                return f"clicked [{index}]"
            return f"[{index}] not found (only {len(handles)} elements)"

        elif action_type == "type":
            if index < len(handles):
                await handles[index].fill(str(value), force=True, timeout=2000)
                await asyncio.sleep(0.5)  # Allow form to register
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
                await asyncio.sleep(0.5)
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
                await asyncio.sleep(0.5)  # Allow hover effects to appear
                return f"hovered [{index}]"
            return f"[{index}] not found (only {len(handles)} elements)"

        elif action_type == "key":
            await page.keyboard.press(str(value))
            await asyncio.sleep(0.5)
            return f"pressed '{value}'"

        elif action_type == "scroll":
            direction = value or "down"
            amount = 500 if direction == "down" else -500
            await page.evaluate(f"window.scrollBy(0, {amount})")
            return f"scrolled {direction}"

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

        elif action_type == "drag":
            target_text = str(value or "")
            if target_text:
                # If the slot is still an empty leaf with the same text, drag failed
                still_empty = await page.evaluate('''(text) => {
                    const all = document.querySelectorAll('*');
                    for (const el of all) {
                        if (el.children.length === 0 && el.textContent.trim() === text) return true;
                    }
                    return false;
                }''', target_text)
                if still_empty:
                    return f"verify failed: '{target_text}' still empty after drag"
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
