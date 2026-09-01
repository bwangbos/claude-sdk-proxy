"""Fail-closed public child-attestation and pre-input gate proofs."""

from __future__ import annotations

import copy
import json
import struct
from dataclasses import replace
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeSDKClient, SystemMessage

from claude_sdk_proxy.attestation import (
    AttestationError,
    AttestationGate,
    AttestationManifest,
    CliExecutableIdentity,
    QueuedTurn,
    RelayReceipt,
    SupervisorBootstrapDescriptors,
    SupervisorIdentityAckConfig,
    SupervisorTrace,
    extract_child_attestation,
    prepare_supervisor_launch,
)
from claude_sdk_proxy.isolation import IsolationConfig

_MANIFEST_SCHEMA = "claude_sdk_proxy.child_attestation_manifest"
_EVENT_SCHEMA = "claude_sdk_proxy.public_child_init"
_PATH_SHA256 = "11" * 32
_CLI_SHA256 = "22" * 32
_ENVIRONMENT_SHA256 = "33" * 32
_REQUIRED_FIELDS = (
    "sdk_version",
    "cli_version",
    "cli_executable",
    "environment_fingerprint",
    "provider",
    "endpoint",
    "auth_source",
    "supervisor_trace",
    "preinput_boundary",
)


def _manifest() -> AttestationManifest:
    return AttestationManifest(
        schema=_MANIFEST_SCHEMA,
        version=1,
        sdk_version="0.2.148",
        cli_version="2.1.251",
        cli_executable=CliExecutableIdentity(
            path_sha256=_PATH_SHA256,
            st_dev=123,
            st_ino=456,
            mode=0o100700,
            sha256=_CLI_SHA256,
        ),
        environment_fingerprint=_ENVIRONMENT_SHA256,
        provider="anthropic",
        endpoint="default",
        auth_source="existing_claude_login",
        supervisor_trace=SupervisorTrace(
            trace=(
                "SUPERVISOR_IDENTITY",
                "IDENTITY_ACK",
                "ANCHOR_IDENTITY",
                "ANCHOR_ACK",
                "CLI_ARMED",
                "ARMED_ACK",
                "CLI_RUNNING",
            ),
            canonical_types=(
                "SUPERVISOR_IDENTITY",
                "ANCHOR_IDENTITY",
                "CLI_ARMED",
                "CLI_RUNNING",
            ),
            canonical_sequences=(1, 2, 3, 4),
            ack_after_durable_certification=True,
            exact_canonical_ack_heads=True,
            post_exec_identity_verified=True,
            cli_control_fd_closed_on_exec=True,
            bootstrap_environment_removed=True,
        ),
    )


def _evidence_values() -> dict[str, object]:
    return {
        "sdk_version": "0.2.148",
        "cli_version": "2.1.251",
        "cli_executable": {
            "path_sha256": _PATH_SHA256,
            "st_dev": 123,
            "st_ino": 456,
            "mode": 0o100700,
            "sha256": _CLI_SHA256,
        },
        "environment_fingerprint": {
            "algorithm": "sha256-name-nul-value-nul-v1",
            "sha256": _ENVIRONMENT_SHA256,
        },
        "provider": "anthropic",
        "endpoint": "default",
        "auth_source": "existing_claude_login",
        "supervisor_trace": {
            "schema": "claude_sdk_proxy.supervisor_trace",
            "version": 1,
            "trace": [
                "SUPERVISOR_IDENTITY",
                "IDENTITY_ACK",
                "ANCHOR_IDENTITY",
                "ANCHOR_ACK",
                "CLI_ARMED",
                "ARMED_ACK",
                "CLI_RUNNING",
            ],
            "canonical_types": [
                "SUPERVISOR_IDENTITY",
                "ANCHOR_IDENTITY",
                "CLI_ARMED",
                "CLI_RUNNING",
            ],
            "canonical_sequences": [1, 2, 3, 4],
            "ack_after_durable_certification": True,
            "exact_canonical_ack_heads": True,
            "post_exec_identity_verified": True,
            "cli_control_fd_closed_on_exec": True,
            "bootstrap_environment_removed": True,
        },
        "preinput_boundary": {
            "model_bytes": 0,
            "network_bytes": 0,
        },
    }


def _init_event(
    *,
    overrides: dict[str, object] | None = None,
    omit: str | None = None,
    duplicate: str | None = None,
    unknown_field: bool = False,
    extra_top_level: bool = False,
) -> SystemMessage:
    values = _evidence_values()
    values.update(overrides or {})
    evidence = [
        {"field": name, "value": values[name]}
        for name in _REQUIRED_FIELDS
        if name != omit
    ]
    if duplicate is not None:
        evidence.append({"field": duplicate, "value": values[duplicate]})
    if unknown_field:
        evidence.append({"field": "future_auth_hint", "value": "unknown"})
    data: dict[str, object] = {
        "schema": _EVENT_SCHEMA,
        "version": 1,
        "evidence": evidence,
    }
    if extra_top_level:
        data["future"] = True
    return SystemMessage(subtype="init", data=data)


class _RecordingBoundary:
    def __init__(self, revalidations: list[SystemMessage]) -> None:
        self._revalidations = iter(revalidations)
        self.revalidation_count = 0
        self.relayed: list[bytes] = []
        self.close_count = 0

    def revalidate(self) -> SystemMessage:
        self.revalidation_count += 1
        return next(self._revalidations)

    def relay(self, serialized_turn: bytes) -> RelayReceipt:
        self.relayed.append(serialized_turn)
        return RelayReceipt(
            model_bytes=len(serialized_turn),
            network_bytes=len(serialized_turn),
        )

    def close(self) -> None:
        self.close_count += 1


def _gate(boundary: _RecordingBoundary) -> AttestationGate:
    return AttestationGate(
        _manifest(),
        revalidate=boundary.revalidate,
        relay=boundary.relay,
        close_child=boundary.close,
    )


def test_exact_existing_login_init_event_returns_redacted_attestation() -> None:
    """A wrong accepted field or a raw executable value must change this result."""
    attestation = extract_child_attestation(_init_event(), _manifest())

    assert attestation.version == 1
    assert attestation.field_names == _REQUIRED_FIELDS
    assert attestation.auth_source == "existing_claude_login"
    assert attestation.provider == "anthropic"
    assert attestation.endpoint == "default"
    assert attestation.environment_fingerprint == _ENVIRONMENT_SHA256
    assert len(attestation.executable_identity_sha256) == 64
    assert len(attestation.supervisor_trace_sha256) == 64
    assert len(attestation.evidence_sha256) == 64
    assert not hasattr(attestation, "cli_executable")
    assert str(123) not in repr(attestation)
    assert str(456) not in repr(attestation)


@pytest.mark.parametrize(
    "auth_source",
    [
        "api_key",
        "api_key_helper",
        "oauth",
        "bedrock",
        "vertex",
        "unknown",
        "",
    ],
)
def test_non_existing_login_auth_source_never_attests(auth_source: str) -> None:
    """Replacing the positive source enum must keep the child gate closed."""
    event = _init_event(overrides={"auth_source": auth_source})

    with pytest.raises(AttestationError, match="existing_claude_login"):
        extract_child_attestation(event, _manifest())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "bedrock"),
        ("provider", "vertex"),
        ("provider", "cloud"),
        ("provider", "unknown"),
        ("endpoint", "https://custom.invalid"),
        ("endpoint", "proxy"),
        ("endpoint", "unknown"),
    ],
)
def test_cloud_provider_or_custom_endpoint_never_attests(
    field: str, value: str
) -> None:
    """Changing provider or endpoint selection must fail exact comparison."""
    with pytest.raises(AttestationError, match=field):
        extract_child_attestation(
            _init_event(overrides={field: value}),
            _manifest(),
        )


@pytest.mark.parametrize("field", _REQUIRED_FIELDS)
def test_every_missing_evidence_field_rejects(field: str) -> None:
    """Deleting any required record must make the evidence incomplete."""
    with pytest.raises(AttestationError, match="missing"):
        extract_child_attestation(_init_event(omit=field), _manifest())


@pytest.mark.parametrize("field", _REQUIRED_FIELDS)
def test_every_duplicate_evidence_field_rejects(field: str) -> None:
    """Repeating any record must make its provenance ambiguous."""
    with pytest.raises(AttestationError, match="duplicate"):
        extract_child_attestation(_init_event(duplicate=field), _manifest())


def test_unknown_evidence_or_schema_keys_reject() -> None:
    """Adding an unversioned hint must not silently expand the accepted schema."""
    with pytest.raises(AttestationError, match="unknown evidence"):
        extract_child_attestation(_init_event(unknown_field=True), _manifest())
    with pytest.raises(AttestationError, match="unknown init"):
        extract_child_attestation(_init_event(extra_top_level=True), _manifest())


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("sdk_version", "0.2.149", "SDK"),
        ("cli_version", "2.1.252", "CLI"),
        (
            "environment_fingerprint",
            {"algorithm": "sha256", "sha256": _ENVIRONMENT_SHA256},
            "environment",
        ),
        ("preinput_boundary", {"model_bytes": 1, "network_bytes": 0}, "pre-input"),
        ("preinput_boundary", {"model_bytes": 0, "network_bytes": 1}, "pre-input"),
    ],
)
def test_runtime_or_preinput_boundary_drift_rejects(
    field: str, value: object, message: str
) -> None:
    """Changing a pinned runtime or byte counter must invalidate attestation."""
    with pytest.raises(AttestationError, match=message):
        extract_child_attestation(
            _init_event(overrides={field: value}),
            _manifest(),
        )


def test_executable_and_supervisor_trace_drift_reject() -> None:
    """A different inode or canonical control sequence must not attest."""
    executable = copy.deepcopy(_evidence_values()["cli_executable"])
    assert isinstance(executable, dict)
    executable["st_ino"] = 999
    with pytest.raises(AttestationError, match="executable"):
        extract_child_attestation(
            _init_event(overrides={"cli_executable": executable}),
            _manifest(),
        )

    trace = copy.deepcopy(_evidence_values()["supervisor_trace"])
    assert isinstance(trace, dict)
    trace["canonical_sequences"] = [1, 2, 3, 5]
    with pytest.raises(AttestationError, match="supervisor"):
        extract_child_attestation(
            _init_event(overrides={"supervisor_trace": trace}),
            _manifest(),
        )


def test_event_subtype_shape_and_nested_unknown_keys_reject() -> None:
    """A non-init message or a widened nested map must fail closed."""
    wrong_subtype = _init_event()
    wrong_subtype.subtype = "status"
    with pytest.raises(AttestationError, match="subtype"):
        extract_child_attestation(wrong_subtype, _manifest())

    executable = copy.deepcopy(_evidence_values()["cli_executable"])
    assert isinstance(executable, dict)
    executable["credential_path"] = "/not/read"
    with pytest.raises(AttestationError, match="executable schema"):
        extract_child_attestation(
            _init_event(overrides={"cli_executable": executable}),
            _manifest(),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", "future"),
        ("version", 2),
        ("sdk_version", "0.2.149"),
        ("cli_version", "2.1.252"),
        ("provider", "bedrock"),
        ("endpoint", "custom"),
        ("auth_source", "api_key"),
    ],
)
def test_manifest_value_accepts_only_the_task6_v1_tuple(
    field: str, value: object
) -> None:
    """Task 10 cannot construct a broader manifest without a schema change."""
    with pytest.raises((AttestationError, TypeError, ValueError)):
        replace(_manifest(), **{field: value})


def test_queued_caller_content_releases_zero_bytes_before_attestation() -> None:
    """Removing the attest gate would increment one of the boundary counters."""
    boundary = _RecordingBoundary([])
    gate = _gate(boundary)

    gate.queue_turn(
        system="CANARY-SYSTEM",
        blocks=[
            {"type": "text", "text": "CANARY-USER"},
            {"type": "tool_result", "content": "CANARY-TOOL"},
        ],
        mcp_schema={"name": "CANARY-MCP"},
    )

    assert gate.relayed_model_bytes == 0
    assert gate.relayed_network_bytes == 0
    assert gate.pending_turn_count == 1
    assert gate.queued_content_bytes > 0
    assert boundary.relayed == []


def test_release_without_attestation_closes_and_revokes_all_content() -> None:
    """Bypassing initial attestation must close the child before relay."""
    boundary = _RecordingBoundary([])
    gate = _gate(boundary)
    first = gate.queue_turn(system="SYSTEM", blocks=[{"type": "text", "text": "A"}])
    gate.queue_turn(system="SYSTEM", blocks=[{"type": "text", "text": "B"}])

    with pytest.raises(AttestationError, match="not attested"):
        gate.release(first)

    assert gate.closed
    assert gate.revoked
    assert gate.pending_turn_count == 0
    assert gate.queued_content_bytes == 0
    assert gate.relayed_model_bytes == 0
    assert gate.relayed_network_bytes == 0
    assert boundary.relayed == []
    assert boundary.close_count == 1


def test_forged_turn_handle_closes_without_relay() -> None:
    """A guessed sequence must not become a capability for queued content."""
    boundary = _RecordingBoundary([])
    gate = _gate(boundary)
    gate.attest(_init_event())
    issued = gate.queue_turn(system="SYSTEM", blocks=[{"type": "text", "text": "A"}])
    forged = QueuedTurn(sequence=issued.sequence)

    with pytest.raises(AttestationError, match="unknown"):
        gate.release(forged)

    assert boundary.revalidation_count == 0
    assert boundary.relayed == []
    assert boundary.close_count == 1
    assert gate.closed and gate.revoked
    assert gate.pending_turn_count == 0
    assert gate.queued_content_bytes == 0


def test_release_revalidates_immediately_and_drops_the_queued_reference() -> None:
    """Deleting per-turn revalidation must make this test observe zero checks."""
    boundary = _RecordingBoundary([_init_event()])
    gate = _gate(boundary)
    gate.attest(_init_event())
    turn = gate.queue_turn(
        system="SYSTEM",
        blocks=[{"type": "text", "text": "USER"}],
    )

    gate.release(turn)

    assert boundary.revalidation_count == 1
    assert len(boundary.relayed) == 1
    assert gate.relayed_model_bytes == len(boundary.relayed[0])
    assert gate.relayed_network_bytes == len(boundary.relayed[0])
    assert gate.pending_turn_count == 0
    assert gate.queued_content_bytes == 0
    assert not gate.closed


def test_profile_change_between_connection_and_turn_closes_before_relay() -> None:
    """Accepting a changed non-secret source would call the relay in this test."""
    changed = _init_event(overrides={"auth_source": "api_key"})
    boundary = _RecordingBoundary([changed])
    gate = _gate(boundary)
    gate.attest(_init_event())
    first = gate.queue_turn(system="SYSTEM", blocks=[{"type": "text", "text": "A"}])
    gate.queue_turn(system="SYSTEM", blocks=[{"type": "text", "text": "B"}])

    with pytest.raises(AttestationError, match="existing_claude_login"):
        gate.release(first)

    assert boundary.revalidation_count == 1
    assert boundary.relayed == []
    assert boundary.close_count == 1
    assert gate.closed and gate.revoked
    assert gate.pending_turn_count == 0
    assert gate.queued_content_bytes == 0
    assert gate.relayed_model_bytes == 0
    assert gate.relayed_network_bytes == 0


def test_every_released_turn_gets_a_fresh_public_revalidation() -> None:
    """Caching the connection event must undercount this two-turn sequence."""
    boundary = _RecordingBoundary([_init_event(), _init_event()])
    gate = _gate(boundary)
    gate.attest(_init_event())

    first = gate.queue_turn(system="S", blocks=[{"type": "text", "text": "one"}])
    gate.release(first)
    second = gate.queue_turn(system="S", blocks=[{"type": "text", "text": "two"}])
    gate.release(second)

    assert boundary.revalidation_count == 2
    assert len(boundary.relayed) == 2


def test_explicit_revalidation_checks_provenance_without_relaying_content() -> None:
    """The actor-facing revalidation method must not itself release a turn."""
    boundary = _RecordingBoundary([_init_event()])
    gate = _gate(boundary)
    gate.attest(_init_event())

    gate.revalidate()

    assert boundary.revalidation_count == 1
    assert boundary.relayed == []
    assert gate.relayed_model_bytes == 0
    assert gate.relayed_network_bytes == 0
    assert not gate.closed


def test_queue_serializes_an_immutable_local_snapshot() -> None:
    """Mutating a caller block after queueing must not change the relayed turn."""
    boundary = _RecordingBoundary([_init_event()])
    gate = _gate(boundary)
    gate.attest(_init_event())
    block = {"type": "text", "text": "ORIGINAL"}
    turn = gate.queue_turn(system="S", blocks=[block])
    block["text"] = "MUTATED"

    gate.release(turn)

    payload = json.loads(boundary.relayed[0])
    assert payload["blocks"][0]["text"] == "ORIGINAL"


def test_invalid_initial_event_closes_and_clears_pending_content() -> None:
    """An initial API-key event must revoke all queued content without relay."""
    boundary = _RecordingBoundary([])
    gate = _gate(boundary)
    gate.queue_turn(system="S", blocks=[{"type": "text", "text": "U"}])

    with pytest.raises(AttestationError, match="existing_claude_login"):
        gate.attest(_init_event(overrides={"auth_source": "api_key"}))

    assert gate.closed and gate.revoked
    assert gate.pending_turn_count == 0
    assert gate.queued_content_bytes == 0
    assert boundary.close_count == 1
    assert boundary.relayed == []


def _isolation_config(tmp_path: Path) -> IsolationConfig:
    workdir = tmp_path / "workdir"
    workdir.mkdir(mode=0o700)
    workdir.chmod(0o700)
    supervisor = tmp_path / "claude-proxy-supervisor"
    supervisor.write_text("#!/bin/sh\nexit 75\n")
    supervisor.chmod(0o700)
    return IsolationConfig(
        model_id="claude-exact",
        system_prompt="CANARY-SYSTEM",
        cwd=workdir,
        supervisor_path=supervisor,
        environment={
            "PATH": "/verified/claude:/usr/bin:/bin",
            "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
        },
    )


def _bootstrap(tmp_path: Path) -> SupervisorBootstrapDescriptors:
    real_cli = tmp_path / "claude"
    real_cli.write_text("#!/bin/sh\nexit 75\n")
    real_cli.chmod(0o700)
    instance = tmp_path / "instance"
    instance.mkdir(mode=0o700)
    return SupervisorBootstrapDescriptors(
        allocation_nonce="ab" * 32,
        instance_dir=instance,
        real_cli=real_cli,
        control_fd=41,
    )


def test_launch_adapter_selects_supervisor_and_exact_task5_descriptors(
    tmp_path: Path,
) -> None:
    """Omitting, adding, or renaming a Task 5 bootstrap descriptor must fail."""
    config = _isolation_config(tmp_path)
    descriptors = _bootstrap(tmp_path)

    launch = prepare_supervisor_launch(
        config,
        descriptors,
        SupervisorIdentityAckConfig(network_proxy_enabled=False),
    )

    assert launch.options.cli_path == config.supervisor_path
    assert launch.bootstrap_descriptor_names == (
        "LOCAL_PROXY_ALLOCATION_NONCE",
        "LOCAL_PROXY_INSTANCE_DIR",
        "LOCAL_PROXY_REAL_CLAUDE",
        "LOCAL_PROXY_CONTROL_FD",
    )
    assert dict(launch.supervisor_environment) == {
        **dict(config.environment),
        "LOCAL_PROXY_ALLOCATION_NONCE": "ab" * 32,
        "LOCAL_PROXY_INSTANCE_DIR": str(descriptors.instance_dir),
        "LOCAL_PROXY_REAL_CLAUDE": str(descriptors.real_cli),
        "LOCAL_PROXY_CONTROL_FD": "41",
    }
    assert launch.options.env == dict(launch.supervisor_environment)
    assert launch.inherited_fds == (41,)
    assert launch.supervisor_internal_relay_fd == 198
    assert launch.identity_ack_config.network_proxy_enabled is False
    payload = launch.identity_ack_config.payload(3, bytes.fromhex("44" * 32))
    assert len(payload) == 48
    assert struct.unpack("<HBBIQ32s", payload) == (
        1,
        0,
        0,
        0,
        3,
        bytes.fromhex("44" * 32),
    )
    client = ClaudeSDKClient(options=launch.options)
    assert client.options.cli_path == config.supervisor_path
    assert client.options.env == dict(launch.supervisor_environment)


def test_launch_adapter_keeps_bootstrap_descriptors_out_of_real_cli_env(
    tmp_path: Path,
) -> None:
    """Forwarding supervisor-only descriptors to the CLI must change this map."""
    config = _isolation_config(tmp_path)
    launch = prepare_supervisor_launch(
        config,
        _bootstrap(tmp_path),
        SupervisorIdentityAckConfig(network_proxy_enabled=True),
    )

    assert dict(launch.real_cli_environment) == dict(config.environment)
    assert set(launch.bootstrap_descriptor_names).isdisjoint(
        launch.real_cli_environment
    )
    assert launch.pre_attestation_model_bytes == 0
    assert launch.pre_attestation_network_bytes == 0
    assert launch.local_option_serialization_only
    assert launch.identity_ack_config.network_proxy_enabled
    assert "LOCAL_PROXY_NETWORK_PROXY" not in launch.supervisor_environment


def test_launch_adapter_rejects_ambient_network_selector(tmp_path: Path) -> None:
    """Network intent belongs only to the authenticated IDENTITY_ACK payload."""
    config = _isolation_config(tmp_path)
    config = replace(
        config,
        environment={
            **dict(config.environment),
            "LOCAL_PROXY_NETWORK_PROXY": "1",
        },
    )

    with pytest.raises(AttestationError, match="network proxy"):
        prepare_supervisor_launch(
            config,
            _bootstrap(tmp_path),
            SupervisorIdentityAckConfig(network_proxy_enabled=True),
        )


@pytest.mark.parametrize(
    "change",
    [
        {"allocation_nonce": "not-hex"},
        {"instance_dir": Path("relative")},
        {"real_cli": Path("relative")},
        {"control_fd": -1},
    ],
)
def test_invalid_bootstrap_descriptors_fail_before_launch(
    tmp_path: Path, change: dict[str, object]
) -> None:
    """An ambiguous descriptor set must never produce a launch plan."""
    values: dict[str, object] = {
        "allocation_nonce": "ab" * 32,
        "instance_dir": tmp_path,
        "real_cli": tmp_path / "claude",
        "control_fd": 41,
    }
    values.update(change)
    with pytest.raises((AttestationError, TypeError, ValueError)):
        SupervisorBootstrapDescriptors(**values)  # type: ignore[arg-type]


def test_identity_ack_config_rejects_non_boolean_network_selector() -> None:
    """The authenticated selector must not accept truthy substitutes."""
    with pytest.raises((AttestationError, TypeError, ValueError)):
        SupervisorIdentityAckConfig(network_proxy_enabled=1)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "change",
    [
        {"version": True},
        {"version": 2},
        {"reserved_byte": False},
        {"reserved_byte": 1},
        {"reserved_word": False},
        {"reserved_word": 1},
    ],
)
def test_identity_ack_config_rejects_noncanonical_header_fields(
    change: dict[str, object],
) -> None:
    """Only the exact v1 zero-reserved authenticated config is valid."""
    with pytest.raises(AttestationError):
        SupervisorIdentityAckConfig(
            network_proxy_enabled=False,
            **change,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("sequence", "certified_hash"),
    [
        (0, bytes.fromhex("44" * 32)),
        (-1, bytes.fromhex("44" * 32)),
        (True, bytes.fromhex("44" * 32)),
        (1 << 64, bytes.fromhex("44" * 32)),
        (1, bytes(32)),
        (1, bytes.fromhex("44" * 31)),
        (1, bytearray.fromhex("44" * 32)),
    ],
)
def test_identity_ack_config_rejects_ambiguous_certified_head(
    sequence: int,
    certified_hash: bytes,
) -> None:
    """Malformed or unbound supervisor heads must not produce wire bytes."""
    config = SupervisorIdentityAckConfig(network_proxy_enabled=False)

    with pytest.raises(AttestationError):
        config.payload(sequence, certified_hash)


def test_queued_turn_handle_contains_no_caller_content() -> None:
    """The public handle must stay opaque if pending storage is later revoked."""
    annotations = QueuedTurn.__annotations__
    assert tuple(annotations) == ("sequence",)

    with pytest.raises(AttestationError):
        QueuedTurn(sequence=True)
