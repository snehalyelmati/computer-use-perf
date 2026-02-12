"""Debug script to verify canvas drawing works using a local test page."""

import asyncio
import random
import tempfile
import os
from playwright.async_api import async_playwright

TEST_HTML = """<!DOCTYPE html>
<html>
<body>
  <h2>Draw 3 strokes on the canvas</h2>
  <p id="counter">Strokes: 0/3</p>
  <canvas id="canvas" width="400" height="300" style="border:1px solid black;"></canvas>
  <script>
    const canvas = document.getElementById('canvas');
    const ctx = canvas.getContext('2d');
    const counter = document.getElementById('counter');
    let strokes = 0;
    let drawing = false;

    canvas.addEventListener('mousedown', (e) => {
      drawing = true;
      ctx.beginPath();
      ctx.moveTo(e.offsetX, e.offsetY);
    });
    canvas.addEventListener('mousemove', (e) => {
      if (!drawing) return;
      ctx.lineTo(e.offsetX, e.offsetY);
      ctx.stroke();
    });
    canvas.addEventListener('mouseup', () => {
      if (drawing) {
        drawing = false;
        strokes++;
        counter.textContent = 'Strokes: ' + strokes + '/3';
      }
    });
  </script>
</body>
</html>"""


async def draw_stroke(page, canvas):
    """Draw a single stroke on a canvas element."""
    box = await canvas.bounding_box()
    if box is None:
        print("ERROR: canvas has no bounding box")
        return False

    margin = 10
    x1 = random.uniform(box["x"] + margin, box["x"] + box["width"] - margin)
    y1 = random.uniform(box["y"] + margin, box["y"] + box["height"] - margin)
    x2 = random.uniform(box["x"] + margin, box["x"] + box["width"] - margin)
    y2 = random.uniform(box["y"] + margin, box["y"] + box["height"] - margin)

    await page.mouse.move(x1, y1)
    await page.mouse.down()
    steps = 5
    for i in range(1, steps + 1):
        t = i / steps
        await page.mouse.move(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
    await page.mouse.up()
    await asyncio.sleep(0.3)
    return True


async def main():
    # Write test HTML to a temp file
    tmp = tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w")
    tmp.write(TEST_HTML)
    tmp.close()
    file_url = "file://" + tmp.name

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context(viewport={"width": 1280, "height": 720})
            page = await context.new_page()

            print(f"Opening test page: {file_url}")
            await page.goto(file_url, wait_until="domcontentloaded")
            await asyncio.sleep(1.0)

            # Find canvas element
            canvas = await page.query_selector("canvas")
            if canvas is None:
                print("ERROR: No canvas element found!")
                await browser.close()
                return

            box = await canvas.bounding_box()
            print(f"Canvas found: {box['width']}x{box['height']} at ({box['x']}, {box['y']})")

            # Check initial counter
            counter = await page.inner_text("#counter")
            print(f"Before drawing: {counter}")

            # Draw 3 strokes
            for i in range(3):
                print(f"Drawing stroke {i + 1}...")
                ok = await draw_stroke(page, canvas)
                if not ok:
                    print(f"  Stroke {i + 1} failed!")
                    break
                await asyncio.sleep(0.3)
                counter = await page.inner_text("#counter")
                print(f"  After stroke {i + 1}: {counter}")

            # Screenshot
            os.makedirs("logs", exist_ok=True)
            await page.screenshot(path="logs/debug_canvas.png")
            print("Screenshot saved to logs/debug_canvas.png")

            # Final verification
            counter = await page.inner_text("#counter")
            if "3/3" in counter:
                print("SUCCESS: All 3 strokes registered!")
            else:
                print(f"FAIL: Expected 3/3 strokes, got: {counter}")

            await asyncio.sleep(2.0)
            await browser.close()
    finally:
        os.unlink(tmp.name)


if __name__ == "__main__":
    asyncio.run(main())
