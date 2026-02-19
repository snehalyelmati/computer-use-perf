from __future__ import annotations

from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.context.snapshot import (
    ElementSnapshot,
    PageSnapshot,
    format_snapshot_for_llm,
    rank_elements,
    sanitize_class_value,
    search_elements,
)


def _el(
    stable_id: str,
    *,
    role: str | None = None,
    name: str | None = None,
    text: str | None = None,
    node_name: str | None = "BUTTON",
    bbox: tuple[float, float, float, float] | None = None,
    attrs: dict[str, str] | None = None,
    frame_url: str | None = None,
) -> ElementSnapshot:
    return ElementSnapshot(
        stable_id=stable_id,
        backend_node_id=None,
        node_name=node_name,
        role=role,
        name=name,
        text=text,
        bounding_box=bbox,
        attributes=attrs or {},
        frame_id=None,
        frame_url=frame_url,
        frame_name=None,
    )


def test_rank_elements_prioritizes_goal_overlap() -> None:
    elements = [
        _el("el_buy", role="button", name="Buy now"),
        _el("el_next", role="button", name="Next Page"),
        _el("el_help", role="link", name="Help"),
    ]
    ranked = rank_elements(elements, query="next page", page_url="https://example.com/")
    assert ranked[0].stable_id == "el_next"


def test_search_elements_is_deterministic() -> None:
    elements = [
        _el("el_a", role="button", name="Next"),
        _el("el_b", role="button", name="Next"),
        _el("el_c", role="button", name="Continue"),
    ]
    first = [e.stable_id for e in search_elements(elements, query="next", limit=2, page_url="https://example.com/")]
    second = [e.stable_id for e in search_elements(elements, query="next", limit=2, page_url="https://example.com/")]
    assert first == second


def test_format_snapshot_for_llm_adds_bbox_for_duplicate_labels() -> None:
    elements = [
        _el("el_left", role="button", name="Next", bbox=(10, 10, 100, 30)),
        _el("el_right", role="button", name="Next", bbox=(200, 10, 100, 30)),
        _el("el_other", role="button", name="Cancel", bbox=(10, 60, 100, 30)),
    ]
    snapshot = PageSnapshot(url="https://example.com/", title="Test", elements=elements, raw_text=[])
    text = format_snapshot_for_llm(snapshot, max_elements=10, query="next")

    lines = [line for line in text.splitlines() if line.startswith("- ")]
    left_line = next(line for line in lines if line.startswith("- el_left:"))
    right_line = next(line for line in lines if line.startswith("- el_right:"))
    other_line = next(line for line in lines if line.startswith("- el_other:"))

    assert re.search(r"\bbbox=\d+,\d+,\d+,\d+\b", left_line)
    assert re.search(r"\bbbox=\d+,\d+,\d+,\d+\b", right_line)
    assert "bbox=" not in other_line


def test_format_snapshot_for_llm_uses_descendant_text_for_unlabeled_containers() -> None:
    elements = [
        _el(
            "el_container",
            role=None,
            name=None,
            text=None,
            node_name="DIV",
            attrs={},
        ),
    ]
    elements[0].descendant_text = "Hidden DOM Challenge: click here 3 more times to reveal"
    snapshot = PageSnapshot(url="https://example.com/", title="Test", elements=elements, raw_text=[])
    text = format_snapshot_for_llm(snapshot, max_elements=5)
    assert "Hidden DOM Challenge" in text


def test_format_snapshot_for_llm_sanitizes_class_and_shows_disabled_boolean_attr() -> None:
    elements = [
        _el(
            "el_btn",
            role="button",
            name="Click Here",
            node_name="BUTTON",
            attrs={
                "class": "w-24 h-24 cursor-pointer bg-gradient-to-br z-[100] fooBar",
                "disabled": "",
            },
        )
    ]
    snapshot = PageSnapshot(url="https://example.com/", title="Test", elements=elements, raw_text=[])
    rendered = format_snapshot_for_llm(snapshot, max_elements=5)
    assert 'class="cursor-pointer fooBar"' in rendered
    assert "w-24" not in rendered
    assert 'disabled=""' in rendered


def test_sanitize_class_value_aggressive_drops_utility_like_tokens() -> None:
    out = sanitize_class_value(
        "w-24 h-24 cursor-pointer bg-gradient-to-br z-[100] fooBar",
        mode="aggressive",
        max_tokens=6,
        max_chars=80,
        fallback_tokens=2,
    )
    assert out == "cursor-pointer fooBar"
