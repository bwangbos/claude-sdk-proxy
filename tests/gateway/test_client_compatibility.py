from __future__ import annotations

import copy

import pytest
from openai.types.chat import ChatCompletionMessage

from quaylet.app import create_app
from quaylet.domain import Completed, TextDelta, ToolCall
from quaylet.openai_api import parse_openai_request
from tests.gateway.asgi_client import lifespan_app, post_json
from tests.gateway.fakes import FakeSessionFactory
from tests.gateway.test_tool_http import RepeatedRoundSession, tool_body


def tool_backend():
    return RepeatedRoundSession(
        (
            (
                ToolCall("call_one", "echo", {"value": "same"}),
                Completed("tool_use", {"input_tokens": 7, "output_tokens": 3}),
            ),
            (
                TextDelta("done"),
                Completed("end_turn", {"input_tokens": 7, "output_tokens": 3}),
            ),
        )
    )


@pytest.mark.anyio
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("empty", ["", [], [{"type": "text", "text": ""}]])
async def test_empty_tool_assistant_content_can_continue(explicit, empty):
    backend = tool_backend()
    app = create_app(models=("sonnet",), session_factory=lambda *a, **k: backend)
    headers = {"x-quaylet-session": "serial"} if explicit else {}
    body = tool_body("openai")
    async with lifespan_app(app):
        first = await post_json(app, "/v1/chat/completions", body, headers=headers)
        assert first.status == 200
        assistant = first.json["choices"][0]["message"]
        assistant["content"] = empty
        body["messages"] += [
            assistant,
            {"role": "tool", "tool_call_id": "call_one", "content": "result"},
        ]
        continuation = await post_json(
            app, "/v1/chat/completions", body, headers=headers
        )
        assert continuation.status == 200, continuation.json
        assert continuation.json["choices"][0]["message"]["content"] == "done"


@pytest.mark.anyio
async def test_returned_implicit_session_header_resumes_tool_wait():
    backend = tool_backend()
    app = create_app(models=("sonnet",), session_factory=lambda *a, **k: backend)
    body = tool_body("openai")
    async with lifespan_app(app):
        first = await post_json(app, "/v1/chat/completions", body)
        headers = {"x-quaylet-session": first.headers["x-quaylet-session"]}
        body["messages"] += [
            first.json["choices"][0]["message"],
            {"role": "tool", "tool_call_id": "call_one", "content": "result"},
        ]
        result = await post_json(app, "/v1/chat/completions", body, headers=headers)
        assert result.status == 200, result.json
        assert result.json["choices"][0]["message"]["content"] == "done"
        assert (
            result.headers["x-quaylet-session"]
            == headers["x-quaylet-session"]
        )


@pytest.mark.anyio
async def test_returned_implicit_header_can_rebase_and_continue():
    factory = FakeSessionFactory(outputs=("first", "rebased"))
    app = create_app(models=("sonnet",), session_factory=factory)
    body = {"model": "sonnet", "messages": [{"role": "user", "content": "one"}]}
    async with lifespan_app(app):
        first = await post_json(app, "/v1/chat/completions", body)
        headers = {"x-quaylet-session": first.headers["x-quaylet-session"]}
        rewritten = {
            "model": "sonnet",
            "messages": [
                {"role": "user", "content": "summary"},
                {"role": "assistant", "content": "noted"},
                {"role": "user", "content": "continue"},
            ],
        }
        rebase = await post_json(
            app, "/v1/chat/completions", rewritten, headers=headers
        )
        assert rebase.status == 200, rebase.json
        rewritten["messages"] += [
            rebase.json["choices"][0]["message"],
            {"role": "user", "content": "next"},
        ]
        following = await post_json(
            app, "/v1/chat/completions", rewritten, headers=headers
        )
        assert following.status == 200, following.json
        assert factory.created == 2
        assert factory.sessions[0].close_count == 1


@pytest.mark.parametrize("with_tools", [False, True])
def test_official_sdk_unfiltered_message_dump_is_accepted(with_tools):
    body = tool_body("openai")
    assistant = {"role": "assistant", "content": "prior"}
    tail = {"role": "user", "content": "next"}
    if with_tools:
        assistant = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_one",
                    "type": "function",
                    "function": {"name": "echo", "arguments": '{"value":"same"}'},
                }
            ],
        }
        tail = {
            "role": "tool",
            "tool_call_id": "call_one",
            "content": [
                {"type": "text", "text": "part one"},
                {"type": "text", "text": " part two"},
            ],
        }
    raw = ChatCompletionMessage.model_validate(assistant).model_dump()
    body["messages"] += [raw, tail]
    parsed = parse_openai_request(body, frozenset({"sonnet"}))
    clean = copy.deepcopy(body)
    clean["messages"][-2] = assistant
    assert parsed == parse_openai_request(clean, frozenset({"sonnet"}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("refusal", "denied"),
        ("audio", {"id": "audio"}),
        ("function_call", {"name": "echo"}),
        ("annotations", [{"type": "citation"}]),
        ("unrecognized", None),
    ],
)
def test_meaningful_unsupported_assistant_fields_still_rejected(field, value):
    body = {
        "model": "sonnet",
        "messages": [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two", field: value},
            {"role": "user", "content": "three"},
        ],
    }
    with pytest.raises(ValueError):
        parse_openai_request(body, frozenset({"sonnet"}))
