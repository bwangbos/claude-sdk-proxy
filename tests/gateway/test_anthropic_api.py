from __future__ import annotations

import json

import pytest

from quaylet.anthropic_api import (
    AnthropicStreamState,
    encode_anthropic_error,
    encode_anthropic_event,
    encode_anthropic_start,
    parse_anthropic_request,
    render_anthropic_response,
)
from quaylet.domain import (
    CanonicalMessage,
    Completed,
    RequestValidationError,
    TextBlock,
    TextDelta,
    ToolCall,
    ToolCallBlock,
    ToolDefinition,
    ToolResultBlock,
    UnsupportedFeature,
)


def event_name(chunk: bytes) -> str:
    return chunk.splitlines()[0].removeprefix(b"event: ").decode()


def payload(chunk: bytes) -> object:
    return json.loads(chunk.split(b"data: ", maxsplit=1)[1])


def anthropic_echo_tool() -> dict[str, object]:
    return {
        "name": "echo",
        "description": "Repeat the provided value.",
        "input_schema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
    }


def test_anthropic_parser_preserves_text_messages_and_required_token_limit() -> None:
    request = parse_anthropic_request(
        {
            "model": "sonnet",
            "system": "system",
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "answer"},
                {"role": "user", "content": "next"},
            ],
            "max_tokens": 321,
            "stream": True,
        },
        frozenset({"sonnet"}),
    )
    assert request.system == "system"
    assert request.messages == (
        CanonicalMessage("user", "hello"),
        CanonicalMessage("assistant", "answer"),
        CanonicalMessage("user", "next"),
    )
    assert request.max_tokens == 321
    assert request.stream is True


def test_anthropic_parser_keeps_empty_fresh_tools_on_the_text_only_path() -> None:
    request = parse_anthropic_request(
        {
            "model": "sonnet",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 1,
            "tools": [],
        },
        frozenset({"sonnet"}),
    )

    assert request.tools == ()
    assert request.next_input == "hello"


@pytest.mark.parametrize(
    "body,field",
    [
        (
            {"model": "sonnet", "messages": [{"role": "user", "content": "hi"}]},
            "max_tokens",
        ),
        (
            {
                "model": "sonnet",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 0,
            },
            "max_tokens",
        ),
        (
            {
                "model": "sonnet",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
                "temperature": 0,
            },
            "temperature",
        ),
        (
            {
                "model": "sonnet",
                "messages": [{"role": "user", "content": [{"type": "image"}]}],
                "max_tokens": 1,
            },
            "messages",
        ),
    ],
)
def test_anthropic_parser_fails_closed_for_nontext_features(
    body: dict[str, object], field: str
) -> None:
    with pytest.raises((RequestValidationError, UnsupportedFeature)) as error:
        parse_anthropic_request(body, frozenset({"sonnet"}))
    assert error.value.field == field


def test_anthropic_parser_normalizes_parallel_calls_and_reverse_results() -> None:
    request = parse_anthropic_request(
        {
            "model": "sonnet",
            "max_tokens": 1024,
            "tools": [anthropic_echo_tool()],
            "tool_choice": {"type": "auto", "disable_parallel_tool_use": False},
            "messages": [
                {"role": "user", "content": "twice"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_a",
                            "name": "echo",
                            "input": {"value": "one"},
                        },
                        {
                            "type": "tool_use",
                            "id": "toolu_b",
                            "name": "echo",
                            "input": {"value": "two"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_b",
                            "content": "B",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_a",
                            "content": [{"type": "text", "text": "A"}],
                        },
                    ],
                },
            ],
        },
        frozenset({"sonnet"}),
    )

    assert request.tools == (
        ToolDefinition(
            "echo",
            "Repeat the provided value.",
            {"type": "object", "properties": {"value": {"type": "string"}}},
        ),
    )
    assert request.messages[1].blocks == (
        ToolCallBlock("toolu_a", "echo", {"value": "one"}),
        ToolCallBlock("toolu_b", "echo", {"value": "two"}),
    )
    assert request.next_input == (
        ToolResultBlock("toolu_a", ("A",), False),
        ToolResultBlock("toolu_b", ("B",), False),
    )


@pytest.mark.parametrize(
    "content,want",
    [
        ("", ("",)),
        ([], ()),
        ([{"type": "text", "text": ""}], ("",)),
        (
            [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}],
            ("first", "second"),
        ),
    ],
)
def test_anthropic_parser_preserves_successful_tool_result_text(
    content: object, want: tuple[str, ...]
) -> None:
    request = parse_anthropic_request(
        {
            "model": "sonnet",
            "max_tokens": 1,
            "tools": [anthropic_echo_tool()],
            "messages": [
                {"role": "user", "content": "call"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_call",
                            "name": "echo",
                            "input": {},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_call",
                            "content": content,
                        }
                    ],
                },
            ],
        },
        frozenset({"sonnet"}),
    )

    assert request.next_input == (ToolResultBlock("toolu_call", want, False),)


def test_anthropic_parser_accepts_nonempty_error_result() -> None:
    request = parse_anthropic_request(
        {
            "model": "sonnet",
            "max_tokens": 1,
            "tools": [anthropic_echo_tool()],
            "messages": [
                {"role": "user", "content": "call"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_call",
                            "name": "echo",
                            "input": {},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_call",
                            "content": "failed",
                            "is_error": True,
                        }
                    ],
                },
            ],
        },
        frozenset({"sonnet"}),
    )

    assert request.next_input == (ToolResultBlock("toolu_call", ("failed",), True),)


@pytest.mark.parametrize(
    "body,field,error_type",
    [
        (
            {
                "model": "sonnet",
                "max_tokens": 1,
                "tools": [anthropic_echo_tool(), anthropic_echo_tool()],
                "messages": [{"role": "user", "content": "call"}],
            },
            "tools",
            RequestValidationError,
        ),
        (
            {
                "model": "sonnet",
                "max_tokens": 1,
                "tools": [anthropic_echo_tool()],
                "messages": [
                    {"role": "user", "content": "call"},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_same",
                                "name": "echo",
                                "input": {},
                            },
                            {
                                "type": "tool_use",
                                "id": "toolu_same",
                                "name": "echo",
                                "input": {},
                            },
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_same",
                                "content": "ok",
                            }
                        ],
                    },
                ],
            },
            "messages",
            RequestValidationError,
        ),
        (
            {
                "model": "sonnet",
                "max_tokens": 1,
                "tools": [anthropic_echo_tool()],
                "messages": [
                    {"role": "user", "content": "call"},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_a",
                                "name": "echo",
                                "input": {},
                            },
                            {
                                "type": "tool_use",
                                "id": "toolu_b",
                                "name": "echo",
                                "input": {},
                            },
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_a",
                                "content": "only one",
                            }
                        ],
                    },
                ],
            },
            "messages",
            RequestValidationError,
        ),
        (
            {
                "model": "sonnet",
                "max_tokens": 1,
                "tools": [anthropic_echo_tool()],
                "messages": [
                    {"role": "user", "content": "call"},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_call",
                                "name": "echo",
                                "input": {},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "mixed"},
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_call",
                                "content": "ok",
                            },
                        ],
                    },
                ],
            },
            "messages",
            RequestValidationError,
        ),
        (
            {
                "model": "sonnet",
                "max_tokens": 1,
                "tools": [anthropic_echo_tool()],
                "messages": [
                    {"role": "user", "content": "call"},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_call",
                                "name": "echo",
                                "input": {},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_call",
                                "content": "",
                                "is_error": True,
                            }
                        ],
                    },
                ],
            },
            "messages",
            RequestValidationError,
        ),
        (
            {
                "model": "sonnet",
                "max_tokens": 1,
                "tools": [
                    {
                        "name": "echo",
                        "description": "Repeat the provided value.",
                        "input_schema": {"type": "definitely-not-a-json-schema-type"},
                    }
                ],
                "messages": [{"role": "user", "content": "call"}],
            },
            "tools",
            RequestValidationError,
        ),
        (
            {
                "model": "sonnet",
                "max_tokens": 1,
                "tools": [anthropic_echo_tool()],
                "messages": [
                    {"role": "user", "content": "call"},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_call",
                                "name": "echo",
                                "input": {},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_call",
                                "content": [{"type": "image", "source": {}}],
                            }
                        ],
                    },
                ],
            },
            "messages",
            UnsupportedFeature,
        ),
    ],
)
def test_anthropic_parser_rejects_invalid_tool_subset(
    body: dict[str, object], field: str, error_type: type[Exception]
) -> None:
    with pytest.raises(error_type) as error:
        parse_anthropic_request(body, frozenset({"sonnet"}))
    assert error.value.field == field  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "tool_choice",
    [
        {"type": "tool", "name": "echo"},
        {"type": "none"},
        {"type": "auto", "disable_parallel_tool_use": True},
    ],
)
def test_anthropic_parser_rejects_semantically_unsupported_tool_choices(
    tool_choice: object,
) -> None:
    with pytest.raises(UnsupportedFeature) as error:
        parse_anthropic_request(
            {
                "model": "sonnet",
                "max_tokens": 1,
                "tools": [anthropic_echo_tool()],
                "tool_choice": tool_choice,
                "messages": [{"role": "user", "content": "call"}],
            },
            frozenset({"sonnet"}),
        )
    assert error.value.field == "tool_choice"


def test_anthropic_parser_rejects_unknown_model() -> None:
    with pytest.raises(RequestValidationError) as error:
        parse_anthropic_request(
            {
                "model": "other",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
            },
            frozenset({"sonnet"}),
        )
    assert error.value.field == "model"


def test_anthropic_parser_rejects_extra_tool_message_fields() -> None:
    with pytest.raises(UnsupportedFeature) as error:
        parse_anthropic_request(
            {
                "model": "sonnet",
                "messages": [
                    {
                        "role": "user",
                        "content": "hello",
                        "tool_use_id": "toolu_1",
                    }
                ],
                "max_tokens": 1,
            },
            frozenset({"sonnet"}),
        )
    assert error.value.field == "messages"


@pytest.mark.parametrize("role", [[], {}])
def test_anthropic_parser_rejects_unhashable_message_roles(role: object) -> None:
    with pytest.raises(RequestValidationError) as error:
        parse_anthropic_request(
            {
                "model": "sonnet",
                "messages": [{"role": role, "content": "hello"}],
                "max_tokens": 1,
            },
            frozenset({"sonnet"}),
        )
    assert error.value.field == "messages"


@pytest.mark.parametrize("field,value", [("extra", 1), ("stream", 1)])
def test_anthropic_parser_rejects_unknown_fields_and_nonboolean_stream(
    field: str, value: object
) -> None:
    with pytest.raises(RequestValidationError) as error:
        parse_anthropic_request(
            {
                "model": "sonnet",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 1,
                field: value,
            },
            frozenset({"sonnet"}),
        )
    assert error.value.field == field


def test_anthropic_stream_uses_message_content_and_stop_order() -> None:
    start = encode_anthropic_start("msg_test", "sonnet", input_tokens=7)
    delta = encode_anthropic_event("msg_test", "sonnet", TextDelta("hello"))
    completed = encode_anthropic_event(
        "msg_test",
        "sonnet",
        Completed("end_turn", {"input_tokens": 2, "output_tokens": 1}),
    )

    assert [event_name(chunk) for chunk in (*start, *delta, *completed)] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert payload(start[0])["message"]["id"] == "msg_test"
    assert payload(start[0])["message"]["usage"] == {
        "input_tokens": 7,
        "output_tokens": 0,
    }
    assert payload(delta[0])["delta"] == {"type": "text_delta", "text": "hello"}
    assert payload(completed[1]) == {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 1},
    }


def test_anthropic_text_only_stream_state_closes_its_eager_text_block() -> None:
    state = AnthropicStreamState()
    start = encode_anthropic_start("msg_test", "sonnet", state=state)
    delta = encode_anthropic_event(
        "msg_test", "sonnet", TextDelta("hello"), state=state
    )
    completed = encode_anthropic_event(
        "msg_test", "sonnet", Completed("end_turn", {"output_tokens": 1}), state=state
    )

    assert [event_name(chunk) for chunk in (*start, *delta, *completed)] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert payload(completed[0]) == {"type": "content_block_stop", "index": 0}


def test_anthropic_tool_stream_state_assigns_monotonic_indexes() -> None:
    state = AnthropicStreamState()
    start = encode_anthropic_start(
        "msg_test", "sonnet", tools=(ToolDefinition("echo", "", {}),), state=state
    )
    text = encode_anthropic_event(
        "msg_test", "sonnet", TextDelta("before"), state=state
    )
    first_call = encode_anthropic_event(
        "msg_test",
        "sonnet",
        ToolCall("toolu_a", "echo", {}),
        block_index=99,
        state=state,
    )
    second_call = encode_anthropic_event(
        "msg_test",
        "sonnet",
        ToolCall("toolu_b", "echo", {}),
        block_index=7,
        state=state,
    )

    assert [event_name(chunk) for chunk in start] == ["message_start"]
    assert payload(text[0])["index"] == 0
    assert payload(first_call[0])["index"] == 0
    assert payload(first_call[1])["index"] == 1
    assert payload(second_call[0])["index"] == 2


def test_anthropic_call_only_stream_has_complete_native_block_sequence() -> None:
    state = AnthropicStreamState()
    start = encode_anthropic_start(
        "msg_test", "sonnet", tools=(ToolDefinition("echo", "", {}),), state=state
    )
    call = encode_anthropic_event(
        "msg_test", "sonnet", ToolCall("toolu_a", "echo", {"value": "one"}), state=state
    )
    completed = encode_anthropic_event(
        "msg_test", "sonnet", Completed("tool_use", {"output_tokens": 1}), state=state
    )

    assert [event_name(chunk) for chunk in (*start, *call, *completed)] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert payload(call[0]) == {
        "type": "content_block_start",
        "index": 0,
        "content_block": {
            "type": "tool_use",
            "id": "toolu_a",
            "name": "echo",
            "input": {},
        },
    }
    assert payload(call[1])["delta"] == {
        "type": "input_json_delta",
        "partial_json": '{"value":"one"}',
    }


def test_anthropic_stream_renders_complete_tool_argument_delta() -> None:
    chunks = encode_anthropic_event(
        "msg_1",
        "sonnet",
        ToolCall("toolu_1", "echo", {"snowman": "☃"}),
        block_index=1,
    )

    assert [event_name(chunk) for chunk in chunks] == [
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
    ]
    assert payload(chunks[0]) == {
        "type": "content_block_start",
        "index": 1,
        "content_block": {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "echo",
            "input": {},
        },
    }
    assert payload(chunks[1])["delta"]["partial_json"] == '{"snowman":"☃"}'


def test_anthropic_tool_enabled_stream_starts_blocks_lazily_and_in_order() -> None:
    state = AnthropicStreamState()
    start = encode_anthropic_start(
        "msg_test", "sonnet", tools=(ToolDefinition("echo", "", {}),), state=state
    )
    text = encode_anthropic_event(
        "msg_test", "sonnet", TextDelta("first"), block_index=0, state=state
    )
    first_call = encode_anthropic_event(
        "msg_test",
        "sonnet",
        ToolCall("toolu_a", "echo", {"n": 1}),
        block_index=1,
        state=state,
    )
    second_call = encode_anthropic_event(
        "msg_test",
        "sonnet",
        ToolCall("toolu_b", "echo", {"n": 2}),
        block_index=2,
        state=state,
    )
    completed = encode_anthropic_event(
        "msg_test",
        "sonnet",
        Completed("tool_use", {"output_tokens": 2}),
        block_index=2,
        state=state,
    )

    assert [
        event_name(chunk)
        for chunk in (*start, *text, *first_call, *second_call, *completed)
    ] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert payload(text[0])["index"] == 0
    assert payload(first_call[1])["index"] == 1
    assert payload(second_call[0])["index"] == 2
    assert payload(completed[0])["delta"]["stop_reason"] == "tool_use"


def test_anthropic_nonstream_response_maps_text_stop_and_usage() -> None:
    response = render_anthropic_response(
        "msg_test",
        "sonnet",
        "hello",
        Completed("end_turn", {"input_tokens": 2, "output_tokens": 1}),
    )
    assert response == {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "sonnet",
        "content": [{"type": "text", "text": "hello"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 2, "output_tokens": 1},
    }


def test_anthropic_nonstream_response_preserves_text_and_tool_call_block_order() -> (
    None
):
    response = render_anthropic_response(
        "msg_test",
        "sonnet",
        (
            TextBlock("before"),
            ToolCall("toolu_a", "echo", {"value": "one"}),
            ToolCall("toolu_b", "echo", {"value": "two"}),
        ),
        Completed("tool_use", {"input_tokens": 2, "output_tokens": 1}),
    )

    assert response == {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "sonnet",
        "content": [
            {"type": "text", "text": "before"},
            {
                "type": "tool_use",
                "id": "toolu_a",
                "name": "echo",
                "input": {"value": "one"},
            },
            {
                "type": "tool_use",
                "id": "toolu_b",
                "name": "echo",
                "input": {"value": "two"},
            },
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 2, "output_tokens": 1},
    }


def test_anthropic_nonstream_response_renders_call_only_content() -> None:
    response = render_anthropic_response(
        "msg_test",
        "sonnet",
        (ToolCall("toolu_a", "echo", {"value": "one"}),),
        Completed("tool_use", {"input_tokens": 2, "output_tokens": 1}),
    )

    assert response["content"] == [
        {"type": "tool_use", "id": "toolu_a", "name": "echo", "input": {"value": "one"}}
    ]
    assert response["stop_reason"] == "tool_use"


def test_anthropic_response_maps_max_tokens_and_invalid_usage_to_zero() -> None:
    response = render_anthropic_response(
        "msg_test",
        "sonnet",
        "  λ\n",
        Completed("max_tokens", {"input_tokens": "bad", "output_tokens": -1}),
    )
    assert response["content"] == [{"type": "text", "text": "  λ\n"}]
    assert response["stop_reason"] == "max_tokens"
    assert response["usage"] == {"input_tokens": 0, "output_tokens": 0}


@pytest.mark.parametrize(
    "reason",
    ("end_turn", "max_tokens", "refusal", "model_context_window_exceeded"),
)
def test_anthropic_preserves_each_terminal_reason_in_both_modes(reason: str) -> None:
    completed = Completed(reason, {"input_tokens": 2, "output_tokens": 1})

    response = render_anthropic_response("msg_test", "sonnet", "text", completed)
    chunks = encode_anthropic_event("msg_test", "sonnet", completed)

    assert response["stop_reason"] == reason
    assert payload(chunks[1])["delta"]["stop_reason"] == reason


@pytest.mark.parametrize("reason", [None, "future_reason"])
def test_anthropic_rejects_unknown_terminal_reason(reason: str | None) -> None:
    completed = Completed(reason, None)

    with pytest.raises(ValueError, match="stop reason"):
        render_anthropic_response("msg_test", "sonnet", "text", completed)
    with pytest.raises(ValueError, match="stop reason"):
        encode_anthropic_event("msg_test", "sonnet", completed)


def test_anthropic_error_has_event_envelope() -> None:
    chunks = encode_anthropic_error("backend_error", "unavailable")
    assert event_name(chunks[0]) == "error"
    assert payload(chunks[0]) == {
        "type": "error",
        "error": {"type": "backend_error", "message": "unavailable"},
    }
