from __future__ import annotations

_CANONICAL: dict[str, str] = {
    "sonnet": "sonnet-5",
    "sonnet-5": "sonnet-5",
    "claude-sonnet-5": "sonnet-5",
    "opus": "opus-5",
    "opus-5": "opus-5",
    "claude-opus-5": "opus-5",
    "opus-4.8": "opus-4.8",
    "claude-opus-4-8": "opus-4.8",
}

_BACKEND: dict[str, str] = {
    "sonnet-5": "claude-sonnet-5",
    "opus-5": "claude-opus-5",
    "opus-4.8": "claude-opus-4-8",
}


def canonical_model(value: str) -> str:
    return _CANONICAL.get(value, value)


def backend_model(value: str) -> str:
    canonical = canonical_model(value)
    return _BACKEND.get(canonical, value)


def canonical_models(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(canonical_model(value) for value in values))


__all__ = ["backend_model", "canonical_model", "canonical_models"]
