from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.context.handlers import (
    _extract_handler_name,
    _infer_intents,
    format_handlers_for_llm,
    prioritize_handlers,
)


# --- _infer_intents tests ---

def test_infer_intents_submit() -> None:
    assert "submit" in _infer_intents("function handleSubmit() { form.submit(); }")


def test_infer_intents_close() -> None:
    intents = _infer_intents("function dismiss() { modal.close(); }")
    assert "close" in intents


def test_infer_intents_toggle() -> None:
    assert "toggle" in _infer_intents("toggle()")


def test_infer_intents_toggle_in_compound() -> None:
    # Word boundary requires 'toggle' as a standalone word
    assert "toggle" in _infer_intents("function toggle() { menu.classList.toggle('open'); }")


def test_infer_intents_no_match() -> None:
    assert _infer_intents("function render() { return el; }") == []


def test_infer_intents_multiple() -> None:
    intents = _infer_intents("function submitAndClose() { submit(); close(); }")
    assert "submit" in intents
    assert "close" in intents


# --- _extract_handler_name tests ---

def test_extract_handler_name_named_call() -> None:
    assert _extract_handler_name("handleClick()") == "handleClick"


def test_extract_handler_name_function_declaration() -> None:
    assert _extract_handler_name("function onSubmit(e) { ... }") == "onSubmit"


def test_extract_handler_name_anonymous() -> None:
    assert _extract_handler_name("() => { console.log('x'); }") is None


def test_extract_handler_name_short_name_excluded() -> None:
    # Names 2 chars or less don't match the {2,} quantifier
    assert _extract_handler_name("fn()") is None


def test_extract_handler_name_dollar_prefix() -> None:
    assert _extract_handler_name("$handleEvent()") == "$handleEvent"


# --- format_handlers_for_llm tests ---

def test_format_handlers_empty() -> None:
    assert format_handlers_for_llm({}) == ""


def test_format_handlers_with_intent() -> None:
    result = format_handlers_for_llm({"click": "function handleSubmit() { form.submit(); }"})
    assert "click:submit" in result


def test_format_handlers_with_name_fallback() -> None:
    result = format_handlers_for_llm({"click": "handleClick() function handleClick() { doStuff(); }"})
    assert "click:handleClick" in result


def test_format_handlers_unknown_falls_back_to_handler() -> None:
    result = format_handlers_for_llm({"click": "() => { x++; }"})
    assert "click:handler" in result


# --- prioritize_handlers tests ---

def test_prioritize_handlers_limits_count() -> None:
    handlers = {
        "click": "fn1()",
        "change": "fn2()",
        "input": "fn3()",
        "scroll": "fn4()",
        "blur": "fn5()",
    }
    result = prioritize_handlers(handlers)
    assert len(result) == 3
    # click, change, input have highest priority
    assert "click" in result
    assert "change" in result
    assert "input" in result


def test_prioritize_handlers_empty() -> None:
    assert prioritize_handlers({}) == {}
