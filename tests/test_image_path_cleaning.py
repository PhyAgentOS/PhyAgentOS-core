"""Debris tolerance for model-supplied image paths.

Models occasionally leak reasoning fragments into the ``image_path``
argument or wrap it in quotes. The cleaner must recover the real file —
including a quoted path that itself contains spaces — and it must apply to
every read mode (vision *and* display), not just vision.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from PhyAgentOS.agent.tools.image import ImageTool


@pytest.mark.parametrize("image_path", ["", "   ", "\t\n"])
def test_clean_empty_path(image_path: str) -> None:
    assert ImageTool._clean_image_path(image_path) == ""


@pytest.mark.parametrize("mode", ["vision", "display"])
@pytest.mark.parametrize("kwargs", [{}, {"image_path": ""}, {"image_path": "   "}])
async def test_execute_without_image_returns_parameter_error(mode: str, kwargs: dict) -> None:
    tool = ImageTool(provider=None)
    result = await tool.execute(mode=mode, **kwargs)
    assert result.startswith("Error: No image provided.")


def test_clean_path_keeps_quoted_path_containing_spaces(tmp_path: Path) -> None:
    real = tmp_path / "a b.jpg"
    real.write_bytes(b"x")

    assert ImageTool._clean_image_path(f"'{real}'") == str(real)


def test_clean_path_strips_debris_after_whitespace(tmp_path: Path) -> None:
    real = tmp_path / "frame.jpg"
    real.write_bytes(b"x")

    assert ImageTool._clean_image_path(f"{real} conventionaloops") == str(real)


def test_clean_path_strips_glued_debris_after_extension(tmp_path: Path) -> None:
    real = tmp_path / "frame.jpg"
    real.write_bytes(b"x")

    assert ImageTool._clean_image_path(f"{real}junk") == str(real)


def test_clean_path_keeps_existing_path_with_spaces_untouched(tmp_path: Path) -> None:
    real = tmp_path / "a b.jpg"
    real.write_bytes(b"x")

    assert ImageTool._clean_image_path(str(real)) == str(real)


async def test_execute_cleans_path_for_display_mode_too(tmp_path: Path) -> None:
    real = tmp_path / "a b.jpg"
    real.write_bytes(b"x")
    tool = ImageTool(provider=None)
    seen: list[str] = []

    async def _capture_display(*, text: str, image_path: str, **kwargs) -> str:
        seen.append(image_path)
        return "displayed"

    tool._execute_display = _capture_display  # type: ignore[method-assign]

    result = await tool.execute(mode="display", text="look", image_path=f"'{real}' junk")

    assert result == "displayed"
    assert seen == [str(real)]
