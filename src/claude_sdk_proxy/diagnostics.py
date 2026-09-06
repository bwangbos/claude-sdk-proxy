"""Opt-in metadata-only diagnostics; never log request bodies or raw session keys."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, cast

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_request_id: ContextVar[str | None] = ContextVar("proxy_request_id", default=None)
logger = logging.getLogger("claude_sdk_proxy.diagnostics")


def record(event: str, **fields: str | int | bool | None) -> None:
    logger.info({"event": event, "request_id": _request_id.get(), **fields})


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
