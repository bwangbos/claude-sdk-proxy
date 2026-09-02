"""Exact backend-model identity must precede every releasable event."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

import claude_sdk_proxy.attestation as attestation
from claude_sdk_proxy.attestation import (
    CanonicalEvent,
    ModelIdentityError,
    ModelIdentityGate,
    current_attestation_availability,
)
from claude_sdk_proxy.probes import (
    ProbeUnavailable,
    run_session_probe,
    run_stream_probe,
    run_thinking_probe,
    run_usage_probe,
)

_EXACT_MODEL_ID = "claude-sonnet-4-5-exact"


def exact_model_aliases(
    aliases: dict[str, str] | None = None,
) -> attestation.ExactModelAliasMap:
    return attestation.ExactModelAliasMap(
        {"sonnet": _EXACT_MODEL_ID} if aliases is None else aliases
    )


def identity_gate(
    *,
    aliases: attestation.ExactModelAliasMap | None = None,
    public_alias: str = "sonnet",
) -> ModelIdentityGate:
    return ModelIdentityGate(
        model_aliases=exact_model_aliases() if aliases is None else aliases,
        public_alias=public_alias,
    )


def text_delta(text: str, *, model: str | None = None) -> CanonicalEvent:
    return CanonicalEvent(event_type="text_delta", model=model, content=text)


def message_start(*, model: str | None) -> CanonicalEvent:
    return CanonicalEvent(event_type="message_start", model=model)


def test_content_is_buffered_until_exact_model_identity() -> None:
    gate = identity_gate()
    assert gate.observe(text_delta("secret")) == ()
    with pytest.raises(ModelIdentityError):
        gate.observe(message_start(model="fallback-model"))
    assert gate.released_content_count == 0


def test_exact_identity_releases_the_original_order_then_streams_immediately() -> None:
    gate = identity_gate()
    first = CanonicalEvent(event_type="content_block_start")
    second = text_delta("first")
    identity = message_start(model="claude-sonnet-4-5-exact")

    assert gate.observe(first) == ()
    assert gate.observe(second) == ()
    assert gate.observe(identity) == (identity, first, second)
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
    gate = identity_gate()
    assert gate.observe(text_delta("never-release")) == ()

    with pytest.raises(ModelIdentityError):
        gate.observe(event)
    with pytest.raises(ModelIdentityError, match="failed"):
        gate.observe(message_start(model="claude-sonnet-4-5-exact"))
    assert gate.released_content_count == 0


@pytest.mark.parametrize("event_type", ["assistant", "message_start", "text_delta"])
def test_every_later_model_bearing_event_must_match(event_type: str) -> None:
    gate = identity_gate()
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
    gate = identity_gate()
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
        "claude-3-5-sonnet",
        "claude-opus-4-1-latest",
    ],
)
def test_generic_moving_aliases_cannot_open_an_identity_gate(
    moving_alias: str,
) -> None:
    with pytest.raises(ModelIdentityError, match="exact backend model"):
        exact_model_aliases({"candidate": moving_alias})


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
    with pytest.raises(ValueError, match="byte bound"):
        text_delta("x" * (1024 * 1024 + 1))

    gate = identity_gate()
    assert gate.observe(text_delta("buffered")) == ()

    with pytest.raises(ModelIdentityError):
        gate.observe(object())  # type: ignore[arg-type]
    assert gate.released_content_count == 0
    with pytest.raises(ModelIdentityError, match="failed"):
        gate.observe(message_start(model="claude-sonnet-4-5-exact"))


def test_all_semantic_probe_entry_points_fail_before_any_live_action() -> None:
    assert current_attestation_availability().core_gate_available is False
    for runner in (
        run_session_probe,
        run_stream_probe,
        run_thinking_probe,
        run_usage_probe,
    ):
        with pytest.raises(ProbeUnavailable, match="child_attestation_unavailable"):
            runner(model_aliases=exact_model_aliases(), public_alias="sonnet")


def test_identity_releases_before_buffered_content_in_original_order() -> None:
    gate = identity_gate()
    first = CanonicalEvent(event_type="content_block_start")
    second = text_delta("second")
    identity = message_start(model="claude-sonnet-4-5-exact")

    assert gate.observe(first) == ()
    assert gate.observe(second) == ()

    assert gate.observe(identity) == (identity, first, second)


def test_finish_before_identity_clears_pending_content_and_fails_closed() -> None:
    gate = identity_gate()
    gate.observe(text_delta("secret"))

    with pytest.raises(ModelIdentityError, match="model identity"):
        gate.finish()

    with pytest.raises(ModelIdentityError, match="model identity"):
        gate.observe(message_start(model="claude-sonnet-4-5-exact"))
    with pytest.raises(ModelIdentityError, match="model identity"):
        gate.finish()


def test_verified_finish_is_one_shot_safe_and_closes_observation() -> None:
    gate = identity_gate()
    identity = message_start(model="claude-sonnet-4-5-exact")
    assert gate.observe(identity) == (identity,)

    assert gate.finish() is None
    assert gate.finish() is None
    with pytest.raises(ModelIdentityError, match="model identity"):
        gate.observe(text_delta("late"))


def test_gate_accepts_only_a_configured_alias_lookup_not_a_raw_backend_id() -> None:
    aliases = exact_model_aliases()
    gate = identity_gate(aliases=aliases, public_alias="sonnet")
    identity = message_start(model=_EXACT_MODEL_ID)
    assert gate.observe(identity) == (identity,)

    with pytest.raises(TypeError):
        ModelIdentityGate(expected=_EXACT_MODEL_ID)  # type: ignore[call-arg]
    with pytest.raises(ModelIdentityError, match="configured model alias"):
        identity_gate(aliases=aliases, public_alias="opus")
    with pytest.raises(TypeError):
        ModelIdentityGate(  # type: ignore[arg-type]
            model_aliases={"sonnet": _EXACT_MODEL_ID},
            public_alias="sonnet",
        )


def test_exact_alias_map_is_copied_immutable_and_rejects_backend_ambiguity() -> None:
    source = {"sonnet": _EXACT_MODEL_ID}
    aliases = exact_model_aliases(source)
    source["sonnet"] = "claude-fallback-exact"

    gate = identity_gate(aliases=aliases)
    identity = message_start(model=_EXACT_MODEL_ID)
    assert gate.observe(identity) == (identity,)
    with pytest.raises(TypeError):
        aliases.aliases["sonnet"] = "claude-fallback-exact"  # type: ignore[index]
    with pytest.raises(ModelIdentityError, match="ambiguous"):
        exact_model_aliases(
            {
                "sonnet": _EXACT_MODEL_ID,
                "primary": _EXACT_MODEL_ID,
            }
        )


def test_exact_alias_map_rejects_hostile_containers_and_unbounded_input() -> None:
    class HostileDict(dict[str, str]):
        def __len__(self) -> int:
            raise AssertionError("hostile mapping must not be inspected")

    with pytest.raises(TypeError, match="exact dictionary"):
        exact_model_aliases(HostileDict({"sonnet": _EXACT_MODEL_ID}))
    with pytest.raises(ValueError, match="alias count"):
        exact_model_aliases(
            {f"model-{index}": f"backend-model-exact-{index}" for index in range(33)}
        )
    bounded = exact_model_aliases(
        {f"model-{index}": f"backend-model-exact-{index}" for index in range(32)}
    )
    assert bounded.resolve("model-31") == "backend-model-exact-31"


def test_exact_alias_map_requires_exact_builtin_strings_at_every_boundary() -> None:
    class Text(str):
        pass

    with pytest.raises(TypeError, match="exact text"):
        exact_model_aliases({Text("sonnet"): _EXACT_MODEL_ID})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="exact text"):
        exact_model_aliases({"sonnet": Text(_EXACT_MODEL_ID)})

    aliases = exact_model_aliases()
    with pytest.raises(TypeError, match="exact text"):
        aliases.resolve(Text("sonnet"))


@pytest.mark.parametrize(
    "aliases",
    [
        {},
        {1: _EXACT_MODEL_ID},  # type: ignore[dict-item]
        {"Sonnet": _EXACT_MODEL_ID},
        {"sonnet ": _EXACT_MODEL_ID},
        {"a" * 65: _EXACT_MODEL_ID},
    ],
)
def test_exact_alias_map_rejects_empty_or_noncanonical_aliases(
    aliases: dict[str, str],
) -> None:
    with pytest.raises((TypeError, ValueError), match="alias"):
        exact_model_aliases(aliases)


def test_semantic_probes_resolve_aliases_before_reporting_unavailable() -> None:
    aliases = exact_model_aliases()
    for runner in (
        run_session_probe,
        run_stream_probe,
        run_thinking_probe,
        run_usage_probe,
    ):
        with pytest.raises(ModelIdentityError, match="configured model alias"):
            runner(model_aliases=aliases, public_alias="unknown")
