from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
from claude_agent_sdk import (
    ConversationResetMessage,
    ResultMessage,
    StreamEvent,
    TaskStartedMessage,
)

from claude_sdk_proxy.app import create_app
from claude_sdk_proxy.domain import (
    BackendFailure,
    CanonicalMessage,
    Completed,
    ConversationEvent,
    Dialect,
    TextDelta,
    ToolDefinition,
)
from claude_sdk_proxy.sdk_session import SdkSession
from tests.gateway.asgi_client import (
    lifespan_app,
    post_json,
    post_json_then_disconnect,
    request,
)
from tests.gateway.fakes import (
    FakeConversationSession,
    FakeSdkClient,
    FakeSessionFactory,
    FixedTemporaryDirectory,
    raw_text_events,
)


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

    async def stream_generation(
        self, prompt: str
    ) -> AsyncIterator[ConversationEvent]:
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

    async def stream_generation(
        self, prompt: str
    ) -> AsyncIterator[ConversationEvent]:
        self.prompts.append(prompt)
        yield TextDelta("first")
        self.entered.set()
        await self.release.wait()
        yield Completed("end_turn", None)


class SlowAfterDeltaSession(FakeConversationSession):
    async def stream_generation(
        self, prompt: str
    ) -> AsyncIterator[ConversationEvent]:
        self.prompts.append(prompt)
        yield TextDelta("partial")
        await asyncio.Event().wait()
        yield Completed("end_turn", None)  # pragma: no cover


class SlowAfterDeltaFailingCloseSession(SlowAfterDeltaSession):
    async def close(self) -> None:
        self.close_count += 1
        raise BackendFailure("cleanup leaked /Users/alice/.claude credential")


class FailingCloseSession(BlockingStreamSession):
    async def close(self) -> None:
        self.close_count += 1
        raise BackendFailure("cleanup leaked /Users/alice/.claude credential")


class BlockingStartFailingCloseSession(FailingCloseSession):
    def __init__(self) -> None:
        super().__init__()
        self.start_entered = asyncio.Event()

    async def start(self) -> None:
        self.start_entered.set()
        await asyncio.Event().wait()


class WedgedCloseSession(BlockingStreamSession):
    def __init__(self, *, block_start: bool = False) -> None:
        super().__init__()
        self.block_start = block_start
        self.close_entered = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_cancellations = 0

    async def start(self) -> None:
        self.start_count += 1
        if self.block_start:
            self.entered.set()
            await asyncio.Event().wait()

    async def close(self) -> None:
        self.close_count += 1
        self.close_entered.set()
        while not self.close_release.is_set():
            try:
                await self.close_release.wait()
            except asyncio.CancelledError:
                self.close_cancellations += 1


class SequentialSession(FakeConversationSession):
    def __init__(self, outputs: tuple[str, ...]) -> None:
        super().__init__("unused")
        self._outputs = iter(outputs)

    async def stream_generation(
        self, prompt: str
    ) -> AsyncIterator[ConversationEvent]:
        self.prompts.append(prompt)
        yield TextDelta(next(self._outputs))
        yield Completed("end_turn", {"output_tokens": 1})


class InputUsageSession(FakeConversationSession):
    async def stream_generation(
        self, prompt: str
    ) -> AsyncIterator[ConversationEvent]:
        from claude_sdk_proxy.domain import InputUsage

        self.prompts.append(prompt)
        yield InputUsage(7)
        yield TextDelta(self._text)
        yield Completed("end_turn", {"input_tokens": 7, "output_tokens": 1})


class EmptyTextSession(FakeConversationSession):
    async def stream_generation(
        self, prompt: str
    ) -> AsyncIterator[ConversationEvent]:
        self.prompts.append(prompt)
        yield Completed("end_turn", {"input_tokens": 1, "output_tokens": 0})


def one_session_factory(session: FakeConversationSession):
    def factory(
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
    ) -> FakeConversationSession:
        del model, system, tools, dialect
        return session

    return factory


def text_event_for_app() -> StreamEvent:
    return StreamEvent(
        uuid="event-1",
        session_id="sdk-1",
        event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "answer"},
        },
    )


def result_message_for_app() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=0,
        duration_api_ms=0,
        is_error=False,
        num_turns=1,
        session_id="sdk-1",
        stop_reason="end_turn",
        usage={"input_tokens": 2, "output_tokens": 1},
    )


def sdk_app(tmp_path, client: FakeSdkClient):
    def factory(
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
    ) -> SdkSession:
        del tools, dialect
        return SdkSession(
            model,
            system,
            directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
            client_factory=lambda options: client.capture_options(options),
        )

    return create_app(models=("sonnet",), session_factory=factory)


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
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/messages", anthropic_body()),
        ("/v1/chat/completions", openai_body()),
    ],
)
async def test_empty_text_completion_preserves_legacy_empty_content(
    path: str, body: dict[str, object]
) -> None:
    session = EmptyTextSession("unused")
    app = create_app(models=("sonnet",), session_factory=one_session_factory(session))
    async with lifespan_app(app):
        response = await post_json(app, path, body)

    assert response.status == 200
    if path == "/v1/messages":
        assert response.json["content"] == [{"type": "text", "text": ""}]
    else:
        assert response.json["choices"][0]["message"]["content"] == ""


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
                "system": "different",
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
            "message": "Session system prompt changed. "
            "Use a unique X-Claude-Proxy-Session per conversation; "
            "retry only after active work completes.",
            "reason": "system_changed",
        },
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("path", "dialect"),
    [
        ("/v1/messages", "anthropic"),
        ("/v1/chat/completions", "openai"),
    ],
)
async def test_explicit_session_rebases_rewritten_text_history(
    path: str, dialect: str
) -> None:
    factory = FakeSessionFactory(outputs=("old answer", "new answer"))
    app = create_app(models=("sonnet",), session_factory=factory)
    first_body = (
        anthropic_body("old question")
        if dialect == "anthropic"
        else openai_body("old question")
    )
    rewritten = {
        "model": "sonnet",
        "messages": [
            {"role": "user", "content": "compacted summary"},
            {"role": "assistant", "content": "kept answer"},
            {"role": "user", "content": "continue"},
        ],
        "max_tokens": 128,
    }

    async with lifespan_app(app):
        first = await post_json(
            app, path, first_body, {"X-Claude-Proxy-Session": "lineage"}
        )
        rebased = await post_json(
            app, path, rewritten, {"X-Claude-Proxy-Session": "lineage"}
        )

    assert first.status == 200
    assert rebased.status == 200
    assert factory.created == 2
    assert factory.histories[1] == (
        CanonicalMessage.user_text("compacted summary"),
        CanonicalMessage.assistant_text("kept answer"),
    )
    assert factory.sessions[0].close_count == 1
    assert factory.sessions[1].prompts == ["continue"]


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
@pytest.mark.parametrize("stream", [False, True])
async def test_preheader_timeout_survives_failing_backend_close(stream: bool) -> None:
    session = BlockingStartFailingCloseSession()
    app = create_app(
        models=("sonnet",),
        session_factory=one_session_factory(session),
        turn_timeout_seconds=0.01,
    )
    async with lifespan_app(app):
        response = await post_json(
            app, "/v1/chat/completions", openai_body(stream=stream)
        )

    assert response.status == 504
    assert response.json["error"]["code"] == "backend_timeout"
    assert b"credential" not in response.body
    assert session.close_count >= 1


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
async def test_midstream_timeout_survives_failing_backend_close() -> None:
    session = SlowAfterDeltaFailingCloseSession("unused")
    app = create_app(
        models=("sonnet",),
        session_factory=one_session_factory(session),
        turn_timeout_seconds=0.01,
    )
    async with lifespan_app(app):
        response = await post_json(
            app, "/v1/messages", anthropic_body(stream=True)
        )

    assert response.status == 200
    assert b'"type":"backend_timeout"' in response.body
    assert b'"type":"backend_error"' not in response.body
    assert b"credential" not in response.body
    assert session.close_count >= 1


@pytest.mark.anyio
async def test_slow_sse_send_is_not_cancelled_by_backend_deadline() -> None:
    session = FakeConversationSession("answer")
    app = create_app(
        models=("sonnet",),
        session_factory=one_session_factory(session),
        turn_timeout_seconds=0.01,
    )
    send_entered = asyncio.Event()
    release_send = asyncio.Event()
    async with lifespan_app(app):
        response_task = asyncio.create_task(
            post_json(
                app,
                "/v1/chat/completions",
                openai_body(stream=True),
                body_send_entered=send_entered,
                body_send_release=release_send,
            )
        )
        await send_entered.wait()
        await asyncio.sleep(0.03)
        assert not response_task.done()
        release_send.set()
        response = await asyncio.wait_for(response_task, timeout=0.5)

    assert response.status == 200
    assert b'"content":"answer"' in response.body


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
async def test_anthropic_stream_replays_actual_input_usage() -> None:
    session = InputUsageSession("answer")
    app = create_app(models=("sonnet",), session_factory=one_session_factory(session))
    async with lifespan_app(app):
        first = await post_json(app, "/v1/messages", anthropic_body(stream=True))
        replay = await post_json(app, "/v1/messages", anthropic_body(stream=True))

    for response in (first, replay):
        first_data = response.body.split(b"data: ", maxsplit=1)[1].splitlines()[0]
        assert json.loads(first_data)["message"]["usage"] == {
            "input_tokens": 7,
            "output_tokens": 0,
        }
    assert session.prompts == ["hello"]


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
async def test_terminal_send_failure_preserves_committed_replay_and_backend() -> None:
    factory = FakeSessionFactory(outputs=("answer", "unexpected"))
    app = create_app(models=("sonnet",), session_factory=factory)
    async with lifespan_app(app):
        with pytest.raises(ConnectionError, match="synthetic send failure"):
            await post_json(
                app,
                "/v1/chat/completions",
                openai_body(stream=True),
                fail_send_after=3,
            )
        replay = await post_json(
            app, "/v1/chat/completions", openai_body(stream=True)
        )
        assert factory.sessions[0].close_count == 0

    assert replay.status == 200
    assert b'"content":"answer"' in replay.body
    assert factory.created == 1
    assert factory.sessions[0].prompts == ["hello"]


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
@pytest.mark.parametrize("stream", [False, True])
async def test_queued_disconnect_cancels_preheader_backend_work(stream: bool) -> None:
    session = BlockingStartFailingCloseSession()
    app = create_app(models=("sonnet",), session_factory=one_session_factory(session))
    async with lifespan_app(app):
        response = await asyncio.wait_for(
            post_json_then_disconnect(
                app, "/v1/chat/completions", openai_body(stream=stream)
            ),
            timeout=0.5,
        )

    assert response is None
    assert session.start_count == 0
    assert session.close_count == 0


@pytest.mark.anyio
async def test_disconnect_does_not_swallow_concurrent_external_cancellation() -> None:
    session = FakeConversationSession("unused")
    app = create_app(models=("sonnet",), session_factory=one_session_factory(session))
    async with lifespan_app(app):
        response_task = asyncio.create_task(
            post_json_then_disconnect(
                app,
                "/v1/chat/completions",
                openai_body(stream=True),
                external_cancel=True,
            )
        )
        with pytest.raises(asyncio.CancelledError):
            await response_task

    assert session.start_count == 0


@pytest.mark.anyio
@pytest.mark.parametrize("stream", [False, True])
async def test_http_disconnect_during_backend_start_closes_turn(stream: bool) -> None:
    session = BlockingStartFailingCloseSession()
    app = create_app(models=("sonnet",), session_factory=one_session_factory(session))
    async with lifespan_app(app):
        response = await asyncio.wait_for(
            post_json_then_disconnect(
                app,
                "/v1/chat/completions",
                openai_body(stream=stream),
                after=session.start_entered,
            ),
            timeout=0.5,
        )

    assert response is None
    assert session.close_count == 1


@pytest.mark.anyio
async def test_disconnect_cancels_sender_even_when_backend_close_fails() -> None:
    session = FailingCloseSession()
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
async def test_nonstream_disconnect_is_watched_through_response_send() -> None:
    session = FakeConversationSession("answer")
    app = create_app(models=("sonnet",), session_factory=one_session_factory(session))
    async with lifespan_app(app):
        response = await asyncio.wait_for(
            post_json(
                app,
                "/v1/chat/completions",
                openai_body(),
                disconnect_after_start=True,
                block_body_after_start=True,
            ),
            timeout=0.5,
        )
        assert session.close_count == 0

    assert response.status == 200
    assert response.body == b""
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
async def test_request_cancellation_does_not_wait_for_wedged_close() -> None:
    session = WedgedCloseSession()
    app = create_app(models=("sonnet",), session_factory=one_session_factory(session))
    async with lifespan_app(app):
        response_task = asyncio.create_task(
            post_json(app, "/v1/chat/completions", openai_body(stream=True))
        )
        await session.entered.wait()
        response_task.cancel()
        try:
            done, _ = await asyncio.wait({response_task}, timeout=0.05)
            assert response_task in done
        finally:
            session.close_release.set()
            await asyncio.gather(response_task, return_exceptions=True)

    assert session.close_count == 1


@pytest.mark.anyio
async def test_request_timeout_does_not_wait_for_wedged_close() -> None:
    session = WedgedCloseSession(block_start=True)
    app = create_app(
        models=("sonnet",),
        session_factory=one_session_factory(session),
        turn_timeout_seconds=0.01,
    )
    async with lifespan_app(app):
        response_task = asyncio.create_task(
            post_json(app, "/v1/chat/completions", openai_body())
        )
        await session.close_entered.wait()
        try:
            done, _ = await asyncio.wait({response_task}, timeout=0.05)
            assert response_task in done
        finally:
            session.close_release.set()
        response = await response_task

    assert response.status == 504
    assert session.close_count == 1


@pytest.mark.anyio
async def test_lifespan_shutdown_bounds_wedged_close_aggregate() -> None:
    session = WedgedCloseSession()
    app = create_app(
        models=("sonnet",),
        session_factory=one_session_factory(session),
        teardown_timeout_seconds=0.01,
    )
    lifespan = lifespan_app(app)
    await lifespan.__aenter__()
    session.release.set()
    response = await post_json(app, "/v1/chat/completions", openai_body())
    assert response.status == 200

    shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
    try:
        await session.close_entered.wait()
        done, _ = await asyncio.wait({shutdown}, timeout=0.05)
        assert shutdown in done
    finally:
        session.close_release.set()
        await asyncio.gather(shutdown, return_exceptions=True)

    assert session.close_cancellations == 1


@pytest.mark.anyio
async def test_external_cancellation_is_not_masked_when_backend_close_fails() -> None:
    session = FailingCloseSession()
    app = create_app(models=("sonnet",), session_factory=one_session_factory(session))
    async with lifespan_app(app):
        response_task = asyncio.create_task(
            post_json(app, "/v1/chat/completions", openai_body(stream=True))
        )
        await session.entered.wait()
        response_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(response_task, timeout=0.5)

    assert session.close_count == 1


@pytest.mark.anyio
@pytest.mark.parametrize("stream", [False, True])
async def test_preheader_cancellation_survives_failing_backend_close(
    stream: bool,
) -> None:
    session = BlockingStartFailingCloseSession()
    app = create_app(models=("sonnet",), session_factory=one_session_factory(session))
    async with lifespan_app(app):
        response_task = asyncio.create_task(
            post_json(
                app,
                "/v1/chat/completions",
                openai_body(stream=stream),
            )
        )
        await session.start_entered.wait()
        response_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(response_task, timeout=0.5)

    assert session.close_count == 1


@pytest.mark.anyio
async def test_send_failure_is_not_masked_when_backend_close_fails() -> None:
    session = FailingCloseSession()
    app = create_app(models=("sonnet",), session_factory=one_session_factory(session))
    async with lifespan_app(app):
        with pytest.raises(ConnectionError, match="synthetic send failure"):
            await asyncio.wait_for(
                post_json(
                    app,
                    "/v1/chat/completions",
                    openai_body(stream=True),
                    fail_send_after=1,
                ),
                timeout=0.5,
            )

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
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/chat/completions", openai_body()),
        ("/v1/messages", anthropic_body()),
    ],
)
@pytest.mark.parametrize("content_type", [None, "text/plain"])
async def test_post_routes_require_json_content_type(
    path: str, body: dict[str, object], content_type: str | None
) -> None:
    app = create_app(models=("sonnet",), session_factory=FakeSessionFactory(()))
    headers = {} if content_type is None else {"content-type": content_type}
    encoded = json.dumps(body, separators=(",", ":")).encode()
    async with lifespan_app(app):
        response = await request(app, "POST", path, encoded, headers)

    assert response.status == 400
    if path.endswith("completions"):
        assert response.json["error"]["code"] == "invalid_request"
    else:
        assert response.json["error"]["type"] == "invalid_request"


@pytest.mark.anyio
async def test_json_content_type_allows_parameters() -> None:
    app = create_app(
        models=("sonnet",), session_factory=FakeSessionFactory(("answer",))
    )
    body = json.dumps(openai_body(), separators=(",", ":")).encode()
    async with lifespan_app(app):
        response = await request(
            app,
            "POST",
            "/v1/chat/completions",
            body,
            {"content-type": "application/json; charset=utf-8"},
        )
    assert response.status == 200


@pytest.mark.anyio
async def test_backend_tool_protocol_violation_is_http_502(tmp_path) -> None:
    client = FakeSdkClient(
        responses=(
            (
                StreamEvent(
                    uuid="event-1",
                    session_id="sdk-1",
                    event={
                        "type": "content_block_start",
                        "content_block": {"type": "tool_use", "name": "secret"},
                    },
                ),
                ResultMessage(
                    subtype="success",
                    duration_ms=0,
                    duration_api_ms=0,
                    is_error=False,
                    num_turns=1,
                    session_id="sdk-1",
                    stop_reason="end_turn",
                    usage={"output_tokens": 1},
                ),
            ),
        )
    )

    def factory(
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
    ) -> SdkSession:
        del tools, dialect
        return SdkSession(
            model,
            system,
            directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
            client_factory=lambda options: client.capture_options(options),
        )

    app = create_app(models=("sonnet",), session_factory=factory)
    async with lifespan_app(app):
        response = await post_json(app, "/v1/chat/completions", openai_body())

    assert response.status == 502
    assert response.json["error"]["code"] == "backend_error"
    assert b"secret" not in response.body


@pytest.mark.anyio
async def test_backend_task_message_is_redacted_http_502(tmp_path) -> None:
    task = TaskStartedMessage(
        "task_started", {}, "task-secret", "work", "task-1", "sdk-1"
    )
    client = FakeSdkClient(responses=((task, result_message_for_app()),))
    app = sdk_app(tmp_path, client)

    async with lifespan_app(app):
        response = await post_json(app, "/v1/chat/completions", openai_body())

    assert response.status == 502
    assert response.json["error"]["code"] == "backend_error"
    assert b"secret" not in response.body


@pytest.mark.anyio
async def test_backend_reset_message_is_redacted_sse_error(tmp_path) -> None:
    reset = ConversationResetMessage("new-secret", "reset-1", "sdk-1")
    raw = raw_text_events("answer", "sdk-1")
    client = FakeSdkClient(
        responses=((raw[0], raw[1], raw[2], reset, result_message_for_app()),)
    )
    app = sdk_app(tmp_path, client)

    async with lifespan_app(app):
        response = await post_json(
            app, "/v1/chat/completions", openai_body(stream=True)
        )

    assert response.status == 200
    assert b'"type":"backend_error"' in response.body
    assert b"secret" not in response.body


@pytest.mark.anyio
async def test_unknown_raw_event_is_redacted_http_502(tmp_path) -> None:
    unknown = StreamEvent(
        uuid="event-secret",
        session_id="sdk-1",
        event={"type": "future_secret_event", "payload": "secret-payload"},
    )
    client = FakeSdkClient(responses=((unknown, result_message_for_app()),))
    app = sdk_app(tmp_path, client)

    async with lifespan_app(app):
        response = await post_json(app, "/v1/chat/completions", openai_body())

    assert response.status == 502
    assert response.json["error"]["code"] == "backend_error"
    assert b"secret" not in response.body


@pytest.mark.anyio
async def test_unknown_raw_event_after_text_is_redacted_sse_error(tmp_path) -> None:
    unknown = StreamEvent(
        uuid="event-secret",
        session_id="sdk-1",
        event={"type": "future_secret_event", "payload": "secret-payload"},
    )
    raw = raw_text_events("answer", "sdk-1")
    client = FakeSdkClient(
        responses=((raw[0], raw[1], raw[2], unknown, result_message_for_app()),)
    )
    app = sdk_app(tmp_path, client)

    async with lifespan_app(app):
        response = await post_json(
            app, "/v1/chat/completions", openai_body(stream=True)
        )

    assert response.status == 200
    assert b'"type":"backend_error"' in response.body
    assert response.body.endswith(b"data: [DONE]\n\n")
    assert b"secret" not in response.body


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


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("path", "body", "error_path"),
    [
        ("/v1/chat/completions", openai_body("second"), ("error", "code")),
        ("/v1/messages", anthropic_body("second"), ("error", "type")),
    ],
)
async def test_all_busy_capacity_is_stable_redacted_503(
    path: str, body: dict[str, object], error_path: tuple[str, str]
) -> None:
    session = BlockingStreamSession()
    app = create_app(
        models=("sonnet",),
        session_factory=one_session_factory(session),
        max_sessions=1,
    )
    async with lifespan_app(app):
        active = asyncio.create_task(
            post_json(
                app,
                "/v1/chat/completions",
                openai_body("first", stream=True),
                {"X-Claude-Proxy-Session": "busy"},
            )
        )
        await session.entered.wait()
        response = await post_json(
            app, path, body, {"X-Claude-Proxy-Session": "second"}
        )
        session.release.set()
        await active

    assert response.status == 503
    assert response.json[error_path[0]][error_path[1]] == "session_capacity"
    assert b"secret" not in response.body


@pytest.mark.parametrize("max_sessions", [0, -1])
def test_app_rejects_nonpositive_session_capacity(max_sessions: int) -> None:
    with pytest.raises(ValueError, match="max sessions"):
        create_app(
            models=("sonnet",),
            session_factory=FakeSessionFactory(()),
            max_sessions=max_sessions,
        )
