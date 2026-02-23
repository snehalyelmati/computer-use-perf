"""Verify Group B memory pipeline changes."""

import re
import sys

# ---------------------------------------------------------------------------
# 1. Structured memory entries
# ---------------------------------------------------------------------------
print("=== 1. Structured memory entries ===")

# Simulate what agent.py now does
class FakeTracker:
    def __init__(self, success: int, failure: int):
        self.success_count = success
        self.failure_count = failure

tracker = FakeTracker(success=2, failure=1)
step = 5
summary = "Clicked the submit button"

tool_status = f"{tracker.success_count} ok, {tracker.failure_count} failed"
memory_entry = f"[step {step}, {tool_status}] {summary}"

expected = "[step 5, 2 ok, 1 failed] Clicked the submit button"
assert memory_entry == expected, f"FAIL: {memory_entry!r} != {expected!r}"
print(f"  OK: {memory_entry}")

# ---------------------------------------------------------------------------
# 2. Memory deduplication in _format_memory()
# ---------------------------------------------------------------------------
print("\n=== 2. Memory deduplication ===")

# Import the actual function
from src.agent.core.agent import _format_memory

# 2a. Five consecutive identical entries (different step numbers, same content)
memory = [
    f"[step {i}, 0 ok, 1 failed] Clicked element el_abc but nothing happened"
    for i in range(1, 6)
]
result = _format_memory(memory)
print(f"  Input: 5 identical entries with different step numbers")
print(f"  Output:\n    {result.replace(chr(10), chr(10) + '    ')}")
assert "(x5)" in result, f"FAIL: expected (x5) in output"
assert result.count("\n") == 0, f"FAIL: expected single line, got {result.count(chr(10)) + 1}"
print("  OK: collapsed to single line with (x5)")

# 2b. Mixed entries: 2 same + 1 different + 2 same
memory = [
    "[step 1, 1 ok, 0 failed] Filled form field",
    "[step 2, 1 ok, 0 failed] Filled form field",
    "[step 3, 0 ok, 1 failed] Clicked submit but failed",
    "[step 4, 2 ok, 0 failed] Scrolled down",
    "[step 5, 2 ok, 0 failed] Scrolled down",
]
result = _format_memory(memory)
print(f"\n  Input: 2 same + 1 different + 2 same")
print(f"  Output:\n    {result.replace(chr(10), chr(10) + '    ')}")
lines = result.strip().split("\n")
assert len(lines) == 3, f"FAIL: expected 3 lines, got {len(lines)}"
assert "(x2)" in lines[0], f"FAIL: expected (x2) in first line"
assert "(x2)" in lines[2], f"FAIL: expected (x2) in third line"
assert "(x" not in lines[1], f"FAIL: middle line should not have count"
print("  OK: 3 lines with correct counts")

# 2c. Empty memory
result = _format_memory([])
assert result == "None.", f"FAIL: empty memory should return 'None.', got {result!r}"
print("\n  OK: empty memory returns 'None.'")

# 2d. No duplicates - should pass through unchanged
memory = [
    "[step 1, 1 ok, 0 failed] Action A",
    "[step 2, 0 ok, 1 failed] Action B",
    "[step 3, 2 ok, 0 failed] Action C",
]
result = _format_memory(memory)
lines = result.strip().split("\n")
assert len(lines) == 3, f"FAIL: expected 3 lines, got {len(lines)}"
assert all("(x" not in line for line in lines), "FAIL: no counts expected"
print("  OK: no duplicates pass through unchanged")

# ---------------------------------------------------------------------------
# 3. Worker cross-step context
# ---------------------------------------------------------------------------
print("\n=== 3. Worker cross-step context ===")

from src.agent.prompts.system import STEP_PROMPT

# 3a. Verify STEP_PROMPT has the "Recent steps" instruction
assert "Recent steps" in STEP_PROMPT, "FAIL: STEP_PROMPT missing 'Recent steps' instruction"
print("  OK: STEP_PROMPT contains 'Recent steps' instruction")

# 3b. Simulate worker context construction
from dataclasses import dataclass

@dataclass
class FakeState:
    memory: list[str]

@dataclass
class FakeConfig:
    worker_context_steps: int

state = FakeState(memory=[
    "[step 1, 1 ok, 0 failed] Clicked login button",
    "[step 2, 2 ok, 0 failed] Filled username field",
    "[step 3, 0 ok, 1 failed] Clicked submit but element not found",
])
config = FakeConfig(worker_context_steps=3)

# Replicate the logic from agent.py
worker_context = ""
if state.memory and config.worker_context_steps > 0:
    recent = state.memory[-config.worker_context_steps:]
    worker_context = "\n\nRecent steps:\n" + "\n".join(f"- {m}" for m in recent)

assert "Recent steps:" in worker_context, "FAIL: missing 'Recent steps:' header"
assert worker_context.count("- [step") == 3, "FAIL: expected 3 step entries"
print(f"  OK: worker_context includes 3 recent steps")

# 3c. Empty memory = no context
state_empty = FakeState(memory=[])
worker_context_empty = ""
if state_empty.memory and config.worker_context_steps > 0:
    recent = state_empty.memory[-config.worker_context_steps:]
    worker_context_empty = "\n\nRecent steps:\n" + "\n".join(f"- {m}" for m in recent)

assert worker_context_empty == "", "FAIL: empty memory should produce no context"
print("  OK: empty memory produces no worker context")

# 3d. worker_context_steps=0 disables context
config_disabled = FakeConfig(worker_context_steps=0)
worker_context_disabled = ""
if state.memory and config_disabled.worker_context_steps > 0:
    recent = state.memory[-config_disabled.worker_context_steps:]
    worker_context_disabled = "\n\nRecent steps:\n" + "\n".join(f"- {m}" for m in recent)

assert worker_context_disabled == "", "FAIL: worker_context_steps=0 should disable context"
print("  OK: worker_context_steps=0 disables context")

# 3e. Check AgentConfig has the field
from src.agent.config import AgentConfig
ac = AgentConfig()
assert ac.worker_context_steps == 3, f"FAIL: default should be 3, got {ac.worker_context_steps}"
print("  OK: AgentConfig.worker_context_steps defaults to 3")

print("\n=== All checks passed! ===")
