"""Caller-only subscription Responses requests; no Claude session dependency."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from quaylet.domain import (
    ImageBlock,
    RedactedThinkingBlock,
    RequestValidationError,
    TextBlock,
    TextRequest,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from quaylet.model_catalog import OPENAI_EFFORTS, OPENAI_MODELS, TEXT_ONLY_MODELS
from quaylet.thinking import ThinkingDisplay, ThinkingEffort, ThinkingOptions

from .replay import (
    ASSISTANT_PREFIX,
    decode_assistant,
    decode_reasoning,
    envelope_scope,
    packed,
    plain,
)

MODELS = OPENAI_MODELS
EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
MAX_REQUEST_BYTES = 32 * 1024 * 1024


def parse_subscription_thinking(
    body: Mapping[str, object], dialect: str
) -> ThinkingOptions:
    """Validate raw provider controls before Claude parsing loses explicit off."""
    display = None
    if dialect == "openai":
        effort = body.get("reasoning_effort")
        if effort is None:
            return ThinkingOptions()
    else:
        raw = body.get("thinking")
        output = body.get("output_config")
        if output is not None and (
            not isinstance(output, Mapping) or set(output) - {"effort"}
        ):
            raise RequestValidationError(
                "output_config", "invalid subscription reasoning options"
            )
        effort = output.get("effort") if isinstance(output, Mapping) else None
        if raw is None:
            if effort is not None:
                raise RequestValidationError(
                    "output_config", "effort requires adaptive thinking"
                )
            return ThinkingOptions()
        if (
            not isinstance(raw, Mapping)
            or raw.get("type") != "adaptive"
            or set(raw) - {"type", "display"}
        ):
            raise RequestValidationError(
                "thinking", "only adaptive subscription reasoning is supported"
            )
        display = raw.get("display")
        if display is not None and (
            not isinstance(display, str) or display not in {"summarized", "omitted"}
        ):
            raise RequestValidationError("thinking", "invalid reasoning display")
    if effort is not None and (not isinstance(effort, str) or effort not in EFFORTS):
        raise RequestValidationError(
            "reasoning_effort", "unsupported subscription effort"
        )
    return ThinkingOptions(
        mode="adaptive",
        effort=cast(ThinkingEffort | None, effort),
        display=cast(ThinkingDisplay | None, display),
    )


def validate_request(request: TextRequest) -> None:
    if request.model not in MODELS:
        raise RequestValidationError("model", "model is not configured")
    thinking = request.thinking
    if thinking.mode == "enabled" or thinking.budget_tokens is not None:
        raise RequestValidationError(
            "thinking", "reasoning token budgets are unsupported"
        )
    if (
        thinking.effort is not None
        and thinking.effort not in OPENAI_EFFORTS[request.model]
    ):
        raise RequestValidationError(
            "reasoning_effort", "unsupported subscription effort"
        )
    if request.model in TEXT_ONLY_MODELS:
        for message in request.messages:
            for block in message.blocks:
                parts = (
                    block.content if isinstance(block, ToolResultBlock) else (block,)
                )
                if any(isinstance(part, ImageBlock) for part in parts):
                    raise RequestValidationError(
                        "messages", "model does not support image inputs"
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
