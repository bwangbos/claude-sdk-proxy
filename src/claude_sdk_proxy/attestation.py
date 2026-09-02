"""Public fail-closed child-attestation and supervised transport API."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from claude_sdk_proxy._attestation_v2 import *  # noqa: F403
from claude_sdk_proxy._attestation_v2 import __all__ as _ATTESTATION_V2_ALL

_MAX_EVENT_TYPE_BYTES: Final = 32
_MAX_MODEL_ID_BYTES: Final = 256
_MAX_EVENT_CONTENT_BYTES: Final = 1024 * 1024
_MAX_BUFFERED_EVENTS: Final = 4096
_MAX_BUFFERED_CONTENT_BYTES: Final = 4 * 1024 * 1024
_EVENT_TYPES: Final = frozenset(
    {
        "message_start",
        "assistant",
        "content_block_start",
        "text_delta",
        "thinking_delta",
        "signature_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
        "result",
        "error",
    }
)
_AUTHORITATIVE_EVENT_TYPES: Final = frozenset({"message_start", "assistant"})
_TERMINAL_EVENT_TYPES: Final = frozenset({"message_stop", "result", "error"})
_GENERIC_MODEL_ALIASES: Final = frozenset(
    {"sonnet", "opus", "haiku", "latest", "default"}
)


class ModelIdentityError(RuntimeError):
    """Raised when an event stream cannot prove one exact backend model."""


class _ModelIdentityState(StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"


def _bounded_text(value: object, label: str, maximum: int) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be exact text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{label} must be valid Unicode") from error
    if not encoded or len(encoded) > maximum:
        raise ValueError(f"{label} is outside its byte bound")
    return value


def _model_text(value: object) -> str:
    model = _bounded_text(value, "model identity", _MAX_MODEL_ID_BYTES)
    if any(character < "!" or character > "~" for character in model):
        raise ValueError("model identity must be visible ASCII")
    return model


def _require_exact_backend_model(value: object) -> str:
    model = _model_text(value)
    lowered = model.lower()
    if lowered in _GENERIC_MODEL_ALIASES or lowered.endswith("-latest"):
        raise ModelIdentityError("expected model must be an exact backend model ID")
    if lowered.startswith("claude-") and lowered.rsplit("-", 1)[-1] in {
        "sonnet",
        "opus",
        "haiku",
    }:
        raise ModelIdentityError("expected model must be an exact backend model ID")
    family_prefixes = ("claude-sonnet-", "claude-opus-", "claude-haiku-")
    if lowered.startswith(family_prefixes):
        final_component = lowered.rsplit("-", 1)[-1]
        if final_component != "exact" and not (
            len(final_component) == 8
            and final_component.isascii()
            and final_component.isdigit()
        ):
            raise ModelIdentityError(
                "expected model must be an exact backend model ID"
            )
    return model


@dataclass(frozen=True, slots=True)
class CanonicalEvent:
    """One immutable, bounded event snapshot retained only for stream release."""

    event_type: str
    model: str | None = None
    content: str | None = None
    success: bool = False

    def __post_init__(self) -> None:
        event_type = _bounded_text(
            self.event_type, "event type", _MAX_EVENT_TYPE_BYTES
        )
        if event_type not in _EVENT_TYPES:
            raise ValueError("unsupported canonical event type")
        if self.model is not None:
            _model_text(self.model)
        if self.content is not None:
            if type(self.content) is not str:
                raise TypeError("event content must be exact text")
            try:
                content_bytes = self.content.encode("utf-8")
            except UnicodeEncodeError as error:
                raise ValueError("event content must be valid Unicode") from error
            if len(content_bytes) > _MAX_EVENT_CONTENT_BYTES:
                raise ValueError("event content exceeds its byte bound")
        if type(self.success) is not bool:
            raise TypeError("event success must be boolean")
        if self.success and event_type != "result":
            raise ValueError("success framing is valid only for result events")


class ModelIdentityGate:
    """Buffer events until an authoritative envelope proves the exact model."""

    __slots__ = (
        "_buffer",
        "_buffered_content_bytes",
        "_expected",
        "_released_content_count",
        "_state",
    )

    def __init__(self, *, expected: str) -> None:
        self._expected = _require_exact_backend_model(expected)
        self._state = _ModelIdentityState.PENDING
        self._buffer: list[CanonicalEvent] = []
        self._buffered_content_bytes = 0
        self._released_content_count = 0

    @property
    def released_content_count(self) -> int:
        return self._released_content_count

    def _fail(self, message: str) -> None:
        self._buffer.clear()
        self._buffered_content_bytes = 0
        self._state = _ModelIdentityState.FAILED
        raise ModelIdentityError(message)

    def _count_released(self, events: tuple[CanonicalEvent, ...]) -> None:
        self._released_content_count += sum(
            event.content is not None for event in events
        )

    def observe(self, event: CanonicalEvent) -> tuple[CanonicalEvent, ...]:
        """Observe one event and return only events safe to release now."""
        if self._state is _ModelIdentityState.FAILED:
            raise ModelIdentityError("model identity gate has failed")
        if type(event) is not CanonicalEvent:
            self._fail("event is not an immutable canonical event")

        if self._state is _ModelIdentityState.VERIFIED:
            if event.event_type in _AUTHORITATIVE_EVENT_TYPES and event.model is None:
                self._fail("authoritative event omitted model identity")
            if event.model is not None and event.model != self._expected:
                self._fail("later event changed model identity")
            immediate = (event,)
            self._count_released(immediate)
            return immediate

        if event.event_type in _TERMINAL_EVENT_TYPES:
            self._fail("terminal framing arrived before model identity")
        if event.model is not None and event.model != self._expected:
            self._fail("event exposed a mismatched model identity")
        if event.event_type in _AUTHORITATIVE_EVENT_TYPES:
            if event.model is None:
                self._fail("authoritative event omitted model identity")
            released = (*self._buffer, event)
            self._buffer.clear()
            self._buffered_content_bytes = 0
            self._state = _ModelIdentityState.VERIFIED
            self._count_released(released)
            return released

        if len(self._buffer) >= _MAX_BUFFERED_EVENTS:
            self._fail("model identity buffer event bound exceeded")
        content_bytes = (
            0 if event.content is None else len(event.content.encode("utf-8"))
        )
        if self._buffered_content_bytes + content_bytes > _MAX_BUFFERED_CONTENT_BYTES:
            self._fail("model identity buffer content bound exceeded")
        self._buffer.append(event)
        self._buffered_content_bytes += content_bytes
        return ()


__all__ = (
    *_ATTESTATION_V2_ALL,
    "CanonicalEvent",
    "ModelIdentityError",
    "ModelIdentityGate",
)
