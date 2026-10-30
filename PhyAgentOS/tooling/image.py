"""Image tools for analyzing, displaying, and generating images.

This is the shared implementation. The legacy tool module re-exports
:class:`ImageTool` for the old loop, while the Core adapter in
:mod:`PhyAgentOS.agent.capabilities.image_capability` passes routing and the
current provider explicitly per call. The legacy ``ContextVar`` routing is kept
only for the old loop's compatibility methods; no Core path reads it.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from PhyAgentOS.bus.events import OutboundMessage
from PhyAgentOS.tooling.base import Tool

_VALID_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


@dataclass(frozen=True, slots=True)
class ImageCallContext:
    """Everything one image call needs, supplied by the caller per turn.

    ``provider`` is the current turn's resolved provider, never a provider
    captured when the tool was built. ``channel``/``chat_id`` route a display
    message; ``send`` is the outbound publisher. All are explicit so two
    concurrent sessions cannot share routing state.

    ``channel_media_paths`` selects the Core channel contract: ``media`` is a
    list of local file paths and the incoming ``metadata``/``reply_to`` are
    preserved. When false (the legacy path), display keeps the historical
    data-URL media payload.
    """

    provider: Any | None = None
    model: str | None = None
    channel: str = ""
    chat_id: str = ""
    message_id: str | None = None
    send: Callable[[OutboundMessage], Awaitable[None]] | None = None
    channel_media_paths: bool = False
    metadata: dict[str, Any] | None = None
    reply_to: str | None = None
    workspace: Path | None = None
    restrict_to_workspace: bool = False


class ImageTool(Tool):
    """
    Tool for analyzing and displaying images.

    Supports three modes:
    - vision: Analyze images using multimodal LLM models (OCR, description, visual QA)
    - display: Display images to users through WebSocket channel
    - generate: Generate images from text prompts
    """

    def __init__(
        self,
        provider: Any | None,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
        default_message_id: str | None = None,
    ):
        """Initialize the image tool with legacy routing defaults."""
        self.provider = provider
        self._send_callback = send_callback
        # Legacy compatibility only: routing belongs to one turn, not to the
        # shared tool instance. Core callers pass ``ImageCallContext``.
        self._context: ContextVar[tuple[str, str, str | None]] = ContextVar(
            "image_tool_context",
            default=(default_channel, default_chat_id, default_message_id),
        )

    @property
    def name(self) -> str:
        return "image"

    @property
    def description(self) -> str:
        return (
            "Analyze or display images. Supports three modes:\n"
            "- vision: Analyze and describe images using multimodal LLM models. "
            "Supports image description, text extraction (OCR), visual analysis, "
            "and answering questions about image content.\n"
            "- display: Display images to users by reading from disk and sending to frontend. "
            "Use this when you want to show an image to the user directly.\n"
            "- generate: Generate images from text prompts using Alibaba Cloud Qwen-Image API. "
            "Supports various artistic styles and text rendering in images.\n"
            "Images are encoded as base64 for processing or display."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["vision", "display", "generate"],
                    "description": (
                        "The operation mode: 'vision' for image analysis, 'display' for showing images to users, "
                        "'generate' for creating images from text prompts."
                    ),
                },
                "text": {
                    "type": "string",
                    "description": (
                        "[In vision mode] The user's request or question about the image. "
                        "Examples: 'Describe this image', 'Extract text from this image', "
                        "'What objects are in this picture?', 'Is there a cat in this photo?'\n"
                        "[In display mode] Caption to display with the image. "
                        "This text will appear above the image in the message box.\n"
                        "[In generate mode] Text prompt describing the desired image content, style, and composition. "
                        "Supports Chinese and English, max 800 characters. Example: 'A sitting orange cat with happy expression'."
                    ),
                },
                "image_path": {
                    "type": "string",
                    "description": (
                        "[In vision/display mode] Path to the image file to analyze or display. Should be an absolute path "
                        "to a local image file (e.g., '/Users/archer/Desktop/photo.png'). "
                        "Supported formats: PNG, JPG, JPEG, GIF, WEBP.\n"
                        "[In generate mode] Absolute path where the generated image will be saved."
                    ),
                },
                "size": {
                    "type": "string",
                    "description": (
                        "[Optional for generate mode] Output image resolution in format 'width*height'. "
                        "For qwen-image-2.0 series: total pixels must be between 512*512 and 2048*2048, default is 1024*1024. "
                        "Examples: '1024*1024', '1664*928', '1328*1328'."
                    ),
                },
                "negative_prompt": {
                    "type": "string",
                    "description": (
                        "[Optional for generate mode] Negative prompt describing what should NOT appear in the image. "
                        "Max 500 characters. Example: 'low resolution, low quality, deformed limbs, blurry text'."
                    ),
                },
                "n": {
                    "type": "integer",
                    "description": (
                        "[Optional for generate mode] Number of images to generate. "
                        "For qwen-image-2.0 series: 1-6 images, default is 1. "
                        "For qwen-image-max/plus series: fixed at 1."
                    ),
                },
                "prompt_extend": {
                    "type": "boolean",
                    "description": (
                        "[Optional for generate mode] Enable AI-powered prompt enhancement. "
                        "When enabled, the model will optimize and refine the prompt for better results. "
                        "Default is true. Set to false for more controlled output."
                    ),
                },
                "watermark": {
                    "type": "boolean",
                    "description": (
                        "[Optional for generate mode] Add 'Qwen-Image' watermark to bottom-right corner. "
                        "Default is false."
                    ),
                },
            },
            "required": ["mode", "image_path"],
        }

    @staticmethod
    def _validate_image_path(image_path: str) -> Path:
        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"Image file not found: {image_path}")
        if path.suffix.lower() not in _VALID_IMAGE_EXTENSIONS:
            raise ValueError(
                f"Unsupported image format: {path.suffix}. "
                f"Supported formats: {', '.join(_VALID_IMAGE_EXTENSIONS)}"
            )
        return path

    def _encode_image(self, image_path: str) -> str:
        """
        Encode an image file to base64 string.

        Args:
            image_path: Absolute path to the image file.

        Returns:
            Base64 encoded image string.

        Raises:
            FileNotFoundError: If image file doesn't exist.
            ValueError: If file is not a valid image.
        """
        path = self._validate_image_path(image_path)

        # Read and encode image
        with open(path, "rb") as image_file:
            encoded = base64.b64encode(image_file.read()).decode("utf-8")

        return encoded

    def _get_mime_type(self, image_path: str) -> str:
        """Get MIME type based on file extension."""
        ext = Path(image_path).suffix.lower()
        return _MIME_TYPES.get(ext, "image/png")

    def set_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Set the legacy turn routing context."""
        self._context.set((channel, chat_id, message_id))

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """Set the legacy callback for sending messages."""
        self._send_callback = callback

    def _provider_for(self, context: ImageCallContext | None) -> Any | None:
        return context.provider if context is not None else self.provider

    def _send_for(
        self, context: ImageCallContext | None
    ) -> Callable[[OutboundMessage], Awaitable[None]] | None:
        return context.send if context is not None else self._send_callback

    def _routing_for(self, context: ImageCallContext | None) -> tuple[str, str, str | None]:
        if context is not None:
            return context.channel, context.chat_id, context.message_id
        return self._context.get()

    def _model_for(self, context: ImageCallContext | None) -> str | None:
        return context.model if context is not None else None

    @staticmethod
    def _resolve_native_path(
        image_path: str, context: ImageCallContext
    ) -> str:
        """Resolve a Core path against the workspace, without legacy fallback.

        An empty path is returned unchanged so the mode's own "path required"
        error stays the same. ``~`` is expanded only on this explicit path;
        legacy calls keep their historical literal handling.
        """
        if not image_path:
            return image_path
        path = Path(image_path).expanduser()
        workspace = context.workspace
        if not path.is_absolute():
            if workspace is None:
                raise ValueError("A workspace is required to resolve image paths.")
            path = workspace / path
        path = path.resolve()
        if context.restrict_to_workspace:
            if workspace is None:
                raise PermissionError("A workspace is required to restrict image paths.")
            try:
                path.relative_to(workspace.resolve())
            except ValueError:
                raise PermissionError(
                    f"Path {image_path} is outside the workspace."
                ) from None
        return str(path)

    async def execute(
        self,
        mode: str,
        text: str = "",
        image_path: str = "",
        size: str = "1024*1024",
        negative_prompt: str = "",
        n: int = 1,
        prompt_extend: bool = True,
        watermark: bool = False,
        *,
        call_context: ImageCallContext | None = None,
        **kwargs: Any,
    ) -> str:
        """
        Execute image tool based on mode.

        Args:
            mode: Operation mode - 'vision' for analysis, 'display' for showing images, 'generate' for creating images.
            text: [Vision mode] User's request/question about the image. [Display mode] Caption to display. [Generate mode] Image generation prompt.
            image_path: Path to the image file (for vision/display) or save path (for generate).
            size: [Generate mode] Output image resolution.
            negative_prompt: [Generate mode] Negative prompt for undesired content.
            n: [Generate mode] Number of images to generate.
            prompt_extend: [Generate mode] Enable AI prompt enhancement.
            watermark: [Generate mode] Add watermark.
            call_context: Explicit per-call provider and routing; legacy callers omit it.

        Returns:
            Analysis result (vision mode), status message (display mode), or generation result (generate mode).
        """
        if mode in ("vision", "display"):
            # Model-supplied read paths get debris tolerance (see _clean_image_path).
            # generate's image_path is an output filename, not an existing file,
            # so it is left untouched.
            image_path = self._clean_image_path(image_path)
        if call_context is not None:
            try:
                image_path = self._resolve_native_path(image_path, call_context)
            except (OSError, ValueError) as exc:
                return f"Error: {exc}"
        if mode == "vision":
            return await self._execute_vision(
                text=text, image_path=image_path, call_context=call_context, **kwargs
            )
        elif mode == "display":
            return await self._execute_display(
                text=text, image_path=image_path, call_context=call_context, **kwargs
            )
        elif mode == "generate":
            return await self._execute_generate(
                text=text,
                image_path=image_path,
                size=size,
                negative_prompt=negative_prompt,
                n=n,
                prompt_extend=prompt_extend,
                watermark=watermark,
                call_context=call_context,
                **kwargs,
            )
        else:
            return f"Error: Invalid mode '{mode}'. Must be 'vision', 'display', or 'generate'."

    @staticmethod
    def _clean_image_path(image_path: str) -> str:
        """Tolerate leaked reasoning debris around the path.

        Models occasionally emit chain-of-thought fragments inside the
        argument value (e.g. ``"/path/frame.jpg conventionaloops"``) or wrap
        it in quotes. Quotes are honoured first — a quoted path may itself
        contain spaces, so the closing quote, not whitespace, ends it —
        then a whitespace-free prefix is tried: file paths from tool
        results never contain spaces in these flows. Every rewrite is
        gated on the candidate existing, so an already-valid path is
        returned unchanged.
        """
        cleaned = image_path.strip()
        if not cleaned:
            return ""

        candidates: list[str] = []
        # Quote-wrapped path (possibly followed by debris): the closing quote ends it.
        if cleaned[:1] in "\"'`":
            end = cleaned.find(cleaned[0], 1)
            if end > 0:
                candidates.append(cleaned[1:end].strip())
        # Wrapping punctuation stripped from the edges only.
        base = cleaned.strip("\"'`").strip()
        candidates.append(base)
        # Debris separated by whitespace after the path.
        if " " in base and not Path(base).exists():
            candidates.append(base.split()[0].strip("\"'`").strip())

        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return candidate

        # Debris glued on WITHOUT whitespace (e.g. "/path/frame.jpgjunk"):
        # keep the prefix ending at the last image extension, but only when
        # that candidate actually exists on disk.
        last = candidates[-1]
        if last:
            match = re.match(r"^(.*\.(?:jpe?g|png|gif|webp|bmp))", last, re.IGNORECASE)
            if match:
                candidate = match.group(1)
                if candidate != last and Path(candidate).exists():
                    return candidate
        return last

    async def _execute_vision(
        self,
        text: str,
        image_path: str,
        *,
        call_context: ImageCallContext | None = None,
        **kwargs: Any,
    ) -> str:
        """
        Execute image vision analysis.

        Args:
            text: User's request/question about the image.
            image_path: Image file path.
            call_context: Explicit provider for this call.

        Returns:
            Analysis result from the vision model.
        """
        if not image_path:
            return "Error: No image provided. Please provide at least one image path."

        content: list[dict[str, Any]] = [{"type": "text", "text": text}]

        # Process image
        try:
            encoded_image = self._encode_image(image_path)
            mime_type = self._get_mime_type(image_path)

            # Add image to content in OpenAI format
            # Reference: https://platform.moonshot.cn/docs/guide/use-kimi-vision-model
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{encoded_image}"},
                }
            )
        except FileNotFoundError as e:
            return f"Error: {str(e)}"
        except ValueError as e:
            return f"Error: {str(e)}"
        except Exception as e:
            return f"Error encoding image '{image_path}': {str(e)}"

        # Build messages for chat completion
        messages = [
            {"role": "system", "content": "Your are a multimodal model"},
            {"role": "user", "content": content},
        ]
        provider = self._provider_for(call_context)
        if provider is None:
            return "Error: Vision model is not configured."
        model = self._model_for(call_context)
        try:
            request: dict[str, Any] = {"messages": messages, "mode": "multimodal"}
            if model:
                request["model"] = model
            response = await provider.chat_with_retry(**request)
            if getattr(response, "finish_reason", None) == "error":
                detail = getattr(response, "content", None) or "provider reported an error"
                return f"Error calling vision model: {detail}"
            if response.content:
                return response.content
            return "Error: No response content from vision model."
        except Exception as e:
            return f"Error calling vision model: {str(e)}"

    async def _execute_display(
        self,
        text: str,
        image_path: str,
        *,
        call_context: ImageCallContext | None = None,
        **kwargs: Any,
    ) -> str:
        """
        Execute image display by sending image data to frontend.

        Args:
            text: Optional caption text to display above the image.
            image_path: Path to the image file to display.
            call_context: Explicit routing and outbound publisher for this call.

        Returns:
            Status message indicating success or error.
        """
        if not image_path:
            return "Error: No image provided. Please provide at least one image path."

        send = self._send_for(call_context)
        if not send:
            return "Error: Message sending not configured"

        channel, chat_id, _ = self._routing_for(call_context)
        if not channel or not chat_id:
            return "Error: no active chat to display the image in."

        try:
            if call_context is not None and call_context.channel_media_paths:
                # Core channels consume local file paths. Validate without
                # encoding so a large image is not copied through base64 here.
                self._validate_image_path(image_path)
                media: list[Any] = [image_path]
                metadata = dict(call_context.metadata or {})
                reply_to = call_context.reply_to
            else:
                # Legacy WebSocket/frontend payload: an inline data URL plus
                # its file name. Kept unchanged for the old loop.
                encoded_image = self._encode_image(image_path)
                mime_type = self._get_mime_type(image_path)
                media = [
                    {
                        "data": f"data:{mime_type};base64,{encoded_image}",
                        "file_name": Path(image_path).name,
                    }
                ]
                metadata = {
                    "msg_type": "image",  # Indicate this is an image message
                    "file_type": mime_type,
                }
                reply_to = None

            # Create outbound message with image as media
            msg = OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=text or "Image display",
                reply_to=reply_to,
                media=media,
                metadata=metadata,
            )

            # Send the message through the callback
            await send(msg)

            return f"Image displayed successfully: {Path(image_path).name}"

        except FileNotFoundError as e:
            return f"Error: {str(e)}"
        except ValueError as e:
            return f"Error: {str(e)}"
        except Exception as e:
            return f"Error: {str(e)}"

    async def _execute_generate(
        self,
        text: str,
        image_path: str,
        size: str = "1024*1024",
        negative_prompt: str = "",
        n: int = 1,
        prompt_extend: bool = True,
        watermark: bool = False,
        *,
        call_context: ImageCallContext | None = None,
        **kwargs: Any,
    ) -> str:
        """
        Execute image generation using Alibaba Cloud Qwen-Image API.

        Args:
            text: Text prompt describing the desired image content, style, and composition.
            image_path: File name where the generated image will be saved.
            size: Output image resolution in format 'width*height'.
            negative_prompt: Negative prompt for undesired content.
            n: Number of images to generate (1-6 for qwen-image-2.0 series, fixed 1 for max/plus).
            prompt_extend: Enable AI-powered prompt enhancement.
            watermark: Add 'Qwen-Image' watermark.
            call_context: Explicit routing used when one image is shown after generation.

        Returns:
            Status message with generation result or error details.
        """
        import json
        import os

        import httpx

        if not text:
            return "Error: Text prompt is required for image generation."

        if not image_path:
            return "Error: Image save path is required."

        # Get API key from environment
        api_key = os.getenv("DASHSCOPE_API_KEY")
        if not api_key:
            return (
                "Error: DASHSCOPE_API_KEY not found. Please set it in environment variables at ~/.PhyAgentOS/workspace/.env. "
                "Get your API key from https://dashscope.console.aliyun.com/"
            )

        # Determine endpoint based on region
        # You can set DASHSCOPE_REGION to 'beijing' or 'singapore', default is beijing
        region = os.getenv("DASHSCOPE_REGION", "beijing").lower()
        if region == "beijing":
            endpoint = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
        elif region == "singapore":
            endpoint = "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
        else:
            return f"Error: Invalid region '{region}'. Must be 'beijing' or 'singapore'."

        # Build request payload
        payload = {
            "model": os.getenv("DASHSCOPE_IMAGE_GEN_MODEL", "qwen-imag-max"),
            "input": {"messages": [{"role": "user", "content": [{"text": text}]}]},
            "parameters": {
                "size": size,
                "prompt_extend": prompt_extend,
                "watermark": watermark,
            },
        }

        # Add optional parameters
        if negative_prompt:
            payload["parameters"]["negative_prompt"] = negative_prompt

        # Only add n parameter for qwen-image-2.0 series (supports 1-6 images)
        if n > 1:
            payload["parameters"]["n"] = min(n, 6)  # Cap at 6 for 2.0 series

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(endpoint, headers=headers, json=payload)
                response.raise_for_status()
                result = response.json()

            # Parse response
            output = result.get("output", {})
            choices = output.get("choices", [])

            if not choices:
                return (
                    f"Error: No image generated. Response: {json.dumps(result, ensure_ascii=False)}"
                )

            # Get image URLs
            image_urls = []
            for choice in choices:
                message = choice.get("message", {})
                content = message.get("content", [])
                for item in content:
                    if "image" in item:
                        image_urls.append(item["image"])

            if not image_urls:
                return f"Error: No image URL in response. Response: {json.dumps(result, ensure_ascii=False)}"

            # Download and save the first image (or all images if n > 1)
            saved_paths = []
            for idx, img_url in enumerate(image_urls):
                async with httpx.AsyncClient(timeout=30.0) as download_client:
                    img_response = await download_client.get(img_url)
                    img_response.raise_for_status()

                    # Determine save path. Core callers pass an already-resolved
                    # path under the workspace; legacy keeps the media base and
                    # its literal ``~`` behavior unchanged.
                    if call_context is not None:
                        path_obj = Path(image_path)
                    else:
                        path_obj = Path("~/.PhyAgentOS/media") / image_path
                    if len(image_urls) > 1:
                        # Multiple images: add index to filename
                        save_path = path_obj.parent / f"{path_obj.stem}_{idx + 1}{path_obj.suffix}"
                    else:
                        save_path = path_obj

                    # Save image
                    save_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(save_path, "wb") as f:
                        f.write(img_response.content)

                    saved_paths.append(str(save_path))

            # Get usage info
            usage = result.get("usage", {})
            width = usage.get("width", "unknown")
            height = usage.get("height", "unknown")

            if len(saved_paths) == 1:
                logger.info(f"Image generated successfully: {saved_paths[0]} ({width}x{height})")
                if call_context is not None and call_context.send is None:
                    # Core with no delivery consumer: the artifact exists, so
                    # report generation itself instead of turning the saved
                    # image into a failed display call.
                    return (
                        f"Image generated successfully: {saved_paths[0]} "
                        f"({width}x{height})"
                    )
                return await self._execute_display(
                    f"Generated image:\n{os.path.basename(saved_paths[0])}",
                    saved_paths[0],
                    call_context=call_context,
                )
            return (
                f"Generated {len(saved_paths)} images successfully:\n"
                + "\n".join(f"- {path}" for path in saved_paths)
                + f"\nResolution: {width}x{height}"
            )

        except httpx.HTTPStatusError as e:
            error_detail = e.response.text if e.response else str(e)
            try:
                error_json = e.response.json()
                error_msg = error_json.get("message", error_json.get("error", error_detail))
            except Exception:
                error_msg = error_detail
            return f"Error: HTTP {e.response.status_code} - {error_msg}"
        except httpx.TimeoutException:
            return (
                "Error: Request timed out. The generation may take 10-30 seconds, please try again."
            )
        except httpx.RequestError as e:
            return f"Error: Network request failed - {str(e)}"
        except Exception as e:
            return f"Error: Generation failed - {str(e)}"
