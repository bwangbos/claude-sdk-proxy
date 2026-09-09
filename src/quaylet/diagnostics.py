"""Opt-in metadata-only diagnostics; never log request bodies or raw session keys."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import traceback
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, cast

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from quaylet.domain import ModelFallbackDisabled

_request_id: ContextVar[str | None] = ContextVar("proxy_request_id", default=None)
logger = logging.getLogger("quaylet.diagnostics")


def current_request_id() -> str | None:
    return _request_id.get()


def bind_task_request_id(identifier: str | None) -> None:
    """Refresh a dedicated actor task's context when its HTTP owner changes."""
    _request_id.set(identifier)


def record(event: str, **fields: str | int | bool | None) -> None:
    logger.info({"event": event, "request_id": _request_id.get(), **fields})


def record_backend_failure(error: BaseException, stage: str) -> None:
    """Record code locations and allowlisted reasons, never exception text/locals."""
    reason = {
        "Agent SDK protocol failure": "sdk_protocol_failure",
        "Agent SDK query failed": "sdk_query_failed",
        "Agent SDK stream ended without result": "sdk_missing_result",
        "Agent SDK message after result": "sdk_message_after_result",
        "SDK tool bridge closed": "tool_bridge_closed",
    }.get(str(error), "backend_failure")
    if isinstance(error, ModelFallbackDisabled):
        reason = "model_fallback_disabled"
    frames = traceback.extract_tb(error.__traceback__)
    location = " > ".join(
        f"{os.path.basename(frame.filename)}:{frame.name}:{frame.lineno}"
        for frame in frames
        if "/quaylet/" in frame.filename
    )
    record(
        "backend_failure",
        stage=stage,
        reason=reason,
        error_type=type(error).__name__,
        location=location,
    )


def session_reference(identifier: str) -> str:
    return hashlib.sha256(identifier.encode()).hexdigest()[:16]


class DiagnosticContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        identifier = "req_" + uuid.uuid4().hex
        token = _request_id.set(identifier)

        async def identified_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"x-request-id"
                ] + [(b"x-request-id", identifier.encode("ascii"))]
                record("response_started", status=message["status"])
            await send(message)

        try:
            await self.app(scope, receive, identified_send)
        finally:
            _request_id.reset(token)


class _DiagnosticFormatter(logging.Formatter):
    def __init__(self, json_output: bool) -> None:
        super().__init__()
        self.json_output = json_output

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            **cast(dict[str, Any], record.msg),
        }
        if self.json_output:
            return json.dumps(payload, separators=(",", ":"))
        return " ".join(f"{key}={value}" for key, value in payload.items())


def configure_logging(path: str | None, json_output: bool) -> logging.Handler:
    if path is None:
        handler = logging.StreamHandler(sys.stderr)
    else:
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        handler = logging.StreamHandler(os.fdopen(descriptor, "a", encoding="utf-8"))
    handler.setFormatter(_DiagnosticFormatter(json_output))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return handler


def close_logging(handler: logging.Handler, *, file_output: bool) -> None:
    logger.removeHandler(handler)
    if file_output and isinstance(handler, logging.StreamHandler):
        handler.stream.close()
    handler.close()
