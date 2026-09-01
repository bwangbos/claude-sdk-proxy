"""Fail-closed Task 6 attestation, binding, and custom transport proofs."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeSDKClient, SystemMessage

import claude_sdk_proxy._attestation_v2 as research
import claude_sdk_proxy.attestation as public
from claude_sdk_proxy.attestation import (
    ATTESTATION_MANIFEST_SCHEMA,
    AttestationAvailability,
    AttestationBinding,
    AttestationError,
    AttestationGate,
    AttestationManifest,
    AttestationReasonCode,
    AttestedSupervisorTransport,
    ChildAttestation,
    CliExecutableIdentity,
    ControlFDIdentity,
    FullSupervisorEvidence,
    RelayReceipt,
    SupervisorBootstrapDescriptors,
    SupervisorIdentityAckConfig,
    build_attested_sdk_client,
    current_attestation_availability,
    extract_child_attestation,
    prepare_supervisor_launch,
)
from claude_sdk_proxy.environment import (
    EnvironmentConfig,
    build_child_environment,
    environment_fingerprint,
)
from claude_sdk_proxy.isolation import IsolationConfig

pytestmark = pytest.mark.anyio

_HASH_A = "11" * 32
_HASH_B = "22" * 32
_HASH_C = "33" * 32
_HASH_D = "44" * 32
_HASH_E = "55" * 32


def _raw_sdk_init(**changes: object) -> SystemMessage:
    """SDK 0.2.148 parser semantics: data is the complete raw CLI object."""
    data: dict[str, object] = {
        "type": "system",
        "subtype": "init",
        "cwd": "/private/tmp/proxy-workdir",
        "session_id": "00000000-0000-4000-8000-000000000001",
        "tools": [],
        "mcp_servers": [],
        "model": "claude-pinned",
        "permissionMode": "default",
        "slash_commands": [],
        "apiKeySource": "none",
        "claude_code_version": "2.1.251",
        "output_style": "default",
        "agents": [],
        "skills": [],
    }
    data.update(changes)
    return SystemMessage(subtype=str(data.get("subtype", "")), data=data)


def _synthetic_evidence() -> SystemMessage:
    return SystemMessage(
        subtype="research_attestation",
        data={
            "schema": "claude_sdk_proxy.research_attestation",
            "version": 1,
            "auth_source": "existing_claude_login",
            "provider": "anthropic",
            "endpoint": "default",
            "preinput_model_bytes": 0,
            "preinput_network_bytes": 0,
        },
    )


def _write_executable(path: Path, body: str = "#!/bin/sh\nexit 75\n") -> None:
    path.write_text(body)
    path.chmod(0o700)


def _cli_identity(path: Path) -> CliExecutableIdentity:
    metadata = path.stat()
    return CliExecutableIdentity(
        version="2.1.251",
        path_sha256=hashlib.sha256(str(path.resolve()).encode()).hexdigest(),
        st_dev=metadata.st_dev,
        st_ino=metadata.st_ino,
        mode=metadata.st_mode,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _task5_evidence(*, network_proxy: bool) -> FullSupervisorEvidence:
    return FullSupervisorEvidence(
        control_trace=(
            "SUPERVISOR_IDENTITY",
            "IDENTITY_ACK",
            "ANCHOR_IDENTITY",
            "ANCHOR_ACK",
            "CLI_ARMED",
            "ARMED_ACK",
            "CLI_RUNNING",
        ),
        canonical_control_types=(
            "SUPERVISOR_IDENTITY",
            "ANCHOR_IDENTITY",
            "CLI_ARMED",
            "CLI_RUNNING",
        ),
        canonical_control_sequences=(1, 2, 3, 4),
        canonical_control_hashes=(_HASH_A, _HASH_B, _HASH_C, _HASH_D),
        supervisor_identity_sha256=_HASH_A,
        anchor_identity_sha256=_HASH_B,
        cli_armed_identity_sha256=_HASH_C,
        cli_running_identity_sha256=_HASH_E,
        canonical_head_certified=True,
        same_canonical_journal=True,
        evidence_observed_not_inferred=True,
        ack_after_durable_certification=True,
        exact_canonical_ack_heads=True,
        post_exec_identity_verified=True,
        cli_control_fd_closed_on_exec=True,
        external_control_fd_closed_on_cli_exec=True,
        internal_control_fd_closed_on_cli_exec=True,
        bootstrap_environment_removed=True,
        bootstrap_descriptor_names=(
            "LOCAL_PROXY_ALLOCATION_NONCE",
            "LOCAL_PROXY_INSTANCE_DIR",
            "LOCAL_PROXY_REAL_CLAUDE",
            "LOCAL_PROXY_CONTROL_FD",
        ),
        private_internal_relay_fd=True,
        network_proxy_selector_authenticated=True,
        identity_ack_config_version=1,
        identity_ack_bound_to_certified_head=True,
        identity_ack_reserved_zero=True,
        shared_proxy_fallback_channel=True,
        external_control_read_by_supervisor_while_live=True,
        anchor_external_read_count_while_supervisor_live=0,
        network_proxy_enabled=network_proxy,
    )


def _binding(*, allocation: str = _HASH_A, child: str = _HASH_E) -> AttestationBinding:
    return AttestationBinding(
        allocation_nonce=allocation,
        child_identity_sha256=child,
        supervisor_certified_sequence=4,
        supervisor_certified_hash=_HASH_D,
        connection_nonce=_HASH_B,
    )


def _config_and_manifest(
    tmp_path: Path,
    control_fd: int,
    *,
    network_proxy: bool = False,
) -> tuple[
    IsolationConfig,
    AttestationManifest,
    SupervisorBootstrapDescriptors,
    SupervisorIdentityAckConfig,
]:
    tmp_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    workdir = tmp_path / "workdir"
    workdir.mkdir(mode=0o700)
    supervisor = tmp_path / "claude-proxy-supervisor"
    _write_executable(supervisor)
    cli_dir = tmp_path / "cli-bin"
    cli_dir.mkdir()
    real_cli = cli_dir / "claude"
    _write_executable(real_cli)
    source = {
        "HOME": str(tmp_path / "home"),
        "USER": "local-user",
        "LOGNAME": "local-user",
        "HTTP_PROXY": "http://127.0.0.1:8181",
        "HTTPS_PROXY": "http://127.0.0.1:8182",
        "HOST_SECRET": "must-not-appear",
    }
    environment = build_child_environment(
        source,
        EnvironmentConfig(cli_dir=cli_dir, network_proxy=network_proxy),
    )
    config = IsolationConfig(
        model_id="claude-pinned",
        system_prompt="CANARY-SYSTEM",
        cwd=workdir,
        supervisor_path=supervisor,
        environment=environment,
    )
    evidence = _task5_evidence(network_proxy=network_proxy)
    manifest = AttestationManifest(
        schema=ATTESTATION_MANIFEST_SCHEMA,
        version=1,
        sdk_version="0.2.148",
        cli_version="2.1.251",
        cli_executable=_cli_identity(real_cli),
        environment_fingerprint=environment_fingerprint(environment),
        supervisor_evidence=evidence,
        availability=current_attestation_availability(),
    )
    descriptors = SupervisorBootstrapDescriptors(
        allocation_nonce=_HASH_A,
        instance_dir=tmp_path,
        real_cli=real_cli,
        control_fd=control_fd,
        control_identity=ControlFDIdentity.from_fd(control_fd),
    )
    ack = SupervisorIdentityAckConfig(
        network_proxy_enabled=network_proxy,
        certified_sequence=4,
        certified_hash=bytes.fromhex(_HASH_D),
    )
    return config, manifest, descriptors, ack


def test_current_availability_is_immutable_and_core_false() -> None:
    verdict = current_attestation_availability()

    assert verdict.core_gate_available is False
    assert verdict.reason_codes == (
        AttestationReasonCode.PUBLIC_AUTH_PROVENANCE_ABSENT_OR_UNVALIDATED,
        AttestationReasonCode.PREINPUT_NETWORK_BOUNDARY_UNPROVED,
        AttestationReasonCode.PER_TURN_FRESH_PROVENANCE_UNAVAILABLE,
    )
    with pytest.raises(FrozenInstanceError):
        verdict.core_gate_available = True  # type: ignore[misc]
    with pytest.raises(AttestationError):
        AttestationAvailability(core_gate_available=True, reason_codes=())


@pytest.mark.parametrize(
    "event",
    [
        _raw_sdk_init(),
        _raw_sdk_init(apiKeySource="user"),
        _raw_sdk_init(apiKeySource="environment"),
        _raw_sdk_init(apiKeySource="none"),
    ],
)
def test_actual_sdk_02148_raw_init_never_mints_attestation(
    event: SystemMessage,
) -> None:
    with pytest.raises(AttestationError, match="public auth provenance"):
        extract_child_attestation(event, current_attestation_availability())


def test_proxy_authored_envelope_is_not_accepted_as_cli_init() -> None:
    invented = SystemMessage(
        subtype="init",
        data={"schema": "claude_sdk_proxy.public_child_init", "evidence": []},
    )

    with pytest.raises(AttestationError, match="raw SDK init"):
        extract_child_attestation(invented, current_attestation_availability())


def test_child_attestation_has_no_ordinary_public_constructor() -> None:
    with pytest.raises(TypeError, match="cannot be constructed"):
        ChildAttestation()  # type: ignore[call-arg]


def test_research_factory_is_private_and_not_exported() -> None:
    assert "_test_only_mint_permit" not in public.__all__
    assert "_test_only_extract_synthetic_attestation" not in public.__all__
    assert not hasattr(public, "_test_only_mint_permit")
    assert not hasattr(public, "_test_only_extract_synthetic_attestation")


class _Boundary:
    def __init__(self) -> None:
        self.events: list[tuple[SystemMessage, object]] = []
        self.relayed: list[bytes] = []
        self.close_count = 0
        self.close_observed_closed: list[bool] = []
        self.gate: AttestationGate | None = None

    def revalidate(self) -> tuple[SystemMessage, object]:
        return self.events.pop(0)

    def relay(self, payload: bytes) -> RelayReceipt:
        self.relayed.append(payload)
        return RelayReceipt(model_bytes=len(payload), network_bytes=len(payload))

    def close(self) -> None:
        self.close_count += 1
        assert self.gate is not None
        self.close_observed_closed.append(self.gate.closed)


def _research_gate(
    boundary: _Boundary,
    binding: AttestationBinding | None = None,
) -> tuple[AttestationGate, AttestationBinding]:
    selected = binding or _binding()
    gate = AttestationGate(
        availability=research._test_only_available_verdict(),
        binding=selected,
        revalidate=boundary.revalidate,
        relay=boundary.relay,
        close_child=boundary.close,
    )
    boundary.gate = gate
    return gate, selected


def _attest_research_gate(
    gate: AttestationGate,
    binding: AttestationBinding,
    sequence: int = 1,
) -> None:
    event = _synthetic_evidence()
    permit = research._test_only_mint_permit(binding, sequence, event)
    gate.attest(
        research._test_only_extract_synthetic_attestation(event, permit),
        permit,
    )


def test_release_is_impossible_while_current_availability_false() -> None:
    boundary = _Boundary()
    binding = _binding()
    gate = AttestationGate(
        availability=current_attestation_availability(),
        binding=binding,
        revalidate=boundary.revalidate,
        relay=boundary.relay,
        close_child=boundary.close,
    )
    boundary.gate = gate
    turn = gate.queue_turn(system="S", blocks=[{"type": "text", "text": "U"}])

    with pytest.raises(AttestationError, match="core attestation gate unavailable"):
        gate.release(turn)

    assert gate.closed and gate.revoked
    assert gate.pending_turn_count == 0
    assert boundary.relayed == []
    assert boundary.close_observed_closed == [True]


def test_research_gate_requires_fresh_bound_permit_for_every_turn() -> None:
    boundary = _Boundary()
    gate, binding = _research_gate(boundary)
    _attest_research_gate(gate, binding)
    second = _synthetic_evidence()
    permit2 = research._test_only_mint_permit(binding, 2, second)
    boundary.events.append(
        (research._test_only_extract_synthetic_attestation(second, permit2), permit2)
    )
    turn = gate.queue_turn(system="S", blocks=[{"type": "text", "text": "U"}])

    gate.release(turn)

    assert len(boundary.relayed) == 1
    assert gate.last_evidence_sequence == 2
    assert gate.pending_turn_count == 0
    assert gate.queued_content_bytes == 0


@pytest.mark.parametrize("change", ["allocation", "child", "connection", "head"])
def test_cross_binding_permit_closes_before_relay(change: str) -> None:
    boundary = _Boundary()
    gate, binding = _research_gate(boundary)
    _attest_research_gate(gate, binding)
    values = {
        "allocation_nonce": binding.allocation_nonce,
        "child_identity_sha256": binding.child_identity_sha256,
        "supervisor_certified_sequence": binding.supervisor_certified_sequence,
        "supervisor_certified_hash": binding.supervisor_certified_hash,
        "connection_nonce": binding.connection_nonce,
    }
    field = {
        "allocation": "allocation_nonce",
        "child": "child_identity_sha256",
        "connection": "connection_nonce",
        "head": "supervisor_certified_hash",
    }[change]
    values[field] = _HASH_C
    wrong = AttestationBinding(**values)
    event = _synthetic_evidence()
    permit = research._test_only_mint_permit(wrong, 2, event)
    boundary.events.append(
        (research._test_only_extract_synthetic_attestation(event, permit), permit)
    )
    turn = gate.queue_turn(system="S", blocks=[])

    with pytest.raises(AttestationError, match="binding"):
        gate.release(turn)

    assert boundary.relayed == []
    assert gate.closed and gate.revoked
    assert boundary.close_observed_closed == [True]


def test_replayed_or_nonmonotonic_permit_closes_and_clears() -> None:
    boundary = _Boundary()
    gate, binding = _research_gate(boundary)
    event = _synthetic_evidence()
    permit1 = research._test_only_mint_permit(binding, 1, event)
    attestation = research._test_only_extract_synthetic_attestation(event, permit1)
    gate.attest(attestation, permit1)
    boundary.events.append((attestation, permit1))
    turn = gate.queue_turn(system="S", blocks=[])

    with pytest.raises(AttestationError, match="replay|sequence"):
        gate.release(turn)

    assert boundary.relayed == []
    assert gate.pending_turn_count == 0
    assert boundary.close_count == 1


def test_forged_handle_and_reentrant_release_are_forbidden() -> None:
    boundary = _Boundary()
    gate, binding = _research_gate(boundary)
    _attest_research_gate(gate, binding)
    issued = gate.queue_turn(system="S", blocks=[])
    forged = replace(issued)

    with pytest.raises(AttestationError, match="unknown"):
        gate.release(forged)

    assert boundary.relayed == []
    assert boundary.close_count == 1


def test_gate_lock_serializes_queue_and_close_without_content_retention() -> None:
    boundary = _Boundary()
    gate, _ = _research_gate(boundary)
    errors: list[BaseException] = []

    def queue() -> None:
        try:
            for _ in range(20):
                gate.queue_turn(system="S", blocks=[{"type": "text", "text": "U"}])
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=queue)
    worker.start()
    gate.close()
    worker.join()

    assert gate.closed and gate.revoked
    assert gate.pending_turn_count == 0
    assert gate.queued_content_bytes == 0
    assert boundary.close_count == 1
    assert all(isinstance(error, AttestationError) for error in errors)


def test_concurrent_release_fails_before_either_relay_callback() -> None:
    boundary = _Boundary()
    binding = _binding()
    entered = threading.Event()
    proceed = threading.Event()
    errors: list[BaseException] = []
    evidence = _synthetic_evidence()
    permit2 = research._test_only_mint_permit(binding, 2, evidence)
    attestation2 = research._test_only_extract_synthetic_attestation(evidence, permit2)

    def revalidate() -> tuple[ChildAttestation, object]:
        entered.set()
        assert proceed.wait(timeout=2)
        return attestation2, permit2

    gate = AttestationGate(
        availability=research._test_only_available_verdict(),
        binding=binding,
        revalidate=revalidate,
        relay=boundary.relay,
        close_child=boundary.close,
    )
    boundary.gate = gate
    _attest_research_gate(gate, binding)
    first = gate.queue_turn(system="S", blocks=[])
    second = gate.queue_turn(system="S", blocks=[])

    def release(turn: object) -> None:
        try:
            gate.release(turn)  # type: ignore[arg-type]
        except BaseException as error:
            errors.append(error)

    first_worker = threading.Thread(target=release, args=(first,))
    second_worker = threading.Thread(target=release, args=(second,))
    first_worker.start()
    assert entered.wait(timeout=2)
    second_worker.start()
    second_worker.join(timeout=2)
    proceed.set()
    first_worker.join(timeout=2)

    assert not first_worker.is_alive() and not second_worker.is_alive()
    assert len(errors) == 2
    assert all(isinstance(error, AttestationError) for error in errors)
    assert boundary.relayed == []
    assert boundary.close_count == 1
    assert gate.closed and gate.revoked
    assert gate.pending_turn_count == 0
    assert gate.queued_content_bytes == 0


def test_full_task5_evidence_rejects_every_reduced_projection() -> None:
    evidence = _task5_evidence(network_proxy=False)
    for field in (
        "canonical_head_certified",
        "same_canonical_journal",
        "evidence_observed_not_inferred",
        "ack_after_durable_certification",
        "post_exec_identity_verified",
        "external_control_fd_closed_on_cli_exec",
        "identity_ack_bound_to_certified_head",
        "shared_proxy_fallback_channel",
        "external_control_read_by_supervisor_while_live",
    ):
        with pytest.raises(AttestationError, match="full Task 5"):
            replace(evidence, **{field: False})


def test_control_fd_must_be_an_open_socket() -> None:
    read_fd, write_fd = os.pipe()
    try:
        with pytest.raises(AttestationError, match="socket"):
            ControlFDIdentity.from_fd(read_fd)
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_launcher_validates_cli_control_fd_trace_and_network_policy(
    tmp_path: Path,
) -> None:
    left, right = socket.socketpair()
    try:
        config, manifest, descriptors, ack = _config_and_manifest(
            tmp_path, right.fileno()
        )
        launch = prepare_supervisor_launch(config, descriptors, ack, manifest)

        assert launch.inherited_fds == (right.fileno(),)
        assert launch.bootstrap_descriptor_names == (
            "LOCAL_PROXY_ALLOCATION_NONCE",
            "LOCAL_PROXY_INSTANCE_DIR",
            "LOCAL_PROXY_REAL_CLAUDE",
            "LOCAL_PROXY_CONTROL_FD",
        )
        assert launch.identity_ack_payload == ack.payload()
        assert launch.supervisor_internal_relay_fd == 198
        assert launch.cli_identity == manifest.cli_executable
        assert launch.full_supervisor_evidence == manifest.supervisor_evidence
        assert set(launch.supervisor_environment) == {
            *config.environment,
            *launch.bootstrap_descriptor_names,
        }
    finally:
        left.close()
        right.close()


def test_launcher_rejects_cli_mutation_fd_reuse_and_proxy_bit_drift(
    tmp_path: Path,
) -> None:
    left, right = socket.socketpair()
    try:
        config, manifest, descriptors, ack = _config_and_manifest(
            tmp_path, right.fileno()
        )
        descriptors.real_cli.write_text("#!/bin/sh\nexit 0\n")
        with pytest.raises(AttestationError, match="CLI executable"):
            prepare_supervisor_launch(config, descriptors, ack, manifest)
    finally:
        left.close()
        right.close()

    left, right = socket.socketpair()
    try:
        config, manifest, descriptors, ack = _config_and_manifest(
            tmp_path / "second", right.fileno()
        )
        stale = descriptors
        right.close()
        replacement_left, replacement_right = socket.socketpair()
        owns_stale = stale.control_fd in {
            replacement_left.fileno(),
            replacement_right.fileno(),
        }
        try:
            if not owns_stale:
                os.dup2(replacement_right.fileno(), stale.control_fd)
            with pytest.raises(AttestationError, match="control FD identity"):
                prepare_supervisor_launch(config, stale, ack, manifest)
        finally:
            if not owns_stale:
                os.close(stale.control_fd)
            replacement_left.close()
            replacement_right.close()
    finally:
        left.close()
        right.close()

    left, right = socket.socketpair()
    try:
        config, manifest, descriptors, _ = _config_and_manifest(
            tmp_path / "third", right.fileno(), network_proxy=False
        )
        wrong = SupervisorIdentityAckConfig(
            network_proxy_enabled=True,
            certified_sequence=4,
            certified_hash=bytes.fromhex(_HASH_D),
        )
        with pytest.raises(AttestationError, match="network proxy"):
            prepare_supervisor_launch(config, descriptors, wrong, manifest)
    finally:
        left.close()
        right.close()


def test_network_enabled_launch_requires_exact_proxy_values_and_fingerprint(
    tmp_path: Path,
) -> None:
    left, right = socket.socketpair()
    try:
        config, manifest, descriptors, ack = _config_and_manifest(
            tmp_path, right.fileno(), network_proxy=True
        )
        launch = prepare_supervisor_launch(config, descriptors, ack, manifest)
        assert launch.network_proxy_items == (
            ("HTTPS_PROXY", "http://127.0.0.1:8182"),
            ("HTTP_PROXY", "http://127.0.0.1:8181"),
        )

        altered = replace(
            config,
            environment={**config.environment, "HTTP_PROXY": "http://wrong"},
        )
        with pytest.raises(AttestationError, match="fingerprint"):
            prepare_supervisor_launch(altered, descriptors, ack, manifest)
    finally:
        left.close()
        right.close()


def _transport_supervisor_script(path: Path) -> None:
    _write_executable(
        path,
        """#!/bin/sh
set -eu
[ -e "/dev/fd/$LOCAL_PROXY_CONTROL_FD" ]
[ "${HOST_SECRET+x}" = "" ]
printf '%s\\n' "$@" > "$LOCAL_PROXY_INSTANCE_DIR/argv.txt"
printf '%s\\n' \
  LOCAL_PROXY_ALLOCATION_NONCE \
  LOCAL_PROXY_INSTANCE_DIR \
  LOCAL_PROXY_REAL_CLAUDE \
  LOCAL_PROXY_CONTROL_FD > "$LOCAL_PROXY_INSTANCE_DIR/bootstrap-names.txt"
while IFS= read -r line; do
  printf '%s\\n' "$line" >> "$LOCAL_PROXY_INSTANCE_DIR/stdin.txt"
done
""",
    )


async def test_custom_transport_uses_real_pipes_exact_env_and_pass_fds(
    tmp_path: Path,
) -> None:
    left, right = socket.socketpair()
    try:
        config, manifest, descriptors, ack = _config_and_manifest(
            tmp_path, right.fileno()
        )
        _transport_supervisor_script(config.supervisor_path)
        launch = prepare_supervisor_launch(config, descriptors, ack, manifest)
        transport = AttestedSupervisorTransport(launch)
        client = build_attested_sdk_client(launch, transport=transport)

        assert isinstance(client, ClaudeSDKClient)
        assert client.options.cli_path == config.supervisor_path
        await transport.connect()
        await transport.write(
            json.dumps(
                {
                    "type": "control_request",
                    "request_id": "malicious",
                    "request": {
                        "subtype": "initialize",
                        "hooks": None,
                        "agents": {"caller": {"prompt": "CANARY-AGENT"}},
                    },
                }
            )
            + "\n"
        )
        initialize = (
            json.dumps(
                {
                    "type": "control_request",
                    "request_id": "1",
                    "request": {"subtype": "initialize", "hooks": None, "skills": []},
                }
            )
            + "\n"
        )
        await transport.write(initialize)
        await transport.write(
            json.dumps(
                {
                    "type": "user",
                    "message": {"role": "user", "content": "CANARY-USER"},
                }
            )
            + "\n"
        )
        await transport.end_input()
        await transport.close()

        assert (tmp_path / "stdin.txt").read_text().splitlines() == [
            initialize.rstrip("\n")
        ]
        assert transport.buffered_user_write_count == 0
        assert transport.discarded_user_write_count == 2
        assert (tmp_path / "bootstrap-names.txt").read_text().splitlines() == list(
            launch.bootstrap_descriptor_names
        )
        argv = (tmp_path / "argv.txt").read_text().splitlines()
        assert argv[0:3] == ["--output-format", "stream-json", "--verbose"]
        assert "CANARY-SYSTEM" in argv
        assert "claude-pinned" in argv
    finally:
        left.close()
        right.close()


async def test_default_sdk_transport_is_rejected_and_false_gate_cannot_flush(
    tmp_path: Path,
) -> None:
    left, right = socket.socketpair()
    try:
        config, manifest, descriptors, ack = _config_and_manifest(
            tmp_path, right.fileno()
        )
        launch = prepare_supervisor_launch(config, descriptors, ack, manifest)
        with pytest.raises(AttestationError, match="custom Task 6 transport"):
            build_attested_sdk_client(launch)

        transport = AttestedSupervisorTransport(launch)
        with pytest.raises(AttestationError, match="core attestation gate unavailable"):
            await transport.release_buffered(None)
    finally:
        left.close()
        right.close()
