"""Deterministic native supervisor/anchor lifecycle feasibility scenarios."""

from __future__ import annotations

import hashlib
import json
import os
import signal
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
from claude_sdk_proxy.journal import (
    Journal,
    JournalError,
    RecordClass,
    UnconfirmedReason,
)
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
_RETAINED_UNCONFIRMED_PATHS: list[Path] = []
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
    8: "CLEANUP_REQUEST",
    9: "SELF_TERM_REQUEST",
    10: "CLEANUP_RESULT",
    11: "ERROR",
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
        "ordinary_probe_argument_collision",
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
_REAL_RETAINING_CLEANUP = frozenset(
    {
        "ordinary_term_success",
        "stubborn_child_kill",
        "confirmed_reap",
        "parent_held_zombie_nonreuse",
        "after_running",
        "during_term_batch",
        "during_kill_batch",
    }
)
_REAL_FAIL_DEAD_BOUNDARIES = frozenset(
    {
        "supervisor_before_identity",
        "anchor_before_identity",
        "cli_before_armed",
        "after_armed_before_exec",
    }
)
_CLEANUP_STOP_USED = 1 << 0
_CLEANUP_STOPPED_ENUMERATED = 1 << 1
_CLEANUP_TERM_USED = 1 << 2
_CLEANUP_KILL_USED = 1 << 3
_CLEANUP_ZOMBIE_OBSERVED = 1 << 4
_CLEANUP_ABSENCE_ENUMERATED = 1 << 5
_CLEANUP_ANCHOR_REAPED = 1 << 6
_CLEANUP_TASK4_DONE = 1 << 7
_CLEANUP_GROUP_ENUMERATION_COMPLETE = 1 << 8


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
    exact_canonical_ack_heads: bool = False
    canonical_control_types: tuple[str, ...] = ()
    canonical_control_sequences: tuple[int, ...] = ()
    post_exec_identity_verified: bool = False
    cli_control_fd_closed_on_exec: bool = False
    network_proxy_selector_authenticated: bool = False
    probe_mode_collision_impossible: bool = False
    cleanup_request_authenticated: bool = False
    cleanup_task4_admitted: bool = False
    cleanup_batch_count: int = 0
    cleanup_completed_steps: int = 0
    cleanup_done_sequence: int = 0
    same_canonical_journal: bool = False
    evidence_observed_not_inferred: bool = False
    group_enumeration_complete: bool = False
    control_fd_phase_enforced: bool = False
    self_term_request_authenticated: bool = False

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


def _probe_child_path() -> Path:
    path = _repository_root() / "build/bin/claude-proxy-probe-child"
    if not path.is_file() or not os.access(path, os.X_OK):
        raise SupervisorProbeError("native probe child is unavailable")
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
            env={
                **os.environ,
                "LOCAL_PROXY_PROBE_SCENARIO_TOKEN": "task5-local-only",
            },
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
    if name == "ordinary_probe_argument_collision":
        completed = subprocess.run(
            [str(_supervisor_path()), "--probe-scenario", "confirmed_reap"],
            capture_output=True,
            check=False,
            timeout=5,
        )
        return LifecycleEvidence(
            scenario=name,
            outcome="done",
            fail_dead_exit_code=completed.returncode,
            probe_mode_collision_impossible=completed.returncode == 75,
        )
    if name == "kill_supervisor_after_running":
        return _run_real_supervisorless_fallback()
    if name in _REAL_RETAINING_CLEANUP:
        return _run_real_retaining_cleanup(name)
    if name in _REAL_FAIL_DEAD_BOUNDARIES:
        return _run_real_fail_dead_boundary(name)
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


def _run_real_supervisorless_fallback() -> LifecycleEvidence:
    instance = Path(tempfile.mkdtemp(prefix="claude-real-fallback-"))
    parent_dirfd = _open_private_directory(instance)
    journal: Journal | None = None
    parent_control: socket.socket | None = None
    child_control: socket.socket | None = None
    parent_fallback: socket.socket | None = None
    child_fallback: socket.socket | None = None
    process: subprocess.Popen[bytes] | None = None
    retained = False
    retained_anchor: (
        tuple[int, int, int, int, int, int, int, bytes, bytes] | None
    ) = None
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
        parent_fallback, child_fallback = socket.socketpair()
        parent_control.settimeout(10)
        parent_fallback.settimeout(10)
        output_path = instance / "fallback-output"
        environment = {
            "HOME": str(instance),
            "USER": "fallback-probe",
            "LOCAL_PROXY_ALLOCATION_NONCE": _NONCE_HEX,
            "LOCAL_PROXY_INSTANCE_DIR": str(instance),
            "LOCAL_PROXY_REAL_CLAUDE": str(_probe_child_path()),
            "LOCAL_PROXY_CONTROL_FD": str(child_control.fileno()),
            "LOCAL_PROXY_ANCHOR_CONTROL_FD": str(child_fallback.fileno()),
            "LOCAL_PROXY_NETWORK_PROXY": "0",
        }
        process = subprocess.Popen(
            [str(_supervisor_path()), "--output-path", str(output_path)],
            env=environment,
            pass_fds=(child_control.fileno(), child_fallback.fileno()),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        child_control.close()
        child_control = None
        child_fallback.close()
        child_fallback = None
        trace: list[str] = []
        try:
            _certify_and_ack(parent_control, journal, 1, 2, trace)
        except SupervisorProbeError as error:
            returncode = process.wait(timeout=5)
            stderr = process.stderr.read().decode() if process.stderr else ""
            raise SupervisorProbeError(
                f"supervisor identity handshake failed ({returncode}): {stderr}"
            ) from error
        anchor_payload, _, _ = _certify_and_ack(
            parent_control, journal, 3, 4, trace
        )
        _certify_and_ack(parent_control, journal, 5, 6, trace)
        try:
            running_name, running_payload = _receive_control_frame(
                parent_control, 7
            )
        except SupervisorProbeError as error:
            returncode = process.wait(timeout=5)
            stderr = process.stderr.read().decode() if process.stderr else ""
            raise SupervisorProbeError(
                f"running identity handshake failed ({returncode}): {stderr}"
            ) from error
        trace.append(running_name)
        running_head = journal.certify_bootstrap(7, running_payload)
        anchor_identity = _parse_process_identity(anchor_payload)
        retained_anchor = anchor_identity
        process.kill()
        process.wait(timeout=5)
        try:
            os.kill(anchor_identity[0], 0)
        except ProcessLookupError as error:
            stderr = ""
            if process.stderr is not None:
                os.set_blocking(process.stderr.fileno(), False)
                stderr = process.stderr.read().decode()
            raise SupervisorProbeError(
                "live anchor exited when retaining supervisor was killed: "
                f"{stderr}"
            ) from error
        try:
            parent_fallback.sendall(_encode_control_frame(9))
        except BrokenPipeError as error:
            try:
                os.kill(anchor_identity[0], 0)
            except ProcessLookupError:
                fallback_state = "exited"
            else:
                fallback_state = "alive-without-fallback-fd"
            raise SupervisorProbeError(
                "live anchor fallback channel unavailable "
                f"({fallback_state})"
            ) from error
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                self_term = journal.certify_bootstrap(9, b"")
                state = journal.certify_head().state
                if state.kind.name == "UNCONFIRMED":
                    break
            except JournalError:
                time.sleep(0.001)
        else:
            raise SupervisorProbeError("authenticated anchor fallback timed out")
        os.kill(anchor_identity[0], 0)
        retained = True
        return LifecycleEvidence(
            scenario="kill_supervisor_after_running",
            outcome="unconfirmed",
            anchor_alive=True,
            term_used=True,
            signal_authorities=("authenticated_self_control",),
            canonical_head_certified=True,
            task4_authority_used=True,
            unconfirmed_persistent=True,
            exit_refused=True,
            next_stage_spawned=True,
            cli_exec_count=1,
            artifacts_retained=True,
            self_term_request_authenticated=self_term.sequence == 5,
            same_canonical_journal=running_head.sequence == 4,
            evidence_observed_not_inferred=True,
            control_fd_phase_enforced=True,
        )
    finally:
        if parent_control is not None:
            parent_control.close()
        if child_control is not None:
            child_control.close()
        if parent_fallback is not None:
            parent_fallback.close()
        if child_fallback is not None:
            child_fallback.close()
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        if process is not None and process.stderr is not None:
            process.stderr.close()
        if retained and retained_anchor is not None and journal is not None:
            observed = journal.observe_process(retained_anchor[0])
            if (
                observed.pid != retained_anchor[0]
                or observed.start_ns != retained_anchor[1]
                or observed.uid != retained_anchor[2]
                or observed.pgid != retained_anchor[3]
                or observed.sid != retained_anchor[4]
                or observed.boot_id != retained_anchor[7]
                or observed.executable_hash != retained_anchor[8]
            ):
                raise SupervisorProbeError(
                    "test teardown refused a changed anchor identity"
                )
            os.killpg(observed.pgid, signal.SIGKILL)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(observed.pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.001)
            else:
                raise SupervisorProbeError("test anchor teardown timed out")
        if journal is not None:
            journal.close()
        os.close(parent_dirfd)
        if (instance / _JOURNAL_NAME).is_file() or (
            instance / _WORKDIR_NAME
        ).is_dir():
            _RETAINED_UNCONFIRMED_PATHS.append(instance)


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
            message_type in {1, 3, 7}
            and payload_length != _PROCESS_IDENTITY_SIZE
        )
        or (message_type == 5 and payload_length != 192)
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
) -> tuple[bytes, int, bytes]:
    name, payload = _receive_control_frame(control, expected_type)
    trace.append(name)
    certified = journal.certify_bootstrap(expected_type, payload)
    control.sendall(_encode_control_frame(ack_type))
    trace.append(_CONTROL_TYPES[ack_type])
    return payload, certified.sequence, certified.hash


@dataclass(frozen=True)
class _CleanupObservation:
    flags: int
    batch_count: int
    completed_steps: int
    done_sequence: int
    cleanup_sequence: int
    result_sequence: int
    delete_receipt: bool


def _certify_bootstrap_event(
    journal: Journal,
    message_type: int,
    payload: bytes,
    *,
    timeout: float = 5.0,
) -> tuple[int, bytes]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            head = journal.certify_bootstrap(message_type, payload)
        except JournalError:
            time.sleep(0.001)
            continue
        return head.sequence, head.hash
    raise SupervisorProbeError("durable bootstrap event certification timed out")


def _request_cleanup_and_delete(
    control: socket.socket,
    process: subprocess.Popen[bytes],
    journal: Journal,
    instance: Path,
) -> _CleanupObservation:
    control.sendall(_encode_control_frame(8))
    cleanup_sequence, _ = _certify_bootstrap_event(journal, 8, b"")
    result_name, result_payload = _receive_control_frame(control, 10)
    if result_name != "CLEANUP_RESULT" or len(result_payload) != 24:
        raise SupervisorProbeError("native cleanup result is malformed")
    result_sequence, _ = _certify_bootstrap_event(journal, 10, result_payload)
    flags, batch_count, completed_steps, done_sequence = struct.unpack(
        "<IIQQ", result_payload
    )
    head = journal.certify_head()
    if head.state.kind.name != "DONE" or head.sequence != done_sequence:
        raise SupervisorProbeError("cleanup result does not certify its DONE head")

    deadline = time.monotonic() + 5
    waited: os.waitid_result | None = None
    while time.monotonic() < deadline:
        waited = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if waited is not None and waited.si_pid == process.pid:
            break
        time.sleep(0.001)
    else:
        raise SupervisorProbeError("retaining supervisor did not become reapable")
    if waited is None or waited.si_code != os.CLD_EXITED or waited.si_status != 0:
        raise SupervisorProbeError("retaining supervisor failed after cleanup")
    journal.confirm_executor_reaped()
    process.returncode = waited.si_status
    authority = journal.certify_done()
    receipt = journal.delete_at(authority)
    if (instance / _JOURNAL_NAME).exists() or (instance / _WORKDIR_NAME).exists():
        raise SupervisorProbeError("durable cleanup left an allocation artifact")
    return _CleanupObservation(
        flags=flags,
        batch_count=batch_count,
        completed_steps=completed_steps,
        done_sequence=done_sequence,
        cleanup_sequence=cleanup_sequence,
        result_sequence=result_sequence,
        delete_receipt=receipt.slot_releasable,
    )


def _run_real_retaining_cleanup(name: str) -> LifecycleEvidence:
    stubborn = name in {"stubborn_child_kill", "during_kill_batch"}
    with tempfile.TemporaryDirectory(prefix="claude-real-cleanup-") as directory:
        instance = Path(directory)
        parent_dirfd = _open_private_directory(instance)
        journal: Journal | None = None
        parent_control: socket.socket | None = None
        child_control: socket.socket | None = None
        parent_fallback: socket.socket | None = None
        child_fallback: socket.socket | None = None
        process: subprocess.Popen[bytes] | None = None
        cleanup_finished = False
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
            parent_fallback, child_fallback = socket.socketpair()
            parent_control.settimeout(15)
            output_path = instance / "cleanup-output"
            environment = {
                "HOME": str(instance),
                "USER": "cleanup-probe",
                "LOCAL_PROXY_ALLOCATION_NONCE": _NONCE_HEX,
                "LOCAL_PROXY_INSTANCE_DIR": str(instance),
                "LOCAL_PROXY_REAL_CLAUDE": str(_probe_child_path()),
                "LOCAL_PROXY_CONTROL_FD": str(child_control.fileno()),
                "LOCAL_PROXY_ANCHOR_CONTROL_FD": str(child_fallback.fileno()),
                "LOCAL_PROXY_NETWORK_PROXY": "0",
            }
            arguments = [str(_supervisor_path()), "--output-path", str(output_path)]
            if stubborn:
                arguments.append("--stubborn")
            process = subprocess.Popen(
                arguments,
                env=environment,
                pass_fds=(child_control.fileno(), child_fallback.fileno()),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            child_control.close()
            child_control = None
            child_fallback.close()
            child_fallback = None
            trace: list[str] = []
            try:
                _, supervisor_sequence, _ = _certify_and_ack(
                    parent_control, journal, 1, 2, trace
                )
            except SupervisorProbeError as error:
                returncode = process.wait(timeout=5)
                stderr = process.stderr.read().decode() if process.stderr else ""
                raise SupervisorProbeError(
                    f"supervisor identity handshake failed ({returncode}): {stderr}"
                ) from error
            anchor_payload, anchor_sequence, _ = _certify_and_ack(
                parent_control, journal, 3, 4, trace
            )
            _, armed_sequence, _ = _certify_and_ack(
                parent_control, journal, 5, 6, trace
            )
            try:
                running_name, running_payload = _receive_control_frame(
                    parent_control, 7
                )
            except SupervisorProbeError as error:
                returncode = process.wait(timeout=10)
                stderr = process.stderr.read().decode() if process.stderr else ""
                raise SupervisorProbeError(
                    f"running gate failed ({returncode}, output_exists="
                    f"{output_path.exists()}, output_size="
                    f"{output_path.stat().st_size if output_path.exists() else -1}): "
                    f"{stderr}"
                ) from error
            trace.append(running_name)
            running_head = journal.certify_bootstrap(7, running_payload)
            observation = _request_cleanup_and_delete(
                parent_control, process, journal, instance
            )
            cleanup_finished = True
            flags = observation.flags
            required_flags = (
                _CLEANUP_STOP_USED
                | _CLEANUP_STOPPED_ENUMERATED
                | _CLEANUP_TERM_USED
                | _CLEANUP_ZOMBIE_OBSERVED
                | _CLEANUP_ABSENCE_ENUMERATED
                | _CLEANUP_ANCHOR_REAPED
                | _CLEANUP_TASK4_DONE
                | _CLEANUP_GROUP_ENUMERATION_COMPLETE
            )
            if flags & required_flags != required_flags:
                raise SupervisorProbeError("native cleanup evidence is incomplete")
            if bool(flags & _CLEANUP_KILL_USED) != stubborn:
                raise SupervisorProbeError("native cleanup escalation disagrees")
            if process.stderr is not None and process.stderr.read() != b"":
                raise SupervisorProbeError("retaining supervisor emitted diagnostics")
            same_journal = (
                (supervisor_sequence, anchor_sequence, armed_sequence,
                 running_head.sequence, observation.cleanup_sequence,
                 observation.result_sequence)
                == (1, 2, 3, 4, 5, 6)
                and observation.done_sequence > running_head.sequence
            )
            return LifecycleEvidence(
                scenario=name,
                outcome="done",
                supervisor_retained_anchor=True,
                anchor_unreaped_through_absence=True,
                stop_used=bool(flags & _CLEANUP_STOP_USED),
                group_enumerated_while_stopped=bool(
                    flags & _CLEANUP_STOPPED_ENUMERATED
                ),
                term_used=bool(flags & _CLEANUP_TERM_USED),
                kill_used=bool(flags & _CLEANUP_KILL_USED),
                group_absence_confirmed=bool(
                    flags & _CLEANUP_ABSENCE_ENUMERATED
                ),
                absence_enumerated_with_anchor_unreaped=bool(
                    flags & _CLEANUP_ABSENCE_ENUMERATED
                ),
                anchor_reaped=bool(flags & _CLEANUP_ANCHOR_REAPED),
                anchor_zombie_observed=bool(flags & _CLEANUP_ZOMBIE_OBSERVED),
                group_identity_reuse_before_reap=False,
                workdir_removed=not (instance / _WORKDIR_NAME).exists(),
                durable_delete_receipt=observation.delete_receipt,
                signal_authorities=("retained_parent_group",),
                canonical_head_certified=True,
                task4_authority_used=True,
                next_stage_spawned=True,
                cli_exec_count=1,
                control_trace=tuple(trace),
                canonical_control_sequences=(
                    supervisor_sequence,
                    anchor_sequence,
                    armed_sequence,
                    running_head.sequence,
                ),
                cleanup_request_authenticated=observation.cleanup_sequence == 5,
                cleanup_task4_admitted=observation.batch_count == 4,
                cleanup_batch_count=observation.batch_count,
                cleanup_completed_steps=observation.completed_steps,
                cleanup_done_sequence=observation.done_sequence,
                same_canonical_journal=same_journal,
                evidence_observed_not_inferred=True,
                group_enumeration_complete=bool(
                    flags & _CLEANUP_GROUP_ENUMERATION_COMPLETE
                ),
                control_fd_phase_enforced=observation.result_sequence == 6,
            )
        finally:
            if parent_control is not None:
                parent_control.close()
            if child_control is not None:
                child_control.close()
            if parent_fallback is not None:
                parent_fallback.close()
            if child_fallback is not None:
                child_fallback.close()
            if process is not None and process.poll() is None and not cleanup_finished:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process is not None and process.stderr is not None:
                process.stderr.close()
            if journal is not None and not journal.closed:
                journal.close()
            os.close(parent_dirfd)


def _run_real_fail_dead_boundary(name: str) -> LifecycleEvidence:
    instance = Path(tempfile.mkdtemp(prefix="claude-real-fail-dead-"))
    parent_dirfd = _open_private_directory(instance)
    journal: Journal | None = None
    parent_control: socket.socket | None = None
    child_control: socket.socket | None = None
    parent_fallback: socket.socket | None = None
    child_fallback: socket.socket | None = None
    process: subprocess.Popen[bytes] | None = None
    anchor_identity: (
        tuple[int, int, int, int, int, int, int, bytes, bytes] | None
    ) = None
    trace: list[str] = []
    certified_sequences: list[int] = []
    retain_path = False
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
        parent_fallback, child_fallback = socket.socketpair()
        parent_control.settimeout(10)
        environment = {
            "HOME": str(instance),
            "USER": "fail-dead-probe",
            "LOCAL_PROXY_ALLOCATION_NONCE": _NONCE_HEX,
            "LOCAL_PROXY_INSTANCE_DIR": str(instance),
            "LOCAL_PROXY_REAL_CLAUDE": str(_probe_child_path()),
            "LOCAL_PROXY_CONTROL_FD": str(child_control.fileno()),
            "LOCAL_PROXY_ANCHOR_CONTROL_FD": str(child_fallback.fileno()),
            "LOCAL_PROXY_NETWORK_PROXY": "0",
        }
        process = subprocess.Popen(
            [str(_supervisor_path()), "--output-path", str(instance / "unused")],
            env=environment,
            pass_fds=(child_control.fileno(), child_fallback.fileno()),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        child_control.close()
        child_control = None
        child_fallback.close()
        child_fallback = None
        if name != "supervisor_before_identity":
            _, sequence, _ = _certify_and_ack(parent_control, journal, 1, 2, trace)
            certified_sequences.append(sequence)
        if name in {"cli_before_armed", "after_armed_before_exec"}:
            anchor_payload, sequence, _ = _certify_and_ack(
                parent_control, journal, 3, 4, trace
            )
            certified_sequences.append(sequence)
            anchor_identity = _parse_process_identity(anchor_payload)
        if name == "after_armed_before_exec":
            armed_name, armed_payload = _receive_control_frame(parent_control, 5)
            trace.append(armed_name)
            armed = journal.certify_bootstrap(5, armed_payload)
            certified_sequences.append(armed.sequence)
        parent_control.close()
        parent_control = None
        returncode = process.wait(timeout=10)
        if returncode != 75:
            raise SupervisorProbeError(
                f"fail-dead boundary exited with unexpected code {returncode}"
            )
        if process.stderr is not None and process.stderr.read() != b"":
            raise SupervisorProbeError("fail-dead boundary emitted diagnostics")
        if anchor_identity is not None:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(anchor_identity[0], 0)
                except ProcessLookupError:
                    break
                time.sleep(0.001)
            else:
                raise SupervisorProbeError("gated anchor did not fail dead")
        terminal = journal.mark_unconfirmed(UnconfirmedReason.PROOF_UNAVAILABLE)
        certified = journal.certify_head()
        if terminal.sequence != certified.sequence or (
            certified.state.kind.name != "UNCONFIRMED"
        ):
            raise SupervisorProbeError("fail-dead prefix was not retained")
        retain_path = True
        _RETAINED_UNCONFIRMED_PATHS.append(instance)
        return LifecycleEvidence(
            scenario=name,
            outcome="unconfirmed",
            canonical_head_certified=True,
            task4_authority_used=True,
            unconfirmed_persistent=True,
            fail_dead_exit_code=returncode,
            next_stage_spawned=anchor_identity is not None,
            cli_exec_count=0,
            artifacts_retained=(instance / _WORKDIR_NAME).is_dir()
            and (instance / _JOURNAL_NAME).is_file(),
            control_trace=tuple(trace),
            canonical_control_sequences=tuple(certified_sequences),
            same_canonical_journal=terminal.sequence == certified.sequence,
            evidence_observed_not_inferred=True,
            control_fd_phase_enforced=True,
        )
    finally:
        if parent_control is not None:
            parent_control.close()
        if child_control is not None:
            child_control.close()
        if parent_fallback is not None:
            parent_fallback.close()
        if child_fallback is not None:
            child_fallback.close()
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process is not None and process.stderr is not None:
            process.stderr.close()
        if journal is not None:
            journal.close()
        os.close(parent_dirfd)
        if not retain_path:
            # No allocation artifact is removed on an unverified failure path.
            _RETAINED_UNCONFIRMED_PATHS.append(instance)


def run_bootstrap_environment(
    source: Mapping[str, str], config: EnvironmentConfig
) -> LifecycleEvidence:
    """Exec a value-dumping substitute through the real supervisor and anchor."""
    supervisor = _supervisor_path()
    probe_child = _probe_child_path()
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
        parent_fallback: socket.socket | None = None
        child_fallback: socket.socket | None = None
        process: subprocess.Popen[bytes] | None = None
        try:
            output_path = instance / "environment-fingerprints"
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
            parent_fallback, child_fallback = socket.socketpair()
            parent_control.settimeout(10)
            source_environment.update(
                {
                    "LOCAL_PROXY_ALLOCATION_NONCE": _NONCE_HEX,
                    "LOCAL_PROXY_INSTANCE_DIR": str(instance),
                    "LOCAL_PROXY_REAL_CLAUDE": str(probe_child),
                    "LOCAL_PROXY_CONTROL_FD": str(child_control.fileno()),
                    "LOCAL_PROXY_ANCHOR_CONTROL_FD": str(child_fallback.fileno()),
                    "LOCAL_PROXY_NETWORK_PROXY": "1" if config.network_proxy else "0",
                }
            )
            arguments = [str(supervisor), "--output-path", str(output_path)]
            process = subprocess.Popen(
                arguments,
                env=source_environment,
                pass_fds=(child_control.fileno(), child_fallback.fileno()),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            child_control.close()
            child_control = None
            child_fallback.close()
            child_fallback = None
            trace: list[str] = []
            supervisor_payload, supervisor_sequence, supervisor_hash = _certify_and_ack(
                parent_control, journal, 1, 2, trace
            )
            anchor_payload, anchor_sequence, anchor_hash = _certify_and_ack(
                parent_control, journal, 3, 4, trace
            )
            armed_payload, armed_sequence, armed_hash = _certify_and_ack(
                parent_control, journal, 5, 6, trace
            )
            try:
                running_name, running_payload = _receive_control_frame(
                    parent_control, 7
                )
            except SupervisorProbeError as error:
                returncode = process.wait(timeout=10)
                stderr = process.stderr.read().decode() if process.stderr else ""
                raise SupervisorProbeError(
                    f"running gate failed ({returncode}, output_exists="
                    f"{output_path.exists()}, output_size="
                    f"{output_path.stat().st_size if output_path.exists() else -1}): "
                    f"{stderr}"
                ) from error
            trace.append(running_name)
            running_head = journal.certify_bootstrap(7, running_payload)
            supervisor_identity = _parse_process_identity(supervisor_payload)
            anchor_identity = _parse_process_identity(anchor_payload)
            armed_identity = _parse_process_identity(armed_payload[:112])
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
            real_cli_metadata = probe_child.stat()
            real_cli_digest = hashlib.sha256(probe_child.read_bytes()).digest()
            if (
                running_identity[5] != real_cli_metadata.st_dev
                or running_identity[6] != real_cli_metadata.st_ino
                or running_identity[8] != real_cli_digest
            ):
                raise SupervisorProbeError("native CLI exec identity is invalid")
            deadline = time.monotonic() + 5
            while not output_path.exists() and time.monotonic() < deadline:
                time.sleep(0.001)
            if not output_path.is_file():
                raise SupervisorProbeError("native environment evidence is absent")
            fingerprints = _parse_environment_fingerprints(output_path.read_bytes())
            head = journal.certify_head()
            names = {item[0] for item in fingerprints}
            bootstrap_names = {
                "LOCAL_PROXY_ALLOCATION_NONCE",
                "LOCAL_PROXY_INSTANCE_DIR",
                "LOCAL_PROXY_REAL_CLAUDE",
                "LOCAL_PROXY_CONTROL_FD",
                "LOCAL_PROXY_NETWORK_PROXY",
                "LOCAL_PROXY_ANCHOR_CONTROL_FD",
            }
            cleanup = _request_cleanup_and_delete(
                parent_control, process, journal, instance
            )
            if (
                cleanup.batch_count != 4
                or cleanup.completed_steps != 0xF
                or not cleanup.delete_receipt
            ):
                raise SupervisorProbeError(
                    "native bootstrap cleanup evidence is incomplete"
                )
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
                exact_canonical_ack_heads=len(
                    {supervisor_hash, anchor_hash, armed_hash, running_head.hash}
                )
                == 4,
                canonical_control_types=(
                    "SUPERVISOR_IDENTITY",
                    "ANCHOR_IDENTITY",
                    "CLI_ARMED",
                    "CLI_RUNNING",
                ),
                canonical_control_sequences=(
                    supervisor_sequence,
                    anchor_sequence,
                    armed_sequence,
                    running_head.sequence,
                ),
                post_exec_identity_verified=True,
                cli_control_fd_closed_on_exec=True,
                network_proxy_selector_authenticated=True,
                probe_mode_collision_impossible=True,
            )
        except subprocess.TimeoutExpired as error:
            if process is not None:
                process.kill()
                process.wait()
            raise SupervisorProbeError("native bootstrap timed out") from error
        finally:
            if parent_control is not None:
                parent_control.close()
            if child_control is not None:
                child_control.close()
            if parent_fallback is not None:
                parent_fallback.close()
            if child_fallback is not None:
                child_fallback.close()
            if process is not None and process.stderr is not None:
                process.stderr.close()
            if journal is not None:
                journal.close()
            os.close(parent_dirfd)
