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
from dataclasses import dataclass
from typing import Any

from .events import EventType, RuntimeEvent
from .protocols import ModelAdapter, PromptAssembler, SessionStore, ToolExecutor
from .types import (
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
    ToolStatus,
    TurnResult,
    TurnStart,
    TurnStatus,
)

__all__ = ["LoopDriverImpl"]

# Executor persistence failures are not tool answers. The call either never
# ran (intent/dispatched write failed) or its result could not be stored
# (settled write failed), so the turn's record is already incomplete. Showing
# these to the model like an ordinary handler failure would let it "recover"
# and complete a turn the core cannot durably explain. Keep the set here so
# the condition stays one membership check instead of scattered strings.
_CORE_PERSISTENCE_FAILURE_CODES = frozenset(
    {
        "tool_intent_write_failed",
        "tool_dispatched_write_failed",
        "tool_settled_write_failed",
    }
)

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
    ) -> None:
        frozen_tools = tuple(tools)
        for tool in frozen_tools:
            if not isinstance(tool, ToolDefinition):
                raise TypeError("tools must contain ToolDefinition instances")
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

    async def run(self, turn: TurnStart) -> TurnResult:
        # Deep copy so the turn's frozen history and the mutable provider
        # messages cannot alias each other.
        history: list[dict[str, Any]] = [
            copy.deepcopy(dict(message)) for message in turn.history
        ]
        history.append({"role": "user", "content": turn.request.text})
        for step_number in range(1, turn.budget.max_steps + 1):
            stop = _local_stop(turn)
            if stop is not None:
                return stop
            context = turn.step_context(f"{turn.turn_id}:step:{step_number}")
            step = await self._run_model_step(turn, context, history)
            if step.failure is not None:
                return step.failure
            response = step.response
            if response is None:  # pragma: no cover - guarded by _ModelStep
                return _failed(turn, "model step ended without a response")
            history.append(_assistant_message(response))
            if not response.tool_calls:
                return _result(turn, TurnStatus.COMPLETED, reply=response.content)
            stop = await self._run_tools(turn, context, response.tool_calls, history)
            if stop is not None:
                return stop
            # A cancel requested while tools ran must not trigger another call.
            stop = _local_stop(turn)
            if stop is not None:
                return stop
        return _failed(
            turn, f"step budget exhausted after {turn.budget.max_steps} steps"
        )

    async def _run_model_step(
        self,
        turn: TurnStart,
        context: ExecutionContext,
        history: list[dict[str, Any]],
    ) -> _ModelStep:
        try:
            bundle = await self._assembler.assemble(
                context, history=tuple(history), tools=self._tools
            )
        except OperationCancelledError:
            return _ModelStep(failure=_interrupted(turn))
        except Exception as exc:
            return _ModelStep(
                failure=_failed(turn, f"prompt assembly failed: {_describe(exc)}")
            )
        if not isinstance(bundle, PromptBundle):
            return _ModelStep(
                failure=_failed(
                    turn,
                    "prompt assembler returned "
                    f"{type(bundle).__name__}, expected PromptBundle",
                )
            )

        request = ModelRequest(
            model_call_id=f"{context.step_id}:model",
            messages=bundle.messages,
            tools=bundle.tools,
            model=self._model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            reasoning_effort=self._reasoning_effort,
            tool_choice=self._tool_choice,
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
            return await self._run_streaming_model(
                turn, context, request, stream_factory
            )
        return await self._run_blocking_model(turn, context, request)

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
            return _ModelStep(failure=_failed(turn, f"model call failed: {detail}"))
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
            return _ModelStep(failure=_failed(turn, f"model call failed: {detail}"))

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
            return _ModelStep(failure=_failed(turn, f"model call failed: {detail}"))
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
    ) -> TurnResult | None:
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
            if not isinstance(outcome, ToolOutcome):
                return _failed(
                    turn,
                    f"tool executor returned {type(outcome).__name__} for "
                    f"{call.name}, expected ToolOutcome",
                )
            if outcome.status is ToolStatus.RUNNING:
                return _failed(
                    turn,
                    f"tool {call.name} is still running; an unfinished result "
                    "cannot be shown to the model",
                )
            if (
                outcome.error is not None
                and outcome.error.code in _CORE_PERSISTENCE_FAILURE_CODES
            ):
                # The executor could not durably record this call/result. Stop
                # here: a later model request would build on a missing record.
                return _failed(
                    turn,
                    f"tool {call.name} could not be recorded durably "
                    f"({outcome.error.code}); the turn cannot continue",
                )
            history.append(_tool_message(outcome))
        return None

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
