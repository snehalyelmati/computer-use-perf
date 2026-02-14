"""Tests for schemas, complete() wrapper, and LLM agent functions."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pydantic import ValidationError

from src.agent.schemas import (
    OracleResponse,
    OverviewResponse,
    ActionItem,
    ActionResponse,
    LearningResponse,
)
from src.agent.llm_client import complete, _strip_think_tags


# --- Schema validation tests -------------------------------------------------


class TestOracleResponse:
    def test_defaults(self):
        r = OracleResponse()
        assert r.status == "OK"
        assert r.reason is None
        assert r.next_directive is None
        assert r.avoid is None

    def test_valid_full(self):
        r = OracleResponse.model_validate_json(
            json.dumps(
                {
                    "status": "OVERRIDE",
                    "reason": "Agent stuck clicking same button",
                    "next_directive": "Click the submit button.",
                    "avoid": "btn Submit",
                }
            )
        )
        assert r.status == "OVERRIDE"
        assert r.next_directive == "Click the submit button."

    def test_unknown_status_normalizes_to_warn(self):
        r = OracleResponse.model_validate_json('{"status": "INVALID"}')
        assert r.status == "WARN"

    def test_status_aliases(self):
        assert OracleResponse.model_validate_json('{"status": "WAIT"}').status == "OK"
        assert (
            OracleResponse.model_validate_json('{"status": "STUCK"}').status
            == "OVERRIDE"
        )

    def test_avoid_coerces_list(self):
        r = OracleResponse.model_validate_json(
            '{"status": "WARN", "avoid": ["a", "b"]}'
        )
        assert r.avoid == "a, b"


class TestOverviewResponse:
    def test_requires_objective_next_and_task(self):
        with pytest.raises(ValidationError):
            OverviewResponse.model_validate_json(
                '{"objective": "Do something", "next": "Click"}'
            )
        with pytest.raises(ValidationError):
            OverviewResponse.model_validate_json(
                '{"next": "Click button", "task": "click 0"}'
            )
        with pytest.raises(ValidationError):
            OverviewResponse.model_validate_json(
                '{"objective": "Do something", "task": "click 0"}'
            )

    def test_backwards_compat_goal_maps_to_objective(self):
        r = OverviewResponse.model_validate_json(
            '{"goal": "Enter the code", "next": "Click", "task": "click 0"}'
        )
        assert r.objective == "Enter the code"

    def test_valid(self):
        r = OverviewResponse.model_validate_json(
            json.dumps(
                {
                    "objective": "Enter the code",
                    "data": "code=ABC123",
                    "progress": "Step 1/3",
                    "next": "Type ABC123 into element [1], then click Submit [2]",
                    "task": "type 1 code\nclick 2",
                }
            )
        )
        assert r.objective == "Enter the code"
        assert r.next == "Type ABC123 into element [1], then click Submit [2]"
        assert r.data == "code=ABC123"
        assert "type" in r.task


class TestActionResponse:
    def test_valid(self):
        r = ActionResponse.model_validate_json('{"actions": [{"a": "click", "n": 0}]}')
        assert len(r.actions) == 1
        assert r.actions[0].a == "click"
        assert r.actions[0].n == 0

    def test_empty_actions_allowed(self):
        r = ActionResponse.model_validate_json('{"actions": []}')
        assert r.actions == []

    def test_excludes_none(self):
        item = ActionItem(a="click", n=0)
        d = item.model_dump(exclude_none=True)
        assert "v" not in d
        assert "t" not in d


class TestLearningResponse:
    def test_valid(self):
        r = LearningResponse.model_validate_json(
            '{"learning": "Always check hidden content for codes."}'
        )
        assert r.learning == "Always check hidden content for codes."

    def test_missing_learning(self):
        with pytest.raises(ValidationError):
            LearningResponse.model_validate_json("{}")


# --- _strip_think_tags tests -------------------------------------------------


class TestStripThinkTags:
    def test_removes_closed_tags(self):
        assert _strip_think_tags("<think>reasoning</think>answer") == "answer"

    def test_removes_unclosed_tags(self):
        assert _strip_think_tags("<think>reasoning truncated") == ""

    def test_preserves_normal_text(self):
        assert _strip_think_tags("just text") == "just text"

    def test_empty_input(self):
        assert _strip_think_tags("") == ""

    def test_none_input(self):
        assert _strip_think_tags(None) is None


# --- complete() tests --------------------------------------------------------


def _mock_client(responses: list[str | Exception]):
    """Create a mock LLM client that returns given responses in sequence."""
    client = MagicMock()
    call_idx = [0]

    async def _create(**kwargs):
        idx = call_idx[0]
        call_idx[0] += 1
        val = responses[idx]
        if isinstance(val, Exception):
            raise val
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = val
        mock_resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        return mock_resp

    client.chat.completions.create = AsyncMock(side_effect=_create)
    return client


@pytest.mark.asyncio
async def test_complete_returns_content_no_model():
    client = _mock_client(["hello world"])
    content, usage = await complete(
        client,
        model="test",
        messages=[{"role": "user", "content": "hi"}],
        max_completion_tokens=100,
    )
    assert content == "hello world"
    assert usage.prompt_tokens == 10


@pytest.mark.asyncio
async def test_complete_strips_think_tags():
    client = _mock_client(["<think>internal reasoning</think>the answer"])
    content, usage = await complete(
        client,
        model="test",
        messages=[{"role": "user", "content": "q"}],
        max_completion_tokens=100,
    )
    assert content == "the answer"


@pytest.mark.asyncio
async def test_complete_returns_model_instance():
    client = _mock_client(['{"status": "WARN", "reason": "stuck"}'])
    result, usage = await complete(
        client,
        model="test",
        messages=[{"role": "user", "content": "q"}],
        max_completion_tokens=100,
        response_model=OracleResponse,
    )
    assert isinstance(result, OracleResponse)
    assert result.status == "WARN"
    assert result.reason == "stuck"


@pytest.mark.asyncio
async def test_complete_cerebras_disable_reasoning_and_reasoning_fallback():
    """Cerebras can return content=None with JSON in reasoning; parse must still work."""

    from src.agent.schemas import OracleResponse

    client = MagicMock()

    async def _create(**kwargs):
        # For qwen-3-32b on Cerebras, we disable reasoning by default.
        assert kwargs.get("disable_reasoning") is True
        assert "response_format" not in kwargs  # qwen-3-32b doesn't support json_object

        mock_resp = MagicMock()
        mock_msg = MagicMock(content=None, reasoning='{"status": "OK"}')
        mock_resp.choices = [MagicMock(message=mock_msg)]
        mock_resp.usage = MagicMock(
            prompt_tokens=10,
            completion_tokens=5,
            prompt_tokens_details=MagicMock(cached_tokens=2),
        )
        return mock_resp

    client.chat.completions.create = AsyncMock(side_effect=_create)

    with patch("src.agent.llm_client._detect_provider", return_value="cerebras"):
        result, usage = await complete(
            client,
            model="qwen-3-32b",
            messages=[{"role": "user", "content": "q"}],
            max_completion_tokens=100,
            response_model=OracleResponse,
        )
    assert isinstance(result, OracleResponse)
    assert result.status == "OK"
    assert usage.prompt_tokens == 10


@pytest.mark.asyncio
async def test_complete_cerebras_does_not_send_disable_reasoning_when_unsupported():
    """Some Cerebras models reject disable_reasoning - never send it for those."""

    from src.agent.schemas import ActionResponse

    client = MagicMock()

    async def _create(**kwargs):
        assert "disable_reasoning" not in kwargs
        assert kwargs.get("response_format") == {"type": "json_object"}

        mock_resp = MagicMock()
        mock_msg = MagicMock(content='{"actions": []}', reasoning=None)
        mock_resp.choices = [MagicMock(message=mock_msg)]
        mock_resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        return mock_resp

    client.chat.completions.create = AsyncMock(side_effect=_create)

    with patch("src.agent.llm_client._detect_provider", return_value="cerebras"):
        result, _usage = await complete(
            client,
            model="llama3.1-8b",
            messages=[{"role": "user", "content": "q"}],
            max_completion_tokens=100,
            response_model=ActionResponse,
        )
    assert isinstance(result, ActionResponse)


@pytest.mark.asyncio
async def test_complete_retries_on_validation_error():
    """First call returns invalid JSON, second returns valid."""
    client = _mock_client(
        [
            '{"status": 123}',  # invalid type -> ValidationError
            '{"status": "OK"}',  # valid
        ]
    )
    result, usage = await complete(
        client,
        model="test",
        messages=[{"role": "user", "content": "q"}],
        max_completion_tokens=100,
        response_model=OracleResponse,
    )
    assert result.status == "OK"
    assert client.chat.completions.create.call_count == 2


@pytest.mark.asyncio
async def test_complete_retries_on_transient_error():
    import groq

    client = MagicMock()
    call_count = [0]

    async def _create(**kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise groq.RateLimitError.__new__(groq.RateLimitError)
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = '{"learning": "check hidden content"}'
        mock_resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        return mock_resp

    client.chat.completions.create = AsyncMock(side_effect=_create)
    result, usage = await complete(
        client,
        model="test",
        messages=[{"role": "user", "content": "q"}],
        max_completion_tokens=100,
        response_model=LearningResponse,
    )
    assert result.learning == "check hidden content"


@pytest.mark.asyncio
async def test_complete_raises_on_permanent_error():
    import groq

    client = MagicMock()

    async def _create(**kwargs):
        raise groq.AuthenticationError.__new__(groq.AuthenticationError)

    client.chat.completions.create = AsyncMock(side_effect=_create)
    with pytest.raises(RuntimeError, match="Model config error"):
        await complete(
            client,
            model="test",
            messages=[{"role": "user", "content": "q"}],
            max_completion_tokens=100,
        )


@pytest.mark.asyncio
async def test_complete_exhausts_retries():
    """3 bad JSON responses -> raises last error."""
    client = _mock_client(
        [
            "not json at all",
            "still not json",
            "nope",
        ]
    )
    with pytest.raises(Exception):
        await complete(
            client,
            model="test",
            messages=[{"role": "user", "content": "q"}],
            max_completion_tokens=100,
            response_model=OracleResponse,
        )


@pytest.mark.asyncio
async def test_complete_auto_sets_json_format_for_supported_model():
    """response_format=json_object auto-set for models in JSON_MODE_MODELS."""
    client = _mock_client(['{"status": "OK"}'])
    await complete(
        client,
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        messages=[{"role": "user", "content": "q"}],
        max_completion_tokens=100,
        response_model=OracleResponse,
    )
    call_kwargs = client.chat.completions.create.call_args[1]
    assert call_kwargs["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_complete_skips_json_format_for_reasoning_model():
    """response_format NOT set for models not in JSON_MODE_MODELS (e.g. qwen3)."""
    client = _mock_client(['{"status": "OK"}'])
    await complete(
        client,
        model="qwen/qwen3-32b",
        messages=[{"role": "user", "content": "q"}],
        max_completion_tokens=100,
        response_model=OracleResponse,
    )
    call_kwargs = client.chat.completions.create.call_args[1]
    assert "response_format" not in call_kwargs


@pytest.mark.asyncio
async def test_complete_uses_default_reasoning_for_supported_model():
    """Model in REASONING_MODELS with reasoning_effort=None uses model default."""
    # qwen3-32b has default "none" - still sent to suppress thinking
    client = _mock_client(['{"status": "OK"}'])
    await complete(
        client,
        model="qwen/qwen3-32b",
        messages=[{"role": "user", "content": "q"}],
        max_completion_tokens=100,
        response_model=OracleResponse,
    )
    call_kwargs = client.chat.completions.create.call_args[1]
    assert call_kwargs["reasoning_effort"] == "none"


@pytest.mark.asyncio
async def test_complete_overrides_reasoning_when_explicit():
    """Explicit reasoning_effort overrides model default."""
    client = _mock_client(['{"status": "OK"}'])
    await complete(
        client,
        model="qwen/qwen3-32b",
        messages=[{"role": "user", "content": "q"}],
        max_completion_tokens=100,
        response_model=OracleResponse,
        reasoning_effort="high",
    )
    call_kwargs = client.chat.completions.create.call_args[1]
    assert call_kwargs["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_complete_skips_reasoning_for_unsupported_model():
    """Model not in REASONING_MODELS never gets reasoning_effort kwarg."""
    client = _mock_client(['{"actions": [{"a": "click", "n": 0}]}'])
    await complete(
        client,
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        messages=[{"role": "user", "content": "q"}],
        max_completion_tokens=100,
        response_model=ActionResponse,
        reasoning_effort="high",  # should be ignored
    )
    call_kwargs = client.chat.completions.create.call_args[1]
    assert "reasoning_effort" not in call_kwargs


# --- Integration-style tests (mock LLM, test full function flow) -------------


@pytest.mark.asyncio
async def test_evaluate_step_returns_oracle_response():
    from src.agent.llm_agents import evaluate_step

    overview = OverviewResponse(
        objective="Click the button",
        next="Click [0]",
        task="click 0",
        data="code=123",
        progress="Step 1/3",
    )
    with patch(
        "src.agent.llm_agents.complete", new_callable=AsyncMock
    ) as mock_complete:
        mock_complete.return_value = (
            OracleResponse(status="OK"),
            MagicMock(prompt_tokens=100, completion_tokens=20),
        )
        result = await evaluate_step(
            client=MagicMock(),
            goal="Solve the current page task",
            overview=overview,
            actions=[{"a": "click", "n": 0}],
            results=[({"a": "click", "n": 0}, "OK clicked")],
            content={
                "url": "http://test",
                "title": "Test",
                "all_text": [],
                "hidden_content": [],
                "data_attrs": [],
            },
            elements=[],
            element_diff="",
            text_diff="",
        )
    assert isinstance(result, OracleResponse)
    assert result.status == "OK"


@pytest.mark.asyncio
async def test_analyze_overview_returns_overview_response():
    from src.agent.llm_agents import analyze_overview

    overview_resp = OverviewResponse(
        objective="Enter the code",
        next="Type into [1]",
        task="type 1 code",
        data="code=XYZ",
        progress="Starting",
    )
    memory = [{"role": "system", "content": "test"}]

    with (
        patch("src.agent.llm_agents.complete", new_callable=AsyncMock) as mock_complete,
        patch(
            "src.agent.llm_agents.filter_page_content", new_callable=AsyncMock
        ) as mock_filter,
    ):
        mock_complete.return_value = (
            overview_resp,
            MagicMock(prompt_tokens=200, completion_tokens=50),
        )
        mock_filter.return_value = ["some text"]
        result, summary, filtered = await analyze_overview(
            client=MagicMock(),
            content={
                "url": "http://test",
                "title": "Test",
                "all_text": ["some text"],
                "hidden_content": [],
                "data_attrs": [],
            },
            elements=[],
            memory=memory,
            goal="Solve the current page task",
        )
    assert isinstance(result, OverviewResponse)
    assert result.objective == "Enter the code"
    assert "OBJECTIVE: Enter the code" in summary
    assert filtered == ["some text"]


@pytest.mark.asyncio
async def test_llm_decide_returns_action_dicts():
    from src.agent.llm_agents import llm_decide

    action_resp = ActionResponse(
        actions=[
            ActionItem(a="click", n=0),
            ActionItem(a="type", n=1, v="hello"),
        ]
    )
    messages = [{"role": "system", "content": "test"}]

    with patch(
        "src.agent.llm_agents.complete", new_callable=AsyncMock
    ) as mock_complete:
        mock_complete.return_value = (
            action_resp,
            MagicMock(prompt_tokens=100, completion_tokens=30),
        )
        result = await llm_decide(
            client=MagicMock(),
            messages=messages,
            context="GOAL: Click\nNEXT: Click [0]",
        )
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["a"] == "click"
    assert result[0]["n"] == 0
    assert result[1]["a"] == "type"
    assert result[1]["v"] == "hello"
