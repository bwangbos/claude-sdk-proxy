from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Never, cast

type ThinkingMode = Literal["disabled", "adaptive", "enabled"]
type ThinkingEffort = Literal["low", "medium", "high", "xhigh", "max"]
type ThinkingDisplay = Literal["summarized", "omitted"]

_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
_DISPLAYS = frozenset({"summarized", "omitted"})

# Snapshot of Claude Code 2.1.259 model metadata. Moving aliases are listed
# explicitly so a future alias change cannot accidentally grant capabilities to
# an unrelated model. Pinned model IDs remain preferable for reproducibility.
_LATEST_ADAPTIVE_EFFORTS: dict[str, frozenset[str]] = {
    model: _EFFORTS
    for model in (
        "opus",
        "opus[1m]",
        "sonnet",
        "claude-opus-5",
        "claude-opus-5[1m]",
        "claude-sonnet-5",
        "opus-5",
        "sonnet-5",
        "opus-4.8",
        "claude-opus-4-8",
    )
}
_FOUR_SIX_EFFORTS = frozenset({"low", "medium", "high", "max"})
_ADAPTIVE_EFFORTS: dict[str, frozenset[str]] = {
    **_LATEST_ADAPTIVE_EFFORTS,
    "claude-opus-4-6": _FOUR_SIX_EFFORTS,
    "claude-opus-4-6[1m]": _FOUR_SIX_EFFORTS,
    "claude-sonnet-4-6": _FOUR_SIX_EFFORTS,
}
_ENABLED_EFFORTS: dict[str, frozenset[str]] = {
    "claude-opus-4-5": frozenset({"low", "medium", "high"}),
    "claude-opus-4-5-20251101": frozenset({"low", "medium", "high"}),
    "claude-sonnet-4-5": frozenset(),
    "claude-sonnet-4-5-20250929": frozenset(),
}


@dataclass(frozen=True, slots=True)
class ThinkingOptions:
    mode: ThinkingMode = "disabled"
    effort: ThinkingEffort | None = None
    budget_tokens: int | None = None
    display: ThinkingDisplay | None = None


def parse_openai_thinking(body: Mapping[str, object], model: str) -> ThinkingOptions:
    value = body.get("reasoning_effort")
    if value is None or value == "none":
        return ThinkingOptions()
    if not isinstance(value, str) or value not in _EFFORTS:
        _invalid(
            "reasoning_effort",
            "must be one of none, low, medium, high, xhigh, or max",
        )
    effort = cast(ThinkingEffort, value)
    _require_effort(model, "adaptive", effort, "reasoning_effort")
    return ThinkingOptions(mode="adaptive", effort=effort)


def parse_anthropic_thinking(body: Mapping[str, object], model: str) -> ThinkingOptions:
    output = body.get("output_config")
    effort: ThinkingEffort | None = None
    if output is not None:
        if not isinstance(output, Mapping):
            _invalid("output_config", "must be an object or null")
        unknown = set(output) - {"effort"}
        if unknown:
            _invalid("output_config", "contains unknown fields")
        raw_effort = output.get("effort")
        if raw_effort is not None:
            if not isinstance(raw_effort, str) or raw_effort not in _EFFORTS:
                _invalid(
                    "output_config",
                    "effort must be one of low, medium, high, xhigh, or max",
                )
            effort = cast(ThinkingEffort, raw_effort)

    raw = body.get("thinking")
    if raw is None:
        if effort is not None:
            _invalid("output_config", "effort requires adaptive or enabled thinking")
        return ThinkingOptions()
    if not isinstance(raw, Mapping):
        _invalid("thinking", "must be an object or null")
    mode = raw.get("type")
    if not isinstance(mode, str) or mode not in {"disabled", "adaptive", "enabled"}:
        _invalid("thinking", "type must be disabled, adaptive, or enabled")
    allowed = {
        "disabled": {"type"},
        "adaptive": {"type", "display"},
        "enabled": {"type", "budget_tokens", "display"},
    }[mode]
    if set(raw) - allowed:
        _invalid("thinking", "contains unknown fields")

    display = _display(raw)
    if mode == "disabled":
        if effort is not None:
            _invalid("output_config", "effort cannot be used with disabled thinking")
        return ThinkingOptions()
    if mode == "adaptive":
        _require_mode(model, "adaptive")
        if effort is not None:
            _require_effort(model, "adaptive", effort, "output_config")
        return ThinkingOptions(mode="adaptive", effort=effort, display=display)

    budget = raw.get("budget_tokens")
    if type(budget) is not int or budget <= 0:
        _invalid("thinking", "budget_tokens must be a positive integer")
    _require_mode(model, "enabled")
    if effort is not None:
        _require_effort(model, "enabled", effort, "output_config")
    return ThinkingOptions(
        mode="enabled", effort=effort, budget_tokens=budget, display=display
    )


def _display(raw: Mapping[str, object]) -> ThinkingDisplay | None:
    value = raw.get("display")
    if value is None:
        return None
    if not isinstance(value, str) or value not in _DISPLAYS:
        _invalid("thinking", "display must be summarized or omitted")
    return cast(ThinkingDisplay, value)


def _require_mode(model: str, mode: Literal["adaptive", "enabled"]) -> None:
    policy = _ADAPTIVE_EFFORTS if mode == "adaptive" else _ENABLED_EFFORTS
    if model not in policy:
        _invalid("thinking", f"{mode} thinking is not supported for model {model}")


def _require_effort(
    model: str,
    mode: Literal["adaptive", "enabled"],
    effort: ThinkingEffort,
    field: str,
) -> None:
    policy = _ADAPTIVE_EFFORTS if mode == "adaptive" else _ENABLED_EFFORTS
    if effort not in policy.get(model, frozenset()):
        _invalid(field, f"effort {effort} is not supported for model {model}")


def _invalid(field: str, reason: str) -> Never:
    from quaylet.domain import RequestValidationError

    raise RequestValidationError(field, reason)


__all__ = ["ThinkingOptions", "parse_anthropic_thinking", "parse_openai_thinking"]
