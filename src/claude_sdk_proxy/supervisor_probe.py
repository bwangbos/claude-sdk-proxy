"""Deterministic native supervisor/anchor lifecycle feasibility scenarios."""

from __future__ import annotations

import ctypes
import hashlib
import os
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from claude_sdk_proxy.environment import EnvironmentConfig
from claude_sdk_proxy.journal import (
    Journal,
    JournalError,
    ReapProof,
    RecordClass,
    UnconfirmedReason,
)
from claude_sdk_proxy.lifecycle import ProcessIdentity, Record

_NORMAL_LIMIT = 32 * 1024
_PHYSICAL_RECORD_SIZE = 1172
_RECOVERY_RECORD_COUNT = 14
_HARD_LIMIT = _NORMAL_LIMIT + _PHYSICAL_RECORD_SIZE * _RECOVERY_RECORD_COUNT
_NONCE = bytes.fromhex("8f" * 32)
_NONCE_HEX = _NONCE.hex()
_JOURNAL_NAME = "allocation.journal"
_WORKDIR_NAME = "allocation.workdir"
_RETAINED_CLEANUP_ACTORS: list[subprocess.Popen[bytes]] = []
_RETAINED_CLEANUP_DIRECTORIES: list[tempfile.TemporaryDirectory[str]] = []
_RETAINED_UNCONFIRMED_PATHS: list[Path] = []
_ProcessIdentityTuple = tuple[int, int, int, int, int, int, int, bytes, bytes]


class SupervisorProbeError(RuntimeError):
    """A native lifecycle substitute failed closed without exposing content."""


@dataclass(frozen=True)
class _RetainedActorKey:
    allocation_nonce: bytes
    actor_identity: _ProcessIdentityTuple


@dataclass
class _RetainedActorChain:
    """Owned exception-path resources awaiting an explicit reconciler."""

    key: _RetainedActorKey
    flow: str
    instance: Path
    parent_dirfd: int
    journal: Journal
    process: subprocess.Popen[bytes]
    anchor: _ProcessIdentityTuple
    controls: tuple[socket.socket, ...]
    executor_reap_proof: ReapProof | None = None
    cleanup_request_may_have_been_delivered: bool = False
    cleanup_request_sequence: int | None = None
    cleanup_request_hash: bytes | None = None
    cleanup_ack_payload: bytes | None = None
    release_certification: _RetainedReleaseCertification | None = None
    released: bool = False
    recovery_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )


class _RetainedActorReservation:
    """One pre-spawn capacity slot, atomically promoted to an exact owner."""

    def __init__(
        self,
        registry: _RetainedActorRegistry,
        token: int,
        allocation_nonce: bytes,
        entry: _RetainedActorEntry,
    ) -> None:
        self._registry = registry
        self._token = token
        self._allocation_nonce = allocation_nonce
        self._entry = entry

    @property
    def owns_resources(self) -> bool:
        return self.owner is not None

    @property
    def owner(self) -> _RetainedActorChain | None:
        return self._registry._owner_for(self)

    @property
    def key(self) -> _RetainedActorKey:
        owner = self.owner
        if owner is None:
            raise SupervisorProbeError("retained owner is not registered")
        return owner.key

    def transfer(
        self,
        *,
        flow: str,
        instance: Path,
        parent_dirfd: int,
        journal: Journal,
        process: subprocess.Popen[bytes],
        anchor: _ProcessIdentityTuple,
        controls: tuple[socket.socket | None, ...],
    ) -> _RetainedActorChain:
        return self._registry._transfer(
            self,
            flow=flow,
            instance=instance,
            parent_dirfd=parent_dirfd,
            journal=journal,
            process=process,
            anchor=anchor,
            controls=controls,
        )

    def cancel(self) -> None:
        self._registry._cancel(self)

    def cancel_if_reserved(self) -> None:
        """Non-throwingly release capacity only while no owner is published."""
        self._registry._cancel_if_reserved(self)


@dataclass
class _RetainedActorEntry:
    allocation_nonce: bytes
    owner: _RetainedActorChain | None = None


@dataclass(frozen=True)
class _RetainedReleaseCertification:
    sequence: int
    head_hash: bytes
    reap_proof: ReapProof


@dataclass
class _DetachedFD:
    """A one-shot close obligation detached from its owning registry entry."""

    resource: int
    started: bool = False

    def begin(self) -> int:
        if self.started:
            raise SupervisorProbeError("detached descriptor close already started")
        self.started = True
        return self.resource


class _RetainedActorRegistry:
    """Fixed cleanup-owner capacity; live or unconfirmed owners are never evicted."""

    def __init__(self, *, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("retained actor capacity must be positive")
        self._capacity = capacity
        self._condition = threading.Condition()
        self._next_token = 1
        self._entries: dict[int, _RetainedActorEntry] = {}

    @property
    def count(self) -> int:
        with self._condition:
            return len(self._entries)

    def reserve(self, allocation_nonce: bytes) -> _RetainedActorReservation:
        with self._condition:
            if len(self._entries) >= self._capacity:
                raise SupervisorProbeError("retained actor capacity is exhausted")
            token = self._next_token
            self._next_token += 1
            entry = _RetainedActorEntry(allocation_nonce)
            self._entries[token] = entry
            return _RetainedActorReservation(self, token, allocation_nonce, entry)

    def _entry_for(
        self, reservation: _RetainedActorReservation
    ) -> _RetainedActorEntry | None:
        entry = self._entries.get(reservation._token)
        if (
            entry is not reservation._entry
            or entry.allocation_nonce != reservation._allocation_nonce
        ):
            return None
        return entry

    def _owner_for(
        self, reservation: _RetainedActorReservation
    ) -> _RetainedActorChain | None:
        with self._condition:
            return reservation._entry.owner

    def _transfer(
        self,
        reservation: _RetainedActorReservation,
        *,
        flow: str,
        instance: Path,
        parent_dirfd: int,
        journal: Journal,
        process: subprocess.Popen[bytes],
        anchor: _ProcessIdentityTuple,
        controls: tuple[socket.socket | None, ...],
    ) -> _RetainedActorChain:
        key = _RetainedActorKey(reservation._allocation_nonce, anchor)
        retained_controls = tuple(
            control for control in controls if control is not None
        )
        owner = _RetainedActorChain(
            key=key,
            flow=flow,
            instance=instance,
            parent_dirfd=parent_dirfd,
            journal=journal,
            process=process,
            anchor=anchor,
            controls=retained_controls,
        )
        with self._condition:
            entry = self._entry_for(reservation)
            if entry is None:
                raise SupervisorProbeError(
                    "retained actor reservation was not admitted"
                )
            if entry.owner is not None:
                raise SupervisorProbeError("retained actor reservation is unavailable")
            if any(
                candidate.owner is not None and candidate.owner.key == key
                for candidate in self._entries.values()
            ):
                raise SupervisorProbeError("retained actor key is already owned")
            # This single assignment is the RESERVED -> OWNED publication.
            # The fixed registry entry is the only state consulted by unwind.
            entry.owner = owner
            return owner

    def _cancel(self, reservation: _RetainedActorReservation) -> None:
        with self._condition:
            entry = self._entry_for(reservation)
            if entry is None:
                return
            if entry.owner is not None:
                raise SupervisorProbeError("cannot cancel transferred ownership")
            del self._entries[reservation._token]
            self._condition.notify_all()

    def _cancel_if_reserved(self, reservation: _RetainedActorReservation) -> None:
        with self._condition:
            entry = self._entry_for(reservation)
            if entry is not None and entry.owner is None:
                del self._entries[reservation._token]
                self._condition.notify_all()

    def keys(self) -> tuple[_RetainedActorKey, ...]:
        with self._condition:
            return tuple(
                entry.owner.key
                for entry in self._entries.values()
                if entry.owner is not None
            )

    def get(self, key: _RetainedActorKey) -> _RetainedActorChain:
        with self._condition:
            for entry in self._entries.values():
                if entry.owner is not None and entry.owner.key == key:
                    return entry.owner
            raise SupervisorProbeError("retained actor key is unknown")

    def release(self, key: _RetainedActorKey, owner: _RetainedActorChain) -> None:
        with self._condition:
            for token, entry in self._entries.items():
                if entry.owner is owner and owner.key == key:
                    del self._entries[token]
                    self._condition.notify_all()
                    return
            raise SupervisorProbeError("retained actor owner changed during release")


@dataclass(frozen=True)
class _RetainedActorChainInspection:
    flow: str
    canonical_state: str
    actor_live_or_task4_reaped: bool
    supervisor_task4_reaped: bool
    anchor_identity_exact: bool
    workdir_retained: bool
    journal_retained: bool
    owner_count_for_key: int
    journal_reopened_and_certified: bool
    exact_group_member_count: int
    group_capability_absent: bool
    retained_child_state: Literal["live", "reapable", "reaped"]


@dataclass(frozen=True)
class _TestActorChainTeardown:
    group_absent: bool
    supervisor_child_reaped: bool
    untracked_orphan_count: int
    workdir_retained: bool
    journal_retained: bool


# The feasibility probe reserves at most 32 cleanup ownership slots. This is a
# conservative explicit bound, independent of journal tail capacity; a later
# server configuration may choose a smaller max_cleanup_ownership_records.
_RETAINED_ACTOR_REGISTRY = _RetainedActorRegistry(capacity=32)
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
    12: "CLEANUP_ACK",
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
        "cleanup_fail_after_admission",
        "cleanup_fail_after_stop",
        "cleanup_fail_after_enumeration",
        "cleanup_fail_after_cont",
        "cleanup_fail_after_term",
        "cleanup_fail_after_kill",
        "anchor_only_cleanup",
        "fallback_while_supervisor_healthy",
        "fallback_early_before_running",
        "fallback_nonempty_payload_after_loss",
        "fallback_wrong_phase_after_loss",
        "fallback_duplicate_after_loss",
    }
)
_HANDOFF = frozenset(
    {"stale_executor", "retirement_replacement", "interrupted_batch_replay"}
)
_REAL_ACTOR_LOSS = frozenset(
    {
        "after_running",
        "during_term_batch",
        "during_kill_batch",
        "cleanup_fail_after_admission",
        "cleanup_fail_after_stop",
        "cleanup_fail_after_enumeration",
        "cleanup_fail_after_cont",
        "cleanup_fail_after_term",
        "cleanup_fail_after_kill",
    }
)
_REAL_RETAINING_CLEANUP = frozenset(
    {
        "ordinary_term_success",
        "stubborn_child_kill",
        "confirmed_reap",
        "parent_held_zombie_nonreuse",
        "anchor_only_cleanup",
        "ordinary_probe_argument_collision",
    }
)
_REAL_FAIL_DEAD_BOUNDARIES = frozenset(
    {
        "supervisor_before_identity",
        "anchor_before_identity",
        "cli_before_armed",
        "after_armed_before_exec",
        "pre_armed_fail_dead",
    }
)
_REAL_IDENTITY_REJECTIONS = frozenset(
    {"altered_executable_identity", "reused_pid", "unexpected_descendant"}
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
_CLEANUP_PROCESS_BATCH_PREAUTHORIZED = 1 << 9
_CLEANUP_TOKEN_RETAINED_THROUGH_SIGNALS = 1 << 10
_CLEANUP_PROCESS_TARGET_EXACT = 1 << 11
_CLEANUP_INJECTION_RECOVERED = 1 << 12
_CLEANUP_ANCHOR_ONLY_OBSERVED = 1 << 13
_CLEANUP_GROUP_RESUMED_AFTER_FAILURE = 1 << 14


def _python_exception_checkpoint(flow: str, checkpoint: str) -> None:
    """Private deterministic fault boundary replaced only by Darwin tests."""


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
    cleanup_failure_injection_observed: bool = False
    process_batch_admitted_before_signal: bool = False
    process_batch_target_exact: bool = False
    action_token_retained_through_signals: bool = False
    frozen_group_left_behind: bool = False
    anchor_only_group_observed: bool = False
    group_resumed_after_failure: bool = False
    fallback_request_rejected: bool = False
    rejected_request_no_signal: bool = False
    supervisor_loss_proven: bool = False
    fallback_payload_exact: bool = False
    self_term_signal_count: int = 0
    cleanup_rejection_authenticated: bool = False
    observed_rejection_reason: str = ""
    observed_handoff_records: int = 0
    observed_live_executor: bool = False
    actor_loss_observed: bool = False
    actor_loss_exit_code: int | None = None
    recovery_executor_reaped: bool = False
    action_lock_released_after_actor_loss: bool = False
    production_recovery_signal_count: int = 0
    group_stopped_at_recovery: bool = False
    test_teardown_group_absent: bool = False
    untracked_orphan_count: int = 0
    original_supervisor_actor_chain: bool = False
    post_armed_executable_mismatch_observed: bool = False
    simulated_reuse_observation_rejected: bool = False
    unexpected_group_member_count: int = 0
    cleanup_ack_rejections: tuple[str, ...] = ()
    cleanup_ack_accept_count: int = 0
    cleanup_action_release_count: int = 0
    cleanup_ack_phase_latched: bool = False
    rejected_cleanup_ack_no_action: bool = False
    cleanup_request_binding_rejections: tuple[str, ...] = ()
    rejected_cleanup_request_no_signal: bool = False

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


def _probe_supervisor_path() -> Path:
    path = _repository_root() / "build/bin/claude-proxy-supervisor-probe"
    if not path.is_file() or not os.access(path, os.X_OK):
        raise SupervisorProbeError("native injection supervisor is unavailable")
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



def run_lifecycle_scenario(name: str) -> LifecycleEvidence:
    """Run one bounded substitute without contacting Claude or a network peer."""
    if name not in _SCENARIOS:
        raise ValueError("unknown lifecycle scenario")
    if name in {
        "kill_supervisor_after_running",
        "fallback_while_supervisor_healthy",
        "fallback_early_before_running",
        "fallback_nonempty_payload_after_loss",
        "fallback_wrong_phase_after_loss",
        "fallback_duplicate_after_loss",
    }:
        return _run_real_supervisorless_fallback(name)
    if name in _REAL_ACTOR_LOSS:
        return _run_real_actor_loss(name)
    if name in _HANDOFF:
        return _run_real_actor_loss(name)
    if name in _REAL_RETAINING_CLEANUP:
        return _run_real_retaining_cleanup(name)
    if name in _REAL_FAIL_DEAD_BOUNDARIES:
        return _run_real_fail_dead_boundary(name)
    if name in _REAL_IDENTITY_REJECTIONS:
        return _run_real_identity_rejection(name)
    if name == "control_frame_validation":
        return _run_real_control_validation()
    if name == "wedged_supervisor":
        return _run_real_wedged_supervisor()
    raise SupervisorProbeError("scenario has no real lifecycle implementation")


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


def _wait_for_unreaped_exit(pid: int, timeout: float = 5) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        information = os.waitid(
            os.P_PID,
            pid,
            os.WEXITED | os.WNOHANG | os.WNOWAIT,
        )
        if information is not None and information.si_pid == pid:
            return information.si_status
        time.sleep(0.001)
    raise SupervisorProbeError("injected supervisor death was not observed")


def _same_parsed_identity(
    observed: ProcessIdentity,
    expected: tuple[int, int, int, int, int, int, int, bytes, bytes],
) -> bool:
    return (
        observed.pid == expected[0]
        and observed.start_ns == expected[1]
        and observed.uid == expected[2]
        and observed.pgid == expected[3]
        and observed.sid == expected[4]
        and observed.executable_dev == expected[5]
        and observed.executable_ino == expected[6]
        and observed.boot_id == expected[7]
        and observed.executable_hash == expected[8]
    )


def _process_is_stopped(pid: int) -> bool:
    completed = subprocess.run(
        ["/bin/ps", "-o", "state=", "-p", str(pid)],
        capture_output=True,
        check=False,
        text=True,
        timeout=2,
    )
    return completed.returncode == 0 and completed.stdout.strip().startswith("T")


def _fresh_group_is_absent(pgid: int) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            # A just-killed, unreaped group leader can remain observable but
            # unsignalable until its retaining parent exits and reaps it.
            pass
        time.sleep(0.001)
    return False


def _teardown_fresh_actor_group(
    journal: Journal,
    anchor: tuple[int, int, int, int, int, int, int, bytes, bytes],
    *,
    stopped: bool,
) -> bool:
    """Test-only cleanup under the caller's fresh-process authorization."""
    try:
        observed = journal.observe_process(anchor[0])
    except JournalError:
        return _fresh_group_is_absent(anchor[3])
    if not _same_parsed_identity(observed, anchor):
        raise SupervisorProbeError("test teardown anchor identity drifted")
    if stopped:
        os.killpg(observed.pgid, signal.SIGCONT)
    os.killpg(observed.pgid, signal.SIGKILL)
    return _fresh_group_is_absent(observed.pgid)


def _teardown_recorded_fresh_group(pgid: int) -> bool:
    """Test-only cleanup for a fresh group whose recorded anchor already exited."""
    completed = subprocess.run(
        ["/bin/ps", "-axo", "pid=,pgid=,comm="],
        capture_output=True,
        check=False,
        text=True,
        timeout=2,
    )
    if completed.returncode != 0:
        raise SupervisorProbeError("test teardown could not enumerate fresh group")
    members: list[tuple[int, str]] = []
    allowed = {
        str(_probe_child_path()),
        str(_repository_root() / "build/bin/claude-proxy-anchor"),
        "/bin/sleep",
        "sleep",
    }
    for line in completed.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) != 3:
            continue
        pid_text, pgid_text, command = fields
        try:
            member_pid = int(pid_text)
            member_pgid = int(pgid_text)
        except ValueError:
            continue
        if member_pgid == pgid:
            members.append((member_pid, command))
    if not members:
        return True
    if any(command not in allowed for _, command in members):
        raise SupervisorProbeError("fresh test group acquired an unknown member")
    os.killpg(pgid, signal.SIGKILL)
    return _fresh_group_is_absent(pgid)


def _register_retained_path(owner: _RetainedActorChain) -> None:
    if owner.instance not in _RETAINED_UNCONFIRMED_PATHS:
        _RETAINED_UNCONFIRMED_PATHS.append(owner.instance)


def _open_retained_journal(owner: _RetainedActorChain) -> tuple[int, Journal]:
    """Independently reopen the exact canonical journal for observation."""
    directory_fd = os.dup(owner.parent_dirfd)
    try:
        journal = Journal.open_at(
            directory_fd,
            _JOURNAL_NAME,
            owner.key.allocation_nonce,
            _NORMAL_LIMIT,
            _HARD_LIMIT,
            workdir_parent_dirfd=directory_fd,
            workdir_name=_WORKDIR_NAME,
        )
    except BaseException:
        os.close(directory_fd)
        raise
    return directory_fd, journal


def _child_wait_state(pid: int) -> Literal["live", "reapable", "reaped"]:
    try:
        waited = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return "reaped"
    if waited is not None and waited.si_pid == pid:
        return "reapable"
    return "live"


def _enumerate_exact_group(pgid: int) -> tuple[tuple[int, str], ...]:
    """Enumerate the complete currently visible member set for one fresh group."""
    completed = subprocess.run(
        ["/bin/ps", "-axo", "pid=,pgid=,comm="],
        capture_output=True,
        check=False,
        text=True,
        timeout=2,
    )
    if completed.returncode != 0:
        raise SupervisorProbeError("fresh group enumeration failed")
    members: list[tuple[int, str]] = []
    for line in completed.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) != 3:
            continue
        try:
            pid = int(fields[0])
            member_pgid = int(fields[1])
        except ValueError:
            continue
        if member_pgid == pgid:
            members.append((pid, fields[2]))
    return tuple(sorted(members))


def _wait_for_enumerated_group_absence(pgid: int, *, timeout: float = 5.0) -> bool:
    """Require both a complete empty enumeration and absent group capability."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _enumerate_exact_group(pgid):
            if _group_capability_absent(pgid):
                return True
        time.sleep(0.001)
    return False


def _group_capability_absent(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _same_reap_proof(left: ReapProof, right: ReapProof) -> bool:
    return (
        left.allocation_nonce == right.allocation_nonce
        and left.generation == right.generation
        and left.authority_epoch == right.authority_epoch
        and left.identity == right.identity
        and left._certified_hash == right._certified_hash
        and left._capability == right._capability
    )


def _close_retained_control(control: socket.socket) -> None:
    control.close()


def _close_retained_stderr(stderr: object) -> None:
    close = getattr(stderr, "close")
    close()


def _close_retained_journal(journal: Journal) -> None:
    journal.close()


def _close_retained_parent_fd(obligation: _DetachedFD) -> None:
    os.close(obligation.begin())


def _ensure_unconfirmed(owner: _RetainedActorChain) -> None:
    head = owner.journal.certify_head()
    _python_exception_checkpoint(owner.flow, "recovery_head_certified")
    if head.state.kind.name not in {"DONE", "UNCONFIRMED"}:
        terminal = owner.journal.mark_unconfirmed(
            UnconfirmedReason.PROOF_UNAVAILABLE
        )
        _python_exception_checkpoint(owner.flow, "recovery_unconfirmed_persisted")
        certified = owner.journal.certify_head()
        _python_exception_checkpoint(owner.flow, "recovery_unconfirmed_certified")
        if (
            certified.sequence != terminal.sequence
            or certified.state.kind.name != "UNCONFIRMED"
        ):
            raise SupervisorProbeError("retained owner terminal state drifted")


def _reconcile_owner(owner: _RetainedActorChain) -> None:
    """Idempotently converge a retained owner using journal and process state."""
    with owner.recovery_lock:
        head = owner.journal.certify_head()
        _python_exception_checkpoint(owner.flow, "recovery_head_certified")
        kind = head.state.kind.name
        if kind in {"DONE", "UNCONFIRMED"}:
            return

        request_exact = False
        if (
            owner.cleanup_request_sequence is not None
            and owner.cleanup_request_hash is not None
        ):
            try:
                request = owner.journal.certify_bootstrap(8, b"")
            except JournalError:
                request_exact = False
            else:
                request_exact = (
                    request.sequence == owner.cleanup_request_sequence
                    and request.hash == owner.cleanup_request_hash
                )
        ack_exact = False
        if (
            owner.cleanup_ack_payload is not None
            and len(owner.cleanup_ack_payload) == 40
        ):
            ack_sequence, ack_hash = struct.unpack("<Q32s", owner.cleanup_ack_payload)
            ack_exact = (
                request_exact
                and ack_sequence == owner.cleanup_request_sequence
                and ack_hash == owner.cleanup_request_hash
            )
        record_kind = owner.journal.scan().head.record.kind.name
        cleanup_progressed = record_kind in {
            "BATCH_ACTIVE",
            "RETIRING_IDLE",
            "RETIRING_BATCH",
        }
        wait_state = _child_wait_state(owner.process.pid)
        if wait_state == "live" and request_exact:
            # Delivery is ambiguous at byte zero. The certified request, exact
            # ACK (if any), canonical action state, and current child state are
            # the decision inputs; the local may-have-delivered flag is not.
            try:
                exit_code = _wait_for_unreaped_exit(
                    owner.process.pid,
                    timeout=(
                        5.0
                        if ack_exact
                        else (0.5 if cleanup_progressed else 0.1)
                    ),
                )
            except SupervisorProbeError:
                exit_code = None
            else:
                wait_state = "reapable"
                owner.process.returncode = exit_code
        if wait_state == "live" and cleanup_progressed:
            # A live executor may still hold the exact admitted action token.
            # Retain that canonical prefix and every handle; do not append a
            # competing terminal record while its authorized batch can finish.
            return
        if wait_state not in {"reapable", "reaped"}:
            _ensure_unconfirmed(owner)
            return

        _python_exception_checkpoint(owner.flow, "recovery_exit_observed")
        record_head = owner.journal.scan().head
        record_kind = record_head.record.kind.name
        if wait_state == "reapable" and record_kind in {
            "ACTIVE_READY",
            "BATCH_ACTIVE",
        }:
            while time.monotonic_ns() <= record_head.record.lease_deadline_ns:
                time.sleep(0.001)
            retired = owner.journal.retire_executor(
                authority="exception-reconciler",
                authority_epoch=1,
                authority_deadline_ns=time.monotonic_ns() + 1_000_000_000,
            )
            record_kind = retired.record.kind.name
            _python_exception_checkpoint(owner.flow, "recovery_executor_retired")
        if record_kind not in {"RETIRING_IDLE", "RETIRING_BATCH"}:
            if record_kind != "BATCH_ACTIVE":
                _ensure_unconfirmed(owner)
            return
        proof = owner.journal.confirm_executor_reaped(
            deadline_ns=time.monotonic_ns() + 5_000_000_000
        )
        owner.executor_reap_proof = proof
        _python_exception_checkpoint(owner.flow, "recovery_reap_receipt_retained")
        if record_kind == "RETIRING_BATCH":
            owner.journal.reconcile_interrupted_batch(proof)
            _python_exception_checkpoint(owner.flow, "recovery_batch_reconciled")
        _ensure_unconfirmed(owner)


def _reconcile_owner_best_effort(owner: _RetainedActorChain) -> None:
    try:
        _reconcile_owner(owner)
    except BaseException:
        # The registry still owns every handle and exact identity. A later
        # bounded reconciliation retry starts from the durable canonical head.
        pass
    _register_retained_path(owner)


def _child_is_live_or_reapable(pid: int) -> bool:
    try:
        waited = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return False
    if waited is not None and waited.si_pid == pid:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _retained_actor_chain_keys() -> tuple[_RetainedActorKey, ...]:
    return _RETAINED_ACTOR_REGISTRY.keys()


def _inspect_retained_actor_chain(
    key: _RetainedActorKey,
) -> _RetainedActorChainInspection:
    """Independently re-open and certify one exact retained owner."""
    retained = _RETAINED_ACTOR_REGISTRY.get(key)
    directory_fd, reopened = _open_retained_journal(retained)
    try:
        head = reopened.certify_head()
        try:
            observed = reopened.observe_process(retained.anchor[0])
        except JournalError:
            anchor_exact = not _enumerate_exact_group(retained.anchor[3])
        else:
            anchor_exact = _same_parsed_identity(observed, retained.anchor)
        supervisor_wait_state = _child_wait_state(retained.process.pid)
        members = _enumerate_exact_group(retained.anchor[3])
        group_capability_absent = _group_capability_absent(retained.anchor[3])
        receipt_exact = False
        if retained.executor_reap_proof is not None:
            try:
                recovered_proof = retained.journal.recover_executor_reap_proof()
            except JournalError:
                receipt_exact = False
            else:
                receipt_exact = _same_reap_proof(
                    recovered_proof, retained.executor_reap_proof
                )
        task4_reaped = (
            receipt_exact
            and retained.executor_reap_proof is not None
            and retained.executor_reap_proof.pid == retained.process.pid
            and supervisor_wait_state == "reaped"
            and head.state.kind.name in {"DONE", "UNCONFIRMED"}
        )
        return _RetainedActorChainInspection(
            flow=retained.flow,
            canonical_state=head.state.kind.name,
            actor_live_or_task4_reaped=(
                task4_reaped or supervisor_wait_state in {"live", "reapable"}
            ),
            supervisor_task4_reaped=task4_reaped,
            anchor_identity_exact=anchor_exact,
            workdir_retained=(retained.instance / _WORKDIR_NAME).is_dir(),
            journal_retained=(retained.instance / _JOURNAL_NAME).is_file(),
            owner_count_for_key=sum(
                candidate == key for candidate in _RETAINED_ACTOR_REGISTRY.keys()
            ),
            journal_reopened_and_certified=head.sequence > 0,
            exact_group_member_count=len(members),
            group_capability_absent=group_capability_absent,
            retained_child_state=supervisor_wait_state,
        )
    finally:
        reopened.close()
        os.close(directory_fd)


def _reconcile_retained_actor_chain(key: _RetainedActorKey) -> None:
    owner = _RETAINED_ACTOR_REGISTRY.get(key)
    _reconcile_owner(owner)


def _reconcile_and_release_retained_actor_chain(key: _RetainedActorKey) -> bool:
    """Production keyed release after independently renewed terminal proofs."""
    owner = _RETAINED_ACTOR_REGISTRY.get(key)
    if not owner.journal.closed:
        _reconcile_owner(owner)
    with owner.recovery_lock:
        if not owner.journal.closed:
            directory_fd, reopened = _open_retained_journal(owner)
            try:
                certified = reopened.certify_head()
                members = _enumerate_exact_group(owner.anchor[3])
                group_capability_absent = _group_capability_absent(owner.anchor[3])
                child_state = _child_wait_state(owner.process.pid)
                if (
                    certified.state.kind.name not in {"DONE", "UNCONFIRMED"}
                    or members
                    or not group_capability_absent
                    or child_state != "reaped"
                    or owner.executor_reap_proof is None
                ):
                    return False
                recovered_proof = owner.journal.recover_executor_reap_proof()
                if not _same_reap_proof(
                    recovered_proof, owner.executor_reap_proof
                ):
                    return False
                owner.release_certification = _RetainedReleaseCertification(
                    certified.sequence, certified.hash, recovered_proof
                )
            finally:
                reopened.close()
                os.close(directory_fd)
        elif owner.release_certification is None:
            raise SupervisorProbeError(
                "retained owner journal closed without release certification"
            )

        for control in owner.controls:
            if control.fileno() != -1:
                _close_retained_control(control)
        if owner.process.stderr is not None and not owner.process.stderr.closed:
            _close_retained_stderr(owner.process.stderr)
        if not owner.journal.closed:
            _close_retained_journal(owner.journal)
        if owner.parent_dirfd >= 0:
            obligation = _DetachedFD(owner.parent_dirfd)
            try:
                owner.parent_dirfd = -1
                _close_retained_parent_fd(obligation)
            except BaseException:
                if not obligation.started:
                    owner.parent_dirfd = obligation.resource
                raise
        if (
            any(control.fileno() != -1 for control in owner.controls)
            or (owner.process.stderr is not None and not owner.process.stderr.closed)
            or not owner.journal.closed
            or owner.parent_dirfd >= 0
        ):
            raise SupervisorProbeError("retained owner handles did not close")
        owner.process.returncode = 86
        owner.released = True
        _RETAINED_ACTOR_REGISTRY.release(key, owner)
        return True


def _test_release_retained_actor_chain(
    key: _RetainedActorKey,
) -> _TestActorChainTeardown:
    """Test-only teardown by exact key; removal occurs only after full proof."""
    retained = _RETAINED_ACTOR_REGISTRY.get(key)
    with retained.recovery_lock:
        directory_fd, reopened = _open_retained_journal(retained)
        try:
            reopened.certify_head()
            _python_exception_checkpoint(retained.flow, "release_journal_certified")
            try:
                observed = reopened.observe_process(retained.anchor[0])
            except JournalError:
                observed = None
            if observed is not None:
                if not _same_parsed_identity(observed, retained.anchor):
                    raise SupervisorProbeError("test teardown anchor identity drifted")
                members = _enumerate_exact_group(observed.pgid)
                _python_exception_checkpoint(retained.flow, "release_group_enumerated")
                if not members:
                    raise SupervisorProbeError(
                        "exact anchor group vanished during release"
                    )
                allowed = {
                    str(_probe_child_path()),
                    str(_repository_root() / "build/bin/claude-proxy-anchor"),
                    "/bin/sleep",
                    "sleep",
                }
                if any(command not in allowed for _, command in members):
                    raise SupervisorProbeError(
                        "fresh test group acquired an unknown member"
                    )
                if _process_is_stopped(observed.pid):
                    os.killpg(observed.pgid, signal.SIGCONT)
                os.killpg(observed.pgid, signal.SIGKILL)
            for control in retained.controls:
                if control.fileno() != -1:
                    control.close()
            wait_state = _child_wait_state(retained.process.pid)
            if wait_state != "reaped":
                try:
                    retained.process.wait(timeout=5)
                except subprocess.TimeoutExpired as error:
                    raise SupervisorProbeError(
                        "fresh supervisor did not exit after test control teardown"
                    ) from error
            elif (
                retained.process.returncode is None
                and retained.executor_reap_proof is not None
                and retained.executor_reap_proof.pid == retained.process.pid
            ):
                # Task 4 already performed waitpid and returned the exact reap
                # receipt, so Popen must not attempt a second wait or warn that
                # its now-nonchild process is still running.
                retained.process.returncode = 86
            supervisor_child_reaped = (
                _child_wait_state(retained.process.pid) == "reaped"
            )
            group_absent = _wait_for_enumerated_group_absence(retained.anchor[3])
            remaining_members = _enumerate_exact_group(retained.anchor[3])
            group_capability_absent = _group_capability_absent(retained.anchor[3])
            untracked_orphan_count = len(remaining_members)
            _python_exception_checkpoint(retained.flow, "release_absence_certified")
            if (
                not group_absent
                or remaining_members
                or not group_capability_absent
                or not supervisor_child_reaped
            ):
                raise SupervisorProbeError("fresh actor chain teardown was incomplete")
            if (
                retained.process.stderr is not None
                and not retained.process.stderr.closed
            ):
                retained.process.stderr.close()
            if not retained.journal.closed:
                retained.journal.close()
            os.close(retained.parent_dirfd)
            retained.parent_dirfd = -1
            retained.released = True
            _RETAINED_ACTOR_REGISTRY.release(key, retained)
            return _TestActorChainTeardown(
                group_absent=True,
                supervisor_child_reaped=True,
                untracked_orphan_count=untracked_orphan_count,
                workdir_retained=(retained.instance / _WORKDIR_NAME).is_dir(),
                journal_retained=(retained.instance / _JOURNAL_NAME).is_file(),
            )
        finally:
            reopened.close()
            os.close(directory_fd)


def _run_real_wedged_supervisor() -> LifecycleEvidence:
    reservation = _RETAINED_ACTOR_REGISTRY.reserve(_NONCE)
    try:
        instance = Path(tempfile.mkdtemp(prefix="claude-real-wedged-"))
        parent_dirfd = _open_private_directory(instance)
    except BaseException:
        reservation.cancel_if_reserved()
        raise
    journal: Journal | None = None
    parent_control: socket.socket | None = None
    child_control: socket.socket | None = None
    parent_fallback: socket.socket | None = None
    child_fallback: socket.socket | None = None
    process: subprocess.Popen[bytes] | None = None
    anchor_identity: (
        tuple[int, int, int, int, int, int, int, bytes, bytes] | None
    ) = None
    retained = False
    owner: _RetainedActorChain | None = None
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
        prepared = journal.append(
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
            "USER": "wedged-probe",
            "LOCAL_PROXY_ALLOCATION_NONCE": _NONCE_HEX,
            "LOCAL_PROXY_INSTANCE_DIR": str(instance),
            "LOCAL_PROXY_REAL_CLAUDE": str(_probe_child_path()),
            "LOCAL_PROXY_CONTROL_FD": str(child_control.fileno()),
            "LOCAL_PROXY_ANCHOR_CONTROL_FD": str(child_fallback.fileno()),
            "LOCAL_PROXY_NETWORK_PROXY": "0",
        }
        process = subprocess.Popen(
            [
                str(_supervisor_path()),
                "--output-path",
                str(instance / "wedged-output"),
            ],
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
        _, supervisor_sequence, _ = _certify_and_ack(
            parent_control, journal, 1, 2, trace
        )
        anchor_payload, anchor_sequence, _ = _certify_and_ack(
            parent_control, journal, 3, 4, trace
        )
        anchor_identity = _parse_process_identity(anchor_payload)
        owner = reservation.transfer(
            flow="wedged",
            instance=instance,
            parent_dirfd=parent_dirfd,
            journal=journal,
            process=process,
            anchor=anchor_identity,
            controls=(parent_control, parent_fallback),
        )
        _python_exception_checkpoint("wedged", "owner_registered")
        _python_exception_checkpoint("wedged", "anchor_identity_known")
        _, armed_sequence, _ = _certify_and_ack(
            parent_control, journal, 5, 6, trace
        )
        running_name, running_payload = _receive_control_frame(parent_control, 7)
        trace.append(running_name)
        running = journal.certify_bootstrap(7, running_payload)
        active = journal.scan().head
        if (
            active.record.kind.name != "ACTIVE_READY"
            or active.record.process_pid != process.pid
        ):
            raise SupervisorProbeError("real wedged supervisor is not executor")
        _python_exception_checkpoint("wedged", "executor_observed")
        retirement_blocked = False
        try:
            journal.retire_executor(
                authority="reconciler-1",
                authority_epoch=1,
                authority_deadline_ns=time.monotonic_ns() + 1_000_000_000,
            )
        except JournalError:
            retirement_blocked = True
        if not retirement_blocked:
            raise SupervisorProbeError("live wedged supervisor was replaceable")
        _python_exception_checkpoint("wedged", "retirement_blocked")
        observed_supervisor = journal.observe_process(process.pid)
        if observed_supervisor.pid != active.record.process_pid:
            raise SupervisorProbeError("wedged executor identity drifted")
        _python_exception_checkpoint("wedged", "supervisor_identity_observed")
        terminal = journal.mark_unconfirmed(UnconfirmedReason.PROOF_UNAVAILABLE)
        _python_exception_checkpoint("wedged", "unconfirmed_persisted")
        certified = journal.certify_head()
        _python_exception_checkpoint("wedged", "unconfirmed_certified")
        retained = True
        _RETAINED_UNCONFIRMED_PATHS.append(instance)
        evidence = LifecycleEvidence(
            scenario="wedged_supervisor",
            outcome="unconfirmed",
            anchor_alive=True,
            canonical_head_certified=(
                certified.sequence == terminal.sequence
                and certified.state.kind.name == "UNCONFIRMED"
            ),
            task4_authority_used=True,
            stale_executor_blocked=retirement_blocked,
            unconfirmed_persistent=True,
            exit_refused=True,
            cli_exec_count=1,
            artifacts_retained=True,
            control_trace=tuple(trace),
            canonical_control_sequences=(
                supervisor_sequence,
                anchor_sequence,
                armed_sequence,
                running.sequence,
            ),
            same_canonical_journal=(
                prepared.sequence < active.sequence < terminal.sequence
                and (supervisor_sequence, anchor_sequence, armed_sequence,
                     running.sequence) == (1, 2, 3, 4)
            ),
            evidence_observed_not_inferred=True,
            control_fd_phase_enforced=True,
            observed_handoff_records=2,
            observed_live_executor=True,
            production_recovery_signal_count=0,
            original_supervisor_actor_chain=True,
        )
        _python_exception_checkpoint("wedged", "evidence_captured")
        _register_retained_path(owner)
        teardown = _test_release_retained_actor_chain(owner.key)
        if not teardown.group_absent or not teardown.supervisor_child_reaped:
            raise SupervisorProbeError("wedged actor chain survived test teardown")
        return replace(
            evidence,
            test_teardown_group_absent=teardown.group_absent,
            untracked_orphan_count=teardown.untracked_orphan_count,
        )
    except BaseException:
        retained_owner = reservation.owner
        if retained_owner is not None:
            _reconcile_owner_best_effort(retained_owner)
        raise
    finally:
        retained_owner = reservation.owner
        reservation.cancel_if_reserved()
        locally_owned = retained_owner is None
        if parent_control is not None and locally_owned:
            parent_control.close()
        if child_control is not None:
            child_control.close()
        if parent_fallback is not None and locally_owned:
            parent_fallback.close()
        if child_fallback is not None:
            child_fallback.close()
        if (
            process is not None
            and locally_owned
            and anchor_identity is None
        ):
            process.wait(timeout=5)
        if (
            process is not None
            and process.stderr is not None
            and locally_owned
        ):
            process.stderr.close()
        if journal is not None and locally_owned:
            journal.close()
        if locally_owned:
            os.close(parent_dirfd)
        if not retained:
            _RETAINED_UNCONFIRMED_PATHS.append(instance)


def _run_real_actor_loss(name: str) -> LifecycleEvidence:
    injection_by_name = {
        "after_running": ("after_running", 11),
        "during_term_batch": ("after_term", 5),
        "during_kill_batch": ("after_kill", 6),
        "cleanup_fail_after_admission": ("after_admission", 1),
        "cleanup_fail_after_stop": ("after_stop", 2),
        "cleanup_fail_after_enumeration": ("after_enumeration", 3),
        "cleanup_fail_after_cont": ("after_cont", 4),
        "cleanup_fail_after_term": ("after_term", 5),
        "cleanup_fail_after_kill": ("after_kill", 6),
        "stale_executor": ("after_admission", 1),
        "retirement_replacement": ("after_admission", 1),
        "interrupted_batch_replay": ("after_admission", 1),
    }
    injection, injection_code = injection_by_name[name]
    handoff = name in _HANDOFF
    reservation = _RETAINED_ACTOR_REGISTRY.reserve(_NONCE)
    try:
        instance = Path(tempfile.mkdtemp(prefix="claude-real-actor-loss-"))
        parent_dirfd = _open_private_directory(instance)
    except BaseException:
        reservation.cancel_if_reserved()
        raise
    journal: Journal | None = None
    parent_control: socket.socket | None = None
    child_control: socket.socket | None = None
    parent_fallback: socket.socket | None = None
    child_fallback: socket.socket | None = None
    process: subprocess.Popen[bytes] | None = None
    anchor_identity: (
        tuple[int, int, int, int, int, int, int, bytes, bytes] | None
    ) = None
    executor_reaped = False
    retained = False
    owner: _RetainedActorChain | None = None
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
            "USER": "actor-loss-probe",
            "LOCAL_PROXY_ALLOCATION_NONCE": _NONCE_HEX,
            "LOCAL_PROXY_INSTANCE_DIR": str(instance),
            "LOCAL_PROXY_REAL_CLAUDE": str(_probe_child_path()),
            "LOCAL_PROXY_CONTROL_FD": str(child_control.fileno()),
            "LOCAL_PROXY_ANCHOR_CONTROL_FD": str(child_fallback.fileno()),
            "LOCAL_PROXY_NETWORK_PROXY": "0",
            "LOCAL_PROXY_TEST_INJECTION": injection,
        }
        arguments = [
            str(_probe_supervisor_path()),
            "--output-path",
            str(instance / "actor-loss-output"),
        ]
        if injection_code == 6:
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
        _, supervisor_sequence, _ = _certify_and_ack(
            parent_control, journal, 1, 2, trace
        )
        anchor_payload, anchor_sequence, _ = _certify_and_ack(
            parent_control, journal, 3, 4, trace
        )
        anchor_identity = _parse_process_identity(anchor_payload)
        owner = reservation.transfer(
            flow="actor_loss",
            instance=instance,
            parent_dirfd=parent_dirfd,
            journal=journal,
            process=process,
            anchor=anchor_identity,
            controls=(parent_control, parent_fallback),
        )
        _python_exception_checkpoint("actor_loss", "owner_registered")
        _python_exception_checkpoint("actor_loss", "anchor_identity_known")
        _, armed_sequence, _ = _certify_and_ack(
            parent_control, journal, 5, 6, trace
        )
        running_name, running_payload = _receive_control_frame(parent_control, 7)
        trace.append(running_name)
        running = journal.certify_bootstrap(7, running_payload)
        cleanup_expectation = _prepare_cleanup_request(journal)
        owner.cleanup_request_sequence = cleanup_expectation.sequence
        owner.cleanup_request_hash = cleanup_expectation.hash
        _python_exception_checkpoint("actor_loss", "cleanup_request_prepared")
        cleanup_sequence = cleanup_expectation.sequence
        _send_cleanup_request_with_ambiguity(
            owner,
            parent_control,
            _cleanup_request_frame(cleanup_expectation),
        )
        _python_exception_checkpoint("actor_loss", "cleanup_request_sent")
        if injection_code != 11:
            ack_name, ack_payload = _receive_control_frame(parent_control, 12)
            owner.cleanup_ack_payload = ack_payload
            _python_exception_checkpoint(
                "actor_loss", "cleanup_ack_received_before_accept"
            )
            _accept_cleanup_ack(cleanup_expectation, ack_name, ack_payload)
            _python_exception_checkpoint("actor_loss", "cleanup_ack_accepted")
        exit_code = _wait_for_unreaped_exit(process.pid)
        if exit_code != 86:
            raise SupervisorProbeError(
                f"actor-loss checkpoint exited with code {exit_code}"
            )
        _python_exception_checkpoint("actor_loss", "actor_loss_observed")
        error_payload = struct.pack("<I", 100 + injection_code)
        injected = journal.certify_bootstrap(11, error_payload)
        _python_exception_checkpoint("actor_loss", "actor_loss_record_certified")
        before_recovery = journal.scan().head
        _python_exception_checkpoint("actor_loss", "recovery_head_observed")
        batch_active = before_recovery.record.kind.name == "BATCH_ACTIVE"
        process_target_exact = False
        exact_batch = ""
        if batch_active:
            exact_batch = before_recovery.record.exact_batch
            descriptors = before_recovery.record.descriptors
            process_target_exact = (
                exact_batch == "process-absent"
                and bool(descriptors)
                and descriptors[0].target is not None
                and _same_parsed_identity(
                    descriptors[0].target,
                    anchor_identity,
                )
            )
        stale_blocked = False
        if handoff:
            try:
                journal.activate_executor(
                    1,
                    "supervisor",
                    os.getpid(),
                    lease_deadline_ns=time.monotonic_ns() + 1_000_000_000,
                )
            except JournalError:
                stale_blocked = True
            if not stale_blocked:
                raise SupervisorProbeError("dead supervisor authority was reused")
            _python_exception_checkpoint(
                "actor_loss", "dead_executor_reuse_rejected"
            )
        while time.monotonic_ns() <= before_recovery.record.lease_deadline_ns:
            time.sleep(0.001)
        _python_exception_checkpoint("actor_loss", "executor_lease_expired")
        authority_deadline = time.monotonic_ns() + (
            50_000_000 if name == "retirement_replacement" else 1_000_000_000
        )
        retired = journal.retire_executor(
            authority="reconciler-1",
            authority_epoch=1,
            authority_deadline_ns=authority_deadline,
        )
        _python_exception_checkpoint("actor_loss", "executor_retired")
        handoff_records = 1
        if name == "retirement_replacement":
            while time.monotonic_ns() <= authority_deadline:
                time.sleep(0.001)
            retired = journal.replace_retirement_authority(
                authority="reconciler-2",
                authority_epoch=2,
                authority_deadline_ns=time.monotonic_ns() + 1_000_000_000,
            )
            _python_exception_checkpoint(
                "actor_loss", "retirement_authority_replaced"
            )
            handoff_records += 1
        proof = journal.confirm_executor_reaped(
            deadline_ns=time.monotonic_ns() + 5_000_000_000
        )
        owner.executor_reap_proof = proof
        executor_reaped = True
        process.returncode = exit_code
        _python_exception_checkpoint("actor_loss", "executor_reap_confirmed")
        reconciled = None
        if retired.record.kind.name == "RETIRING_BATCH":
            reconciled = journal.reconcile_interrupted_batch(proof)
            _python_exception_checkpoint(
                "actor_loss", "interrupted_batch_reconciled"
            )
            handoff_records += 1
        successor = None
        activated = None
        if handoff:
            successor = journal.prepare_successor(
                proof,
                2,
                "recovery-2",
                claim_deadline_ns=time.monotonic_ns() + 1_000_000_000,
            )
            _python_exception_checkpoint("actor_loss", "successor_prepared")
            activated = journal.activate_executor(
                2,
                "recovery-2",
                os.getpid(),
                lease_deadline_ns=time.monotonic_ns() + 1_000_000_000,
            )
            _python_exception_checkpoint("actor_loss", "successor_activated")
            handoff_records += 2
        group_stopped = injection_code in {2, 3} and _process_is_stopped(
            anchor_identity[0]
        )
        _python_exception_checkpoint("actor_loss", "group_state_observed")
        terminal = journal.mark_unconfirmed(UnconfirmedReason.PROOF_UNAVAILABLE)
        _python_exception_checkpoint("actor_loss", "unconfirmed_persisted")
        certified = journal.certify_head()
        _python_exception_checkpoint("actor_loss", "unconfirmed_certified")
        retained = True
        _RETAINED_UNCONFIRMED_PATHS.append(instance)
        canonical_sequences = (
            supervisor_sequence,
            anchor_sequence,
            armed_sequence,
            running.sequence,
        )
        evidence = LifecycleEvidence(
            scenario=name,
            outcome="unconfirmed",
            stop_used=injection_code in {2, 3, 4, 5, 6},
            group_enumerated_while_stopped=injection_code in {3, 4, 5, 6},
            term_used=injection_code in {5, 6},
            kill_used=injection_code == 6,
            workdir_removed=False,
            durable_delete_receipt=False,
            canonical_head_certified=(
                certified.sequence == terminal.sequence
                and certified.state.kind.name == "UNCONFIRMED"
            ),
            task4_authority_used=True,
            stale_executor_blocked=stale_blocked,
            exact_batch_preserved=(
                not batch_active
                or (
                    reconciled is not None
                    and reconciled.record.exact_batch == exact_batch
                )
            ),
            unconfirmed_persistent=True,
            successor_activated=(
                successor is not None
                and activated is not None
                and successor.record.generation == 2
                and activated.record.generation == 2
            ),
            exit_refused=True,
            next_stage_spawned=True,
            cli_exec_count=1,
            artifacts_retained=(instance / _WORKDIR_NAME).is_dir()
            and (instance / _JOURNAL_NAME).is_file(),
            control_trace=tuple(trace),
            canonical_control_sequences=canonical_sequences,
            cleanup_request_authenticated=(
                cleanup_sequence == 5
            ),
            same_canonical_journal=(
                canonical_sequences == (1, 2, 3, 4)
                and injected.sequence == 6
                and terminal.sequence > retired.sequence
            ),
            evidence_observed_not_inferred=True,
            control_fd_phase_enforced=True,
            cleanup_failure_injection_observed=injected.payload == error_payload,
            process_batch_admitted_before_signal=(
                injection_code == 11 or batch_active
            ),
            process_batch_target_exact=(
                injection_code == 11 or process_target_exact
            ),
            action_token_retained_through_signals=injection_code in {5, 6},
            group_stopped_at_recovery=group_stopped,
            actor_loss_observed=True,
            actor_loss_exit_code=exit_code,
            recovery_executor_reaped=proof.pid == process.pid,
            action_lock_released_after_actor_loss=(
                retired.sequence > before_recovery.sequence
                and (not batch_active or reconciled is not None)
            ),
            production_recovery_signal_count=0,
            original_supervisor_actor_chain=True,
            observed_handoff_records=handoff_records,
        )
        _python_exception_checkpoint("actor_loss", "evidence_captured")
        _register_retained_path(owner)
        teardown = _test_release_retained_actor_chain(owner.key)
        if not teardown.group_absent or not teardown.supervisor_child_reaped:
            raise SupervisorProbeError(
                "fresh actor-loss chain survived test teardown"
            )
        return replace(
            evidence,
            test_teardown_group_absent=teardown.group_absent,
            untracked_orphan_count=teardown.untracked_orphan_count,
        )
    except BaseException:
        retained_owner = reservation.owner
        if retained_owner is not None:
            _reconcile_owner_best_effort(retained_owner)
        raise
    finally:
        retained_owner = reservation.owner
        reservation.cancel_if_reserved()
        locally_owned = retained_owner is None
        if parent_control is not None and locally_owned:
            parent_control.close()
        if child_control is not None:
            child_control.close()
        if parent_fallback is not None and locally_owned:
            parent_fallback.close()
        if child_fallback is not None:
            child_fallback.close()
        if (
            process is not None
            and locally_owned
            and anchor_identity is None
            and not executor_reaped
        ):
            process.wait(timeout=5)
        if (
            process is not None
            and process.stderr is not None
            and locally_owned
        ):
            process.stderr.close()
        if journal is not None and locally_owned:
            journal.close()
        if locally_owned:
            os.close(parent_dirfd)
        if not retained:
            _RETAINED_UNCONFIRMED_PATHS.append(instance)


def _run_one_real_control_rejection(case: str) -> ctypes.CDLL:
    instance = Path(tempfile.mkdtemp(prefix="claude-real-control-reject-"))
    parent_dirfd = _open_private_directory(instance)
    journal: Journal | None = None
    parent_control: socket.socket | None = None
    child_control: socket.socket | None = None
    parent_fallback: socket.socket | None = None
    child_fallback: socket.socket | None = None
    process: subprocess.Popen[bytes] | None = None
    library: ctypes.CDLL | None = None
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
        library = journal._library
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
        process = subprocess.Popen(
            [str(_supervisor_path()), "--output-path", str(instance / "unused")],
            env={
                "HOME": str(instance),
                "USER": "control-rejection-probe",
                "LOCAL_PROXY_ALLOCATION_NONCE": _NONCE_HEX,
                "LOCAL_PROXY_INSTANCE_DIR": str(instance),
                "LOCAL_PROXY_REAL_CLAUDE": str(_probe_child_path()),
                "LOCAL_PROXY_CONTROL_FD": str(child_control.fileno()),
                "LOCAL_PROXY_ANCHOR_CONTROL_FD": str(child_fallback.fileno()),
                "LOCAL_PROXY_NETWORK_PROXY": "0",
            },
            pass_fds=(child_control.fileno(), child_fallback.fileno()),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        child_control.close()
        child_control = None
        child_fallback.close()
        child_fallback = None
        try:
            _, identity_payload = _receive_control_frame(parent_control, 1)
        except SupervisorProbeError as error:
            returncode = process.wait(timeout=5)
            state = journal.certify_head().state.kind.name
            diagnostics = process.stderr.read().decode() if process.stderr else ""
            raise SupervisorProbeError(
                f"control rejection startup failed ({returncode}, {state}, "
                f"{diagnostics.strip()})"
            ) from error
        if journal.certify_bootstrap(1, identity_payload).sequence != 1:
            raise SupervisorProbeError("control rejection used a stale head")
        nonce = _NONCE
        message_type = 2
        payload_length = 0
        if case == "unknown_type":
            message_type = 99
        elif case == "wrong_nonce":
            nonce = bytes([_NONCE[0] ^ 0xFF]) + _NONCE[1:]
        elif case == "duplicate_phase":
            message_type = 1
        elif case == "phase_regression":
            message_type = 7
        elif case == "oversize_payload":
            payload_length = _CONTROL_MAX_PAYLOAD + 1
        header = struct.pack(
            "<IHHI32s",
            _CONTROL_MAGIC,
            _CONTROL_VERSION,
            message_type,
            payload_length,
            nonce,
        )
        wire = header + struct.pack("<I", _crc32c(header))
        if case == "bad_checksum":
            wire = wire[:-1] + bytes([wire[-1] ^ 0xFF])
        parent_control.sendall(wire)
        if process.wait(timeout=10) != 75:
            raise SupervisorProbeError(
                f"control rejection {case} did not fail closed"
            )
        terminal = journal.mark_unconfirmed(UnconfirmedReason.PROOF_UNAVAILABLE)
        if terminal.sequence == 0:
            raise SupervisorProbeError("control rejection was not retained")
        _RETAINED_UNCONFIRMED_PATHS.append(instance)
        if library is None:
            raise SupervisorProbeError("control library was unavailable")
        return library
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


def _run_real_cleanup_request_binding_rejection(case: str) -> bool:
    instance = Path(tempfile.mkdtemp(prefix="claude-real-cleanup-proof-reject-"))
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
    retained = False
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
        process = subprocess.Popen(
            [
                str(_supervisor_path()),
                "--output-path",
                str(instance / "cleanup-proof-output"),
            ],
            env={
                "HOME": str(instance),
                "USER": "cleanup-proof-rejection-probe",
                "LOCAL_PROXY_ALLOCATION_NONCE": _NONCE_HEX,
                "LOCAL_PROXY_INSTANCE_DIR": str(instance),
                "LOCAL_PROXY_REAL_CLAUDE": str(_probe_child_path()),
                "LOCAL_PROXY_CONTROL_FD": str(child_control.fileno()),
                "LOCAL_PROXY_ANCHOR_CONTROL_FD": str(child_fallback.fileno()),
                "LOCAL_PROXY_NETWORK_PROXY": "0",
            },
            pass_fds=(child_control.fileno(), child_fallback.fileno()),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        child_control.close()
        child_control = None
        child_fallback.close()
        child_fallback = None
        trace: list[str] = []
        _certify_and_ack(parent_control, journal, 1, 2, trace)
        anchor_payload, _, _ = _certify_and_ack(
            parent_control, journal, 3, 4, trace
        )
        _certify_and_ack(parent_control, journal, 5, 6, trace)
        _, running_payload = _receive_control_frame(parent_control, 7)
        running = journal.certify_bootstrap(7, running_payload)
        anchor_identity = _parse_process_identity(anchor_payload)
        expectation = _prepare_cleanup_request(journal)
        if case == "wrong_sequence":
            supplied = struct.pack(
                "<Q32s", expectation.sequence - 1, expectation.hash
            )
        elif case == "wrong_hash":
            supplied = struct.pack(
                "<Q32s",
                expectation.sequence,
                bytes([expectation.hash[0] ^ 0xFF]) + expectation.hash[1:],
            )
        else:
            raise SupervisorProbeError("unknown cleanup proof rejection")
        parent_control.sendall(_encode_control_frame(8, supplied))
        if process.wait(timeout=5) != 75:
            raise SupervisorProbeError("invalid cleanup proof did not fail closed")
        if parent_control.recv(1) != b"":
            raise SupervisorProbeError("invalid cleanup proof produced a reply")
        observed = journal.observe_process(anchor_identity[0])
        if (
            not _same_parsed_identity(observed, anchor_identity)
            or _process_is_stopped(anchor_identity[0])
            or journal.certify_bootstrap(8, b"").sequence
            != expectation.sequence
            or running.sequence != 4
        ):
            raise SupervisorProbeError("invalid cleanup proof changed the actor")
        terminal = journal.mark_unconfirmed(UnconfirmedReason.PROOF_UNAVAILABLE)
        if terminal.sequence == 0:
            raise SupervisorProbeError("cleanup proof rejection was not retained")
        if not _teardown_fresh_actor_group(
            journal, anchor_identity, stopped=False
        ):
            raise SupervisorProbeError("cleanup proof test group survived teardown")
        retained = True
        _RETAINED_UNCONFIRMED_PATHS.append(instance)
        return True
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
        if anchor_identity is not None and journal is not None and not retained:
            _teardown_fresh_actor_group(journal, anchor_identity, stopped=False)
        if journal is not None:
            journal.close()
        os.close(parent_dirfd)
        if not retained:
            _RETAINED_UNCONFIRMED_PATHS.append(instance)


def _run_real_control_validation() -> LifecycleEvidence:
    cases = (
        "unknown_type",
        "wrong_nonce",
        "duplicate_phase",
        "phase_regression",
        "oversize_payload",
        "bad_checksum",
    )
    rejected: list[str] = []
    library: ctypes.CDLL | None = None
    for case in cases:
        library = _run_one_real_control_rejection(case)
        rejected.append(case)
    cleanup_request_binding_rejections = tuple(
        case
        for case in ("wrong_sequence", "wrong_hash")
        if _run_real_cleanup_request_binding_rejection(case)
    )
    if library is None:
        raise SupervisorProbeError("native control library was unavailable")
    phase = ctypes.c_uint32(1)
    ack_without_certification_rejected = int(
        library.cpl_control_phase_accept(ctypes.byref(phase), 2, False)
    ) != 0
    request_hash = hashlib.sha256(b"caller-owned-cleanup-request").digest()
    expected = _CleanupAckExpectation(5, request_hash)
    exact_payload = struct.pack("<Q32s", expected.sequence, expected.hash)
    cleanup_ack_accept_count = 0
    cleanup_action_release_count = 0
    cleanup_phase = ctypes.c_uint32(8)
    if int(
        library.cpl_control_phase_accept(
            ctypes.byref(cleanup_phase), 12, True
        )
    ) != 0:
        raise SupervisorProbeError("certified cleanup ACK phase was rejected")
    _accept_cleanup_ack(expected, "CLEANUP_ACK", exact_payload)
    cleanup_ack_accept_count += 1
    cleanup_action_release_count += 1
    ack_rejections: list[str] = []
    rejection_cases = (
        ("duplicate", expected, exact_payload),
        (
            "stale_sequence",
            _CleanupAckExpectation(5, request_hash),
            struct.pack("<Q32s", 4, request_hash),
        ),
        (
            "wrong_hash",
            _CleanupAckExpectation(5, request_hash),
            struct.pack("<Q32s", 5, bytes([request_hash[0] ^ 0xFF])
                        + request_hash[1:]),
        ),
    )
    for case, expectation, payload in rejection_cases:
        try:
            _accept_cleanup_ack(expectation, "CLEANUP_ACK", payload)
        except SupervisorProbeError:
            ack_rejections.append(case)
        else:
            cleanup_action_release_count += 1
    duplicate_phase_rejected = int(
        library.cpl_control_phase_accept(
            ctypes.byref(cleanup_phase), 12, True
        )
    ) != 0
    return LifecycleEvidence(
        scenario="control_frame_validation",
        outcome="unconfirmed",
        canonical_head_certified=len(rejected) == len(cases),
        task4_authority_used=True,
        fail_dead_exit_code=75,
        cli_exec_count=0,
        control_rejections=tuple(rejected),
        ack_without_certification_rejected=ack_without_certification_rejected,
        artifacts_retained=True,
        same_canonical_journal=len(rejected) == len(cases),
        evidence_observed_not_inferred=True,
        control_fd_phase_enforced=True,
        cleanup_ack_rejections=tuple(ack_rejections),
        cleanup_ack_accept_count=cleanup_ack_accept_count,
        cleanup_action_release_count=cleanup_action_release_count,
        cleanup_ack_phase_latched=(
            cleanup_phase.value == 12 and duplicate_phase_rejected
        ),
        rejected_cleanup_ack_no_action=cleanup_action_release_count == 1,
        cleanup_request_binding_rejections=cleanup_request_binding_rejections,
        rejected_cleanup_request_no_signal=(
            len(cleanup_request_binding_rejections) == 2
        ),
    )


def _run_real_supervisorless_fallback(name: str) -> LifecycleEvidence:
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
        if name == "fallback_early_before_running":
            parent_fallback.sendall(_encode_control_frame(9))
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
        if name == "fallback_while_supervisor_healthy":
            parent_fallback.sendall(_encode_control_frame(9))
        if name in {
            "fallback_while_supervisor_healthy",
            "fallback_early_before_running",
        }:
            before_rejection = journal.certify_head().sequence
            time.sleep(0.05)
            os.kill(anchor_identity[0], 0)
            if journal.certify_head().sequence != before_rejection:
                raise SupervisorProbeError(
                    "fallback request changed the healthy lifecycle"
                )
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
        request_type = 9
        request_payload = b""
        if name == "fallback_nonempty_payload_after_loss":
            request_payload = b"not-allowed"
        elif name == "fallback_wrong_phase_after_loss":
            request_type = 8
        invalid_request = name in {
            "fallback_while_supervisor_healthy",
            "fallback_early_before_running",
            "fallback_nonempty_payload_after_loss",
            "fallback_wrong_phase_after_loss",
        }
        try:
            if name not in {
                "fallback_while_supervisor_healthy",
                "fallback_early_before_running",
            }:
                parent_fallback.sendall(
                    _encode_control_frame(request_type, request_payload)
                )
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
        if invalid_request:
            before_rejection = journal.certify_head().sequence
            time.sleep(0.05)
            os.kill(anchor_identity[0], 0)
            after_rejection = journal.certify_head().sequence
            if after_rejection != before_rejection:
                raise SupervisorProbeError(
                    "rejected fallback request changed the lifecycle"
                )
            terminal = journal.mark_unconfirmed(
                UnconfirmedReason.PROOF_UNAVAILABLE
            )
            retained = True
            return LifecycleEvidence(
                scenario=name,
                outcome="unconfirmed",
                anchor_alive=True,
                canonical_head_certified=True,
                task4_authority_used=True,
                unconfirmed_persistent=True,
                exit_refused=True,
                next_stage_spawned=True,
                cli_exec_count=1,
                artifacts_retained=True,
                same_canonical_journal=terminal.sequence > running_head.sequence,
                evidence_observed_not_inferred=True,
                control_fd_phase_enforced=True,
                fallback_request_rejected=True,
                rejected_request_no_signal=True,
                supervisor_loss_proven=True,
                self_term_signal_count=0,
            )
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
        duplicate_rejected = False
        if name == "fallback_duplicate_after_loss":
            before_duplicate = journal.certify_head().sequence
            parent_fallback.sendall(_encode_control_frame(9))
            time.sleep(0.05)
            os.kill(anchor_identity[0], 0)
            duplicate_rejected = (
                journal.certify_head().sequence == before_duplicate
            )
            if not duplicate_rejected:
                raise SupervisorProbeError(
                    "duplicate fallback request changed the lifecycle"
                )
        retained = True
        return LifecycleEvidence(
            scenario=name,
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
            fallback_request_rejected=duplicate_rejected,
            rejected_request_no_signal=duplicate_rejected,
            supervisor_loss_proven=True,
            fallback_payload_exact=self_term.payload == b"",
            self_term_signal_count=1,
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


def _encode_control_frame(message_type: int, payload: bytes = b"") -> bytes:
    if message_type not in _CONTROL_TYPES:
        raise SupervisorProbeError("invalid local control message")
    if not isinstance(payload, bytes) or len(payload) > _CONTROL_MAX_PAYLOAD:
        raise SupervisorProbeError("invalid local control payload")
    header = struct.pack(
        "<IHHI32s",
        _CONTROL_MAGIC,
        _CONTROL_VERSION,
        message_type,
        len(payload),
        _NONCE,
    )
    return header + payload + struct.pack("<I", _crc32c(header + payload))


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
    process_batch_admission_sequence: int
    injection_stage: int


@dataclass
class _CleanupAckExpectation:
    sequence: int
    hash: bytes
    consumed: bool = False


def _prepare_cleanup_request(journal: Journal) -> _CleanupAckExpectation:
    appended = journal.append_bootstrap(8, b"")
    certified = journal.certify_bootstrap(8, b"")
    if (
        appended.sequence != certified.sequence
        or appended.hash != certified.hash
        or appended.payload != b""
    ):
        raise SupervisorProbeError("cleanup request certification drifted")
    return _CleanupAckExpectation(certified.sequence, certified.hash)


def _cleanup_request_frame(expectation: _CleanupAckExpectation) -> bytes:
    return _encode_control_frame(8, struct.pack("<Q32s", expectation.sequence,
                                                expectation.hash))


def _send_cleanup_request_with_ambiguity(
    owner: _RetainedActorChain,
    control: socket.socket,
    frame: bytes,
) -> None:
    """Treat delivery as ambiguous before byte one and expose exact boundaries."""
    owner.cleanup_request_may_have_been_delivered = True
    _python_exception_checkpoint(owner.flow, "cleanup_write_before_first_byte")
    split = len(frame) // 2
    control.sendall(frame[:split])
    _python_exception_checkpoint(owner.flow, "cleanup_write_after_partial")
    control.sendall(frame[split:])
    _python_exception_checkpoint(owner.flow, "cleanup_write_after_full")


def _accept_cleanup_ack(
    expectation: _CleanupAckExpectation,
    name: str,
    payload: bytes,
) -> None:
    if expectation.consumed:
        raise SupervisorProbeError("cleanup ACK was replayed")
    if name != "CLEANUP_ACK" or len(payload) != 40:
        raise SupervisorProbeError("cleanup ACK is malformed")
    sequence, ack_hash = struct.unpack("<Q32s", payload)
    if sequence != expectation.sequence or ack_hash != expectation.hash:
        raise SupervisorProbeError("cleanup ACK does not name the request")
    expectation.consumed = True


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
    expectation = _prepare_cleanup_request(journal)
    control.sendall(_cleanup_request_frame(expectation))
    ack_name, ack_payload = _receive_control_frame(control, 12)
    _accept_cleanup_ack(expectation, ack_name, ack_payload)
    cleanup_sequence, cleanup_hash = struct.unpack("<Q32s", ack_payload)
    if not expectation.consumed or cleanup_hash != expectation.hash:
        raise SupervisorProbeError("native cleanup ACK was not consumed")
    result_name, result_payload = _receive_control_frame(control, 10)
    if result_name != "CLEANUP_RESULT" or len(result_payload) != 40:
        raise SupervisorProbeError("native cleanup result is malformed")
    result_sequence, _ = _certify_bootstrap_event(journal, 10, result_payload)
    (
        flags,
        batch_count,
        completed_steps,
        done_sequence,
        process_batch_admission_sequence,
        injection_stage,
        reserved,
    ) = struct.unpack(
        "<IIQQQII", result_payload
    )
    if reserved != 0:
        raise SupervisorProbeError("native cleanup result reserved field is nonzero")
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
        process_batch_admission_sequence=process_batch_admission_sequence,
        injection_stage=injection_stage,
    )


def _run_real_identity_rejection(name: str) -> LifecycleEvidence:
    injection_by_name = {
        "altered_executable_identity": "altered_executable_identity",
        "reused_pid": "reused_pid",
        "unexpected_descendant": "unexpected_descendant",
    }
    reason_by_code = {
        8: "altered_executable_identity",
        9: "reused_pid",
        10: "unexpected_descendant",
    }
    instance = Path(tempfile.mkdtemp(prefix="claude-real-rejection-"))
    parent_dirfd = _open_private_directory(instance)
    journal: Journal | None = None
    parent_control: socket.socket | None = None
    child_control: socket.socket | None = None
    parent_fallback: socket.socket | None = None
    child_fallback: socket.socket | None = None
    process: subprocess.Popen[bytes] | None = None
    retained_anchor: (
        tuple[int, int, int, int, int, int, int, bytes, bytes] | None
    ) = None
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
        output_path = instance / "rejection-output"
        environment = {
            "HOME": str(instance),
            "USER": "rejection-probe",
            "LOCAL_PROXY_ALLOCATION_NONCE": _NONCE_HEX,
            "LOCAL_PROXY_INSTANCE_DIR": str(instance),
            "LOCAL_PROXY_REAL_CLAUDE": str(_probe_child_path()),
            "LOCAL_PROXY_CONTROL_FD": str(child_control.fileno()),
            "LOCAL_PROXY_ANCHOR_CONTROL_FD": str(child_fallback.fileno()),
            "LOCAL_PROXY_NETWORK_PROXY": "0",
            "LOCAL_PROXY_TEST_INJECTION": injection_by_name[name],
        }
        arguments = [str(_probe_supervisor_path())]
        if name == "altered_executable_identity":
            arguments.append("--exec-different")
        else:
            arguments.extend(("--output-path", str(output_path)))
            if name == "unexpected_descendant":
                arguments.append("--spawn-descendant")
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
        _, supervisor_sequence, _ = _certify_and_ack(
            parent_control, journal, 1, 2, trace
        )
        anchor_payload, anchor_sequence, _ = _certify_and_ack(
            parent_control, journal, 3, 4, trace
        )
        armed_payload, armed_sequence, _ = _certify_and_ack(
            parent_control, journal, 5, 6, trace
        )
        anchor_identity = _parse_process_identity(anchor_payload)
        retained_anchor = anchor_identity
        running_sequence = 0
        cleanup_sequence = 0
        if name != "altered_executable_identity":
            running_name, running_payload = _receive_control_frame(
                parent_control, 7
            )
            trace.append(running_name)
            running = journal.certify_bootstrap(7, running_payload)
            running_sequence = running.sequence
            expectation = _prepare_cleanup_request(journal)
            cleanup_sequence = expectation.sequence
            parent_control.sendall(_cleanup_request_frame(expectation))
            ack_name, ack_payload = _receive_control_frame(parent_control, 12)
            _accept_cleanup_ack(expectation, ack_name, ack_payload)
        error_name, error_payload = _receive_control_frame(parent_control, 11)
        if error_name != "ERROR" or len(error_payload) != 120:
            raise SupervisorProbeError("cleanup rejection frame is malformed")
        rejection = journal.certify_bootstrap(11, error_payload)
        reason_code, unexpected_member_count = struct.unpack_from(
            "<II", error_payload
        )
        reason = reason_by_code.get(reason_code)
        if reason is None:
            raise SupervisorProbeError("cleanup rejection reason is unknown")
        rejection_observed = _parse_process_identity(error_payload[8:])
        returncode = process.wait(timeout=5)
        if returncode != 75:
            raise SupervisorProbeError("rejected cleanup did not fail closed")
        altered_mismatch = False
        reuse_rejected = False
        if name == "altered_executable_identity":
            armed_member = _parse_process_identity(armed_payload[:112])
            expected_dev, expected_ino = struct.unpack_from(
                "<QQ", armed_payload, 112
            )
            expected_hash = armed_payload[128:160]
            observed_executable = (
                rejection_observed[5],
                rejection_observed[6],
                rejection_observed[8],
            )
            altered_mismatch = (
                observed_executable != (expected_dev, expected_ino, expected_hash)
                and observed_executable
                != (armed_member[5], armed_member[6], armed_member[8])
            )
            if not altered_mismatch:
                raise SupervisorProbeError("post-ARMED executable did not change")
        elif name == "reused_pid":
            simulated_identity = (
                anchor_identity[0],
                anchor_identity[1] + 1,
                *anchor_identity[2:],
            )
            reuse_rejected = rejection_observed == simulated_identity
            if not reuse_rejected:
                raise SupervisorProbeError("reuse observation was not preserved")
        elif (
            unexpected_member_count < 1
            or rejection_observed != anchor_identity
        ):
            raise SupervisorProbeError("unexpected group evidence was incomplete")
        terminal = journal.mark_unconfirmed(UnconfirmedReason.IDENTITY_UNAVAILABLE)
        teardown_absent = _teardown_fresh_actor_group(
            journal, anchor_identity, stopped=False
        )
        if not teardown_absent:
            teardown_absent = _teardown_recorded_fresh_group(anchor_identity[3])
        remaining_members = _enumerate_exact_group(anchor_identity[3])
        group_capability_absent = _group_capability_absent(anchor_identity[3])
        untracked_orphan_count = len(remaining_members)
        if (
            not teardown_absent
            or remaining_members
            or not group_capability_absent
        ):
            raise SupervisorProbeError("identity test group survived teardown")
        retain_path = True
        _RETAINED_UNCONFIRMED_PATHS.append(instance)
        sequences: tuple[int, ...]
        expected_sequences: tuple[int, ...]
        if name == "altered_executable_identity":
            sequences = (
                supervisor_sequence,
                anchor_sequence,
                armed_sequence,
                rejection.sequence,
            )
            expected_sequences = (1, 2, 3, 4)
        else:
            sequences = (
                supervisor_sequence,
                anchor_sequence,
                armed_sequence,
                running_sequence,
                cleanup_sequence,
                rejection.sequence,
            )
            expected_sequences = (1, 2, 3, 4, 5, 6)
        return LifecycleEvidence(
            scenario=name,
            outcome="unconfirmed",
            anchor_alive=False,
            identity_mismatch_detected=reason in reason_by_code.values(),
            canonical_head_certified=True,
            task4_authority_used=True,
            unconfirmed_persistent=True,
            exit_refused=True,
            next_stage_spawned=True,
            cli_exec_count=1,
            artifacts_retained=True,
            control_trace=tuple(trace),
            canonical_control_sequences=sequences,
            same_canonical_journal=(
                sequences == expected_sequences
                and terminal.sequence > 0
            ),
            evidence_observed_not_inferred=True,
            control_fd_phase_enforced=True,
            cleanup_rejection_authenticated=True,
            observed_rejection_reason=reason,
            original_supervisor_actor_chain=True,
            post_armed_executable_mismatch_observed=altered_mismatch,
            simulated_reuse_observation_rejected=reuse_rejected,
            unexpected_group_member_count=unexpected_member_count,
            production_recovery_signal_count=0,
            test_teardown_group_absent=teardown_absent,
            untracked_orphan_count=untracked_orphan_count,
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
        if retained_anchor is not None and journal is not None and not retain_path:
            if not _teardown_fresh_actor_group(
                journal, retained_anchor, stopped=False
            ):
                _teardown_recorded_fresh_group(retained_anchor[3])
        if journal is not None:
            journal.close()
        os.close(parent_dirfd)
        if not retain_path:
            _RETAINED_UNCONFIRMED_PATHS.append(instance)


def _run_real_retaining_cleanup(name: str) -> LifecycleEvidence:
    injection_names = {
        "anchor_only_cleanup": "anchor_only",
    }
    injection = injection_names.get(name)
    stubborn = name == "stubborn_child_kill"
    directory = tempfile.TemporaryDirectory(prefix="claude-real-cleanup-")
    retain_directory = False
    instance = Path(directory.name)
    parent_dirfd = _open_private_directory(instance)
    journal: Journal | None = None
    parent_control: socket.socket | None = None
    child_control: socket.socket | None = None
    parent_fallback: socket.socket | None = None
    child_fallback: socket.socket | None = None
    process: subprocess.Popen[bytes] | None = None
    cleanup_finished = False
    collision_probe_argv = False
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
        anchor_only_exit_marker = instance / "anchor-only-exit"
        environment = {
            "HOME": str(instance),
            "USER": "cleanup-probe",
            "LOCAL_PROXY_ALLOCATION_NONCE": _NONCE_HEX,
            "LOCAL_PROXY_INSTANCE_DIR": str(instance),
            "LOCAL_PROXY_REAL_CLAUDE": str(_probe_child_path()),
            "LOCAL_PROXY_CONTROL_FD": str(child_control.fileno()),
            "LOCAL_PROXY_ANCHOR_CONTROL_FD": str(child_fallback.fileno()),
            "LOCAL_PROXY_NETWORK_PROXY": "0",
            "LOCAL_PROXY_PROBE_SCENARIO_TOKEN": "task5-local-only",
        }
        supervisor = _probe_supervisor_path() if injection else _supervisor_path()
        if injection:
            environment["LOCAL_PROXY_TEST_INJECTION"] = injection
        if name == "ordinary_probe_argument_collision":
            arguments = [
                str(supervisor),
                "--probe-scenario",
                "confirmed_reap",
            ]
            collision_probe_argv = arguments[1:] == [
                "--probe-scenario",
                "confirmed_reap",
            ]
        else:
            arguments = [str(supervisor), "--output-path", str(output_path)]
        if stubborn:
            arguments.append("--stubborn")
        elif name == "anchor_only_cleanup":
            arguments.extend(("--exit-on-marker", str(anchor_only_exit_marker)))
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
        if name == "anchor_only_cleanup":
            marker_fd = os.open(
                anchor_only_exit_marker,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
            )
            os.close(marker_fd)
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
            | _CLEANUP_PROCESS_BATCH_PREAUTHORIZED
            | _CLEANUP_TOKEN_RETAINED_THROUGH_SIGNALS
            | _CLEANUP_PROCESS_TARGET_EXACT
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
            cleanup_failure_injection_observed=bool(
                flags & _CLEANUP_INJECTION_RECOVERED
            ),
            process_batch_admitted_before_signal=bool(
                flags & _CLEANUP_PROCESS_BATCH_PREAUTHORIZED
            ),
            process_batch_target_exact=bool(
                flags & _CLEANUP_PROCESS_TARGET_EXACT
            ),
            action_token_retained_through_signals=bool(
                flags & _CLEANUP_TOKEN_RETAINED_THROUGH_SIGNALS
            ),
            frozen_group_left_behind=not bool(
                flags & _CLEANUP_ABSENCE_ENUMERATED
                and flags & _CLEANUP_ANCHOR_REAPED
            ),
            anchor_only_group_observed=bool(
                flags & _CLEANUP_ANCHOR_ONLY_OBSERVED
            ),
            group_resumed_after_failure=bool(
                flags & _CLEANUP_GROUP_RESUMED_AFTER_FAILURE
            ),
            probe_mode_collision_impossible=(
                collision_probe_argv
                and supervisor == _supervisor_path()
                and same_journal
                and observation.delete_receipt
            ),
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
                retain_directory = True
                _RETAINED_CLEANUP_ACTORS.append(process)
                _RETAINED_CLEANUP_DIRECTORIES.append(directory)
        if process is not None and process.stderr is not None:
            process.stderr.close()
        if journal is not None and not journal.closed:
            journal.close()
        os.close(parent_dirfd)
        if not retain_directory:
            directory.cleanup()


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
        if name in {
            "cli_before_armed",
            "after_armed_before_exec",
            "pre_armed_fail_dead",
        }:
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
