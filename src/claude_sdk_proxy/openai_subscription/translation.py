"""Caller-only subscription Responses requests; no Claude session dependency."""

from __future__ import annotations

from typing import Any

from claude_sdk_proxy.domain import (
    ImageBlock,
    RedactedThinkingBlock,
    RequestValidationError,
    TextBlock,
    TextRequest,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
)

from .replay import (
    ASSISTANT_PREFIX,
    decode_assistant,
    decode_reasoning,
    envelope_scope,
    packed,
    plain,
)

MODELS = ("gpt-6-astra", "gpt-5.6-sol")
EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
MAX_REQUEST_BYTES = 32 * 1024 * 1024


def validate_request(request: TextRequest) -> None:
    if request.model not in MODELS:
        raise RequestValidationError("model", "model is not configured")
    thinking = request.thinking
    if thinking.mode == "enabled" or thinking.budget_tokens is not None:
        raise RequestValidationError(
            "thinking", "reasoning token budgets are unsupported"
        )
    if thinking.effort is not None and thinking.effort not in EFFORTS:
        raise RequestValidationError(
            "reasoning_effort", "unsupported subscription effort"
        )
    if request.refusal_fallback != "off":
        raise RequestValidationError(
            "refusal_fallback", "subscription fallback is unsupported"
        )
    # Bound canonical input before credentials can refresh or HTTP is opened.
    if len(packed(request)) > MAX_REQUEST_BYTES:
        raise RequestValidationError("messages", "subscription request exceeds limit")


def image_part(block: ImageBlock) -> dict[str, str]:
    return {
        "type": "input_image",
        "detail": "auto",
        "image_url": f"data:{block.media_type};base64,{block.data}",
    }


def translate_messages(
    request: TextRequest, account: str, *, only_last: bool = False
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, message in enumerate(request.messages):
        if only_last and index != len(request.messages) - 1:
            continue
        carriers = [
            b.data
            for b in message.blocks
            if isinstance(b, RedactedThinkingBlock)
            and b.data.startswith(ASSISTANT_PREFIX)
        ]
        if message.role == "assistant" and len(carriers) == 1:
            native = decode_assistant(
                carriers[0], request, account, request.messages[: index + 1]
            )
            if native is not None:
                result.extend(native)
                continue
        parts: list[dict[str, Any]] = []

        def flush() -> None:
            if parts:
                result.append({"role": message.role, "content": list(parts)})
                parts.clear()

        for block in message.blocks:
            if isinstance(block, TextBlock):
                parts.append(
                    {
                        "type": "input_text"
                        if message.role == "user"
                        else "output_text",
                        "text": block.text,
                    }
                )
            elif isinstance(block, ImageBlock):
                parts.append(image_part(block))
            elif isinstance(block, ToolCallBlock):
                flush()
                result.append(
                    {
                        "type": "function_call",
                        "call_id": block.id,
                        "name": block.name,
                        "arguments": packed(block.arguments).decode(),
                    }
                )
            elif isinstance(block, ToolResultBlock):
                flush()
                output: str | list[dict[str, Any]]
                if any(isinstance(p, ImageBlock) for p in block.content):
                    output = [
                        image_part(p)
                        if isinstance(p, ImageBlock)
                        else {"type": "input_text", "text": p}
                        for p in block.content
                    ]
                else:
                    output = "\n".join(str(p) for p in block.content)
                result.append(
                    {
                        "type": "function_call_output",
                        "call_id": block.tool_call_id,
                        "output": output,
                    }
                )
            elif isinstance(block, ThinkingBlock):
                item = decode_reasoning(
                    block.signature,
                    account,
                    request.model,
                    scope=envelope_scope(
                        request, account, request.messages[: index + 1]
                    ),
                )
                if item is not None:
                    flush()
                    result.append(item)
            elif isinstance(block, RedactedThinkingBlock):
                continue  # Foreign opaque state cannot be used by this provider.
        flush()
    return result


def build_body(
    request: TextRequest, account: str, replay: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    validate_request(request)
    body: dict[str, Any] = {
        "model": request.model,
        "instructions": request.system,
        "store": False,
        "stream": True,
        "input": translate_messages(request, account)
        if replay is None
        else replay + translate_messages(request, account, only_last=True),
        "include": ["reasoning.encrypted_content"],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }
    if request.tools:
        body["tools"] = [
            {
                "type": "function",
                "name": t.name,
                "description": t.description,
                "parameters": plain(t.input_schema),
                "strict": None,
            }
            for t in request.tools
        ]
    if request.thinking.effort is not None:
        body["reasoning"] = {"effort": request.thinking.effort}
        if request.thinking.display != "omitted":
            body["reasoning"]["summary"] = "auto"
    # Pi's observed subscription builder does not send max_output_tokens.
    # max_tokens is accepted for dialect compatibility but not enforced here.
    return body
