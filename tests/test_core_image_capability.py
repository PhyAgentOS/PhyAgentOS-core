"""Core image capability: current selection, routing isolation, explicit failures."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from PhyAgentOS.agent.capabilities.image_capability import IMAGE_TOOL, ImageCapability
from PhyAgentOS.agent.core.types import (
    Budget,
    CancellationToken,
    ExecutionContext,
    ToolCall,
    ToolStatus,
)
from PhyAgentOS.agent.services.background_runtime import TurnBinding, TurnRoutes
from PhyAgentOS.agent_legacy.tools.image import ImageTool as LegacyImageTool
from PhyAgentOS.config.schema import Config
from PhyAgentOS.tooling.image import ImageTool


class RecordingVisionProvider:
    def __init__(self, answer: str | None) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    async def chat_with_retry(self, **kwargs: Any):
        self.calls.append(kwargs)
        return SimpleNamespace(content=self.answer)


class FailingVisionProvider:
    async def chat_with_retry(self, **kwargs: Any):
        raise RuntimeError("vision endpoint refused")


def _context(session_id: str, turn_id: str = "turn-1") -> ExecutionContext:
    return ExecutionContext(
        session_id=session_id,
        turn_id=turn_id,
        step_id="step-1",
        cancellation=CancellationToken(),
        budget=Budget(max_steps=4),
    )


def _call(mode: str, image_path: str, text: str = "", call_id: str = "call-1") -> ToolCall:
    return ToolCall(
        tool_call_id=call_id,
        name="image",
        arguments={"mode": mode, "image_path": image_path, "text": text},
    )


def _image(tmp_path: Path, name: str = "shot.png") -> Path:
    path = tmp_path / name
    path.write_bytes(b"not-a-real-png-but-readable")
    return path


def _config(workspace: Path | None = None, *, restrict: bool = False) -> Config:
    config = Config()
    if workspace is not None:
        config.agents.defaults.workspace = str(workspace)
    config.tools.restrict_to_workspace = restrict
    return config


def _capability(
    selections: dict[str, Any],
    *,
    config: Config | None = None,
    routes: TurnRoutes | None = None,
    send=None,
) -> ImageCapability:
    return ImageCapability(
        config if config is not None else Config(),
        provider_resolver=lambda session_id: selections[session_id],
        routes=routes,
        send=send,
    )


async def test_vision_uses_the_current_session_selection(tmp_path: Path) -> None:
    first = RecordingVisionProvider("first answer")
    second = RecordingVisionProvider("second answer")
    capability = _capability(
        {
            "s1": SimpleNamespace(provider=first, model="model-one", effort=None),
            "s2": SimpleNamespace(provider=second, model="model-two", effort=None),
        }
    )
    image = _image(tmp_path)

    one = await capability._handle(_call("vision", str(image)), _context("s1"))
    two = await capability._handle(_call("vision", str(image), call_id="call-2"), _context("s2"))

    assert one.status is ToolStatus.SUCCEEDED
    assert one.text == "first answer"
    assert two.status is ToolStatus.SUCCEEDED
    assert two.text == "second answer"
    assert first.calls[0]["model"] == "model-one"
    assert second.calls[0]["model"] == "model-two"
    assert first.calls[0]["mode"] == "multimodal"


async def test_display_routes_each_session_from_its_own_binding(tmp_path: Path) -> None:
    routes = TurnRoutes()
    routes.bind("s1", TurnBinding(channel="telegram", chat_id="chat-1"))
    routes.bind("s2", TurnBinding(channel="discord", chat_id="chat-2"))
    sent = []

    async def send(message) -> None:
        sent.append(message)

    capability = _capability({}, routes=routes, send=send)
    image = _image(tmp_path)

    first = await capability._handle(
        _call("display", str(image), text="one"), _context("s1")
    )
    second = await capability._handle(
        _call("display", str(image), text="two", call_id="call-2"), _context("s2")
    )

    assert first.status is ToolStatus.SUCCEEDED
    assert second.status is ToolStatus.SUCCEEDED
    assert [(m.channel, m.chat_id, m.content) for m in sent] == [
        ("telegram", "chat-1", "one"),
        ("discord", "chat-2", "two"),
    ]


async def test_display_without_a_delivery_consumer_is_a_failure(tmp_path: Path) -> None:
    routes = TurnRoutes()
    routes.bind("s1", TurnBinding(channel="telegram", chat_id="chat-1"))
    capability = _capability({}, routes=routes, send=None)

    outcome = await capability._handle(
        _call("display", str(_image(tmp_path))), _context("s1")
    )

    assert outcome.status is ToolStatus.FAILED
    assert outcome.text == "Error: Message sending not configured"
    assert outcome.error is not None
    assert outcome.error.code == "image_display_failed"


async def test_display_without_a_turn_binding_is_a_failure(tmp_path: Path) -> None:
    sent = []

    async def send(message) -> None:
        sent.append(message)

    capability = _capability({}, routes=TurnRoutes(), send=send)

    outcome = await capability._handle(
        _call("display", str(_image(tmp_path))), _context("s1")
    )

    assert outcome.status is ToolStatus.FAILED
    assert "no active chat" in outcome.text
    assert sent == []


async def test_vision_provider_failure_is_not_a_successful_string(tmp_path: Path) -> None:
    capability = _capability(
        {"s1": SimpleNamespace(provider=FailingVisionProvider(), model="m", effort=None)}
    )

    outcome = await capability._handle(
        _call("vision", str(_image(tmp_path))), _context("s1")
    )

    assert outcome.status is ToolStatus.FAILED
    assert outcome.text.startswith("Error calling vision model:")
    assert outcome.error is not None
    assert outcome.error.code == "image_vision_failed"


async def test_provider_resolution_failure_is_explicit(tmp_path: Path) -> None:
    def resolver(session_id: str):
        raise RuntimeError("no selection for this session")

    capability = ImageCapability(Config(), provider_resolver=resolver)

    outcome = await capability._handle(
        _call("vision", str(_image(tmp_path))), _context("s1")
    )

    assert outcome.status is ToolStatus.FAILED
    assert "no selection" in outcome.text


async def test_cancel_stops_only_that_session_and_aclose_is_clean(tmp_path: Path) -> None:
    class BlockingProvider:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def chat_with_retry(self, **kwargs: Any):
            self.started.set()
            await asyncio.Event().wait()  # released only by cancellation

    provider = BlockingProvider()
    capability = _capability(
        {
            "s1": SimpleNamespace(provider=provider, model="m", effort=None),
            "s2": SimpleNamespace(provider=provider, model="m", effort=None),
        }
    )
    image = _image(tmp_path)

    task = asyncio.create_task(
        capability._handle(_call("vision", str(image)), _context("s1"))
    )
    await provider.started.wait()

    await capability.cancel("s1", None)

    with pytest.raises(asyncio.CancelledError):
        await task
    await capability.aclose()


async def test_native_schema_matches_the_legacy_image_tool() -> None:
    legacy = ImageTool(provider=None)
    assert IMAGE_TOOL.name == legacy.name == "image"
    assert IMAGE_TOOL.description == legacy.description
    assert dict(IMAGE_TOOL.parameters) == legacy.parameters
    assert IMAGE_TOOL.parameters["properties"]["mode"]["enum"] == [
        "vision",
        "display",
        "generate",
    ]


async def test_native_vision_cleans_a_quoted_path(tmp_path: Path) -> None:
    provider = RecordingVisionProvider("seen")
    capability = _capability(
        {"s1": SimpleNamespace(provider=provider, model="m", effort=None)}
    )
    image = _image(tmp_path, "a b.jpg")

    outcome = await capability._handle(
        _call("vision", f"'{image}' debris"), _context("s1")
    )

    assert outcome.status is ToolStatus.SUCCEEDED
    encoded = provider.calls[0]["messages"][1]["content"][1]["image_url"]["url"]
    assert encoded.startswith("data:image/jpeg;base64,")


async def test_generate_without_a_prompt_fails_explicitly() -> None:
    capability = _capability({})

    outcome = await capability._handle(
        _call("generate", "out.png", text=""), _context("s1")
    )

    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code == "image_generate_failed"


async def test_provider_error_finish_reason_with_text_is_failed(tmp_path: Path) -> None:
    class ErrorFinishProvider:
        async def chat_with_retry(self, **kwargs: Any):
            return SimpleNamespace(
                content="upstream overloaded", finish_reason="error"
            )

    capability = _capability(
        {"s1": SimpleNamespace(provider=ErrorFinishProvider(), model="m", effort=None)}
    )

    outcome = await capability._handle(
        _call("vision", str(_image(tmp_path))), _context("s1")
    )

    assert outcome.status is ToolStatus.FAILED
    assert "upstream overloaded" in outcome.text
    assert outcome.error is not None
    assert outcome.error.code == "image_vision_failed"


async def test_native_display_sends_paths_thread_metadata_and_reply_to(
    tmp_path: Path,
) -> None:
    routes = TurnRoutes()
    binding = TurnBinding(
        channel="telegram",
        chat_id="chat-1",
        reply_to="reply-7",
        metadata={
            "message_id": "message-1",
            "message_thread_id": "thread-9",
            "slack": {"thread_ts": "ts-1", "channel_type": "channel"},
        },
    )
    routes.bind("s1", binding)
    sent = []

    async def send(message) -> None:
        sent.append(message)

    capability = _capability({}, routes=routes, send=send)
    image = _image(tmp_path)

    outcome = await capability._handle(
        _call("display", str(image), text="view"), _context("s1")
    )

    assert outcome.status is ToolStatus.SUCCEEDED
    message = sent[0]
    assert message.media == [str(image)]
    assert all(isinstance(item, str) for item in message.media)
    assert message.reply_to == "reply-7"
    assert message.metadata == dict(binding.metadata)
    assert message.metadata["message_thread_id"] == "thread-9"
    # An image-only turn does not consume the final-text reply.
    assert routes.consume_reply_sent("s1", "turn-1") is False


async def test_legacy_display_keeps_data_url_payload(tmp_path: Path) -> None:
    sent = []

    async def send(message) -> None:
        sent.append(message)

    tool = LegacyImageTool(provider=None, send_callback=send)
    tool.set_context("telegram", "chat-1")
    image = _image(tmp_path)

    result = await tool.execute(mode="display", image_path=str(image), text="legacy")

    assert result.startswith("Image displayed successfully")
    media = sent[0].media[0]
    assert media["data"].startswith("data:image/png;base64,")
    assert media["file_name"] == image.name
    assert sent[0].metadata["msg_type"] == "image"


async def test_native_display_resolves_relative_path_in_the_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    image = workspace / "inside.png"
    image.write_bytes(b"png")
    routes = TurnRoutes()
    routes.bind("s1", TurnBinding(channel="telegram", chat_id="chat-1"))
    sent = []

    async def send(message) -> None:
        sent.append(message)

    capability = _capability(
        {}, config=_config(workspace, restrict=True), routes=routes, send=send
    )

    outcome = await capability._handle(
        _call("display", "inside.png"), _context("s1")
    )

    assert outcome.status is ToolStatus.SUCCEEDED
    assert sent[0].media == [str(image.resolve())]


async def test_native_input_outside_a_restricted_workspace_is_rejected(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"png")
    routes = TurnRoutes()
    routes.bind("s1", TurnBinding(channel="telegram", chat_id="chat-1"))
    sent = []

    async def send(message) -> None:
        sent.append(message)

    capability = _capability(
        {}, config=_config(workspace, restrict=True), routes=routes, send=send
    )

    outcome = await capability._handle(
        _call("display", str(outside)), _context("s1")
    )

    assert outcome.status is ToolStatus.FAILED
    assert "outside the workspace" in outcome.text
    assert sent == []


async def test_native_expands_a_literal_tilde_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    image = home / "shot.png"
    image.write_bytes(b"png")
    monkeypatch.setenv("HOME", str(home))
    routes = TurnRoutes()
    routes.bind("s1", TurnBinding(channel="telegram", chat_id="chat-1"))
    sent = []

    async def send(message) -> None:
        sent.append(message)

    capability = _capability({}, config=_config(), routes=routes, send=send)

    outcome = await capability._handle(
        _call("display", "~/shot.png"), _context("s1")
    )

    assert outcome.status is ToolStatus.SUCCEEDED
    assert sent[0].media == [str(image.resolve())]


class _FakeResponse:
    def __init__(self, payload: dict[str, Any] | None = None, content: bytes = b"") -> None:
        self._payload = payload
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload or {}


class _FakeHTTPClient:
    def __init__(self, timeout: float | None = None) -> None:
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, *args: Any, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(
            payload={
                "output": {
                    "choices": [
                        {"message": {"content": [{"image": "https://example.test/img"}]}}
                    ]
                },
                "usage": {"width": 1024, "height": 1024},
            }
        )

    async def get(self, *args: Any, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(content=b"generated-image")


async def test_native_generate_saves_under_workspace_without_a_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(httpx, "AsyncClient", _FakeHTTPClient)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    capability = _capability({}, config=_config(workspace, restrict=True))

    outcome = await capability._handle(
        _call("generate", "out.png", text="a cat"), _context("s1")
    )

    saved = workspace / "out.png"
    assert outcome.status is ToolStatus.SUCCEEDED
    assert saved.read_bytes() == b"generated-image"
    assert str(saved) in outcome.text


async def test_native_generate_output_outside_workspace_is_rejected(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.png"
    capability = _capability({}, config=_config(workspace, restrict=True))

    outcome = await capability._handle(
        _call("generate", str(outside), text="a cat"), _context("s1")
    )

    assert outcome.status is ToolStatus.FAILED
    assert "outside the workspace" in outcome.text
    assert outside.exists() is False


async def test_native_vision_output_is_limited_but_input_bytes_are_not(
    tmp_path: Path,
) -> None:
    long_answer = "prefix " + "x" * 9000
    provider = RecordingVisionProvider(long_answer)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    image = workspace / "large.png"
    raw = b"image-bytes" * 800
    image.write_bytes(raw)
    capability = _capability(
        {"s1": SimpleNamespace(provider=provider, model="m", effort=None)},
        config=_config(workspace),
    )

    outcome = await capability._handle(
        _call("vision", str(image)), _context("s1")
    )

    assert outcome.status is ToolStatus.SUCCEEDED
    assert len(outcome.text) < len(long_answer)
    assert "truncated" in outcome.text
    assert list((workspace / ".paos" / "tool_outputs").glob("*.txt"))
    sent_url = provider.calls[0]["messages"][1]["content"][1]["image_url"]["url"]
    assert base64.b64encode(raw).decode() in sent_url
