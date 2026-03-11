from pathlib import Path
import sys

from pydantic_ai.messages import ModelResponse, TextPart

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.config import MODEL_PRICES, ModelPricing
from src.agent.metrics import compute_cost_from_usage, extract_openrouter_cost, UsageStats


def test_extract_openrouter_cost_returns_none_when_absent() -> None:
    messages = [ModelResponse(parts=[TextPart("ok")], provider_details={"downstream_provider": "x"})]
    assert extract_openrouter_cost(messages) is None


def test_compute_cost_cache_aware_pricing() -> None:
    """Cache reads/writes are priced at their own rates, not the flat input rate."""
    MODEL_PRICES["_test_cache"] = ModelPricing(
        input_per_mtok=1.0,
        output_per_mtok=2.0,
        cache_write_per_mtok=1.25,
        cache_read_per_mtok=0.0,  # free cache reads
    )
    try:
        usage = UsageStats(
            requests=1,
            tool_calls=0,
            input_tokens=1_000_000,  # includes cache tokens per convention
            output_tokens=500_000,
            cache_write_tokens=200_000,
            cache_read_tokens=300_000,
            input_audio_tokens=0,
            cache_audio_read_tokens=0,
            output_audio_tokens=0,
        )
        result = compute_cost_from_usage("_test_cache", usage)
        assert result is not None

        # uncached = 1M - 200k - 300k = 500k
        # input_cost = 500k*1.0 + 200k*1.25 + 300k*0.0 = 750_000
        # output_cost = 500k*2.0 = 1_000_000
        # total = 1_750_000 / 1_000_000 = 1.75
        assert result.cost_usd == 1.75

        # flat-rate would be: (1M*1.0 + 500k*2.0) / 1M = 2.0
        flat_rate = (1_000_000 * 1.0 + 500_000 * 2.0) / 1_000_000
        assert result.cost_usd < flat_rate
    finally:
        del MODEL_PRICES["_test_cache"]


def test_compute_cost_cache_defaults_to_input_rate() -> None:
    """When cache rates are None, cache tokens are charged at the input rate."""
    MODEL_PRICES["_test_no_cache"] = ModelPricing(
        input_per_mtok=1.0,
        output_per_mtok=2.0,
    )
    try:
        usage = UsageStats(
            requests=1,
            tool_calls=0,
            input_tokens=1_000_000,
            output_tokens=0,
            cache_write_tokens=400_000,
            cache_read_tokens=100_000,
            input_audio_tokens=0,
            cache_audio_read_tokens=0,
            output_audio_tokens=0,
        )
        result = compute_cost_from_usage("_test_no_cache", usage)
        assert result is not None
        # All input tokens charged at 1.0 regardless of cache split
        assert result.cost_usd == 1.0
    finally:
        del MODEL_PRICES["_test_no_cache"]


def test_extract_openrouter_cost_byok_uses_upstream_when_cost_zero() -> None:
    """BYOK providers report cost=0; cost_usd should fall back to upstream_inference_cost."""
    messages = [
        ModelResponse(
            parts=[TextPart("a")],
            provider_response_id="r1",
            provider_details={"cost": 0, "upstream_inference_cost": 0.005},
        ),
        ModelResponse(
            parts=[TextPart("b")],
            provider_response_id="r2",
            provider_details={"cost": 0, "upstream_inference_cost": 0.010},
        ),
    ]
    stats = extract_openrouter_cost(messages)
    assert stats is not None
    assert stats.cost_usd == 0.015
    assert stats.upstream_inference_cost_usd == 0.015


def test_extract_openrouter_cost_sums_and_dedupes_by_response_id() -> None:
    messages = [
        ModelResponse(
            parts=[TextPart("a")],
            provider_response_id="r1",
            provider_details={"cost": 0.01, "upstream_inference_cost": 0.005},
        ),
        ModelResponse(
            parts=[TextPart("b")],
            provider_response_id="r1",
            provider_details={"cost": 0.99, "upstream_inference_cost": 0.99},
        ),
        ModelResponse(
            parts=[TextPart("c")],
            provider_response_id="r2",
            provider_details={"cost": 0.02},
        ),
    ]
    stats = extract_openrouter_cost(messages)
    assert stats is not None
    assert stats.cost_usd == 0.03
    assert stats.upstream_inference_cost_usd == 0.005

