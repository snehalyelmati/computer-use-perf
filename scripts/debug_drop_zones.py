#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass


@dataclass
class BlockResult:
    start_line: int
    has_drop_text: bool
    slot_texts: list[str]
    element_slot_lines: list[str]
    element_droppable_lines: list[str]


def _extract_blocks(lines: list[str]) -> list[BlockResult]:
    results: list[BlockResult] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip() == "Interactive elements:":
            start_line = i + 1
            elements: list[str] = []
            i += 1
            while i < len(lines):
                cur = lines[i]
                if cur.startswith(" Hidden content:") or cur.startswith("Data attributes:"):
                    break
                if cur.strip() == "":
                    break
                elements.append(cur.rstrip("\n"))
                i += 1

            page_text: list[str] = []
            while i < len(lines) and lines[i].strip() != "Page content:":
                i += 1
            if i < len(lines) and lines[i].strip() == "Page content:":
                i += 1
                while i < len(lines):
                    cur = lines[i]
                    if cur.strip() == "What should we do next?":
                        break
                    page_text.append(cur.rstrip("\n"))
                    i += 1

            slot_texts = [t for t in page_text if t.strip().lower().startswith("slot ")]
            has_drop_text = any("drop zones" in t.lower() for t in page_text)
            element_slot_lines = [e for e in elements if "slot" in e.lower()]
            element_droppable_lines = [e for e in elements if "[droppable]" in e.lower()]
            results.append(
                BlockResult(
                    start_line=start_line,
                    has_drop_text=has_drop_text,
                    slot_texts=slot_texts,
                    element_slot_lines=element_slot_lines,
                    element_droppable_lines=element_droppable_lines,
                )
            )
        else:
            i += 1
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan verbose logs for drop zone text vs extracted elements."
    )
    parser.add_argument("log_path", help="Path to agent_verbose log")
    parser.add_argument(
        "--max",
        type=int,
        default=10,
        help="Maximum blocks to print (default: 10)",
    )
    args = parser.parse_args()

    with open(args.log_path, "r") as f:
        lines = f.readlines()

    blocks = _extract_blocks(lines)
    flagged = [b for b in blocks if b.has_drop_text]

    print(f"Total blocks: {len(blocks)}")
    print(f"Blocks with drop text: {len(flagged)}")

    shown = 0
    for block in flagged:
        if shown >= args.max:
            break
        slot_preview = ", ".join(s.strip() for s in block.slot_texts[:6])
        print("-")
        print(f"Start line: {block.start_line}")
        print(f"Page slot text: {slot_preview or 'none'}")
        print(f"Element lines with 'slot': {len(block.element_slot_lines)}")
        print(f"Element lines with [droppable]: {len(block.element_droppable_lines)}")
        if block.element_slot_lines:
            print("  Sample element lines:")
            for line in block.element_slot_lines[:5]:
                print(f"  {line}")
        shown += 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
