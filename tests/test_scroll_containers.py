from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.context.scroll_containers import (
    extract_scroll_containers,
    cleanup_scroll_container_attributes,
)


@pytest.mark.asyncio
async def test_extract_scroll_containers_returns_count() -> None:
    page = AsyncMock()
    page.evaluate.return_value = 5
    count = await extract_scroll_containers(page)
    assert count == 5
    page.evaluate.assert_called_once()


@pytest.mark.asyncio
async def test_extract_scroll_containers_non_int_returns_zero() -> None:
    page = AsyncMock()
    page.evaluate.return_value = None
    count = await extract_scroll_containers(page)
    assert count == 0


@pytest.mark.asyncio
async def test_extract_scroll_containers_exception_returns_zero() -> None:
    page = AsyncMock()
    page.evaluate.side_effect = Exception("page crashed")
    count = await extract_scroll_containers(page)
    assert count == 0


@pytest.mark.asyncio
async def test_cleanup_runs_evaluate() -> None:
    page = AsyncMock()
    await cleanup_scroll_container_attributes(page)
    page.evaluate.assert_called_once()


@pytest.mark.asyncio
async def test_cleanup_swallows_exception() -> None:
    page = AsyncMock()
    page.evaluate.side_effect = Exception("page crashed")
    # Should not raise
    await cleanup_scroll_container_attributes(page)
