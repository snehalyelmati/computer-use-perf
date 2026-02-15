"""OpenRouter client configuration (scaffold)."""

from __future__ import annotations

from dataclasses import dataclass
import os

from openai import AsyncOpenAI

from src.agent.config import LLMConfig


@dataclass(frozen=True)
class OpenRouterClient:
    """Thin wrapper for OpenRouter's OpenAI-compatible API."""

    client: AsyncOpenAI
    config: LLMConfig


def build_openrouter_client(config: LLMConfig) -> OpenRouterClient:
    api_key = os.environ.get(config.api_key_env)
    client = AsyncOpenAI(base_url=config.base_url, api_key=api_key)
    return OpenRouterClient(client=client, config=config)
