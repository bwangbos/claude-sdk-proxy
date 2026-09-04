from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from starlette.responses import JSONResponse

from claude_sdk_proxy.domain import RequestValidationError, UnsupportedFeature
from claude_sdk_proxy.session_turn import (
    SessionCapacity,
    SessionConflict,
    SessionMismatch,
    SessionTimeout,
)


@dataclass(frozen=True, slots=True)
class ErrorDetail:
    status: int
    code: str
    message: str
    param: str | None = None


def error_detail(error: Exception) -> ErrorDetail:
    if isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)):
        return ErrorDetail(400, "invalid_request", "Invalid request", "body")
    if isinstance(error, UnsupportedFeature):
        return ErrorDetail(
            400, "unsupported_feature", "Feature is not supported", error.field
        )
    if isinstance(error, RequestValidationError):
        if error.field == "model" and error.reason == "model is not configured":
            return ErrorDetail(
                404, "model_not_found", "Model is not configured", "model"
            )
        return ErrorDetail(400, "invalid_request", "Invalid request", error.field)
    if isinstance(error, SessionConflict):
        return ErrorDetail(409, "request_in_flight", "Request is already in flight")
    if isinstance(error, SessionCapacity):
        return ErrorDetail(503, "session_capacity", "Session capacity is exhausted")
    if isinstance(error, SessionMismatch):
        return ErrorDetail(409, "session_mismatch", "Session transcript does not match")
    if isinstance(error, SessionTimeout):
        return ErrorDetail(504, "backend_timeout", "Backend turn timed out")
    return ErrorDetail(502, "backend_error", "Backend request failed")


def error_response(
    dialect: str,
    error: Exception,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    detail = error_detail(error)
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
