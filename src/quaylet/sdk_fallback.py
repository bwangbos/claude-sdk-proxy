"""Admission of unpublished native response payload and observed fallback legs."""

from __future__ import annotations

from collections.abc import Mapping
from io import StringIO
from typing import Any, cast

from quaylet.domain import (
    BackendFailure,
    ConversationEvent,
    RedactedThinkingBlock,
    TextDelta,
    ThinkingCompleted,
    ThinkingDelta,
    ToolCall,
)
from quaylet.tool_contract import JsonValue, canonical_json


class FallbackBuffer:
    """Bound retained payload, coalescing adjacent public deltas.

    A separate native admission counter includes signatures and arguments before
    their completed normalized events exist. Neither counter is a Python RSS cap.
    StringIO accumulates each adjacent run without copying its prefix per delta.
    """

    def __init__(self, limit_bytes: int = 64 * 1024 * 1024) -> None:
        self._limit = limit_bytes
        self._bytes = 0
        self._native_bytes = 0
        self._events: list[ConversationEvent] = []
        self._pending: TextDelta | ThinkingDelta | None = None
        self._text = StringIO()

    def _check(self, size: int) -> None:
        if size > self._limit:
            self.discard()
            raise BackendFailure("fallback_buffer_limit")

    def admit_raw(self, event: Mapping[str, Any]) -> None:
        """Count raw payload before the validator retains it, not its envelopes."""
        payload = event.get("delta") or event.get("content_block")
        if not isinstance(payload, Mapping):
            return
        for key in ("text", "thinking", "signature", "data", "partial_json"):
            value = payload.get(key)
            if isinstance(value, str):
                size = self._native_bytes + len(value.encode("utf-8"))
                self._check(size)
                self._native_bytes = size

    def append(self, event: ConversationEvent) -> None:
        size = 0
        if isinstance(event, (TextDelta, ThinkingDelta)):
            size = len(event.text.encode("utf-8"))
        elif isinstance(event, ThinkingCompleted):
            block = event.block
            size = len(
                (
                    block.data
                    if isinstance(block, RedactedThinkingBlock)
                    else block.thinking + block.signature
                ).encode("utf-8")
            )
        elif isinstance(event, ToolCall):
            size = len(
                (
                    event.id
                    + event.name
                    + canonical_json(cast(Mapping[str, JsonValue], event.arguments))
                ).encode("utf-8")
            )
        self._check(self._bytes + size)
        self._bytes += size
        previous = self._pending
        adjacent = (
            isinstance(previous, TextDelta) and isinstance(event, TextDelta)
        ) or (
            isinstance(previous, ThinkingDelta)
            and isinstance(event, ThinkingDelta)
            and previous.index == event.index
        )
        if not adjacent:
            self._flush()
        if isinstance(event, (TextDelta, ThinkingDelta)):
            self._pending = event
            self._text.write(event.text)
        else:
            self._events.append(event)

    def _flush(self) -> None:
        pending = self._pending
        if pending is not None:
            text = self._text.getvalue()
            self._events.append(
                TextDelta(text)
                if isinstance(pending, TextDelta)
                else ThinkingDelta(pending.index, text)
            )
            self._pending = None
            self._text = StringIO()

    def discard(self) -> None:
        self._events.clear()
        self._pending = None
        self._text = StringIO()
        self._bytes = self._native_bytes = 0

    def release(self) -> tuple[ConversationEvent, ...]:
        self._flush()
        events = tuple(self._events)
        self.discard()
        return events
