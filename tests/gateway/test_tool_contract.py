from __future__ import annotations

from types import MappingProxyType

import pytest

from claude_sdk_proxy.domain import (
    CanonicalMessage,
    RequestValidationError,
    TextBlock,
    TextRequest,
    ToolCall,
    ToolCallBlock,
    ToolDefinition,
    ToolResultBlock,
)
from claude_sdk_proxy.tool_contract import (
    MAX_JSON_CONTAINER_DEPTH,
    canonical_json,
    freeze_json,
    plain_json,
    validate_tool_arguments,
    validate_tool_definitions,
    validate_tool_results,
)

JSON_CONTAINER_DEPTH_LIMIT = 64


def nested_mapping(depth: int, leaf: object = True) -> dict[str, object]:
    value = leaf
    for _ in range(depth):
        value = {"not": value}
    assert isinstance(value, dict)
    return value


def nested_arguments(depth: int) -> dict[str, object]:
    value: object = "leaf"
    for _ in range(depth - 1):
        value = [value]
    return {"value": value}


def nested_frozen_arguments(depth: int) -> MappingProxyType[str, object]:
    value: object = "leaf"
    for _ in range(depth - 1):
        value = (value,)
    return MappingProxyType({"value": value})


def echo_definition(schema: dict[str, object] | None = None) -> ToolDefinition:
    return ToolDefinition(
        "echo",
        "Repeat the provided value.",
        {"type": "object", "properties": {"value": {"type": "string"}}}
        if schema is None
        else schema,
    )


def tool_request(messages: tuple[CanonicalMessage, ...]) -> TextRequest:
    return TextRequest(
        dialect="openai",
        model="sonnet",
        system="",
        messages=messages,
        tools=(echo_definition(),),
        max_tokens=1024,
        stream=True,
    )


def test_text_only_construction_keeps_content_helpers_and_defaults() -> None:
    request = TextRequest(
        "sonnet",
        "system",
        (CanonicalMessage("user", "hello"),),
        1024,
        True,
    )

    assert request.dialect == "anthropic"
    assert request.tools == ()
    assert request.messages[0].blocks == (TextBlock("hello"),)
    assert request.messages[0].content == "hello"
    assert request.next_input == "hello"
    assert CanonicalMessage.user_text("ask") == CanonicalMessage("user", "ask")
    assert CanonicalMessage.assistant_text("answer") == CanonicalMessage(
        "assistant", "answer"
    )


def test_gateway_request_accepts_complete_result_turn_in_any_order() -> None:
    request = tool_request(
        (
            CanonicalMessage.user_text("use both"),
            CanonicalMessage(
                "assistant",
                (
                    ToolCallBlock("call_a", "echo", {"value": "same"}),
                    ToolCallBlock("call_b", "echo", {"value": "same"}),
                ),
            ),
            CanonicalMessage(
                "user",
                (
                    ToolResultBlock("call_b", ("second",), False),
                    ToolResultBlock("call_a", ("first",), False),
                ),
            ),
        )
    )

    assert request.next_input == (
        ToolResultBlock("call_a", ("first",), False),
        ToolResultBlock("call_b", ("second",), False),
    )
    assert request.messages[-1].blocks == request.next_input


def test_gateway_request_rejects_mixed_user_text_and_tool_results() -> None:
    with pytest.raises(RequestValidationError) as error:
        tool_request(
            (
                CanonicalMessage.user_text("use echo"),
                CanonicalMessage("assistant", (ToolCallBlock("call_a", "echo", {}),)),
                CanonicalMessage(
                    "user",
                    (TextBlock("mixed"), ToolResultBlock("call_a", ("ok",), False)),
                ),
            )
        )

    assert error.value.field == "messages"


def test_gateway_request_rejects_duplicate_result_ids() -> None:
    with pytest.raises(RequestValidationError) as error:
        tool_request(
            (
                CanonicalMessage.user_text("use echo"),
                CanonicalMessage("assistant", (ToolCallBlock("call_a", "echo", {}),)),
                CanonicalMessage(
                    "user",
                    (
                        ToolResultBlock("call_a", ("first",), False),
                        ToolResultBlock("call_a", ("second",), False),
                    ),
                ),
            )
        )

    assert error.value.field == "messages"


def test_gateway_request_requires_results_before_later_user_text() -> None:
    with pytest.raises(RequestValidationError) as error:
        tool_request(
            (
                CanonicalMessage.user_text("use echo"),
                CanonicalMessage("assistant", (ToolCallBlock("call_a", "echo", {}),)),
                CanonicalMessage.user_text("skip the result"),
            )
        )

    assert error.value.field == "messages"


def test_blocks_copy_and_recursively_freeze_caller_json() -> None:
    schema: dict[str, object] = {"type": "object", "properties": {"v": [1]}}
    arguments: dict[str, object] = {"nested": {"items": ["before"]}}
    definition = ToolDefinition("echo", "", schema)
    call = ToolCallBlock("call_1", "echo", arguments)
    schema["properties"] = {"changed": True}
    arguments["nested"] = {"items": ["after"]}

    assert definition.input_schema == {
        "type": "object",
        "properties": {"v": (1,)},
    }
    assert call.arguments == {"nested": {"items": ("before",)}}
    assert isinstance(definition.input_schema, MappingProxyType)
    assert isinstance(definition.input_schema["properties"], MappingProxyType)
    assert isinstance(call.arguments, MappingProxyType)
    assert isinstance(call.arguments["nested"], MappingProxyType)
    with pytest.raises(TypeError):
        definition.input_schema["new"] = "value"  # type: ignore[index]
    with pytest.raises(TypeError):
        definition.input_schema["properties"]["new"] = "value"  # type: ignore[index]
    with pytest.raises(TypeError):
        definition.input_schema["properties"]["v"][0] = 2  # type: ignore[index]
    with pytest.raises(TypeError):
        call.arguments["nested"]["new"] = "value"  # type: ignore[index]
    with pytest.raises(TypeError):
        call.arguments["nested"]["items"][0] = "changed"  # type: ignore[index]


def test_canonical_json_is_compact_sorted_and_rejects_non_json_values() -> None:
    assert canonical_json({"snowman": "☃", "a": [True, None, 1]}) == (
        '{"a":[true,null,1],"snowman":"☃"}'
    )
    with pytest.raises(RequestValidationError) as infinity:
        canonical_json({"value": float("inf")})
    assert infinity.value.field == "tools"
    with pytest.raises(RequestValidationError) as non_string_key:
        canonical_json({1: "value"})
    assert non_string_key.value.field == "tools"


def test_canonical_json_rejects_recursive_input() -> None:
    recursive: dict[str, object] = {}
    recursive["self"] = recursive

    with pytest.raises(RequestValidationError) as error:
        canonical_json(recursive)  # type: ignore[arg-type]

    assert error.value.field == "tools"


def test_json_container_depth_accepts_exact_limit_and_rejects_first_over() -> None:
    assert MAX_JSON_CONTAINER_DEPTH == JSON_CONTAINER_DEPTH_LIMIT
    accepted_schema = nested_mapping(JSON_CONTAINER_DEPTH_LIMIT)
    accepted_arguments = nested_arguments(JSON_CONTAINER_DEPTH_LIMIT)

    assert validate_tool_definitions((echo_definition(accepted_schema),))
    assert validate_tool_arguments(accepted_arguments)

    with pytest.raises(RequestValidationError, match="nesting") as schema_error:
        echo_definition(nested_mapping(JSON_CONTAINER_DEPTH_LIMIT + 1))
    assert schema_error.value.field == "tools"
    with pytest.raises(RequestValidationError, match="nesting") as arguments_error:
        validate_tool_arguments(nested_arguments(JSON_CONTAINER_DEPTH_LIMIT + 1))
    assert arguments_error.value.field == "tools"


def test_all_json_normalizers_enforce_the_same_container_depth() -> None:
    over_depth = JSON_CONTAINER_DEPTH_LIMIT + 1

    for operation, value in (
        (freeze_json, nested_arguments(over_depth)),
        (plain_json, nested_frozen_arguments(over_depth)),
        (canonical_json, nested_arguments(over_depth)),
    ):
        with pytest.raises(RequestValidationError, match="nesting") as error:
            operation(value)  # type: ignore[arg-type]
        assert error.value.field == "tools"


def test_historical_arguments_map_depth_failure_to_messages() -> None:
    with pytest.raises(RequestValidationError, match="nesting") as error:
        ToolCallBlock(
            "call_a",
            "echo",
            nested_arguments(JSON_CONTAINER_DEPTH_LIMIT + 1),
        )

    assert error.value.field == "messages"


def test_tool_definition_validation_sorts_names_and_rejects_duplicates() -> None:
    normalized = validate_tool_definitions(
        (ToolDefinition("zebra", "", {"type": "object"}), echo_definition())
    )

    assert tuple(item.name for item in normalized) == ("echo", "zebra")
    with pytest.raises(RequestValidationError) as error:
        validate_tool_definitions((echo_definition(), echo_definition()))
    assert error.value.field == "tools"


def test_tool_definition_limits_are_measured_after_canonical_json() -> None:
    oversized = ToolDefinition("echo", "", {"type": "object", "x": "x" * 65536})

    with pytest.raises(RequestValidationError) as error:
        validate_tool_definitions((oversized,))

    assert error.value.field == "tools"


def test_tool_definition_count_accepts_128_and_rejects_129() -> None:
    accepted = tuple(ToolDefinition(f"tool{index}", "", {}) for index in range(128))
    rejected = accepted + (ToolDefinition("tool129", "", {}),)

    assert len(validate_tool_definitions(accepted)) == 128
    with pytest.raises(RequestValidationError) as error:
        validate_tool_definitions(rejected)
    assert error.value.field == "tools"


def test_tool_description_byte_limit_accepts_8_kib_and_rejects_one_more_byte() -> None:
    accepted = ToolDefinition("echo", "d" * (8 * 1024), {})
    rejected = ToolDefinition("echo", "d" * (8 * 1024 + 1), {})

    assert validate_tool_definitions((accepted,)) == (accepted,)
    with pytest.raises(RequestValidationError) as error:
        validate_tool_definitions((rejected,))
    assert error.value.field == "tools"


def test_aggregate_schema_limit_accepts_512_kib_and_rejects_one_more_byte() -> None:
    schema = {"x": "s" * (64 * 1024 - 8)}
    accepted = tuple(ToolDefinition(f"tool{index}", "", schema) for index in range(8))
    rejected = (
        accepted[:-1]
        + (ToolDefinition("tool7", "", {"x": "s" * (64 * 1024 - 9)}),)
        + (ToolDefinition("tool8", "", {}),)
    )

    assert len(validate_tool_definitions(accepted)) == 8
    with pytest.raises(RequestValidationError) as error:
        validate_tool_definitions(rejected)
    assert error.value.field == "tools"


@pytest.mark.parametrize(
    "schema",
    (
        {"type": "object", "properties": {"x": {"type": "not-a-type"}}},
        {"type": "object", "properties": {"x": {"minimum": "zero"}}},
        [],
    ),
)
def test_tool_definition_rejects_malformed_schema(schema: object) -> None:
    with pytest.raises(RequestValidationError) as error:
        validate_tool_definitions((ToolDefinition("echo", "", schema),))  # type: ignore[arg-type]

    assert error.value.field == "tools"


def test_tool_definition_maps_unsupported_schema_dialect_to_request_error() -> None:
    with pytest.raises(RequestValidationError) as error:
        validate_tool_definitions(
            (
                ToolDefinition(
                    "echo",
                    "",
                    {"$schema": "https://example.test/unsupported-schema"},
                ),
            )
        )

    assert error.value.field == "tools"


def test_schema_validation_accepts_self_contained_fragment_references() -> None:
    schema = {
        "$defs": {
            "leaf": {"$anchor": "leaf", "type": "string"},
            "nested": {
                "$id": "nested",
                "$defs": {"item": {"type": "integer"}},
                "allOf": [{"$ref": "#/$defs/item"}],
            },
        },
        "allOf": [
            {"$ref": "#/$defs/leaf"},
            {"$dynamicRef": "#leaf"},
            {"$recursiveRef": "#/$defs/nested"},
        ],
    }

    assert validate_tool_definitions((ToolDefinition("echo", "", schema),)) == (
        ToolDefinition("echo", "", schema),
    )


@pytest.mark.parametrize(
    "reference", ("https://example.test/schema", "other.json#x", "#/$defs/missing")
)
def test_schema_validation_rejects_external_relative_or_missing_reference(
    reference: str,
) -> None:
    with pytest.raises(RequestValidationError) as error:
        validate_tool_definitions((ToolDefinition("echo", "", {"$ref": reference}),))

    assert error.value.field == "tools"


def test_schema_reference_walk_ignores_non_schema_reference_lookalikes() -> None:
    schema = {
        "type": "object",
        "const": {"$ref": "literal"},
        "examples": [{"$dynamicRef": "literal"}],
        "properties": {"$ref": {"type": "string"}},
    }

    assert validate_tool_definitions((ToolDefinition("echo", "", schema),)) == (
        ToolDefinition("echo", "", schema),
    )


def test_tool_argument_validation_requires_a_bounded_json_object() -> None:
    assert validate_tool_arguments({"b": 2, "a": {"items": [True]}}) == {
        "b": 2,
        "a": {"items": (True,)},
    }
    with pytest.raises(RequestValidationError) as array_error:
        validate_tool_arguments(["not", "an", "object"])  # type: ignore[arg-type]
    assert array_error.value.field == "tools"
    with pytest.raises(RequestValidationError) as size_error:
        validate_tool_arguments({"value": "x" * 262144})
    assert size_error.value.field == "tools"


def test_tool_result_validation_normalizes_order_and_rejects_invalid_values() -> None:
    normalized = validate_tool_results(
        (
            ToolResultBlock("call_b", ("",), False),
            ToolResultBlock("call_a", (), False),
        )
    )

    assert normalized == (
        ToolResultBlock("call_a", (), False),
        ToolResultBlock("call_b", ("",), False),
    )
    with pytest.raises(RequestValidationError) as duplicate:
        validate_tool_results(
            (
                ToolResultBlock("call_a", ("one",), False),
                ToolResultBlock("call_a", ("two",), False),
            )
        )
    assert duplicate.value.field == "messages"
    with pytest.raises(RequestValidationError) as empty_error:
        validate_tool_results((ToolResultBlock("call_a", (), True),))
    assert empty_error.value.field == "messages"
    with pytest.raises(RequestValidationError) as flag_error:
        validate_tool_results((ToolResultBlock("call_a", ("error",), 1),))  # type: ignore[arg-type]
    assert flag_error.value.field == "messages"


def test_tool_result_byte_limit_accepts_256_kib_and_rejects_one_more_byte() -> None:
    accepted = ToolResultBlock("call_a", ("r" * (256 * 1024),), False)
    rejected = ToolResultBlock("call_a", ("r" * (256 * 1024 + 1),), False)

    assert validate_tool_results((accepted,)) == (accepted,)
    with pytest.raises(RequestValidationError) as error:
        validate_tool_results((rejected,))
    assert error.value.field == "messages"


def test_aggregate_tool_result_limit_accepts_one_mib_and_rejects_one_more_byte() -> (
    None
):
    accepted = tuple(
        ToolResultBlock(f"call_{index}", ("r" * (256 * 1024),), False)
        for index in range(4)
    )
    rejected = accepted + (ToolResultBlock("call_4", ("r",), False),)

    assert len(validate_tool_results(accepted)) == 4
    with pytest.raises(RequestValidationError) as error:
        validate_tool_results(rejected)
    assert error.value.field == "messages"


def test_tool_call_event_copies_and_freezes_arguments() -> None:
    arguments: dict[str, object] = {"nested": [1]}
    event = ToolCall("call_1", "echo", arguments)
    arguments["nested"] = [2]

    assert event.arguments == {"nested": (1,)}
    with pytest.raises(TypeError):
        event.arguments["new"] = "value"  # type: ignore[index]
