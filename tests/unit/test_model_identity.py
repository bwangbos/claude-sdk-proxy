"""Exact backend-model identity must precede every releasable event."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from claude_sdk_proxy.attestation import (
    CanonicalEvent,
    ModelIdentityError,
    ModelIdentityGate,
)


def text_delta(text: str, *, model: str | None = None) -> CanonicalEvent:
    return CanonicalEvent(event_type="text_delta", model=model, content=text)


def message_start(*, model: str | None) -> CanonicalEvent:
    return CanonicalEvent(event_type="message_start", model=model)


def test_content_is_buffered_until_exact_model_identity() -> None:
    gate = ModelIdentityGate(expected="claude-sonnet-4-5-exact")
    assert gate.observe(text_delta("secret")) == ()
    with pytest.raises(ModelIdentityError):
        gate.observe(message_start(model="fallback-model"))
    assert gate.released_content_count == 0


def test_exact_identity_releases_the_original_order_then_streams_immediately() -> None:
    gate = ModelIdentityGate(expected="claude-sonnet-4-5-exact")
    first = CanonicalEvent(event_type="content_block_start")
    second = text_delta("first")
    identity = message_start(model="claude-sonnet-4-5-exact")

    assert gate.observe(first) == ()
    assert gate.observe(second) == ()
    assert gate.observe(identity) == (first, second, identity)
    later = text_delta("second")
    assert gate.observe(later) == (later,)
    assert gate.released_content_count == 2


@pytest.mark.parametrize(
    "event",
    [
        CanonicalEvent(event_type="message_start", model=None),
        CanonicalEvent(event_type="assistant", model=None),
        CanonicalEvent(event_type="message_stop"),
        CanonicalEvent(event_type="result", success=True),
    ],
)
def test_missing_identity_or_terminal_framing_discards_every_buffered_event(
    event: CanonicalEvent,
) -> None:
    gate = ModelIdentityGate(expected="claude-sonnet-4-5-exact")
    assert gate.observe(text_delta("never-release")) == ()

    with pytest.raises(ModelIdentityError):
        gate.observe(event)
    with pytest.raises(ModelIdentityError, match="failed"):
        gate.observe(message_start(model="claude-sonnet-4-5-exact"))
    assert gate.released_content_count == 0


@pytest.mark.parametrize("event_type", ["assistant", "message_start", "text_delta"])
def test_every_later_model_bearing_event_must_match(event_type: str) -> None:
    gate = ModelIdentityGate(expected="claude-sonnet-4-5-exact")
    identity = message_start(model="claude-sonnet-4-5-exact")
    assert gate.observe(identity) == (identity,)

    with pytest.raises(ModelIdentityError, match="model identity"):
        gate.observe(
            CanonicalEvent(
                event_type=event_type,
                model="claude-fallback-exact",
                content="never-release" if event_type == "text_delta" else None,
            )
        )
    assert gate.released_content_count == 0


def test_a_later_authoritative_envelope_cannot_omit_model_identity() -> None:
    gate = ModelIdentityGate(expected="claude-sonnet-4-5-exact")
    identity = message_start(model="claude-sonnet-4-5-exact")
    assert gate.observe(identity) == (identity,)

    with pytest.raises(ModelIdentityError, match="model identity"):
        gate.observe(CanonicalEvent(event_type="assistant"))


@pytest.mark.parametrize(
    "moving_alias",
    [
        "sonnet",
        "opus",
        "haiku",
        "latest",
        "claude-sonnet-4-5",
        "claude-opus-4-1-latest",
    ],
)
def test_generic_moving_aliases_cannot_open_an_identity_gate(
    moving_alias: str,
) -> None:
    with pytest.raises(ModelIdentityError, match="exact backend model"):
        ModelIdentityGate(expected=moving_alias)


def test_events_are_frozen_snapshots_and_exact_builtin_types_are_required() -> None:
    event = text_delta("unchanged")
    with pytest.raises(FrozenInstanceError):
        event.content = "mutated"  # type: ignore[misc]
    with pytest.raises((TypeError, ValueError)):
        CanonicalEvent(event_type="text_delta", content=object())  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError)):
        CanonicalEvent(event_type="result", success=1)  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError)):
        CanonicalEvent(event_type="unknown")


def test_invalid_or_oversized_events_poison_without_releasing_content() -> None:
    gate = ModelIdentityGate(expected="claude-sonnet-4-5-exact")
    assert gate.observe(text_delta("buffered")) == ()

    with pytest.raises(ModelIdentityError):
        gate.observe(object())  # type: ignore[arg-type]
    assert gate.released_content_count == 0
    with pytest.raises(ModelIdentityError, match="failed"):
        gate.observe(message_start(model="claude-sonnet-4-5-exact"))

