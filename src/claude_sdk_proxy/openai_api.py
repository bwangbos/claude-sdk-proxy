from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from claude_sdk_proxy.domain import (
    Completed,
    ConversationEvent,
    InputUsage,
    RedactedThinkingBlock,
    RefusalDelta,
    RequestValidationError,
    ResponseIdentity,
    TextBlock,
    TextDelta,
    TextRequest,
    ThinkingBlock,
    ThinkingCompleted,
    ThinkingDelta,
    ToolCall,
)
from claude_sdk_proxy.model_catalog import canonical_model
from claude_sdk_proxy.openai_subscription.translation import (
    MODELS,
    parse_subscription_thinking,
)
from claude_sdk_proxy.openai_tools import (
    parse_openai_messages,
    parse_openai_tools,
    validate_openai_tool_controls,
)
from claude_sdk_proxy.text_api import (
    boolean,
    reject_fields,
    required_string,
    usage_counter,
)
from claude_sdk_proxy.thinking import parse_openai_thinking
from claude_sdk_proxy.tool_contract import JsonValue, canonical_json

_SUPPORTED_FIELDS = {
    "model",
    "messages",
    "max_tokens",
    "max_completion_tokens",
    "stream",
    "store",
    "stream_options",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "user",
    "reasoning_effort",
}
_UNSUPPORTED_FIELDS = {
    "temperature",
    "top_p",
    "top_k",
    "stop",
    "response_format",
    "modalities",
    "audio",
}
_NULLABLE_OPTIONAL_FIELDS = (_SUPPORTED_FIELDS | _UNSUPPORTED_FIELDS) - {
    "model",
    "messages",
    "user",
}


@dataclass(slots=True)
class OpenAIStreamState:
    next_tool_index: int = 0


def parse_openai_request(
    body: Mapping[str, object], allowed_models: frozenset[str]
) -> TextRequest:
    body = {
        field: value
        for field, value in body.items()
        if value is not None or field not in _NULLABLE_OPTIONAL_FIELDS
    }
    reject_fields(body, _SUPPORTED_FIELDS, _UNSUPPORTED_FIELDS)
    user = body.get("user")
    if user is not None and not isinstance(user, str):
        raise RequestValidationError("user", "must be a string or null")
    model = canonical_model(required_string(body, "model"))
    if model not in {canonical_model(value) for value in allowed_models}:
        raise RequestValidationError("model", "model is not configured")
    thinking = (
        parse_subscription_thinking(body, "openai")
        if model in MODELS
        else parse_openai_thinking(body, model)
    )
    stream = boolean(body, "stream")
    if "store" in body and body["store"] is not False:
        raise RequestValidationError("store", "must be exactly false")
    include_usage = _stream_options(body)
    max_tokens = _openai_max_tokens(body)
    tools = parse_openai_tools(body)
    validate_openai_tool_controls(body)
    system, messages = parse_openai_messages(body, subscription=model in MODELS)
    try:
        return TextRequest(
            model,
            system,
            messages,
            max_tokens,
            stream,
            include_usage,
            dialect="openai",
            tools=tools,
            thinking=thinking,
        )
    except RequestValidationError:
        raise
    except ValueError as error:
        raise RequestValidationError("messages", str(error)) from None


def render_openai_response(
    request_id: str,
    model: str,
    blocks: str
    | tuple[TextBlock | ToolCall | ThinkingBlock | RedactedThinkingBlock, ...],
    completed: Completed,
    *,
    created: int = 0,
) -> dict[str, object]:
    normalized = (TextBlock(blocks),) if isinstance(blocks, str) else blocks
    message = _response_message(normalized)
    if completed.refusal is not None:
        message["refusal"] = completed.refusal
    return {
        "id": request_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "logprobs": None,
                "finish_reason": _openai_stop_reason(completed.stop_reason),
            }
        ],
        "usage": None
        if completed.provider == "openai-subscription" and completed.usage is None
        else _openai_usage(completed.usage),
    }


def encode_openai_start(
    request_id: str, model: str, *, created: int = 0
) -> tuple[bytes, ...]:
    return (
        _sse(
            _chunk(
                request_id,
                model,
                {"role": "assistant", "content": ""},
                created=created,
            )
        ),
    )


def encode_openai_event(
    request_id: str,
    model: str,
    event: ConversationEvent,
    include_usage: bool,
    state: OpenAIStreamState | None = None,
    *,
    created: int = 0,
) -> tuple[bytes, ...]:
    if isinstance(event, (InputUsage, ResponseIdentity)):
        return ()
    if isinstance(event, ThinkingCompleted):
        return ()
    if isinstance(event, ThinkingDelta):
        return (
            _sse(
                _chunk(
                    request_id,
                    model,
                    {"reasoning_content": event.text},
                    created=created,
                )
            ),
        )
    if isinstance(event, TextDelta):
        return (
            _sse(_chunk(request_id, model, {"content": event.text}, created=created)),
        )
    if isinstance(event, RefusalDelta):
        return (
            _sse(_chunk(request_id, model, {"refusal": event.text}, created=created)),
        )
    if isinstance(event, ToolCall):
        index = 0 if state is None else state.next_tool_index
        if state is not None:
            state.next_tool_index += 1
        return (
            _sse(
                _chunk(
                    request_id,
                    model,
                    {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": event.id,
                                "type": "function",
                                "function": {
                                    "name": event.name,
                                    "arguments": canonical_json(
                                        cast(JsonValue, event.arguments)
                                    ),
                                },
                            }
                        ]
                    },
                    created=created,
                )
            ),
        )
    if not isinstance(event, Completed):
        raise TypeError("unsupported conversation event")
    terminal = _chunk(
        request_id,
        model,
        {},
        finish_reason=_openai_stop_reason(event.stop_reason),
        created=created,
    )
    chunks = [_sse(terminal)]
    if include_usage:
        chunks.append(
            _sse(
                {
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [],
                    "usage": None
                    if event.provider == "openai-subscription" and event.usage is None
                    else _openai_usage(event.usage),
                }
            )
        )
    chunks.append(b"data: [DONE]\n\n")
    return tuple(chunks)


def encode_openai_error(code: str, message: str) -> tuple[bytes, ...]:
    return (_sse({"error": {"message": message, "type": code, "code": code}}),)


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
    delta: dict[str, object],
    *,
    finish_reason: str | None = None,
    created: int = 0,
) -> dict[str, object]:
    return {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": created,
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
        "tool_use": "tool_calls",
    }
    try:
        return mapping[reason]  # type: ignore[index]
    except KeyError:
        raise ValueError("unsupported stop reason") from None


def _openai_usage(usage: Mapping[str, Any] | None) -> dict[str, object]:
    input_tokens = usage_counter(usage, "input_tokens")
    output_tokens = usage_counter(usage, "output_tokens")
    cache_read = usage_counter(usage, "cache_read_input_tokens")
    cache_creation = usage_counter(usage, "cache_creation_input_tokens")
    prompt_tokens = input_tokens + cache_read + cache_creation
    normalized: dict[str, object] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": prompt_tokens + output_tokens,
    }
    details: dict[str, int] = {}
    if usage is not None and "cache_read_input_tokens" in usage:
        details["cached_tokens"] = cache_read
    if usage is not None and "cache_creation_input_tokens" in usage:
        details["cache_write_tokens"] = cache_creation
    if details:
        normalized["prompt_tokens_details"] = details
    if usage is not None and "reasoning_tokens" in usage:
        normalized["completion_tokens_details"] = {
            "reasoning_tokens": usage_counter(usage, "reasoning_tokens")
        }
    return normalized


def _response_message(
    blocks: tuple[TextBlock | ToolCall | ThinkingBlock | RedactedThinkingBlock, ...],
) -> dict[str, object]:
    text: list[str] = []
    thinking: list[str] = []
    calls: list[dict[str, object]] = []
    for block in blocks:
        if isinstance(block, TextBlock):
            text.append(block.text)
        elif isinstance(block, ThinkingBlock):
            thinking.append(block.thinking)
        elif isinstance(block, RedactedThinkingBlock):
            continue
        elif isinstance(block, ToolCall):
            calls.append(
                {
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": canonical_json(cast(JsonValue, block.arguments)),
                    },
                }
            )
        else:
            raise TypeError("response blocks must be text or tool calls")
    message: dict[str, object] = {"role": "assistant", "content": "".join(text)}
    if not text:
        message["content"] = None
    if calls:
        message["tool_calls"] = calls
    if thinking:
        message["reasoning_content"] = "".join(thinking)
    return message


def _sse(value: dict[str, object]) -> bytes:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return f"data: {encoded}\n\n".encode()
