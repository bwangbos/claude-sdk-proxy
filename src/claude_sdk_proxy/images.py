from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable, Mapping
from typing import Any

from claude_sdk_proxy.domain import (
    CanonicalMessage,
    ImageBlock,
    RequestValidationError,
    TextBlock,
    ToolResultBlock,
    UnsupportedFeature,
)

# Conservative transport limits, including images in retained input history.
MAX_IMAGE_BYTES = 3 * 1024 * 1024
MAX_IMAGE_TOTAL_BYTES = 12 * 1024 * 1024
MAX_IMAGES = 20
MEDIA_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
type ResultIdentity = str | tuple[str | ImageBlock, ...]


def validate_image(image: ImageBlock) -> None:
    if not isinstance(image.media_type, str) or image.media_type not in MEDIA_TYPES:
        raise UnsupportedFeature("messages", "image media type is unsupported")
    if (
        not isinstance(image.data, str)
        or not image.data
        or len(image.data) > 4 * MAX_IMAGE_BYTES // 3
    ):
        raise RequestValidationError(
            "messages", "image exceeds its size limit or is empty"
        )
    try:
        raw = base64.b64decode(image.data, validate=True)
    except ValueError, binascii.Error:
        raise RequestValidationError("messages", "image base64 is invalid") from None
    if (
        not raw
        or len(raw) > MAX_IMAGE_BYTES
        or base64.b64encode(raw).decode() != image.data
    ):
        raise RequestValidationError("messages", "image base64 is invalid or too large")
    valid = {
        "image/png": raw.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": raw.startswith(b"\xff\xd8\xff"),
        "image/gif": raw.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": raw.startswith(b"RIFF") and raw[8:12] == b"WEBP",
    }
    if not valid[image.media_type]:
        raise RequestValidationError("messages", "image does not match its media type")


def validate_image_budget(messages: Iterable[CanonicalMessage]) -> None:
    count = total = 0
    for message in messages:
        for block in message.blocks:
            parts = block.content if isinstance(block, ToolResultBlock) else (block,)
            for part in parts:
                if isinstance(part, ImageBlock):
                    count += 1
                    padding = len(part.data) - len(part.data.rstrip("="))
                    total += len(part.data) * 3 // 4 - padding
    if count > MAX_IMAGES or total > MAX_IMAGE_TOTAL_BYTES:
        raise RequestValidationError("messages", "images exceed request limits")


def anthropic_image(raw: Mapping[str, object]) -> ImageBlock:
    if set(raw) != {"type", "source"}:
        raise RequestValidationError("messages", "image fields are invalid")
    source = raw.get("source")
    if not isinstance(source, Mapping):
        raise RequestValidationError("messages", "image source is invalid")
    if source.get("type") != "base64":
        raise UnsupportedFeature(
            "messages", "only embedded base64 images are supported"
        )
    if (
        set(source) != {"type", "media_type", "data"}
        or not isinstance(source.get("media_type"), str)
        or not isinstance(source.get("data"), str)
    ):
        raise RequestValidationError("messages", "image source is invalid")
    return ImageBlock(source["media_type"], source["data"])


def openai_image(raw: Mapping[str, object]) -> ImageBlock:
    if set(raw) != {"type", "image_url"}:
        raise RequestValidationError("messages", "image fields are invalid")
    source = raw.get("image_url")
    if not isinstance(source, Mapping) or set(source) - {"url", "detail"}:
        raise RequestValidationError("messages", "image_url is invalid")
    if source.get("detail", "auto") != "auto":
        raise UnsupportedFeature("messages", "image detail controls are unsupported")
    url = source.get("url")
    if not isinstance(url, str):
        raise RequestValidationError("messages", "image URL must be a string")
    if not url.startswith("data:"):
        raise UnsupportedFeature("messages", "only embedded data URLs are supported")
    header, separator, data = url[5:].partition(";base64,")
    if not separator:
        raise RequestValidationError("messages", "image data URL is invalid")
    return ImageBlock(header, data)


def render_image(image: ImageBlock) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": image.media_type,
            "data": image.data,
        },
    }


def render_content(
    parts: Iterable[str | TextBlock | ImageBlock],
) -> list[dict[str, Any]]:
    return [
        render_image(part)
        if isinstance(part, ImageBlock)
        else {"type": "text", "text": part if isinstance(part, str) else part.text}
        for part in parts
    ]


def result_identity(parts: Iterable[str | ImageBlock]) -> ResultIdentity:
    items = tuple(parts)
    if all(isinstance(part, str) for part in items):
        return "".join(part for part in items if isinstance(part, str))
    return items
