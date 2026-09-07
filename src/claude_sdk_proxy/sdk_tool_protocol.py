from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, cast

from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock
from claude_agent_sdk import ThinkingBlock as SdkThinkingBlock
from jsonschema.validators import validator_for  # type: ignore[import-untyped]

from claude_sdk_proxy.domain import (
    InputUsage,
    RedactedThinkingBlock,
    TextDelta,
    ThinkingBlock,
    ThinkingCompleted,
    ThinkingDelta,
    ToolDefinition,
)
from claude_sdk_proxy.sdk_text_protocol import (
    USAGE_FIELDS,
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
from claude_sdk_proxy.tool_namespace import SDK_TOOL_PREFIX

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
    signature: str = ""
    data: str = ""
    reasoning_events: list[ThinkingDelta | ThinkingCompleted] = dataclass_field(
        default_factory=list
    )


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
        self._input_usage: dict[str, int] = {}
        self._latest_output_tokens: int | None = None
        self.stop_reason: str | None = None
        self._allow_seeded_history = allow_seeded_history
        self._refusal_diagnostic = False

    def observe(
        self, event: Mapping[str, Any]
    ) -> InputUsage | TextDelta | ThinkingDelta | ThinkingCompleted | None:
        event_type = event.get("type")
        if event_type == "message_start":
            return self._message_start(event)
        if event_type == "content_block_start":
            self._block_start(event)
            return None
        if event_type == "content_block_delta":
            return self._block_delta(event)
        if event_type == "content_block_stop":
            return self._block_stop(event)
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
    def thinking_active(self) -> bool:
        return (
            self._phase == "block_delta"
            and self._current is not None
            and self._current.kind == "thinking"
        )

    @property
    def has_tools(self) -> bool:
        return any(block.kind == "tool_use" for block in self._blocks)

    @property
    def tool_activity(self) -> bool:
        return self.has_tools or (
            self._current is not None and self._current.kind == "tool_use"
        )

    @property
    def can_begin_fallback(self) -> bool:
        return (
            self._phase == "block_start"
            and self._current is None
            and not self.tool_activity
            and not self._refusal_diagnostic
        )

    def observe_fallback_termination(
        self, event: Mapping[str, Any], category: str
    ) -> None:
        # The observed discarded leg has no synthetic assistant diagnostic.
        if event.get("type") == "message_delta":
            self._refusal_message_delta(event, category, discarded=True)
        elif event.get("type") == "message_stop":
            self._message_stop(event)
        else:
            fail_protocol()

    @property
    def boundary_usage(self) -> dict[str, int]:
        if self.input_tokens is None or self.output_tokens is None:
            fail_protocol()
        return {**self._input_usage, "output_tokens": self.output_tokens}

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

    @property
    def tool_suffix(
        self,
    ) -> tuple[RawToolCall | ThinkingDelta | ThinkingCompleted, ...]:
        """Raw-order suffix, published only after all tool callbacks are sealed."""
        calls = iter(self.tool_calls)
        events: list[RawToolCall | ThinkingDelta | ThinkingCompleted] = []
        started = False
        for block in self._blocks:
            if block.kind == "tool_use":
                started = True
                events.append(next(calls))
            elif started:
                events.extend(block.reasoning_events)
        return tuple(events)

    def validate_assistant(self, message: AssistantMessage) -> None:
        if (
            message.parent_tool_use_id is not None
            or message.error is not None
            or self._phase not in {"block_delta", "block_start"}
            or not isinstance(message.content, list)
        ):
            fail_protocol()
        content = message.content
        # Preserve the legacy text-only SDK projection: multiple typed text
        # pieces can describe a single raw text block when no tools are enabled.
        if (
            not self._public_names
            and self._current is not None
            and self._current.kind == "text"
            and content
            and all(type(block) is TextBlock for block in content)
        ):
            text_blocks = cast(list[TextBlock], content)
            if any(not isinstance(block.text, str) for block in text_blocks):
                fail_protocol()
            content = [TextBlock("".join(block.text for block in text_blocks))]
        raw_blocks: list[_RawBlock]
        raw_indices: tuple[int, ...]
        if self._phase == "block_delta":
            current_index = len(self._blocks)
            if (
                self._assistant_mode == "batch"
                or self._current is None
                or not self._current.saw_delta
                or current_index in self._assistant_validated_blocks
                or len(content)
                != (0 if self._current.kind == "redacted_thinking" else 1)
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
                or len(content)
                != sum(block.kind != "redacted_thinking" for block in self._blocks)
            ):
                fail_protocol()
            self._assistant_mode = "batch"
            raw_blocks = [*self._blocks]
            raw_indices = tuple(range(len(raw_blocks)))
        # The installed SDK parser drops redacted_thinking only. Compare its
        # precise projection, preserving the raw opaque block separately.
        raw_blocks = [
            block for block in raw_blocks if block.kind != "redacted_thinking"
        ]
        typed_suffix_started = False
        for raw, typed in zip(raw_blocks, content, strict=True):
            if type(typed) is SdkThinkingBlock:
                if (
                    raw.kind != "thinking"
                    or not raw.signature
                    or typed.thinking != "".join(raw.text)
                    or typed.signature != raw.signature
                ):
                    fail_protocol()
                continue
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
        usage = normalize_usage(message.usage, USAGE_FIELDS)
        if usage is not None:
            self._reconcile_usage(usage)
            if "output_tokens" in usage:
                self._latest_output_tokens = usage["output_tokens"]

    @property
    def can_begin_refusal(self) -> bool:
        return (
            self._phase == "block_start"
            and not self._blocks
            and self._current is None
            and self._assistant_mode is None
            and not self._refusal_diagnostic
        )

    def validate_refusal_assistant(self, message: AssistantMessage) -> None:
        content = message.content
        usage = normalize_usage(message.usage, USAGE_FIELDS)
        if (
            not self.can_begin_refusal
            or message.parent_tool_use_id is not None
            or message.model != "<synthetic>"
            or message.error != "invalid_request"
            or message.stop_reason != "refusal"
            or not isinstance(content, list)
            or len(content) != 1
            or type(content[0]) is not TextBlock
            or not isinstance(content[0].text, str)
            or not content[0].text
            or usage is None
            or "input_tokens" not in usage
            or "output_tokens" not in usage
            or any(value != 0 for value in usage.values())
        ):
            fail_protocol()
        self._refusal_diagnostic = True

    def observe_refusal(
        self, event: Mapping[str, Any], category: str
    ) -> InputUsage | TextDelta | ThinkingDelta | ThinkingCompleted | None:
        event_type = event.get("type")
        if not self._refusal_diagnostic:
            fail_protocol()
        if event_type == "message_delta":
            self._refusal_message_delta(event, category)
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
            field: usage[field]
            for field in (
                "input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
            if field in usage
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
        elif block_type == "thinking":
            if (
                set(value) != {"type", "thinking", "signature"}
                or value.get("thinking") != ""
                or value.get("signature") != ""
            ):
                fail_protocol()
            self._current = _RawBlock("thinking", [])
        elif block_type == "redacted_thinking":
            data = value.get("data")
            if set(value) != {"type", "data"} or not isinstance(data, str) or not data:
                fail_protocol()
            self._current = _RawBlock(
                "redacted_thinking", [], data=data, saw_delta=True
            )
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

    def _block_delta(
        self, event: Mapping[str, Any]
    ) -> TextDelta | ThinkingDelta | None:
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
        if self._current.kind == "thinking":
            if delta.get("type") == "thinking_delta":
                estimate = delta.get("estimated_tokens")
                text = delta.get("thinking")
                if (
                    set(delta) - {"type", "thinking", "estimated_tokens"}
                    or not isinstance(text, str)
                    or self._current.signature
                    or (
                        estimate is not None
                        and (type(estimate) is not int or estimate < 0)
                    )
                ):
                    fail_protocol()
                self._current.text[:] = ["".join(self._current.text) + text]
                self._current.saw_delta = True
                thinking_delta = ThinkingDelta(self._block_index, text)
                retained = self._current.reasoning_events
                if retained and isinstance(retained[-1], ThinkingDelta):
                    retained[-1] = ThinkingDelta(
                        self._block_index, retained[-1].text + text
                    )
                else:
                    retained.append(thinking_delta)
                return None if self._tool_suffix_started else thinking_delta
            if delta.get("type") == "signature_delta":
                signature = delta.get("signature")
                if (
                    set(delta) != {"type", "signature"}
                    or not isinstance(signature, str)
                    or not signature
                ):
                    fail_protocol()
                self._current.signature += signature
                self._current.saw_delta = True
                return None
            fail_protocol()
        if self._current.kind == "redacted_thinking":
            fail_protocol()
        if self._current.kind == "text":
            if set(delta) != {"type", "text"} or delta.get("type") != "text_delta":
                fail_protocol()
            text = delta.get("text")
            if not isinstance(text, str):
                fail_protocol()
            self._current.saw_delta = True
            self._current.text[:] = ["".join(self._current.text) + text]
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
        self._current.json_parts[:] = ["".join(self._current.json_parts) + partial]
        self._current.json_chars += len(partial)
        if self._current.json_chars > _MAX_RAW_ARGUMENT_CHARS:
            fail_protocol()
        return None

    def _block_stop(self, event: Mapping[str, Any]) -> ThinkingCompleted | None:
        if (
            self._phase != "block_delta"
            or self._current is None
            or not self._current.saw_delta
        ):
            fail_protocol()
        self._require_keys(event, {"type", "index"})
        self._require_index(event)
        completed = None
        if self._current.kind == "thinking":
            if not self._current.signature:
                fail_protocol()
            completed = ThinkingCompleted(
                self._block_index,
                ThinkingBlock("".join(self._current.text), self._current.signature),
            )
        elif self._current.kind == "redacted_thinking":
            completed = ThinkingCompleted(
                self._block_index, RedactedThinkingBlock(self._current.data)
            )
        if completed is not None:
            self._current.reasoning_events.append(completed)
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
        return None if self._tool_suffix_started else completed

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
        usage = normalize_usage(event.get("usage"), USAGE_FIELDS)
        if usage is None or "output_tokens" not in usage:
            fail_protocol()
        self._reconcile_usage(usage)
        self.stop_reason = cast(str, delta["stop_reason"])
        self.output_tokens = usage["output_tokens"]
        self._phase = "message_stop"

    def _refusal_message_delta(
        self, event: Mapping[str, Any], category: str, *, discarded: bool = False
    ) -> None:
        if (
            self._phase != "block_start"
            or (self._blocks and not discarded)
            or self._current is not None
        ):
            fail_protocol()
        self._require_keys(event, {"type", "delta", "usage", "context_management"})
        delta = event.get("delta")
        if discarded and event.get("context_management") is None:
            self._bundled_fallback_close(event)
            return
        if (
            not isinstance(delta, Mapping)
            or set(delta) != {"stop_reason", "stop_sequence", "stop_details"}
            or delta.get("stop_reason") != "refusal"
            or delta.get("stop_sequence") is not None
        ):
            fail_protocol()
        details = delta.get("stop_details")
        if (
            not isinstance(details, Mapping)
            or set(details)
            not in (
                {"type", "category", "explanation"},
                {"type", "category", "explanation", "fallback_has_prefill_claim"},
            )
            or details.get("type") != "refusal"
            or details.get("category") != category
            or not isinstance(details.get("explanation"), str)
            or not details["explanation"]
            or details.get("fallback_has_prefill_claim", False) is not False
        ):
            fail_protocol()
        context = event.get("context_management")
        if not isinstance(context, Mapping) or dict(context) != {"applied_edits": []}:
            fail_protocol()
        usage = normalize_usage(event.get("usage"), USAGE_FIELDS)
        if (
            usage is None
            or "output_tokens" not in usage
            or (not discarded and usage["output_tokens"] != 0)
        ):
            fail_protocol()
        self._reconcile_usage(usage)
        self.stop_reason = "refusal"
        self.output_tokens = usage["output_tokens"]
        self._phase = "message_stop"

    def _bundled_fallback_close(self, event: Mapping[str, Any]) -> None:
        # Claude Code 2.1.259 Qs/Xs closes a retracted partial stream after the
        # validated fallback banner. This is not the no-fallback API refusal.
        if event.get("delta") != {
            "container": None,
            "stop_details": None,
            "stop_reason": "refusal",
            "stop_sequence": None,
        }:
            fail_protocol()
        raw_usage = event.get("usage")
        ancillary = {"output_tokens_details", "iterations", "server_tool_use"}
        if (
            not isinstance(raw_usage, Mapping)
            or set(raw_usage) != set(USAGE_FIELDS) | ancillary
            or raw_usage["iterations"] not in (None, [])
        ):
            fail_protocol()
        # Source-correlated live shapes only; nonempty iterations are unobserved.
        for field, keys in (
            ("output_tokens_details", {"thinking_tokens"}),
            ("server_tool_use", {"web_search_requests", "web_fetch_requests"}),
        ):
            details = raw_usage[field]
            if details is None:
                continue
            if (
                not isinstance(details, Mapping)
                or set(details) != keys
                or any(
                    type(value) is not int or value < 0 for value in details.values()
                )
            ):
                fail_protocol()
        # Only Xs's nullable input/cache counters may be absent semantically.
        # Output remains required; known counters retain normal reconciliation.
        usage = normalize_usage(
            {
                field: raw_usage[field]
                for field in USAGE_FIELDS
                if field == "output_tokens" or raw_usage[field] is not None
            },
            USAGE_FIELDS,
        )
        assert usage is not None
        self._reconcile_usage(usage)
        self.stop_reason = "refusal"
        self.output_tokens = usage["output_tokens"]
        self._phase = "message_stop"

    def _reconcile_usage(self, usage: Mapping[str, int]) -> None:
        for field in (
            "input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            if field in usage and usage[field] != self._input_usage.get(field):
                fail_protocol()
        if (
            "output_tokens" in usage
            and self._latest_output_tokens is not None
            and usage["output_tokens"] < self._latest_output_tokens
        ):
            fail_protocol()

    def _message_stop(self, event: Mapping[str, Any]) -> None:
        if self._phase != "message_stop" or (
            (
                self.has_tools
                or any(
                    block.kind in {"thinking", "redacted_thinking"}
                    for block in self._blocks
                )
            )
            and len(self._assistant_validated_blocks) != len(self._blocks)
        ):
            fail_protocol()
        self._require_keys(event, {"type"})
        self._phase = "complete"

    def _public_name(self, sdk_name: str) -> str:
        if not sdk_name.startswith(SDK_TOOL_PREFIX):
            fail_protocol()
        public_name = sdk_name.removeprefix(SDK_TOOL_PREFIX)
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
