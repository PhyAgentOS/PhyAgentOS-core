"""Entry-facing host over an injected runtime and capabilities.

The host is a thin coordination layer, not a second entry implementation. It
forwards turns to the runtime exactly once, forwards session cancellation to
the registered capabilities, and owns only the shutdown order. Every
dependency arrives already built (see
:class:`~PhyAgentOS.agent.core.protocols.AgentHost`): this module reads no
config or environment and constructs no provider, store, Forge client, or
model loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from .protocols import AgentRuntime, Capability
from .types import ExecutionContext, TurnRequest, TurnResult

__all__ = ["AgentHostCloseError", "AgentHostImpl"]


class AgentHostCloseError(RuntimeError):
    """Shutdown finished with at least one failed step.

    Every remaining shutdown step was still attempted. ``failures`` keeps the
    original exceptions in execution order (``label, exception`` pairs are
    rendered into the message); the first exception is chained as the cause so
    no failure is swallowed.
    """

    def __init__(self, failures: Sequence[tuple[str, Exception]]) -> None:
        self.failures: tuple[Exception, ...] = tuple(exception for _, exception in failures)
        self.operations: tuple[str, ...] = tuple(label for label, _ in failures)
        details = "; ".join(f"{label}: {exception}" for label, exception in failures)
        super().__init__(f"agent host shutdown failed ({len(failures)} step(s)): {details}")


class AgentHostImpl:
    """Concrete :class:`~PhyAgentOS.agent.core.protocols.AgentHost`.

    The constructor only keeps the injected references and the capability
    order; it opens nothing and starts no background work. Every session that
    accepted a submit is remembered for shutdown, because a capability may
    still own remote work for a session whose local turn already finished.
    """

    def __init__(self, runtime: AgentRuntime, capabilities: Sequence[Capability]) -> None:
        self._runtime = runtime
        # Snapshot the order once: cancellation and shutdown must keep the
        # registration order even if the caller mutates its own list later.
        self._capabilities = tuple(capabilities)
        # Insertion-ordered set: shutdown visits accepted sessions in the order
        # they were first seen and cancels each session exactly once, even
        # when several turns were submitted for it. A finished turn is not
        # removed, because the capability may still need the stop request.
        self._accepted_sessions: dict[str, None] = {}
        self._closed = False
        # Serializes concurrent aclose calls and makes the second caller wait
        # for the first shutdown instead of returning while work is pending.
        self._close_lock = asyncio.Lock()

    async def submit(self, request: TurnRequest) -> TurnResult:
        """Delegate one turn to the runtime and return its result unchanged.

        The session is recorded before awaiting the runtime so a shutdown that
        starts while the turn is in flight still knows about it. Recording
        never removes an earlier entry: shutdown must ask capabilities to stop
        for sessions whose local turn has already completed.
        """
        if self._closed:
            raise RuntimeError("agent host is closed")
        self._accepted_sessions.setdefault(request.session_id, None)
        return await self._runtime.submit(request)

    async def cancel(self, session_id: str) -> None:
        """Ask the runtime to stop a session, then inform each capability.

        ``runtime.cancel`` returns the interrupted turn's context, or ``None``
        when no turn was active or the turn did not end as interrupted. Either
        way every capability receives ``cancel(session_id, context)`` in
        registration order, because a completed session may still own a
        running remote task. Returning from this method means the stop request
        was delivered, not that remote work stopped; the host makes no such
        claim.
        """
        context: ExecutionContext | None = await self._runtime.cancel(session_id)
        for capability in self._capabilities:
            await capability.cancel(session_id, context)

    async def aclose(self) -> None:
        """Shut down runtime and capabilities in the fixed contract order.

        Steps: reject new input, ``await runtime.stop()`` (cancels the active
        turn and waits for every accepted submit, queued turns included, while
        the store stays open), ask every capability to stop for every accepted
        session with ``cancel(session_id, None)``, close capabilities in
        registration order, then ``runtime.aclose()``, which closes the store.
        Per-session ``runtime.cancel`` is deliberately not used here: ``stop``
        already settled all local turns, and a queued turn must not start after
        capabilities are closed. Idempotent: later calls do nothing. A failing
        step is recorded and the remaining steps still run; at the end a
        single :class:`AgentHostCloseError` reports them.
        """
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True

            failures: list[tuple[str, Exception]] = []

            # One runtime-level stop instead of one cancel per session: it
            # also drains turns that were still queued behind an active one,
            # which a per-session cancel could not cover. On failure the
            # capabilities are still notified below so remote work is not
            # skipped because local settlement failed.
            try:
                await self._runtime.stop()
            except Exception as exc:  # recorded, not swallowed
                failures.append(("runtime.stop", exc))

            # Every accepted session gets a stop request, including sessions
            # whose local turn already finished. No context is passed: stop
            # has settled all local turns, so there is no interrupted turn to
            # report and the capability owns its domain state by session id.
            for session_id in self._accepted_sessions:
                for index, capability in enumerate(self._capabilities):
                    try:
                        await capability.cancel(session_id, None)
                    except Exception as exc:  # keep stopping others
                        failures.append((f"capability[{index}].cancel", exc))

            for index, capability in enumerate(self._capabilities):
                try:
                    await capability.aclose()
                except Exception as exc:  # keep closing others
                    failures.append((f"capability[{index}].aclose", exc))

            # The runtime owns the store, so it is closed last even when a
            # capability failed; otherwise the terminal turn events would
            # never reach a closed store.
            try:
                await self._runtime.aclose()
            except Exception as exc:  # surfaced below
                failures.append(("runtime.aclose", exc))

            if failures:
                raise AgentHostCloseError(failures) from failures[0][1]
