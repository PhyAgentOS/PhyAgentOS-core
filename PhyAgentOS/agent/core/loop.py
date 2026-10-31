"""Concrete loop driver for the agent core.

One turn is a sequence of model requests, each followed by the settlement of
the tool calls it produced. The driver starts from ``TurnStart.history`` (the
completed turns) plus the current input, mints a step id per iteration, and
stops on a final answer, cancellation, deadline, failure, or step exhaustion.

Event ownership is split so nothing is recorded twice: this module writes only
``request.prepared``, ``model.attempt.*``, ``model.response.delta``, and
``context.compacted``. ``input.accepted`` and terminal ``turn.*`` events belong
to the runtime; ``tool.*`` events belong to the executor. A driver that wrote
them would make a single transition look like two.

Persistence comes before side effects: the request snapshot and
``model.attempt.started`` are durably appended before the adapter is called.
If either append fails, the model is not called and the turn fails, so a crash
cannot leave an unrecorded model call behind and a failed write cannot turn
into a completed turn.

Cancellation is local only. A token request ends the local model wait (the
adapter task is cancelled and reaped) and stops the driver before the next
call; it says nothing about remote work that was already dispatched. Whether a
dispatched action stopped is reported by the tool outcome, never inferred
here. An external task cancellation is re-raised rather than being recorded as
an answer.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from .events import EventType, RuntimeEvent
from .protocols import ModelAdapter, PromptAssembler, SessionStore, ToolExecutor
from .types import (
    CORE_PERSISTENCE_FAILURE_CODES,
    ExecutionContext,
    ModelCallError,
    ModelRequest,
    ModelResponse,
    ModelStreamDelta,
    OperationCancelledError,
    PromptBundle,
    ToolCall,
    ToolDefinition,
    ToolOutcome,
    ToolPolicy,
    ToolStatus,
    TurnResult,
    TurnStart,
    TurnStatus,
)

__all__ = ["LoopDriverImpl"]

# A provider may report an over-window request with this structured code; the
# driver then asks the injected context policy for one forced reduction and
# retries the model call exactly once. The old failure text alone is not enough
# to start that recovery.
_CONTEXT_OVERFLOW_CODE = "context_overflow"

# Streaming display batches: the first delta is written immediately so text is
# visible before the model finishes; later deltas are grouped by size or by a
# short idle interval so one token does not become one synchronous SQLite
# commit and a slow stream still appears promptly. The queue is bounded so a
# slow store applies backpressure instead of growing memory without limit.
_DELTA_FLUSH_CHARS = 64
_DELTA_FLUSH_INTERVAL = 0.05
_DELTA_QUEUE_SIZE = 64


@dataclass(frozen=True, slots=True)
class _ModelStep:
    """Result of one model attempt: an answer or an already-decided failure."""

    response: ModelResponse | None = None
    failure: TurnResult | None = None
    policy: ToolPolicy | None = None
    failure_code: str | None = None
    failure_usage: Mapping[str, int] | None = None


def _describe(exc: BaseException) -> str:
    detail = str(exc).strip()
    return detail or type(exc).__name__


async def _close_stream(stream: object) -> None:
    """Close an adapter stream, awaiting the result when needed."""
    for name in ("aclose", "close"):
        close = getattr(stream, name, None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result
            return


def _tool_call_payload(call: ToolCall) -> dict[str, Any]:
    """Rebuild the provider's tool-call shape, keeping raw JSON and extras."""
    function: dict[str, Any] = {
        "name": call.name,
        "arguments": (
            call.raw_arguments
            if call.raw_arguments is not None
            else json.dumps(dict(call.arguments), ensure_ascii=False)
        ),
    }
    if call.function_provider_fields:
        function["provider_specific_fields"] = copy.deepcopy(
            dict(call.function_provider_fields)
        )
    payload: dict[str, Any] = {
        "id": call.tool_call_id,
        "type": "function",
        "function": function,
    }
    if call.provider_fields:
        payload["provider_specific_fields"] = copy.deepcopy(dict(call.provider_fields))
    return payload


def _assistant_message(response: ModelResponse) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": response.content}
    if response.tool_calls:
        message["tool_calls"] = [_tool_call_payload(call) for call in response.tool_calls]
    if response.reasoning_content is not None:
        message["reasoning_content"] = response.reasoning_content
    if response.thinking_blocks is not None:
        message["thinking_blocks"] = [
            copy.deepcopy(dict(block)) for block in response.thinking_blocks
        ]
    return message


def _tool_message(outcome: ToolOutcome) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": outcome.tool_call_id,
        "name": outcome.tool_name,
        "content": outcome.for_model(),
    }


def _prepared_payload(request: ModelRequest) -> dict[str, Any]:
    """Snapshot the exact request: messages, tools and safe generation knobs."""
    return {
        "model_call_id": request.model_call_id,
        "messages": request.to_messages(),
        "tools": request.to_provider_tools(),
        "model": request.model,
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "reasoning_effort": request.reasoning_effort,
        "tool_choice": request.tool_choice,
    }


def _completed_payload(
    response: ModelResponse, model_call_id: str, streaming: str = "blocking"
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model_call_id": model_call_id,
        "finish_reason": response.finish_reason,
        "usage": dict(response.usage),
        "tool_calls": [_tool_call_payload(call) for call in response.tool_calls],
        "streaming": streaming,
    }
    if response.content is not None:
        payload["content"] = response.content
    if response.reasoning_content is not None:
        payload["reasoning_content"] = response.reasoning_content
    if response.thinking_blocks is not None:
        payload["thinking_blocks"] = [
            copy.deepcopy(dict(block)) for block in response.thinking_blocks
        ]
    return payload


def _result(
    turn: TurnStart,
    status: TurnStatus,
    *,
    reply: str | None = None,
    error: str | None = None,
) -> TurnResult:
    return TurnResult(
        session_id=turn.request.session_id,
        turn_id=turn.turn_id,
        status=status,
        reply=reply,
        error=error,
    )


def _failed(turn: TurnStart, error: str) -> TurnResult:
    return _result(turn, TurnStatus.FAILED, error=error)


def _failed_with_reply(
    turn: TurnStart, error: str, reply: str | None
) -> TurnResult:
    """A failed turn that still reports the useful progress text it had."""
    return _result(turn, TurnStatus.FAILED, reply=reply, error=error)


def _interrupted(turn: TurnStart) -> TurnResult:
    return _result(turn, TurnStatus.INTERRUPTED)


def _local_stop(turn: TurnStart) -> TurnResult | None:
    """Stop local work before starting anything new, if asked or out of time."""
    if turn.cancellation.cancelled:
        return _interrupted(turn)
    if turn.budget.expired():
        return _failed(turn, "turn budget deadline exceeded")
    return None


class LoopDriverImpl:
    """Drives one turn: assemble, call the model, run tools, repeat.

    ``tools`` is the set of definitions offered for this driver; the assembler
    may narrow it but cannot add to it. ``model`` and the generation parameters
    are fixed at construction so every attempt in the turn is comparable.

    ``context_policy``, ``turn_budget`` and ``steering`` are optional
    integrations. When all are ``None`` (the default) the driver behaves
    exactly like the version without them: no extra model call, no history
    rewrite, and no budget change. ``max_parallel_tools`` only reaches an
    executor that implements the optional ``execute_batch`` extension.
    """

    def __init__(
        self,
        *,
        assembler: PromptAssembler,
        adapter: ModelAdapter,
        executor: ToolExecutor,
        store: SessionStore,
        tools: Sequence[ToolDefinition] = (),
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        tool_choice: str | None = None,
        max_parallel_tools: int = 4,
        context_policy: Any = None,
        turn_budget: Any = None,
        steering: Any = None,
    ) -> None:
        frozen_tools = tuple(tools)
        for tool in frozen_tools:
            if not isinstance(tool, ToolDefinition):
                raise TypeError("tools must contain ToolDefinition instances")
        if (
            not isinstance(max_parallel_tools, int)
            or isinstance(max_parallel_tools, bool)
            or max_parallel_tools < 1
        ):
            raise ValueError("max_parallel_tools must be an int >= 1")
        self._assembler = assembler
        self._adapter = adapter
        self._executor = executor
        self._store = store
        self._tools = frozen_tools
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._reasoning_effort = reasoning_effort
        self._tool_choice = tool_choice
        self._max_parallel_tools = max_parallel_tools
        self._context_policy = context_policy
        self._turn_budget = turn_budget
        self._steering = steering

    async def run(self, turn: TurnStart) -> TurnResult:
        budget = self._begin_turn_budget(turn)
        # Deep copy so the turn's frozen history and the mutable provider
        # messages cannot alias each other.
        history: list[dict[str, Any]] = [
            copy.deepcopy(dict(message)) for message in turn.history
        ]
        history.append({"role": "user", "content": turn.request.text})
        last_reply: str | None = None
        for step_number in range(1, turn.budget.max_steps + 1):
            stop = _local_stop(turn)
            if stop is not None:
                return stop
            if budget is not None and budget.token_budget_exhausted():
                # A hard token ceiling is not a budget for one more call.
                return _failed_with_reply(
                    turn,
                    "turn token budget exhausted; no further model call was made",
                    last_reply,
                )
            context = turn.step_context(f"{turn.turn_id}:step:{step_number}")
            steering_failure = await self._apply_steering(turn, context, history)
            if steering_failure is not None:
                return steering_failure
            # The context policy needs the real next request to estimate live
            # fragments and the assembler-visible tool set. Assemble once here;
            # when nothing is reduced, the same bundle is reused for the model
            # call. A reduction replaces only the pure history and forces one
            # re-assembly, so runtime fragments are never stored in history.
            bundle: PromptBundle | None = None
            if self._context_policy is not None:
                bundle, assemble_failure = await self._assemble_bundle(
                    turn, context, history
                )
                if assemble_failure is not None:
                    return assemble_failure
                bundle, _, policy_failure = await self._prepare_context(
                    turn, context, history, bundle=bundle, force=False, budget=budget
                )
                if policy_failure is not None:
                    return policy_failure
                stop = _local_stop(turn)
                if stop is not None:
                    return stop
                if budget is not None and budget.token_budget_exhausted():
                    # The preparation itself may have paid for a summary. Do
                    # not spend the normal request on top of the cap.
                    return _failed_with_reply(
                        turn,
                        "turn token budget exhausted after context preparation; "
                        "no model call was made",
                        last_reply,
                    )
            if budget is not None and budget.is_summary_step(
                step_number, turn.budget.max_steps
            ):
                return await self._run_summary_step(
                    turn, context, history, budget, bundle=bundle
                )
            step = await self._run_model_step(
                turn, context, history, bundle=bundle
            )
            failure_usage_recorded = False
            if (
                step.failure is not None
                and step.failure_code == _CONTEXT_OVERFLOW_CODE
                and self._context_policy is not None
            ):
                if budget is not None and step.failure_usage:
                    budget.record_usage(step.failure_usage)
                    failure_usage_recorded = True
                if budget is not None and budget.token_budget_exhausted():
                    # A retry (and any forced summary inside the policy) is a
                    # new paid request. Once the measured total reached the
                    # hard cap, do not make it.
                    return _failed_with_reply(
                        turn,
                        "turn token budget exhausted after a context-overflow "
                        "failure; the model call was not retried",
                        last_reply,
                    )
                bundle, changed, policy_failure = await self._prepare_context(
                    turn, context, history, bundle=bundle, force=True, budget=budget
                )
                if policy_failure is not None:
                    return policy_failure
                stop = _local_stop(turn)
                if stop is not None:
                    return stop
                if budget is not None and budget.token_budget_exhausted():
                    # A forced summary may have consumed the remaining budget;
                    # a retry would exceed it.
                    return _failed_with_reply(
                        turn,
                        "turn token budget exhausted after context-preparation "
                        "recovery; the model call was not retried",
                        last_reply,
                    )
                if changed:
                    # One bounded recovery only: the retry uses a fresh step
                    # id (so its model call id cannot collide) and no tool is
                    # replayed because the failed attempt never ran one.
                    context = context.with_step(f"{context.step_id}:retry")
                    step = await self._run_model_step(
                        turn, context, history, bundle=bundle
                    )
                    failure_usage_recorded = False
            if step.failure is not None:
                if budget is not None and step.failure_usage and not failure_usage_recorded:
                    budget.record_usage(step.failure_usage)
                return step.failure
            response = step.response
            if response is None:  # pragma: no cover - guarded by _ModelStep
                return _failed(turn, "model step ended without a response")
            if budget is not None:
                budget.record_usage(response.usage)
            if response.content is not None:
                last_reply = response.content
            history.append(_assistant_message(response))
            if not response.tool_calls:
                return _result(turn, TurnStatus.COMPLETED, reply=response.content)
            if budget is not None and budget.token_budget_exhausted():
                # The model already asked for tools, but following up would
                # require another paid call. Stop before dispatching anything.
                return _failed_with_reply(
                    turn,
                    "turn token budget exhausted; the requested tools were not "
                    "executed and no further model call was made",
                    response.content,
                )
            tool_context = (
                context
                if step.policy is None
                else replace(context, policy=step.policy)
            )
            stop = await self._run_tools(
                turn,
                tool_context,
                response.tool_calls,
                history,
                budget=budget,
                reply=response.content,
            )
            if stop is not None:
                return stop
            # A cancel requested while tools ran must not trigger another call.
            stop = _local_stop(turn)
            if stop is not None:
                return stop
        return _failed(
            turn, f"step budget exhausted after {turn.budget.max_steps} steps"
        )

    async def _assemble_bundle(
        self,
        turn: TurnStart,
        context: ExecutionContext,
        history: list[dict[str, Any]],
    ) -> tuple[PromptBundle | None, TurnResult | None]:
        """Assemble the real next request once, or return a turn failure."""
        try:
            bundle = await self._assembler.assemble(
                context, history=tuple(history), tools=self._tools
            )
        except OperationCancelledError:
            return None, _interrupted(turn)
        except Exception as exc:
            return None, _failed(
                turn, f"prompt assembly failed: {_describe(exc)}"
            )
        if not isinstance(bundle, PromptBundle):
            return None, _failed(
                turn,
                "prompt assembler returned "
                f"{type(bundle).__name__}, expected PromptBundle",
            )
        return bundle, None

    async def _run_model_step(
        self,
        turn: TurnStart,
        context: ExecutionContext,
        history: list[dict[str, Any]],
        *,
        summary: bool = False,
        bundle: PromptBundle | None = None,
    ) -> _ModelStep:
        if bundle is None:
            bundle, assemble_failure = await self._assemble_bundle(
                turn, context, history
            )
            if assemble_failure is not None:
                return _ModelStep(failure=assemble_failure)

        request = ModelRequest(
            model_call_id=f"{context.step_id}:model",
            messages=bundle.messages,
            tools=() if summary else bundle.tools,
            model=self._model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            reasoning_effort=self._reasoning_effort,
            tool_choice="none" if summary else self._tool_choice,
        )

        # Persist before the side effect: no model call without a durable
        # snapshot, so a crash cannot leave an unexplained request behind.
        if not await self._persist(
            turn, context, EventType.REQUEST_PREPARED, _prepared_payload(request)
        ):
            return _ModelStep(
                failure=_failed(
                    turn, "could not persist request.prepared; model was not called"
                )
            )
        if not await self._persist(
            turn,
            context,
            EventType.MODEL_ATTEMPT_STARTED,
            {"model_call_id": request.model_call_id},
        ):
            return _ModelStep(
                failure=_failed(
                    turn,
                    "could not persist model.attempt.started; model was not called",
                )
            )

        stream_factory = getattr(self._adapter, "stream", None)
        if callable(stream_factory):
            step = await self._run_streaming_model(
                turn, context, request, stream_factory
            )
        else:
            step = await self._run_blocking_model(turn, context, request)
        if bundle.policy is not None and step.policy is None:
            return replace(step, policy=bundle.policy)
        return step

    async def _run_streaming_model(
        self,
        turn: TurnStart,
        context: ExecutionContext,
        request: ModelRequest,
        stream_factory: Any,
    ) -> _ModelStep:
        """Run one streamed attempt and persist its deltas before it ends.

        A producer task feeds a bounded queue with a fixed-capacity backlog, so
        a slow consumer applies backpressure instead of buffering without
        limit. The consumer flushes the first delta immediately and then on
        either a character threshold or a short idle interval, so a slow stream
        still becomes visible and a token is never one SQLite commit.

        Everything already received is made durable before a failure or local
        stop is recorded: the queue is drained, the buffered tail is written,
        and only then are ``model.attempt.failed`` / ``interrupted`` appended.
        That keeps the partial answer traceable while leaving the turn
        unsuccessful and the conversation history untouched. A provider failure
        after the first delta is never replayed.
        """
        model_call_id = request.model_call_id

        try:
            stream = stream_factory(request)
        except Exception as exc:
            detail = _describe(exc)
            await self._record_attempt_failure(
                turn, context, model_call_id, detail
            )
            return _ModelStep(
                failure=_failed(turn, f"model call failed: {detail}"),
                failure_code=(exc.code if isinstance(exc, ModelCallError) else None),
            )
        if not hasattr(stream, "__aiter__"):
            detail = (
                "model adapter stream did not return an async iterator "
                f"(got {type(stream).__name__})"
            )
            await self._record_attempt_failure(
                turn, context, model_call_id, detail
            )
            return _ModelStep(failure=_failed(turn, f"model call failed: {detail}"))

        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=_DELTA_QUEUE_SIZE)
        end = object()

        async def pump() -> None:
            try:
                async for item in stream:
                    await queue.put(item)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - reported to the driver
                await queue.put(exc)
                return
            await queue.put(end)

        producer = asyncio.create_task(pump())

        final: ModelResponse | None = None
        pending: list[str] = []
        pending_chars = 0
        persisted_any = False
        last_flush = time.monotonic()
        local_stop = False
        failure_detail: str | None = None
        failure_usage: dict[str, int] = {}
        failure_code: str | None = None
        flush_failed = False
        observed_usage: dict[str, int] = {}

        async def flush_pending() -> bool:
            nonlocal pending_chars, last_flush
            if not pending:
                return True
            if not await self._persist_delta(
                turn, context, model_call_id, "".join(pending)
            ):
                return False
            pending.clear()
            pending_chars = 0
            last_flush = time.monotonic()
            return True

        try:
            while True:
                if turn.cancellation.cancelled:
                    local_stop = True
                    break
                try:
                    item = await asyncio.wait_for(
                        queue.get(), timeout=_DELTA_FLUSH_INTERVAL
                    )
                except asyncio.TimeoutError:
                    # A slow stream must still become visible: flush what was
                    # received instead of waiting to reach the char threshold.
                    if not await flush_pending():
                        flush_failed = True
                        failure_detail = (
                            "could not persist model.response.delta; "
                            "the model call was stopped"
                        )
                        break
                    continue
                if item is end:
                    break
                if isinstance(item, BaseException):
                    failure_detail = _describe(item)
                    if isinstance(item, ModelCallError):
                        failure_usage = dict(item.usage)
                        failure_code = item.code
                    break
                if isinstance(item, ModelStreamDelta):
                    pending.append(item.content)
                    pending_chars += len(item.content)
                    elapsed = time.monotonic() - last_flush
                    if (
                        not persisted_any
                        or pending_chars >= _DELTA_FLUSH_CHARS
                        or elapsed >= _DELTA_FLUSH_INTERVAL
                    ):
                        if not await flush_pending():
                            flush_failed = True
                            failure_detail = (
                                "could not persist model.response.delta; "
                                "the model call was stopped"
                            )
                            break
                        persisted_any = True
                    continue
                if isinstance(item, ModelResponse):
                    final = item
                    break
                failure_detail = (
                    "model adapter stream yielded "
                    f"{type(item).__name__}, expected ModelStreamDelta or ModelResponse"
                )
                break
        finally:
            # Stop the producer first, then keep whatever text was already
            # handed over. This is what makes a cut-off answer traceable.
            if not producer.done():
                producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
            observed_usage = dict(getattr(stream, "usage", {}) or {})
            while True:
                try:
                    buffered = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if isinstance(buffered, ModelStreamDelta):
                    pending.append(buffered.content)
            if not await flush_pending():
                flush_failed = True
            await _close_stream(stream)

        if local_stop:
            payload: dict[str, Any] = {"model_call_id": model_call_id}
            if observed_usage:
                payload["usage"] = observed_usage
            await self._persist(
                turn, context, EventType.MODEL_ATTEMPT_INTERRUPTED, payload
            )
            return _ModelStep(failure=_interrupted(turn))

        usage_payload = dict(observed_usage)
        usage_payload.update(failure_usage)

        if failure_detail is not None or flush_failed:
            detail = failure_detail or (
                "could not persist model.response.delta; no tool was executed"
            )
            await self._record_attempt_failure(
                turn, context, model_call_id, detail, usage_payload or None
            )
            return _ModelStep(
                failure=_failed(turn, f"model call failed: {detail}"),
                # A provider stream that already emitted body text is never
                # replayed, even if its terminal error names context overflow.
                failure_code=None if persisted_any else failure_code,
                failure_usage=usage_payload or None,
            )

        if final is None:
            detail = "model stream ended without a terminal response"
            await self._record_attempt_failure(
                turn, context, model_call_id, detail, usage_payload or None
            )
            return _ModelStep(failure=_failed(turn, f"model call failed: {detail}"))

        streaming_mode = "native" if getattr(stream, "native", False) else "fallback"
        if not await self._persist(
            turn,
            context,
            EventType.MODEL_ATTEMPT_COMPLETED,
            _completed_payload(final, model_call_id, streaming_mode),
        ):
            return _ModelStep(
                failure=_failed(
                    turn,
                    "could not persist model.attempt.completed; no tool was executed",
                )
            )

        if turn.cancellation.cancelled:
            return _ModelStep(failure=_interrupted(turn))

        # The attempt itself succeeded, so it is recorded as completed with its
        # usage and reasoning. The turn still fails: truncated tool arguments
        # are not a usable answer and must never be executed.
        if final.finish_reason == "length" and final.has_tool_calls:
            return _ModelStep(
                failure=_failed(
                    turn,
                    "model output was truncated (finish_reason=length) with tool "
                    "calls; no tool was executed",
                )
            )
        return _ModelStep(response=final)

    async def _run_blocking_model(
        self,
        turn: TurnStart,
        context: ExecutionContext,
        request: ModelRequest,
    ) -> _ModelStep:
        attempt_task = asyncio.create_task(self._adapter.complete(request))
        cancel_task = asyncio.create_task(turn.cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                {attempt_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            # An external task cancellation is not a token cancellation and
            # must never be recorded as a completed model answer. Stop and reap
            # both local tasks before letting the cancellation propagate.
            attempt_task.cancel()
            cancel_task.cancel()
            await asyncio.gather(attempt_task, cancel_task, return_exceptions=True)
            raise
        cancel_task.cancel()
        if attempt_task not in done:
            # The token won the race: stop waiting locally and reap the adapter
            # task so no background model call is left running unreferenced.
            attempt_task.cancel()
        await asyncio.gather(attempt_task, cancel_task, return_exceptions=True)

        if attempt_task not in done:
            await self._persist(
                turn,
                context,
                EventType.MODEL_ATTEMPT_INTERRUPTED,
                {"model_call_id": request.model_call_id},
            )
            return _ModelStep(failure=_interrupted(turn))

        try:
            response = attempt_task.result()
        except OperationCancelledError:
            await self._record_attempt_failure(
                turn, context, request.model_call_id, "model call cancelled locally"
            )
            return _ModelStep(failure=_interrupted(turn))
        except ModelCallError as exc:
            detail = _describe(exc)
            await self._record_attempt_failure(
                turn, context, request.model_call_id, detail, exc.usage
            )
            return _ModelStep(
                failure=_failed(turn, f"model call failed: {detail}"),
                failure_code=exc.code,
                failure_usage=dict(exc.usage) or None,
            )
        except Exception as exc:
            detail = _describe(exc)
            await self._record_attempt_failure(
                turn, context, request.model_call_id, detail
            )
            return _ModelStep(failure=_failed(turn, f"model call failed: {detail}"))

        if not isinstance(response, ModelResponse):
            detail = (
                f"model adapter returned {type(response).__name__}, "
                "expected ModelResponse"
            )
            await self._record_attempt_failure(
                turn, context, request.model_call_id, detail
            )
            return _ModelStep(failure=_failed(turn, detail))

        if not await self._persist(
            turn,
            context,
            EventType.MODEL_ATTEMPT_COMPLETED,
            _completed_payload(response, request.model_call_id),
        ):
            return _ModelStep(
                failure=_failed(
                    turn,
                    "could not persist model.attempt.completed; no tool was executed",
                )
            )

        # A token request during the race stops the turn even though the real
        # answer was recorded above. No tool may run after a local stop.
        if turn.cancellation.cancelled:
            return _ModelStep(failure=_interrupted(turn))

        # The attempt itself succeeded, so it is recorded as completed with its
        # usage and reasoning. The turn still fails: truncated tool arguments
        # are not a usable answer and must never be executed.
        if response.finish_reason == "length" and response.has_tool_calls:
            return _ModelStep(
                failure=_failed(
                    turn,
                    "model output was truncated (finish_reason=length) with tool "
                    "calls; no tool was executed",
                )
            )
        return _ModelStep(response=response)

    async def _run_tools(
        self,
        turn: TurnStart,
        context: ExecutionContext,
        calls: Sequence[ToolCall],
        history: list[dict[str, Any]],
        *,
        budget: Any = None,
        reply: str | None = None,
    ) -> TurnResult | None:
        batch = getattr(self._executor, "execute_batch", None)
        if callable(batch):
            try:
                raw = await batch(
                    calls, context, max_parallel=self._max_parallel_tools
                )
                outcomes = tuple(raw)
            except OperationCancelledError:
                return _interrupted(turn)
            except Exception as exc:
                return _failed(
                    turn,
                    f"tool executor raised while running a batch: {_describe(exc)}",
                )
            if len(outcomes) != len(calls):
                return _failed(
                    turn,
                    f"tool executor returned {len(outcomes)} outcomes for "
                    f"{len(calls)} calls",
                )
            for call, outcome in zip(calls, outcomes):
                failure = self._check_tool_outcome(
                    turn, call, outcome, match_identity=True
                )
                if failure is not None:
                    return failure
            for call, outcome in zip(calls, outcomes):
                failure = self._observe_repetition(
                    turn, call, outcome, budget, reply
                )
                if failure is not None:
                    return failure
            for outcome in outcomes:
                history.append(_tool_message(outcome))
            return None

        for call in calls:
            if turn.cancellation.cancelled:
                return _interrupted(turn)
            try:
                outcome = await self._executor.execute(call, context)
            except OperationCancelledError:
                return _interrupted(turn)
            except Exception as exc:
                return _failed(
                    turn,
                    f"tool executor raised for {call.name}: {_describe(exc)}",
                )
            failure = self._check_tool_outcome(
                turn, call, outcome, match_identity=False
            )
            if failure is not None:
                return failure
            failure = self._observe_repetition(turn, call, outcome, budget, reply)
            if failure is not None:
                return failure
            history.append(_tool_message(outcome))
        return None

    def _check_tool_outcome(
        self,
        turn: TurnStart,
        call: ToolCall,
        outcome: Any,
        *,
        match_identity: bool,
    ) -> TurnResult | None:
        if not isinstance(outcome, ToolOutcome):
            return _failed(
                turn,
                f"tool executor returned {type(outcome).__name__} for "
                f"{call.name}, expected ToolOutcome",
            )
        if match_identity and (
            outcome.tool_call_id != call.tool_call_id
            or outcome.tool_name != call.name
        ):
            return _failed(
                turn,
                "tool executor returned an outcome for "
                f"{outcome.tool_name}/{outcome.tool_call_id} while settling "
                f"{call.name}/{call.tool_call_id}",
            )
        if outcome.status is ToolStatus.RUNNING:
            return _failed(
                turn,
                f"tool {call.name} is still running; an unfinished result "
                "cannot be shown to the model",
            )
        if (
            outcome.error is not None
            and outcome.error.code in CORE_PERSISTENCE_FAILURE_CODES
        ):
            # The executor could not durably record this call/result. Stop
            # here: a later model request would build on a missing record.
            return _failed(
                turn,
                f"tool {call.name} could not be recorded durably "
                f"({outcome.error.code}); the turn cannot continue",
            )
        return None

    def _observe_repetition(
        self,
        turn: TurnStart,
        call: ToolCall,
        outcome: ToolOutcome,
        budget: Any,
        reply: str | None,
    ) -> TurnResult | None:
        if budget is None:
            return None
        # Use the provider's raw payload as the identity when it exists: the
        # executor validates that payload, not the adapter's earlier mapping.
        arguments: Mapping[str, Any]
        if call.raw_arguments is not None:
            arguments = {"raw_arguments": call.raw_arguments}
        else:
            arguments = call.arguments
        if budget.observe_tool_outcome(
            call.name, arguments, outcome.status.value, outcome.text
        ):
            return _failed_with_reply(
                turn,
                f"no progress: tool {call.name} repeated the same arguments and "
                "the same result up to the configured limit; the turn was stopped",
                reply,
            )
        return None

    def _begin_turn_budget(self, turn: TurnStart) -> Any:
        """Build one tracker for this run; a shared config is never mutated."""
        budget = self._turn_budget
        if budget is None:
            return None
        begin = getattr(budget, "begin_turn", None)
        tracker = begin() if callable(begin) else budget
        registry = getattr(budget, "registry", None)
        if registry is not None:
            registry.register(turn.request.session_id, turn.turn_id, tracker)
        for name in (
            "record_usage",
            "token_budget_exhausted",
            "is_summary_step",
            "observe_tool_outcome",
        ):
            if not callable(getattr(tracker, name, None)):
                raise TypeError(
                    f"turn_budget must provide {name}() (TurnBudget or a tracker)"
                )
        return tracker

    async def _run_summary_step(
        self,
        turn: TurnStart,
        context: ExecutionContext,
        history: list[dict[str, Any]],
        budget: Any,
        *,
        bundle: PromptBundle | None = None,
    ) -> TurnResult:
        """Spend the reserved final step on a tools-hidden progress summary.

        The request has no tool definitions and ``tool_choice="none"``, so the
        effective policy for this step allows nothing; any tool call that still
        appears is not executed. The turn stays FAILED: a summary is partial
        progress, never a completed task.
        """
        step = await self._run_model_step(
            turn, context, history, summary=True, bundle=bundle
        )
        if step.failure is not None:
            if step.failure_usage:
                budget.record_usage(step.failure_usage)
            return step.failure
        response = step.response
        if response is None:  # pragma: no cover - guarded by _ModelStep
            return _failed(turn, "summary step ended without a response")
        budget.record_usage(response.usage)
        if response.tool_calls:
            return _failed_with_reply(
                turn,
                "step budget was exhausted; the tools-hidden summary request "
                "returned tool calls, which were not executed",
                response.content,
            )
        return _failed_with_reply(
            turn,
            f"step budget exhausted after {turn.budget.max_steps} steps; "
            "a summary was returned instead of a completed task",
            response.content,
        )

    async def _apply_steering(
        self,
        turn: TurnStart,
        context: ExecutionContext,
        history: list[dict[str, Any]],
    ) -> TurnResult | None:
        """Read corrections only at a safe step boundary.

        This runs before the step's model request, never during a stream or a
        tool batch, and it neither cancels nor resumes dispatched work. Each
        text is persisted before it becomes a user message.
        """
        if self._steering is None:
            return None
        try:
            texts = tuple(
                self._steering.drain(turn.request.session_id, turn.turn_id)
            )
        except OperationCancelledError:
            return _interrupted(turn)
        except Exception as exc:
            return _failed(turn, f"steering drain failed: {_describe(exc)}")
        if not texts:
            return None
        event_type = getattr(EventType, "INPUT_STEERED", None)
        if event_type is None:
            return _failed(
                turn,
                "steering has a correction but input.steered cannot be recorded",
            )
        for text in texts:
            if not isinstance(text, str) or not text.strip():
                return _failed(turn, "steering returned an empty non-string text")
            if not await self._persist(turn, context, event_type, {"text": text}):
                return _failed(
                    turn,
                    "could not persist input.steered; the model was not called",
                )
            history.append({"role": "user", "content": text})
        return None

    async def _prepare_context(
        self,
        turn: TurnStart,
        context: ExecutionContext,
        history: list[dict[str, Any]],
        *,
        bundle: PromptBundle,
        force: bool,
        budget: Any = None,
    ) -> tuple[PromptBundle, bool, TurnResult | None]:
        """Apply the optional context policy to pure history.

        The policy sees the assembler-visible tools and, when its ``prepare``
        accepts it, the real request messages (including live fragments). It
        returns replacement *history* messages only; the driver never stores
        the assembled request tail in internal history. A real reduction forces
        one re-assembly so the next request is built from the reduced history.
        """
        if self._context_policy is None:
            return bundle, False, None
        kwargs: dict[str, Any] = {}
        if self._policy_accepts_request_messages():
            kwargs["request_messages"] = bundle.messages
        if self._policy_accepts_parameter("protected_user_text"):
            kwargs["protected_user_text"] = turn.request.text
        prepare_task = asyncio.create_task(
            self._context_policy.prepare(
                context,
                tuple(history),
                bundle.tools,
                force=force,
                **kwargs,
            ),
            name=f"context-prepare:{context.step_id}",
        )
        cancel_task = asyncio.create_task(
            turn.cancellation.wait(), name=f"context-cancel:{context.step_id}"
        )
        try:
            try:
                done, _ = await asyncio.wait(
                    {prepare_task, cancel_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                # An external task cancellation is not a token cancellation.
                # Reap both local tasks before letting it propagate.
                prepare_task.cancel()
                cancel_task.cancel()
                await asyncio.gather(
                    prepare_task, cancel_task, return_exceptions=True
                )
                raise
            cancel_task.cancel()
            if prepare_task not in done:
                # A local stop ends the wait. The summary call is model-only,
                # so cancelling it cannot undo or interrupt a dispatched tool.
                prepare_task.cancel()
            await asyncio.gather(prepare_task, cancel_task, return_exceptions=True)
            if turn.cancellation.cancelled:
                # Cancel wins even when the preparation settled in the same
                # loop turn; no normal or retry model call may start after it.
                return bundle, False, _interrupted(turn)
            try:
                reduction = prepare_task.result()
            except asyncio.CancelledError:
                return bundle, False, _interrupted(turn)
            except OperationCancelledError:
                return bundle, False, _interrupted(turn)
            except Exception as exc:
                return bundle, False, _failed(
                    turn, f"context policy failed: {_describe(exc)}"
                )
        finally:
            # A summary attempt may have spent tokens before succeeding,
            # failing, or being cancelled. Drain exactly what the policy
            # observed into this turn's tracker, never as an invented zero.
            self._record_policy_usage(budget)
        if reduction is None:
            return bundle, False, None
        fields = self._reduction_fields(reduction)
        if fields is None:
            return bundle, False, _failed(
                turn,
                "context policy returned an invalid reduction; the model was not called",
            )
        messages, before_tokens, after_tokens, reason = fields
        current = [copy.deepcopy(dict(message)) for message in history]
        reduced = messages != current
        if force:
            # A forced recovery is only useful when the measured next request
            # actually shrank; otherwise the same over-window call is retried.
            reduced = reduced and after_tokens < before_tokens
        if not reduced:
            return bundle, False, None
        if not await self._persist_turn_scoped(
            turn,
            EventType.CONTEXT_COMPACTED,
            {
                "before_tokens": before_tokens,
                "after_tokens": after_tokens,
                "reason": reason,
                "messages": messages,
            },
        ):
            return bundle, False, _failed(
                turn,
                "could not persist context.compacted; the model was not called",
            )
        history[:] = messages
        reduced_bundle, assemble_failure = await self._assemble_bundle(
            turn, context, history
        )
        if assemble_failure is not None:
            return bundle, False, assemble_failure
        return reduced_bundle, True, None

    def _record_policy_usage(self, budget: Any) -> None:
        """Record provider usage the optional policy drained for a summary.

        The policy owns the summary call and its attempt bookkeeping; it exposes
        only the usage it actually observed. An empty drain means no call was
        made, and a mapping with missing fields stays usage-unknown rather than
        becoming zero. Drain happens even without a tracker so stale records
        cannot leak into a later turn, and accounting failure never rewrites the
        turn's outcome.
        """
        if self._context_policy is None:
            return
        drain = getattr(self._context_policy, "drain_usage", None)
        if not callable(drain):
            return
        try:
            drained = drain()
            if drained is None:
                return
            for usage in drained:
                if budget is not None:
                    budget.record_usage(usage)
        except Exception:
            # Bookkeeping must not rewrite a turn that already succeeded or
            # failed for a real reason.
            return

    def _policy_accepts_request_messages(self) -> bool:
        return self._policy_accepts_parameter("request_messages")

    def _policy_accepts_parameter(self, name: str) -> bool:
        prepare = getattr(self._context_policy, "prepare", None)
        if prepare is None:
            return False
        try:
            parameters = inspect.signature(prepare).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(
            parameter.name == name
            or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )

    @staticmethod
    def _reduction_fields(
        reduction: Any,
    ) -> tuple[list[dict[str, Any]], int, int, str] | None:
        try:
            messages = [
                copy.deepcopy(dict(message))
                for message in getattr(reduction, "messages")
            ]
        except (AttributeError, TypeError, ValueError):
            return None
        if not messages:
            return None
        before = getattr(reduction, "before_tokens", None)
        after = getattr(reduction, "after_tokens", None)
        for value in (before, after):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                return None
        reason = getattr(reduction, "reason", None)
        if not isinstance(reason, str) or not reason.strip():
            reason = "context_policy"
        return messages, before, after, reason

    async def _record_attempt_failure(
        self,
        turn: TurnStart,
        context: ExecutionContext,
        model_call_id: str,
        error: str,
        usage: Mapping[str, int] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"model_call_id": model_call_id, "error": error}
        if usage:
            payload["usage"] = dict(usage)
        await self._persist(
            turn,
            context,
            EventType.MODEL_ATTEMPT_FAILED,
            payload,
        )

    async def _persist_delta(
        self,
        turn: TurnStart,
        context: ExecutionContext,
        model_call_id: str,
        content: str,
    ) -> bool:
        if not content:
            return True
        return await self._persist(
            turn,
            context,
            EventType.MODEL_RESPONSE_DELTA,
            {"model_call_id": model_call_id, "content": content},
        )

    async def _persist_turn_scoped(
        self,
        turn: TurnStart,
        event_type: EventType,
        payload: Mapping[str, Any],
    ) -> bool:
        """Append one driver-owned turn-scoped event (no ``step_id``)."""
        event = RuntimeEvent(
            type=event_type,
            session_id=turn.request.session_id,
            turn_id=turn.turn_id,
            payload=payload,
        )
        try:
            await self._store.append(event)
        except Exception:
            return False
        return True

    async def _persist(
        self,
        turn: TurnStart,
        context: ExecutionContext,
        event_type: EventType,
        payload: Mapping[str, Any],
    ) -> bool:
        """Append one driver-owned event; a failed write is reported as False."""
        event = RuntimeEvent(
            type=event_type,
            session_id=turn.request.session_id,
            turn_id=turn.turn_id,
            step_id=context.step_id,
            payload=payload,
        )
        try:
            await self._store.append(event)
        except Exception:
            return False
        return True
