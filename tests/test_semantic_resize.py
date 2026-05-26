from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.context.snapshot import ElementSnapshot
from src.agent.tools.semantic import (
    _OBSERVER_INJECT_JS,
    _is_unqualified_text_selection_control,
    _resize_change_verified,
)


def test_resize_change_verified_checks_requested_direction() -> None:
    assert _resize_change_verified(
        before_width=100,
        before_height=50,
        after_width=80,
        after_height=50,
        delta_width=-20,
        delta_height=0,
    )
    assert not _resize_change_verified(
        before_width=100,
        before_height=50,
        after_width=100,
        after_height=50,
        delta_width=-20,
        delta_height=0,
    )
    assert not _resize_change_verified(
        before_width=100,
        before_height=50,
        after_width=120,
        after_height=50,
        delta_width=-20,
        delta_height=0,
    )


def test_mutation_observer_tracks_style_changes() -> None:
    assert "'style'" in _OBSERVER_INJECT_JS


def test_resize_fallback_measures_wrapper_before_target_dimensions() -> None:
    assert "const base = wrapper && wrapper !== this ? wrapper : this;" in _resize_fallback_source()
    assert "const before = base.getBoundingClientRect();" in _resize_fallback_source()
    assert "const after = base.getBoundingClientRect();" in _resize_fallback_source()
    assert "const targets = wrapper && wrapper !== this ? [wrapper, this] : [this];" in _resize_fallback_source()


def test_unqualified_select_text_rejects_submit_controls() -> None:
    button = ElementSnapshot(
        stable_id="el_submit",
        backend_node_id=1,
        node_name="BUTTON",
        role="button",
        name="Submit",
        text=None,
        bounding_box=None,
        attributes={},
        frame_id=None,
        frame_url=None,
        frame_name=None,
    )

    assert _is_unqualified_text_selection_control(button, None)
    assert not _is_unqualified_text_selection_control(button, "Submit")


def _resize_fallback_source() -> str:
    import inspect

    from src.agent.tools.semantic import resize_element

    return inspect.getsource(resize_element)
