from __future__ import annotations

import json
from dataclasses import replace

import pytest
from claude_agent_sdk import ToolResultBlock as SdkToolResultBlock
from claude_agent_sdk import UserMessage, project_key_for_directory

from claude_sdk_proxy.anthropic_api import parse_anthropic_request
from claude_sdk_proxy.app import create_app
from claude_sdk_proxy.domain import (
    BackendFailure,
    CanonicalMessage,
    Completed,
    InputUsage,
    RequestValidationError,
    TextBlock,
    ThinkingBlock,
    ThinkingDelta,
)
from claude_sdk_proxy.sdk_session import SdkSession
from claude_sdk_proxy.sessions import SessionRegistry
from claude_sdk_proxy.tool_session_actor import ToolSessionState
from tests.gateway.asgi_client import _LIFESPAN_STATES, lifespan_app, post_json
from tests.gateway.fakes import (
    FakeSdkClient,
    FixedTemporaryDirectory,
    raw_tool_events,
    sdk_response,
)
from tests.gateway.test_sdk_refusal import (
    RAW_USAGE,
    SYNTHETIC_DIAGNOSTIC,
    _assert_http_refusal,
    _collect_sdk_refusal,
    _http_body,
    _init,
    _raw_delta,
    _raw_start,
    _raw_stop,
    _result,
    refusal_response,
)
from tests.gateway.test_thinking_streams import thinking_response
from tests.gateway.test_tool_sessions import (
    ToolSession,
    collect,
    echo_tool,
    first_request,
)


def _body(dialect, messages, stream=False, tools=False):
    body = _http_body(dialect, messages, stream)
    if tools:
        schema = {"type": "object", "properties": {"value": {"type": "string"}}}
        body["tools"] = (
            [{"name": "echo", "input_schema": schema}]
            if dialect == "anthropic"
            else [
                {"type": "function", "function": {"name": "echo", "parameters": schema}}
            ]
        )
    return body


def _assistant(response, dialect, stream):
    if not stream:
        return (
            {"role": "assistant", "content": response.json["content"]}
            if dialect == "anthropic"
            else response.json["choices"][0]["message"]
        )
    frames = [
        json.loads(line[6:])
        for line in response.body.decode().splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    if dialect == "anthropic":
        message = next(
            frame["message"] for frame in frames if frame["type"] == "message_start"
        )
        content = list(message["content"])
        for frame in frames:
            if frame["type"] == "content_block_start":
                content.append(frame["content_block"])
        assert content == []
        delta = next(frame for frame in frames if frame["type"] == "message_delta")
        assert {**message["usage"], **delta["usage"]} == RAW_USAGE
        return {"role": "assistant", "content": content}
    assert frames[-1]["usage"] == {
        "prompt_tokens": 1015,
        "completion_tokens": 0,
        "total_tokens": 1015,
        "prompt_tokens_details": {"cached_tokens": 948, "cache_write_tokens": 43},
    }
    return {
        "role": "assistant",
        "content": "".join(
            choice["delta"].get("content", "") or ""
            for frame in frames
            for choice in frame["choices"]
        ),
    }


class RefusalFactory:
    def __init__(self, tmp_path, responses):
        self.tmp_path = tmp_path
        self.responses = iter(responses)
        self.clients = []
        self.histories = []
        self.directories = []

    def __call__(self, model, system, *, history=(), **kwargs):
        client = FakeSdkClient(next(self.responses))
        directory = FixedTemporaryDirectory(self.tmp_path / str(len(self.clients)))
        directory.path.mkdir()
        self.clients.append(client)
        self.histories.append(history)
        self.directories.append(directory)
        return SdkSession(
            model,
            system,
            history=history,
            **kwargs,
            directory_factory=lambda: directory,
            client_factory=client.capture_options,
        )


@pytest.mark.anyio
@pytest.mark.parametrize("dialect", ["anthropic", "openai"])
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
@pytest.mark.parametrize("tools", [False, True], ids=["no-tools", "tools"])
@pytest.mark.parametrize("explicit", [False, True], ids=["implicit", "explicit"])
@pytest.mark.parametrize("switch", [False, True], ids=["continue", "switch"])
async def test_http_empty_refusal_continues_native_history(
    tmp_path, dialect, stream, tools, explicit, switch
):
    prior = tuple(replace(event, session_id="sdk-1") for event in thinking_response())
    first_responses = [
        (_init(tools_enabled=tools), *prior),
        refusal_response(tools_enabled=tools),
    ]
    if not switch:
        first_responses.append(sdk_response("continued", "sdk-1"))
    factory = RefusalFactory(
        tmp_path, [tuple(first_responses), (sdk_response("continued", "sdk-2"),)]
    )
    app = create_app(models=("sonnet", "opus"), session_factory=factory)
    path = "/v1/messages" if dialect == "anthropic" else "/v1/chat/completions"
    messages = [{"role": "user", "content": "first"}]
    async with lifespan_app(app):
        first = await post_json(app, path, _body(dialect, messages, tools=tools))
        assert first.status == 200, first.body
        sid = first.headers["x-claude-proxy-session"]
        headers = {"x-claude-proxy-session": sid} if explicit else {}
        messages += [
            _assistant(first, dialect, False),
            {"role": "user", "content": "refuse"},
        ]
        body = _body(dialect, messages, stream, tools)
        refused = await post_json(app, path, body, headers)
        _assert_http_refusal(dialect, stream, refused)
        replay = await post_json(app, path, body, headers)
        _assert_http_refusal(dialect, stream, replay)
        assert factory.clients[0].prompts == ["first", "refuse"]
        registry = _LIFESPAN_STATES[app]["registry"]
        entry = registry._implicit[sid]
        native = entry.transcript
        assert native[1].blocks == (
            ThinkingBlock("reasoning summary", "opaque-signature"),
            TextBlock("answer"),
        )
        assert native[-1] == CanonicalMessage.assistant_text("")
        assert entry.in_flight_fingerprint is None
        if tools:
            assert entry.state is ToolSessionState.READY
            assert entry.pending_call_ids == frozenset()
        messages += [
            _assistant(refused, dialect, stream),
            {"role": "user", "content": "continue"},
        ]
        body = _body(dialect, messages, tools=tools)
        if switch:
            body["model"] = "opus"
            if dialect == "anthropic":
                body["thinking"] = {"type": "adaptive"}
                body["output_config"] = {"effort": "high"}
            else:
                body["reasoning_effort"] = "high"
        continued = await post_json(app, path, body, headers)
        assert continued.status == 200, continued.body
        assert "continued" in continued.body.decode()
        assert continued.headers["x-claude-proxy-session"] == sid
        assert registry._implicit[sid].transcript[: len(native)] == native
        assert SYNTHETIC_DIAGNOSTIC not in repr(registry._implicit[sid].transcript)
        if switch:
            assert factory.histories == [(), native]
            options = factory.clients[-1].options
            entries = await options.session_store.load(
                {
                    "project_key": project_key_for_directory(
                        factory.directories[-1].path
                    ),
                    "session_id": options.resume,
                }
            )
            assert entries[1]["message"]["content"] == [
                {
                    "type": "thinking",
                    "thinking": "reasoning summary",
                    "signature": "opaque-signature",
                }
            ]
            assert entries[-1]["message"]["content"] == [{"type": "text", "text": ""}]
            assert SYNTHETIC_DIAGNOSTIC not in repr(entries)
        else:
            assert len(factory.clients) == 1
            assert factory.clients[0].prompts == ["first", "refuse", "continue"]
    assert all(client.disconnect_count == 1 for client in factory.clients)
    assert all(directory.cleanup_count == 1 for directory in factory.directories)


@pytest.mark.anyio
@pytest.mark.parametrize("dialect", ["anthropic", "openai"])
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
async def test_http_refusal_after_completed_tool_result_boundary(
    tmp_path, dialect, stream
):
    native = (
        _init(tools_enabled=True),
        *raw_tool_events(
            (("sdk-tool", "mcp__caller_tools__echo", '{"value":"one"}'),), "sdk-1"
        ),
        UserMessage(
            [SdkToolResultBlock("sdk-tool", [{"type": "text", "text": "one"}], None)],
            tool_use_result=[{"type": "text", "text": "one"}],
        ),
        *refusal_response(tools_enabled=True)[1:],
    )
    factory = RefusalFactory(tmp_path, [(native, sdk_response("continued", "sdk-1"))])
    app = create_app(models=("sonnet",), session_factory=factory)
    path = "/v1/messages" if dialect == "anthropic" else "/v1/chat/completions"
    messages = [{"role": "user", "content": "call echo"}]
    async with lifespan_app(app):
        first = await post_json(app, path, _body(dialect, messages, tools=True))
        assert first.status == 200, first.body
        assistant = _assistant(first, dialect, False)
        if dialect == "anthropic":
            call_id = assistant["content"][0]["id"]
            result = {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": call_id, "content": "one"}
                ],
            }
        else:
            call_id = assistant["tool_calls"][0]["id"]
            result = {"role": "tool", "tool_call_id": call_id, "content": "one"}
        messages += [assistant, result]
        sid = first.headers["x-claude-proxy-session"]
        headers = {"x-claude-proxy-session": sid}
        body = _body(dialect, messages, stream, True)
        refused = await post_json(app, path, body, headers)
        _assert_http_refusal(dialect, stream, refused)
        replay = await post_json(app, path, body, headers)
        _assert_http_refusal(dialect, stream, replay)
        actor = _LIFESPAN_STATES[app]["registry"]._implicit[sid]
        assert actor.state is ToolSessionState.READY
        assert actor.pending_call_ids == frozenset()
        assert actor.transcript[-1] == CanonicalMessage.assistant_text("")
        assert factory.clients[0].prompts == ["call echo"]
        assert len(factory.clients[0].tool_results) == 1
        messages += [
            _assistant(refused, dialect, stream),
            {"role": "user", "content": "continue"},
        ]
        continued = await post_json(
            app, path, _body(dialect, messages, tools=True), headers
        )
        assert continued.status == 200, continued.body
        assert "continued" in continued.body.decode()
        assert factory.clients[0].prompts == ["call echo", "continue"]
    assert factory.clients[0].disconnect_count == 1
    assert factory.directories[0].cleanup_count == 1


@pytest.mark.parametrize("content", [[], "", [{"type": "text", "text": ""}]])
def test_anthropic_empty_assistant_replay_normalizes_narrowly(content):
    body = _body(
        "anthropic",
        [
            {"role": "user", "content": "refuse"},
            {"role": "assistant", "content": content},
            {"role": "user", "content": "continue"},
        ],
    )
    request = parse_anthropic_request(body, frozenset({"sonnet"}))
    assert request.messages[1] == CanonicalMessage.assistant_text("")


@pytest.mark.parametrize(
    "role,content",
    [
        ("user", []),
        ("user", ""),
        ("user", [{"type": "text", "text": ""}]),
        ("assistant", [{"type": "text", "text": ""}] * 2),
        ("assistant", [{"type": "text", "text": None}]),
        ("assistant", [{"type": "thinking", "thinking": "", "signature": ""}]),
    ],
)
def test_empty_refusal_replay_does_not_admit_malformed_or_ordinary_empty_messages(
    role, content
):
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer"},
    ]
    if role == "assistant":
        messages += [
            {"role": "user", "content": "next"},
            {"role": role, "content": content},
            {"role": "user", "content": "last"},
        ]
    else:
        messages += [{"role": role, "content": content}]
    with pytest.raises(RequestValidationError):
        parse_anthropic_request(_body("anthropic", messages), frozenset({"sonnet"}))


@pytest.mark.anyio
@pytest.mark.parametrize("stop_reason", ["end_turn", "max_tokens", "tool_use", None])
async def test_tool_actor_rejects_empty_nonrefusal_output(stop_reason):
    backend = ToolSession(((InputUsage(24), Completed(stop_reason, RAW_USAGE)),))
    registry = SessionRegistry(lambda *args, **kwargs: backend)
    try:
        lease = await registry.open_turn(first_request(tools=(echo_tool(),)), None)
        with pytest.raises(BackendFailure):
            await collect(lease.stream())
    finally:
        await registry.close()


@pytest.mark.anyio
async def test_tool_actor_does_not_commit_unfinished_thinking_as_empty_refusal():
    backend = ToolSession(
        ((ThinkingDelta(0, "unfinished"), Completed("refusal", RAW_USAGE)),)
    )
    registry = SessionRegistry(lambda *args, **kwargs: backend)
    try:
        lease = await registry.open_turn(first_request(tools=(echo_tool(),)), None)
        with pytest.raises(BackendFailure):
            await collect(lease.stream())
    finally:
        await registry.close()


@pytest.mark.anyio
@pytest.mark.parametrize("tools", [False, True])
async def test_sdk_rejects_blockless_success_without_native_refusal(tmp_path, tools):
    delta = _raw_delta(stop_reason="end_turn")
    delta.event["delta"]["stop_details"] = None
    messages = (
        _init(tools_enabled=tools),
        _raw_start(),
        delta,
        _raw_stop(),
        _result(is_error=False, stop_reason="end_turn", terminal_reason=None),
    )
    with pytest.raises(BackendFailure):
        await _collect_sdk_refusal(
            tmp_path, messages, tools=(echo_tool(),) if tools else ()
        )


@pytest.mark.parametrize("content", [[], "", [{"type": "text", "text": ""}]])
def test_empty_assistant_replay_cannot_skip_pending_tool_results(content):
    body = _body(
        "anthropic",
        [
            {"role": "user", "content": "first"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_call1",
                        "name": "echo",
                        "input": {},
                    }
                ],
            },
            {"role": "user", "content": "skip results"},
            {"role": "assistant", "content": content},
            {"role": "user", "content": "continue"},
        ],
        tools=True,
    )
    with pytest.raises(
        RequestValidationError, match="tool results must match prior calls"
    ):
        parse_anthropic_request(body, frozenset({"sonnet"}))
