from __future__ import annotations

# Verified transport catalog, not a claim of access on every subscription tier.
# Provenance and verification limits: docs/models.md.
OPENAI_EFFORTS: dict[str, frozenset[str]] = {
    "gpt-6-astra": frozenset({"low", "medium", "high", "xhigh", "max"}),
    "gpt-5.6-sol": frozenset({"low", "medium", "high", "xhigh", "max"}),
    "gpt-5.6-terra": frozenset({"low", "medium", "high", "xhigh", "max"}),
    "gpt-5.6-luna": frozenset({"low", "medium", "high", "xhigh", "max"}),
    "gpt-5.5": frozenset({"low", "medium", "high", "xhigh"}),
    "gpt-5.3-codex-spark": frozenset({"low", "medium", "high", "xhigh"}),
}
OPENAI_MODELS = tuple(OPENAI_EFFORTS)
TEXT_ONLY_MODELS = frozenset({"gpt-5.3-codex-spark"})

_CANONICAL: dict[str, str] = {
    "sonnet": "sonnet-5",
    "sonnet-5": "sonnet-5",
    "claude-sonnet-5": "sonnet-5",
    "opus": "opus-5",
    "opus-5": "opus-5",
    "claude-opus-5": "opus-5",
    "opus-4.8": "opus-4.8",
    "claude-opus-4-8": "opus-4.8",
    "haiku": "haiku-4.5",
    "haiku-4.5": "haiku-4.5",
    "claude-haiku-4-5": "haiku-4.5",
    "claude-haiku-4-5-20251001": "haiku-4.5",
    "fable-5.1": "fable-5.1",
    "claude-fable-5-1": "fable-5.1",
}

_BACKEND: dict[str, str] = {
    "sonnet-5": "claude-sonnet-5",
    "opus-5": "claude-opus-5",
    "opus-4.8": "claude-opus-4-8",
    "haiku-4.5": "claude-haiku-4-5-20251001",
    "fable-5.1": "claude-fable-5-1",
}

ALL_MODELS = (
    *_BACKEND,
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-opus-4-5-20251101",
    "claude-sonnet-4-5-20250929",
    *OPENAI_MODELS,
)


def canonical_model(value: str) -> str:
    return _CANONICAL.get(value, value)


def backend_model(value: str) -> str:
    canonical = canonical_model(value)
    return _BACKEND.get(canonical, value)


def canonical_models(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(canonical_model(value) for value in values))


__all__ = ["backend_model", "canonical_model", "canonical_models"]
