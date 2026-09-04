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
_TOOL_MESSAGE_FIELDS = {"tool_calls", "tool_call_id", "function_call"}


def parse_openai_request(
    body: Mapping[str, object], allowed_models: frozenset[str]
) -> TextRequest:
    reject_fields(body, _SUPPORTED_FIELDS, _UNSUPPORTED_FIELDS)
    model = required_string(body, "model")
    if model not in allowed_models:
        raise RequestValidationError("model", "model is not configured")
    stream = boolean(body, "stream")
    if "store" in body and body["store"] is not False:
        raise RequestValidationError("store", "must be exactly false")
    include_usage = _stream_options(body)
    max_tokens = _openai_max_tokens(body)
    system, messages = text_messages(body, _TOOL_MESSAGE_FIELDS, allow_system=True)
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
    if isinstance(event, InputUsage):
        return ()
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
    mapping = {
        "end_turn": "stop",
        "max_tokens": "length",
        "refusal": "content_filter",
        "model_context_window_exceeded": "length",
    }
    try:
        return mapping[reason]  # type: ignore[index]
    except KeyError:
        raise ValueError("unsupported stop reason") from None


def _openai_usage(usage: Mapping[str, Any] | None) -> dict[str, int]:
    input_tokens = usage_counter(usage, "input_tokens")
    output_tokens = usage_counter(usage, "output_tokens")
    return {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
def _sse(value: dict[str, object]) -> bytes:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return f"data: {encoded}\n\n".encode()
