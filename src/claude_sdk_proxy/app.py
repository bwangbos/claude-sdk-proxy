from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from claude_sdk_proxy.anthropic_api import (
    encode_anthropic_error,
    encode_anthropic_event,
    encode_anthropic_start,
    parse_anthropic_request,
    render_anthropic_response,
)
from claude_sdk_proxy.asgi_stream import ClosableEventStream, EventStreamResponse
from claude_sdk_proxy.domain import (
    BackendFailure,
    Completed,
    RequestValidationError,
    SdkSessionFactory,
    TextDelta,
    TextRequest,
    UnsupportedFeature,
)
from claude_sdk_proxy.openai_api import (
    encode_openai_error,
    encode_openai_event,
    encode_openai_start,
    parse_openai_request,
    render_openai_response,
)
from claude_sdk_proxy.sdk_session import SdkSession
from claude_sdk_proxy.sessions import (
    SessionConflict,
    SessionMismatch,
    SessionRegistry,
    SessionTimeout,
    TurnLease,
)


@dataclass(frozen=True, slots=True)
class _Error:
    status: int
    code: str
    message: str
    param: str | None = None


def create_app(
    *,
    models: tuple[str, ...],
    session_factory: SdkSessionFactory = SdkSession,
    turn_timeout_seconds: float = 300.0,
) -> Starlette:
    if (
        not models
        or any(not model.strip() for model in models)
        or len(set(models)) != len(models)
    ):
        raise ValueError("models must be non-empty and unique")
    allowed_models = frozenset(models)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[dict[str, SessionRegistry]]:
        registry = SessionRegistry(session_factory, turn_timeout_seconds)
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
    try:
        body = await request.json()
        if not isinstance(body, Mapping):
            raise RequestValidationError("body", "must be a JSON object")
        allowed = cast(frozenset[str], request.app.state.allowed_models)
        parser = (
            parse_openai_request if dialect == "openai" else parse_anthropic_request
        )
        parsed = parser(cast(Mapping[str, object], body), allowed)
        registry = cast(SessionRegistry, request.state.registry)
        lease = await registry.open_turn(
            parsed, request.headers.get("x-claude-proxy-session")
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        return _error_response(dialect, error)
    request_id = ("chatcmpl_" if dialect == "openai" else "msg_") + uuid.uuid4().hex
    if parsed.stream:
        return await _stream_response(dialect, request_id, parsed, lease)
    return await _nonstream_response(dialect, request_id, parsed, lease)


async def _stream_response(
    dialect: str, request_id: str, request: TextRequest, lease: TurnLease
) -> Response:
    stream = cast(ClosableEventStream, lease.stream())
    try:
        first = await anext(stream)
    except asyncio.CancelledError:
        await lease.abort()
        await stream.aclose()
        raise
    except Exception as error:
        await lease.abort()
        await stream.aclose()
        return _error_response(dialect, error, lease.response_headers)
    if dialect == "openai":
        return cast(
            Response,
            EventStreamResponse(
                lease,
                stream,
                first,
                encode_openai_start(request_id, request.model),
                lambda event: encode_openai_event(
                    request_id, request.model, event, request.include_usage
                ),
                lambda error: encode_openai_error(
                    _error_detail(error).code, _error_detail(error).message
                ),
            ),
        )
    return cast(
        Response,
        EventStreamResponse(
            lease,
            stream,
            first,
            encode_anthropic_start(request_id, request.model),
            lambda event: encode_anthropic_event(request_id, request.model, event),
            lambda error: encode_anthropic_error(
                _error_detail(error).code, _error_detail(error).message
            ),
        ),
    )


async def _nonstream_response(
    dialect: str, request_id: str, request: TextRequest, lease: TurnLease
) -> Response:
    stream = cast(ClosableEventStream, lease.stream())
    text: list[str] = []
    completed: Completed | None = None
    try:
        async for event in stream:
            if isinstance(event, TextDelta):
                text.append(event.text)
            elif isinstance(event, Completed):
                completed = event
    except asyncio.CancelledError:
        await lease.abort()
        await stream.aclose()
        raise
    except Exception as error:
        await lease.abort()
        await stream.aclose()
        return _error_response(dialect, error, lease.response_headers)
    finally:
        await stream.aclose()
    if completed is None:
        await lease.abort()
        return _error_response(dialect, BackendFailure("missing completion"))
    renderer = (
        render_openai_response if dialect == "openai" else render_anthropic_response
    )
    payload = renderer(request_id, request.model, "".join(text), completed)
    return JSONResponse(payload, headers=lease.response_headers)


def _error_detail(error: Exception) -> _Error:
    if isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)):
        return _Error(400, "invalid_request", "Invalid request", "body")
    if isinstance(error, UnsupportedFeature):
        return _Error(
            400, "unsupported_feature", "Feature is not supported", error.field
        )
    if isinstance(error, RequestValidationError):
        if error.field == "model" and error.reason == "model is not configured":
            return _Error(404, "model_not_found", "Model is not configured", "model")
        return _Error(400, "invalid_request", "Invalid request", error.field)
    if isinstance(error, SessionConflict):
        return _Error(409, "request_in_flight", "Request is already in flight")
    if isinstance(error, SessionMismatch):
        return _Error(409, "session_mismatch", "Session transcript does not match")
    if isinstance(error, SessionTimeout):
        return _Error(504, "backend_timeout", "Backend turn timed out")
    return _Error(502, "backend_error", "Backend request failed")


def _error_response(
    dialect: str,
    error: Exception,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    detail = _error_detail(error)
    if dialect == "openai":
        payload: dict[str, Any] = {
            "error": {
                "message": detail.message,
                "type": detail.code,
                "param": detail.param,
                "code": detail.code,
            }
        }
    else:
        payload = {
            "type": "error",
            "error": {"type": detail.code, "message": detail.message},
        }
    return JSONResponse(payload, status_code=detail.status, headers=headers)
