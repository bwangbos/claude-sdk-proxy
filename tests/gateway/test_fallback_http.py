import asyncio
import json

import pytest

from claude_sdk_proxy.app import create_app
from claude_sdk_proxy.sdk_session import SdkSession
from tests.gateway.asgi_client import (
    _LIFESPAN_STATES,
    lifespan_app,
    post_json,
    post_json_then_disconnect,
)
from tests.gateway.fakes import FakeSdkClient, FixedTemporaryDirectory, raw_text_events
from tests.gateway.test_sdk_fallback import fallback_notice, pinned_response
from tests.gateway.test_sdk_refusal import _raw_delta, _raw_stop


def native_app(tmp_path, client, *, policy="off", **app_options):
    def factory(model, system, **kwargs):
        return SdkSession(
            model,
            system,
            **kwargs,
            directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
            client_factory=client.capture_options,
        )

    return create_app(
        models=("opus-5", "opus-4.8"),
        session_factory=factory,
        refusal_fallback=policy,
        **app_options,
    )


def switched_response():
    return (
        *raw_text_events("discarded original output", "sdk-1", model="claude-opus-5")[
            :4
        ],
        fallback_notice(),
        _raw_delta(usage={"input_tokens": 2, "output_tokens": 0}),
        _raw_stop(),
        *pinned_response(),
    )


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/v1/messages", "/v1/chat/completions"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("switched", [False, True])
async def test_native_auto_publishes_accepted_identity_and_replays(
    tmp_path, path, stream, switched
):
    messages = switched_response() if switched else pinned_response("claude-opus-5")
    client = FakeSdkClient((messages,))
    app = native_app(tmp_path, client, policy="auto")
    body = {
        "model": "opus",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 128,
        "stream": stream,
    }
    async with lifespan_app(app):
        response = await post_json(app, path, body)
        replay = await post_json(app, path, body)
        assert response.status == replay.status == 200
        assert len(client.prompts) == 1
        for result in (response, replay):
            assert result.headers["x-claude-proxy-requested-model"] == "opus-5"
            actual = "opus-4.8" if switched else "opus-5"
            assert result.headers["x-claude-proxy-actual-model"] == actual
            assert result.headers.get("x-claude-proxy-fallback") == (
                "true" if switched else None
            )
            assert b"discarded original output" not in result.body
            if not stream:
                assert result.json["model"] == actual
            else:
                frames = [
                    json.loads(line[6:])
                    for line in result.body.splitlines()
                    if line.startswith(b"data: ") and line != b"data: [DONE]"
                ]
                models = [f["model"] for f in frames if "model" in f]
                models += [f["message"]["model"] for f in frames if "message" in f]
                assert models and set(models) == {actual}


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/v1/messages", "/v1/chat/completions"])
@pytest.mark.parametrize("policy", ["off", "auto"])
async def test_native_publication_waits_for_accepted_boundary_only_in_auto(
    tmp_path, path, policy
):
    entered, release, sent = asyncio.Event(), asyncio.Event(), asyncio.Event()
    started = asyncio.Event()
    client = FakeSdkClient(
        (pinned_response("claude-opus-5"),), message_barriers={2: (entered, release)}
    )
    app = native_app(tmp_path, client, policy=policy)
    body = {
        "model": "opus-5",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 128,
        "stream": True,
    }
    async with lifespan_app(app):
        task = asyncio.create_task(
            post_json(
                app, path, body, body_send_entered=sent, response_start_entered=started
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if policy == "off":
                await asyncio.wait_for(sent.wait(), 2)
                assert started.is_set()
            else:
                assert not sent.is_set()
                assert not started.is_set()
            release.set()
            assert (await asyncio.wait_for(task, 2)).status == 200
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/v1/messages", "/v1/chat/completions"])
@pytest.mark.parametrize("stream", [False, True])
async def test_native_identity_failure_is_safe_and_closes(tmp_path, path, stream):
    client = FakeSdkClient((pinned_response("claude-sonnet-5"),))
    app = native_app(tmp_path, client)
    async with lifespan_app(app):
        response = await post_json(
            app,
            path,
            {
                "model": "opus-5",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "hello"}],
                "stream": stream,
            },
        )
        assert response.status == 502
        assert response.json["error"]["reason"] == "backend_model_mismatch"
        assert "x-claude-proxy-actual-model" not in response.headers
        await asyncio.wait_for(client.disconnected.wait(), 2)


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/v1/messages", "/v1/chat/completions"])
@pytest.mark.parametrize("failure", ["cancel", "disconnect", "deadline"])
@pytest.mark.parametrize("tools", [False, True])
async def test_buffering_failure_keeps_monitor_deadline_capacity_and_cleanup(
    tmp_path, path, failure, tools
):
    entered, release = asyncio.Event(), asyncio.Event()
    client = FakeSdkClient(
        (pinned_response("claude-opus-5"),), message_barriers={2: (entered, release)}
    )
    app = native_app(
        tmp_path,
        client,
        policy="auto",
        max_sessions=1,
        turn_timeout_seconds=0.1 if failure == "deadline" else 5,
    )
    body = {
        "model": "opus-5",
        "max_tokens": 128,
        "stream": True,
        "messages": [{"role": "user", "content": "go"}],
    }
    if tools:
        tool = {
            "name": "echo",
            "description": "echo",
            "input_schema": {"type": "object"},
        }
        body["tools"] = (
            [tool]
            if path == "/v1/messages"
            else [
                {
                    "type": "function",
                    "function": {
                        "name": "echo",
                        "description": "echo",
                        "parameters": {"type": "object"},
                    },
                }
            ]
        )
    async with lifespan_app(app):
        operation = (
            post_json_then_disconnect(app, path, body, after=entered)
            if failure == "disconnect"
            else post_json(app, path, body)
        )
        task = asyncio.create_task(operation)
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if failure != "disconnect":
                busy = await post_json(
                    app,
                    path,
                    {**body, "messages": [{"role": "user", "content": "other"}]},
                )
                assert busy.status == 503
            if failure == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                result = await asyncio.wait_for(task, 2)
                if failure == "deadline":
                    assert result.status == 504
                    assert "x-claude-proxy-actual-model" not in result.headers
                else:
                    assert result is None
            await asyncio.wait_for(client.disconnected.wait(), 2)
            registry = _LIFESPAN_STATES[app]["registry"]
            assert not registry._implicit and not registry._explicit
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/v1/messages", "/v1/chat/completions"])
async def test_strict_later_identity_conflict_never_sends_success_marker(
    tmp_path, path
):
    from dataclasses import replace

    from claude_agent_sdk import AssistantMessage

    events = tuple(
        replace(event, model="claude-opus-4-8")
        if isinstance(event, AssistantMessage)
        else event
        for event in pinned_response("claude-opus-5")
    )
    client = FakeSdkClient((events,))
    app = native_app(tmp_path, client)
    async with lifespan_app(app):
        result = await post_json(
            app,
            path,
            {
                "model": "opus-5",
                "max_tokens": 128,
                "stream": True,
                "messages": [{"role": "user", "content": "go"}],
            },
        )
        assert result.status == 200
        assert b"backend_error" in result.body
        assert b"[DONE]" not in result.body
        assert b"event: message_stop" not in result.body
        await asyncio.wait_for(client.disconnected.wait(), 2)


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/v1/messages", "/v1/chat/completions"])
async def test_auto_buffer_limit_never_publishes_and_releases_capacity(
    tmp_path, path, monkeypatch
):
    from claude_sdk_proxy import sdk_session
    from claude_sdk_proxy.sdk_fallback import FallbackBuffer

    monkeypatch.setattr(
        sdk_session, "FallbackBuffer", lambda: FallbackBuffer(limit_bytes=3)
    )
    client = FakeSdkClient((pinned_response("claude-opus-5"),))
    app = native_app(tmp_path, client, policy="auto", max_sessions=1)
    async with lifespan_app(app):
        result = await post_json(
            app,
            path,
            {
                "model": "opus-5",
                "max_tokens": 128,
                "stream": True,
                "messages": [{"role": "user", "content": "go"}],
            },
        )
        assert result.status == 502
        assert result.json["error"]["reason"] == "fallback_buffer_limit"
        assert "x-claude-proxy-actual-model" not in result.headers
        assert b"accepted" not in result.body
        await asyncio.wait_for(client.disconnected.wait(), 2)
        assert not _LIFESPAN_STATES[app]["registry"]._implicit
