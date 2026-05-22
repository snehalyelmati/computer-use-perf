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


def test_observer_collect_js_defines_attrs_helper_before_use() -> None:
    script = semantic._OBSERVER_COLLECT_JS
    assert script.index("function attrsOf") < script.index("attrs: attrsOf(el)")
    assert "const SVG_ATTRS" in script


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


@pytest.mark.asyncio
async def test_click_at_dispatches_relative_coordinates(monkeypatch: pytest.MonkeyPatch) -> None:
    element = _el("el_svg", node_name="SVG", backend_node_id=123, role="image")
    element_index = ElementIndex(elements={"el_svg": element})
    events: list[tuple[str, dict[str, Any] | None]] = []

    class _Session:
        async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            events.append((method, params))
            return {}

    context = ToolContext(
        page=cast(Any, SimpleNamespace(url="https://example.com")),
        cdp_session=cast(Any, _Session()),
        element_index=element_index,
        frame_sessions={},
        active_frame_id=None,
    )

    async def fake_inject_observer(_session: Any) -> bool:
        return True

    async def fake_element_rect_info(_backend_node_id: int, _session: Any) -> dict[str, Any]:
        return {"result": {"value": {"left": 50, "top": 60, "width": 100, "height": 80}}}

    async def fake_collect_mutations_with_ids(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(semantic, "_inject_observer", fake_inject_observer)
    monkeypatch.setattr(semantic, "_element_rect_info", fake_element_rect_info)
    monkeypatch.setattr(semantic, "_collect_mutations_with_ids", fake_collect_mutations_with_ids)

    result = await semantic.click_at("el_svg", 25, 30, context)

    assert result.ok is True
    mouse_events = [params for method, params in events if method == "Input.dispatchMouseEvent"]
    assert [event["type"] for event in mouse_events] == ["mouseMoved", "mousePressed", "mouseReleased"]
    assert mouse_events[0]["x"] == 75
    assert mouse_events[0]["y"] == 90


@pytest.mark.asyncio
async def test_click_at_preserves_snapshot_viewport_coordinates_after_scroll(monkeypatch: pytest.MonkeyPatch) -> None:
    element = _el("el_svg", node_name="SVG", backend_node_id=123, role="image")
    element_index = ElementIndex(elements={"el_svg": element})
    events: list[tuple[str, dict[str, Any] | None]] = []

    class _Session:
        async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            events.append((method, params))
            return {}

    context = ToolContext(
        page=cast(Any, SimpleNamespace(url="https://example.com")),
        cdp_session=cast(Any, _Session()),
        element_index=element_index,
        frame_sessions={},
        active_frame_id=None,
    )

    async def fake_inject_observer(_session: Any) -> bool:
        return True

    async def fake_element_rect_info(_backend_node_id: int, _session: Any) -> dict[str, Any]:
        return {
            "result": {
                "value": {
                    "left": 10,
                    "top": 20,
                    "width": 100,
                    "height": 80,
                    "beforeLeft": 300,
                    "beforeTop": 400,
                    "beforeWidth": 100,
                    "beforeHeight": 80,
                }
            }
        }

    async def fake_collect_mutations_with_ids(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(semantic, "_inject_observer", fake_inject_observer)
    monkeypatch.setattr(semantic, "_element_rect_info", fake_element_rect_info)
    monkeypatch.setattr(semantic, "_collect_mutations_with_ids", fake_collect_mutations_with_ids)

    result = await semantic.click_at("el_svg", 325, 430, context)

    assert result.ok is True
    mouse_events = [params for method, params in events if method == "Input.dispatchMouseEvent"]
    assert mouse_events[0]["x"] == 35
    assert mouse_events[0]["y"] == 50


@pytest.mark.asyncio
async def test_click_at_uses_dom_events_for_clickable_svg_leaf(monkeypatch: pytest.MonkeyPatch) -> None:
    element = _el("el_svg_text", node_name="TEXT", backend_node_id=123, role="generic")
    element.handlers = {"click": "handler"}
    element_index = ElementIndex(elements={"el_svg_text": element})
    context = ToolContext(
        page=cast(Any, SimpleNamespace(url="https://example.com")),
        cdp_session=cast(Any, object()),
        element_index=element_index,
        frame_sessions={},
        active_frame_id=None,
    )

    async def fake_inject_observer(_session: Any) -> bool:
        return True

    async def fake_call_on_node(
        _backend_node_id: int,
        _session: Any,
        function_body: str,
        args: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        del args
        assert "MouseEvent('click'" in function_body
        return {"result": {"value": True}}

    async def fail_element_rect_info(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("clickable SVG leaves should not use CDP coordinate fallback")

    async def fake_collect_mutations_with_ids(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"removedElements": ["text"], "resolvedInteractive": []}

    monkeypatch.setattr(semantic, "_inject_observer", fake_inject_observer)
    monkeypatch.setattr(semantic, "_call_on_node", fake_call_on_node)
    monkeypatch.setattr(semantic, "_element_rect_info", fail_element_rect_info)
    monkeypatch.setattr(semantic, "_collect_mutations_with_ids", fake_collect_mutations_with_ids)

    result = await semantic.click_at("el_svg_text", 100, 100, context)

    assert result.ok is True
    assert "Clicked el_svg_text SVG element" in result.message


@pytest.mark.asyncio
async def test_click_element_uses_mouse_events_for_svg(monkeypatch: pytest.MonkeyPatch) -> None:
    element = _el("el_svg_text", node_name="TEXT", backend_node_id=123, role="generic")
    element_index = ElementIndex(elements={"el_svg_text": element})
    context = ToolContext(
        page=cast(Any, SimpleNamespace(url="https://example.com")),
        cdp_session=cast(Any, object()),
        element_index=element_index,
        frame_sessions={},
        active_frame_id=None,
    )

    async def fake_inject_observer(_session: Any) -> bool:
        return True

    async def fake_call_on_node(
        _backend_node_id: int,
        _session: Any,
        function_body: str,
        args: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        del args
        assert "ownerSVGElement" in function_body
        assert "MouseEvent('click'" in function_body
        return {"result": {"value": True}}

    async def fake_collect_mutations_with_ids(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"removedElements": ["text"]}

    monkeypatch.setattr(semantic, "_inject_observer", fake_inject_observer)
    monkeypatch.setattr(semantic, "_call_on_node", fake_call_on_node)
    monkeypatch.setattr(semantic, "_collect_mutations_with_ids", fake_collect_mutations_with_ids)

    result = await semantic.click_element("el_svg_text", context)

    assert result.ok is True
    assert "- element <text>" in result.message


@pytest.mark.asyncio
async def test_type_text_sets_value_when_keyboard_insert_does_not_stick(monkeypatch: pytest.MonkeyPatch) -> None:
    element = _el("el_input", node_name="INPUT", backend_node_id=321, role="textbox")
    element_index = ElementIndex(elements={"el_input": element})
    context = ToolContext(
        page=cast(Any, SimpleNamespace(url="https://example.com")),
        cdp_session=cast(Any, object()),
        element_index=element_index,
        frame_sessions={},
        active_frame_id=None,
    )
    values = iter(["", "", "Booker"])
    fallback_calls: list[str] = []

    async def fake_true(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def fake_collect_mutations_with_ids(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"attrChanges": [{"tag": "input", "attr": "value", "old": "", "new": "Booker"}]}

    async def fake_read_input_value(_backend_node_id: int, _session: Any) -> str:
        return next(values)

    async def fake_set_text_value(_backend_node_id: int, _session: Any, text: str) -> bool:
        fallback_calls.append(text)
        return True

    monkeypatch.setattr(semantic, "_inject_observer", fake_true)
    monkeypatch.setattr(semantic, "_dom_focus", fake_true)
    monkeypatch.setattr(semantic, "_insert_text", fake_true)
    monkeypatch.setattr(semantic, "_collect_mutations_with_ids", fake_collect_mutations_with_ids)
    monkeypatch.setattr(semantic, "_read_input_value", fake_read_input_value)
    monkeypatch.setattr(semantic, "_set_text_value", fake_set_text_value)

    result = await semantic.type_text("el_input", "Booker", context)

    assert result.ok is True
    assert fallback_calls == ["Booker"]
    assert "set value via form events" in result.message
    assert 'Current value: "Booker"' in result.message


@pytest.mark.asyncio
async def test_type_text_reports_live_previous_value(monkeypatch: pytest.MonkeyPatch) -> None:
    element = _el(
        "el_input",
        node_name="INPUT",
        backend_node_id=321,
        role="textbox",
    )
    element.attributes = {"value": ""}
    element_index = ElementIndex(elements={"el_input": element})
    context = ToolContext(
        page=cast(Any, SimpleNamespace(url="https://example.com")),
        cdp_session=cast(Any, object()),
        element_index=element_index,
        frame_sessions={},
        active_frame_id=None,
    )
    values = iter(["Booker", "Booker"])

    async def fake_true(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def fake_collect_mutations_with_ids(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    async def fake_read_input_value(_backend_node_id: int, _session: Any) -> str:
        return next(values)

    monkeypatch.setattr(semantic, "_inject_observer", fake_true)
    monkeypatch.setattr(semantic, "_dom_focus", fake_true)
    monkeypatch.setattr(semantic, "_insert_text", fake_true)
    monkeypatch.setattr(semantic, "_collect_mutations_with_ids", fake_collect_mutations_with_ids)
    monkeypatch.setattr(semantic, "_read_input_value", fake_read_input_value)

    result = await semantic.type_text("el_input", "Booker", context)

    assert result.ok is True
    assert 'Current value: "Booker"' in result.message
    assert 'Previous value: "Booker"' in result.message


@pytest.mark.asyncio
async def test_watch_for_text_requires_observable_change(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Session:
        async def send(self, method: str, _params: dict[str, Any] | None = None) -> dict[str, Any]:
            assert method == "Runtime.evaluate"
            return {"result": {"value": {"status": "found", "tag": "A", "text": "16"}}}

    context = ToolContext(
        page=cast(Any, SimpleNamespace(url="https://example.com")),
        cdp_session=cast(Any, _Session()),
        element_index=ElementIndex(elements={}),
        frame_sessions={},
        active_frame_id=None,
    )

    async def fake_inject_observer(_session: Any) -> bool:
        return True

    async def fake_collect_mutations_with_ids(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(semantic, "_inject_observer", fake_inject_observer)
    monkeypatch.setattr(semantic, "_collect_mutations_with_ids", fake_collect_mutations_with_ids)

    result = await semantic.watch_for_text("16", context, timeout_ms=100)

    assert result.ok is True
    assert "no observable page change" in result.message
    assert "Do not repeat this watch" in result.message


@pytest.mark.asyncio
async def test_draw_on_click_driven_svg_directs_click_at(monkeypatch: pytest.MonkeyPatch) -> None:
    element = _el("el_svg", node_name="SVG", backend_node_id=123, role="image")
    element.handlers = {"click": "graphClicked(event)"}
    element_index = ElementIndex(elements={"el_svg": element})
    context = ToolContext(
        page=cast(Any, SimpleNamespace(url="https://example.com")),
        cdp_session=cast(Any, object()),
        element_index=element_index,
        frame_sessions={},
        active_frame_id=None,
    )

    async def fake_inject_observer(_session: Any) -> bool:
        return True

    async def fake_call_on_node(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"result": {"value": True}}

    async def fake_collect_mutations_with_ids(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(semantic, "_inject_observer", fake_inject_observer)
    monkeypatch.setattr(semantic, "_call_on_node", fake_call_on_node)
    monkeypatch.setattr(semantic, "_collect_mutations_with_ids", fake_collect_mutations_with_ids)

    result = await semantic.draw("el_svg", [[10, 10], [40, 40]], context)

    assert result.ok is False
    assert "use click_at" in result.message


def test_build_change_lines_reports_form_and_visual_changes() -> None:
    lines = semantic._build_change_lines({
        "formChanges": [{"tag": "input", "key": "Date", "attr": "value", "old": "", "new": "12/16/2016"}],
        "addedElements": ["circle", "line"],
        "resolvedInteractive": [
            {
                "stable_id": "el_abc123abc123",
                "tag": "rect",
                "attrs": {"data-index": "2", "x": "50", "y": "60", "width": "20", "height": "20"},
                "bbox": {"x": 50, "y": 60, "w": 20, "h": 20},
            }
        ],
    })

    assert '  ~ input Date[value]: "" -> 12/16/2016' in lines
    assert "  + element <circle>" in lines
    assert "  + element <line>" in lines
    assert '  + interactive el_abc123abc123: rect (data-index="2" x="50" y="60" width="20" height="20") bbox=50,60,20,20' in lines


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
