from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

from claude_sdk_proxy.domain import (
    CanonicalMessage,
    Completed,
    ConversationEvent,
    RequestValidationError,
    Role,
    TextDelta,
    TextRequest,
    UnsupportedFeature,
)

_SUPPORTED_FIELDS = {"model", "system", "messages", "max_tokens", "stream"}
_UNSUPPORTED_FIELDS = {
    "temperature",
    "top_p",
    "top_k",
    "stop_sequences",
    "tools",
    "tool_choice",
    "thinking",
}
_MESSAGE_FIELDS = {"role", "content"}
_TOOL_MESSAGE_FIELDS = {"tool_use_id", "tool_call_id", "tool_calls"}


def parse_anthropic_request(
    body: Mapping[str, object], allowed_models: frozenset[str]
) -> TextRequest:
    _reject_fields(body)
    model = _required_string(body, "model")
    if model not in allowed_models:
        raise RequestValidationError("model", "model is not configured")
    system = ""
    if "system" in body:
        system = _required_string(body, "system")
    max_tokens = body.get("max_tokens")
    if type(max_tokens) is not int or max_tokens <= 0:
        raise RequestValidationError("max_tokens", "must be a positive integer")
    stream = _boolean(body, "stream", default=False)
    messages = _messages(body)
    try:
        return TextRequest(model, system, tuple(messages), max_tokens, stream)
    except ValueError as error:
        raise RequestValidationError("messages", str(error)) from None


def render_anthropic_response(
    request_id: str, model: str, text: str, completed: Completed
) -> dict[str, object]:
    return {
        "id": request_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": _anthropic_stop_reason(completed.stop_reason),
        "stop_sequence": None,
        "usage": _anthropic_usage(completed.usage),
    }


def encode_anthropic_start(request_id: str, model: str) -> tuple[bytes, ...]:
    return (
        _sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": request_id,
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        ),
        _sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
    )


def encode_anthropic_event(
    request_id: str, model: str, event: ConversationEvent
) -> tuple[bytes, ...]:
    del request_id, model
    if isinstance(event, TextDelta):
        return (
            _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": event.text},
                },
            ),
        )
    if not isinstance(event, Completed):
        raise TypeError("unsupported conversation event")
    return (
        _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": _anthropic_stop_reason(event.stop_reason),
                    "stop_sequence": None,
                },
                "usage": {
                    "output_tokens": _usage_counter(event.usage, "output_tokens")
                },
            },
        ),
        _sse("message_stop", {"type": "message_stop"}),
    )


def encode_anthropic_error(code: str, message: str) -> tuple[bytes, ...]:
    return (
        _sse("error", {"type": "error", "error": {"type": code, "message": message}}),
    )


def _reject_fields(body: Mapping[str, object]) -> None:
    for field in body:
        if field in _UNSUPPORTED_FIELDS:
            raise UnsupportedFeature(field, "not supported by the text gateway")
        if field not in _SUPPORTED_FIELDS:
            raise RequestValidationError(field, "field is not supported")


def _required_string(body: Mapping[str, object], field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str):
        raise RequestValidationError(field, "must be a string")
    if field == "model" and not value:
        raise RequestValidationError(field, "must not be empty")
    return value


def _boolean(body: Mapping[str, object], field: str, *, default: bool) -> bool:
    if field not in body:
        return default
    value = body[field]
    if type(value) is not bool:
        raise RequestValidationError(field, "must be a boolean")
    return value


def _messages(body: Mapping[str, object]) -> list[CanonicalMessage]:
    value = body.get("messages")
    if not isinstance(value, list) or not value:
        raise RequestValidationError("messages", "must be a non-empty array")
    messages: list[CanonicalMessage] = []
    for raw_message in value:
        if not isinstance(raw_message, Mapping):
            raise RequestValidationError("messages", "entries must be objects")
        extras = set(raw_message) - _MESSAGE_FIELDS
        if extras & _TOOL_MESSAGE_FIELDS:
            raise UnsupportedFeature(
                "messages", "tool message fields are not supported"
            )
        if extras:
            raise RequestValidationError("messages", "message fields are not supported")
        role = raw_message.get("role")
        content = raw_message.get("content")
        if role in {"tool", "function"}:
            raise UnsupportedFeature("messages", "tool roles are not supported")
        if role not in {"user", "assistant"}:
            raise RequestValidationError("messages", "role must be user or assistant")
        if not isinstance(content, str):
            if isinstance(content, list):
                raise UnsupportedFeature("messages", "content blocks are not supported")
            raise RequestValidationError("messages", "content must be a string")
        messages.append(CanonicalMessage(cast(Role, role), content))
    return messages


def _anthropic_stop_reason(reason: str | None) -> str:
    return "end_turn" if reason in {None, "end_turn"} else "max_tokens"


def _anthropic_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    return {
        "input_tokens": _usage_counter(usage, "input_tokens"),
        "output_tokens": _usage_counter(usage, "output_tokens"),
    }


def _usage_counter(usage: dict[str, Any] | None, field: str) -> int:
    value = 0 if usage is None else usage.get(field, 0)
    return value if type(value) is int and value >= 0 else 0


def _sse(event: str, value: dict[str, object]) -> bytes:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {encoded}\n\n".encode()
