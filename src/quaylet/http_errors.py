from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from starlette.responses import JSONResponse

from quaylet.diagnostics import record
from quaylet.domain import (
    BackendFailure,
    ModelFallbackDisabled,
    RequestValidationError,
    UnsupportedFeature,
)
from quaylet.openai_subscription.backend import SubscriptionFailure
from quaylet.session_turn import (
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
    reason: str | None = None


_SESSION_REASONS = {
    "pending tool calls changed": (
        "pending_calls_changed",
        "Pending tool call definitions changed.",
    ),
    "invalid explicit session ID": (
        "invalid_session_id",
        "Invalid session identifier.",
    ),
    "request transcript matches multiple conversations": (
        "ambiguous_session",
        "Transcript matches multiple conversations.",
    ),
    "request transcript does not match conversation": (
        "transcript_changed",
        "Transcript differs from the selected conversation.",
    ),
    "request transcript is stale": (
        "stale_head",
        "Transcript is an older conversation head.",
    ),
    "request transcript is not a fresh conversation": (
        "missing_session",
        "Tool results require a matching session or complete imported history.",
    ),
    "session model does not match": ("model_changed", "Session model changed."),
    "session system does not match": (
        "system_changed",
        "Session system prompt changed.",
    ),
    "session tool configuration does not match": (
        "configuration_changed",
        "Session tools or API dialect changed.",
    ),
    "tool results are required": (
        "tool_results_required",
        "Pending calls require tool results.",
    ),
    "tool results do not match pending calls": (
        "tool_ids_mismatch",
        "Tool result IDs do not match the pending calls.",
    ),
    "conversation is no longer active": (
        "session_closed",
        "Session is no longer active.",
    ),
    "conversation has pending tools": (
        "pending_tools",
        "Complete pending tool calls before replacing this conversation.",
    ),
    "request is already in flight": (
        "duplicate_in_flight",
        "This request is already in flight.",
    ),
    "conversation is busy": ("session_busy", "The selected conversation is busy."),
}


def _session_error(error: SessionMismatch | SessionConflict) -> ErrorDetail:
    reason, message = _SESSION_REASONS.get(
        str(error), ("session_mismatch", "Session transcript does not match.")
    )
    code = (
        "request_in_flight"
        if isinstance(error, SessionConflict)
        else "session_mismatch"
    )
    record("request_rejected", code=code, reason=reason, status=409)
    return ErrorDetail(
        409,
        code,
        message + " Use a unique X-Quaylet-Session per conversation; "
        "retry only after active work completes.",
        "messages",
        reason,
    )


def error_detail(error: Exception) -> ErrorDetail:
    if isinstance(error, SubscriptionFailure):
        status, code, message = {
            "authentication": (
                401,
                "authentication_error",
                "Proxy OpenAI login is required.",
            ),
            "access_denied": (
                403,
                "permission_error",
                "OpenAI subscription access was denied.",
            ),
            "rate_limit": (
                429,
                "rate_limit_error",
                "OpenAI subscription rate limit reached.",
            ),
            "timeout": (
                504,
                "backend_timeout",
                "OpenAI subscription request timed out.",
            ),
            "closed": (
                503,
                "backend_unavailable",
                "OpenAI subscription backend is closed.",
            ),
        }.get(
            error.category,
            (502, "backend_error", "OpenAI subscription request failed."),
        )
        record("request_rejected", code=code, reason=error.category, status=status)
        return ErrorDetail(status, code, message)
    if isinstance(error, BackendFailure):
        reason = str(error).partition(":")[0]
        messages = {
            "backend_model_mismatch": "Backend model identity validation failed.",
            "fallback_buffer_limit": (
                "Automatic fallback response exceeded its buffer limit."
            ),
            "fallback_tool_rollback_unsupported": (
                "Fallback cannot retract native tool activity."
            ),
        }
        if reason in messages:
            record("request_rejected", code="backend_error", reason=reason, status=502)
            return ErrorDetail(502, "backend_error", messages[reason], "model", reason)
    if isinstance(error, ModelFallbackDisabled):
        return ErrorDetail(
            502, "backend_error", str(error), "model", "model_fallback_disabled"
        )
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
        return _session_error(error)
    if isinstance(error, SessionCapacity):
        return ErrorDetail(503, "session_capacity", "Session capacity is exhausted")
    if isinstance(error, SessionMismatch):
        return _session_error(error)
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
    if detail.reason is not None:
        payload["error"]["reason"] = detail.reason
    return JSONResponse(payload, status_code=detail.status, headers=headers)
