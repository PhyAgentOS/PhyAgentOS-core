"""Image tool routing follows the current turn, not the last one that wrote a field."""

from __future__ import annotations

from pathlib import Path

from PhyAgentOS.agent.tools.image import ImageTool


async def test_display_uses_the_turn_context(tmp_path: Path) -> None:
    image = tmp_path / "shot.png"
    image.write_bytes(b"not-a-real-png-but-readable")
    sent = []

    async def send(message) -> None:
        sent.append(message)

    tool = ImageTool(provider=None, send_callback=send)
    tool.set_context("telegram", "chat-1")

    result = await tool.execute(mode="display", image_path=str(image), text="view")

    assert result.startswith("Image displayed successfully")
    assert len(sent) == 1
    assert (sent[0].channel, sent[0].chat_id) == ("telegram", "chat-1")


async def test_display_without_a_context_reports_an_error(tmp_path: Path) -> None:
    image = tmp_path / "shot.png"
    image.write_bytes(b"not-a-real-png-but-readable")
    sent = []

    async def send(message) -> None:
        sent.append(message)

    tool = ImageTool(provider=None, send_callback=send)

    result = await tool.execute(mode="display", image_path=str(image))

    assert result.startswith("Error:")
    assert sent == []
