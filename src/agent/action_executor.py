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
                await handles[index].click(force=True, timeout=2000)
                await asyncio.sleep(0.3)  # Allow page to react
                return f"clicked [{index}]"
            return f"[{index}] not found (only {len(handles)} elements)"

        elif action_type == "type":
            if index < len(handles):
                await handles[index].fill(str(value), force=True, timeout=2000)
                await asyncio.sleep(0.2)  # Allow form to register
                return f"typed '{value}'"
            return f"[{index}] not found (only {len(handles)} elements)"

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
