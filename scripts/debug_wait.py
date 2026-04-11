"""Debug script to verify the wait tool reports DOM mutations."""

import asyncio

from playwright.async_api import async_playwright

from src.agent.context.snapshot import capture_snapshot, build_element_index
from src.agent.tools.semantic import ToolContext, wait

MUTATION_HTML = """
<!DOCTYPE html>
<html>
<body>
  <div id="container">
    <div id="text">Initial</div>
    <button id="btn" aria-expanded="false">Toggle</button>
    <div id="remove-me">Remove me</div>
  </div>
  <script>
    setTimeout(() => {
      document.getElementById('text').textContent = 'Updated text';
      const added = document.createElement('span');
      added.id = 'added';
      added.textContent = 'Added text';
      document.getElementById('container').appendChild(added);
      const btn = document.getElementById('btn');
      btn.setAttribute('aria-expanded', 'true');
      const remove = document.getElementById('remove-me');
      remove.parentNode.removeChild(remove);
    }, 300);
  </script>
</body>
</html>
"""

NO_MUTATION_HTML = """
<!DOCTYPE html>
<html>
<body>
  <div id="container">
    <div id="text">Initial</div>
  </div>
</body>
</html>
"""


def _has_all(message: str, substrings: list[str]) -> bool:
    return all(substring in message for substring in substrings)


async def run_scenario(label: str, html: str, wait_ms: int, expectations: list[str]) -> bool:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 800, "height": 600})
        page = await context.new_page()
        await page.set_content(html)
        await page.wait_for_load_state("domcontentloaded")
        cdp = await context.new_cdp_session(page)

        snapshot = await capture_snapshot(page, cdp)
        index = build_element_index(snapshot)
        tool_ctx = ToolContext(page=page, cdp_session=cdp, element_index=index)
        result = await wait(wait_ms, tool_ctx)

        passed = result.ok and _has_all(result.message, expectations)
        print(f"[{label}] ok={result.ok} passed={passed}")
        print(result.message)

        await context.close()
        await browser.close()
        return passed


async def main() -> None:
    print("\n--- wait tool mutation test ---")
    mutation_expectations = [
        "Changes during wait",
        "New text appeared:",
        "Attribute changes:",
        "Elements added:",
        "Elements removed:",
    ]
    mutation_passed = await run_scenario(
        "mutations",
        MUTATION_HTML,
        600,
        mutation_expectations,
    )

    print("\n--- wait tool no-mutation test ---")
    no_mutation_expectations = ["No changes during wait"]
    no_mutation_passed = await run_scenario(
        "no_mutations",
        NO_MUTATION_HTML,
        400,
        no_mutation_expectations,
    )

    all_passed = mutation_passed and no_mutation_passed
    print(f"\nOverall: {'ALL PASSED' if all_passed else 'SOME FAILED'}")


if __name__ == "__main__":
    asyncio.run(main())
