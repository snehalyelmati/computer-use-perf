"""Retry wrapper for pydantic-ai models."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models import ModelRequestParameters, ModelResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.messages import ModelMessage
from pydantic_ai.settings import ModelSettings

logger = logging.getLogger("agent")

# Status codes worth retrying: the request itself isn't wrong,
# the provider just failed to produce a valid response.
_RETRYABLE_STATUS_CODES = {
    400,  # generation / parse errors (e.g. degenerate tool-call output)
    429,  # rate-limit
    500,  # internal server error
    502,  # bad gateway
    503,  # service unavailable
    504,  # gateway timeout
}


def _is_retryable(exc: ModelHTTPError) -> bool:
    return exc.status_code in _RETRYABLE_STATUS_CODES


@dataclass(init=False)
class ResilientModel(WrapperModel):
    """Wraps a Model with retry logic for transient HTTP errors."""

    max_retries: int
    base_delay: float

    def __init__(
        self,
        wrapped: object,
        *,
        max_retries: int = 2,
        base_delay: float = 1.0,
    ) -> None:
        super().__init__(wrapped)  # type: ignore[arg-type]
        self.max_retries = max_retries
        self.base_delay = base_delay

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        last_exc: ModelHTTPError | None = None
        for attempt in range(1, self.max_retries + 2):  # 1-indexed, includes initial try
            try:
                return await self.wrapped.request(
                    messages, model_settings, model_request_parameters
                )
            except ModelHTTPError as exc:
                if not _is_retryable(exc) or attempt == self.max_retries + 1:
                    raise
                last_exc = exc
                delay = self.base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "LLM request failed (status %s), retry %s/%s in %.1fs: %s",
                    exc.status_code,
                    attempt,
                    self.max_retries,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
        # Unreachable, but keeps the type checker happy.
        raise last_exc  # type: ignore[misc]
