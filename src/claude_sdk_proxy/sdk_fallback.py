"""Admission of unpublished native response payload and observed fallback legs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from claude_sdk_proxy.domain import (
    BackendFailure,
    ConversationEvent,
    RedactedThinkingBlock,
    TextDelta,
    ThinkingCompleted,
    ThinkingDelta,
    ToolCall,
)
from claude_sdk_proxy.tool_contract import JsonValue, canonical_json


class FallbackBuffer:
    """Bound retained payload, coalescing adjacent public deltas.

    A separate native admission counter includes signatures and arguments before
    their completed normalized events exist. Neither counter is a Python RSS cap.
    """

    def __init__(self, limit_bytes: int = 64 * 1024 * 1024) -> None:
        self._limit = limit_bytes
        self._bytes = 0
        self._native_bytes = 0
        self._events: list[ConversationEvent] = []

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
        previous = self._events[-1] if self._events else None
        if isinstance(previous, TextDelta) and isinstance(event, TextDelta):
            self._events[-1] = TextDelta(previous.text + event.text)
        elif (
            isinstance(previous, ThinkingDelta)
            and isinstance(event, ThinkingDelta)
            and previous.index == event.index
        ):
            self._events[-1] = ThinkingDelta(event.index, previous.text + event.text)
        else:
            self._events.append(event)

    def discard(self) -> None:
        self._events.clear()
        self._bytes = self._native_bytes = 0

    def release(self) -> tuple[ConversationEvent, ...]:
        events = tuple(self._events)
        self.discard()
        return events
