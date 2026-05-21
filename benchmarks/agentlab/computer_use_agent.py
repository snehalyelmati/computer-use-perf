"""AgentLab adapter that runs this agent inside BrowserGym-owned pages."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass
import traceback
from typing import Any, Callable, TypeVar

from src.agent.browser.external import build_external_browser_session_async
from src.agent.config import AgentConfig, LLMConfig, PROVIDER_DEFAULTS
from src.agent.core.step_runtime import BrowserAgentStepRuntime, RuntimeStepResult

try:  # pragma: no cover - exercised when optional benchmark deps are installed
    import bgym
    from agentlab.agents.agent_args import AgentArgs
except ImportError:  # pragma: no cover - local tests run without AgentLab
    bgym = None

    class AgentArgs:  # type: ignore[no-redef]
        agent_name: str | None = None

        def __post_init__(self) -> None:
            if self.agent_name is None:
                self.agent_name = self.__class__.__name__

    class _FallbackAgent:
        pass

    class _FallbackActionSet:
        def to_python_code(self, action: str) -> str:
            return action

    def _agent_info(**kwargs: Any) -> dict[str, Any]:
        return dict(kwargs)

else:
    _FallbackAgent = bgym.Agent
    _FallbackActionSet = bgym.HighLevelActionSet

    def _agent_info(**kwargs: Any) -> Any:
        return bgym.AgentInfo(**kwargs)


def _goal_from_obs(obs: dict[str, Any]) -> str:
    goal = obs.get("goal")
    if isinstance(goal, str) and goal.strip():
        return goal.strip()
    goal_object = obs.get("goal_object")
    if goal_object:
        return str(goal_object)
    return ""


def _noop_action() -> str:
    return "noop()"


BENCHMARK_DEFAULT_MODEL = "z-ai/glm-4.7:nitro"

T = TypeVar("T")


def _run_async_from_sync(
    coro_factory: Callable[[], Coroutine[Any, Any, T]],
    sync_owner: Any | None = None,
) -> T:
    sync_runner = getattr(sync_owner, "_sync", None)
    if callable(sync_runner):
        return sync_runner(coro_factory())
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    raise RuntimeError("Cannot run async agent step from a synchronous callback")


@dataclass
class ComputerUseAgentArgs(AgentArgs):
    """Serializable AgentLab configuration for this repo's full runtime."""

    agent_name: str = "computer-use-agent"
    use_raw_page_output: bool = True
    provider: str = "openrouter"
    model: str | None = BENCHMARK_DEFAULT_MODEL
    worker_model: str | None = None
    filter_model: str | None = None
    oracle_model: str | None = None
    max_steps: int = 100
    max_elements: int = 80
    max_worker_tool_calls: int = 10
    worker_context_steps: int = 3
    oracle_interval: int = 5
    stuck_threshold: int = 3
    unchanged_abort_threshold: int = 8
    step_timeout_seconds: int = 300
    max_tokens: int = 2048
    timeout_seconds: int = 60
    max_retries: int = 2
    log_dir: str = "logs/agentlab"
    log_level: str = "INFO"
    metrics_enabled: bool = True
    handlers_enabled: bool = True
    save_pages: bool = False
    unified: bool = True

    def make_agent(self) -> "ComputerUseAgentLabAgent":
        defaults = PROVIDER_DEFAULTS[self.provider]
        agent_config = AgentConfig(
            target_url=None,
            goal=None,
            max_steps=int(self.max_steps),
            max_elements=int(self.max_elements),
            max_worker_tool_calls=int(self.max_worker_tool_calls),
            worker_context_steps=int(self.worker_context_steps),
            oracle_interval=int(self.oracle_interval),
            stuck_threshold=int(self.stuck_threshold),
            unchanged_abort_threshold=int(self.unchanged_abort_threshold),
            step_timeout_seconds=int(self.step_timeout_seconds),
            log_dir=str(self.log_dir),
            log_level=str(self.log_level),
            metrics_enabled=bool(self.metrics_enabled),
            color_logs=False,
            handlers_enabled=bool(self.handlers_enabled),
            save_pages=bool(self.save_pages),
            unified=bool(self.unified),
        )
        llm_config = LLMConfig(
            provider=self.provider,  # type: ignore[arg-type]
            model=self.model or defaults["model"],
            worker_model=self.worker_model or defaults.get("worker_model") or None,
            filter_model=self.filter_model or defaults.get("filter_model") or None,
            oracle_model=self.oracle_model or defaults.get("oracle_model") or None,
            api_key_env=defaults["api_key_env"],
            timeout_seconds=int(self.timeout_seconds),
            max_retries=int(self.max_retries),
            max_tokens=int(self.max_tokens),
        )
        return ComputerUseAgentLabAgent(agent_config=agent_config, llm_config=llm_config)

    def set_reproducibility_mode(self) -> None:
        self.max_retries = 0


class ComputerUseAgentLabAgent(_FallbackAgent):
    """BrowserGym/AgentLab agent wrapper.

    BrowserGym owns task setup, browser lifecycle, and validation. This wrapper
    runs one internal step of the repo's normal runtime against the live
    BrowserGym page, then returns ``noop()`` so BrowserGym observes and validates
    the page that our tools already mutated.
    """

    def __init__(
        self,
        *,
        agent_config: AgentConfig,
        llm_config: LLMConfig,
        runtime_factory: Callable[[AgentConfig, LLMConfig], BrowserAgentStepRuntime] | None = None,
    ) -> None:
        self.agent_config = agent_config
        self.llm_config = llm_config
        self._runtime_factory = runtime_factory or BrowserAgentStepRuntime
        self._runtime = self._runtime_factory(agent_config, llm_config)
        self._raw_page: Any | None = None
        self.action_set = _FallbackActionSet() if bgym is None else bgym.HighLevelActionSet()

    def reset(self, seed: int | None = None) -> None:
        del seed
        old_runtime = self._runtime
        try:
            _run_async_from_sync(
                lambda: old_runtime.close(stop_reason="reset"),
                sync_owner=self._raw_page,
            )
        except RuntimeError:
            pass
        self._raw_page = None
        self._runtime = self._runtime_factory(self.agent_config, self.llm_config)

    def close(self) -> None:
        old_runtime = self._runtime
        try:
            _run_async_from_sync(
                lambda: old_runtime.close(stop_reason="close"),
                sync_owner=self._raw_page,
            )
        except RuntimeError:
            pass
        self._raw_page = None

    def obs_preprocessor(self, obs: dict[str, Any]) -> dict[str, Any]:
        processed = dict(obs)
        self._raw_page = processed.pop("page", None)
        return processed

    def get_action(self, obs: dict[str, Any]) -> tuple[str, Any]:
        if self._raw_page is None:
            info = _agent_info(
                stats={"computer_use_error": 1},
                markdown_page="No raw BrowserGym page was present. Set use_raw_page_output=True.",
                extra_info={"error": "missing_raw_page"},
            )
            return _noop_action(), info

        try:
            result = _run_async_from_sync(
                lambda: self._run_step(obs),
                sync_owner=self._raw_page,
            )
            info = self._info_from_result(result)
            return _noop_action(), info
        except Exception as exc:  # pragma: no cover - runtime containment
            info = _agent_info(
                stats={"computer_use_error": 1},
                markdown_page=f"Internal agent error: {type(exc).__name__}: {exc}",
                extra_info={
                    "err_msg": f"{type(exc).__name__}: {exc}",
                    "stack_trace": traceback.format_exc(),
                },
            )
            return _noop_action(), info

    async def _run_step(self, obs: dict[str, Any]) -> RuntimeStepResult:
        session = await build_external_browser_session_async(self._raw_page)
        try:
            return await self._runtime.run_one_step(session, goal=_goal_from_obs(obs))
        finally:
            await session.detach()

    def _info_from_result(self, result: RuntimeStepResult) -> Any:
        stats = {
            "computer_use_steps": result.step,
            "computer_use_step_elapsed": result.duration_ms / 1000.0,
            "computer_use_input_tokens": result.input_tokens,
            "computer_use_output_tokens": result.output_tokens,
            "computer_use_total_tokens": result.total_tokens,
            "computer_use_cost_usd": result.cost_usd or 0.0,
            "computer_use_internal_done": int(result.done),
        }
        lines = [
            f"Step: {result.step}",
            f"Internal done: {result.done}",
            f"Stop reason: {result.stop_reason or 'none'}",
            f"Worker goal: {result.worker_goal or 'none'}",
            f"Tool calls: {result.tool_calls or 'none'}",
            "",
            result.summary,
        ]
        return _agent_info(
            stats=stats,
            markdown_page="\n".join(lines),
            extra_info={
                "summary": result.summary,
                "stop_reason": result.stop_reason,
                "worker_goal": result.worker_goal,
                "tool_calls": result.tool_calls,
                "log_dir": result.log_dir,
                "trace": result.trace,
            },
        )


AGENTLAB_COMPUTER_USE = ComputerUseAgentArgs()
