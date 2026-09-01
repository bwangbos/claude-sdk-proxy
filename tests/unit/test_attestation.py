"""Fail-closed Task 6 provenance, launch, and transport proofs."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import pickle
import stat
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, dataclass, replace
from pathlib import Path
from typing import Any

import anyio
import pytest
from claude_agent_sdk import ClaudeSDKClient, SystemMessage

import claude_sdk_proxy._attestation_v2 as implementation
import claude_sdk_proxy.attestation as public
from claude_sdk_proxy.attestation import (
    ATTESTATION_MANIFEST_SCHEMA,
    AttestationAvailability,
    AttestationError,
    AttestationGate,
    AttestationManifest,
    AttestationReasonCode,
    AttestedSupervisorTransport,
    ChildAttestation,
    CliExecutableIdentity,
    PreparedSupervisorLaunch,
    SupervisorBootstrapDescriptors,
    build_attested_sdk_client,
    current_attestation_availability,
    extract_child_attestation,
    prepare_supervisor_launch,
)
from claude_sdk_proxy.environment import (
    EnvironmentAmbiguityError,
    EnvironmentConfig,
    build_child_environment,
    environment_fingerprint,
)
from claude_sdk_proxy.journal import Journal, RecordClass
from claude_sdk_proxy.lifecycle import Record
from claude_sdk_proxy.isolation import IsolationConfig

pytestmark = pytest.mark.anyio

_NONCE = bytes.fromhex("8f" * 32)
_NONCE_HEX = _NONCE.hex()
_NORMAL_LIMIT = 32 * 1024
_HARD_LIMIT = _NORMAL_LIMIT + 1172 * 14
_JOURNAL_NAME = "allocation.journal"
_WORKDIR_NAME = "allocation.workdir"
_BOOTSTRAP_NAMES = (
    "LOCAL_PROXY_ALLOCATION_NONCE",
    "LOCAL_PROXY_INSTANCE_DIR",
    "LOCAL_PROXY_REAL_CLAUDE",
    "LOCAL_PROXY_CONTROL_FD",
)


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _supervisor_path() -> Path:
    return _repository_root() / "build/bin/claude-proxy-supervisor"


def _write_cli(path: Path) -> None:
    path.write_text(
        """#!/bin/sh
set -eu
if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then
  printf '2.1.251 (Claude Code)\\n'
  exit 0
fi
while IFS= read -r line; do
  printf '%s\\n' "$line" >> "$PWD/stdin.txt"
done
"""
    )
    path.chmod(0o700)


def _cli_identity(path: Path) -> CliExecutableIdentity:
    metadata = path.stat()
    return CliExecutableIdentity(
        version="2.1.251",
        path_sha256=hashlib.sha256(str(path).encode()).hexdigest(),
        st_dev=metadata.st_dev,
        st_ino=metadata.st_ino,
        mode=metadata.st_mode,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _make_lock_files(parent_dirfd: int) -> None:
    for suffix in (".append.lock", ".action.lock"):
        descriptor = os.open(
            _JOURNAL_NAME + suffix,
            os.O_RDONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_dirfd,
        )
        os.close(descriptor)


@dataclass
class _LaunchInputs:
    config: IsolationConfig
    source: dict[str, str]
    environment_config: EnvironmentConfig
    manifest: AttestationManifest
    descriptors: SupervisorBootstrapDescriptors
    journal: Journal
    parent_dirfd: int


@contextmanager
def _launch_inputs(
    tmp_path: Path,
    *,
    network_proxy: bool = False,
    cli_body: str | None = None,
) -> Iterator[_LaunchInputs]:
    instance = tmp_path / "instance"
    instance.mkdir(mode=0o700, parents=True)
    parent_dirfd = os.open(
        instance, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    journal: Journal | None = None
    try:
        _make_lock_files(parent_dirfd)
        journal, receipt = Journal.create_at(
            parent_dirfd,
            _JOURNAL_NAME,
            _NONCE,
            _NORMAL_LIMIT,
            _HARD_LIMIT,
            workdir_parent_dirfd=parent_dirfd,
            workdir_name=_WORKDIR_NAME,
        )
        journal.create_workdir(receipt)
        journal.append(
            Record.prepared(
                1,
                "supervisor",
                claim_deadline_ns=time.monotonic_ns() + 5_000_000_000,
            ),
            RecordClass.NORMAL,
        )
        cli_dir = tmp_path / "cli-bin"
        cli_dir.mkdir()
        real_cli = cli_dir / "claude"
        if cli_body is None:
            _write_cli(real_cli)
        else:
            real_cli.write_text(cli_body)
            real_cli.chmod(0o700)
        source = {
            "HOME": str(tmp_path / "home"),
            "USER": "local-user",
            "LOGNAME": "local-user",
            "HTTP_PROXY": "http://127.0.0.1:8181",
            "HTTPS_PROXY": "http://127.0.0.1:8182",
            "HOST_SECRET": "must-not-appear",
        }
        environment_config = EnvironmentConfig(
            cli_dir=cli_dir,
            network_proxy=network_proxy,
        )
        environment = build_child_environment(source, environment_config)
        config = IsolationConfig(
            model_id="claude-pinned",
            system_prompt="CANARY-SYSTEM",
            cwd=instance / _WORKDIR_NAME,
            supervisor_path=_supervisor_path(),
            environment=environment,
        )
        manifest = AttestationManifest(
            schema=ATTESTATION_MANIFEST_SCHEMA,
            version=1,
            sdk_version="0.2.148",
            cli_version="2.1.251",
            cli_executable=_cli_identity(real_cli),
            environment_fingerprint=environment_fingerprint(environment),
            availability=current_attestation_availability(),
        )
        descriptors = SupervisorBootstrapDescriptors(
            allocation_nonce=_NONCE_HEX,
            instance_dir=instance,
            real_cli=real_cli,
            journal=journal,
        )
        yield _LaunchInputs(
            config,
            source,
            environment_config,
            manifest,
            descriptors,
            journal,
            parent_dirfd,
        )
    finally:
        if journal is not None:
            journal.close()
        os.close(parent_dirfd)


def _prepare(inputs: _LaunchInputs) -> PreparedSupervisorLaunch:
    return prepare_supervisor_launch(
        inputs.config,
        inputs.descriptors,
        inputs.manifest,
        source_environment=inputs.source,
        environment_config=inputs.environment_config,
    )


def _raw_sdk_init(**changes: object) -> SystemMessage:
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


def test_no_installed_production_path_can_mint_positive_attestation() -> None:
    assert not any(name.startswith("_test_only") for name in vars(implementation))
    assert "FullSupervisorEvidence" not in public.__all__
    assert not hasattr(public, "FullSupervisorEvidence")
    with pytest.raises(TypeError, match="cannot be constructed"):
        ChildAttestation()  # type: ignore[call-arg]


@pytest.mark.parametrize("operation", [copy.copy, copy.deepcopy, pickle.dumps])
def test_forged_child_attestation_cannot_be_copied_or_pickled(operation: Any) -> None:
    forged = object.__new__(ChildAttestation)

    with pytest.raises((AttestationError, TypeError, pickle.PicklingError)):
        operation(forged)


def test_gate_failure_callback_never_runs_under_gate_lock() -> None:
    callback_thread_finished: list[bool] = []
    gate_cell: list[AttestationGate] = []

    def close_child() -> None:
        finished = threading.Event()

        def inspect_gate() -> None:
            assert gate_cell[0].closed
            assert gate_cell[0].pending_turn_count == 0
            finished.set()

        worker = threading.Thread(target=inspect_gate)
        worker.start()
        worker.join(timeout=1)
        callback_thread_finished.append(finished.is_set())

    gate = AttestationGate(
        availability=current_attestation_availability(),
        relay=lambda _payload: (_ for _ in ()).throw(AssertionError("relayed")),
        close_child=close_child,
    )
    gate_cell.append(gate)
    turn = gate.queue_turn(system="S", blocks=[{"type": "text", "text": "U"}])

    with pytest.raises(AttestationError, match="core attestation gate unavailable"):
        gate.release(turn)

    assert callback_thread_finished == [True]
    assert gate.closed and gate.revoked
    assert gate.pending_turn_count == 0
    assert gate.queued_content_bytes == 0


def test_gate_close_callback_never_runs_under_gate_lock() -> None:
    observed: list[bool] = []
    gate_cell: list[AttestationGate] = []

    def close_child() -> None:
        worker = threading.Thread(target=lambda: observed.append(gate_cell[0].closed))
        worker.start()
        worker.join(timeout=1)
        assert not worker.is_alive()

    gate = AttestationGate(
        availability=current_attestation_availability(),
        relay=lambda _payload: None,
        close_child=close_child,
    )
    gate_cell.append(gate)
    gate.close()
    gate.close()

    assert observed == [True]


def test_prepare_reapplies_task2_environment_and_rejects_ambient_override(
    tmp_path: Path,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        changed_source = {
            **inputs.source,
            "ANTHROPIC_API_KEY": "must-never-reach-child",
        }
        with pytest.raises(EnvironmentAmbiguityError, match="authentication"):
            prepare_supervisor_launch(
                inputs.config,
                inputs.descriptors,
                inputs.manifest,
                source_environment=changed_source,
                environment_config=inputs.environment_config,
            )


def test_prepare_rejects_prebuilt_environment_or_proxy_bit_drift(tmp_path: Path) -> None:
    with _launch_inputs(tmp_path, network_proxy=True) as inputs:
        altered = replace(
            inputs.config,
            environment={**inputs.config.environment, "HTTP_PROXY": "http://wrong"},
        )
        with pytest.raises(AttestationError, match="effective child environment"):
            prepare_supervisor_launch(
                altered,
                inputs.descriptors,
                inputs.manifest,
                source_environment=inputs.source,
                environment_config=inputs.environment_config,
            )

        wrong_proxy_config = EnvironmentConfig(
            cli_dir=inputs.environment_config.cli_dir,
            network_proxy=False,
        )
        with pytest.raises(AttestationError, match="environment config|fingerprint"):
            prepare_supervisor_launch(
                inputs.config,
                inputs.descriptors,
                inputs.manifest,
                source_environment=inputs.source,
                environment_config=wrong_proxy_config,
            )


def test_prepare_actually_runs_bounded_exact_cli_version_probe(tmp_path: Path) -> None:
    body = "#!/bin/sh\nprintf '2.1.250 (Claude Code)\\n'\n"
    with _launch_inputs(tmp_path, cli_body=body) as inputs:
        with pytest.raises(AttestationError, match="version"):
            _prepare(inputs)


def test_prepare_rejects_cli_replacement_during_version_probe(tmp_path: Path) -> None:
    body = """#!/bin/sh
if [ "$1" = "--version" ]; then
  cp "$0.next" "$0"
  chmod 700 "$0"
  printf '2.1.251 (Claude Code)\\n'
fi
"""
    with _launch_inputs(tmp_path, cli_body=body) as inputs:
        replacement = inputs.descriptors.real_cli.with_name("claude.next")
        _write_cli(replacement)
        with pytest.raises(AttestationError, match="changed during measurement"):
            _prepare(inputs)


def test_prepared_launch_is_opaque_sealed_single_use_and_not_picklable(
    tmp_path: Path,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        with pytest.raises(TypeError, match="cannot be constructed"):
            PreparedSupervisorLaunch()  # type: ignore[call-arg]
        launch = _prepare(inputs)
        try:
            assert set(launch.supervisor_environment) == {
                *inputs.config.environment,
                *_BOOTSTRAP_NAMES,
            }
            with pytest.raises((AttributeError, AttestationError, TypeError)):
                launch.command = ("/tmp/forged",)  # type: ignore[misc]
            for operation in (copy.copy, copy.deepcopy, pickle.dumps):
                with pytest.raises((AttestationError, TypeError, pickle.PicklingError)):
                    operation(launch)
            transport = AttestedSupervisorTransport(launch)
            with pytest.raises(AttestationError, match="already claimed"):
                AttestedSupervisorTransport(launch)
            assert isinstance(build_attested_sdk_client(launch, transport=transport), ClaudeSDKClient)
        finally:
            launch.close()


def _initialize(request_id: str = "req_1_deadbeef") -> str:
    return (
        json.dumps(
            {
                "type": "control_request",
                "request_id": request_id,
                "request": {"subtype": "initialize", "hooks": None, "skills": []},
            },
            separators=(",", ":"),
        )
        + "\n"
    )


@pytest.mark.parametrize(
    "data",
    [
        _initialize("req_0_deadbeef"),
        _initialize("req_01_deadbeef"),
        _initialize("req_1_DEADBEEF"),
        _initialize("req_1_deadbee"),
        _initialize("request-1"),
        _initialize() + _initialize("req_2_feedface"),
        '{"type":"control_request","type":"control_request","request_id":"req_1_deadbeef","request":{"subtype":"initialize","hooks":null}}\n',
        json.dumps(
            {
                "type": "control_request",
                "request_id": "req_1_deadbeef",
                "request": {
                    "subtype": "initialize",
                    "hooks": None,
                    "agents": {"x": {"prompt": "CONTENT"}},
                },
            }
        )
        + "\n",
        " " * 4097 + "\n",
    ],
)
def test_initialize_classifier_has_exact_bounded_sdk_shape(data: str) -> None:
    assert implementation._is_safe_initialize(data) is False


def test_initialize_classifier_accepts_only_the_exact_sdk_request_pattern() -> None:
    assert implementation._is_safe_initialize(_initialize()) is True


async def test_real_task5_handshake_creates_opaque_runtime_receipt_and_sends_ack(
    tmp_path: Path,
) -> None:
    with _launch_inputs(tmp_path, network_proxy=True) as inputs:
        launch = _prepare(inputs)
        transport = AttestedSupervisorTransport(launch)
        try:
            await transport.connect()
            receipt = transport.handshake_receipt
            assert receipt is not None
            assert receipt.control_trace == (
                "SUPERVISOR_IDENTITY",
                "IDENTITY_ACK",
                "ANCHOR_IDENTITY",
                "ANCHOR_ACK",
                "CLI_ARMED",
                "ARMED_ACK",
                "CLI_RUNNING",
            )
            assert receipt.canonical_control_types == (
                "SUPERVISOR_IDENTITY",
                "ANCHOR_IDENTITY",
                "CLI_ARMED",
                "CLI_RUNNING",
            )
            assert tuple(sorted(receipt.canonical_control_sequences)) == (
                receipt.canonical_control_sequences
            )
            assert len(set(receipt.canonical_control_hashes)) == 4
            assert receipt.identity_ack_sequence > 0
            assert receipt.identity_ack_hash in receipt.canonical_control_hashes
            assert receipt.network_proxy_enabled is True
            with pytest.raises((AttestationError, TypeError)):
                copy.copy(receipt)
        finally:
            await transport.close()

        assert not (inputs.descriptors.instance_dir / _JOURNAL_NAME).exists()
        assert not (inputs.descriptors.instance_dir / _WORKDIR_NAME).exists()


async def test_transport_sends_only_one_exact_initialize_and_buffers_everything_else(
    tmp_path: Path,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        launch = _prepare(inputs)
        transport = AttestedSupervisorTransport(launch)
        try:
            await transport.connect()
            await transport.write(_initialize("malicious"))
            initialize = _initialize()
            await transport.write(initialize)
            await transport.write(_initialize("req_2_feedface"))
            await transport.write(
                json.dumps(
                    {"type": "user", "message": {"content": "CANARY-USER"}}
                )
                + "\n"
            )
            await transport.end_input()
            stdin_path = inputs.config.cwd / "stdin.txt"
            deadline = time.monotonic() + 2
            while not stdin_path.exists() and time.monotonic() < deadline:
                await anyio.sleep(0.01)
            assert stdin_path.read_text().splitlines() == [initialize.rstrip("\n")]
            assert transport.buffered_user_write_count == 3
            with pytest.raises(AttestationError, match="core attestation gate unavailable"):
                await transport.release_buffered()
            assert transport.discarded_user_write_count == 3
        finally:
            await transport.close()


class _HangingCloseProcess:
    returncode = 0

    async def wait(self) -> int:
        return 0

    def terminate(self) -> None:
        raise AssertionError("terminated an exited process")

    def kill(self) -> None:
        raise AssertionError("killed an exited process")

    async def aclose(self) -> None:
        await anyio.sleep_forever()


async def test_process_aclose_is_bounded_and_unconfirmed_ownership_is_retained(
    tmp_path: Path,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        launch = _prepare(inputs)
        transport = AttestedSupervisorTransport(launch)
        process = _HangingCloseProcess()
        transport._process = process  # type: ignore[assignment]
        started = time.monotonic()

        with pytest.raises(AttestationError, match="cleanup is unconfirmed"):
            await transport.close()

        assert time.monotonic() - started < 1.0
        assert transport.cleanup_unconfirmed is True
        assert transport._process is process
        launch.close()


def test_default_sdk_transport_is_rejected(tmp_path: Path) -> None:
    with _launch_inputs(tmp_path) as inputs:
        launch = _prepare(inputs)
        try:
            with pytest.raises(AttestationError, match="custom Task 6 transport"):
                build_attested_sdk_client(launch)
        finally:
            launch.close()
