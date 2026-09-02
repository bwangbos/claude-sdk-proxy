"""Fail-closed Task 6 provenance, launch, and transport proofs."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import pickle
import shlex
import shutil
import signal
import socket
import struct
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, dataclass, replace
from pathlib import Path
from typing import Any, Never

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
from claude_sdk_proxy.isolation import IsolationConfig
from claude_sdk_proxy.journal import Journal, RecordClass
from claude_sdk_proxy.lifecycle import Record

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
    fixture = _repository_root() / "build/bin/claude-proxy-task6-test-cli"
    if not fixture.is_file():
        raise RuntimeError("Task 6 native test CLI is unavailable; run make native")
    shutil.copyfile(fixture, path)
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


def _identity_payload(
    pid: int,
    *,
    pgid: int,
    sid: int,
    executable_dev: int = 1,
    executable_ino: int = 1,
    executable_hash: bytes = b"e" * 32,
) -> bytes:
    return (
        struct.pack(
            "<qQIiiIQQ",
            pid,
            1,
            os.getuid(),
            pgid,
            sid,
            0x3F,
            executable_dev,
            executable_ino,
        )
        + b"b" * 32
        + executable_hash
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


def _wait_for_fork_child(pid: int, timeout: float = 2.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        waited, status = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return os.waitstatus_to_exitcode(status)
        time.sleep(0.005)
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    raise AssertionError("fork child did not fail closed within the bound")


def _child_alarm() -> None:
    signal.signal(signal.SIGALRM, lambda _signum, _frame: os._exit(124))
    signal.alarm(1)


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
    assert "RelayReceipt" not in public.__all__
    assert not hasattr(public, "RelayReceipt")
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


def test_prepare_rejects_prebuilt_environment_or_proxy_bit_drift(
    tmp_path: Path,
) -> None:
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
        with pytest.raises(
            AttestationError,
            match="effective child environment|environment config|fingerprint",
        ):
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


def test_prepare_rejects_nonexact_cli_version_output(tmp_path: Path) -> None:
    body = "#!/bin/sh\nprintf '  2.1.251 (Claude Code)\\n'\n"
    with _launch_inputs(tmp_path, cli_body=body) as inputs:
        with pytest.raises(AttestationError, match="version"):
            _prepare(inputs)


def test_manifest_mismatch_rejects_before_cli_execution(tmp_path: Path) -> None:
    marker = tmp_path / "version-executed"
    body = (
        "#!/bin/sh\n"
        f"printf marker > {shlex.quote(str(marker))}\n"
        "printf '2.1.251 (Claude Code)\\n'\n"
    )
    with _launch_inputs(tmp_path, cli_body=body) as inputs:
        mismatched = replace(
            inputs.manifest,
            cli_executable=replace(inputs.manifest.cli_executable, sha256="0" * 64),
        )
        with pytest.raises(AttestationError, match="manifest identity"):
            prepare_supervisor_launch(
                inputs.config,
                inputs.descriptors,
                mismatched,
                source_environment=inputs.source,
                environment_config=inputs.environment_config,
            )
        assert not marker.exists()


def test_cli_version_probe_bounds_concurrent_stdout_and_stderr(tmp_path: Path) -> None:
    marker = tmp_path / "flood-completed"
    body = (
        "#!/bin/sh\n"
        "/usr/bin/head -c 1048576 /dev/zero\n"
        "/usr/bin/head -c 1048576 /dev/zero >&2\n"
        f"printf marker > {shlex.quote(str(marker))}\n"
    )
    with _launch_inputs(tmp_path, cli_body=body) as inputs:
        started = time.monotonic()
        with pytest.raises(AttestationError, match="output bound"):
            _prepare(inputs)
        assert time.monotonic() - started < 2
        assert not marker.exists()


def test_cli_version_probe_cleans_group_after_leader_exits(tmp_path: Path) -> None:
    descendant_file = tmp_path / "version-descendant.pid"
    descendant_script = (
        "trap '' TERM; "
        f'printf "$$" > {shlex.quote(str(descendant_file))}; '
        "/bin/sleep 0.1; "
        "/usr/bin/head -c 300 /dev/zero; "
        "while :; do /bin/sleep 1; done"
    )
    body = f"#!/bin/sh\n/bin/sh -c {shlex.quote(descendant_script)} &\nexit 0\n"
    descendant_pid: int | None = None
    try:
        with _launch_inputs(tmp_path, cli_body=body) as inputs:
            with pytest.raises(AttestationError, match="output bound"):
                _prepare(inputs)
            deadline = time.monotonic() + 1
            while not descendant_file.exists() and time.monotonic() < deadline:
                time.sleep(0.001)
            descendant_pid = int(descendant_file.read_text())
            with pytest.raises(ProcessLookupError):
                os.kill(descendant_pid, 0)
    finally:
        if descendant_pid is not None:
            try:
                descendant_pgid = os.getpgid(descendant_pid)
            except ProcessLookupError:
                pass
            else:
                assert descendant_pgid != os.getpgrp()
                os.killpg(descendant_pgid, signal.SIGKILL)


def _cleanup_fault_injected_version_owner(owner: Any) -> None:
    try:
        implementation._terminate_version_probe(owner)
    finally:
        implementation._RETAINED_VERSION_PROBES.pop(owner.leader_pid, None)
        for stream in (owner.process.stdin, owner.process.stdout, owner.process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


class _CloseFaultStream:
    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self.fail_close = True

    @property
    def closed(self) -> bool:
        return self.stream.closed

    def fileno(self) -> int:
        return self.stream.fileno()

    def close(self) -> None:
        if self.fail_close:
            raise RuntimeError("injected stream close failure")
        self.stream.close()


class _CloseFaultSelector:
    def __init__(self, selector: Any) -> None:
        self.selector = selector
        self.fail_close = True

    def __getattr__(self, name: str) -> Any:
        return getattr(self.selector, name)

    def close(self) -> None:
        if self.fail_close:
            raise RuntimeError("injected selector close failure")
        self.selector.close()


class _BlockingCloseStream:
    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self.entered = threading.Event()
        self.release = threading.Event()
        self.close_count = 0

    @property
    def closed(self) -> bool:
        return self.stream.closed

    def fileno(self) -> int:
        return self.stream.fileno()

    def close(self) -> None:
        self.close_count += 1
        self.entered.set()
        assert self.release.wait(timeout=2)
        self.stream.close()


class _ReentrantCloseResource:
    def __init__(self, resource: Any, owner: Any, *, catch_recursive: bool) -> None:
        self.resource = resource
        self.owner = owner
        self.catch_recursive = catch_recursive
        self.reenter = True
        self.in_callback = False
        self.close_count = 0
        self.recursive_errors: list[AttestationError] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.resource, name)

    @property
    def closed(self) -> bool:
        return self.resource.closed

    def close(self) -> None:
        self.close_count += 1
        if self.reenter:
            if self.in_callback:
                raise AssertionError("recursive cleanup repeated resource close")
            self.in_callback = True
            try:
                implementation._cleanup_version_probe_owner(self.owner)
            except AttestationError as error:
                self.recursive_errors.append(error)
                if not self.catch_recursive:
                    raise
            finally:
                self.in_callback = False
        self.resource.close()


class _EffectThenReentrantSelector:
    def __init__(self, selector: Any, owner: Any, *, reentry: str) -> None:
        self.selector = selector
        self.owner = owner
        self.reentry = reentry
        self.in_callback = False
        self.close_count = 0
        self.real_close_count = 0
        self.recursive_errors: list[AttestationError] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.selector, name)

    def _reenter(self) -> AttestationError:
        try:
            implementation._cleanup_version_probe_owner(self.owner)
        except AttestationError as error:
            self.recursive_errors.append(error)
            return error
        raise AssertionError("recursive selector cleanup unexpectedly succeeded")

    def _close_real_selector(self) -> None:
        self.real_close_count += 1
        self.selector.close()

    def close(self) -> None:
        self.close_count += 1
        if self.in_callback:
            raise AssertionError("recursive cleanup repeated selector close")
        self.in_callback = True
        try:
            if self.reentry == "before":
                error = self._reenter()
                self._close_real_selector()
                raise error
            self._close_real_selector()
            raise self._reenter()
        finally:
            self.in_callback = False


@pytest.mark.parametrize("failure_type", [RuntimeError, KeyboardInterrupt])
def test_selector_construction_failure_cleans_captured_version_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
) -> None:
    body = "#!/bin/sh\ntrap '' TERM\nwhile :; do /bin/sleep 1; done\n"
    captured: list[Any] = []
    original_capture = implementation._capture_version_probe_owner

    def capture_owner(process: Any) -> Any:
        owner = original_capture(process)
        captured.append(owner)
        return owner

    def fail_selector() -> Never:
        raise failure_type("injected selector construction failure")

    monkeypatch.setattr(implementation, "_capture_version_probe_owner", capture_owner)
    monkeypatch.setattr(implementation.selectors, "DefaultSelector", fail_selector)
    try:
        with _launch_inputs(tmp_path, cli_body=body) as inputs:
            with pytest.raises(failure_type, match="selector construction"):
                _prepare(inputs)
        assert len(captured) == 1
        owner = captured[0]
        assert owner.process.returncode is not None
        assert owner.leader_pid not in implementation._RETAINED_VERSION_PROBES
        assert owner.process.stdout.closed
        assert owner.process.stderr.closed
    finally:
        if captured and captured[0].process.returncode is None:
            _cleanup_fault_injected_version_owner(captured[0])


def test_unproved_post_capture_cleanup_retains_owner_and_blocks_future_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "#!/bin/sh\ntrap '' TERM\nwhile :; do /bin/sleep 1; done\n"
    captured: list[Any] = []
    original_capture = implementation._capture_version_probe_owner
    original_wait = implementation._wait_for_version_probe_group_absence

    def capture_owner(process: Any) -> Any:
        owner = original_capture(process)
        captured.append(owner)
        return owner

    def fail_selector() -> Never:
        raise RuntimeError("injected selector construction failure")

    def fail_absence(*_args: object) -> Never:
        raise AttestationError("injected group absence failure")

    monkeypatch.setattr(implementation, "_capture_version_probe_owner", capture_owner)
    monkeypatch.setattr(implementation.selectors, "DefaultSelector", fail_selector)
    monkeypatch.setattr(
        implementation, "_wait_for_version_probe_group_absence", fail_absence
    )
    try:
        with _launch_inputs(tmp_path, cli_body=body) as inputs:
            with pytest.raises(AttestationError, match="group absence failure"):
                _prepare(inputs)
            assert len(captured) == 1
            owner = captured[0]
            assert implementation._RETAINED_VERSION_PROBES == {owner.leader_pid: owner}
            assert owner.process.returncode is None
            assert not owner.process.stdout.closed
            assert not owner.process.stderr.closed
            with pytest.raises(AttestationError, match="cleanup is unconfirmed"):
                _prepare(inputs)
    finally:
        monkeypatch.setattr(
            implementation, "_wait_for_version_probe_group_absence", original_wait
        )
        if captured:
            _cleanup_fault_injected_version_owner(captured[0])


def test_interrupt_immediately_after_capture_retains_authoritative_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "#!/bin/sh\ntrap '' TERM\nwhile :; do /bin/sleep 1; done\n"
    captured: list[Any] = []
    original_capture = implementation._capture_version_probe_owner

    def interrupt_after_capture(process: Any) -> Never:
        owner = original_capture(process)
        captured.append(owner)
        raise KeyboardInterrupt("injected immediately after capture")

    monkeypatch.setattr(
        implementation, "_capture_version_probe_owner", interrupt_after_capture
    )
    try:
        with _launch_inputs(tmp_path, cli_body=body) as inputs:
            with pytest.raises(KeyboardInterrupt, match="immediately after capture"):
                _prepare(inputs)
            assert len(captured) == 1
            owner = captured[0]
            assert implementation._RETAINED_VERSION_PROBES == {owner.leader_pid: owner}
            assert owner.process.returncode is None
            assert not owner.process.stdout.closed
            assert not owner.process.stderr.closed
            with pytest.raises(AttestationError, match="cleanup is unconfirmed"):
                _prepare(inputs)
    finally:
        if captured:
            _cleanup_fault_injected_version_owner(captured[0])


@pytest.mark.parametrize("resource", ["selector", "stdout", "stderr"])
def test_post_reap_close_failure_retains_resource_only_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resource: str,
) -> None:
    captured: list[Any] = []
    faults: list[Any] = []
    original_capture = implementation._capture_version_probe_owner
    original_selector = implementation.selectors.DefaultSelector

    def capture_owner(process: Any) -> Any:
        owner = original_capture(process)
        captured.append(owner)
        if resource in {"stdout", "stderr"}:
            fault = _CloseFaultStream(getattr(process, resource))
            setattr(process, resource, fault)
            faults.append(fault)
        return owner

    if resource == "selector":

        def create_selector() -> _CloseFaultSelector:
            fault = _CloseFaultSelector(original_selector())
            faults.append(fault)
            return fault

        monkeypatch.setattr(
            implementation.selectors, "DefaultSelector", create_selector
        )
    monkeypatch.setattr(implementation, "_capture_version_probe_owner", capture_owner)
    try:
        with _launch_inputs(tmp_path) as inputs:
            with pytest.raises(RuntimeError, match="close failure"):
                _prepare(inputs)
        assert len(captured) == 1
        owner = captured[0]
        assert owner.process.returncode is not None
        assert implementation._RETAINED_VERSION_PROBES == {owner.leader_pid: owner}
        assert (
            owner.state
            is implementation._VersionProbeOwnerState.REAPED_RESOURCE_CLOSE_PENDING
        )

        original_state = owner.state
        owner.state = "tampered"
        with pytest.raises(AttestationError, match="owner state"):
            implementation._cleanup_version_probe_owner(owner)
        assert implementation._RETAINED_VERSION_PROBES == {owner.leader_pid: owner}
        owner.state = original_state

        def forbidden_killpg(_pgid: int, _signal: int) -> Never:
            raise AssertionError("post-reap cleanup attempted group signaling")

        monkeypatch.setattr(implementation.os, "killpg", forbidden_killpg)
        faults[0].fail_close = False
        if resource == "selector":
            assert (
                owner.selector_close_disposition
                is implementation._VersionProbeSelectorCloseDisposition.AMBIGUOUS
            )
            with pytest.raises(AttestationError, match="selector close is ambiguous"):
                implementation._cleanup_version_probe_owner(owner)
            assert implementation._RETAINED_VERSION_PROBES == {
                owner.leader_pid: owner
            }
        else:
            implementation._cleanup_version_probe_owner(owner)
            assert owner.leader_pid not in implementation._RETAINED_VERSION_PROBES
    finally:
        if captured:
            owner = captured[0]
            state_type = getattr(implementation, "_VersionProbeOwnerState", None)
            if state_type is not None:
                owner.state = state_type.REAPED_RESOURCE_CLOSE_PENDING
            for fault in faults:
                fault.fail_close = False
            implementation._RETAINED_VERSION_PROBES.pop(owner.leader_pid, None)
            selector = getattr(owner, "selector", None)
            if selector is not None:
                selector.close()
            for stream in (owner.process.stdout, owner.process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()


@pytest.mark.parametrize(
    "failure_type", [KeyboardInterrupt, MemoryError, SystemExit, RuntimeError]
)
def test_popen_baseexception_retains_ambiguous_spawn_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
) -> None:
    calls = 0
    signals: list[tuple[int, int]] = []

    def fail_popen(*_args: object, **_kwargs: object) -> Never:
        nonlocal calls
        calls += 1
        raise failure_type("injected Popen failure")

    monkeypatch.setattr(implementation.subprocess, "Popen", fail_popen)
    monkeypatch.setattr(
        implementation.os,
        "killpg",
        lambda pgid, signal_number: signals.append((pgid, signal_number)),
    )
    try:
        with _launch_inputs(tmp_path) as inputs:
            with pytest.raises(failure_type, match="Popen failure"):
                _prepare(inputs)
            assert len(implementation._RETAINED_VERSION_PROBES) == 1
            reservation = next(iter(implementation._RETAINED_VERSION_PROBES.values()))
            assert (
                reservation.state
                is implementation._VersionProbeOwnerState.AMBIGUOUS_SPAWN
            )
            assert reservation.process is None
            with pytest.raises(AttestationError, match="cleanup is unconfirmed"):
                _prepare(inputs)
        assert calls == 1
        assert signals == []
    finally:
        implementation._RETAINED_VERSION_PROBES.clear()


def test_interrupt_between_popen_return_and_promotion_retains_exact_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes: list[Any] = []

    def interrupt_before_promotion(_reservation: Any, process: Any) -> Never:
        processes.append(process)
        raise KeyboardInterrupt("injected before promotion")

    monkeypatch.setattr(
        implementation,
        "_promote_version_probe_reservation",
        interrupt_before_promotion,
    )
    try:
        with _launch_inputs(tmp_path) as inputs:
            with pytest.raises(KeyboardInterrupt, match="before promotion"):
                _prepare(inputs)
            assert len(processes) == 1
            assert len(implementation._RETAINED_VERSION_PROBES) == 1
            owner = next(iter(implementation._RETAINED_VERSION_PROBES.values()))
            assert owner.process is processes[0]
            assert (
                owner.state
                is implementation._VersionProbeOwnerState.AMBIGUOUS_SPAWN
            )
    finally:
        if processes:
            process = processes[0]
            process.wait(timeout=2)
            for stream in (process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        implementation._RETAINED_VERSION_PROBES.clear()


def _spawn_finished_version_probe(inputs: _LaunchInputs) -> Any:
    effective = build_child_environment(inputs.source, inputs.environment_config)
    owner = implementation._spawn_and_register_version_probe(
        inputs.descriptors.real_cli, effective
    )
    assert owner.process.stdout is not None
    assert owner.process.stderr is not None
    owner.process.stdout.read()
    owner.process.stderr.read()
    return owner


@pytest.mark.parametrize("status_available", [False, True])
def test_wait_baseexception_reconciles_only_available_exit_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_available: bool,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        owner = _spawn_finished_version_probe(inputs)
        original_wait = owner.process.wait

        def interrupting_wait(*args: object, **kwargs: object) -> Never:
            if status_available:
                original_wait(*args, **kwargs)
            raise KeyboardInterrupt("injected wait interruption")

        monkeypatch.setattr(owner.process, "wait", interrupting_wait)
        try:
            with pytest.raises(KeyboardInterrupt, match="wait interruption"):
                implementation._reap_version_probe_leader(owner)
            expected = (
                implementation._VersionProbeOwnerState.REAPED_RESOURCE_CLOSE_PENDING
                if status_available
                else implementation._VersionProbeOwnerState.LIVE_CLEANUP_PENDING
            )
            assert owner.state is expected
            assert (owner.process.returncode is not None) is status_available
            assert implementation._RETAINED_VERSION_PROBES == {
                owner.leader_pid: owner
            }
        finally:
            monkeypatch.setattr(owner.process, "wait", original_wait)
            if (
                owner.process.returncode is not None
                and owner.state
                is implementation._VersionProbeOwnerState.LIVE_CLEANUP_PENDING
            ):
                owner.state = (
                    implementation._VersionProbeOwnerState.REAPED_RESOURCE_CLOSE_PENDING
                )
            implementation._cleanup_version_probe_owner(owner)


def test_concurrent_reaped_cleanup_retries_are_serialized(
    tmp_path: Path,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        owner = _spawn_finished_version_probe(inputs)
        implementation._reap_version_probe_leader(owner)
        assert owner.process.stdout is not None
        blocking = _BlockingCloseStream(owner.process.stdout)
        owner.process.stdout = blocking  # type: ignore[assignment]
        outcomes: list[BaseException | None] = []

        def cleanup() -> None:
            try:
                implementation._cleanup_version_probe_owner(owner)
            except BaseException as error:
                outcomes.append(error)
            else:
                outcomes.append(None)

        first = threading.Thread(target=cleanup)
        second = threading.Thread(target=cleanup)
        first.start()
        assert blocking.entered.wait(timeout=2)
        second.start()
        time.sleep(0.05)
        blocking.release.set()
        first.join(timeout=2)
        second.join(timeout=2)
        assert not first.is_alive() and not second.is_alive()
        assert blocking.close_count == 1
        assert sum(outcome is None for outcome in outcomes) == 1
        assert sum(isinstance(outcome, AttestationError) for outcome in outcomes) == 1
        assert owner.leader_pid not in implementation._RETAINED_VERSION_PROBES


@pytest.mark.parametrize("callback", ["termination", "wait"])
@pytest.mark.parametrize("catch_recursive", [False, True])
def test_live_cleanup_rejects_same_thread_reentry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    callback: str,
    catch_recursive: bool,
) -> None:
    body = "#!/bin/sh\ntrap 'exit 0' TERM\nwhile :; do /bin/sleep 1; done\n"
    cli_body = body if callback == "termination" else None
    with _launch_inputs(tmp_path, cli_body=cli_body) as inputs:
        if callback == "termination":
            effective = build_child_environment(
                inputs.source, inputs.environment_config
            )
            owner = implementation._spawn_and_register_version_probe(
                inputs.descriptors.real_cli, effective
            )
        else:
            owner = _spawn_finished_version_probe(inputs)

        callback_count = 0
        recursive_errors: list[AttestationError] = []
        if callback == "termination":
            original_callback = implementation.os.killpg

            def reentrant_callback(pgid: int, signal_number: int) -> None:
                nonlocal callback_count
                callback_count += 1
                if callback_count > 1:
                    raise AssertionError("recursive cleanup repeated group signaling")
                try:
                    implementation._cleanup_version_probe_owner(owner)
                except AttestationError as error:
                    recursive_errors.append(error)
                    if not catch_recursive:
                        raise
                original_callback(pgid, signal_number)

            monkeypatch.setattr(implementation.os, "killpg", reentrant_callback)
        else:
            original_callback = owner.process.wait

            def reentrant_callback(*args: object, **kwargs: object) -> int:
                nonlocal callback_count
                callback_count += 1
                if callback_count > 1:
                    raise AssertionError("recursive cleanup repeated leader wait")
                try:
                    implementation._cleanup_version_probe_owner(owner)
                except AttestationError as error:
                    recursive_errors.append(error)
                    if not catch_recursive:
                        raise
                return original_callback(*args, **kwargs)

            monkeypatch.setattr(owner.process, "wait", reentrant_callback)

        try:
            if catch_recursive:
                implementation._cleanup_version_probe_owner(owner)
                assert owner.leader_pid not in implementation._RETAINED_VERSION_PROBES
            else:
                with pytest.raises(AttestationError, match="already in progress"):
                    implementation._cleanup_version_probe_owner(owner)
                assert implementation._RETAINED_VERSION_PROBES == {
                    owner.leader_pid: owner
                }
                assert owner.cleanup_in_progress is False
            assert callback_count == 1
            assert len(recursive_errors) == 1
        finally:
            if callback == "termination":
                monkeypatch.setattr(implementation.os, "killpg", original_callback)
            else:
                monkeypatch.setattr(owner.process, "wait", original_callback)
            if implementation._RETAINED_VERSION_PROBES:
                implementation._cleanup_version_probe_owner(owner)


@pytest.mark.parametrize("resource_name", ["selector", "stdout", "stderr"])
@pytest.mark.parametrize("catch_recursive", [False, True])
def test_reaped_cleanup_rejects_same_thread_reentry(
    tmp_path: Path,
    resource_name: str,
    catch_recursive: bool,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        owner = _spawn_finished_version_probe(inputs)
        implementation._reap_version_probe_leader(owner)
        if resource_name == "selector":
            resource = implementation.selectors.DefaultSelector()
            wrapped = _ReentrantCloseResource(
                resource, owner, catch_recursive=catch_recursive
            )
            owner.selector = wrapped
        else:
            resource = getattr(owner.process, resource_name)
            assert resource is not None
            wrapped = _ReentrantCloseResource(
                resource, owner, catch_recursive=catch_recursive
            )
            setattr(owner.process, resource_name, wrapped)

        try:
            if catch_recursive:
                implementation._cleanup_version_probe_owner(owner)
                assert owner.leader_pid not in implementation._RETAINED_VERSION_PROBES
            else:
                with pytest.raises(AttestationError, match="already in progress"):
                    implementation._cleanup_version_probe_owner(owner)
                assert implementation._RETAINED_VERSION_PROBES == {
                    owner.leader_pid: owner
                }
                assert owner.cleanup_in_progress is False
                if resource_name == "selector":
                    assert owner.selector is wrapped
                else:
                    assert not resource.closed
            assert wrapped.close_count == 1
            assert len(wrapped.recursive_errors) == 1
        finally:
            if implementation._RETAINED_VERSION_PROBES:
                disposition = getattr(
                    implementation, "_VersionProbeSelectorCloseDisposition", None
                )
                if resource_name == "selector" and disposition is not None:
                    assert (
                        owner.selector_close_disposition
                        is disposition.AMBIGUOUS
                    )
                    with pytest.raises(
                        AttestationError, match="selector close is ambiguous"
                    ):
                        implementation._cleanup_version_probe_owner(owner)
                    assert wrapped.close_count == 1
                    implementation._RETAINED_VERSION_PROBES.pop(
                        owner.leader_pid, None
                    )
                    resource.close()
                elif resource_name == "selector":
                    implementation._RETAINED_VERSION_PROBES.pop(
                        owner.leader_pid, None
                    )
                    resource.close()
                    for stream in (owner.process.stdout, owner.process.stderr):
                        if stream is not None and not stream.closed:
                            stream.close()
                else:
                    wrapped.reenter = False
                    implementation._cleanup_version_probe_owner(owner)


@pytest.mark.parametrize("reentry", ["before", "after"])
def test_selector_effect_then_reentrant_error_is_never_retried(
    tmp_path: Path,
    reentry: str,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        owner = _spawn_finished_version_probe(inputs)
        implementation._reap_version_probe_leader(owner)
        selector = implementation.selectors.DefaultSelector()
        wrapped = _EffectThenReentrantSelector(selector, owner, reentry=reentry)
        owner.selector = wrapped
        try:
            disposition = implementation._VersionProbeSelectorCloseDisposition
            assert owner.selector_close_disposition is disposition.PENDING
            with pytest.raises(AttestationError, match="already in progress"):
                implementation._cleanup_version_probe_owner(owner)
            assert owner.selector_close_disposition is disposition.AMBIGUOUS
            assert owner.selector is wrapped
            assert wrapped.close_count == 1
            assert wrapped.real_close_count == 1
            assert len(wrapped.recursive_errors) == 1
            assert owner.process.stdout.closed
            assert owner.process.stderr.closed
            assert owner.cleanup_in_progress is False
            assert implementation._RETAINED_VERSION_PROBES == {
                owner.leader_pid: owner
            }

            with pytest.raises(AttestationError, match="selector close is ambiguous"):
                implementation._cleanup_version_probe_owner(owner)
            assert wrapped.close_count == 1
            assert wrapped.real_close_count == 1
            assert implementation._RETAINED_VERSION_PROBES == {
                owner.leader_pid: owner
            }
        finally:
            implementation._RETAINED_VERSION_PROBES.pop(owner.leader_pid, None)
            if wrapped.real_close_count == 0:
                selector.close()
            for stream in (owner.process.stdout, owner.process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()


@pytest.mark.parametrize("tampered", [None, 0, 1, "pending"])
def test_selector_close_disposition_tampering_fails_before_action(
    tmp_path: Path,
    tampered: object,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        owner = _spawn_finished_version_probe(inputs)
        implementation._reap_version_probe_leader(owner)
        selector = implementation.selectors.DefaultSelector()
        owner.selector = selector
        owner.selector_close_disposition = tampered
        try:
            with pytest.raises(AttestationError, match="owner state"):
                implementation._cleanup_version_probe_owner(owner)
            assert implementation._RETAINED_VERSION_PROBES == {
                owner.leader_pid: owner
            }
            assert owner.selector is selector
            assert not owner.process.stdout.closed
            assert not owner.process.stderr.closed
        finally:
            if implementation._RETAINED_VERSION_PROBES:
                disposition = getattr(
                    implementation, "_VersionProbeSelectorCloseDisposition", None
                )
                if disposition is not None:
                    owner.selector_close_disposition = disposition.PENDING
                    implementation._cleanup_version_probe_owner(owner)
                else:
                    implementation._RETAINED_VERSION_PROBES.pop(
                        owner.leader_pid, None
                    )
                    selector.close()
                    for stream in (owner.process.stdout, owner.process.stderr):
                        if stream is not None and not stream.closed:
                            stream.close()


@pytest.mark.parametrize("tampered", [None, 0, 1, "yes"])
def test_cleanup_guard_tampering_fails_before_action(
    tmp_path: Path,
    tampered: object,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        owner = _spawn_finished_version_probe(inputs)
        owner.cleanup_in_progress = tampered
        try:
            with pytest.raises(AttestationError, match="owner state"):
                implementation._cleanup_version_probe_owner(owner)
            assert implementation._RETAINED_VERSION_PROBES == {
                owner.leader_pid: owner
            }
            assert owner.process.returncode is None
            assert not owner.process.stdout.closed
            assert not owner.process.stderr.closed
        finally:
            owner.cleanup_in_progress = False
            implementation._cleanup_version_probe_owner(owner)


def test_prepare_binds_workdir_to_task5_instance(tmp_path: Path) -> None:
    with _launch_inputs(tmp_path) as inputs:
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir(mode=0o700)
        changed = replace(inputs.config, cwd=unrelated)
        with pytest.raises(AttestationError, match="Task 5 allocation workdir"):
            prepare_supervisor_launch(
                changed,
                inputs.descriptors,
                inputs.manifest,
                source_environment=inputs.source,
                environment_config=inputs.environment_config,
            )


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
            assert isinstance(
                build_attested_sdk_client(launch, transport=transport), ClaudeSDKClient
            )
        finally:
            launch.close()


def test_fork_child_cannot_claim_prepared_launch_while_parent_claim_lock_is_held(
    tmp_path: Path,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        launch = _prepare(inputs)
        parent_fds = tuple(control.fileno() for control in (
            launch._child_control,
            launch._parent_control,
        ))
        claim_lock = getattr(implementation, "_LAUNCH_CLAIM_LOCK", threading.RLock())
        claim_lock.acquire()
        pid = os.fork()
        if pid == 0:
            _child_alarm()
            try:
                AttestedSupervisorTransport(launch)
            except AttestationError:
                os._exit(0)
            except BaseException:
                os._exit(2)
            os._exit(3)
        try:
            assert _wait_for_fork_child(pid) == 0
        finally:
            claim_lock.release()

        assert tuple(control.fileno() for control in (
            launch._child_control,
            launch._parent_control,
        )) == parent_fds
        transport = AttestedSupervisorTransport(launch)
        assert isinstance(
            build_attested_sdk_client(launch, transport=transport), ClaudeSDKClient
        )
        launch.close()


def test_stale_global_launch_authority_is_invalidated_in_fork_child(
    tmp_path: Path,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        inherited_authority = implementation._LAUNCH_TOKEN
        effective_environment = build_child_environment(
            inputs.source, inputs.environment_config
        )
        pid = os.fork()
        if pid == 0:
            _child_alarm()
            try:
                PreparedSupervisorLaunch._create(
                    inherited_authority,
                    config=inputs.config,
                    descriptors=inputs.descriptors,
                    cli_identity=inputs.manifest.cli_executable,
                    effective_environment=effective_environment,
                    network_proxy_enabled=False,
                )
            except AttestationError:
                os._exit(0)
            except BaseException:
                os._exit(2)
            os._exit(3)
        assert _wait_for_fork_child(pid) == 0


class _InheritedBlockingAsyncLock:
    def __init__(self, descriptor: int) -> None:
        self._descriptor = descriptor

    async def __aenter__(self) -> None:
        os.read(self._descriptor, 1)

    async def __aexit__(
        self,
        _exc_type: object,
        _exc: object,
        _traceback: object,
    ) -> None:
        return None


class _ResourceTouchTrap:
    def __getattribute__(self, name: str) -> object:
        if name.startswith("__"):
            return object.__getattribute__(self, name)
        raise AssertionError(f"transport traversed hostile resource attribute {name}")


class _LockTouchTrap:
    async def __aenter__(self) -> None:
        raise AssertionError("transport acquired an unauthorized async lock")

    async def __aexit__(
        self,
        _exc_type: object,
        _exc: object,
        _traceback: object,
    ) -> None:
        return None


class _EffectThenRaiseProcess:
    returncode = 0

    def __init__(self) -> None:
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1
        raise RuntimeError("synthetic close effect then failure")


class _ReentrantCloseProcess:
    returncode = 0

    def __init__(self, transport: AttestedSupervisorTransport) -> None:
        self._transport = transport
        self.ready_during_close: bool | None = None
        self.connect_error: AttestationError | None = None

    async def aclose(self) -> None:
        self.ready_during_close = self._transport.is_ready()
        try:
            await self._transport.connect()
        except AttestationError as error:
            self.connect_error = error


class _ConcurrentCloseProcess:
    returncode = 0

    def __init__(self) -> None:
        self.entered = anyio.Event()
        self.release = anyio.Event()

    async def aclose(self) -> None:
        self.entered.set()
        await self.release.wait()


def _exact_transport_forge(
    transport: AttestedSupervisorTransport,
) -> AttestedSupervisorTransport:
    forged = object.__new__(AttestedSupervisorTransport)
    for name, value in vars(transport).items():
        object.__setattr__(forged, name, value)
    return forged


@pytest.mark.parametrize("operation", [copy.copy, copy.deepcopy, pickle.dumps])
def test_transport_copy_and_pickle_are_rejected(
    tmp_path: Path,
    operation: Any,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        launch = _prepare(inputs)
        transport = AttestedSupervisorTransport(launch)
        try:
            with pytest.raises(AttestationError, match="transport.*(copied|pickled)"):
                operation(transport)
        finally:
            launch.close()


def test_shallow_copy_rejects_before_touching_aliased_async_lock(
    tmp_path: Path,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        launch = _prepare(inputs)
        transport = AttestedSupervisorTransport(launch)
        copied = _exact_transport_forge(transport)
        assert copied._lock is transport._lock

        async def exercise() -> None:
            async with transport._lock:
                with anyio.fail_after(0.1):
                    with pytest.raises(AttestationError, match="transport authority"):
                        await copied.connect()

        try:
            anyio.run(exercise)
        finally:
            launch.close()


async def test_exact_class_forge_rejects_every_surface_before_resource_touch(
    tmp_path: Path,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        launch = _prepare(inputs)
        transport = AttestedSupervisorTransport(launch)
        forged = _exact_transport_forge(transport)
        object.__setattr__(forged, "_lock", _LockTouchTrap())
        object.__setattr__(forged, "_control", _ResourceTouchTrap())
        object.__setattr__(forged, "_journal", _ResourceTouchTrap())
        object.__setattr__(forged, "launch", _ResourceTouchTrap())
        try:
            property_names = (
                "handshake_receipt",
                "cleanup_unconfirmed",
                "buffered_user_write_count",
                "discarded_user_write_count",
            )
            for name in property_names:
                descriptor = vars(AttestedSupervisorTransport)[name]
                assert isinstance(descriptor, property)
                getter = descriptor.fget
                assert getter is not None
                with pytest.raises(AttestationError, match="transport authority"):
                    getter(forged)

            with pytest.raises(AttestationError, match="transport authority"):
                AttestedSupervisorTransport.is_ready(forged)
            for operation in (
                lambda: AttestedSupervisorTransport.connect(forged),
                lambda: AttestedSupervisorTransport.write(
                    forged, _ResourceTouchTrap()
                ),
                lambda: AttestedSupervisorTransport.release_buffered(forged),
                lambda: AttestedSupervisorTransport.end_input(forged),
                lambda: AttestedSupervisorTransport.close(forged),
                lambda: AttestedSupervisorTransport._bounded_stdin_close(forged),
                lambda: AttestedSupervisorTransport._bounded_process_aclose(forged),
                lambda: AttestedSupervisorTransport._close_failed_handshake_process(
                    forged
                ),
                lambda: AttestedSupervisorTransport._close_running_process(forged),
                lambda: AttestedSupervisorTransport._close_transition(forged),
            ):
                with pytest.raises(AttestationError, match="transport authority"):
                    await operation()
            messages = AttestedSupervisorTransport.read_messages(forged)
            with pytest.raises(AttestationError, match="transport authority"):
                await anext(messages)
        finally:
            launch.close()


async def test_close_burns_authority_before_effect_then_raise(tmp_path: Path) -> None:
    with _launch_inputs(tmp_path) as inputs:
        launch = _prepare(inputs)
        transport = AttestedSupervisorTransport(launch)
        process = _EffectThenRaiseProcess()
        transport._process = process  # type: ignore[assignment]
        try:
            with pytest.raises(RuntimeError, match="effect then failure"):
                await transport.close()
            assert process.close_calls == 1
            assert transport.cleanup_unconfirmed is True

            with pytest.raises(AttestationError, match="transport authority"):
                await transport.close()
            assert process.close_calls == 1
        finally:
            launch.close()


async def test_close_reentrant_methods_reject_without_reacquiring_async_lock(
    tmp_path: Path,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        launch = _prepare(inputs)
        transport = AttestedSupervisorTransport(launch)
        process = _ReentrantCloseProcess(transport)
        transport._process = process  # type: ignore[assignment]
        try:
            with anyio.fail_after(0.2):
                await transport.close()

            assert process.ready_during_close is False
            assert isinstance(process.connect_error, AttestationError)
        finally:
            launch.close()


async def test_concurrent_method_rejects_after_close_revokes_before_effects(
    tmp_path: Path,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        launch = _prepare(inputs)
        transport = AttestedSupervisorTransport(launch)
        process = _ConcurrentCloseProcess()
        transport._process = process  # type: ignore[assignment]

        async def close_transport() -> None:
            await transport.close()

        try:
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(close_transport)
                await process.entered.wait()
                assert transport.is_ready() is False
                with anyio.fail_after(0.1):
                    with pytest.raises(AttestationError, match="transport authority"):
                        await transport.connect()
                process.release.set()
        finally:
            process.release.set()
            launch.close()


async def test_successful_close_revokes_all_stale_transport_access(
    tmp_path: Path,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        launch = _prepare(inputs)
        transport = AttestedSupervisorTransport(launch)
        await transport.close()

        for operation in (
            transport.connect,
            transport.release_buffered,
            transport.end_input,
            transport.close,
        ):
            with pytest.raises(AttestationError, match="transport authority"):
                await operation()
        with pytest.raises(AttestationError, match="transport authority"):
            transport.is_ready()
        with pytest.raises(AttestationError, match="transport authority"):
            _ = transport.handshake_receipt
        launch.close()


def test_transport_authority_capacity_releases_only_after_safe_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        implementation, "_MAX_LIVE_TRANSPORT_AUTHORITIES", 1, raising=False
    )
    with _launch_inputs(tmp_path / "one") as first_inputs:
        with _launch_inputs(tmp_path / "two") as second_inputs:
            first_launch = _prepare(first_inputs)
            second_launch = _prepare(second_inputs)
            first = AttestedSupervisorTransport(first_launch)
            with pytest.raises(AttestationError, match="capacity"):
                AttestedSupervisorTransport(second_launch)

            anyio.run(first.close)
            second = AttestedSupervisorTransport(second_launch)
            anyio.run(second.close)
            first_launch.close()
            second_launch.close()


async def test_transport_attribute_tampering_rejects_before_lock_or_resource(
    tmp_path: Path,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        launch = _prepare(inputs)
        transport = AttestedSupervisorTransport(launch)
        original_lock = transport._lock
        original_control = transport._control
        transport._lock = _LockTouchTrap()  # type: ignore[assignment]
        transport._control = _ResourceTouchTrap()  # type: ignore[assignment]
        try:
            with pytest.raises(AttestationError, match="transport authority"):
                await transport.connect()
            with pytest.raises(AttestationError, match="transport authority"):
                await transport.close()
        finally:
            transport._lock = original_lock
            transport._control = original_control
            launch.close()


def test_claimed_transport_rejects_fork_child_before_inherited_async_lock(
    tmp_path: Path,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        launch = _prepare(inputs)
        transport = AttestedSupervisorTransport(launch)
        original_lock = transport._lock
        blocked_read, blocked_write = os.pipe()
        transport._lock = _InheritedBlockingAsyncLock(blocked_read)  # type: ignore[assignment]
        pid = os.fork()
        if pid == 0:
            os.close(blocked_write)
            _child_alarm()
            try:
                anyio.run(transport.connect)
            except AttestationError:
                os._exit(0)
            except BaseException:
                os._exit(2)
            os._exit(3)
        try:
            assert _wait_for_fork_child(pid) == 0
        finally:
            os.close(blocked_read)
            os.close(blocked_write)
            transport._lock = original_lock

        assert transport.is_ready() is False
        assert isinstance(
            build_attested_sdk_client(launch, transport=transport), ClaudeSDKClient
        )
        launch.close()


def test_fork_child_close_connect_and_ready_reject_before_held_lock_and_resources(
    tmp_path: Path,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        launch = _prepare(inputs)
        transport = AttestedSupervisorTransport(launch)
        original_lock = transport._lock
        blocked_read, blocked_write = os.pipe()
        transport._lock = _InheritedBlockingAsyncLock(blocked_read)  # type: ignore[assignment]
        pid = os.fork()
        if pid == 0:
            os.close(blocked_write)
            _child_alarm()
            try:
                for operation in (transport.connect, transport.close):
                    try:
                        anyio.run(operation)
                    except AttestationError:
                        pass
                    else:
                        os._exit(3)
                try:
                    transport.is_ready()
                except AttestationError:
                    pass
                else:
                    os._exit(4)
            except BaseException:
                os._exit(2)
            os._exit(0)
        try:
            assert _wait_for_fork_child(pid) == 0
        finally:
            os.close(blocked_read)
            os.close(blocked_write)
            transport._lock = original_lock

        assert transport.is_ready() is False
        assert isinstance(
            build_attested_sdk_client(launch, transport=transport), ClaudeSDKClient
        )
        anyio.run(transport.close)
        launch.close()


def test_fork_child_cannot_rebind_copied_transport_to_child_pid(tmp_path: Path) -> None:
    with _launch_inputs(tmp_path) as inputs:
        launch = _prepare(inputs)
        transport = AttestedSupervisorTransport(launch)
        forged = _exact_transport_forge(transport)
        pid = os.fork()
        if pid == 0:
            _child_alarm()
            object.__setattr__(forged, "_creator_pid", os.getpid())
            try:
                forged.is_ready()
            except AttestationError:
                os._exit(0)
            except BaseException:
                os._exit(2)
            os._exit(3)
        assert _wait_for_fork_child(pid) == 0

        assert transport.is_ready() is False
        assert isinstance(
            build_attested_sdk_client(launch, transport=transport), ClaudeSDKClient
        )
        anyio.run(transport.close)
        launch.close()


def test_launch_and_transport_creator_guards_reject_tampering_and_hostile_subclasses(
    tmp_path: Path,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        launch = _prepare(inputs)
        creator_pid = os.getpid()
        object.__setattr__(launch, "_creator_pid", "hostile")
        try:
            with pytest.raises(AttestationError, match="creator process"):
                _ = launch.command
        finally:
            object.__setattr__(launch, "_creator_pid", creator_pid)

        class HostileLaunch(PreparedSupervisorLaunch):
            def __getattribute__(self, _name: str) -> object:
                raise AssertionError("hostile launch attributes were traversed")

        forged_launch = object.__new__(HostileLaunch)
        command_descriptor = vars(PreparedSupervisorLaunch)["command"]
        assert isinstance(command_descriptor, property)
        command_getter = command_descriptor.fget
        assert command_getter is not None
        with pytest.raises(AttestationError, match="creator process"):
            command_getter(forged_launch)

        transport = AttestedSupervisorTransport(launch)
        object.__setattr__(transport, "_creator_pid", "hostile")
        with pytest.raises(AttestationError, match="creator process"):
            transport.is_ready()
        object.__setattr__(transport, "_creator_pid", creator_pid)

        class HostileTransport(AttestedSupervisorTransport):
            def __getattribute__(self, _name: str) -> object:
                raise AssertionError("hostile transport attributes were traversed")

        forged_transport = object.__new__(HostileTransport)
        with pytest.raises(AttestationError, match="creator process"):
            AttestedSupervisorTransport.is_ready(forged_transport)
        launch.close()


def test_concurrent_prepared_launch_claims_have_exactly_one_winner(
    tmp_path: Path,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        launch = _prepare(inputs)
        start = threading.Barrier(3)
        transports: list[AttestedSupervisorTransport] = []
        failures: list[AttestationError] = []

        def claim() -> None:
            start.wait()
            try:
                transports.append(AttestedSupervisorTransport(launch))
            except AttestationError as error:
                failures.append(error)

        claimers = [threading.Thread(target=claim) for _ in range(2)]
        for claimer in claimers:
            claimer.start()
        start.wait()
        for claimer in claimers:
            claimer.join(timeout=1)
            assert not claimer.is_alive()
        assert len(transports) == 1
        assert len(failures) == 1
        assert "already claimed" in str(failures[0])
        launch.close()


def test_prepared_launch_fingerprint_binds_immutable_workdir(tmp_path: Path) -> None:
    with _launch_inputs(tmp_path) as inputs:
        launch = _prepare(inputs)
        unrelated = tmp_path / "changed-cwd"
        unrelated.mkdir(mode=0o700)
        object.__setattr__(launch._config, "cwd", unrelated)
        try:
            with pytest.raises(AttestationError, match="fields changed"):
                _ = launch.supervisor_environment
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


def test_cleanup_result_requires_complete_normal_task5_evidence() -> None:
    normal_flags = 0xFF7
    implementation._validate_cleanup_result(
        struct.pack("<IIQQQII", normal_flags, 4, 0xF, 12, 9, 0, 0),
        expected_done_sequence=12,
    )
    with pytest.raises(AttestationError, match="cleanup result"):
        implementation._validate_cleanup_result(
            struct.pack("<IIQQQII", normal_flags, 3, 0xF, 12, 9, 0, 0),
            expected_done_sequence=12,
        )
    with pytest.raises(AttestationError, match="cleanup result"):
        implementation._validate_cleanup_result(
            struct.pack("<IIQQQII", normal_flags, 4, 0xF, 12, 9, 1, 0),
            expected_done_sequence=12,
        )


@pytest.mark.parametrize("observation_mode", ["nonexistent", "mismatched"])
def test_authenticated_armed_frame_requires_fresh_exact_os_observations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observation_mode: str,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        parent, peer = socket.socketpair()
        parent.settimeout(2)
        peer.settimeout(2)
        supervisor = _identity_payload(100, pgid=100, sid=100)
        anchor = _identity_payload(200, pgid=200, sid=200)
        cli = inputs.manifest.cli_executable
        armed = (
            _identity_payload(
                201,
                pgid=200,
                sid=200,
                executable_dev=cli.st_dev,
                executable_ino=cli.st_ino,
                executable_hash=bytes.fromhex(cli.sha256),
            )
            + struct.pack("<QQ", cli.st_dev, cli.st_ino)
            + bytes.fromhex(cli.sha256)
            + bytes.fromhex(cli.path_sha256)
        )
        if observation_mode == "mismatched":
            observed_anchor = implementation._parse_process_identity(anchor)
            monkeypatch.setattr(
                Journal,
                "observe_process",
                lambda _journal, _pid: observed_anchor,
            )

        def publish_authenticated_frames() -> None:
            try:
                for message_type, payload, ack_type in (
                    (1, supervisor, 2),
                    (3, anchor, 4),
                    (5, armed, None),
                ):
                    inputs.journal.append_bootstrap(message_type, payload)
                    peer.sendall(
                        implementation._encode_control_frame(
                            message_type, _NONCE, payload
                        )
                    )
                    if ack_type is not None:
                        implementation._receive_control_frame(peer, _NONCE, ack_type)
            finally:
                peer.close()

        publisher = threading.Thread(target=publish_authenticated_frames)
        publisher.start()
        try:
            with pytest.raises(AttestationError, match="armed CLI identity"):
                implementation._perform_handshake(
                    parent,
                    inputs.journal,
                    _NONCE,
                    cli,
                    False,
                    100,
                )
        finally:
            parent.close()
            publisher.join(timeout=2)
        assert not publisher.is_alive()


def test_authenticated_distinct_anchor_and_armed_member_are_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        parent, peer = socket.socketpair()
        parent.settimeout(2)
        peer.settimeout(2)
        supervisor = _identity_payload(100, pgid=100, sid=100)
        anchor = _identity_payload(200, pgid=200, sid=200)
        cli = inputs.manifest.cli_executable
        armed_member = _identity_payload(
            201,
            pgid=200,
            sid=200,
            executable_dev=cli.st_dev,
            executable_ino=cli.st_ino,
            executable_hash=bytes.fromhex(cli.sha256),
        )
        armed = (
            armed_member
            + struct.pack("<QQ", cli.st_dev, cli.st_ino)
            + bytes.fromhex(cli.sha256)
            + bytes.fromhex(cli.path_sha256)
        )
        observations = {
            200: implementation._parse_process_identity(anchor),
            201: implementation._parse_process_identity(armed_member),
        }
        monkeypatch.setattr(
            Journal,
            "observe_process",
            lambda _journal, pid: observations[pid],
        )

        def publish_authenticated_frames() -> None:
            try:
                for message_type, payload, ack_type in (
                    (1, supervisor, 2),
                    (3, anchor, 4),
                    (5, armed, 6),
                    (7, armed_member, None),
                ):
                    inputs.journal.append_bootstrap(message_type, payload)
                    peer.sendall(
                        implementation._encode_control_frame(
                            message_type, _NONCE, payload
                        )
                    )
                    if ack_type is not None:
                        implementation._receive_control_frame(peer, _NONCE, ack_type)
            finally:
                peer.close()

        publisher = threading.Thread(target=publish_authenticated_frames)
        publisher.start()
        try:
            receipt = implementation._perform_handshake(
                parent,
                inputs.journal,
                _NONCE,
                cli,
                False,
                100,
            )
            assert receipt.control_trace == implementation._EXPECTED_TRACE
            assert observations[200].pid != observations[201].pid
        finally:
            parent.close()
            publisher.join(timeout=2)
        assert not publisher.is_alive()


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
            assert len(receipt.identity_ack_hash) == hashlib.sha256().digest_size
            assert receipt.network_proxy_enabled is True
            assert receipt.authorizes_authentication is False
            original_hash = receipt.identity_ack_hash
            object.__setattr__(receipt, "_identity_ack_hash", b"x" * 32)
            with pytest.raises(AttestationError, match="receipt"):
                _ = receipt.identity_ack_hash
            object.__setattr__(receipt, "_identity_ack_hash", original_hash)
            with pytest.raises(AttestationError, match="sealed"):
                receipt._identity_ack_hash = b"x" * 32
            with pytest.raises((AttestationError, TypeError)):
                copy.copy(receipt)
        finally:
            await transport.close()

        assert not (inputs.descriptors.instance_dir / _JOURNAL_NAME).exists()
        assert not (inputs.descriptors.instance_dir / _WORKDIR_NAME).exists()


def test_forged_handshake_receipt_properties_fail_closed() -> None:
    forged = object.__new__(implementation.SupervisorHandshakeReceipt)
    for name, value in {
        "_token": object(),
        "_control_trace": ("forged",),
        "_canonical_control_types": ("forged",),
        "_canonical_control_sequences": (1,),
        "_canonical_control_hashes": (b"x" * 32,),
        "_identity_ack_sequence": 1,
        "_identity_ack_hash": b"x" * 32,
        "_network_proxy_enabled": True,
    }.items():
        object.__setattr__(forged, name, value)
    try:
        object.__setattr__(forged, "_fingerprint", "0" * 64)
    except AttributeError:
        pass

    with pytest.raises(AttestationError, match="receipt"):
        _ = forged.identity_ack_hash
    with pytest.raises((AttestationError, TypeError, pickle.PicklingError)):
        pickle.dumps(forged)


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
                json.dumps({"type": "user", "message": {"content": "CANARY-USER"}})
                + "\n"
            )
            await transport.end_input()
            stdin_path = inputs.config.cwd.parent / "stdin.txt"
            deadline = time.monotonic() + 2
            while not stdin_path.exists() and time.monotonic() < deadline:
                await anyio.sleep(0.01)
            assert stdin_path.read_text().splitlines() == [initialize.rstrip("\n")]
            assert transport.buffered_user_write_count == 3
            with pytest.raises(
                AttestationError, match="core attestation gate unavailable"
            ):
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


class _ImmediateCloseProcess:
    returncode = 0

    async def wait(self) -> int:
        return 0

    async def aclose(self) -> None:
        return None


def _finish_native_handshake(
    transport: AttestedSupervisorTransport,
    cli: CliExecutableIdentity,
    *,
    supervisor_certification: tuple[bytes, Any] | None = None,
) -> implementation.SupervisorHandshakeReceipt:
    control = transport._control
    journal = transport._journal
    nonce = transport._nonce
    process = transport._process
    assert process is not None
    if supervisor_certification is None:
        supervisor_payload, supervisor_head = implementation._certify_identity(
            control, journal, nonce, 1
        )
    else:
        supervisor_payload, supervisor_head = supervisor_certification
    assert implementation._parse_process_identity(supervisor_payload).pid == process.pid
    canonical = journal.certify_head()
    control.sendall(
        implementation._encode_control_frame(
            2,
            nonce,
            struct.pack("<HBBIQ32s", 1, 0, 0, 0, canonical.sequence, canonical.hash),
        )
    )
    anchor_payload, anchor_head = implementation._certify_identity(
        control, journal, nonce, 3
    )
    control.sendall(implementation._encode_control_frame(4, nonce))
    armed_payload, armed_head = implementation._certify_identity(
        control, journal, nonce, 5
    )
    assert (
        implementation._parse_process_identity(anchor_payload).pid
        != implementation._parse_process_identity(armed_payload[:112]).pid
    )
    control.sendall(implementation._encode_control_frame(6, nonce))
    running_payload, running_head = implementation._certify_identity(
        control, journal, nonce, 7
    )
    running_hash = implementation._parse_process_identity(
        running_payload
    ).executable_hash.hex()
    assert running_hash == cli.sha256
    return implementation.SupervisorHandshakeReceipt._create(
        implementation._RECEIPT_TOKEN,
        sequences=(
            supervisor_head.sequence,
            anchor_head.sequence,
            armed_head.sequence,
            running_head.sequence,
        ),
        hashes=(
            supervisor_head.hash,
            anchor_head.hash,
            armed_head.hash,
            running_head.hash,
        ),
        ack_sequence=canonical.sequence,
        ack_hash=canonical.hash,
        network_proxy_enabled=False,
    )


async def _finish_and_close_failed_handshake(
    transport: AttestedSupervisorTransport,
    cli: CliExecutableIdentity,
    *,
    supervisor_certification: tuple[bytes, Any] | None = None,
) -> None:
    if transport._process is None or transport._control.fileno() < 0:
        return
    receipt = await anyio.to_thread.run_sync(
        lambda: _finish_native_handshake(
            transport,
            cli,
            supervisor_certification=supervisor_certification,
        )
    )
    transport._handshake_receipt = receipt
    transport._cleanup_unconfirmed = False
    await transport.close()


async def test_confirmed_prehandshake_process_is_released(tmp_path: Path) -> None:
    with _launch_inputs(tmp_path) as inputs:
        launch = _prepare(inputs)
        transport = AttestedSupervisorTransport(launch)
        transport._process = _ImmediateCloseProcess()  # type: ignore[assignment]

        await transport.close()

        assert transport._process is None


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


async def test_close_from_cancelled_scope_shields_cleanup_transition(
    tmp_path: Path,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        launch = _prepare(inputs)
        transport = AttestedSupervisorTransport(launch)
        process = _HangingCloseProcess()
        transport._process = process  # type: ignore[assignment]
        transport._ready = True

        with anyio.CancelScope() as scope:
            scope.cancel()
            await transport.close()

        assert transport.is_ready() is False
        assert transport.cleanup_unconfirmed is True
        assert transport._process is process
        launch.close()


async def test_failed_handshake_before_first_frame_retains_task4_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        launch = _prepare(inputs)
        transport = AttestedSupervisorTransport(launch)

        def fail_before_first_frame(*_args: object) -> Never:
            deadline = time.monotonic() + 2
            while (
                inputs.journal.scan().head.record.kind.name != "ACTIVE_READY"
                and time.monotonic() < deadline
            ):
                time.sleep(0.001)
            raise AttestationError("fault before first frame")

        monkeypatch.setattr(
            implementation, "_perform_handshake", fail_before_first_frame
        )
        try:
            with pytest.raises(AttestationError, match="handshake failed closed"):
                await transport.connect()
            owned = transport._process
            assert owned is not None
            assert inputs.journal.scan().head.record.kind.name == "ACTIVE_READY"

            with pytest.raises(AttestationError, match="cleanup is unconfirmed"):
                await transport.close()

            assert transport._process is owned
            assert inputs.journal.scan().head.record.kind.name == "ACTIVE_READY"
            assert transport.cleanup_unconfirmed is True
        finally:
            monkeypatch.undo()
            await _finish_and_close_failed_handshake(
                transport, inputs.manifest.cli_executable
            )


async def test_failed_handshake_after_certified_first_frame_retains_task4_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _launch_inputs(tmp_path) as inputs:
        launch = _prepare(inputs)
        transport = AttestedSupervisorTransport(launch)
        certification: list[tuple[bytes, Any]] = []

        def fail_after_certification(
            control: socket.socket,
            journal: Journal,
            nonce: bytes,
            *_args: object,
        ) -> Never:
            certification.append(
                implementation._certify_identity(control, journal, nonce, 1)
            )
            raise AttestationError("fault after certified first frame")

        monkeypatch.setattr(
            implementation, "_perform_handshake", fail_after_certification
        )
        try:
            with pytest.raises(AttestationError, match="handshake failed closed"):
                await transport.connect()
            assert len(certification) == 1
            owned = transport._process
            assert owned is not None
            assert inputs.journal.scan().head.record.kind.name == "ACTIVE_READY"

            with pytest.raises(AttestationError, match="cleanup is unconfirmed"):
                await transport.close()

            assert transport._process is owned
            assert inputs.journal.scan().head.record.kind.name == "ACTIVE_READY"
            assert transport.cleanup_unconfirmed is True
        finally:
            monkeypatch.undo()
            await _finish_and_close_failed_handshake(
                transport,
                inputs.manifest.cli_executable,
                supervisor_certification=(certification[0] if certification else None),
            )


def test_default_sdk_transport_is_rejected(tmp_path: Path) -> None:
    with _launch_inputs(tmp_path) as inputs:
        launch = _prepare(inputs)
        try:
            with pytest.raises(AttestationError, match="custom Task 6 transport"):
                build_attested_sdk_client(launch)
        finally:
            launch.close()
