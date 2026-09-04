from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest

from claude_sdk_proxy.app import create_app
from claude_sdk_proxy.domain import (
    BackendFailure,
    Completed,
    ConversationEvent,
    TextDelta,
)
from tests.gateway.asgi_client import lifespan_app, post_json, request
from tests.gateway.fakes import FakeConversationSession, FakeSessionFactory


def openai_body(text: str = "hello", *, stream: bool = False) -> dict[str, object]:
    return {
        "model": "sonnet",
        "messages": [{"role": "user", "content": text}],
        "max_tokens": 128,
        "stream": stream,
    }


def anthropic_body(text: str = "hello", *, stream: bool = False) -> dict[str, object]:
    return {
        "model": "sonnet",
        "messages": [{"role": "user", "content": text}],
        "max_tokens": 128,
        "stream": stream,
    }


class FailingSession(FakeConversationSession):
    def __init__(self, *, after_delta: bool = False) -> None:
        super().__init__("unused")
        self.after_delta = after_delta

    async def stream_turn(self, prompt: str) -> AsyncIterator[ConversationEvent]:
        self.prompts.append(prompt)
        if self.after_delta:
            yield TextDelta("partial")
        raise BackendFailure("credential=/Users/alice/secret-token")
        yield  # pragma: no cover


class SlowStartSession(FakeConversationSession):
    async def start(self) -> None:
        await asyncio.Event().wait()


class BlockingStreamSession(FakeConversationSession):
    def __init__(self) -> None:
        super().__init__("unused")
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def stream_turn(self, prompt: str) -> AsyncIterator[ConversationEvent]:
        self.prompts.append(prompt)
        yield TextDelta("first")
        self.entered.set()
        await self.release.wait()
        yield Completed("end_turn", None)


class SlowAfterDeltaSession(FakeConversationSession):
    async def stream_turn(self, prompt: str) -> AsyncIterator[ConversationEvent]:
        self.prompts.append(prompt)
        yield TextDelta("partial")
        await asyncio.Event().wait()
        yield Completed("end_turn", None)  # pragma: no cover


class SequentialSession(FakeConversationSession):
    def __init__(self, outputs: tuple[str, ...]) -> None:
        super().__init__("unused")
        self._outputs = iter(outputs)

    async def stream_turn(self, prompt: str) -> AsyncIterator[ConversationEvent]:
        self.prompts.append(prompt)
        yield TextDelta(next(self._outputs))
        yield Completed("end_turn", {"output_tokens": 1})


def one_session_factory(session: FakeConversationSession):
    def factory(model: str, system: str) -> FakeConversationSession:
        del model, system
        return session

    return factory


@pytest.mark.anyio
async def test_openai_nonstream_conversation_continues_through_one_sdk_session() -> (
    None
):
    session = SequentialSession(("first answer", "second answer"))
    app = create_app(models=("sonnet",), session_factory=one_session_factory(session))

    async with lifespan_app(app):
        first = await post_json(app, "/v1/chat/completions", openai_body("first"))
        session_id = first.headers["x-claude-proxy-session"]
        second = await post_json(
            app,
            "/v1/chat/completions",
            {
                "model": "sonnet",
                "messages": [
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": "first answer"},
                    {"role": "user", "content": "second"},
                ],
                "max_tokens": 128,
            },
        )

    assert first.status == 200
    assert first.json["choices"][0]["message"]["content"] == "first answer"
    assert second.json["choices"][0]["message"]["content"] == "second answer"
    assert second.headers["x-claude-proxy-session"] == session_id
    assert session.prompts == ["first", "second"]
    assert session.start_count == 2
    assert session.close_count == 1


@pytest.mark.anyio
async def test_anthropic_nonstream_uses_anthropic_envelope_and_echoes_explicit_id() -> (
    None
):
    factory = FakeSessionFactory(outputs=("answer",))
    app = create_app(models=("sonnet",), session_factory=factory)
    async with lifespan_app(app):
        response = await post_json(
            app,
            "/v1/messages",
            anthropic_body(),
            {"X-Claude-Proxy-Session": "client-one"},
        )

    assert response.status == 200
    assert response.headers["x-claude-proxy-session"] == "client-one"
    assert response.json["type"] == "message"
    assert response.json["content"] == [{"type": "text", "text": "answer"}]


@pytest.mark.anyio
async def test_health_and_models_report_only_configured_models() -> None:
    app = create_app(models=("sonnet", "opus"), session_factory=FakeSessionFactory(()))
    async with lifespan_app(app):
        health = await request(app, "GET", "/health")
        models = await request(app, "GET", "/v1/models")

    assert health.status == 200
    assert health.json == {"status": "ok"}
    assert models.json == {
        "object": "list",
        "data": [
            {"id": "sonnet", "object": "model", "created": 0, "owned_by": "anthropic"},
            {"id": "opus", "object": "model", "created": 0, "owned_by": "anthropic"},
        ],
    }


@pytest.mark.anyio
async def test_openai_errors_distinguish_invalid_unsupported_and_unknown_model() -> (
    None
):
    app = create_app(models=("sonnet",), session_factory=FakeSessionFactory(()))
    async with lifespan_app(app):
        malformed = await request(
            app,
            "POST",
            "/v1/chat/completions",
            b"{",
            {"content-type": "application/json"},
        )
        unsupported_body = openai_body()
        unsupported_body["temperature"] = 0
        unsupported = await post_json(app, "/v1/chat/completions", unsupported_body)
        unknown_body = openai_body()
        unknown_body["model"] = "unknown"
        unknown = await post_json(app, "/v1/chat/completions", unknown_body)

    assert (malformed.status, malformed.json["error"]["code"]) == (
        400,
        "invalid_request",
    )
    assert (unsupported.status, unsupported.json["error"]["code"]) == (
        400,
        "unsupported_feature",
    )
    assert unsupported.json["error"]["param"] == "temperature"
    assert (unknown.status, unknown.json["error"]["code"]) == (404, "model_not_found")
    assert unknown.json["error"]["param"] == "model"


@pytest.mark.anyio
async def test_anthropic_session_mismatch_uses_anthropic_error_envelope() -> None:
    factory = FakeSessionFactory(outputs=("answer",))
    app = create_app(models=("sonnet",), session_factory=factory)
    async with lifespan_app(app):
        first = await post_json(
            app,
            "/v1/messages",
            anthropic_body(),
            {"X-Claude-Proxy-Session": "lineage"},
        )
        mismatch = await post_json(
            app,
            "/v1/messages",
            {
                "model": "sonnet",
                "messages": [
                    {"role": "user", "content": "edited"},
                    {"role": "assistant", "content": "answer"},
                    {"role": "user", "content": "next"},
                ],
                "max_tokens": 128,
            },
            {"X-Claude-Proxy-Session": "lineage"},
        )

    assert first.status == 200
    assert mismatch.status == 409
    assert mismatch.json == {
        "type": "error",
        "error": {
            "type": "session_mismatch",
            "message": "Session transcript does not match",
        },
    }


@pytest.mark.anyio
async def test_preheader_backend_failure_is_redacted_http_502() -> None:
    session = FailingSession()
    app = create_app(models=("sonnet",), session_factory=one_session_factory(session))
    async with lifespan_app(app):
        response = await post_json(
            app, "/v1/chat/completions", openai_body(stream=True)
        )

    assert response.status == 502
    assert response.json["error"]["code"] == "backend_error"
    assert response.json["error"]["message"] == "Backend request failed"
    assert "x-claude-proxy-session" in response.headers
    assert b"secret-token" not in response.body
    assert session.close_count == 1


@pytest.mark.anyio
async def test_preheader_timeout_is_http_504() -> None:
    session = SlowStartSession("unused")
    app = create_app(
        models=("sonnet",),
        session_factory=one_session_factory(session),
        turn_timeout_seconds=0.01,
    )
    async with lifespan_app(app):
        response = await post_json(app, "/v1/messages", anthropic_body(stream=True))

    assert response.status == 504
    assert response.json["error"]["type"] == "backend_timeout"
    assert session.close_count == 1


@pytest.mark.anyio
async def test_midstream_backend_failure_is_redacted_openai_sse_error() -> None:
    session = FailingSession(after_delta=True)
    app = create_app(models=("sonnet",), session_factory=one_session_factory(session))
    async with lifespan_app(app):
        response = await post_json(
            app, "/v1/chat/completions", openai_body(stream=True)
        )

    records = [
        line.removeprefix(b"data: ") for line in response.body.splitlines() if line
    ]
    assert response.status == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert json.loads(records[-2])["error"] == {
        "message": "Backend request failed",
        "type": "backend_error",
        "code": "backend_error",
    }
    assert records[-1] == b"[DONE]"
    assert b"secret-token" not in response.body
    assert session.close_count == 1


@pytest.mark.anyio
async def test_midstream_timeout_is_anthropic_sse_error_with_http_200() -> None:
    session = SlowAfterDeltaSession("unused")
    app = create_app(
        models=("sonnet",),
        session_factory=one_session_factory(session),
        turn_timeout_seconds=0.01,
    )
    async with lifespan_app(app):
        response = await post_json(app, "/v1/messages", anthropic_body(stream=True))

    assert response.status == 200
    assert b"event: content_block_delta\n" in response.body
    assert b"event: error\n" in response.body
    assert b'"type":"backend_timeout"' in response.body
    assert session.close_count == 1


@pytest.mark.anyio
async def test_anthropic_stream_primes_backend_event_before_protocol_start() -> None:
    factory = FakeSessionFactory(outputs=("answer",))
    app = create_app(models=("sonnet",), session_factory=factory)
    async with lifespan_app(app):
        response = await post_json(app, "/v1/messages", anthropic_body(stream=True))

    assert response.status == 200
    assert response.body.startswith(b"event: message_start\n")
    assert b"event: content_block_delta\n" in response.body
    assert response.body.endswith(
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )


@pytest.mark.anyio
async def test_send_failure_aborts_and_closes_active_turn() -> None:
    session = BlockingStreamSession()
    app = create_app(models=("sonnet",), session_factory=one_session_factory(session))
    async with lifespan_app(app):
        with pytest.raises(ConnectionError, match="synthetic send failure"):
            await post_json(
                app,
                "/v1/chat/completions",
                openai_body(stream=True),
                fail_send_after=1,
            )

    assert session.close_count == 1


@pytest.mark.anyio
async def test_explicit_http_disconnect_aborts_without_waiting_for_backend() -> None:
    session = BlockingStreamSession()
    app = create_app(models=("sonnet",), session_factory=one_session_factory(session))
    async with lifespan_app(app):
        response = await asyncio.wait_for(
            post_json(
                app,
                "/v1/chat/completions",
                openai_body(stream=True),
                disconnect_after_start=True,
            ),
            timeout=0.5,
        )

    assert response.status == 200
    assert session.close_count == 1


@pytest.mark.anyio
async def test_cancelling_asgi_response_aborts_active_turn_immediately() -> None:
    session = BlockingStreamSession()
    app = create_app(models=("sonnet",), session_factory=one_session_factory(session))
    async with lifespan_app(app):
        response_task = asyncio.create_task(
            post_json(app, "/v1/chat/completions", openai_body(stream=True))
        )
        await session.entered.wait()
        response_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await response_task
        await asyncio.sleep(0)
        assert session.close_count == 1


@pytest.mark.anyio
async def test_inflight_duplicate_returns_409_without_joining_active_stream() -> None:
    session = BlockingStreamSession()
    app = create_app(models=("sonnet",), session_factory=one_session_factory(session))
    async with lifespan_app(app):
        first_task = asyncio.create_task(
            post_json(app, "/v1/chat/completions", openai_body(stream=True))
        )
        await session.entered.wait()
        duplicate = await post_json(app, "/v1/chat/completions", openai_body())
        session.release.set()
        first = await first_task

    assert first.status == 200
    assert duplicate.status == 409
    assert duplicate.json["error"]["code"] == "request_in_flight"


@pytest.mark.anyio
async def test_nonstream_backend_failure_is_redacted_http_error() -> None:
    session = FailingSession(after_delta=True)
    app = create_app(models=("sonnet",), session_factory=one_session_factory(session))
    async with lifespan_app(app):
        response = await post_json(app, "/v1/messages", anthropic_body())

    assert response.status == 502
    assert response.json["error"] == {
        "type": "backend_error",
        "message": "Backend request failed",
    }
    assert b"secret-token" not in response.body


@pytest.mark.anyio
async def test_empty_and_duplicate_models_are_rejected_at_configuration_time() -> None:
    with pytest.raises(ValueError, match="models"):
        create_app(models=(), session_factory=FakeSessionFactory(()))
    with pytest.raises(ValueError, match="models"):
        create_app(models=("sonnet", "sonnet"), session_factory=FakeSessionFactory(()))


@pytest.mark.anyio
async def test_streaming_completed_response_can_be_replayed_after_send() -> None:
    factory = FakeSessionFactory(outputs=("answer",))
    app = create_app(models=("sonnet",), session_factory=factory)
    async with lifespan_app(app):
        first = await post_json(app, "/v1/chat/completions", openai_body(stream=True))
        replay = await post_json(app, "/v1/chat/completions", openai_body(stream=True))

    assert first.status == replay.status == 200
    first_records = [line for line in first.body.splitlines() if line]
    replay_records = [line for line in replay.body.splitlines() if line]
    assert len(first_records) == len(replay_records)
    assert b'"content":"answer"' in first.body
    assert b'"content":"answer"' in replay.body
    assert first_records[-1] == replay_records[-1] == b"data: [DONE]"
    assert factory.created == 1
    assert factory.sessions[0].prompts == ["hello"]
