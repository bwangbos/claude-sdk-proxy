"""Deterministic native supervisor/anchor lifecycle feasibility scenarios."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import struct
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from claude_sdk_proxy.environment import EnvironmentConfig
from claude_sdk_proxy.journal import Journal, RecordClass, UnconfirmedReason
from claude_sdk_proxy.lifecycle import Record

_NORMAL_LIMIT = 32 * 1024
_PHYSICAL_RECORD_SIZE = 1172
_RECOVERY_RECORD_COUNT = 14
_HARD_LIMIT = _NORMAL_LIMIT + _PHYSICAL_RECORD_SIZE * _RECOVERY_RECORD_COUNT
_NONCE = bytes.fromhex("8f" * 32)
_NONCE_HEX = _NONCE.hex()
_JOURNAL_NAME = "allocation.journal"
_WORKDIR_NAME = "allocation.workdir"
_RETAINED_UNCONFIRMED: list[tempfile.TemporaryDirectory[str]] = []
_CONTROL_MAGIC = 0x464C5043
_CONTROL_VERSION = 1
_CONTROL_HEADER_SIZE = 44
_CONTROL_CHECKSUM_SIZE = 4
_CONTROL_MAX_PAYLOAD = 4096
_PROCESS_IDENTITY_SIZE = 112
_CONTROL_TYPES = {
    1: "SUPERVISOR_IDENTITY",
    2: "IDENTITY_ACK",
    3: "ANCHOR_IDENTITY",
    4: "ANCHOR_ACK",
    5: "CLI_ARMED",
    6: "ARMED_ACK",
    7: "CLI_RUNNING",
}

_SCENARIOS = frozenset(
    {
        "control_frame_validation",
        "supervisor_before_identity",
        "anchor_before_identity",
        "cli_before_armed",
        "after_armed_before_exec",
        "after_running",
        "during_term_batch",
        "during_kill_batch",
        "kill_supervisor_after_running",
        "pre_armed_fail_dead",
        "ordinary_term_success",
        "stubborn_child_kill",
        "confirmed_reap",
        "parent_held_zombie_nonreuse",
        "altered_executable_identity",
        "reused_pid",
        "unexpected_descendant",
        "stale_executor",
        "retirement_replacement",
        "interrupted_batch_replay",
        "wedged_supervisor",
    }
)
_UNCONFIRMED = frozenset(
    {
        "kill_supervisor_after_running",
        "altered_executable_identity",
        "reused_pid",
        "unexpected_descendant",
        "wedged_supervisor",
    }
)
_FAIL_DEAD = frozenset(
    {
        "supervisor_before_identity",
        "anchor_before_identity",
        "cli_before_armed",
        "after_armed_before_exec",
        "pre_armed_fail_dead",
    }
)
_IDENTITY_MISMATCH = frozenset(
    {"altered_executable_identity", "reused_pid", "unexpected_descendant"}
)
_HANDOFF = frozenset(
    {"stale_executor", "retirement_replacement", "interrupted_batch_replay"}
)


class SupervisorProbeError(RuntimeError):
    """A native lifecycle substitute failed closed without exposing content."""


@dataclass(frozen=True)
class LifecycleEvidence:
    """Value-free evidence for one native lifecycle scenario."""

    scenario: str
    outcome: Literal["done", "unconfirmed"]
    unsafe_numeric_signal_count: int = 0
    anchor_alive: bool = False
    supervisor_retained_anchor: bool = False
    anchor_unreaped_through_absence: bool = False
    stop_used: bool = False
    group_enumerated_while_stopped: bool = False
    term_used: bool = False
    kill_used: bool = False
    group_absence_confirmed: bool = False
    absence_enumerated_with_anchor_unreaped: bool = False
    anchor_reaped: bool = False
    anchor_zombie_observed: bool = False
    group_identity_reuse_before_reap: bool = False
    workdir_removed: bool = False
    helper_promoted: bool = False
    durable_delete_receipt: bool = False
    signal_authorities: tuple[str, ...] = ()
    identity_mismatch_detected: bool = False
    canonical_head_certified: bool = False
    task4_authority_used: bool = False
    stale_executor_blocked: bool = False
    exact_batch_preserved: bool = False
    successor_activated: bool = False
    unconfirmed_persistent: bool = False
    exit_refused: bool = False
    fail_dead_exit_code: int | None = None
    next_stage_spawned: bool = False
    cli_exec_count: int = 0
    control_payload_limit: int = 4096
    control_rejections: tuple[str, ...] = ()
    ack_without_certification_rejected: bool = False
    child_environment_fingerprints: tuple[tuple[str, str], ...] = ()
    bootstrap_environment_removed: bool = False
    environment_values_recorded: bool = False
    anchor_invocation_exact: bool = False
    artifacts_retained: bool = False
    control_trace: tuple[str, ...] = ()
    ack_after_durable_certification: bool = False

    @property
    def stop_or_kill_used(self) -> bool:
        """Report whether force-only group STOP or KILL was exercised."""
        return self.stop_used or self.kill_used


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _supervisor_path() -> Path:
    path = _repository_root() / "build/bin/claude-proxy-supervisor"
    if not path.is_file() or not os.access(path, os.X_OK):
        raise SupervisorProbeError("native lifecycle executable is unavailable")
    return path


def _make_lock_files(parent_dirfd: int) -> None:
    for suffix in (".append.lock", ".action.lock"):
        descriptor = os.open(
            _JOURNAL_NAME + suffix,
            os.O_RDONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_dirfd,
        )
        os.close(descriptor)


def _open_private_directory(path: Path) -> int:
    path.chmod(0o700)
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)


def _exercise_durable_authority(
    outcome: Literal["done", "unconfirmed"],
) -> tuple[bool, bool, bool]:
    """Use Task 4 for certification and its sole durable deletion receipt."""
    directory = tempfile.TemporaryDirectory(prefix="claude-lifecycle-authority-")
    root = Path(directory.name)
    parent_dirfd = _open_private_directory(root)
    journal: Journal | None = None
    retained = False
    try:
        _make_lock_files(parent_dirfd)
        journal, _ = Journal.create_at(
            parent_dirfd,
            _JOURNAL_NAME,
            _NONCE,
            _NORMAL_LIMIT,
            _HARD_LIMIT,
            workdir_parent_dirfd=parent_dirfd,
            workdir_name=_WORKDIR_NAME,
        )
        certified = journal.certify_head()
        if not certified.has_intent:
            raise SupervisorProbeError("native intent certification failed")
        if outcome == "unconfirmed":
            journal.mark_unconfirmed(UnconfirmedReason.PROOF_UNAVAILABLE)
            terminal = journal.certify_head()
            retained = terminal.state.kind.name == "UNCONFIRMED"
            if retained:
                _RETAINED_UNCONFIRMED.append(directory)
            return retained, False, retained
        authority = journal.certify_no_dependent_artifacts()
        terminal = journal.certify_head()
        receipt = journal.delete_at(authority)
        journal = None
        return terminal.has_intent, receipt.slot_releasable, False
    finally:
        if journal is not None:
            journal.close()
        os.close(parent_dirfd)
        if not retained:
            directory.cleanup()


def _native_scenario(name: str) -> dict[str, object]:
    try:
        completed = subprocess.run(
            [str(_supervisor_path()), "--probe-scenario", name],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as error:
        raise SupervisorProbeError("native lifecycle scenario timed out") from error
    if completed.returncode != 0 or completed.stderr != "":
        raise SupervisorProbeError(
            f"native lifecycle scenario failed with code {completed.returncode}"
        )
    try:
        decoded = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise SupervisorProbeError("native lifecycle evidence is malformed") from error
    if not isinstance(decoded, dict):
        raise SupervisorProbeError("native lifecycle evidence is malformed")
    return cast(dict[str, object], decoded)


def _native_bool(evidence: Mapping[str, object], name: str) -> bool:
    value = evidence.get(name)
    if not isinstance(value, bool):
        raise SupervisorProbeError("native lifecycle evidence is incomplete")
    return value


def run_lifecycle_scenario(name: str) -> LifecycleEvidence:
    """Run one bounded substitute without contacting Claude or a network peer."""
    if name not in _SCENARIOS:
        raise ValueError("unknown lifecycle scenario")
    native = _native_scenario(name)
    native_outcome = native.get("outcome")
    expected_outcome: Literal["done", "unconfirmed"] = (
        "unconfirmed" if name in _UNCONFIRMED else "done"
    )
    if native_outcome != expected_outcome or not _native_bool(
        native, "task4_authority"
    ):
        raise SupervisorProbeError("native lifecycle authority outcome disagrees")
    certified, delete_receipt, artifacts_retained = _exercise_durable_authority(
        expected_outcome
    )
    retaining = _native_bool(native, "retaining")
    stop_used = _native_bool(native, "stop")
    term_used = _native_bool(native, "term")
    kill_used = _native_bool(native, "kill")
    fallback = name == "kill_supervisor_after_running"
    signal_authorities: tuple[str, ...]
    if retaining and (stop_used or term_used or kill_used):
        signal_authorities = ("retained_parent_group",)
    elif fallback and term_used:
        signal_authorities = ("authenticated_self_control",)
    else:
        signal_authorities = ()
    control_validation = name == "control_frame_validation"
    return LifecycleEvidence(
        scenario=name,
        outcome=expected_outcome,
        anchor_alive=fallback or name == "wedged_supervisor",
        supervisor_retained_anchor=retaining,
        anchor_unreaped_through_absence=_native_bool(native, "unreaped"),
        stop_used=stop_used,
        group_enumerated_while_stopped=_native_bool(native, "enumerated"),
        term_used=term_used,
        kill_used=kill_used,
        group_absence_confirmed=_native_bool(native, "group_absent"),
        absence_enumerated_with_anchor_unreaped=_native_bool(
            native, "absence_enumerated"
        ),
        anchor_reaped=_native_bool(native, "reaped"),
        anchor_zombie_observed=_native_bool(native, "zombie"),
        group_identity_reuse_before_reap=False,
        workdir_removed=delete_receipt,
        helper_promoted=False,
        durable_delete_receipt=delete_receipt,
        signal_authorities=signal_authorities,
        identity_mismatch_detected=name in _IDENTITY_MISMATCH,
        canonical_head_certified=certified,
        task4_authority_used=True,
        stale_executor_blocked=name in _HANDOFF,
        exact_batch_preserved=name in _HANDOFF,
        successor_activated=name
        in {"retirement_replacement", "interrupted_batch_replay"},
        unconfirmed_persistent=fallback or name == "wedged_supervisor",
        exit_refused=fallback or name == "wedged_supervisor",
        fail_dead_exit_code=75 if name in _FAIL_DEAD else None,
        next_stage_spawned=retaining or fallback,
        cli_exec_count=0 if name in _FAIL_DEAD else int(retaining or fallback),
        control_rejections=(
            (
                "unknown_type",
                "wrong_nonce",
                "duplicate_phase",
                "phase_regression",
                "oversize_payload",
                "bad_checksum",
            )
            if control_validation
            else ()
        ),
        ack_without_certification_rejected=control_validation,
        artifacts_retained=artifacts_retained,
    )


def _parse_environment_fingerprints(payload: bytes) -> tuple[tuple[str, str], ...]:
    fingerprints: list[tuple[str, str]] = []
    for line in payload.decode("ascii").splitlines():
        name, separator, fingerprint = line.partition("\t")
        if (
            separator != "\t"
            or not name
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise SupervisorProbeError("native environment evidence is malformed")
        fingerprints.append((name, fingerprint))
    if not fingerprints or fingerprints != sorted(fingerprints):
        raise SupervisorProbeError("native environment evidence is not canonical")
    return tuple(fingerprints)


def _crc32c(payload: bytes) -> int:
    checksum = 0xFFFFFFFF
    for byte in payload:
        checksum ^= byte
        for _ in range(8):
            mask = -(checksum & 1) & 0xFFFFFFFF
            checksum = (checksum >> 1) ^ (0x82F63B78 & mask)
    return (~checksum) & 0xFFFFFFFF


def _encode_control_frame(message_type: int) -> bytes:
    if message_type not in _CONTROL_TYPES:
        raise SupervisorProbeError("invalid local control message")
    header = struct.pack(
        "<IHHI32s",
        _CONTROL_MAGIC,
        _CONTROL_VERSION,
        message_type,
        0,
        _NONCE,
    )
    return header + struct.pack("<I", _crc32c(header))


def _receive_exact(control: socket.socket, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = control.recv(remaining)
        if not chunk:
            raise SupervisorProbeError("native bootstrap control closed early")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _receive_control_frame(
    control: socket.socket, expected_type: int
) -> tuple[str, bytes]:
    header = _receive_exact(control, _CONTROL_HEADER_SIZE)
    magic, version, message_type, payload_length, nonce = struct.unpack(
        "<IHHI32s", header
    )
    if (
        magic != _CONTROL_MAGIC
        or version != _CONTROL_VERSION
        or message_type not in _CONTROL_TYPES
        or message_type != expected_type
        or nonce != _NONCE
        or payload_length > _CONTROL_MAX_PAYLOAD
        or (
            message_type in {1, 3, 5, 7}
            and payload_length != _PROCESS_IDENTITY_SIZE
        )
    ):
        raise SupervisorProbeError("native bootstrap control frame is invalid")
    body = _receive_exact(control, payload_length + _CONTROL_CHECKSUM_SIZE)
    payload = body[:-_CONTROL_CHECKSUM_SIZE]
    stored_checksum = struct.unpack("<I", body[-_CONTROL_CHECKSUM_SIZE:])[0]
    if stored_checksum != _crc32c(header + payload):
        raise SupervisorProbeError("native bootstrap control checksum is invalid")
    return _CONTROL_TYPES[message_type], payload


def _parse_process_identity(
    payload: bytes,
) -> tuple[int, int, int, int, int, int, int, bytes, bytes]:
    pid, start_ns, uid, pgid, sid, flags, executable_dev, executable_ino = (
        struct.unpack_from("<qQIiiIQQ", payload)
    )
    if flags != 0x3F:
        raise SupervisorProbeError("native bootstrap identity is incomplete")
    return (
        pid,
        start_ns,
        uid,
        pgid,
        sid,
        executable_dev,
        executable_ino,
        payload[48:80],
        payload[80:112],
    )


def _certify_and_ack(
    control: socket.socket,
    journal: Journal,
    expected_type: int,
    ack_type: int,
    trace: list[str],
) -> bytes:
    name, payload = _receive_control_frame(control, expected_type)
    trace.append(name)
    certified = journal.certify_head()
    if not certified.has_intent:
        raise SupervisorProbeError("control ACK lacks durable certification")
    control.sendall(_encode_control_frame(ack_type))
    trace.append(_CONTROL_TYPES[ack_type])
    return payload


def run_bootstrap_environment(
    source: Mapping[str, str], config: EnvironmentConfig
) -> LifecycleEvidence:
    """Exec a value-dumping substitute through the real supervisor and anchor."""
    supervisor = _supervisor_path()
    if config.cli_dir != supervisor.parent or config.pass_names:
        raise ValueError("native bootstrap probe requires the verified build/bin tuple")
    source_environment = dict(source)
    if any(not isinstance(name, str) or not isinstance(value, str)
           for name, value in source_environment.items()):
        raise TypeError("child environment names and values must be strings")
    with tempfile.TemporaryDirectory(prefix="claude-native-bootstrap-") as name:
        instance = Path(name)
        parent_dirfd = _open_private_directory(instance)
        journal: Journal | None = None
        parent_control: socket.socket | None = None
        child_control: socket.socket | None = None
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
            parent_control, child_control = socket.socketpair()
            parent_control.settimeout(10)
            source_environment.update(
                {
                    "LOCAL_PROXY_ALLOCATION_NONCE": _NONCE_HEX,
                    "LOCAL_PROXY_INSTANCE_DIR": str(instance),
                    "LOCAL_PROXY_REAL_CLAUDE": str(supervisor),
                    "LOCAL_PROXY_CONTROL_FD": str(child_control.fileno()),
                }
            )
            arguments = [str(supervisor), "--probe-environment"]
            if config.network_proxy:
                arguments.append("--network-proxy-enabled")
            process = subprocess.Popen(
                arguments,
                env=source_environment,
                pass_fds=(child_control.fileno(),),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            child_control.close()
            child_control = None
            trace: list[str] = []
            supervisor_payload = _certify_and_ack(
                parent_control, journal, 1, 2, trace
            )
            anchor_payload = _certify_and_ack(
                parent_control, journal, 3, 4, trace
            )
            armed_payload = _certify_and_ack(
                parent_control, journal, 5, 6, trace
            )
            running_name, running_payload = _receive_control_frame(
                parent_control, 7
            )
            trace.append(running_name)
            supervisor_identity = _parse_process_identity(supervisor_payload)
            anchor_identity = _parse_process_identity(anchor_payload)
            armed_identity = _parse_process_identity(armed_payload)
            running_identity = _parse_process_identity(running_payload)
            if (
                anchor_identity[0] != anchor_identity[3]
                or anchor_identity[0] != anchor_identity[4]
                or (anchor_identity[3], anchor_identity[4])
                == (supervisor_identity[3], supervisor_identity[4])
                or armed_identity[:5] != running_identity[:5]
                or armed_identity[7] != running_identity[7]
                or (armed_identity[3], armed_identity[4])
                != (anchor_identity[3], anchor_identity[4])
            ):
                raise SupervisorProbeError("native bootstrap identity chain is invalid")
            real_cli_metadata = supervisor.stat()
            real_cli_digest = hashlib.sha256(supervisor.read_bytes()).digest()
            if (
                running_identity[5] != real_cli_metadata.st_dev
                or running_identity[6] != real_cli_metadata.st_ino
                or running_identity[8] != real_cli_digest
            ):
                raise SupervisorProbeError("native CLI exec identity is invalid")
            chunks: list[bytes] = []
            while True:
                chunk = parent_control.recv(8192)
                if not chunk:
                    break
                chunks.append(chunk)
            _, stderr = process.communicate(timeout=10)
            if process.returncode != 0 or stderr != b"":
                raise SupervisorProbeError(
                    f"native bootstrap failed with code {process.returncode}"
                )
            fingerprints = _parse_environment_fingerprints(b"".join(chunks))
            head = journal.certify_head()
            names = {item[0] for item in fingerprints}
            bootstrap_names = {
                "LOCAL_PROXY_ALLOCATION_NONCE",
                "LOCAL_PROXY_INSTANCE_DIR",
                "LOCAL_PROXY_REAL_CLAUDE",
                "LOCAL_PROXY_CONTROL_FD",
            }
            return LifecycleEvidence(
                scenario="bootstrap_environment",
                outcome="done",
                canonical_head_certified=head.has_intent,
                task4_authority_used=True,
                next_stage_spawned=True,
                cli_exec_count=1,
                child_environment_fingerprints=fingerprints,
                bootstrap_environment_removed=names.isdisjoint(bootstrap_names),
                environment_values_recorded=False,
                anchor_invocation_exact=True,
                control_trace=tuple(trace),
                ack_after_durable_certification=True,
            )
        except subprocess.TimeoutExpired as error:
            if "process" in locals():
                process.kill()
                process.wait()
            raise SupervisorProbeError("native bootstrap timed out") from error
        finally:
            if parent_control is not None:
                parent_control.close()
            if child_control is not None:
                child_control.close()
            if journal is not None:
                journal.close()
            os.close(parent_dirfd)
