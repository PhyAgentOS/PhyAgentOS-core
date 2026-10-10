"""Gateway lease announcer for an embedded ToolEndpoint node.

A compact state machine over the forge_tool wire primitives: announce the
endpoint, renew at lease_ttl/3, retry with capped exponential backoff, and
unregister best-effort on shutdown. Transport stays with the caller.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Callable
from typing import Literal

from forge_tool import (
    EndpointRegistryResponse,
    ToolEndpointDescriptor,
    ToolEnvelope,
    ToolError,
    endpoint_registry_response_from_payload,
    make_registration_envelope,
    make_unregister_envelope,
    validate_management_response_correlation,
)

LeaseState = Literal["inactive", "announcing", "active", "retrying", "failed", "stopped"]


class GatewayLeaseAnnouncer:
    """Announce and renew one Gateway lease; no transport, no threads."""

    def __init__(
        self,
        descriptor: ToolEndpointDescriptor,
        *,
        endpoint_instance_id: str,
        response_timeout_ms: int = 500,
        initial_backoff_ms: int = 100,
        max_backoff_ms: int = 1_000,
        request_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        if initial_backoff_ms > max_backoff_ms:
            raise ValueError("initial_backoff_ms must not exceed max_backoff_ms")
        self._descriptor = descriptor
        self._endpoint_instance_id = endpoint_instance_id
        self._response_timeout_s = response_timeout_ms / 1_000
        self._initial_backoff_ms = initial_backoff_ms
        self._max_backoff_ms = max_backoff_ms
        self._request_id_factory = request_id_factory

        self._state: LeaseState = "inactive"
        self._pending: ToolEnvelope | None = None
        self._response_deadline: float | None = None
        self._next_announce_at = 0.0
        self._next_retry_at = 0.0
        self._backoff_ms = float(initial_backoff_ms)
        self._last_error: ToolError | None = None
        self._announcement_attempted = False

    @property
    def state(self) -> LeaseState:
        return self._state

    @property
    def last_error(self) -> ToolError | None:
        return self._last_error

    def poll(self, now: float) -> ToolEnvelope | None:
        """Return the register request to send now, if any."""
        if self._state in ("failed", "stopped"):
            return None
        if self._pending is not None:
            assert self._response_deadline is not None
            if now < self._response_deadline:
                return None
            self._pending = None
            self._response_deadline = None
            self._schedule_retry(
                now,
                ToolError(
                    code="FORGE_ENDPOINT_REGISTRY_TIMEOUT",
                    message="the Gateway did not acknowledge endpoint register",
                    retryable=True,
                ),
            )
            return None
        if self._state == "active" and now < self._next_announce_at:
            return None
        if self._state == "retrying" and now < self._next_retry_at:
            return None
        request = make_registration_envelope(
            self._descriptor,
            endpoint_instance_id=self._endpoint_instance_id,
            request_id=self._request_id_factory(),
        )
        self._announcement_attempted = True
        self._pending = request
        self._response_deadline = now + self._response_timeout_s
        self._state = "announcing"
        return request

    def send_failed(self, request: ToolEnvelope, *, now: float, error: Exception) -> bool:
        """Record an ambiguous register send failure and schedule a retry."""
        if request.message_type != "endpoint.register" or request != self._pending:
            return False
        self._pending = None
        self._response_deadline = None
        self._schedule_retry(
            now,
            ToolError(
                code="FORGE_ENDPOINT_REGISTRY_SEND_FAILED",
                message="failed to send endpoint register to Gateway",
                retryable=True,
                details={"error_type": type(error).__name__},
            ),
        )
        self._next_retry_at = now
        return True

    def handle_response(self, response: ToolEnvelope, *, now: float) -> bool:
        """Consume a Gateway registry response; True when it matched a pending request."""
        pending = self._pending
        if pending is None or response.request_id != pending.request_id:
            return False
        assert self._response_deadline is not None
        if now >= self._response_deadline:
            self._pending = None
            self._response_deadline = None
            self._schedule_retry(
                now,
                ToolError(
                    code="FORGE_ENDPOINT_REGISTRY_TIMEOUT",
                    message="the Gateway acknowledged endpoint register too late",
                    retryable=True,
                ),
            )
            return False
        validate_management_response_correlation(pending, response)
        decision: EndpointRegistryResponse = endpoint_registry_response_from_payload(response.payload)
        self._pending = None
        self._response_deadline = None
        if decision.status == "accepted":
            assert decision.lease_ttl_ms is not None
            lease_s = decision.lease_ttl_ms / 1_000
            self._next_announce_at = now + lease_s / 3
            self._next_retry_at = 0.0
            self._backoff_ms = float(self._initial_backoff_ms)
            self._last_error = None
            self._state = "active"
        elif decision.error is not None and decision.error.retryable:
            self._schedule_retry(now, decision.error)
        elif decision.error is not None:
            self._last_error = decision.error
            self._state = "failed"
        return True

    def stop(self, now: float) -> ToolEnvelope | None:
        """Enter the stopped state and return a best-effort unregister request."""
        self._pending = None
        self._response_deadline = None
        self._state = "stopped"
        if not self._announcement_attempted:
            return None
        self._announcement_attempted = False
        return make_unregister_envelope(
            endpoint_id=self._descriptor.endpoint_id,
            endpoint_instance_id=self._endpoint_instance_id,
            request_id=self._request_id_factory(),
        )

    def _schedule_retry(self, now: float, error: ToolError) -> None:
        cap = min(self._backoff_ms, float(self._max_backoff_ms))
        self._next_retry_at = now + random.uniform(0.0, cap) / 1_000
        self._backoff_ms = min(float(self._max_backoff_ms), self._backoff_ms * 2)
        self._last_error = error
        self._state = "retrying"


__all__ = ["GatewayLeaseAnnouncer", "LeaseState"]
