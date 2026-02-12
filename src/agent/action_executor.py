import asyncio
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
            await asyncio.sleep(min(float(value) if value else 1, 3))
            return "waited"

    except Exception as e:
        return f"error: {e}"

    return f"unknown: {action_type}"
