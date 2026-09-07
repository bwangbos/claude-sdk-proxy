from __future__ import annotations

from typing import Literal, cast

from claude_sdk_proxy.domain import RequestValidationError
from claude_sdk_proxy.model_catalog import backend_model, canonical_model

type RefusalFallback = Literal["off", "auto"]


def resolve_fallback(values: list[str], default: RefusalFallback) -> RefusalFallback:
    if len(values) > 1:
        raise RequestValidationError(
            "X-Claude-Proxy-Refusal-Fallback", "must not be repeated"
        )
    value = default if not values else values[0]
    if value not in {"off", "auto"}:
        raise RequestValidationError(
            "X-Claude-Proxy-Refusal-Fallback", "must be exactly off or auto"
        )
    return cast(RefusalFallback, value)


def native_allowlist(
    model: str, configured: tuple[str, ...], policy: RefusalFallback
) -> tuple[str, ...]:
    active = canonical_model(model)
    configured_canonical = {canonical_model(value) for value in configured}
    if active not in configured_canonical:
        raise RequestValidationError("model", "model is not configured")
    allowed = [backend_model(active)]
    if policy == "auto" and active == "opus-5":
        if "opus-4.8" not in configured_canonical:
            raise RequestValidationError(
                "X-Claude-Proxy-Refusal-Fallback",
                "auto for opus-5 requires configured model opus-4.8",
            )
        allowed.append(backend_model("opus-4.8"))
    return tuple(allowed)


__all__ = ["RefusalFallback", "native_allowlist", "resolve_fallback"]
