from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.context.snapshot import build_stable_id_from_backend


def test_backend_based_stable_id_is_deterministic() -> None:
    first = build_stable_id_from_backend("frame_1", 123)
    second = build_stable_id_from_backend("frame_1", 123)
    assert first == second


def test_backend_based_stable_id_changes_with_backend_or_frame() -> None:
    a = build_stable_id_from_backend("frame_1", 123)
    b = build_stable_id_from_backend("frame_1", 124)
    c = build_stable_id_from_backend("frame_2", 123)
    assert a != b
    assert a != c

