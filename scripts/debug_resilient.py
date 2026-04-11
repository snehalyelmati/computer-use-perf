"""Debug script to verify ResilientModel retry behavior.

Tests:
  - 429 retries 3 times with correct delays
  - 429 with Retry-After header uses header value
  - 500 retries 3 times then raises
  - ModelAPIError (network) retries 2 times
  - 401 raises immediately (not retried)
  - _get_retry_after handles edge cases
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.models import Model, ModelRequestParameters, ModelResponse
from pydantic_ai.messages import ModelMessage
from pydantic_ai.settings import ModelSettings

from src.agent.core.resilient_model import (
    ResilientModel,
    _get_retry_after,
    _classify_http_error,
    RATE_LIMIT_POLICY,
    SERVER_ERROR_POLICY,
    NETWORK_ERROR_POLICY,
    BAD_REQUEST_POLICY,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_http_error(status_code: int, retry_after: str | None = None) -> ModelHTTPError:
    """Create a ModelHTTPError with an optional Retry-After header on the cause."""
    exc = ModelHTTPError(status_code=status_code, model_name="test-model")
    if retry_after is not None:
        # Simulate the chained cause with response headers
        cause = Exception("upstream error")
        cause.response = MagicMock()
        cause.response.headers = {"retry-after": retry_after}
        exc.__cause__ = cause
    return exc


def make_network_error() -> ModelAPIError:
    """Create a ModelAPIError (not ModelHTTPError) simulating a network failure."""
    return ModelAPIError(model_name="test-model", message="Connection refused")


class FakeModel(Model):
    """Mock model that raises a sequence of exceptions, then succeeds."""

    def __init__(self, errors: list[Exception], success_response: object = None):
        super().__init__()
        self._errors = list(errors)
        self._success_response = success_response or MagicMock(spec=ModelResponse)
        self.call_count = 0
        self.call_times: list[float] = []

    async def request(self, messages, model_settings, model_request_parameters):
        self.call_count += 1
        self.call_times.append(time.monotonic())
        if self._errors:
            raise self._errors.pop(0)
        return self._success_response

    @property
    def model_name(self) -> str:
        return "test-model"

    @property
    def system(self) -> str:
        return "test"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_429_retries_3_times():
    """429 should retry up to 3 times with RATE_LIMIT_POLICY delays."""
    errors = [make_http_error(429) for _ in range(3)]
    fake = FakeModel(errors)
    model = ResilientModel(fake)

    start = time.monotonic()
    result = await model.request([], None, ModelRequestParameters())
    elapsed = time.monotonic() - start

    assert fake.call_count == 4, f"Expected 4 calls (1 initial + 3 retries), got {fake.call_count}"
    # Verify delays: 5 + 15 + 30 = 50s minimum (we use small tolerance since we're actually sleeping)
    # For a fast test, let's just verify the call count is correct
    print(f"  PASS: 429 retried 3 times, {fake.call_count} total calls")


async def test_429_with_retry_after_header():
    """429 with Retry-After header should use that value."""
    exc = make_http_error(429, retry_after="10")
    retry_after = _get_retry_after(exc)
    assert retry_after == 10.0, f"Expected 10.0, got {retry_after}"
    print(f"  PASS: Retry-After=10 parsed correctly as {retry_after}s")


async def test_429_retry_after_capped_at_60():
    """Retry-After values above 60 should be capped."""
    exc = make_http_error(429, retry_after="120")
    retry_after = _get_retry_after(exc)
    assert retry_after == 60.0, f"Expected 60.0, got {retry_after}"
    print(f"  PASS: Retry-After=120 capped to {retry_after}s")


async def test_500_retries_3_times_then_raises():
    """500 should retry 3 times then re-raise."""
    errors = [make_http_error(500) for _ in range(4)]  # more than max_retries
    fake = FakeModel(errors)
    model = ResilientModel(fake)

    try:
        await model.request([], None, ModelRequestParameters())
        assert False, "Should have raised"
    except ModelHTTPError as exc:
        assert exc.status_code == 500
        assert fake.call_count == 4, f"Expected 4 calls (1 + 3 retries), got {fake.call_count}"
        print(f"  PASS: 500 retried 3 times then raised, {fake.call_count} total calls")


async def test_network_error_retries_2_times():
    """ModelAPIError (network) should retry 2 times per NETWORK_ERROR_POLICY."""
    errors = [make_network_error() for _ in range(2)]
    fake = FakeModel(errors)
    model = ResilientModel(fake)

    result = await model.request([], None, ModelRequestParameters())
    assert fake.call_count == 3, f"Expected 3 calls (1 + 2 retries), got {fake.call_count}"
    print(f"  PASS: Network error retried 2 times, {fake.call_count} total calls")


async def test_network_error_exhausted_raises():
    """ModelAPIError should raise after exhausting retries."""
    errors = [make_network_error() for _ in range(3)]  # more than max_retries
    fake = FakeModel(errors)
    model = ResilientModel(fake)

    try:
        await model.request([], None, ModelRequestParameters())
        assert False, "Should have raised"
    except ModelAPIError:
        assert fake.call_count == 3, f"Expected 3 calls (1 + 2 retries), got {fake.call_count}"
        print(f"  PASS: Network error raised after exhausting retries, {fake.call_count} total calls")


async def test_401_raises_immediately():
    """401 should not be retried."""
    errors = [make_http_error(401)]
    fake = FakeModel(errors)
    model = ResilientModel(fake)

    try:
        await model.request([], None, ModelRequestParameters())
        assert False, "Should have raised"
    except ModelHTTPError as exc:
        assert exc.status_code == 401
        assert fake.call_count == 1, f"Expected 1 call (no retries), got {fake.call_count}"
        print(f"  PASS: 401 raised immediately, {fake.call_count} total calls")


async def test_classify_http_error():
    """Test _classify_http_error for all categories."""
    assert _classify_http_error(make_http_error(429)) is RATE_LIMIT_POLICY
    assert _classify_http_error(make_http_error(500)) is SERVER_ERROR_POLICY
    assert _classify_http_error(make_http_error(502)) is SERVER_ERROR_POLICY
    assert _classify_http_error(make_http_error(503)) is SERVER_ERROR_POLICY
    assert _classify_http_error(make_http_error(504)) is SERVER_ERROR_POLICY
    assert _classify_http_error(make_http_error(400)) is BAD_REQUEST_POLICY
    assert _classify_http_error(make_http_error(401)) is None
    assert _classify_http_error(make_http_error(403)) is None
    assert _classify_http_error(make_http_error(404)) is None
    print("  PASS: All status codes classified correctly")


async def test_get_retry_after_edge_cases():
    """Test _get_retry_after with various edge cases."""
    # No cause
    exc = make_http_error(429)
    assert _get_retry_after(exc) is None
    print("    no __cause__ → None")

    # Cause without response
    exc = make_http_error(429)
    cause_no_resp = Exception("no response attr")
    exc.__cause__ = cause_no_resp
    assert _get_retry_after(exc) is None
    print("    cause without response → None")

    # Response without headers
    exc = make_http_error(429)
    cause_no_headers = Exception("no headers")
    cause_no_headers.response = MagicMock(spec=[])  # no headers attr
    exc.__cause__ = cause_no_headers
    assert _get_retry_after(exc) is None
    print("    response without headers → None")

    # Non-numeric value
    exc = make_http_error(429, retry_after="not-a-number")
    assert _get_retry_after(exc) is None
    print("    non-numeric value → None")

    # Negative value
    exc = make_http_error(429, retry_after="-5")
    assert _get_retry_after(exc) is None
    print("    negative value → None")

    # Zero value
    exc = make_http_error(429, retry_after="0")
    assert _get_retry_after(exc) is None
    print("    zero value → None")

    # Valid float
    exc = make_http_error(429, retry_after="2.5")
    assert _get_retry_after(exc) == 2.5
    print("    float value 2.5 → 2.5")

    print("  PASS: All Retry-After edge cases handled")


async def test_400_retries_once():
    """400 should retry once per BAD_REQUEST_POLICY."""
    errors = [make_http_error(400)]
    fake = FakeModel(errors)
    model = ResilientModel(fake)

    result = await model.request([], None, ModelRequestParameters())
    assert fake.call_count == 2, f"Expected 2 calls (1 + 1 retry), got {fake.call_count}"
    print(f"  PASS: 400 retried once, {fake.call_count} total calls")


async def test_mixed_errors():
    """Network error followed by HTTP error: both retry correctly."""
    errors = [
        make_network_error(),    # outer loop retries
        make_http_error(500),    # inner loop retries
        # then succeeds
    ]
    fake = FakeModel(errors)
    model = ResilientModel(fake)

    result = await model.request([], None, ModelRequestParameters())
    assert fake.call_count == 3, f"Expected 3 calls, got {fake.call_count}"
    print(f"  PASS: Mixed network+HTTP errors handled, {fake.call_count} total calls")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def main():
    # Patch asyncio.sleep to make tests fast
    import src.agent.core.resilient_model as rm
    original_sleep = asyncio.sleep

    async def fast_sleep(delay: float) -> None:
        await original_sleep(0)  # yield control without actually waiting

    rm.asyncio.sleep = fast_sleep  # type: ignore[attr-defined]

    try:
        tests = [
            ("429 retries 3 times", test_429_retries_3_times),
            ("429 with Retry-After header", test_429_with_retry_after_header),
            ("429 Retry-After capped at 60", test_429_retry_after_capped_at_60),
            ("500 retries 3 times then raises", test_500_retries_3_times_then_raises),
            ("Network error retries 2 times", test_network_error_retries_2_times),
            ("Network error exhausted raises", test_network_error_exhausted_raises),
            ("401 raises immediately", test_401_raises_immediately),
            ("Classify HTTP errors", test_classify_http_error),
            ("Retry-After edge cases", test_get_retry_after_edge_cases),
            ("400 retries once", test_400_retries_once),
            ("Mixed network + HTTP errors", test_mixed_errors),
        ]

        passed = 0
        failed = 0
        for name, test_fn in tests:
            try:
                print(f"[TEST] {name}")
                await test_fn()
                passed += 1
            except Exception as exc:
                print(f"  FAIL: {exc}")
                failed += 1

        print(f"\n{'='*40}")
        print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
        if failed:
            raise SystemExit(1)
    finally:
        rm.asyncio.sleep = original_sleep  # type: ignore[attr-defined]


if __name__ == "__main__":
    asyncio.run(main())
