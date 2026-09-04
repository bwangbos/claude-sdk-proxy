from __future__ import annotations

import json

import pytest

from claude_sdk_proxy.anthropic_api import (
    encode_anthropic_error,
    encode_anthropic_event,
    encode_anthropic_start,
    parse_anthropic_request,
    render_anthropic_response,
)
from claude_sdk_proxy.domain import (
    CanonicalMessage,
    Completed,
    RequestValidationError,
    TextDelta,
    UnsupportedFeature,
)


def event_name(chunk: bytes) -> str:
    return chunk.splitlines()[0].removeprefix(b"event: ").decode()


def payload(chunk: bytes) -> object:
    return json.loads(chunk.split(b"data: ", maxsplit=1)[1])


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
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
                "tools": [],
            },
            "tools",
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


def test_anthropic_stream_uses_message_content_and_stop_order() -> None:
    start = encode_anthropic_start("msg_test", "sonnet")
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
    assert payload(delta[0])["delta"] == {"type": "text_delta", "text": "hello"}
    assert payload(completed[1]) == {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 1},
    }


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


def test_anthropic_error_has_event_envelope() -> None:
    chunks = encode_anthropic_error("backend_error", "unavailable")
    assert event_name(chunks[0]) == "error"
    assert payload(chunks[0]) == {
        "type": "error",
        "error": {"type": "backend_error", "message": "unavailable"},
    }
