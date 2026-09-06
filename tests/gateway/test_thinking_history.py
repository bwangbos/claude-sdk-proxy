from __future__ import annotations

import pytest

from claude_sdk_proxy.anthropic_api import parse_anthropic_request
from claude_sdk_proxy.app import create_app
from claude_sdk_proxy.domain import CanonicalMessage, TextBlock, ThinkingBlock
from claude_sdk_proxy.openai_api import parse_openai_request
from claude_sdk_proxy.sdk_history import seed_history
from claude_sdk_proxy.sdk_session import SdkSession
from claude_sdk_proxy.session_identity import request_fingerprint
from tests.gateway.asgi_client import _LIFESPAN_STATES, lifespan_app, post_json
from tests.gateway.fakes import FakeSdkClient, FixedTemporaryDirectory, sdk_response
from tests.gateway.test_thinking_streams import thinking_response


@pytest.mark.anyio
async def test_native_response_round_trip_preserves_signed_and_redacted_seed_bytes(
    tmp_path,
):
    client = FakeSdkClient((thinking_response(),))
    app = create_app(
        models=("sonnet",),
        session_factory=lambda model, system, **kw: SdkSession(
            model,
            system,
            **kw,
            directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
            client_factory=client.capture_options,
        ),
    )
    user = {"role": "user", "content": "hi"}
    async with lifespan_app(app):
        response = await post_json(
            app,
            "/v1/messages",
            {"model": "sonnet", "max_tokens": 100, "messages": [user]},
        )
    assert response.status == 200
    assert response.json["content"] == [
        {
            "type": "thinking",
            "thinking": "reasoning summary",
            "signature": "opaque-signature",
        },
        {"type": "text", "text": "answer"},
    ]
    content = response.json["content"] + [
        {"type": "redacted_thinking", "data": "opaque\n+/="}
    ]
    request = parse_anthropic_request(
        {
            "model": "sonnet",
            "max_tokens": 100,
            "messages": [user, {"role": "assistant", "content": content}, user],
        },
        frozenset({"sonnet"}),
    )
    seeded = await seed_history(
        request.messages[:-1], cwd=tmp_path, model="sonnet", sdk_tool_names={}
    )
    entries = await seeded.store.load(
        {"project_key": seeded.project_key, "session_id": seeded.session_id}
    )
    assert [e["message"]["content"][0] for e in entries[1:]] == content


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning", "reasoning_text"])
def test_openai_unsigned_reasoning_is_only_metadata(field):
    body = {
        "model": "sonnet",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "answer", field: "not native thinking"},
            {"role": "user", "content": "next"},
        ],
    }
    request = parse_openai_request(body, frozenset({"sonnet"}))
    assert request.messages[1].blocks == (TextBlock("answer"),)
    body["messages"][1].pop(field)
    assert request_fingerprint(request) == request_fingerprint(
        parse_openai_request(body, frozenset({"sonnet"}))
    )


@pytest.mark.anyio
@pytest.mark.parametrize("tools_enabled", [False, True])
async def test_openai_public_replay_reuses_native_session_and_retains_thinking(
    tmp_path, tools_enabled
):
    client = FakeSdkClient(
        (thinking_response(), sdk_response("next answer", "sdk-thinking"))
    )
    app = create_app(
        models=("sonnet",),
        session_factory=lambda model, system, **kw: SdkSession(
            model,
            system,
            **kw,
            directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
            client_factory=client.capture_options,
        ),
    )
    body = {"model": "sonnet", "messages": [{"role": "user", "content": "hi"}]}
    if tools_enabled:
        body["tools"] = [
            {
                "type": "function",
                "function": {"name": "echo", "parameters": {"type": "object"}},
            }
        ]
    async with lifespan_app(app):
        first = await post_json(app, "/v1/chat/completions", body)
        assert first.status == 200
        sid = first.headers["x-claude-proxy-session"]
        registry = _LIFESPAN_STATES[app]["registry"]
        entry = registry._implicit[sid]
        assert entry.transcript[1].blocks == (
            ThinkingBlock("reasoning summary", "opaque-signature"),
            TextBlock("answer"),
        )
        body["messages"] += [
            {
                "role": "assistant",
                "content": "answer",
                "reasoning_content": "reasoning summary",
            },
            {"role": "user", "content": "next"},
        ]
        second = await post_json(app, "/v1/chat/completions", body)
        assert second.status == 200, second.json
        assert second.headers["x-claude-proxy-session"] == sid
        assert client.connect_count == 1
        assert client.prompts == ["hi", "next"]
        assert registry._implicit[sid].transcript[1].blocks == (
            ThinkingBlock("reasoning summary", "opaque-signature"),
            TextBlock("answer"),
        )


@pytest.mark.parametrize(
    "block",
    [
        {"type": "thinking", "thinking": "x"},
        {"type": "thinking", "thinking": "x", "signature": ""},
        {"type": "thinking", "thinking": 3, "signature": "sig"},
        {"type": "redacted_thinking", "data": ""},
    ],
)
def test_unsigned_or_malformed_native_history_is_rejected(block):
    from claude_sdk_proxy.domain import RequestValidationError

    with pytest.raises(RequestValidationError):
        parse_anthropic_request(
            {
                "model": "sonnet",
                "max_tokens": 100,
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": [block]},
                    {"role": "user", "content": "next"},
                ],
            },
            frozenset({"sonnet"}),
        )


@pytest.mark.anyio
async def test_openai_reasoning_only_max_tokens_replays_without_answer_text(tmp_path):
    events = list(thinking_response())
    del events[7:11]
    events[-3].event["delta"]["stop_reason"] = "max_tokens"
    events[-1].stop_reason = "max_tokens"
    client = FakeSdkClient((tuple(events), sdk_response("next answer", "sdk-thinking")))
    app = create_app(
        models=("sonnet",),
        session_factory=lambda model, system, **kw: SdkSession(
            model,
            system,
            **kw,
            directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
            client_factory=client.capture_options,
        ),
    )
    body = {"model": "sonnet", "messages": [{"role": "user", "content": "hi"}]}
    async with lifespan_app(app):
        first = await post_json(app, "/v1/chat/completions", body)
        assert first.status == 200
        message = first.json["choices"][0]["message"]
        assert message == {
            "role": "assistant",
            "content": None,
            "reasoning_content": "reasoning summary",
        }
        body["messages"] += [message, {"role": "user", "content": "next"}]
        second = await post_json(app, "/v1/chat/completions", body)
        assert second.status == 200, second.json
        assert (
            second.headers["x-claude-proxy-session"]
            == first.headers["x-claude-proxy-session"]
        )
        assert client.prompts == ["hi", "next"]


@pytest.mark.parametrize("dialect,expected", [("anthropic", False), ("openai", True)])
def test_native_signed_history_identity_is_dialect_specific(dialect, expected):
    from claude_sdk_proxy.session_identity import messages_equal

    left = (
        CanonicalMessage(
            "assistant", (ThinkingBlock("native", "sig"), TextBlock("answer"))
        ),
    )
    right = (CanonicalMessage.assistant_text("answer"),)
    assert messages_equal(left, right, dialect=dialect) is expected


@pytest.mark.anyio
@pytest.mark.parametrize("dialect", ["anthropic", "openai"])
async def test_interleaved_thinking_continuation_keeps_order_and_one_result_batch(
    tmp_path, dialect
):
    from claude_agent_sdk import ToolResultBlock as SdkToolResultBlock
    from claude_agent_sdk import UserMessage

    from tests.gateway.test_thinking_streams import interleaved_tool_response

    response = interleaved_tool_response()
    client = FakeSdkClient(
        (
            (
                *response,
                UserMessage(
                    [
                        SdkToolResultBlock("sdk-1", "first", False),
                        SdkToolResultBlock("sdk-3", "second", False),
                    ]
                ),
                *sdk_response("done", "sdk-thinking"),
            ),
        ),
        start_tool_callbacks=False,
    )

    def reverse_callbacks():
        client.start_tool_callback("echo", {}, internal_id="sdk-3")
        client.start_tool_callback("echo", {}, internal_id="sdk-1")

    client._before_message_actions[len(response) - 2] = reverse_callbacks
    app = create_app(
        models=("sonnet",),
        session_factory=lambda model, system, **kw: SdkSession(
            model,
            system,
            **kw,
            directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
            client_factory=client.capture_options,
        ),
    )
    body = {"model": "sonnet", "messages": [{"role": "user", "content": "go"}]}
    if dialect == "anthropic":
        body.update(
            max_tokens=100, tools=[{"name": "echo", "input_schema": {"type": "object"}}]
        )
    else:
        body["tools"] = [
            {
                "type": "function",
                "function": {"name": "echo", "parameters": {"type": "object"}},
            }
        ]
    path = "/v1/messages" if dialect == "anthropic" else "/v1/chat/completions"
    async with lifespan_app(app):
        first = await post_json(app, path, body)
        assert first.status == 200
        sid = first.headers["x-claude-proxy-session"]
        replay = await post_json(app, path, body)
        assert replay.status == 200
        assert client.tool_handler_count == 2
        if dialect == "anthropic":
            content = first.json["content"]
            calls = [b for b in content if b["type"] == "tool_use"]
            body["messages"] += [
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": calls[0]["id"],
                            "content": "first",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": calls[1]["id"],
                            "content": "second",
                        },
                    ],
                },
            ]
        else:
            message = first.json["choices"][0]["message"]
            message.pop("reasoning_content")  # Omission must preserve native state.
            calls = message["tool_calls"]
            body["messages"] += [
                message,
                {"role": "tool", "tool_call_id": calls[0]["id"], "content": "first"},
                {"role": "tool", "tool_call_id": calls[1]["id"], "content": "second"},
            ]
        final = await post_json(app, path, body)
        assert final.status == 200, final.json
        assert final.headers["x-claude-proxy-session"] == sid
        registry = _LIFESPAN_STATES[app]["registry"]
        transcript = registry._implicit[sid].transcript
        assert [m.role for m in transcript] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert transcript[1].blocks[0] == ThinkingBlock("reason-0", "sig-0")
        assert transcript[1].blocks[2] == ThinkingBlock("reason-2", "sig-2")
        assert {r.tool_call_id: r.content for r in transcript[2].blocks} == {
            calls[0]["id"]: ("first",),
            calls[1]["id"]: ("second",),
        }
        assert client.tool_handler_count == 2
        assert client.prompts == ["go"]


@pytest.mark.anyio
@pytest.mark.parametrize("kind", [[], {}])
async def test_malformed_anthropic_block_type_is_a_client_error(kind):
    from tests.gateway.fakes import FakeSessionFactory

    factory = FakeSessionFactory(("unused",))
    app = create_app(models=("sonnet",), session_factory=factory)
    async with lifespan_app(app):
        response = await post_json(
            app,
            "/v1/messages",
            {
                "model": "sonnet",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": [{"type": kind}]}],
            },
        )
    assert response.status == 400
    assert factory.created == 0
