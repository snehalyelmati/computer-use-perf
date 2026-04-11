from __future__ import annotations

from collections import defaultdict

import pytest

from src.agent.context.snapshot import ElementSnapshot, PageSnapshot, format_snapshot_for_llm
from src.agent.core.agent import _container_prefixes
from src.agent.core.pruning import extract_instruction_phrases, match_phrases_to_elements
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
