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

_SUPPORTED_FIELDS = {
    "model",
    "messages",
    "max_tokens",
    "max_completion_tokens",
    "stream",
    "store",
    "stream_options",
}
_UNSUPPORTED_FIELDS = {
    "temperature",
    "top_p",
    "top_k",
    "stop",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "response_format",
    "modalities",
    "audio",
    "reasoning_effort",
}
_MESSAGE_FIELDS = {"role", "content"}
_TOOL_MESSAGE_FIELDS = {"tool_calls", "tool_call_id", "function_call"}


def parse_openai_request(
    body: Mapping[str, object], allowed_models: frozenset[str]
) -> TextRequest:
    _reject_fields(body)
    model = _string(body, "model", required=True)
    if model not in allowed_models:
        raise RequestValidationError("model", "model is not configured")
    stream = _boolean(body, "stream", default=False)
    if "store" in body and body["store"] is not False:
        raise RequestValidationError("store", "must be exactly false")
    include_usage = _stream_options(body)
    max_tokens = _openai_max_tokens(body)
    system, messages = _messages(body)
    try:
        return TextRequest(
            model, system, tuple(messages), max_tokens, stream, include_usage
        )
    except ValueError as error:
        raise RequestValidationError("messages", str(error)) from None


def render_openai_response(
    request_id: str, model: str, text: str, completed: Completed
) -> dict[str, object]:
    return {
        "id": request_id,
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "logprobs": None,
                "finish_reason": _openai_stop_reason(completed.stop_reason),
            }
        ],
        "usage": _openai_usage(completed.usage),
    }


def encode_openai_start(request_id: str, model: str) -> tuple[bytes, ...]:
    return (_sse(_chunk(request_id, model, {"role": "assistant", "content": ""})),)


def encode_openai_event(
    request_id: str, model: str, event: ConversationEvent, include_usage: bool
) -> tuple[bytes, ...]:
    if isinstance(event, TextDelta):
        return (_sse(_chunk(request_id, model, {"content": event.text})),)
    if not isinstance(event, Completed):
        raise TypeError("unsupported conversation event")
    terminal = _chunk(
        request_id,
        model,
        {},
        finish_reason=_openai_stop_reason(event.stop_reason),
    )
    chunks = [_sse(terminal)]
    if include_usage:
        chunks.append(
            _sse(
                {
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": model,
                    "choices": [],
                    "usage": _openai_usage(event.usage),
                }
            )
        )
    chunks.append(b"data: [DONE]\n\n")
    return tuple(chunks)


def encode_openai_error(code: str, message: str) -> tuple[bytes, ...]:
    return (
        _sse({"error": {"message": message, "type": code, "code": code}}),
        b"data: [DONE]\n\n",
    )


def _reject_fields(body: Mapping[str, object]) -> None:
    for field in body:
        if field in _UNSUPPORTED_FIELDS:
            raise UnsupportedFeature(field, "not supported by the text gateway")
        if field not in _SUPPORTED_FIELDS:
            raise RequestValidationError(field, "field is not supported")


def _string(body: Mapping[str, object], field: str, *, required: bool) -> str:
    value = body.get(field)
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise RequestValidationError(field, "must be a string")
    if required and not value:
        raise RequestValidationError(field, "must not be empty")
    return value


def _boolean(body: Mapping[str, object], field: str, *, default: bool) -> bool:
    if field not in body:
        return default
    value = body[field]
    if type(value) is not bool:
        raise RequestValidationError(field, "must be a boolean")
    return value


def _openai_max_tokens(body: Mapping[str, object]) -> int | None:
    names = [name for name in ("max_tokens", "max_completion_tokens") if name in body]
    if len(names) > 1:
        raise RequestValidationError(names[1], "cannot be used with max_tokens")
    if not names:
        return None
    value = body[names[0]]
    if type(value) is not int or value <= 0:
        raise RequestValidationError(names[0], "must be a positive integer")
    return value


def _stream_options(body: Mapping[str, object]) -> bool:
    if "stream_options" not in body:
        return False
    value = body["stream_options"]
    if not isinstance(value, Mapping) or set(value) != {"include_usage"}:
        raise RequestValidationError(
            "stream_options", "must be exactly {'include_usage': boolean}"
        )
    include_usage = value["include_usage"]
    if type(include_usage) is not bool:
        raise RequestValidationError("stream_options", "include_usage must be boolean")
    return include_usage


def _messages(body: Mapping[str, object]) -> tuple[str, list[CanonicalMessage]]:
    value = body.get("messages")
    if not isinstance(value, list) or not value:
        raise RequestValidationError("messages", "must be a non-empty array")
    system = ""
    messages: list[CanonicalMessage] = []
    for index, raw_message in enumerate(value):
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
        if not isinstance(role, str):
            raise RequestValidationError("messages", "role must be a string")
        if not isinstance(content, str):
            if isinstance(content, list):
                raise UnsupportedFeature("messages", "content blocks are not supported")
            raise RequestValidationError("messages", "content must be a string")
        if role == "system":
            if index != 0:
                raise RequestValidationError(
                    "messages", "system must be the first message"
                )
            system = content
            continue
        if role in {"tool", "function"}:
            raise UnsupportedFeature("messages", "tool roles are not supported")
        if role not in {"user", "assistant"}:
            raise RequestValidationError("messages", "role must be user or assistant")
        messages.append(CanonicalMessage(cast(Role, role), content))
    return system, messages


def _chunk(
    request_id: str,
    model: str,
    delta: dict[str, str],
    *,
    finish_reason: str | None = None,
) -> dict[str, object]:
    return {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "logprobs": None,
                "finish_reason": finish_reason,
            }
        ],
    }


def _openai_stop_reason(reason: str | None) -> str:
    return "stop" if reason in {None, "end_turn"} else "length"


def _openai_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    input_tokens = _usage_counter(usage, "input_tokens")
    output_tokens = _usage_counter(usage, "output_tokens")
    return {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def _usage_counter(usage: dict[str, Any] | None, field: str) -> int:
    value = 0 if usage is None else usage.get(field, 0)
    return value if type(value) is int and value >= 0 else 0


def _sse(value: dict[str, object]) -> bytes:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return f"data: {encoded}\n\n".encode()
