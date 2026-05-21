from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.context.snapshot import ElementIndex, ElementSnapshot, PageSnapshot, format_snapshot_for_llm
from src.agent.tools import semantic
from src.agent.tools.semantic import ToolContext


def _el(
    stable_id: str,
    *,
    node_name: str | None = "BUTTON",
    backend_node_id: int | None = None,
    role: str | None = None,
    name: str | None = None,
    text: str | None = None,
    frame_id: str | None = None,
    frame_url: str | None = None,
    frame_name: str | None = None,
) -> ElementSnapshot:
    return ElementSnapshot(
        stable_id=stable_id,
        backend_node_id=backend_node_id,
        node_name=node_name,
        role=role,
        name=name,
        text=text,
        bounding_box=None,
        attributes={},
        frame_id=frame_id,
        frame_url=frame_url,
        frame_name=frame_name,
    )


@pytest.mark.asyncio
async def test_switch_to_iframe_rejects_non_iframe() -> None:
    element = _el("el_btn", node_name="BUTTON", frame_id="frame_main", frame_url="https://example.com")
    element_index = ElementIndex(elements={"el_btn": element})
    context = ToolContext(
        page=cast(Any, object()),
        cdp_session=cast(Any, object()),
        element_index=element_index,
        frame_sessions={},
        active_frame_id=None,
    )

    result = await semantic.switch_to_iframe("el_btn", context)
    assert result.ok is False
    assert "not an IFRAME" in result.message


@pytest.mark.asyncio
async def test_active_frame_error_explains_main_vs_iframe() -> None:
    page = SimpleNamespace(url="https://example.com")
    iframe_el = _el(
        "el_iframe",
        node_name="IFRAME",
        frame_id="frame_iframe",
        frame_url="https://iframe.example",
    )
    main_el = _el(
        "el_main",
        node_name="BUTTON",
        backend_node_id=123,
        role="button",
        name="Main Button",
        frame_id="frame_main",
        frame_url="https://example.com",
    )
    element_index = ElementIndex(elements={"el_iframe": iframe_el, "el_main": main_el})
    context = ToolContext(
        page=cast(Any, page),
        cdp_session=cast(Any, object()),
        element_index=element_index,
        frame_sessions={},
        active_frame_id="frame_iframe",
    )

    result = await semantic.click_element("el_main", context)
    assert result.ok is False
    assert "main frame" in result.message
    assert "switch_to_main_frame()" in result.message
    assert "el_iframe" in result.message


@pytest.mark.asyncio
async def test_click_option_sets_parent_select(monkeypatch: pytest.MonkeyPatch) -> None:
    option = _el(
        "el_option",
        node_name="OPTION",
        backend_node_id=456,
        role="option",
        text="Appolonia",
        frame_id="frame_main",
        frame_url="https://example.com",
    )
    element_index = ElementIndex(elements={"el_option": option})

    class _FrameSession:
        async def send(self, _method: str, _params: dict[str, Any] | None = None) -> dict[str, Any]:
            return {}

    context = ToolContext(
        page=cast(Any, SimpleNamespace(url="https://example.com")),
        cdp_session=cast(Any, object()),
        element_index=element_index,
        frame_sessions={"frame_main": cast(Any, _FrameSession())},
        active_frame_id=None,
    )

    async def fake_inject_observer(_session: Any) -> bool:
        return True

    async def fake_call_on_node(
        backend_node_id: int,
        _session: Any,
        function_body: str,
        args: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        del args
        assert backend_node_id == 456
        assert "selectedIndex" in function_body
        assert "dispatchEvent(new Event('change'" in function_body
        return {
            "result": {
                "value": {
                    "ok": True,
                    "text": "Appolonia",
                    "value": "Appolonia",
                    "selectedIndex": 3,
                }
            }
        }

    async def fake_collect_mutations_with_ids(
        _session: Any,
        _context: ToolContext,
        _settle_ms: int,
        *,
        frame_id: str | None = None,
        frame_url: str | None = None,
    ) -> dict[str, Any]:
        assert frame_id == "frame_main"
        assert frame_url == "https://example.com"
        return {}

    monkeypatch.setattr(semantic, "_inject_observer", fake_inject_observer)
    monkeypatch.setattr(semantic, "_call_on_node", fake_call_on_node)
    monkeypatch.setattr(semantic, "_collect_mutations_with_ids", fake_collect_mutations_with_ids)

    result = await semantic.click_element("el_option", context)

    assert result.ok is True
    assert 'Selected option el_option text="Appolonia" value="Appolonia" index=3' in result.message


def test_format_snapshot_for_llm_groups_by_frame_and_marks_active() -> None:
    elements = [
        _el("el_iframe", node_name="IFRAME", frame_id="frame_iframe", frame_url="https://iframe.example"),
        _el("el_iframe_btn", node_name="BUTTON", role="button", name="Inside", frame_id="frame_iframe", frame_url="https://iframe.example"),
        _el("el_main_btn", node_name="BUTTON", role="button", name="Outside", frame_id="frame_main", frame_url="https://example.com"),
    ]
    snapshot = PageSnapshot(url="https://example.com", title="Test", elements=elements, raw_text=[])
    rendered = format_snapshot_for_llm(snapshot, max_elements=50, active_frame_id="frame_iframe")

    assert "ACTIVE FRAME: IFRAME el_iframe" in rendered

    lines = rendered.splitlines()
    iframe_header_idx = next(i for i, l in enumerate(lines) if l.startswith("[FRAME: IFRAME el_iframe]"))
    main_header_idx = next(i for i, l in enumerate(lines) if l.startswith("[FRAME: MAIN]"))
    assert iframe_header_idx < main_header_idx


def test_format_snapshot_for_llm_does_not_mislabel_main_when_iframe_url_matches() -> None:
    elements = [
        _el("el_iframe", node_name="IFRAME", frame_id="frame_iframe", frame_url="https://example.com"),
        _el("el_iframe_btn1", node_name="BUTTON", role="button", name="Inside 1", frame_id="frame_iframe", frame_url="https://example.com"),
        _el("el_iframe_btn2", node_name="BUTTON", role="button", name="Inside 2", frame_id="frame_iframe", frame_url="https://example.com"),
        _el("el_main_btn", node_name="BUTTON", role="button", name="Outside", frame_id="frame_main", frame_url="https://example.com"),
    ]
    snapshot = PageSnapshot(url="https://example.com", title="Test", elements=elements, raw_text=[])
    rendered = format_snapshot_for_llm(snapshot, max_elements=50, active_frame_id="frame_iframe")

    assert "ACTIVE FRAME: IFRAME el_iframe" in rendered


@pytest.mark.asyncio
async def test_active_frame_error_still_identifies_main_when_iframe_url_matches() -> None:
    page = SimpleNamespace(url="https://example.com")
    iframe_el = _el(
        "el_iframe",
        node_name="IFRAME",
        frame_id="frame_iframe",
        frame_url="https://example.com",
    )
    main_el = _el(
        "el_main",
        node_name="BUTTON",
        backend_node_id=123,
        role="button",
        name="Main Button",
        frame_id="frame_main",
        frame_url="https://example.com",
    )
    element_index = ElementIndex(elements={"el_iframe": iframe_el, "el_main": main_el})
    context = ToolContext(
        page=cast(Any, page),
        cdp_session=cast(Any, object()),
        element_index=element_index,
        frame_sessions={},
        active_frame_id="frame_iframe",
    )

    result = await semantic.click_element("el_main", context)
    assert result.ok is False
    assert "main frame" in result.message
    assert "switch_to_main_frame()" in result.message
