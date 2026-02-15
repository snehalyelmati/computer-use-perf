from pathlib import Path
import sys

from pydantic_ai.messages import ModelResponse, TextPart

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.metrics import extract_openrouter_cost


def test_extract_openrouter_cost_returns_none_when_absent() -> None:
    messages = [ModelResponse(parts=[TextPart("ok")], provider_details={"downstream_provider": "x"})]
    assert extract_openrouter_cost(messages) is None


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

