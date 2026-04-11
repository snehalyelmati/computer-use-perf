"""Retry wrapper for pydantic-ai models.

Two layers of retry:
- Inner: catches ModelHTTPError, retries per status-code category (429/5xx/400)
- Outer: catches ModelAPIError (network/timeout), retries with its own policy
Network errors reset the HTTP retry counter (nested design).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.models import ModelRequestParameters, ModelResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.messages import ModelMessage
from pydantic_ai.settings import ModelSettings

logger = logging.getLogger("agent")

# ---------------------------------------------------------------------------
# Retry policies
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int
    delays: tuple[float, ...]  # explicit delay per attempt

RATE_LIMIT_POLICY = RetryPolicy(max_retries=3, delays=(5.0, 15.0, 30.0))
SERVER_ERROR_POLICY = RetryPolicy(max_retries=3, delays=(2.0, 4.0, 8.0))
NETWORK_ERROR_POLICY = RetryPolicy(max_retries=2, delays=(2.0, 4.0))
BAD_REQUEST_POLICY = RetryPolicy(max_retries=1, delays=(1.0,))

_MAX_RETRY_AFTER = 60.0  # cap Retry-After at 60s


def _get_retry_after(exc: ModelHTTPError) -> float | None:
    """Extract Retry-After header from the chained cause, if present.

    PydanticAI chains the original API error via ``raise ... from e``.
    The cause (e.g. openai.APIStatusError) may carry response headers.
    We use getattr defensively so this degrades gracefully if the chain changes.
    """
    cause = getattr(exc, "__cause__", None)
    if cause is None:
        return None
    response = getattr(cause, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (ValueError, TypeError):
        return None
    if value <= 0:
        return None
    return min(value, _MAX_RETRY_AFTER)


def _classify_http_error(exc: ModelHTTPError) -> RetryPolicy | None:
    """Map an HTTP status code to the appropriate retry policy, or None if not retryable."""
    code = exc.status_code
    if code == 429:
        return RATE_LIMIT_POLICY
    if code in (500, 502, 503, 504):
        return SERVER_ERROR_POLICY
    if code == 400:
        return BAD_REQUEST_POLICY
    return None  # not retryable (e.g. 401, 403)


# ---------------------------------------------------------------------------
# ResilientModel
# ---------------------------------------------------------------------------

@dataclass(init=False)
class ResilientModel(WrapperModel):
    """Wraps a Model with nested retry logic for HTTP and network errors."""

    total_retry_wait_seconds: float

    def __init__(self, wrapped: object) -> None:
        super().__init__(wrapped)  # type: ignore[arg-type]
        self.total_retry_wait_seconds = 0.0

    # -- inner layer: HTTP status retries --

    async def _request_with_http_retries(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        """Attempt a request, retrying on retryable HTTP status codes."""
        # Track per-category attempt counts.
        category_attempts: dict[int, int] = {}  # status_code -> attempts so far

        while True:
            try:
                return await self.wrapped.request(
                    messages, model_settings, model_request_parameters
                )
            except ModelHTTPError as exc:
                policy = _classify_http_error(exc)
                if policy is None:
                    raise  # not retryable

                code = exc.status_code
                attempts = category_attempts.get(code, 0)
                if attempts >= policy.max_retries:
                    raise  # exhausted retries for this category

                category_attempts[code] = attempts + 1

                # Determine delay: prefer Retry-After for 429, else use policy schedule
                retry_after = _get_retry_after(exc) if code == 429 else None
                delay = retry_after if retry_after is not None else (
                    policy.delays[attempts] if attempts < len(policy.delays) else policy.delays[-1]
                )

                logger.warning(
                    "LLM HTTP %s, retry %s/%s in %.1fs%s: %s",
                    code,
                    attempts + 1,
                    policy.max_retries,
                    delay,
                    f" (Retry-After: {retry_after}s)" if retry_after is not None else "",
                    exc,
                )
                self.total_retry_wait_seconds += delay
                await asyncio.sleep(delay)

    # -- outer layer: network/timeout retries --

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        policy = NETWORK_ERROR_POLICY
        for attempt in range(policy.max_retries + 1):
            try:
                return await self._request_with_http_retries(
                    messages, model_settings, model_request_parameters
                )
            except ModelHTTPError:
                raise  # HTTP errors already handled by inner loop; don't catch here
            except ModelAPIError as exc:
                # Network/timeout error (ModelAPIError that is NOT ModelHTTPError)
                if attempt >= policy.max_retries:
                    raise
                delay = policy.delays[attempt] if attempt < len(policy.delays) else policy.delays[-1]
                logger.warning(
                    "Network error, retry %s/%s in %.1fs: %s",
                    attempt + 1,
                    policy.max_retries,
                    delay,
                    exc,
                )
                self.total_retry_wait_seconds += delay
                await asyncio.sleep(delay)
        # Unreachable, but keeps the type checker happy.
        raise RuntimeError("unreachable")
