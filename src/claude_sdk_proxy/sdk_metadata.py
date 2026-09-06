from __future__ import annotations

import math
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
