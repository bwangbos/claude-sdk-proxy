from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from claude_sdk_proxy.anthropic_tools import (
    parse_anthropic_messages,
    parse_anthropic_tools,
    validate_anthropic_tool_choice,
)
from claude_sdk_proxy.domain import (
    Completed,
    ConversationEvent,
    InputUsage,
    RequestValidationError,
    TextBlock,
    TextDelta,
    TextRequest,
    ToolCall,
    ToolDefinition,
)
from claude_sdk_proxy.text_api import (
    boolean,
    reject_fields,
    required_string,
    usage_counter,
)
from claude_sdk_proxy.tool_contract import JsonValue, canonical_json, plain_json

_SUPPORTED_FIELDS = {
    "model",
    "system",
    "messages",
    "max_tokens",
    "stream",
    "tools",
    "tool_choice",
}
_UNSUPPORTED_FIELDS = {
    "temperature",
    "top_p",
    "top_k",
    "stop_sequences",
    "thinking",
}


@dataclass(slots=True)
class AnthropicStreamState:
    tools_enabled: bool = False
    open_text_index: int | None = None
    next_block_index: int = 0


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
    tools = parse_anthropic_tools(body)
    validate_anthropic_tool_choice(body)
    messages = parse_anthropic_messages(body)
    try:
        return TextRequest(
            model,
            system,
            messages,
            max_tokens,
            stream,
            dialect="anthropic",
            tools=tools,
        )
    except RequestValidationError:
        raise
    except ValueError as error:
        raise RequestValidationError("messages", str(error)) from None


def render_anthropic_response(
    request_id: str,
    model: str,
    blocks: str | tuple[TextBlock | ToolCall, ...],
    completed: Completed,
) -> dict[str, object]:
    normalized = (TextBlock(blocks),) if isinstance(blocks, str) else blocks
    return {
        "id": request_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [_response_block(block) for block in normalized],
        "stop_reason": _anthropic_stop_reason(completed.stop_reason),
        "stop_sequence": None,
        "usage": _anthropic_usage(completed.usage),
    }


def encode_anthropic_start(
    request_id: str,
    model: str,
    input_tokens: int = 0,
    tools: tuple[ToolDefinition, ...] = (),
    state: AnthropicStreamState | None = None,
) -> tuple[bytes, ...]:
    tools_enabled = bool(tools)
    if state is not None:
        state.tools_enabled = tools_enabled
        state.open_text_index = None if tools_enabled else 0
        state.next_block_index = 0 if tools_enabled else 1
    start = _sse(
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
    )
    if tools_enabled:
        return (start,)
    return (start, _text_block_start(0))


def encode_anthropic_event(
    request_id: str,
    model: str,
    event: ConversationEvent,
    block_index: int = 0,
    state: AnthropicStreamState | None = None,
) -> tuple[bytes, ...]:
    del request_id, model
    enabled = state is not None and state.tools_enabled
    if isinstance(event, InputUsage):
        return ()
    if isinstance(event, TextDelta):
        if enabled:
            chunks: list[bytes] = []
            assert state is not None
            if state.open_text_index is None:
                state.open_text_index = state.next_block_index
                state.next_block_index += 1
                chunks.append(_text_block_start(state.open_text_index))
            chunks.append(_text_delta(state.open_text_index, event.text))
            return tuple(chunks)
        return (_text_delta(0, event.text),)
    if isinstance(event, ToolCall):
        chunks = []
        index = block_index
        if state is not None and state.open_text_index is not None:
            chunks.append(_content_block_stop(state.open_text_index))
            state.open_text_index = None
        if state is not None:
            index = state.next_block_index
            state.next_block_index += 1
        chunks.extend(_tool_call_events(event, index))
        return tuple(chunks)
    if not isinstance(event, Completed):
        raise TypeError("unsupported conversation event")
    close_index = None
    if state is not None:
        close_index = state.open_text_index
        state.open_text_index = None
    elif not enabled:
        close_index = 0
    terminal = (
        _sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": _anthropic_stop_reason(event.stop_reason),
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": usage_counter(event.usage, "output_tokens")},
            },
        ),
        _sse("message_stop", {"type": "message_stop"}),
    )
    return (
        () if close_index is None else (_content_block_stop(close_index),)
    ) + terminal


def encode_anthropic_error(code: str, message: str) -> tuple[bytes, ...]:
    return (
        _sse("error", {"type": "error", "error": {"type": code, "message": message}}),
    )


def _anthropic_stop_reason(reason: str | None) -> str:
    allowed = {
        "end_turn": "end_turn",
        "max_tokens": "max_tokens",
        "refusal": "refusal",
        "model_context_window_exceeded": "model_context_window_exceeded",
        "tool_use": "tool_use",
    }
    try:
        return allowed[reason]  # type: ignore[index]
    except KeyError:
        raise ValueError("unsupported stop reason") from None


def _anthropic_usage(usage: Mapping[str, Any] | None) -> dict[str, int]:
    return {
        "input_tokens": usage_counter(usage, "input_tokens"),
        "output_tokens": usage_counter(usage, "output_tokens"),
    }


def _response_block(block: TextBlock | ToolCall) -> dict[str, object]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolCall):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": plain_json(cast(JsonValue, block.arguments)),
        }
    raise TypeError("response blocks must be text or tool calls")


def _text_block_start(index: int) -> bytes:
    return _sse(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "text", "text": ""},
        },
    )


def _text_delta(index: int, text: str) -> bytes:
    return _sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        },
    )


def _content_block_stop(index: int) -> bytes:
    return _sse("content_block_stop", {"type": "content_block_stop", "index": index})


def _tool_call_events(event: ToolCall, index: int) -> tuple[bytes, ...]:
    return (
        _sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {
                    "type": "tool_use",
                    "id": event.id,
                    "name": event.name,
                    "input": {},
                },
            },
        ),
        _sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": canonical_json(cast(JsonValue, event.arguments)),
                },
            },
        ),
        _content_block_stop(index),
    )


def _sse(event: str, value: dict[str, object]) -> bytes:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {encoded}\n\n".encode()
