from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .providers import TokenPricingPer1M, find_unique_model_spec, get_model_spec


def _compute_cost_usd(
    *,
    pricing: TokenPricingPer1M,
    prompt_tokens: int,
    cached_prompt_tokens: int,
    completion_tokens: int,
) -> float:
    cached = max(0, int(cached_prompt_tokens or 0))
    prompt = max(0, int(prompt_tokens or 0))
    uncached = max(0, prompt - cached)
    completion = max(0, int(completion_tokens or 0))
    return (
        (uncached / 1_000_000.0) * pricing.input_usd_per_1m
        + (cached / 1_000_000.0) * pricing.cached_input_usd_per_1m
        + (completion / 1_000_000.0) * pricing.output_usd_per_1m
    )


def _pricing_for(provider: str, model: str) -> TokenPricingPer1M:
    spec = None
    if provider in ("groq", "cerebras"):
        spec = get_model_spec(provider, model)
    if spec is None:
        spec = find_unique_model_spec(model)
    if spec is not None:
        return spec.pricing
    return TokenPricingPer1M(0.0, 0.0, 0.0)


@dataclass
class LLMCallRecord:
    call_type: str
    model: str
    provider: str
    attempt: int
    ok: bool
    duration_s: float
    prompt_tokens: int = 0
    cached_prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None


@dataclass
class ChallengeRecord:
    index: int
    url: str
    start_ts: float
    end_ts: float | None = None
    duration_s: float | None = None
    completed: bool = False
    steps: int = 0
    reason: str | None = None

    prompt_tokens: int = 0
    cached_prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class RunStats:
    run_mode: str
    provider: str
    model_overview: str | None
    model_oracle: str | None
    model_action: str | None
    model_filter: str | None
    start_ts: float = field(default_factory=time.time)
    end_ts: float | None = None
    duration_s: float | None = None
    max_steps: int | None = None

    challenges: list[ChallengeRecord] = field(default_factory=list)
    llm_calls: list[LLMCallRecord] = field(default_factory=list)

    prompt_tokens: int = 0
    cached_prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    by_call_type: dict[str, dict[str, float]] = field(default_factory=dict)
    by_model: dict[str, dict[str, float]] = field(default_factory=dict)


class StatsCollector:
    """Collect run statistics (tokens, time, cost).

    Cost uses a per-model dummy pricing table in `src/agent/providers.py`.
    """

    def __init__(
        self,
        *,
        run_mode: str,
        provider: str,
        model_overview: str | None,
        model_oracle: str | None,
        model_action: str | None,
        model_filter: str | None,
        max_steps: int | None = None,
    ):
        self._run = RunStats(
            run_mode=run_mode,
            provider=provider,
            model_overview=model_overview,
            model_oracle=model_oracle,
            model_action=model_action,
            model_filter=model_filter,
            max_steps=max_steps,
        )
        self._current_challenge: ChallengeRecord | None = None

    @property
    def run(self) -> RunStats:
        return self._run

    def start_challenge(self, index: int, url: str) -> None:
        rec = ChallengeRecord(index=index, url=url, start_ts=time.time())
        self._run.challenges.append(rec)
        self._current_challenge = rec

    def end_challenge(self, *, completed: bool, reason: str | None = None) -> None:
        if not self._current_challenge:
            return
        end_ts = time.time()
        self._current_challenge.end_ts = end_ts
        self._current_challenge.duration_s = max(
            0.0, end_ts - self._current_challenge.start_ts
        )
        self._current_challenge.completed = completed
        self._current_challenge.reason = reason
        self._current_challenge = None

    def increment_step(self) -> None:
        if self._current_challenge:
            self._current_challenge.steps += 1

    def record_llm_call(
        self,
        *,
        call_type: str,
        model: str,
        provider: str,
        attempt: int,
        ok: bool,
        duration_s: float,
        prompt_tokens: int = 0,
        cached_prompt_tokens: int = 0,
        completion_tokens: int = 0,
        error: str | None = None,
    ) -> None:
        pricing = _pricing_for(provider, model)
        cost = _compute_cost_usd(
            pricing=pricing,
            prompt_tokens=prompt_tokens,
            cached_prompt_tokens=cached_prompt_tokens,
            completion_tokens=completion_tokens,
        )

        self._run.llm_calls.append(
            LLMCallRecord(
                call_type=call_type,
                model=model,
                provider=provider,
                attempt=attempt,
                ok=ok,
                duration_s=duration_s,
                prompt_tokens=int(prompt_tokens or 0),
                cached_prompt_tokens=int(cached_prompt_tokens or 0),
                completion_tokens=int(completion_tokens or 0),
                cost_usd=float(cost or 0.0),
                error=(error[:300] if error else None),
            )
        )

        self._run.prompt_tokens += int(prompt_tokens or 0)
        self._run.cached_prompt_tokens += int(cached_prompt_tokens or 0)
        self._run.completion_tokens += int(completion_tokens or 0)
        self._run.cost_usd += float(cost or 0.0)

        # Call-type aggregates
        bct = self._run.by_call_type.setdefault(
            call_type,
            {
                "prompt_tokens": 0.0,
                "cached_prompt_tokens": 0.0,
                "completion_tokens": 0.0,
                "cost_usd": 0.0,
            },
        )
        bct["prompt_tokens"] += int(prompt_tokens or 0)
        bct["cached_prompt_tokens"] += int(cached_prompt_tokens or 0)
        bct["completion_tokens"] += int(completion_tokens or 0)
        bct["cost_usd"] += float(cost or 0.0)

        # Model aggregates
        bm = self._run.by_model.setdefault(
            model,
            {
                "prompt_tokens": 0.0,
                "cached_prompt_tokens": 0.0,
                "completion_tokens": 0.0,
                "cost_usd": 0.0,
            },
        )
        bm["prompt_tokens"] += int(prompt_tokens or 0)
        bm["cached_prompt_tokens"] += int(cached_prompt_tokens or 0)
        bm["completion_tokens"] += int(completion_tokens or 0)
        bm["cost_usd"] += float(cost or 0.0)

        # Best-effort challenge attribution.
        if self._current_challenge and call_type not in ("learning",):
            self._current_challenge.prompt_tokens += int(prompt_tokens or 0)
            self._current_challenge.cached_prompt_tokens += int(
                cached_prompt_tokens or 0
            )
            self._current_challenge.completion_tokens += int(completion_tokens or 0)
            self._current_challenge.cost_usd += float(cost or 0.0)

    def end_run(self) -> None:
        if self._run.end_ts is None:
            self._run.end_ts = time.time()
        if self._run.duration_s is None and self._run.end_ts is not None:
            self._run.duration_s = max(0.0, self._run.end_ts - self._run.start_ts)

    def to_json(self) -> str:
        self.end_run()
        return json.dumps(asdict(self._run), indent=2, sort_keys=False)

    def to_markdown(self) -> str:
        self.end_run()

        dur = self._run.duration_s or 0.0
        total_in = int(self._run.prompt_tokens)
        total_cached = int(self._run.cached_prompt_tokens)
        total_out = int(self._run.completion_tokens)
        total_tokens = total_in + total_out

        completed = sum(1 for c in self._run.challenges if c.completed)
        total_challenges = len(self._run.challenges)

        lines: list[str] = []
        lines.append("# Run Statistics")
        lines.append("")
        lines.append(f"- Mode: {self._run.run_mode}")
        lines.append(f"- Provider: {self._run.provider}")
        lines.append(
            f"- Models: overview={self._run.model_overview}, oracle={self._run.model_oracle}, action={self._run.model_action}, filter={self._run.model_filter}"
        )
        lines.append(f"- Duration: {dur:.2f}s")
        lines.append(f"- Challenges: {completed}/{total_challenges} completed")
        lines.append(
            f"- Tokens: {total_tokens} (in={total_in}, cached_in={total_cached}, out={total_out})"
        )
        lines.append(f"- Cost (USD, dummy): {self._run.cost_usd:.6f}")
        lines.append("")

        if self._run.by_call_type:
            lines.append("## Tokens By Call Type")
            for ct, agg in sorted(self._run.by_call_type.items()):
                pt = int(agg.get("prompt_tokens", 0))
                cp = int(agg.get("cached_prompt_tokens", 0))
                ot = int(agg.get("completion_tokens", 0))
                cost = float(agg.get("cost_usd", 0.0))
                lines.append(
                    f"- {ct}: tokens={pt + ot} (in={pt}, cached_in={cp}, out={ot}), cost_usd={cost:.6f}"
                )
            lines.append("")

        if self._run.challenges:
            lines.append("## Per-Challenge")
            for c in self._run.challenges:
                d_str = (
                    f"{c.duration_s:.2f}s" if c.duration_s is not None else "(running)"
                )
                status = "ok" if c.completed else "incomplete"
                tokens = c.prompt_tokens + c.completion_tokens
                lines.append(
                    f"- {c.index}: {status}, steps={c.steps}, duration={d_str}, tokens={tokens} (cached_in={c.cached_prompt_tokens}), cost_usd={c.cost_usd:.6f}"
                )
            lines.append("")

        lines.append("## Notes")
        lines.append(
            "- Cost uses dummy pricing in `src/agent/providers.py` (update later)."
        )
        lines.append("- Cached tokens are reported when the provider returns them.")
        lines.append("")
        return "\n".join(lines)


def write_run_stats(*, log_dir: str, stats: StatsCollector) -> tuple[str, str]:
    """Write `run_stats.json` and `run_stats.md` into log_dir."""

    Path(log_dir).mkdir(parents=True, exist_ok=True)
    json_path = str(Path(log_dir) / "run_stats.json")
    md_path = str(Path(log_dir) / "run_stats.md")
    with open(json_path, "w") as f:
        f.write(stats.to_json())
        f.write("\n")
    with open(md_path, "w") as f:
        f.write(stats.to_markdown())
        f.write("\n")
    return (json_path, md_path)
