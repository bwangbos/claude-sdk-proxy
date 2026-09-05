from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import cast

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from claude_sdk_proxy.anthropic_api import (
    AnthropicStreamState,
    encode_anthropic_error,
    encode_anthropic_event,
    encode_anthropic_start,
    parse_anthropic_request,
    render_anthropic_response,
)
from claude_sdk_proxy.asgi_stream import (
    ClosableEventStream,
    DisconnectMonitor,
    EventStreamResponse,
    abort_best_effort,
    cancellation_pending,
    cleanup_best_effort,
    close_best_effort,
)
from claude_sdk_proxy.domain import (
    BackendFailure,
    Completed,
    InputUsage,
    RequestValidationError,
    SdkSessionFactory,
    TextBlock,
    TextDelta,
    TextRequest,
    ToolCall,
)
from claude_sdk_proxy.http_errors import error_detail, error_response
from claude_sdk_proxy.openai_api import (
    OpenAIStreamState,
    encode_openai_error,
    encode_openai_event,
    encode_openai_start,
    parse_openai_request,
    render_openai_response,
)
from claude_sdk_proxy.sdk_session import SdkSession
from claude_sdk_proxy.session_turn import TurnLeaseProtocol
from claude_sdk_proxy.sessions import SessionRegistry


def create_app(
    *,
    models: tuple[str, ...],
    session_factory: SdkSessionFactory = SdkSession,
    turn_timeout_seconds: float = 300.0,
    tool_result_timeout_seconds: float = 300.0,
    teardown_timeout_seconds: float = 5.0,
    max_sessions: int = 8,
) -> Starlette:
    if max_sessions <= 0:
        raise ValueError("max sessions must be positive")
    if (
        not models
        or any(not model.strip() for model in models)
        or len(set(models)) != len(models)
    ):
        raise ValueError("models must be non-empty and unique")
    allowed_models = frozenset(models)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[dict[str, SessionRegistry]]:
        registry = SessionRegistry(
            session_factory,
            turn_timeout_seconds=turn_timeout_seconds,
            tool_result_timeout_seconds=tool_result_timeout_seconds,
            teardown_timeout_seconds=teardown_timeout_seconds,
            max_sessions=max_sessions,
        )
        try:
            yield {"registry": registry}
        finally:
            await registry.close()

    app = Starlette(
        routes=[
            Route("/health", _health, methods=["GET"]),
            Route("/v1/models", _models, methods=["GET"]),
            Route("/v1/messages", _anthropic, methods=["POST"]),
            Route("/v1/chat/completions", _openai, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
    app.state.allowed_models = allowed_models
    app.state.model_order = models
    return app


async def _health(_: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def _models(request: Request) -> Response:
    data = [
        {"id": model, "object": "model", "created": 0, "owned_by": "anthropic"}
        for model in cast(tuple[str, ...], request.app.state.model_order)
    ]
    return JSONResponse({"object": "list", "data": data})


async def _openai(request: Request) -> Response:
    return await _handle(request, dialect="openai")


async def _anthropic(request: Request) -> Response:
    return await _handle(request, dialect="anthropic")


async def _handle(request: Request, *, dialect: str) -> Response:
    monitor: DisconnectMonitor | None = None
    try:
        media_type = request.headers.get("content-type", "").partition(";")[0]
        if media_type.strip().lower() != "application/json":
            raise RequestValidationError(
                "body", "content type must be application/json"
            )
        body = await request.json()
        if not isinstance(body, Mapping):
            raise RequestValidationError("body", "must be a JSON object")
        allowed = cast(frozenset[str], request.app.state.allowed_models)
        parser = (
            parse_openai_request if dialect == "openai" else parse_anthropic_request
        )
        parsed = parser(cast(Mapping[str, object], body), allowed)
        monitor = DisconnectMonitor(request.receive)
        await monitor.start()
        registry = cast(SessionRegistry, request.state.registry)
        lease = await registry.open_turn(
            parsed, request.headers.get("x-claude-proxy-session")
        )
    except asyncio.CancelledError:
        if monitor is not None and monitor.consume_disconnect_cancellation():
            return cast(Response, monitor.wrap(None))
        if monitor is not None:
            await monitor.close()
        raise
    except Exception as error:
        response = error_response(dialect, error)
        return response if monitor is None else cast(Response, monitor.wrap(response))
    request_id = ("chatcmpl_" if dialect == "openai" else "msg_") + uuid.uuid4().hex
    if parsed.stream:
        return await _stream_response(dialect, request_id, parsed, lease, monitor)
    return await _nonstream_response(dialect, request_id, parsed, lease, monitor)


async def _stream_response(
    dialect: str,
    request_id: str,
    request: TextRequest,
    lease: TurnLeaseProtocol,
    monitor: DisconnectMonitor,
) -> Response:
    stream = cast(ClosableEventStream, lease.stream())
    try:
        first = await anext(stream)
    except asyncio.CancelledError:
        await cleanup_best_effort(lease, stream)
        if monitor.consume_disconnect_cancellation():
            return cast(Response, monitor.wrap(None))
        await monitor.close()
        raise
    except Exception as error:
        await cleanup_best_effort(lease, stream)
        if cancellation_pending():
            raise asyncio.CancelledError from None
        response = error_response(dialect, error, lease.response_headers)
        return cast(Response, monitor.wrap(response))
    if dialect == "openai":
        openai_state = OpenAIStreamState()
        stream_response = EventStreamResponse(
                lease,
                stream,
                first,
                encode_openai_start(request_id, request.model),
                lambda event: encode_openai_event(
                    request_id,
                    request.model,
                    event,
                    request.include_usage,
                    openai_state,
                ),
                lambda error: encode_openai_error(
                    error_detail(error).code, error_detail(error).message
                ),
            )
        return cast(Response, monitor.wrap(stream_response))
    anthropic_state = AnthropicStreamState()
    stream_response = EventStreamResponse(
            lease,
            stream,
            first,
            encode_anthropic_start(
                request_id,
                request.model,
                first.input_tokens if isinstance(first, InputUsage) else 0,
                request.tools,
                anthropic_state,
            ),
            lambda event: encode_anthropic_event(
                request_id, request.model, event, state=anthropic_state
            ),
            lambda error: encode_anthropic_error(
                error_detail(error).code, error_detail(error).message
            ),
        )
    return cast(Response, monitor.wrap(stream_response))


async def _nonstream_response(
    dialect: str,
    request_id: str,
    request: TextRequest,
    lease: TurnLeaseProtocol,
    monitor: DisconnectMonitor,
) -> Response:
    stream = cast(ClosableEventStream, lease.stream())
    blocks: list[TextBlock | ToolCall] = []
    completed: Completed | None = None
    try:
        async for event in stream:
            if isinstance(event, TextDelta):
                if blocks and isinstance(blocks[-1], TextBlock):
                    blocks[-1] = TextBlock(blocks[-1].text + event.text)
                else:
                    blocks.append(TextBlock(event.text))
            elif isinstance(event, ToolCall):
                blocks.append(event)
            elif isinstance(event, Completed):
                completed = event
    except asyncio.CancelledError:
        await cleanup_best_effort(lease, stream)
        if monitor.consume_disconnect_cancellation():
            return cast(Response, monitor.wrap(None))
        await monitor.close()
        raise
    except Exception as error:
        await cleanup_best_effort(lease, stream)
        if cancellation_pending():
            raise asyncio.CancelledError from None
        response = error_response(dialect, error, lease.response_headers)
        return cast(Response, monitor.wrap(response))
    finally:
        await close_best_effort(stream)
    if completed is None:
        await abort_best_effort(lease)
        response = error_response(dialect, BackendFailure("missing completion"))
        return cast(Response, monitor.wrap(response))
    renderer = (
        render_openai_response if dialect == "openai" else render_anthropic_response
    )
    rendered = (
        tuple(blocks)
        if request.tools
        else "".join(block.text for block in blocks if isinstance(block, TextBlock))
    )
    payload = renderer(request_id, request.model, rendered, completed)
    response = JSONResponse(payload, headers=lease.response_headers)
    return cast(Response, monitor.wrap(response))
