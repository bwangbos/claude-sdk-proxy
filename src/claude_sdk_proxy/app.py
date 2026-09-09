from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
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
from claude_sdk_proxy.diagnostics import DiagnosticContextMiddleware
from claude_sdk_proxy.domain import (
    BackendFailure,
    Completed,
    ConversationEvent,
    InputUsage,
    RedactedThinkingBlock,
    RequestValidationError,
    ResponseIdentity,
    SdkSessionFactory,
    TextBlock,
    TextDelta,
    TextRequest,
    ThinkingBlock,
    ThinkingCompleted,
    ToolCall,
)
from claude_sdk_proxy.fallback_policy import (
    RefusalFallback,
    native_allowlist,
    resolve_fallback,
)
from claude_sdk_proxy.http_errors import error_detail, error_response
from claude_sdk_proxy.model_catalog import canonical_models
from claude_sdk_proxy.openai_api import (
    OpenAIStreamState,
    encode_openai_error,
    encode_openai_event,
    encode_openai_start,
    parse_openai_request,
    render_openai_response,
)
from claude_sdk_proxy.openai_subscription.backend import Backend as SubscriptionBackend
from claude_sdk_proxy.openai_subscription.translation import MODELS as OPENAI_MODELS
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
    refusal_fallback: RefusalFallback = "off",
    subscription_backend: SubscriptionBackend | None = None,
) -> Starlette:
    if max_sessions <= 0:
        raise ValueError("max sessions must be positive")
    if (
        not models
        or any(not model.strip() for model in models)
        or len(set(models)) != len(models)
    ):
        raise ValueError("models must be non-empty and unique")
    if refusal_fallback not in {"off", "auto"}:
        raise ValueError("refusal fallback must be off or auto")
    model_order = canonical_models(models)
    allowed_models = frozenset(model_order)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[dict[str, object]]:
        registry = SessionRegistry(
            session_factory,
            turn_timeout_seconds=turn_timeout_seconds,
            tool_result_timeout_seconds=tool_result_timeout_seconds,
            teardown_timeout_seconds=teardown_timeout_seconds,
            max_sessions=max_sessions,
            configured_models=tuple(m for m in model_order if m not in OPENAI_MODELS),
        )
        subscription = subscription_backend
        if subscription is None and allowed_models.intersection(OPENAI_MODELS):
            subscription = SubscriptionBackend(
                max_sessions=max_sessions, timeout=turn_timeout_seconds
            )
        try:
            yield {"registry": registry, "subscription": subscription}
        finally:
            try:
                if subscription is not None:
                    await subscription.close()
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
    app.add_middleware(DiagnosticContextMiddleware)
    app.state.allowed_models = allowed_models
    app.state.model_order = model_order
    app.state.refusal_fallback = refusal_fallback
    return app


async def _health(_: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def _models(request: Request) -> Response:
    data = [
        {
            "id": model,
            "object": "model",
            "created": 0,
            "owned_by": "openai" if model in OPENAI_MODELS else "anthropic",
        }
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
        try:
            body = await request.json()
        except RecursionError:
            raise RequestValidationError("body", "JSON nesting is invalid") from None
        if not isinstance(body, Mapping):
            raise RequestValidationError("body", "must be a JSON object")
        allowed = cast(frozenset[str], request.app.state.allowed_models)
        parser = (
            parse_openai_request if dialect == "openai" else parse_anthropic_request
        )
        parsed = parser(cast(Mapping[str, object], body), allowed)
        policy = resolve_fallback(
            request.headers.getlist("x-claude-proxy-refusal-fallback"),
            "off"
            if parsed.model in OPENAI_MODELS
            else cast(RefusalFallback, request.app.state.refusal_fallback),
        )
        if parsed.model not in OPENAI_MODELS:
            for message in parsed.messages:
                for block in message.blocks:
                    opaque = (
                        block.signature
                        if isinstance(block, ThinkingBlock)
                        else block.data
                        if isinstance(block, RedactedThinkingBlock)
                        else ""
                    )
                    if opaque.startswith("openai-subscription:"):
                        raise RequestValidationError(
                            "messages", "OpenAI metadata cannot be sent to Claude"
                        )
            native_allowlist(
                parsed.model,
                tuple(
                    m for m in request.app.state.model_order if m not in OPENAI_MODELS
                ),
                policy,
            )
        parsed = replace(parsed, refusal_fallback=policy)
        monitor = DisconnectMonitor(request.receive)
        await monitor.start()
        lease: TurnLeaseProtocol
        if parsed.model in OPENAI_MODELS:
            subscription = cast(SubscriptionBackend, request.state.subscription)
            lease = await subscription.open_turn(parsed)
        else:
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
    created = _completion_created() if dialect == "openai" else 0
    if parsed.stream:
        return await _stream_response(
            dialect, request_id, parsed, lease, monitor, created
        )
    return await _nonstream_response(
        dialect, request_id, parsed, lease, monitor, created
    )


def _completion_created() -> int:
    return int(time.time())


async def _stream_response(
    dialect: str,
    request_id: str,
    request: TextRequest,
    lease: TurnLeaseProtocol,
    monitor: DisconnectMonitor,
    created: int,
) -> Response:
    stream = cast(ClosableEventStream, lease.stream())
    try:
        identity = await anext(stream)
        if not isinstance(identity, ResponseIdentity):
            raise BackendFailure("backend_model_mismatch")
        _identity_headers(lease, identity)
        first = await anext(stream)
        while isinstance(first, ResponseIdentity):
            identity = first
            _identity_headers(lease, identity)
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
        current_identity: ResponseIdentity = identity

        def render_event(event: ConversationEvent) -> tuple[bytes, ...]:
            nonlocal current_identity
            if isinstance(event, ResponseIdentity):
                current_identity = event
                return ()
            return encode_openai_event(
                request_id,
                current_identity.actual_model,
                event,
                request.include_usage,
                openai_state,
                created=created,
            )

        stream_response = EventStreamResponse(
            lease,
            stream,
            first,
            encode_openai_start(request_id, identity.actual_model, created=created),
            render_event,
            lambda error: encode_openai_error(
                error_detail(error).code, error_detail(error).message
            ),
        )
        return cast(Response, monitor.wrap(stream_response))
    anthropic_state = AnthropicStreamState(
        lazy_blocks=True, subscription=request.model in OPENAI_MODELS
    )
    stream_response = EventStreamResponse(
        lease,
        stream,
        first,
        encode_anthropic_start(
            request_id,
            identity.actual_model,
            first if isinstance(first, InputUsage) else 0,
            request.tools,
            anthropic_state,
        ),
        lambda event: encode_anthropic_event(
            request_id, identity.actual_model, event, state=anthropic_state
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
    created: int,
) -> Response:
    stream = cast(ClosableEventStream, lease.stream())
    blocks: list[TextBlock | ToolCall | ThinkingBlock | RedactedThinkingBlock] = []
    completed: Completed | None = None
    identity: ResponseIdentity | None = None
    try:
        async for event in stream:
            if isinstance(event, ResponseIdentity):
                identity = event
                continue
            if identity is None:
                raise BackendFailure("backend_model_mismatch")
            if isinstance(event, TextDelta):
                if blocks and isinstance(blocks[-1], TextBlock):
                    blocks[-1] = TextBlock(blocks[-1].text + event.text)
                else:
                    blocks.append(TextBlock(event.text))
            elif isinstance(event, ToolCall):
                blocks.append(event)
            elif isinstance(event, ThinkingCompleted):
                blocks.append(event.block)
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
    if completed is None or identity is None:
        await abort_best_effort(lease)
        response = error_response(dialect, BackendFailure("missing completion"))
        return cast(Response, monitor.wrap(response))
    rendered: tuple[TextBlock | ToolCall | ThinkingBlock | RedactedThinkingBlock, ...]
    if dialect == "anthropic" and completed.refusal is not None:
        blocks.append(TextBlock(completed.refusal))
    if not blocks and completed.stop_reason == "refusal":
        rendered = () if dialect == "anthropic" else (TextBlock(""),)
    else:
        rendered = tuple(blocks) if blocks or request.tools else (TextBlock(""),)
    payload = (
        render_openai_response(
            request_id,
            identity.actual_model,
            rendered,
            completed,
            created=created,
        )
        if dialect == "openai"
        else render_anthropic_response(
            request_id, identity.actual_model, rendered, completed
        )
    )
    _identity_headers(lease, identity)
    response = JSONResponse(payload, headers=lease.response_headers)
    return cast(Response, monitor.wrap(response))


def _identity_headers(lease: TurnLeaseProtocol, identity: ResponseIdentity) -> None:
    lease.response_headers["X-Claude-Proxy-Requested-Model"] = identity.requested_model
    if identity.verified:
        lease.response_headers["X-Claude-Proxy-Actual-Model"] = identity.actual_model
    else:
        lease.response_headers.pop("X-Claude-Proxy-Actual-Model", None)
    if identity.fallback:
        lease.response_headers["X-Claude-Proxy-Fallback"] = "true"
