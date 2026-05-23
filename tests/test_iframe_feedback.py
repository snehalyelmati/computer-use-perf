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
    assert "'value','href','src','class'" in semantic._OBSERVER_INJECT_JS


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
async def test_frame_switch_noops_report_already() -> None:
    iframe = _el(
        "el_iframe",
        node_name="IFRAME",
        frame_id="frame_1",
        frame_url="https://frame.example",
    )
    element_index = ElementIndex(elements={"el_iframe": iframe})
    context = ToolContext(
        page=cast(Any, object()),
        cdp_session=cast(Any, object()),
        element_index=element_index,
        frame_sessions={},
        active_frame_id="frame_1",
    )

    iframe_result = await semantic.switch_to_iframe("el_iframe", context)
    assert iframe_result.ok is True
    assert "Already in iframe el_iframe" in iframe_result.message

    context.active_frame_id = None
    main_result = await semantic.switch_to_main_frame(context)
    assert main_result.ok is True
    assert main_result.message == "Already in main frame"


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
async def test_focus_element_reports_focus_change(monkeypatch: pytest.MonkeyPatch) -> None:
    element = _el("el_input", node_name="INPUT", backend_node_id=123, role="textbox")
    element_index = ElementIndex(elements={"el_input": element})
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
        backend_node_id: int,
        _session: Any,
        function_body: str,
        args: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        del function_body, args
        assert backend_node_id == 123
        return {"result": {"value": {"active": True, "changed": True, "activeLabel": "INPUT#name"}}}

    async def fake_collect_mutations_with_ids(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(semantic, "_inject_observer", fake_inject_observer)
    monkeypatch.setattr(semantic, "_call_on_node", fake_call_on_node)
    monkeypatch.setattr(semantic, "_collect_mutations_with_ids", fake_collect_mutations_with_ids)

    result = await semantic.focus_element("el_input", context)

    assert result.ok is True
    assert "Focus changed: true" in result.message


@pytest.mark.asyncio
async def test_pointer_drag_dispatches_mouse_path(monkeypatch: pytest.MonkeyPatch) -> None:
    element = _el("el_slider", node_name="DIV", backend_node_id=123, role="slider")
    element_index = ElementIndex(elements={"el_slider": element})
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
        return {"result": {"value": {"left": 10, "top": 20, "width": 100, "height": 40}}}

    async def fake_collect_mutations_with_ids(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(semantic, "_inject_observer", fake_inject_observer)
    monkeypatch.setattr(semantic, "_element_rect_info", fake_element_rect_info)
    monkeypatch.setattr(semantic, "_collect_mutations_with_ids", fake_collect_mutations_with_ids)

    result = await semantic.pointer_drag("el_slider", 0, 20, 100, 20, context, steps=2)

    assert result.ok is True
    mouse_events = [params for method, params in events if method == "Input.dispatchMouseEvent"]
    assert [event["type"] for event in mouse_events] == [
        "mouseMoved",
        "mousePressed",
        "mouseMoved",
        "mouseMoved",
        "mouseReleased",
    ]
    assert mouse_events[0]["x"] == 10
    assert mouse_events[-1]["x"] == 110


@pytest.mark.asyncio
async def test_pointer_drag_allows_endpoint_outside_source(monkeypatch: pytest.MonkeyPatch) -> None:
    element = _el("el_drag", node_name="DIV", backend_node_id=123)
    element_index = ElementIndex(elements={"el_drag": element})
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
        return {"result": {"value": {"left": 10, "top": 20, "width": 100, "height": 40}}}

    async def fake_collect_mutations_with_ids(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(semantic, "_inject_observer", fake_inject_observer)
    monkeypatch.setattr(semantic, "_element_rect_info", fake_element_rect_info)
    monkeypatch.setattr(semantic, "_collect_mutations_with_ids", fake_collect_mutations_with_ids)

    result = await semantic.pointer_drag("el_drag", 50, 20, 180, 60, context, steps=1)

    assert result.ok is True
    mouse_events = [params for method, params in events if method == "Input.dispatchMouseEvent"]
    assert mouse_events[-1]["x"] == 190
    assert mouse_events[-1]["y"] == 80
    assert "to (180.0, 60.0)" in result.message


@pytest.mark.asyncio
async def test_drag_and_drop_uses_pointer_fallback_when_dom_drag_noops(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _el("el_source", node_name="DIV", backend_node_id=123)
    target = _el("el_target", node_name="DIV", backend_node_id=456)
    element_index = ElementIndex(elements={"el_source": source, "el_target": target})
    context = ToolContext(
        page=cast(Any, SimpleNamespace(url="https://example.com")),
        cdp_session=cast(Any, object()),
        element_index=element_index,
        frame_sessions={},
        active_frame_id=None,
    )
    fallback_calls: list[tuple[int, int]] = []
    observer_injections = 0
    mutation_calls = 0

    async def fake_inject_observer(_session: Any) -> bool:
        nonlocal observer_injections
        observer_injections += 1
        return True

    async def fake_resolve_object_id(_backend_node_id: int, _session: Any) -> str:
        return "object-id"

    async def fake_call_on_node(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"result": {"value": True}}

    async def fake_collect_mutations_with_ids(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal mutation_calls
        mutation_calls += 1
        if mutation_calls == 1:
            return {}
        return {"attrChanges": [{"tag": "div", "attr": "style", "old": "left:0", "new": "left:10px"}]}

    async def fake_pointer_between(
        source_backend_node_id: int,
        target_backend_node_id: int,
        _session: Any,
        *,
        steps: int,
    ) -> tuple[bool, dict[str, float], None]:
        assert steps == 18
        fallback_calls.append((source_backend_node_id, target_backend_node_id))
        return True, {
            "start_local_x": 10.0,
            "start_local_y": 10.0,
            "end_local_x": 80.0,
            "end_local_y": 30.0,
            "start_viewport_x": 20.0,
            "start_viewport_y": 20.0,
            "end_viewport_x": 90.0,
            "end_viewport_y": 40.0,
            "width": 20.0,
            "height": 20.0,
        }, None

    monkeypatch.setattr(semantic, "_inject_observer", fake_inject_observer)
    monkeypatch.setattr(semantic, "_resolve_object_id", fake_resolve_object_id)
    monkeypatch.setattr(semantic, "_call_on_node", fake_call_on_node)
    monkeypatch.setattr(semantic, "_collect_mutations_with_ids", fake_collect_mutations_with_ids)
    monkeypatch.setattr(semantic, "_dispatch_pointer_drag_between_nodes", fake_pointer_between)

    result = await semantic.drag_and_drop("el_source", "el_target", context)

    assert result.ok is True
    assert observer_injections == 2
    assert fallback_calls == [(123, 456)]
    assert "pointer fallback" in result.message


@pytest.mark.asyncio
async def test_pointer_drag_between_nodes_uses_single_scroll_state(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatch_args: dict[str, Any] = {}

    async def fake_resolve_object_id(backend_node_id: int, _session: Any) -> str:
        assert backend_node_id == 456
        return "target-object"

    async def fake_call_on_node(
        backend_node_id: int,
        _session: Any,
        function_body: str,
        args: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        assert backend_node_id == 123
        assert args == [{"objectId": "target-object"}]
        assert "targetEl.getBoundingClientRect()" in function_body
        return {
            "result": {
                "value": {
                    "source": {"left": 10, "top": 20, "width": 40, "height": 20},
                    "target": {"left": 210, "top": 80, "width": 30, "height": 20},
                }
            }
        }

    async def fail_element_rect_info(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("pair rect helper should not scroll source and target separately")

    async def fake_dispatch_pointer_drag_local(*_args: Any, **kwargs: Any) -> tuple[bool, dict[str, float], None]:
        dispatch_args.update(kwargs)
        return True, {
            "start_local_x": kwargs["start_x"],
            "start_local_y": kwargs["start_y"],
            "end_local_x": kwargs["end_x"],
            "end_local_y": kwargs["end_y"],
            "start_viewport_x": 30.0,
            "start_viewport_y": 30.0,
            "end_viewport_x": 235.0,
            "end_viewport_y": 90.0,
            "width": 40.0,
            "height": 20.0,
        }, None

    monkeypatch.setattr(semantic, "_resolve_object_id", fake_resolve_object_id)
    monkeypatch.setattr(semantic, "_call_on_node", fake_call_on_node)
    monkeypatch.setattr(semantic, "_element_rect_info", fail_element_rect_info)
    monkeypatch.setattr(semantic, "_dispatch_pointer_drag_local", fake_dispatch_pointer_drag_local)

    ok, _coords, error = await semantic._dispatch_pointer_drag_between_nodes(123, 456, cast(Any, object()), steps=7)

    assert ok is True
    assert error is None
    assert dispatch_args["start_x"] == 20
    assert dispatch_args["start_y"] == 10
    assert dispatch_args["end_x"] == 215
    assert dispatch_args["end_y"] == 70
    assert dispatch_args["allow_outside"] is True
    assert dispatch_args["steps"] == 7


@pytest.mark.asyncio
async def test_set_slider_value_reports_refreshed_pointer_value(monkeypatch: pytest.MonkeyPatch) -> None:
    element = _el("el_slider", node_name="DIV", backend_node_id=123, role="slider")
    element_index = ElementIndex(elements={"el_slider": element})
    context = ToolContext(
        page=cast(Any, SimpleNamespace(url="https://example.com")),
        cdp_session=cast(Any, object()),
        element_index=element_index,
        frame_sessions={},
        active_frame_id=None,
    )
    call_bodies: list[str] = []

    async def fake_inject_observer(_session: Any) -> bool:
        return True

    async def fake_call_on_node(
        backend_node_id: int,
        _session: Any,
        function_body: str,
        args: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        del args
        assert backend_node_id == 123
        call_bodies.append(function_body)
        if "function (desired)" in function_body:
            return {"result": {"value": {"ok": False, "before": "0", "after": "0"}}}
        return {"result": {"value": "75"}}

    async def fake_element_rect_info(_backend_node_id: int, _session: Any) -> dict[str, Any]:
        return {"result": {"value": {"left": 10, "top": 20, "width": 100, "height": 40}}}

    async def fake_dispatch_pointer_drag_local(*_args: Any, **_kwargs: Any) -> tuple[bool, dict[str, float], None]:
        return True, {
            "start_local_x": 50.0,
            "start_local_y": 20.0,
            "end_local_x": 75.0,
            "end_local_y": 20.0,
            "start_viewport_x": 60.0,
            "start_viewport_y": 40.0,
            "end_viewport_x": 85.0,
            "end_viewport_y": 40.0,
            "width": 100.0,
            "height": 40.0,
        }, None

    async def fake_collect_mutations_with_ids(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(semantic, "_inject_observer", fake_inject_observer)
    monkeypatch.setattr(semantic, "_call_on_node", fake_call_on_node)
    monkeypatch.setattr(semantic, "_element_rect_info", fake_element_rect_info)
    monkeypatch.setattr(semantic, "_dispatch_pointer_drag_local", fake_dispatch_pointer_drag_local)
    monkeypatch.setattr(semantic, "_collect_mutations_with_ids", fake_collect_mutations_with_ids)

    result = await semantic.set_slider_value("el_slider", 75, context)

    assert result.ok is True
    assert 'Previous value: "0"' in result.message
    assert 'Current value: "75"' in result.message
    assert len(call_bodies) == 2


@pytest.mark.asyncio
async def test_set_slider_value_uses_jquery_ui_slider_root(monkeypatch: pytest.MonkeyPatch) -> None:
    element = _el("el_handle", node_name="SPAN", backend_node_id=123, role=None)
    element_index = ElementIndex(elements={"el_handle": element})
    context = ToolContext(
        page=cast(Any, SimpleNamespace(url="https://example.com")),
        cdp_session=cast(Any, object()),
        element_index=element_index,
        frame_sessions={},
        active_frame_id=None,
    )
    bodies: list[str] = []

    async def fake_inject_observer(_session: Any) -> bool:
        return True

    async def fake_call_on_node(
        backend_node_id: int,
        _session: Any,
        function_body: str,
        args: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        del args
        assert backend_node_id == 123
        bodies.append(function_body)
        assert "closest('.ui-slider')" in function_body
        assert "slider('value', desiredNumber)" in function_body
        return {
            "result": {
                "value": {
                    "ok": True,
                    "before": "8",
                    "after": "4",
                    "min": 0,
                    "max": 10,
                    "method": "jquery-ui",
                    "root": "ancestor",
                }
            }
        }

    async def fail_dispatch(*_args: Any, **_kwargs: Any) -> tuple[bool, None, str]:
        raise AssertionError("pointer fallback should not run after verified jQuery UI setting")

    async def fake_collect_mutations_with_ids(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"attrChanges": [{"tag": "div", "attr": "value", "old": "8", "new": "4"}]}

    monkeypatch.setattr(semantic, "_inject_observer", fake_inject_observer)
    monkeypatch.setattr(semantic, "_call_on_node", fake_call_on_node)
    monkeypatch.setattr(semantic, "_dispatch_pointer_drag_local", fail_dispatch)
    monkeypatch.setattr(semantic, "_collect_mutations_with_ids", fake_collect_mutations_with_ids)

    result = await semantic.set_slider_value("el_handle", 4, context)

    assert result.ok is True
    assert 'Previous value: "8"' in result.message
    assert 'Current value: "4"' in result.message
    assert len(bodies) == 1


@pytest.mark.asyncio
async def test_set_slider_value_fails_when_pointer_does_not_change_value(monkeypatch: pytest.MonkeyPatch) -> None:
    element = _el("el_slider", node_name="DIV", backend_node_id=123, role="slider")
    element_index = ElementIndex(elements={"el_slider": element})
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
        backend_node_id: int,
        _session: Any,
        function_body: str,
        args: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        del args
        assert backend_node_id == 123
        if "function (desired)" in function_body:
            return {"result": {"value": {"ok": False, "before": "8", "after": "8", "min": 0, "max": 10}}}
        return {"result": {"value": "8"}}

    async def fake_element_rect_info(_backend_node_id: int, _session: Any) -> dict[str, Any]:
        return {"result": {"value": {"left": 10, "top": 20, "width": 100, "height": 40}}}

    async def fake_dispatch_pointer_drag_local(*_args: Any, **_kwargs: Any) -> tuple[bool, dict[str, float], None]:
        return True, {
            "start_local_x": 50.0,
            "start_local_y": 20.0,
            "end_local_x": 40.0,
            "end_local_y": 20.0,
            "start_viewport_x": 60.0,
            "start_viewport_y": 40.0,
            "end_viewport_x": 50.0,
            "end_viewport_y": 40.0,
            "width": 100.0,
            "height": 40.0,
        }, None

    async def fake_collect_mutations_with_ids(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(semantic, "_inject_observer", fake_inject_observer)
    monkeypatch.setattr(semantic, "_call_on_node", fake_call_on_node)
    monkeypatch.setattr(semantic, "_element_rect_info", fake_element_rect_info)
    monkeypatch.setattr(semantic, "_dispatch_pointer_drag_local", fake_dispatch_pointer_drag_local)
    monkeypatch.setattr(semantic, "_collect_mutations_with_ids", fake_collect_mutations_with_ids)

    result = await semantic.set_slider_value("el_slider", 4, context)

    assert result.ok is False
    assert "Set slider failed verification" in result.message
    assert 'Current value: "8"' in result.message


@pytest.mark.asyncio
async def test_select_text_without_target_selects_full_contents(monkeypatch: pytest.MonkeyPatch) -> None:
    element = _el("el_editor", node_name="DIV", backend_node_id=123, role="textbox")
    element_index = ElementIndex(elements={"el_editor": element})
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
        backend_node_id: int,
        _session: Any,
        function_body: str,
        args: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        del args
        assert backend_node_id == 123
        assert function_body.index("if (!wanted)") < function_body.index("const walker")
        return {"result": {"value": {"ok": True, "selected": "Hello world"}}}

    async def fake_collect_mutations_with_ids(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(semantic, "_inject_observer", fake_inject_observer)
    monkeypatch.setattr(semantic, "_call_on_node", fake_call_on_node)
    monkeypatch.setattr(semantic, "_collect_mutations_with_ids", fake_collect_mutations_with_ids)

    result = await semantic.select_text("el_editor", context)

    assert result.ok is True
    assert 'Selected text: "Hello world"' in result.message


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
async def test_type_text_uses_keyboard_events_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    element = _el("el_input", node_name="INPUT", backend_node_id=321, role="textbox")
    element_index = ElementIndex(elements={"el_input": element})
    typed: list[str] = []
    insert_calls: list[str] = []

    class _Keyboard:
        async def type(self, text: str) -> None:
            typed.append(text)

    context = ToolContext(
        page=cast(Any, SimpleNamespace(url="https://example.com", keyboard=_Keyboard())),
        cdp_session=cast(Any, object()),
        element_index=element_index,
        frame_sessions={},
        active_frame_id=None,
    )
    values = iter(["", "hello"])

    async def fake_true(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def fake_insert_text(_session: Any, text: str) -> bool:
        insert_calls.append(text)
        return True

    async def fake_collect_mutations_with_ids(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"attrChanges": [{"tag": "input", "attr": "value", "old": "", "new": "hello"}]}

    async def fake_read_input_value(_backend_node_id: int, _session: Any) -> str:
        return next(values)

    monkeypatch.setattr(semantic, "_inject_observer", fake_true)
    monkeypatch.setattr(semantic, "_dom_focus", fake_true)
    monkeypatch.setattr(semantic, "_insert_text", fake_insert_text)
    monkeypatch.setattr(semantic, "_collect_mutations_with_ids", fake_collect_mutations_with_ids)
    monkeypatch.setattr(semantic, "_read_input_value", fake_read_input_value)

    result = await semantic.type_text("el_input", "hello", context)

    assert result.ok is True
    assert typed == ["hello"]
    assert insert_calls == []
    assert 'Current value: "hello"' in result.message


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


@pytest.mark.asyncio
async def test_draw_falls_back_to_pointer_events_when_synthetic_noops(monkeypatch: pytest.MonkeyPatch) -> None:
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

    async def fake_call_on_node(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"result": {"value": True}}

    async def fake_collect_mutations_with_ids(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    async def fake_viewport_info(_backend_node_id: int, _session: Any) -> dict[str, Any]:
        return {"result": {"value": {"x": 60, "y": 70, "width": 100, "height": 80}}}

    monkeypatch.setattr(semantic, "_inject_observer", fake_inject_observer)
    monkeypatch.setattr(semantic, "_call_on_node", fake_call_on_node)
    monkeypatch.setattr(semantic, "_collect_mutations_with_ids", fake_collect_mutations_with_ids)
    monkeypatch.setattr(semantic, "_viewport_info", fake_viewport_info)

    result = await semantic.draw("el_svg", [[10, 10], [40, 40], [70, 20]], context)

    assert result.ok is True
    assert "pointer fallback" in result.message
    mouse_events = [params for method, params in events if method == "Input.dispatchMouseEvent"]
    assert [event["type"] for event in mouse_events] == [
        "mouseMoved",
        "mousePressed",
        "mouseMoved",
        "mouseMoved",
        "mouseReleased",
    ]
    assert mouse_events[0]["x"] == 20
    assert mouse_events[0]["y"] == 40
    assert mouse_events[-1]["x"] == 80
    assert mouse_events[-1]["y"] == 50


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
