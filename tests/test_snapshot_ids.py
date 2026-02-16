from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.context import snapshot


def test_build_stable_id_is_deterministic() -> None:
    payload = {
        "node": "BUTTON",
        "role": "button",
        "name": "Submit",
        "text": "Submit",
        "attrs": {"id": "submit"},
        "frame": "frame-1",
        "frame_url": "https://example.com",
    }
    first = snapshot.build_stable_id(payload)
    second = snapshot.build_stable_id(payload)
    assert first == second
    assert first.startswith("el_")


def test_unique_stable_id_suffixes_duplicates() -> None:
    counts: dict[str, int] = {}
    base = "el_123456789abc"
    first = snapshot.unique_stable_id(base, counts)
    second = snapshot.unique_stable_id(base, counts)
    third = snapshot.unique_stable_id(base, counts)
    assert first == base
    assert second == f"{base}-2"
    assert third == f"{base}-3"


def test_attribute_map_decodes_pairs() -> None:
    strings = ["id", "login", "type", "submit"]
    raw = [0, 1, 2, 3]
    result = snapshot.attribute_map(raw, strings)
    assert result == {"id": "login", "type": "submit"}


def test_attribute_map_normalizes_case() -> None:
    strings = ["DATA-ID", "value"]
    raw = [0, 1]
    result = snapshot.attribute_map(raw, strings)
    assert result == {"data-id": "value"}


def test_is_interactive_cursor_pointer() -> None:
    is_interactive, reason, confidence = snapshot._interactive_reason("DIV", None, {}, "pointer")
    assert is_interactive is True
    assert reason == "cursor_pointer"
    assert confidence < 0.6
