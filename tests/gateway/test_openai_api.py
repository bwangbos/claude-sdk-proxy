from __future__ import annotations

import json

import pytest

from claude_sdk_proxy.domain import (
    CanonicalMessage,
    Completed,
    RequestValidationError,
    TextBlock,
    TextDelta,
    ToolCall,
    ToolCallBlock,
    ToolResultBlock,
    UnsupportedFeature,
)
from claude_sdk_proxy.openai_api import (
    encode_openai_error,
    encode_openai_event,
    encode_openai_start,
    parse_openai_request,
    render_openai_response,
)


def openai_echo_tool() -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": "echo",
            "description": "Repeat the provided value.",
            "parameters": {
                "type": "object",
                "properties": {"v": {"type": "integer"}},
            },
        },
    }


def function_call(identifier: str, name: str, arguments: str) -> dict[str, object]:
    return {
        "id": identifier,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def payload(chunk: bytes) -> object:
    return json.loads(chunk.removeprefix(b"data: ").strip())


def test_openai_parser_preserves_exact_text_roles() -> None:
    request = parse_openai_request(
        {
            "model": "sonnet",
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "answer"},
                {"role": "user", "content": "next"},
            ],
            "max_tokens": 321,
            "stream": True,
        },
        allowed_models=frozenset({"sonnet"}),
    )

    assert request.system == "system"
    assert request.messages == (
        CanonicalMessage("user", "hello"),
        CanonicalMessage("assistant", "answer"),
        CanonicalMessage("user", "next"),
    )
    assert request.max_tokens == 321
    assert request.stream is True
    assert request.include_usage is False


def test_openai_parser_accepts_text_only_content_blocks() -> None:
    request = parse_openai_request(
        {
            "model": "sonnet",
            "messages": [
                {
                    "role": "system",
                    "content": [{"type": "text", "text": "system"}],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "hel"},
                        {"type": "text", "text": "lo"},
                    ],
                },
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "answer"}],
                },
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "next"}],
                },
            ],
        },
        allowed_models=frozenset({"sonnet"}),
    )

    assert request.system == "system"
    assert request.messages == (
        CanonicalMessage("user", "hello"),
        CanonicalMessage("assistant", "answer"),
        CanonicalMessage("user", "next"),
    )


@pytest.mark.parametrize(
    "field,value", [("temperature", 0), ("top_p", 1), ("stop", ["x"])]
)
def test_openai_parser_rejects_explicit_unsupported_controls(
    field: str, value: object
) -> None:
    body = {
        "model": "sonnet",
        "messages": [{"role": "user", "content": "hi"}],
        field: value,
    }
    with pytest.raises(UnsupportedFeature) as error:
        parse_openai_request(body, frozenset({"sonnet"}))
    assert error.value.field == field


def test_openai_parser_accepts_only_planned_pi_defaults() -> None:
    request = parse_openai_request(
        {
            "model": "sonnet",
            "messages": [{"role": "user", "content": "hi"}],
            "store": False,
            "stream_options": {"include_usage": True},
        },
        frozenset({"sonnet"}),
    )
    assert request.include_usage is True


@pytest.mark.parametrize(
    "body,field",
    [
        (
            {
                "model": "sonnet",
                "messages": [{"role": "user", "content": "hi"}],
                "store": True,
            },
            "store",
        ),
        (
            {
                "model": "sonnet",
                "messages": [{"role": "user", "content": "hi"}],
                "stream_options": {},
            },
            "stream_options",
        ),
        (
            {
                "model": "sonnet",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
                "max_completion_tokens": 1,
            },
            "max_completion_tokens",
        ),
        (
            {"model": "sonnet", "messages": [{"role": "tool", "content": "hi"}]},
            "messages",
        ),
        (
            {
                "model": "sonnet",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,AA=="},
                            }
                        ],
                    }
                ],
            },
            "messages",
        ),
    ],
)
def test_openai_parser_fails_closed_for_unsafe_shapes(
    body: dict[str, object], field: str
) -> None:
    with pytest.raises((RequestValidationError, UnsupportedFeature)) as error:
        parse_openai_request(body, frozenset({"sonnet"}))
    assert error.value.field == field


def test_openai_parser_rejects_unknown_model() -> None:
    with pytest.raises(RequestValidationError) as error:
        parse_openai_request(
            {"model": "other", "messages": [{"role": "user", "content": "hi"}]},
            frozenset({"sonnet"}),
        )
    assert error.value.field == "model"


def test_openai_parser_coalesces_reverse_order_tool_messages() -> None:
    request = parse_openai_request(
        {
            "model": "sonnet",
            "tools": [openai_echo_tool()],
            "parallel_tool_calls": True,
            "messages": [
                {"role": "user", "content": "twice"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        function_call("call_a", "echo", '{"v":1}'),
                        function_call("call_b", "echo", '{"v":1}'),
                    ],
                },
                {"role": "tool", "tool_call_id": "call_b", "content": "B"},
                {"role": "tool", "tool_call_id": "call_a", "content": "A"},
            ],
        },
        frozenset({"sonnet"}),
    )

    assert request.dialect == "openai"
    assert tuple(item.name for item in request.tools) == ("echo",)
    assert request.messages[1].blocks == (
        ToolCallBlock("call_a", "echo", {"v": 1}),
        ToolCallBlock("call_b", "echo", {"v": 1}),
    )
    assert {item.tool_call_id: item.content for item in request.next_input} == {
        "call_a": ("A",),
        "call_b": ("B",),
    }


def test_openai_parser_accepts_empty_fresh_tools_and_auto_controls() -> None:
    request = parse_openai_request(
        {
            "model": "sonnet",
            "tools": [],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "messages": [{"role": "user", "content": "hello"}],
        },
        frozenset({"sonnet"}),
    )

    assert request.tools == ()
    assert request.next_prompt == "hello"


@pytest.mark.parametrize(
    "tools",
    [
        [openai_echo_tool(), openai_echo_tool()],
        [
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "description": "Repeat the provided value.",
                    "parameters": {"type": "not-a-type"},
                },
            }
        ],
    ],
)
def test_openai_parser_preserves_tool_definition_validation_field(
    tools: list[object],
) -> None:
    with pytest.raises(RequestValidationError) as error:
        parse_openai_request(
            {
                "model": "sonnet",
                "tools": tools,
                "messages": [{"role": "user", "content": "hello"}],
            },
            frozenset({"sonnet"}),
        )

    assert error.value.field == "tools"


@pytest.mark.parametrize(
    ("tool_choice", "field"),
    [
        ("required", "tool_choice"),
        ("none", "tool_choice"),
        ({"type": "function", "function": {"name": "echo"}}, "tool_choice"),
    ],
)
def test_openai_parser_rejects_semantically_unsupported_tool_choices(
    tool_choice: object, field: str
) -> None:
    with pytest.raises(UnsupportedFeature) as error:
        parse_openai_request(
            {
                "model": "sonnet",
                "tools": [openai_echo_tool()],
                "tool_choice": tool_choice,
                "messages": [{"role": "user", "content": "hello"}],
            },
            frozenset({"sonnet"}),
        )
    assert error.value.field == field


@pytest.mark.parametrize(
    ("body", "field"),
    [
        (
            {
                "model": "sonnet",
                "tools": [{"type": "code_interpreter"}],
                "messages": [{"role": "user", "content": "hello"}],
            },
            "tools",
        ),
        (
            {
                "model": "sonnet",
                "tools": [openai_echo_tool()],
                "parallel_tool_calls": False,
                "messages": [{"role": "user", "content": "hello"}],
            },
            "parallel_tool_calls",
        ),
        (
            {
                "model": "sonnet",
                "tools": [openai_echo_tool()],
                "messages": [
                    {"role": "user", "content": "hello"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            function_call("call_a", "echo", "{}"),
                            function_call("call_a", "echo", "{}"),
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_a", "content": "A"},
                ],
            },
            "messages",
        ),
        (
            {
                "model": "sonnet",
                "tools": [openai_echo_tool()],
                "messages": [
                    {"role": "user", "content": "hello"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            function_call("call_a", "echo", "{}"),
                            function_call("call_b", "echo", "{}"),
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_a", "content": "A"},
                    {
                        "role": "tool",
                        "tool_call_id": "call_a",
                        "content": "again",
                    },
                ],
            },
            "messages",
        ),
        (
            {
                "model": "sonnet",
                "tools": [openai_echo_tool()],
                "messages": [
                    {"role": "user", "content": "hello"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [function_call("call_a", "echo", "not json")],
                    },
                    {"role": "tool", "tool_call_id": "call_a", "content": "A"},
                ],
            },
            "messages",
        ),
        (
            {
                "model": "sonnet",
                "tools": [openai_echo_tool()],
                "messages": [
                    {"role": "user", "content": "hello"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [function_call("call_a", "echo", "[]")],
                    },
                    {"role": "tool", "tool_call_id": "call_a", "content": "A"},
                ],
            },
            "messages",
        ),
        (
            {
                "model": "sonnet",
                "tools": [openai_echo_tool()],
                "messages": [
                    {"role": "user", "content": "hello"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [function_call("call_a", "echo", "{}")],
                    },
                    {"role": "tool", "content": "A"},
                ],
            },
            "messages",
        ),
        (
            {
                "model": "sonnet",
                "tools": [openai_echo_tool()],
                "messages": [
                    {"role": "user", "content": "hello"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [function_call("call_a", "echo", "{}")],
                    },
                    {"role": "user", "content": "interleaved"},
                    {"role": "tool", "tool_call_id": "call_a", "content": "A"},
                ],
            },
            "messages",
        ),
    ],
)
def test_openai_parser_rejects_unsupported_or_incomplete_tool_shapes(
    body: dict[str, object], field: str
) -> None:
    with pytest.raises((RequestValidationError, UnsupportedFeature)) as error:
        parse_openai_request(body, frozenset({"sonnet"}))
    assert error.value.field == field


def test_openai_parser_maps_argument_decoder_recursion_to_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_decode(value: str) -> object:
        del value
        raise RecursionError

    monkeypatch.setattr("claude_sdk_proxy.openai_tools.json.loads", fail_decode)
    body = {
        "model": "sonnet",
        "tools": [openai_echo_tool()],
        "messages": [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [function_call("call_a", "echo", "{}")],
            },
            {"role": "tool", "tool_call_id": "call_a", "content": "A"},
        ],
    }

    with pytest.raises(RequestValidationError) as error:
        parse_openai_request(body, frozenset({"sonnet"}))

    assert error.value.field == "messages"


def test_openai_parser_preserves_empty_and_json_string_tool_results() -> None:
    request = parse_openai_request(
        {
            "model": "sonnet",
            "tools": [openai_echo_tool()],
            "messages": [
                {"role": "user", "content": "twice"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        function_call("call_a", "echo", "{}"),
                        function_call("call_b", "echo", "{}"),
                    ],
                },
                {"role": "tool", "tool_call_id": "call_b", "content": ""},
                {
                    "role": "tool",
                    "tool_call_id": "call_a",
                    "content": '{"value":1}',
                },
            ],
        },
        frozenset({"sonnet"}),
    )

    assert request.next_input == (
        ToolResultBlock("call_a", ('{"value":1}',), False),
        ToolResultBlock("call_b", ("",), False),
    )


@pytest.mark.parametrize("field,value", [("extra", 1), ("stream", "yes")])
def test_openai_parser_rejects_unknown_fields_and_nonboolean_stream(
    field: str, value: object
) -> None:
    with pytest.raises(RequestValidationError) as error:
        parse_openai_request(
            {
                "model": "sonnet",
                "messages": [{"role": "user", "content": "hello"}],
                field: value,
            },
            frozenset({"sonnet"}),
        )
    assert error.value.field == field


def test_openai_start_and_delta_use_literal_chunk_order() -> None:
    from claude_sdk_proxy.domain import InputUsage

    start = encode_openai_start("chatcmpl_test", "sonnet")
    delta = encode_openai_event(
        request_id="chatcmpl_test",
        model="sonnet",
        event=TextDelta("hello"),
        include_usage=False,
    )

    assert payload(start[0]) == {
        "id": "chatcmpl_test",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "sonnet",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": ""},
                "logprobs": None,
                "finish_reason": None,
            }
        ],
    }
    assert payload(delta[0])["choices"][0]["delta"] == {"content": "hello"}
    assert (
        encode_openai_event(
            "chatcmpl_test", "sonnet", InputUsage(7), include_usage=False
        )
        == ()
    )


def test_openai_completed_event_emits_requested_usage_then_done() -> None:
    usage = {"input_tokens": 2, "output_tokens": 1}
    chunks = encode_openai_event(
        request_id="chatcmpl_test",
        model="sonnet",
        event=Completed("end_turn", usage),
        include_usage=True,
    )
    assert payload(chunks[0])["choices"][0]["finish_reason"] == "stop"
    assert payload(chunks[-2]) == {
        "id": "chatcmpl_test",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "sonnet",
        "choices": [],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }
    assert chunks[-1] == b"data: [DONE]\n\n"


def test_openai_completed_event_without_usage_ends_after_terminal_chunk() -> None:
    chunks = encode_openai_event(
        request_id="chatcmpl_test",
        model="sonnet",
        event=Completed("max_tokens", None),
        include_usage=False,
    )
    assert payload(chunks[0])["choices"][0]["finish_reason"] == "length"
    assert chunks == (chunks[0], b"data: [DONE]\n\n")


def test_openai_nonstream_response_maps_text_stop_and_usage() -> None:
    response = render_openai_response(
        "chatcmpl_test",
        "sonnet",
        "hello",
        Completed("end_turn", {"input_tokens": 2, "output_tokens": 1}),
    )
    assert response["id"] == "chatcmpl_test"
    assert response["choices"] == [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "hello"},
            "logprobs": None,
            "finish_reason": "stop",
        }
    ]
    assert response["usage"] == {
        "prompt_tokens": 2,
        "completion_tokens": 1,
        "total_tokens": 3,
    }


def test_openai_nonstream_response_renders_ordered_tool_calls() -> None:
    response = render_openai_response(
        "chatcmpl_test",
        "sonnet",
        (
            TextBlock("before"),
            ToolCall("call_a", "echo", {"v": 1}),
            TextBlock("after"),
            ToolCall("call_b", "echo", {"v": 1}),
        ),
        Completed("tool_use", {"input_tokens": 2, "output_tokens": 1}),
    )

    assert response["choices"] == [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "beforeafter",
                "tool_calls": [
                    {
                        "id": "call_a",
                        "type": "function",
                        "function": {"name": "echo", "arguments": '{"v":1}'},
                    },
                    {
                        "id": "call_b",
                        "type": "function",
                        "function": {"name": "echo", "arguments": '{"v":1}'},
                    },
                ],
            },
            "logprobs": None,
            "finish_reason": "tool_calls",
        }
    ]


def test_openai_nonstream_call_only_response_uses_null_content() -> None:
    response = render_openai_response(
        "chatcmpl_test",
        "sonnet",
        (ToolCall("call_a", "echo", {"snowman": "☃"}),),
        Completed("tool_use", None),
    )

    assert response["choices"][0]["message"] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_a",
                "type": "function",
                "function": {"name": "echo", "arguments": '{"snowman":"☃"}'},
            }
        ],
    }


def test_openai_stream_renders_two_identical_calls_with_distinct_ids() -> None:
    from claude_sdk_proxy.openai_api import OpenAIStreamState

    state = OpenAIStreamState()
    first = payload(
        encode_openai_event(
            "chatcmpl_1", "sonnet", ToolCall("call_a", "echo", {"v": 1}), False, state
        )[0]
    )
    second = payload(
        encode_openai_event(
            "chatcmpl_1", "sonnet", ToolCall("call_b", "echo", {"v": 1}), False, state
        )[0]
    )

    assert first["choices"][0]["delta"]["tool_calls"][0] == {
        "index": 0,
        "id": "call_a",
        "type": "function",
        "function": {"name": "echo", "arguments": '{"v":1}'},
    }
    assert second["choices"][0]["delta"]["tool_calls"][0] == {
        "index": 1,
        "id": "call_b",
        "type": "function",
        "function": {"name": "echo", "arguments": '{"v":1}'},
    }


def test_openai_stream_tool_completion_has_tool_calls_finish_reason() -> None:
    chunks = encode_openai_event(
        "chatcmpl_test",
        "sonnet",
        Completed("tool_use", {"input_tokens": 2, "output_tokens": 1}),
        include_usage=True,
    )

    assert payload(chunks[0])["choices"][0]["finish_reason"] == "tool_calls"
    assert payload(chunks[1])["choices"] == []
    assert chunks[2] == b"data: [DONE]\n\n"


def test_openai_response_preserves_unicode_whitespace_and_zeroes_bad_usage() -> None:
    response = render_openai_response(
        "chatcmpl_test",
        "sonnet",
        "  λ\n",
        Completed("max_tokens", {"input_tokens": "bad", "output_tokens": -1}),
    )
    assert response["choices"][0]["message"]["content"] == "  λ\n"
    assert response["choices"][0]["finish_reason"] == "length"
    assert response["usage"] == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("end_turn", "stop"),
        ("max_tokens", "length"),
        ("refusal", "content_filter"),
        ("model_context_window_exceeded", "length"),
    ],
)
def test_openai_maps_each_terminal_reason_in_both_modes(
    reason: str, expected: str
) -> None:
    completed = Completed(reason, {"input_tokens": 2, "output_tokens": 1})

    response = render_openai_response("chatcmpl_test", "sonnet", "text", completed)
    chunks = encode_openai_event(
        "chatcmpl_test", "sonnet", completed, include_usage=False
    )

    assert response["choices"][0]["finish_reason"] == expected
    assert payload(chunks[0])["choices"][0]["finish_reason"] == expected


@pytest.mark.parametrize("reason", [None, "future_reason"])
def test_openai_rejects_unknown_terminal_reason(reason: str | None) -> None:
    completed = Completed(reason, None)

    with pytest.raises(ValueError, match="stop reason"):
        render_openai_response("chatcmpl_test", "sonnet", "text", completed)
    with pytest.raises(ValueError, match="stop reason"):
        encode_openai_event("chatcmpl_test", "sonnet", completed, include_usage=False)


def test_openai_error_ends_the_stream() -> None:
    chunks = encode_openai_error("backend_error", "unavailable")
    assert payload(chunks[0]) == {
        "error": {
            "message": "unavailable",
            "type": "backend_error",
            "code": "backend_error",
        }
    }
    assert len(chunks) == 1
