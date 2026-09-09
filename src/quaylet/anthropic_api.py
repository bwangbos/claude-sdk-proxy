from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, cast

from quaylet.anthropic_tools import (
    parse_anthropic_messages,
    parse_anthropic_tools,
    parse_system_blocks,
    validate_anthropic_tool_choice,
)
from quaylet.domain import (
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
    ToolDefinition,
)
from quaylet.model_catalog import canonical_model
from quaylet.openai_subscription.translation import (
    MODELS,
    parse_subscription_thinking,
)
from quaylet.text_api import (
    boolean,
    reject_fields,
    required_string,
    usage_counter,
)
from quaylet.thinking import parse_anthropic_thinking
from quaylet.tool_contract import JsonValue, canonical_json, plain_json

_SUPPORTED_FIELDS = {
    "model",
    "system",
    "messages",
    "max_tokens",
    "stream",
    "tools",
    "tool_choice",
    "thinking",
    "output_config",
}
_UNSUPPORTED_FIELDS = {
    "temperature",
    "top_p",
    "top_k",
    "stop_sequences",
}


@dataclass(slots=True)
class AnthropicStreamState:
    tools_enabled: bool = False
    open_text_index: int | None = None
    next_block_index: int = 0
    lazy_blocks: bool = False
    open_thinking_index: int | None = None
    subscription: bool = False
    thinking_indices: dict[int, int] = dataclass_field(default_factory=dict)


def parse_anthropic_request(
    body: Mapping[str, object], allowed_models: frozenset[str]
) -> TextRequest:
    reject_fields(body, _SUPPORTED_FIELDS, _UNSUPPORTED_FIELDS)
    model = canonical_model(required_string(body, "model"))
    if model not in {canonical_model(value) for value in allowed_models}:
        raise RequestValidationError("model", "model is not configured")
    thinking = (
        parse_subscription_thinking(body, "anthropic")
        if model in MODELS
        else parse_anthropic_thinking(body, model)
    )
    system = ""
    if "system" in body:
        value = body["system"]
        system = (
            parse_system_blocks(value)
            if isinstance(value, list)
            else required_string(body, "system")
        )
    max_tokens = body.get("max_tokens")
    if type(max_tokens) is not int or max_tokens <= 0:
        raise RequestValidationError("max_tokens", "must be a positive integer")
    if thinking.budget_tokens is not None and thinking.budget_tokens >= max_tokens:
        raise RequestValidationError(
            "thinking", "budget_tokens must be less than max_tokens"
        )
    stream = boolean(body, "stream")
    tools = parse_anthropic_tools(body)
    validate_anthropic_tool_choice(body)
    messages = parse_anthropic_messages(body, subscription=model in MODELS)
    try:
        return TextRequest(
            model,
            system,
            messages,
            max_tokens,
            stream,
            dialect="anthropic",
            tools=tools,
            thinking=thinking,
        )
    except RequestValidationError:
        raise
    except ValueError as error:
        raise RequestValidationError("messages", str(error)) from None


def render_anthropic_response(
    request_id: str,
    model: str,
    blocks: str
    | tuple[TextBlock | ToolCall | ThinkingBlock | RedactedThinkingBlock, ...],
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
        "usage": None
        if completed.provider == "openai-subscription" and completed.usage is None
        else _anthropic_usage(completed.usage),
    }


def encode_anthropic_start(
    request_id: str,
    model: str,
    input_tokens: int | InputUsage = 0,
    tools: tuple[ToolDefinition, ...] = (),
    state: AnthropicStreamState | None = None,
) -> tuple[bytes, ...]:
    tools_enabled = bool(tools) or (state is not None and state.lazy_blocks)
    if state is not None:
        state.tools_enabled = tools_enabled
        state.open_text_index = None if tools_enabled else 0
        state.next_block_index = 0 if tools_enabled else 1
    input_usage = (
        _input_usage(input_tokens)
        if isinstance(input_tokens, InputUsage)
        else {"input_tokens": input_tokens}
    )
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
                "usage": {"input_tokens": None, "output_tokens": None}
                if state is not None and state.subscription
                else {**input_usage, "output_tokens": 0},
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
    if isinstance(event, (InputUsage, ResponseIdentity)):
        return ()
    if isinstance(event, (ThinkingDelta, ThinkingCompleted)):
        if state is None:
            raise ValueError("thinking streaming requires block state")
        chunks: list[bytes] = []
        if state.subscription:
            index = state.thinking_indices.get(event.index)
            if index is None:
                index = state.next_block_index
                state.next_block_index += 1
                state.thinking_indices[event.index] = index
                block = event.block if isinstance(event, ThinkingCompleted) else None
                content = (
                    _response_block(block)
                    if isinstance(block, RedactedThinkingBlock)
                    else {"type": "thinking", "thinking": "", "signature": ""}
                )
                chunks.append(
                    _sse(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": index,
                            "content_block": content,
                        },
                    )
                )
            if isinstance(event, ThinkingDelta):
                chunks.append(
                    _sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {"type": "thinking_delta", "thinking": event.text},
                        },
                    )
                )
            else:
                if isinstance(event.block, ThinkingBlock):
                    chunks.append(
                        _sse(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": index,
                                "delta": {
                                    "type": "signature_delta",
                                    "signature": event.block.signature,
                                },
                            },
                        )
                    )
                chunks.append(_content_block_stop(index))
                del state.thinking_indices[event.index]
            return tuple(chunks)
        if state.open_text_index is not None:
            chunks.append(_content_block_stop(state.open_text_index))
            state.open_text_index = None
        if state.open_thinking_index is None:
            state.open_thinking_index = state.next_block_index
            state.next_block_index += 1
            block = event.block if isinstance(event, ThinkingCompleted) else None
            content = (
                _response_block(block)
                if isinstance(block, RedactedThinkingBlock)
                else {"type": "thinking", "thinking": "", "signature": ""}
            )
            chunks.append(
                _sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": state.open_thinking_index,
                        "content_block": content,
                    },
                )
            )
        index = state.open_thinking_index
        if isinstance(event, ThinkingDelta):
            chunks.append(
                _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "thinking_delta", "thinking": event.text},
                    },
                )
            )
        else:
            if isinstance(event.block, ThinkingBlock):
                chunks.append(
                    _sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {
                                "type": "signature_delta",
                                "signature": event.block.signature,
                            },
                        },
                    )
                )
            chunks.append(_content_block_stop(index))
            state.open_thinking_index = None
        return tuple(chunks)
    if isinstance(event, (TextDelta, RefusalDelta)):
        if enabled:
            chunks = []
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
                "usage": (
                    {"input_tokens": None, "output_tokens": None}
                    if event.usage is None
                    else _anthropic_usage(event.usage)
                )
                if event.provider == "openai-subscription"
                else {"output_tokens": usage_counter(event.usage, "output_tokens")},
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
    normalized = {
        "input_tokens": usage_counter(usage, "input_tokens"),
        "output_tokens": usage_counter(usage, "output_tokens"),
    }
    for field in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        if usage is not None and field in usage:
            normalized[field] = usage_counter(usage, field)
    return normalized


def _input_usage(usage: InputUsage) -> dict[str, int]:
    normalized = {"input_tokens": usage.input_tokens}
    for field in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        value = getattr(usage, field)
        if value is not None:
            normalized[field] = value
    return normalized


def _response_block(
    block: TextBlock | ToolCall | ThinkingBlock | RedactedThinkingBlock,
) -> dict[str, object]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ThinkingBlock):
        return {
            "type": "thinking",
            "thinking": block.thinking,
            "signature": block.signature,
        }
    if isinstance(block, RedactedThinkingBlock):
        return {"type": "redacted_thinking", "data": block.data}
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
