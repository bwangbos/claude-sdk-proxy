from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import cast

from claude_sdk_proxy.domain import (
    CanonicalMessage,
    ImageBlock,
    TextBlock,
    TextRequest,
    ToolCallBlock,
    ToolDefinition,
)
from claude_sdk_proxy.images import render_image
from claude_sdk_proxy.tool_contract import JsonValue, plain_json


def _json_value(value: Mapping[str, object]) -> object:
    return plain_json(cast(Mapping[str, JsonValue], value))


def _canonical_tool(tool: ToolDefinition) -> dict[str, object]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": _json_value(tool.input_schema),
    }


def _canonical_message(message: CanonicalMessage) -> dict[str, object]:
    blocks: list[dict[str, object]] = []
    for block in message.blocks:
        if isinstance(block, TextBlock):
            item: dict[str, object] = {"type": "text", "text": block.text}
        elif isinstance(block, ImageBlock):
            item = render_image(block)
        elif isinstance(block, ToolCallBlock):
            item = {
                "type": "tool_call",
                "id": block.id,
                "name": block.name,
                "arguments": _json_value(block.arguments),
            }
        else:
            item = {
                "type": "tool_result",
                "tool_call_id": block.tool_call_id,
                "content": [
                    render_image(part) if isinstance(part, ImageBlock) else part
                    for part in block.content
                ],
                "is_error": block.is_error,
            }
        blocks.append(item)
    if blocks and all(item["type"] == "tool_result" for item in blocks):
        blocks.sort(key=lambda item: cast(str, item["tool_call_id"]))
    return {"role": message.role, "blocks": blocks}


def request_fingerprint(request: TextRequest) -> str:
    identity = {
        "dialect": request.dialect,
        "model": request.model,
        "system": request.system,
        "thinking": {
            "mode": request.thinking.mode,
            "effort": request.thinking.effort,
            "budget_tokens": request.thinking.budget_tokens,
            "display": request.thinking.display,
        },
        "tools": [
            _canonical_tool(tool)
            for tool in sorted(request.tools, key=lambda item: item.name)
        ],
        "messages": [_canonical_message(message) for message in request.messages],
    }
    encoded = json.dumps(
        identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def messages_equal(
    left: tuple[CanonicalMessage, ...], right: tuple[CanonicalMessage, ...]
) -> bool:
    return tuple(map(_canonical_message, left)) == tuple(map(_canonical_message, right))


def tools_equal(
    left: tuple[ToolDefinition, ...], right: tuple[ToolDefinition, ...]
) -> bool:
    return tuple(
        _canonical_tool(tool) for tool in sorted(left, key=lambda item: item.name)
    ) == tuple(
        _canonical_tool(tool) for tool in sorted(right, key=lambda item: item.name)
    )


__all__ = ["messages_equal", "request_fingerprint", "tools_equal"]
