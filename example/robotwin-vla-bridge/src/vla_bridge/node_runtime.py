"""Dora glue for the vla_bridge ToolEndpoint (vla.set_mode / vla.get_status).

Same responsibilities as the image_vision runtime: the endpoint binding, the
Gateway lease announcer (driven on every event), tool_in routing and tool_out
publishing. Nothing here is vla-specific.
"""

from __future__ import annotations

import time
from typing import Any

import pyarrow as pa
from forge_common import get_logger
from forge_msgs import ToolMessage, ToolMessageSizeError
from forge_tool import ToolProtocolError
from forge_tool.dora import DoraToolEndpointBinding, tool_envelope_to_message, tool_message_to_envelope

from .common import RegistrationConfig
from .registration import GatewayLeaseAnnouncer

logger = get_logger(__name__)

TOOL_INPUT_ID = "tool_in"
TOOL_OUTPUT_ID = "tool_out"


class ToolEndpointRuntime:
    """Drive one ToolEndpointBinding and its Gateway lease on a dora node."""

    def __init__(
        self,
        node: Any,
        binding: DoraToolEndpointBinding,
        *,
        registration: RegistrationConfig,
        tag: str,
        endpoint_instance_id: str,
    ) -> None:
        self._node = node
        self._binding = binding
        self._tag = tag
        self._announcer = (
            GatewayLeaseAnnouncer(
                binding.handler.descriptor,
                endpoint_instance_id=endpoint_instance_id,
                response_timeout_ms=registration.response_timeout_ms,
                initial_backoff_ms=registration.initial_backoff_ms,
                max_backoff_ms=registration.max_backoff_ms,
            )
            if registration.enabled
            else None
        )
        self._async_runner: Any = None

    @property
    def announcer_state(self) -> str | None:
        return None if self._announcer is None else str(self._announcer.state)

    def send_envelope(self, envelope: Any) -> None:
        message = tool_envelope_to_message(envelope, max_message_bytes=self._binding.max_message_bytes)
        self._node.send_output(TOOL_OUTPUT_ID, message.to_arrow())

    def drive_lease(self) -> None:
        if self._announcer is None:
            return
        now = time.monotonic()
        request = self._announcer.poll(now)
        if request is None:
            return
        try:
            self.send_envelope(request)
        except Exception as exc:  # noqa: BLE001 - transport-level failure is retryable
            self._announcer.send_failed(request, now=now, error=exc)
            logger.warning("[%s] register send failed: %s", self._tag, exc)

    def handle_tool_input(self, value: object) -> None:
        """Route one tool_in arrow: registry response or invoke request."""
        if not isinstance(value, (pa.RecordBatch, pa.Table, pa.StructArray)):
            logger.warning("[%s] tool_in requires an in-memory Arrow carrier", self._tag)
            return
        try:
            message = ToolMessage.from_arrow(value, max_payload_json_bytes=self._binding.max_message_bytes)
        except (ToolMessageSizeError, TypeError, ValueError) as exc:
            logger.warning("[%s] rejected tool_in message: %s", self._tag, exc)
            return
        if message.message_type == "endpoint.registry.response":
            self._handle_registry_response(message)
        elif message.message_type == "tool.invoke.request":
            self._handle_tool_request(value)
        else:
            logger.warning("[%s] unsupported tool_in type=%s", self._tag, message.message_type)

    def _handle_registry_response(self, message: ToolMessage) -> None:
        if self._announcer is None:
            logger.warning("[%s] registry response while registration disabled", self._tag)
            return
        try:
            envelope = tool_message_to_envelope(message, max_message_bytes=self._binding.max_message_bytes)
            handled = self._announcer.handle_response(envelope, now=time.monotonic())
        except (ToolProtocolError, TypeError, ValueError) as exc:
            logger.warning("[%s] rejected registry response: %s", self._tag, exc)
            return
        if handled:
            logger.info("[%s] gateway lease state=%s", self._tag, self._announcer.state)

    def _handle_tool_request(self, value: object) -> None:
        if self._async_runner is None:
            import asyncio

            self._async_runner = asyncio.Runner()
        try:
            response = self._async_runner.run(self._binding.handle_input(value))
        except ToolProtocolError as exc:
            logger.warning("[%s] unroutable tool request code=%s: %s", self._tag, exc.code, exc.message)
            return
        except (TypeError, ValueError) as exc:
            logger.warning("[%s] invalid tool request: %s", self._tag, exc)
            return
        except Exception:
            logger.exception("[%s] tool request handling failed", self._tag)
            return
        self._node.send_output(TOOL_OUTPUT_ID, response)

    def shutdown(self) -> None:
        if self._announcer is not None:
            request = self._announcer.stop(time.monotonic())
            if request is not None:
                try:
                    self.send_envelope(request)
                except Exception:
                    logger.exception("[%s] best-effort unregister failed", self._tag)
        if self._async_runner is not None:
            self._async_runner.close()
            self._async_runner = None


__all__ = ["TOOL_INPUT_ID", "TOOL_OUTPUT_ID", "ToolEndpointRuntime"]
