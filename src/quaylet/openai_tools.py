from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import cast

from quaylet.domain import (
    CanonicalBlock,
    CanonicalMessage,
    ImageBlock,
    RequestValidationError,
    TextBlock,
    ToolCallBlock,
    ToolDefinition,
    ToolResultBlock,
    UnsupportedFeature,
)
from quaylet.images import openai_image

_FUNCTION_TOOL_FIELDS = {"type", "function"}
_FUNCTION_FIELDS = {"name", "description", "parameters"}
_MESSAGE_FIELDS = {"role", "content"}
_REASONING_FIELDS = {"reasoning_content", "reasoning", "reasoning_text"}
_ASSISTANT_FIELDS = _MESSAGE_FIELDS | {"tool_calls"} | _REASONING_FIELDS
_TOOL_MESSAGE_FIELDS = {"role", "tool_call_id", "content"}
_TOOL_CALL_FIELDS = {"id", "type", "function"}
_TOOL_CALL_FUNCTION_FIELDS = {"name", "arguments"}
_TEXT_CONTENT_PART_FIELDS = {"type", "text"}
_CALL_ID = re.compile(r"call_[A-Za-z0-9_-]+")


def parse_openai_tools(body: Mapping[str, object]) -> tuple[ToolDefinition, ...]:
    value = body.get("tools", [])
    if not isinstance(value, list):
        raise RequestValidationError("tools", "must be an array")
    definitions: list[ToolDefinition] = []
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) - _FUNCTION_TOOL_FIELDS:
            raise RequestValidationError("tools", "definitions must be objects")
        if raw.get("type") != "function":
            raise UnsupportedFeature("tools", "only function tools are supported")
        function = raw.get("function")
        if not isinstance(function, Mapping) or set(function) - _FUNCTION_FIELDS:
            raise RequestValidationError("tools", "function definitions are invalid")
        name = function.get("name")
        description = function.get("description", "")
        parameters = function.get("parameters")
        if not isinstance(name, str):
            raise RequestValidationError("tools", "function name must be a string")
        if not isinstance(description, str):
            raise RequestValidationError("tools", "description must be a string")
        if not isinstance(parameters, Mapping):
            raise RequestValidationError("tools", "parameters must be an object")
        definitions.append(ToolDefinition(name, description, parameters))
    return tuple(definitions)


def validate_openai_tool_controls(body: Mapping[str, object]) -> None:
    if "tool_choice" in body:
        choice = body["tool_choice"]
        if choice == "auto":
            pass
        elif isinstance(choice, Mapping) or (
            isinstance(choice, str) and choice in {"required", "none"}
        ):
            raise UnsupportedFeature("tool_choice", "choice is not supported")
        else:
            raise RequestValidationError("tool_choice", "must be 'auto' or an object")
    if "parallel_tool_calls" in body:
        parallel = body["parallel_tool_calls"]
        if type(parallel) is not bool:
            raise RequestValidationError("parallel_tool_calls", "must be a boolean")
        if not parallel:
            raise UnsupportedFeature("parallel_tool_calls", "false is not supported")


def parse_openai_messages(
    body: Mapping[str, object],
    *,
    subscription: bool = False,
) -> tuple[str, tuple[CanonicalMessage, ...]]:
    value = body.get("messages")
    if not isinstance(value, list) or not value:
        raise RequestValidationError("messages", "must be a non-empty array")
    system = ""
    messages: list[CanonicalMessage] = []
    index = 0
    while index < len(value):
        raw = value[index]
        if not isinstance(raw, Mapping):
            raise RequestValidationError("messages", "entries must be objects")
        role = raw.get("role")
        if role == "system":
            system = _system_message(raw, index)
            index += 1
        elif role == "user":
            if isinstance(raw.get("content"), list):
                if set(raw) - _MESSAGE_FIELDS:
                    raise _message_field_error(raw)
                messages.append(CanonicalMessage("user", _user_parts(raw["content"])))
            else:
                messages.append(CanonicalMessage("user", _text_content(raw, "user")))
            index += 1
        elif role == "assistant":
            messages.append(_assistant_message(raw, subscription=subscription))
            index += 1
        elif role == "tool":
            results, index = _tool_results(value, index, subscription=subscription)
            messages.append(CanonicalMessage("user", results))
        elif role in {"function"}:
            raise UnsupportedFeature("messages", "tool roles are not supported")
        elif not isinstance(role, str):
            raise RequestValidationError("messages", "role must be a string")
        else:
            raise RequestValidationError("messages", "role must be user or assistant")
    return system, tuple(messages)


def _system_message(raw: Mapping[str, object], index: int) -> str:
    if index:
        raise RequestValidationError("messages", "system must be the first message")
    return _text_content(raw, "system")


def _text_content(raw: Mapping[str, object], role: str) -> str:
    if set(raw) - _MESSAGE_FIELDS:
        raise _message_field_error(raw)
    content = raw.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return _text_content_parts(content)
    raise RequestValidationError("messages", f"{role} content must be a string")


def _text_content_parts(content: list[object]) -> str:
    text: list[str] = []
    for raw in content:
        if not isinstance(raw, Mapping):
            raise RequestValidationError("messages", "content blocks must be objects")
        if raw.get("type") != "text":
            raise UnsupportedFeature(
                "messages", "only text content blocks are supported"
            )
        if set(raw) - _TEXT_CONTENT_PART_FIELDS:
            raise RequestValidationError(
                "messages", "text content block fields are invalid"
            )
        value = raw.get("text")
        if not isinstance(value, str):
            raise RequestValidationError(
                "messages", "text content block text must be a string"
            )
        text.append(value)
    return "".join(text)


def _user_parts(content: object) -> tuple[TextBlock | ImageBlock, ...]:
    if not isinstance(content, list):
        raise RequestValidationError("messages", "content must be an array")
    parts: list[TextBlock | ImageBlock] = []
    for raw in content:
        if isinstance(raw, Mapping) and raw.get("type") == "image_url":
            parts.append(openai_image(raw))
        else:
            parts.append(TextBlock(_text_content_parts([raw])))
    if parts and all(isinstance(part, TextBlock) for part in parts):
        return (
            TextBlock(
                "".join(part.text for part in parts if isinstance(part, TextBlock))
            ),
        )
    return tuple(parts)


def _assistant_message(
    raw: Mapping[str, object], *, subscription: bool = False
) -> CanonicalMessage:
    # SDK response dumps include these fields even when the feature is absent.
    nullable_metadata = {"refusal", "annotations", "audio", "function_call"}
    raw = {
        key: value
        for key, value in raw.items()
        if key not in nullable_metadata or value is not None
    }
    if subscription and "refusal" in raw:
        refusal = raw["refusal"]
        if not isinstance(refusal, str):
            raise RequestValidationError("messages", "refusal must be a string or null")
        raw = {**raw, "content": _assistant_text(raw) + refusal}
        del raw["refusal"]
    if set(raw) - _ASSISTANT_FIELDS:
        raise _message_field_error(raw)
    for field in _REASONING_FIELDS:
        if raw.get(field) is not None and not isinstance(raw[field], str):
            raise RequestValidationError(
                "messages", "reasoning metadata must be a string or null"
            )
    # Pi replays these unsigned fields. They are display metadata, not native
    # signed thinking and not answer text; leave them out of canonical history.
    calls = raw.get("tool_calls")
    if calls is None:
        return CanonicalMessage("assistant", _assistant_text(raw))
    if not isinstance(calls, list) or not calls:
        raise RequestValidationError("messages", "tool_calls must be a non-empty array")
    blocks: list[CanonicalBlock] = []
    content = raw.get("content")
    if isinstance(content, str):
        if content:
            blocks.append(TextBlock(content))
    elif isinstance(content, list):
        text = _text_content_parts(content)
        if text:
            blocks.append(TextBlock(text))
    elif content is not None:
        raise RequestValidationError("messages", "assistant content must be a string")
    blocks.extend(_tool_call(item, subscription=subscription) for item in calls)
    return CanonicalMessage("assistant", tuple(blocks))


def _assistant_text(raw: Mapping[str, object]) -> str:
    content = raw.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return _text_content_parts(content)
    raise RequestValidationError("messages", "assistant content must be a string")


def _tool_call(raw: object, *, subscription: bool = False) -> ToolCallBlock:
    if not isinstance(raw, Mapping) or set(raw) - _TOOL_CALL_FIELDS:
        raise RequestValidationError("messages", "tool calls must be objects")
    identifier = raw.get("id")
    if not isinstance(identifier, str) or (
        not subscription and _CALL_ID.fullmatch(identifier) is None
    ):
        raise RequestValidationError("messages", "tool call ID is invalid")
    if raw.get("type") != "function":
        raise UnsupportedFeature("messages", "only function calls are supported")
    function = raw.get("function")
    if not isinstance(function, Mapping) or set(function) - _TOOL_CALL_FUNCTION_FIELDS:
        raise RequestValidationError("messages", "tool call function is invalid")
    name, encoded = function.get("name"), function.get("arguments")
    if not isinstance(name, str) or not isinstance(encoded, str):
        raise RequestValidationError("messages", "tool call function is invalid")
    try:
        arguments = json.loads(encoded)
    except json.JSONDecodeError, RecursionError:
        raise RequestValidationError(
            "messages", "tool call arguments must be JSON"
        ) from None
    if not isinstance(arguments, Mapping):
        raise RequestValidationError(
            "messages", "tool call arguments must be an object"
        )
    return ToolCallBlock(identifier, name, cast(Mapping[str, object], arguments))


def _tool_results(
    value: list[object], index: int, *, subscription: bool = False
) -> tuple[tuple[ToolResultBlock, ...], int]:
    results: list[ToolResultBlock] = []
    while index < len(value):
        raw = value[index]
        if not isinstance(raw, Mapping):
            raise RequestValidationError("messages", "entries must be objects")
        if raw.get("role") != "tool":
            break
        if set(raw) - _TOOL_MESSAGE_FIELDS:
            raise _message_field_error(raw)
        identifier, content = raw.get("tool_call_id"), raw.get("content")
        if not isinstance(identifier, str) or (
            not subscription and _CALL_ID.fullmatch(identifier) is None
        ):
            raise RequestValidationError("messages", "tool result ID is invalid")
        if isinstance(content, list):
            parts = tuple(
                part.text if isinstance(part, TextBlock) else part
                for part in _user_parts(content)
            )
        elif isinstance(content, str):
            parts = (content,)
        else:
            raise RequestValidationError(
                "messages", "tool result content must be a string"
            )
        results.append(ToolResultBlock(identifier, parts, False))
        index += 1
    return tuple(results), index


def _message_field_error(raw: Mapping[str, object]) -> ValueError:
    if {"tool_call_id", "tool_calls", "function_call"} & set(raw):
        return UnsupportedFeature("messages", "tool message fields are not supported")
    return RequestValidationError("messages", "message fields are not supported")
