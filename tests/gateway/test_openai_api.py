from __future__ import annotations

import json

import pytest

from claude_sdk_proxy.domain import (
    CanonicalMessage,
    Completed,
    RequestValidationError,
    TextDelta,
    UnsupportedFeature,
)
from claude_sdk_proxy.openai_api import (
    encode_openai_error,
    encode_openai_event,
    encode_openai_start,
    parse_openai_request,
    render_openai_response,
)


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
                    {"role": "user", "content": [{"type": "text", "text": "hi"}]}
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


def test_openai_parser_rejects_tool_calls_even_when_text_content_is_present() -> None:
    with pytest.raises(UnsupportedFeature) as error:
        parse_openai_request(
            {
                "model": "sonnet",
                "messages": [
                    {
                        "role": "assistant",
                        "content": "answer",
                        "tool_calls": [{"id": "call_1"}],
                    }
                ],
            },
            frozenset({"sonnet"}),
        )
    assert error.value.field == "messages"


def test_openai_start_and_delta_use_literal_chunk_order() -> None:
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


def test_openai_error_ends_the_stream() -> None:
    chunks = encode_openai_error("backend_error", "unavailable")
    assert payload(chunks[0]) == {
        "error": {
            "message": "unavailable",
            "type": "backend_error",
            "code": "backend_error",
        }
    }
    assert chunks[1] == b"data: [DONE]\n\n"
