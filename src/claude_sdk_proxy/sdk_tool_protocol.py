from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock
from jsonschema.validators import validator_for  # type: ignore[import-untyped]

from claude_sdk_proxy.domain import InputUsage, TextDelta, ToolDefinition
from claude_sdk_proxy.sdk_text_protocol import (
    fail_protocol,
    normalize_usage,
    valid_message_diagnostics,
)
from claude_sdk_proxy.tool_contract import (
    JsonValue,
    canonical_json,
    plain_json,
    validate_tool_arguments,
    validate_tool_definitions,
)

_SDK_PREFIX = "mcp__caller_tools_v1__"
_TEXT_STOP_REASONS = frozenset(
    {"end_turn", "max_tokens", "model_context_window_exceeded", "refusal"}
)
_MAX_RAW_ARGUMENT_CHARS = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class RawToolCall:
    internal_id: str
    sdk_name: str
    public_name: str
    arguments: Mapping[str, JsonValue]


@dataclass(slots=True)
class _RawBlock:
    kind: str
    text: list[str]
    internal_id: str | None = None
    sdk_name: str | None = None
    public_name: str | None = None
    json_parts: list[str] | None = None
    json_chars: int = 0
    arguments: Mapping[str, JsonValue] | None = None
    saw_delta: bool = False


class RawSdkMessageValidator:
    """Validate one raw assistant message and its typed SDK counterpart."""

    def __init__(
        self,
        definitions: tuple[ToolDefinition, ...],
        *,
        allow_seeded_history: bool = False,
    ) -> None:
        normalized = validate_tool_definitions(definitions)
        self._public_names = {definition.name for definition in normalized}
        self._validators: dict[str, Any] = {}
        for definition in normalized:
            schema = plain_json(cast(Mapping[str, JsonValue], definition.input_schema))
            assert isinstance(schema, dict)
            validator_class = validator_for(schema)
            self._validators[definition.name] = validator_class(schema)
        self._phase = "message_start"
        self._block_index = 0
        self._blocks: list[_RawBlock] = []
        self._current: _RawBlock | None = None
        self._tool_suffix_started = False
        self._assistant_mode: str | None = None
        self._assistant_validated_blocks: set[int] = set()
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.stop_reason: str | None = None
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

    @property
    def complete(self) -> bool:
        return self._phase == "complete"

    @property
    def has_tools(self) -> bool:
        return any(block.kind == "tool_use" for block in self._blocks)

    @property
    def boundary_usage(self) -> dict[str, int]:
        if self.input_tokens is None or self.output_tokens is None:
            fail_protocol()
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }

    @property
    def tool_calls(self) -> tuple[RawToolCall, ...]:
        calls: list[RawToolCall] = []
        for block in self._blocks:
            if block.kind != "tool_use":
                continue
            if (
                block.internal_id is None
                or block.sdk_name is None
                or block.public_name is None
                or block.arguments is None
            ):
                fail_protocol()
            calls.append(
                RawToolCall(
                    block.internal_id,
                    block.sdk_name,
                    block.public_name,
                    block.arguments,
                )
            )
        return tuple(calls)

    def validate_assistant(self, message: AssistantMessage) -> None:
        if (
            message.parent_tool_use_id is not None
            or message.error is not None
            or self._phase not in {"block_delta", "block_start"}
            or not isinstance(message.content, list)
        ):
            fail_protocol()
        raw_blocks: list[_RawBlock]
        raw_indices: tuple[int, ...]
        if self._phase == "block_delta":
            current_index = len(self._blocks)
            if (
                self._assistant_mode == "batch"
                or self._current is None
                or not self._current.saw_delta
                or current_index in self._assistant_validated_blocks
                or len(message.content) != 1
            ):
                fail_protocol()
            self._assistant_mode = "per_block"
            raw_blocks = [self._current]
            raw_indices = (current_index,)
        else:
            if (
                self._assistant_mode == "per_block"
                or not self._blocks
                or self._assistant_validated_blocks
                or len(message.content) != len(self._blocks)
            ):
                fail_protocol()
            self._assistant_mode = "batch"
            raw_blocks = [*self._blocks]
            raw_indices = tuple(range(len(raw_blocks)))
        typed_suffix_started = False
        for raw, typed in zip(raw_blocks, message.content, strict=True):
            if type(typed) is TextBlock:
                if typed_suffix_started or raw.kind != "text":
                    fail_protocol()
                if not isinstance(typed.text, str) or typed.text != "".join(raw.text):
                    fail_protocol()
                continue
            if type(typed) is not ToolUseBlock or raw.kind != "tool_use":
                fail_protocol()
            typed_suffix_started = True
            if raw.public_name is None:
                fail_protocol()
            arguments = self._validated_arguments(typed.input, raw.public_name)
            raw_arguments = raw.arguments
            if raw_arguments is None and raw.json_parts is not None:
                raw_arguments = self._parse_arguments(raw.json_parts, raw.public_name)
            if (
                typed.id != raw.internal_id
                or typed.name != raw.sdk_name
                or raw_arguments is None
                or canonical_json(arguments) != canonical_json(raw_arguments)
            ):
                fail_protocol()
        self._assistant_validated_blocks.update(raw_indices)

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
        usage = normalize_usage(message.get("usage"), ("input_tokens",))
        if usage is None or "input_tokens" not in usage:
            fail_protocol()
        self.input_tokens = usage["input_tokens"]
        self._phase = "block_start"
        return InputUsage(self.input_tokens)

    def _block_start(self, event: Mapping[str, Any]) -> None:
        if self._phase != "block_start" or (
            self._assistant_mode == "per_block"
            and len(self._assistant_validated_blocks) != len(self._blocks)
        ):
            fail_protocol()
        self._require_keys(event, {"type", "index", "content_block"})
        self._require_index(event)
        value = event.get("content_block")
        if not isinstance(value, Mapping):
            fail_protocol()
        block_type = value.get("type")
        if block_type == "text":
            if (
                self._tool_suffix_started
                or set(value) != {"type", "text"}
                or value.get("text") != ""
            ):
                fail_protocol()
            self._current = _RawBlock("text", [])
        elif block_type == "tool_use":
            if set(value) != {
                "type",
                "id",
                "name",
                "input",
                "caller",
            } or value.get("caller") != {"type": "direct"}:
                fail_protocol()
            internal_id = value.get("id")
            sdk_name = value.get("name")
            if (
                not isinstance(internal_id, str)
                or not internal_id
                or any(block.internal_id == internal_id for block in self._blocks)
                or not isinstance(sdk_name, str)
                or value.get("input") != {}
            ):
                fail_protocol()
            public_name = self._public_name(sdk_name)
            self._tool_suffix_started = True
            self._current = _RawBlock(
                "tool_use",
                [],
                internal_id=internal_id,
                sdk_name=sdk_name,
                public_name=public_name,
                json_parts=[],
            )
        else:
            fail_protocol()
        self._phase = "block_delta"

    def _block_delta(self, event: Mapping[str, Any]) -> TextDelta | None:
        if (
            self._phase != "block_delta"
            or len(self._blocks) in self._assistant_validated_blocks
            or self._current is None
        ):
            fail_protocol()
        self._require_keys(event, {"type", "index", "delta"})
        self._require_index(event)
        delta = event.get("delta")
        if not isinstance(delta, Mapping):
            fail_protocol()
        if self._current.kind == "text":
            if set(delta) != {"type", "text"} or delta.get("type") != "text_delta":
                fail_protocol()
            text = delta.get("text")
            if not isinstance(text, str):
                fail_protocol()
            self._current.saw_delta = True
            self._current.text.append(text)
            return TextDelta(text)
        if (
            set(delta) != {"type", "partial_json"}
            or delta.get("type") != "input_json_delta"
        ):
            fail_protocol()
        partial = delta.get("partial_json")
        if not isinstance(partial, str) or self._current.json_parts is None:
            fail_protocol()
        self._current.saw_delta = True
        self._current.json_parts.append(partial)
        self._current.json_chars += len(partial)
        if self._current.json_chars > _MAX_RAW_ARGUMENT_CHARS:
            fail_protocol()
        return None

    def _block_stop(self, event: Mapping[str, Any]) -> None:
        if (
            self._phase != "block_delta"
            or self._current is None
            or not self._current.saw_delta
        ):
            fail_protocol()
        self._require_keys(event, {"type", "index"})
        self._require_index(event)
        if self._current.kind == "tool_use":
            assert self._current.json_parts is not None
            assert self._current.public_name is not None
            self._current.arguments = self._parse_arguments(
                self._current.json_parts, self._current.public_name
            )
        self._blocks.append(self._current)
        self._current = None
        self._block_index += 1
        self._phase = "block_start"

    def _message_delta(self, event: Mapping[str, Any]) -> None:
        if self._phase != "block_start" or not self._blocks:
            fail_protocol()
        self._require_keys(event, {"type", "delta", "usage", "context_management"})
        delta = event.get("delta")
        allowed = {"tool_use"} if self.has_tools else _TEXT_STOP_REASONS
        if (
            not isinstance(delta, Mapping)
            or set(delta) != {"stop_reason", "stop_sequence", "stop_details"}
            or delta.get("stop_reason") not in allowed
            or delta.get("stop_sequence") is not None
            or delta.get("stop_details") is not None
        ):
            fail_protocol()
        context = event.get("context_management")
        if not isinstance(context, Mapping) or dict(context) != {"applied_edits": []}:
            fail_protocol()
        usage = normalize_usage(event.get("usage"), ("input_tokens", "output_tokens"))
        if usage is None or "output_tokens" not in usage:
            fail_protocol()
        if usage.get("input_tokens", self.input_tokens) != self.input_tokens:
            fail_protocol()
        self.stop_reason = cast(str, delta["stop_reason"])
        self.output_tokens = usage["output_tokens"]
        self._phase = "message_stop"

    def _message_stop(self, event: Mapping[str, Any]) -> None:
        if self._phase != "message_stop" or (
            self.has_tools
            and len(self._assistant_validated_blocks) != len(self._blocks)
        ):
            fail_protocol()
        self._require_keys(event, {"type"})
        self._phase = "complete"

    def _public_name(self, sdk_name: str) -> str:
        if not sdk_name.startswith(_SDK_PREFIX):
            fail_protocol()
        public_name = sdk_name.removeprefix(_SDK_PREFIX)
        if public_name not in self._public_names:
            fail_protocol()
        return public_name

    def _validated_arguments(
        self, value: object, public_name: str
    ) -> Mapping[str, JsonValue]:
        try:
            arguments = validate_tool_arguments(value)
            self._validators[public_name].validate(plain_json(arguments))
            return arguments
        except Exception:
            fail_protocol()

    def _parse_arguments(
        self, parts: list[str], public_name: str
    ) -> Mapping[str, JsonValue]:
        try:
            # Parameterless tools can emit an empty input_json_delta for the
            # initial empty input object. Typed input and schema are
            # still checked before publishing the call.
            parsed = json.loads("".join(parts) or "{}")
        except json.JSONDecodeError, RecursionError:
            fail_protocol()
        return self._validated_arguments(parsed, public_name)

    def _require_index(self, event: Mapping[str, Any]) -> None:
        if type(event.get("index")) is not int or event["index"] != self._block_index:
            fail_protocol()

    @staticmethod
    def _require_keys(event: Mapping[str, Any], keys: set[str]) -> None:
        if set(event) != keys:
            fail_protocol()


__all__ = ["RawSdkMessageValidator", "RawToolCall"]
