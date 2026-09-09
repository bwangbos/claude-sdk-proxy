from collections.abc import Mapping
from typing import Any, cast

from quaylet.domain import (
    CanonicalMessage,
    RequestValidationError,
    Role,
    UnsupportedFeature,
)

_MESSAGE_FIELDS = {"role", "content"}


def reject_fields(
    body: Mapping[str, object], supported: set[str], unsupported: set[str]
) -> None:
    for field in body:
        if field in unsupported:
            raise UnsupportedFeature(field, "not supported by the text gateway")
        if field not in supported:
            raise RequestValidationError(field, "field is not supported")


def required_string(body: Mapping[str, object], field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str):
        raise RequestValidationError(field, "must be a string")
    if field == "model" and not value:
        raise RequestValidationError(field, "must not be empty")
    return value


def boolean(body: Mapping[str, object], field: str, default: bool = False) -> bool:
    if field not in body:
        return default
    value = body[field]
    if type(value) is not bool:
        raise RequestValidationError(field, "must be a boolean")
    return value


def text_messages(
    body: Mapping[str, object], tool_fields: set[str], *, allow_system: bool
) -> tuple[str, list[CanonicalMessage]]:
    value = body.get("messages")
    if not isinstance(value, list) or not value:
        raise RequestValidationError("messages", "must be a non-empty array")
    system = ""
    messages: list[CanonicalMessage] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise RequestValidationError("messages", "entries must be objects")
        extras = set(raw) - _MESSAGE_FIELDS
        if extras & tool_fields:
            raise UnsupportedFeature(
                "messages", "tool message fields are not supported"
            )
        if extras:
            raise RequestValidationError("messages", "message fields are not supported")
        role, content = raw.get("role"), raw.get("content")
        if not isinstance(role, str):
            raise RequestValidationError("messages", "role must be a string")
        if not isinstance(content, str):
            if isinstance(content, list):
                raise UnsupportedFeature("messages", "content blocks are not supported")
            raise RequestValidationError("messages", "content must be a string")
        if allow_system and role == "system":
            if index:
                raise RequestValidationError(
                    "messages", "system must be the first message"
                )
            system = content
        elif role in {"tool", "function"}:
            raise UnsupportedFeature("messages", "tool roles are not supported")
        elif role not in {"user", "assistant"}:
            raise RequestValidationError("messages", "role must be user or assistant")
        else:
            messages.append(CanonicalMessage(cast(Role, role), content))
    return system, messages


def usage_counter(usage: Mapping[str, Any] | None, field: str) -> int:
    value = 0 if usage is None else usage.get(field, 0)
    return value if type(value) is int and value >= 0 else 0
