"""Expose image analysis/display/generation to the Core as a native capability.

The reusable implementation lives in :mod:`PhyAgentOS.tooling.image`; this
adapter adds only Core-specific wiring:

* the provider is resolved for the current ``ExecutionContext.session_id`` on
  every call, so a later selection change is never masked by a provider
  captured at composition time;
* display routing comes from the turn's frozen :class:`TurnBinding`, never a
  shared ``ContextVar``;
* failures become ``ToolStatus.FAILED`` outcomes instead of looking like a
  successful tool string;
* in-flight work is tracked per session so ``cancel``/``aclose`` stop local
  work and let HTTP clients unwind.

Display has no default target here. Without a delivery consumer the tool says
so; it never claims an image was shown when nothing was sent.
"""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from PhyAgentOS.agent.core.types import (
    ExecutionContext,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolOutcome,
    ToolRegistration,
    ToolStatus,
)
from PhyAgentOS.agent.services.background_runtime import TurnRoutes
from PhyAgentOS.bus.events import OutboundMessage
from PhyAgentOS.config.schema import Config
from PhyAgentOS.tooling.base import SessionTaskRegistry
from PhyAgentOS.tooling.image import ImageCallContext, ImageTool
from PhyAgentOS.tooling.tool_results import ToolResultLimiter

__all__ = ["ImageCapability", "IMAGE_TOOL"]

_SCHEMA_TOOL = ImageTool(provider=None)
IMAGE_TOOL = ToolDefinition(
    name=_SCHEMA_TOOL.name,
    description=_SCHEMA_TOOL.description,
    parameters=copy.deepcopy(_SCHEMA_TOOL.parameters),
)

_FAILED_CODES = {
    "vision": "image_vision_failed",
    "display": "image_display_failed",
    "generate": "image_generate_failed",
}


class ImageCapability:
    """Native Core ``image`` tool over the shared image implementation."""

    def __init__(
        self,
        config: Config,
        *,
        provider_resolver: Callable[[str], Any],
        routes: TurnRoutes | None = None,
        send: Callable[[OutboundMessage], Awaitable[None]] | None = None,
    ) -> None:
        self.config = config
        self._provider_resolver = provider_resolver
        self._routes = routes
        self._send = send
        self._workspace = Path(config.workspace_path)
        self._restrict_to_workspace = config.tools.restrict_to_workspace
        self._limiter = ToolResultLimiter(self._workspace)
        self._tasks = SessionTaskRegistry()
        self._tool = ImageTool(provider=None)

    @property
    def name(self) -> str:
        return "image"

    def tool_registrations(self) -> tuple[ToolRegistration, ...]:
        return (
            ToolRegistration(definition=IMAGE_TOOL, handler=self._handle),
        )

    def prompt_context(self, context: ExecutionContext) -> str | None:
        return None

    async def cancel(
        self, session_id: str, context: ExecutionContext | None
    ) -> None:
        # Cancellation stops only local waits. A generation POST already sent
        # to the provider is not claimed to have been stopped remotely.
        del context
        await self._tasks.cancel_session(session_id)

    async def aclose(self) -> None:
        await self._tasks.aclose()

    async def _handle(self, call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        arguments = call.arguments_dict()
        mode = str(arguments.get("mode", ""))
        try:
            with self._tasks.scope(context.session_id):
                result = await self._tool.execute(
                    mode=mode,
                    text=arguments.get("text", ""),
                    image_path=arguments.get("image_path", ""),
                    size=arguments.get("size", "1024*1024"),
                    negative_prompt=arguments.get("negative_prompt", ""),
                    n=arguments.get("n", 1),
                    prompt_extend=arguments.get("prompt_extend", True),
                    watermark=arguments.get("watermark", False),
                    call_context=self._call_context(mode, context),
                )
        except Exception as exc:
            return self._failed(
                call, mode, f"Error: image tool failed: {exc}"
            )
        if result.startswith("Error"):
            return self._failed(call, mode, result)
        if mode == "vision":
            # Bound model-facing text only. Image bytes were already handled
            # above and are never truncated by this limiter.
            result = self._limiter.limit(call.tool_call_id, result)
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.SUCCEEDED,
            text=result,
        )

    def _call_context(self, mode: str, context: ExecutionContext) -> ImageCallContext:
        provider: Any | None = None
        model: str | None = None
        if mode == "vision":
            selection = self._provider_resolver(context.session_id)
            if selection is not None:
                provider = getattr(selection, "provider", selection)
                model = getattr(selection, "model", None)

        channel = ""
        chat_id = ""
        message_id: str | None = None
        metadata: dict[str, Any] = {}
        reply_to: str | None = None
        if self._routes is not None:
            binding = self._routes.get(context.session_id)
            if binding is not None:
                channel = binding.channel
                chat_id = binding.chat_id
                metadata = dict(binding.metadata)
                message_id = metadata.get("message_id")
                reply_to = binding.reply_to

        return ImageCallContext(
            provider=provider,
            model=model,
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            send=self._send,
            channel_media_paths=True,
            metadata=metadata,
            reply_to=reply_to,
            workspace=self._workspace,
            restrict_to_workspace=self._restrict_to_workspace,
        )

    @staticmethod
    def _failed(call: ToolCall, mode: str, text: str) -> ToolOutcome:
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.FAILED,
            text=text,
            error=ToolError(
                _FAILED_CODES.get(mode, "image_failed"),
                text.removeprefix("Error: ").strip() or "image tool failed",
            ),
        )
