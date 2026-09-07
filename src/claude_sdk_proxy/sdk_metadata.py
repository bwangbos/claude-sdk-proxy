from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Never

from claude_agent_sdk import RateLimitEvent, SystemMessage

from claude_sdk_proxy.domain import BackendFailure

_COUNTER_MAX = 2**63 - 1
_RATE_LIMIT_STATUSES = {"allowed", "allowed_warning", "rejected"}
_RATE_LIMIT_TYPES = {
    "five_hour",
    "seven_day",
    "seven_day_opus",
    "seven_day_sonnet",
    "overage",
}
_REFUSAL_CATEGORIES = {
    "cyber",
    "bio",
    "frontier_llm",
    "reasoning_extraction",
    "general_harms",
}


def validate_system_message(
    message: SystemMessage,
    expected_tools: tuple[str, ...] = (),
    expected_mcp_servers: tuple[str, ...] = (),
) -> object:
    data = message.data
    servers = [{"name": name, "status": "connected"} for name in expected_mcp_servers]
    if (
        not isinstance(data, Mapping)
        or message.subtype not in {"init", "status", "thinking_tokens"}
        or data.get("type") != "system"
        or data.get("subtype") != message.subtype
        or not _identifier(data.get("uuid"))
    ):
        _fail()
    if message.subtype == "thinking_tokens":
        total = data.get("estimated_tokens")
        delta = data.get("estimated_tokens_delta")
        if (
            set(data)
            != {
                "type",
                "subtype",
                "estimated_tokens",
                "estimated_tokens_delta",
                "session_id",
                "uuid",
            }
            or type(total) is not int
            or type(delta) is not int
            or not 0 <= delta <= total <= _COUNTER_MAX
        ):
            _fail()
    elif message.subtype == "status":
        if data.get("status") != "requesting":
            _fail()
    elif (
        not _identifier(data.get("model"))
        or data.get("permissionMode") != "dontAsk"
        or data.get("tools") != list(expected_tools)
        or data.get("mcp_servers") != servers
        or data.get("skills") != []
        or data.get("plugins") != []
    ):
        _fail()
    return data.get("session_id")


def validate_refusal_notice(message: SystemMessage) -> tuple[str, str]:
    """Validate the observed no-fallback refusal discriminator and identity."""
    data = message.data
    keys = {
        "type",
        "subtype",
        "session_id",
        "uuid",
        "request_id",
        "refused_user_message_uuid",
        "original_model",
        "content",
        "api_refusal_category",
        "api_refusal_explanation",
    }
    if (
        message.subtype != "model_refusal_no_fallback"
        or not isinstance(data, Mapping)
        or set(data) != keys
        or data.get("type") != "system"
        or data.get("subtype") != message.subtype
        or not isinstance(data.get("api_refusal_category"), str)
        or data["api_refusal_category"] not in _REFUSAL_CATEGORIES
        or not isinstance(data.get("api_refusal_explanation"), str)
        or not data["api_refusal_explanation"]
        or any(
            not _identifier(data.get(field))
            for field in (
                "session_id",
                "uuid",
                "request_id",
                "refused_user_message_uuid",
            )
        )
    ):
        _fail()
    return data["session_id"], data["api_refusal_category"]


def validate_fallback_notice(message: SystemMessage) -> tuple[str, str, str]:
    """Validate the observed switch shape; unobserved retractions fail closed."""
    data = message.data
    if (
        not isinstance(data, Mapping)
        or message.subtype != "model_refusal_fallback"
        or data.get("type") != "system"
        or data.get("subtype") != message.subtype
        or data.get("trigger") != "refusal"
        or data.get("direction") != "retry"
        or data.get("scope") != "session"
        or not isinstance(data.get("content"), str)
        or set(data)
        != {
            "type",
            "subtype",
            "session_id",
            "uuid",
            "request_id",
            "refused_user_message_uuid",
            "original_model",
            "fallback_model",
            "content",
            "api_refusal_category",
            "api_refusal_explanation",
            "trigger",
            "direction",
            "scope",
            "retracted_message_uuids",
        }
        or not isinstance(data.get("api_refusal_category"), str)
        or data["api_refusal_category"] not in _REFUSAL_CATEGORIES
        or not isinstance(data.get("api_refusal_explanation"), str)
        or not data["api_refusal_explanation"]
        or any(
            not _identifier(data.get(key))
            for key in (
                "uuid",
                "session_id",
                "request_id",
                "refused_user_message_uuid",
            )
        )
        or any(
            not isinstance(data.get(key), str)
            or re.fullmatch(r"[a-zA-Z0-9._:-]{1,128}", data[key]) is None
            for key in ("original_model", "fallback_model")
        )
        or data.get("retracted_message_uuids") != []
    ):
        _fail()
    return data["session_id"], data["original_model"], data["fallback_model"]


def validate_rate_limit_event(message: RateLimitEvent) -> object:
    info = message.rate_limit_info
    utilization = info.utilization
    if (
        info.status not in _RATE_LIMIT_STATUSES
        or info.rate_limit_type not in _RATE_LIMIT_TYPES | {None}
        or info.overage_status not in _RATE_LIMIT_STATUSES | {None}
        or not _counter(info.resets_at)
        or not _counter(info.overage_resets_at)
        or (
            utilization is not None
            and (
                type(utilization) not in (int, float)
                or not math.isfinite(utilization)
                or not 0 <= utilization <= 1
            )
        )
        or not isinstance(info.overage_disabled_reason, (str, type(None)))
        or not isinstance(info.raw, Mapping)
        or not _identifier(message.uuid)
    ):
        _fail()
    return message.session_id


def _counter(value: object) -> bool:
    return value is None or type(value) is int and 0 <= value <= _COUNTER_MAX


def _identifier(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _fail() -> Never:
    raise BackendFailure("Agent SDK protocol failure")
