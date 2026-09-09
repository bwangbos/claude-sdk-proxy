from __future__ import annotations

import re
from collections.abc import Mapping
from typing import cast

from quaylet.domain import (
    CanonicalBlock,
    CanonicalMessage,
    ImageBlock,
    RedactedThinkingBlock,
    RequestValidationError,
    Role,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolDefinition,
    ToolResultBlock,
    UnsupportedFeature,
)
from quaylet.images import anthropic_image

_MESSAGE_FIELDS = {"role", "content"}
_TOOL_FIELDS = {"name", "description", "input_schema"}
_TEXT_BLOCK_FIELDS = {"type", "text"}
_TOOL_USE_FIELDS = {"type", "id", "name", "input"}
_TOOL_RESULT_FIELDS = {"type", "tool_use_id", "content", "is_error"}
_TOOL_CHOICE_FIELDS = {"type", "name", "disable_parallel_tool_use"}
_UNSUPPORTED_MESSAGE_FIELDS = {"tool_use_id", "tool_call_id", "tool_calls"}
_TOOL_ID = re.compile(r"toolu_[A-Za-z0-9_-]+")


def without_cache_hint(raw: Mapping[str, object]) -> Mapping[str, object]:
    if "cache_control" not in raw:
        return raw
    hint = raw["cache_control"]
    if (
        not isinstance(hint, Mapping)
        or set(hint) - {"type", "ttl"}
        or hint.get("type") != "ephemeral"
        or hint.get("ttl", "5m") not in ("5m", "1h")
    ):
        raise RequestValidationError("messages", "cache hint is invalid")
    return {key: value for key, value in raw.items() if key != "cache_control"}


def parse_system_blocks(value: list[object]) -> str:
    parts: list[str] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise RequestValidationError("system", "system blocks must be text")
        raw = without_cache_hint(raw)
        if (
            set(raw) != {"type", "text"}
            or raw.get("type") != "text"
            or not isinstance(raw.get("text"), str)
        ):
            raise RequestValidationError("system", "system blocks must be text")
        parts.append(cast(str, raw["text"]))
    return "".join(parts)


def parse_anthropic_tools(body: Mapping[str, object]) -> tuple[ToolDefinition, ...]:
    value = body.get("tools", [])
    if not isinstance(value, list):
        raise RequestValidationError("tools", "must be an array")
    definitions: list[ToolDefinition] = []
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) - _TOOL_FIELDS:
            raise RequestValidationError("tools", "definitions must be objects")
        name = raw.get("name")
        schema = raw.get("input_schema")
        description = raw.get("description", "")
        if not isinstance(name, str):
            raise RequestValidationError("tools", "name must be a string")
        if not isinstance(description, str):
            raise RequestValidationError("tools", "description must be a string")
        if not isinstance(schema, Mapping):
            raise RequestValidationError("tools", "input_schema must be an object")
        definitions.append(ToolDefinition(name, description, schema))
    return tuple(definitions)


def validate_anthropic_tool_choice(body: Mapping[str, object]) -> None:
    if "tool_choice" not in body:
        return
    value = body["tool_choice"]
    if not isinstance(value, Mapping) or set(value) - _TOOL_CHOICE_FIELDS:
        raise RequestValidationError("tool_choice", "must be a supported object")
    choice_type = value.get("type")
    if not isinstance(choice_type, str):
        raise RequestValidationError("tool_choice", "type must be a string")
    if (
        "disable_parallel_tool_use" in value
        and type(value["disable_parallel_tool_use"]) is not bool
    ):
        raise RequestValidationError(
            "tool_choice", "disable_parallel_tool_use must be a boolean"
        )
    if choice_type == "tool" and not isinstance(value.get("name"), str):
        raise RequestValidationError("tool_choice", "named choice requires a name")
    if choice_type == "auto" and "name" in value:
        raise RequestValidationError("tool_choice", "auto choice cannot name a tool")
    if choice_type == "auto" and not value.get("disable_parallel_tool_use", False):
        return
    if choice_type in {"auto", "any", "tool", "none"}:
        raise UnsupportedFeature("tool_choice", "choice is not supported")
    raise RequestValidationError("tool_choice", "type is not supported")


def parse_anthropic_messages(
    body: Mapping[str, object],
    *,
    subscription: bool = False,
) -> tuple[CanonicalMessage, ...]:
    value = body.get("messages")
    if not isinstance(value, list) or not value:
        raise RequestValidationError("messages", "must be a non-empty array")
    messages: list[CanonicalMessage] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise RequestValidationError("messages", "entries must be objects")
        extras = set(raw) - _MESSAGE_FIELDS
        if extras & _UNSUPPORTED_MESSAGE_FIELDS:
            raise UnsupportedFeature(
                "messages", "tool message fields are not supported"
            )
        if extras:
            raise RequestValidationError("messages", "message fields are not supported")
        role = raw.get("role")
        if not isinstance(role, str) or role not in {"user", "assistant"}:
            raise RequestValidationError("messages", "role must be user or assistant")
        parsed_role = cast(Role, role)
        messages.append(
            CanonicalMessage(
                parsed_role, _parse_content(raw, parsed_role, subscription=subscription)
            )
        )
    return tuple(messages)


def _parse_content(
    raw: Mapping[str, object], role: Role, *, subscription: bool = False
) -> str | tuple[CanonicalBlock, ...]:
    content = raw.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise RequestValidationError("messages", "content must be a string or array")
    # Empty refusal JSON and assembled SSE have no content blocks. Keep their
    # replay identical to the existing canonical empty assistant text entry.
    if not content and role == "assistant":
        return ""
    blocks = tuple(
        _parse_block(item, role, subscription=subscription) for item in content
    )
    return blocks


def _parse_block(
    raw: object, role: Role, *, subscription: bool = False
) -> CanonicalBlock:
    if not isinstance(raw, Mapping):
        raise RequestValidationError("messages", "content blocks must be objects")
    raw = without_cache_hint(raw)
    kind = raw.get("type")
    if isinstance(kind, str) and kind in {"thinking", "redacted_thinking"}:
        if role != "assistant":
            raise RequestValidationError(
                "messages", "thinking blocks require assistant"
            )
        if kind == "thinking":
            if set(raw) != {"type", "thinking", "signature"}:
                raise RequestValidationError(
                    "messages", "thinking block fields are invalid"
                )
            return ThinkingBlock(
                cast(str, raw["thinking"]), cast(str, raw["signature"])
            )
        if set(raw) != {"type", "data"}:
            raise RequestValidationError(
                "messages", "redacted thinking block fields are invalid"
            )
        return RedactedThinkingBlock(cast(str, raw["data"]))
    if kind == "image" and role == "user":
        return anthropic_image(raw)
    if kind == "text":
        if set(raw) - _TEXT_BLOCK_FIELDS or not isinstance(raw.get("text"), str):
            raise RequestValidationError("messages", "text blocks must contain text")
        return TextBlock(cast(str, raw["text"]))
    if kind == "tool_use":
        if role != "assistant":
            raise RequestValidationError(
                "messages", "tool use blocks require assistant"
            )
        if set(raw) - _TOOL_USE_FIELDS:
            raise RequestValidationError(
                "messages", "tool use block fields are invalid"
            )
        identifier, name, arguments = raw.get("id"), raw.get("name"), raw.get("input")
        if not isinstance(identifier, str) or (
            not subscription and _TOOL_ID.fullmatch(identifier) is None
        ):
            raise RequestValidationError("messages", "tool use ID is invalid")
        if not isinstance(name, str) or not isinstance(arguments, Mapping):
            raise RequestValidationError("messages", "tool use block is invalid")
        return ToolCallBlock(identifier, name, arguments)
    if kind == "tool_result":
        if role != "user":
            raise RequestValidationError("messages", "tool results require user")
        if set(raw) - _TOOL_RESULT_FIELDS:
            raise RequestValidationError(
                "messages", "tool result block fields are invalid"
            )
        identifier = raw.get("tool_use_id")
        is_error = raw.get("is_error", False)
        if not isinstance(identifier, str) or (
            not subscription and _TOOL_ID.fullmatch(identifier) is None
        ):
            raise RequestValidationError("messages", "tool result ID is invalid")
        if type(is_error) is not bool:
            raise RequestValidationError(
                "messages", "tool result error flag must be boolean"
            )
        return ToolResultBlock(identifier, _parse_result_content(raw), is_error)
    raise UnsupportedFeature("messages", "content block type is not supported")


def _parse_result_content(raw: Mapping[str, object]) -> tuple[str | ImageBlock, ...]:
    content = raw.get("content")
    if isinstance(content, str):
        return (content,)
    if not isinstance(content, list):
        raise RequestValidationError("messages", "tool result must be text or array")
    text: list[str | ImageBlock] = []
    for block in content:
        if isinstance(block, Mapping):
            block = without_cache_hint(block)
        if isinstance(block, Mapping) and block.get("type") == "image":
            text.append(anthropic_image(block))
            continue
        if not isinstance(block, Mapping) or block.get("type") != "text":
            raise UnsupportedFeature("messages", "tool result must be text or image")
        if set(block) - _TEXT_BLOCK_FIELDS or not isinstance(block.get("text"), str):
            raise RequestValidationError("messages", "text blocks must contain text")
        text.append(cast(str, block["text"]))
    return tuple(text)
