from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from claude_sdk_proxy.domain import (
    Completed,
    ConversationEvent,
    InputUsage,
    RequestValidationError,
    TextDelta,
    TextRequest,
)
from claude_sdk_proxy.text_api import (
    boolean,
    reject_fields,
    required_string,
    text_messages,
    usage_counter,
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
_TOOL_MESSAGE_FIELDS = {"tool_use_id", "tool_call_id", "tool_calls"}


def parse_anthropic_request(
    body: Mapping[str, object], allowed_models: frozenset[str]
) -> TextRequest:
    reject_fields(body, _SUPPORTED_FIELDS, _UNSUPPORTED_FIELDS)
    model = required_string(body, "model")
    if model not in allowed_models:
        raise RequestValidationError("model", "model is not configured")
    system = ""
    if "system" in body:
        system = required_string(body, "system")
    max_tokens = body.get("max_tokens")
    if type(max_tokens) is not int or max_tokens <= 0:
        raise RequestValidationError("max_tokens", "must be a positive integer")
    stream = boolean(body, "stream")
    _, messages = text_messages(body, _TOOL_MESSAGE_FIELDS, allow_system=False)
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


def encode_anthropic_start(
    request_id: str, model: str, input_tokens: int = 0
) -> tuple[bytes, ...]:
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
                    "usage": {"input_tokens": input_tokens, "output_tokens": 0},
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
    if isinstance(event, InputUsage):
        return ()
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
                    "output_tokens": usage_counter(event.usage, "output_tokens")
                },
            },
        ),
        _sse("message_stop", {"type": "message_stop"}),
    )


def encode_anthropic_error(code: str, message: str) -> tuple[bytes, ...]:
    return (
        _sse("error", {"type": "error", "error": {"type": code, "message": message}}),
    )


def _anthropic_stop_reason(reason: str | None) -> str:
    return "end_turn" if reason in {None, "end_turn"} else "max_tokens"


def _anthropic_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    return {
        "input_tokens": usage_counter(usage, "input_tokens"),
        "output_tokens": usage_counter(usage, "output_tokens"),
    }


def _sse(event: str, value: dict[str, object]) -> bytes:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {encoded}\n\n".encode()
