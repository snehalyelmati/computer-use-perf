from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.context.snapshot import ElementSnapshot, PageSnapshot, format_snapshot_for_llm
from src.agent.core.agent import _container_prefixes
from src.agent.core.pruning import extract_instruction_phrases, match_phrases_to_elements
from src.agent.core.step_runtime import _selectable_text_priority_ids
from src.agent.core.text_compress import compress_text_lines


def _el(
    stable_id: str,
    *,
    role: str | None = None,
    name: str | None = None,
    text: str | None = None,
    node_name: str | None = "BUTTON",
    attrs: dict[str, str] | None = None,
    parent_chain: tuple[tuple[int, str, str], ...] | None = None,
) -> ElementSnapshot:
    return ElementSnapshot(
        stable_id=stable_id,
        backend_node_id=None,
        node_name=node_name,
        role=role,
        name=name,
        text=text,
        bounding_box=None,
        attributes=attrs or {},
        frame_id=None,
        frame_url=None,
        frame_name=None,
        parent_chain=parent_chain,
    )


def test_container_expansion_includes_parent_siblings() -> None:
    chain_parent = ((10, "div", "mt-4"),)
    chain_child = ((10, "div", "mt-4"), (11, "div", "flex"))
    el_play = _el("el_play", role="button", name="Play Again", parent_chain=chain_child)
    el_complete = _el("el_complete", role="button", name="Complete Challenge", parent_chain=chain_parent)
    el_other = _el("el_other", role="button", name="Next", parent_chain=((1, "div", "footer"),))
    elements = [el_play, el_complete, el_other]

    kept_ids = {"el_play"}
    container_prefixes: set[tuple[tuple[int, str, str], ...]] = set()
    for el in elements:
        if el.stable_id in kept_ids and el.parent_chain:
            for prefix in _container_prefixes(el.parent_chain):
                container_prefixes.add(prefix)

    chain_index: dict[tuple, list[str]] = defaultdict(list)
    for el in elements:
        if el.stable_id in kept_ids or not el.parent_chain:
            continue
        for depth in range(1, len(el.parent_chain) + 1):
            chain_index[el.parent_chain[:depth]].append(el.stable_id)

    for prefix in container_prefixes:
        for sid in chain_index.get(prefix, []):
            kept_ids.add(sid)

    assert "el_complete" in kept_ids


def test_snapshot_format_includes_context_and_widget_hints() -> None:
    snapshot = PageSnapshot(
        url="https://example.test",
        title="Test",
        elements=[
            ElementSnapshot(
                stable_id="el_slider",
                backend_node_id=1,
                node_name="INPUT",
                role="slider",
                name="Volume",
                text=None,
                bounding_box=(10, 20, 100, 20),
                attributes={"type": "range", "min": "0", "max": "100", "value": "50"},
                frame_id=None,
                frame_url=None,
                frame_name=None,
                context='label="Volume control"; row="Audio Volume 50"',
                widget="min=0 max=100 value=50 bbox=10,20,100,20",
            )
        ],
        raw_text=[],
    )

    rendered = format_snapshot_for_llm(snapshot)

    assert "[context: label=\"Volume control\"; row=\"Audio Volume 50\"]" in rendered
    assert "[widget: min=0 max=100 value=50 bbox=10,20,100,20]" in rendered


def test_snapshot_format_reserves_capacity_for_actionable_controls() -> None:
    structural = [
        _el(f"el_text_{idx}", node_name="P", text=f"Instruction paragraph {idx}")
        for idx in range(6)
    ]
    target = _el("el_target", node_name="BUTTON", role="button", name="Submit")
    snapshot = PageSnapshot(
        url="https://example.test",
        title="Test",
        elements=[*structural, target],
        raw_text=[],
    )

    rendered = format_snapshot_for_llm(snapshot, max_elements=3)

    assert "- el_target:" in rendered


def test_instruction_anchored_keep_matches_quoted_phrase() -> None:
    useful_lines = ['Play the audio, then click "Complete" to reveal the real code.']
    phrases = extract_instruction_phrases(useful_lines, oracle_hint=None)
    assert "complete" in phrases

    elements = [
        _el("el_1", role="button", name="Complete Challenge"),
        _el("el_2", role="button", name="Play Again"),
    ]
    matched = match_phrases_to_elements(phrases, elements, max_matches=10)
    assert "el_1" in matched


def test_instruction_anchored_keep_can_prefer_large_clickable_container_over_decoy_button() -> None:
    useful_lines = ['Click "Click Here" three times to reveal the code.']
    phrases = extract_instruction_phrases(useful_lines, oracle_hint=None)
    assert "click here" in phrases

    decoy_button = ElementSnapshot(
        stable_id="el_button",
        backend_node_id=None,
        node_name="BUTTON",
        role="button",
        name="Click Here",
        text=None,
        bounding_box=None,
        attributes={},
        frame_id=None,
        frame_url=None,
        frame_name=None,
        interactive_confidence=None,  # defaults to 0.6 in scoring
        area=14000.0,
        handlers=None,
    )
    clickable_container = ElementSnapshot(
        stable_id="el_container",
        backend_node_id=None,
        node_name="DIV",
        role=None,
        name=None,
        text=None,
        bounding_box=None,
        attributes={"onclick": "1"},
        frame_id=None,
        frame_url=None,
        frame_name=None,
        interactive_reason="detected_handler",
        interactive_confidence=0.95,
        area=400000.0,
        handlers={"click": "handler"},
        descendant_text="Click Here",
    )
    matched = match_phrases_to_elements(phrases, [decoy_button, clickable_container], max_matches=2)
    assert matched and matched[0] == "el_container"


def test_text_selection_intent_keeps_selectable_text_targets() -> None:
    selectable = _el("el_text", node_name="DIV", text="Important paragraph")
    selectable.interactive_reason = "selectable_text"
    selectable.interactive_confidence = 0.25
    paragraph = _el("el_paragraph", node_name="P", text="Semantic paragraph")
    paragraph.interactive_reason = "non_interactive_hint"
    paragraph.interactive_confidence = 0.25
    submit = _el("el_submit", role="button", name="Submit")
    snapshot = PageSnapshot(
        url="https://example.com",
        title="Test",
        elements=[selectable, paragraph, submit],
        raw_text=[],
    )

    kept = _selectable_text_priority_ids(
        snapshot,
        goal="Highlight the text in the paragraph below and submit",
        useful_lines=[],
        valid_ids={"el_text", "el_paragraph", "el_submit"},
        avoid_ids=set(),
    )

    assert kept == ["el_text", "el_paragraph"]


def test_format_snapshot_for_llm_prioritizes_priority_ids_under_max_elements() -> None:
    elements = [_el(f"el_{i}", role="button", name=f"Button {i}") for i in range(10)]
    target = _el("el_target", role="button", name="Complete Challenge")
    snapshot = PageSnapshot(url="https://example.com", title="Test", elements=elements + [target], raw_text=[])

    text = format_snapshot_for_llm(snapshot, max_elements=3, priority_ids=["el_target"])
    assert "- el_target:" in text


@pytest.mark.parametrize("max_lines,max_chars", [(6, 200), (10, 120)])
def test_compress_text_lines_dedupes_and_caps_deterministically(max_lines: int, max_chars: int) -> None:
    lines = [
        "Item 1",
        "Item 2",
        "Item 3",
        "Click the Complete button to proceed",
        "❌ Wrong Button! Try Again!",
        "el_deadbeef1234",
        "AAAAAA",
        "AAAAAA",
    ]
    out = compress_text_lines(lines, max_lines=max_lines, max_chars=max_chars)
    assert out
    assert any("click" in s.lower() for s in out)
    assert any("❌" in s for s in out)
    assert any("el_deadbeef1234" in s for s in out)
