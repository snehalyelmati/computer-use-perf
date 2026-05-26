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
    _iter_svg_text_fallback_candidates,
    _looks_like_semantic_icon_control,
    _looks_like_selectable_text_container,
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


def test_format_snapshot_for_llm_shows_bbox_for_unlabeled_graphics() -> None:
    elements = [
        _el(
            "el_svg_text",
            role="generic",
            name=None,
            text=None,
            node_name="text",
            bbox=(240, 246, 22, 44),
        ),
    ]
    elements[0].descendant_text = "3"
    snapshot = PageSnapshot(url="https://example.com/", title="Test", elements=elements, raw_text=[])
    rendered = format_snapshot_for_llm(snapshot, max_elements=5)

    assert "- el_svg_text: generic | 3 | text" in rendered
    assert "bbox=240,246,22,44" in rendered


def test_format_snapshot_for_llm_includes_graphics_summary() -> None:
    elements = [
        _el("el_svg", role="image", name=None, text=None, node_name="svg", bbox=(0, 0, 160, 160)),
    ]
    elements[0].graphics = 'line x1=20 y1=20 x2=120 y2=80; text text="45" x=40 y=30'
    snapshot = PageSnapshot(url="https://example.com/", title="Test", elements=elements, raw_text=[])
    rendered = format_snapshot_for_llm(snapshot, max_elements=5)

    assert "[graphics: line x1=20 y1=20 x2=120 y2=80" in rendered
    assert 'text="45"' in rendered


def test_graphics_summary_participates_in_query_ranking_before_truncation() -> None:
    elements = [
        _el("el_aaaaaaaaaaaa", role="button", name="Unrelated"),
        _el("el_zzzzzzzzzzzz", role="image", name=None, text=None, node_name="svg", bbox=(0, 0, 160, 160)),
    ]
    elements[1].graphics = 'circle text="needle-target" cx=10 cy=10'
    snapshot = PageSnapshot(url="https://example.com/", title="Test", elements=elements, raw_text=[])

    rendered = format_snapshot_for_llm(snapshot, max_elements=1, query="needle-target")

    assert "el_zzzzzzzzzzzz" in rendered
    assert "el_aaaaaaaaaaaa" not in rendered


def test_svg_text_fallback_candidates_are_capped_by_attempts() -> None:
    elements = [
        _el(f"el_{index}", node_name="circle", attrs={"data-index": str(index)})
        for index in range(45)
    ]
    for index, element in enumerate(elements):
        element.backend_node_id = index + 1

    candidates = list(_iter_svg_text_fallback_candidates(elements, limit=40))

    assert len(candidates) == 40
    assert candidates[-1].stable_id == "el_39"


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


def test_semantic_icon_control_uses_class_hint_and_bbox() -> None:
    assert _looks_like_semantic_icon_control(
        "SPAN",
        {"class": "reply"},
        (10, 20, 14, 14),
        name=None,
        text=None,
    )
    assert not _looks_like_semantic_icon_control(
        "SPAN",
        {"class": "spacer"},
        (10, 20, 14, 14),
        name=None,
        text=None,
    )


def test_selectable_text_container_accepts_leaf_text_div() -> None:
    assert _looks_like_selectable_text_container(
        "DIV",
        {"id": "content"},
        (10, 20, 240, 24),
        "Highlight this paragraph",
        has_direct_text=True,
        child_element_count=0,
    )


def test_selectable_text_container_rejects_large_wrapper() -> None:
    assert not _looks_like_selectable_text_container(
        "DIV",
        {"id": "page"},
        (0, 0, 1200, 900),
        "Header Form body Submit Footer",
        has_direct_text=False,
        child_element_count=8,
    )


def test_selectable_text_container_rejects_missing_text() -> None:
    assert not _looks_like_selectable_text_container(
        "DIV",
        {"id": "empty"},
        (10, 20, 240, 24),
        None,
        has_direct_text=True,
        child_element_count=0,
    )


def test_class_labeled_icon_control_participates_in_query_ranking() -> None:
    icon = _el(
        "el_reply",
        role=None,
        name=None,
        text=None,
        node_name="SPAN",
        bbox=(10, 20, 14, 14),
        attrs={"class": "reply"},
    )
    icon.interactive_reason = "semantic_icon"
    icon.interactive_confidence = 0.55
    icon.context = 'nearby="Ada @target Example post"'
    other = _el("el_other", role=None, name=None, text=None, node_name="DIV")
    other.descendant_text = "Ada @target Example post"
    snapshot = PageSnapshot(url="https://example.com/", title="Test", elements=[other, icon], raw_text=[])

    rendered = format_snapshot_for_llm(snapshot, max_elements=1, query="reply @target")

    assert "el_reply" in rendered
    assert 'class="reply"' in rendered
    assert 'nearby="Ada @target Example post"' in rendered
    assert "el_other" not in rendered
