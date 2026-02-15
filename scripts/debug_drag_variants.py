#!/usr/bin/env python3
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from pathlib import Path

from playwright.async_api import async_playwright

import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.agent.element_utils import extract_elements, format_element_summary


@dataclass
class Variant:
    name: str
    html: str


VARIANTS: list[Variant] = [
    Variant(
        name="html5-dropzone-attrs",
        html="""
        <html><body>
          <style>
            .tile { width: 40px; height: 40px; background: #ccc; display: inline-block; margin: 4px; }
            .zone { width: 80px; height: 80px; border: 2px dashed #999; display: inline-block; margin: 4px; }
          </style>
          <div class="tile" draggable="true">A</div>
          <div class="zone" ondrop="return false" ondragover="return false">Slot 1</div>
        </body></html>
        """,
    ),
    Variant(
        name="aria-dropeffect",
        html="""
        <html><body>
          <div draggable="true">B</div>
          <div aria-dropeffect="move" style="width:80px;height:80px;border:1px solid #999;">Slot 2</div>
        </body></html>
        """,
    ),
    Variant(
        name="role-grid",
        html="""
        <html><body>
          <div draggable="true">C</div>
          <div role="grid" style="width:120px;height:60px;border:1px solid #999;">
            <div role="row">
              <div role="gridcell">Slot 3</div>
            </div>
          </div>
        </body></html>
        """,
    ),
    Variant(
        name="tabindex-container",
        html="""
        <html><body>
          <div draggable="true">D</div>
          <div tabindex="0" style="width:100px;height:70px;border:1px dashed #999;"></div>
        </body></html>
        """,
    ),
    Variant(
        name="label-plus-empty-zone",
        html="""
        <html><body>
          <div draggable="true">E</div>
          <div class="label">Slot 4</div>
          <div class="zone" style="width:80px;height:80px;border:2px solid #666;"></div>
        </body></html>
        """,
    ),
]


async def _run_variant(page, variant: Variant) -> None:
    await page.set_content(variant.html, wait_until="load")
    elements, _handles = await extract_elements(page)
    print("=")
    print(f"Variant: {variant.name}")
    print(f"Elements captured: {len(elements)}")
    summary = format_element_summary(elements)
    droppables = [e for e in elements if e.get("droppable")]
    draggables = [e for e in elements if e.get("draggable")]
    print(f"Draggables: {len(draggables)}, Droppables: {len(droppables)}")
    if droppables:
        print("Droppable elements:")
        for line in summary.splitlines():
            if "[droppable]" in line:
                print(f"  {line}")
    else:
        print("No droppable elements detected.")


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        for variant in VARIANTS:
            await _run_variant(page, variant)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
