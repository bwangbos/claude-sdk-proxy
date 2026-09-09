from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Never, cast

from claude_agent_sdk import AssistantMessage, TextBlock

from quaylet.domain import BackendFailure, InputUsage, TextDelta

_PROTOCOL_ERROR = "Agent SDK protocol failure"
_STOP_REASONS = frozenset(
    {"end_turn", "max_tokens", "model_context_window_exceeded", "refusal"}
)
USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
_INPUT_USAGE_FIELDS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def fail_protocol() -> Never:
    raise BackendFailure(_PROTOCOL_ERROR)


def normalize_usage(
    usage: object, fields: tuple[str, ...]
) -> dict[str, int] | None:
    if usage is None:
        return None
    if not isinstance(usage, Mapping):
        fail_protocol()
    normalized: dict[str, int] = {}
    for field in fields:
        if field not in usage:
            continue
        value = usage[field]
        if type(value) is not int or value < 0:
            fail_protocol()
        normalized[field] = value
    return normalized


class RawTextEventValidator:
    def __init__(self, *, allow_seeded_history: bool = False) -> None:
        self._phase = "message_start"
        self._block_index = 0
        self._saw_delta = False
        self._text: list[str] = []
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self._input_usage: dict[str, int] = {}
        self._latest_output_tokens: int | None = None
        self.stop_reason: str | None = None
        self._assistant_text: str | None = None
        self._allow_seeded_history = allow_seeded_history

    def observe(self, event: Mapping[str, Any]) -> InputUsage | TextDelta | None:
        event_type = event.get("type")
        if event_type == "message_start":
            return self._message_start(event)
        if event_type == "content_block_start":
            self._block_start(event)
            return None
        if event_type == "content_block_delta":
            return self._block_delta(event)
        if event_type == "content_block_stop":
            self._block_stop(event)
            return None
        if event_type == "message_delta":
            self._message_delta(event)
            return None
        if event_type == "message_stop":
            self._message_stop(event)
            return None
        fail_protocol()

    def _message_start(self, event: Mapping[str, Any]) -> InputUsage:
        if self._phase != "message_start":
            fail_protocol()
        self._require_keys(event, {"type", "message"})
        message = event.get("message")
        if not isinstance(message, Mapping):
            fail_protocol()
        if message.get("type", "message") != "message":
            fail_protocol()
        if message.get("role", "assistant") != "assistant":
            fail_protocol()
        if message.get("content", []) != []:
            fail_protocol()
        if message.get("stop_reason") is not None:
            fail_protocol()
        if message.get("stop_sequence") is not None:
            fail_protocol()
        if message.get("stop_details") is not None:
            fail_protocol()
        if not valid_message_diagnostics(
            message.get("diagnostics"), self._allow_seeded_history
        ):
            fail_protocol()
        for field in ("model", "id"):
            value = message.get(field)
            if value is not None and (not isinstance(value, str) or not value):
                fail_protocol()
        usage = normalize_usage(message.get("usage"), USAGE_FIELDS)
        if usage is None or "input_tokens" not in usage:
            fail_protocol()
        self._input_usage = {
            field: usage[field] for field in _INPUT_USAGE_FIELDS if field in usage
        }
        self._latest_output_tokens = usage.get("output_tokens")
        self.input_tokens = usage["input_tokens"]
        self._phase = "block_start"
        return InputUsage(
            self.input_tokens,
            self._input_usage.get("cache_read_input_tokens"),
            self._input_usage.get("cache_creation_input_tokens"),
        )

    def _block_start(self, event: Mapping[str, Any]) -> None:
        if self._phase != "block_start":
            fail_protocol()
        self._require_keys(event, {"type", "index", "content_block"})
        self._require_index(event)
        block = event.get("content_block")
        if (
            not isinstance(block, Mapping)
            or set(block) != {"type", "text"}
            or block.get("type") != "text"
            or block.get("text") != ""
        ):
            fail_protocol()
        self._saw_delta = False
        self._phase = "block_delta"

    def _block_delta(self, event: Mapping[str, Any]) -> TextDelta:
        if self._phase != "block_delta" or self._assistant_text is not None:
            fail_protocol()
        self._require_keys(event, {"type", "index", "delta"})
        self._require_index(event)
        delta = event.get("delta")
        if (
            not isinstance(delta, Mapping)
            or set(delta) != {"type", "text"}
            or delta.get("type") != "text_delta"
        ):
            fail_protocol()
        text = delta.get("text")
        if not isinstance(text, str):
            fail_protocol()
        self._saw_delta = True
        self._text.append(text)
        return TextDelta(text)

    def _block_stop(self, event: Mapping[str, Any]) -> None:
        if self._phase != "block_delta" or not self._saw_delta:
            fail_protocol()
        self._require_keys(event, {"type", "index"})
        self._require_index(event)
        self._phase = "after_block"

    def _message_delta(self, event: Mapping[str, Any]) -> None:
        if self._phase != "after_block":
            fail_protocol()
        self._require_keys(
            event, {"type", "delta", "usage", "context_management"}
        )
        delta = event.get("delta")
        if (
            not isinstance(delta, Mapping)
            or set(delta) != {"stop_reason", "stop_sequence", "stop_details"}
            or delta.get("stop_reason") not in _STOP_REASONS
            or delta.get("stop_sequence") is not None
            or delta.get("stop_details") is not None
        ):
            fail_protocol()
        context = event.get("context_management")
        if not isinstance(context, Mapping) or dict(context) != {"applied_edits": []}:
            fail_protocol()
        usage = normalize_usage(event.get("usage"), USAGE_FIELDS)
        if usage is None or "output_tokens" not in usage:
            fail_protocol()
        self._reconcile_usage(usage)
        self.stop_reason = delta["stop_reason"]
        self.output_tokens = usage["output_tokens"]
        self._phase = "message_stop"

    def _message_stop(self, event: Mapping[str, Any]) -> None:
        if self._phase != "message_stop":
            fail_protocol()
        self._require_keys(event, {"type"})
        self._phase = "complete"

    def validate_assistant(self, message: AssistantMessage) -> None:
        if message.parent_tool_use_id is not None or message.error is not None:
            fail_protocol()
        content = message.content
        if not isinstance(content, list):
            fail_protocol()
        if any(type(block) is not TextBlock for block in content):
            fail_protocol()
        text_blocks = cast(list[TextBlock], content)
        if any(not isinstance(block.text, str) for block in text_blocks):
            fail_protocol()
        complete_text = "".join(block.text for block in text_blocks)
        if self._phase != "block_delta" or not self._saw_delta:
            fail_protocol()
        if self._assistant_text is not None or complete_text != "".join(self._text):
            fail_protocol()
        usage = normalize_usage(message.usage, USAGE_FIELDS)
        if usage is not None:
            self._reconcile_usage(usage)
            if "output_tokens" in usage:
                self._latest_output_tokens = usage["output_tokens"]
        self._assistant_text = complete_text

    @property
    def boundary_usage(self) -> dict[str, int]:
        if self.output_tokens is None:
            fail_protocol()
        return {**self._input_usage, "output_tokens": self.output_tokens}

    def _reconcile_usage(self, usage: Mapping[str, int]) -> None:
        for field in _INPUT_USAGE_FIELDS:
            if field in usage and usage[field] != self._input_usage.get(field):
                fail_protocol()
        if (
            "output_tokens" in usage
            and self._latest_output_tokens is not None
            and usage["output_tokens"] < self._latest_output_tokens
        ):
            fail_protocol()

    def validate_result(
        self, stop_reason: object, usage: Mapping[str, int] | None
    ) -> None:
        del usage
        if self._phase != "complete" or stop_reason != self.stop_reason:
            fail_protocol()

    def _require_index(self, event: Mapping[str, Any]) -> None:
        if type(event.get("index")) is not int or event["index"] != self._block_index:
            fail_protocol()

    @staticmethod
    def _require_keys(event: Mapping[str, Any], keys: set[str]) -> None:
        if set(event) != keys:
            fail_protocol()


def valid_message_diagnostics(value: object, allow_seeded_history: bool) -> bool:
    if value is None:
        return True
    return allow_seeded_history and value == {
        "cache_miss_reason": {"type": "previous_message_not_found"}
    }
