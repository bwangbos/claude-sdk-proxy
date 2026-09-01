"""Typed data wrappers for the authoritative native lifecycle automaton."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Final, Self

HASH_SIZE: Final = 32
ID_SIZE: Final = 32
REASON_SIZE: Final = 64
WORKDIR_NAME_SIZE: Final = 64
MAX_BATCH_DESCRIPTORS: Final = 4

STEP_PROCESS_ABSENT: Final = 1 << 0
STEP_EXECUTOR_REAPED: Final = 1 << 1
STEP_WORKDIR_REMOVED: Final = 1 << 2
STEP_TERMINAL_CHECKS: Final = 1 << 3
ALL_COMPLETED_STEPS: Final = (
    STEP_PROCESS_ABSENT
    | STEP_EXECUTOR_REAPED
    | STEP_WORKDIR_REMOVED
    | STEP_TERMINAL_CHECKS
)


class StateKind(IntEnum):
    NO_GENERATION = 0
    PREPARED = 1
    ACTIVE_READY = 2
    BATCH_ACTIVE = 3
    RETIRING_IDLE = 4
    RETIRING_BATCH = 5
    DONE = 6
    UNCONFIRMED = 7


class RecordKind(IntEnum):
    INTENT = 1
    PREPARED = 2
    ACTIVE_READY = 3
    BATCH_ACTIVE = 4
    BATCH_DONE = 5
    RETIRING_IDLE = 6
    RETIRING_BATCH = 7
    REPLACE_AUTHORITY = 8
    DONE = 9
    UNCONFIRMED = 10


class BatchOutcome(IntEnum):
    NONE = 0
    COMPLETED = 1
    INTERRUPTED = 2


class BatchDescriptorKind(IntEnum):
    PROCESS_ABSENT = 1
    REAP_PROCESS = 2
    REMOVE_WORKDIR = 3
    TERMINAL_CHECKS = 4


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_ns: int
    uid: int
    pgid: int
    sid: int
    flags: int
    executable_dev: int
    executable_ino: int
    boot_id: bytes
    executable_hash: bytes


@dataclass(frozen=True)
class BatchDescriptor:
    kind: BatchDescriptorKind
    required_steps: int
    target: ProcessIdentity | None = None


class IllegalTransition(RuntimeError):
    """Raised when native authority rejects a lifecycle edge."""


class NativeLifecycleError(RuntimeError):
    """Raised when the native transition entry point itself fails."""

    def __init__(self, code: int) -> None:
        super().__init__(f"native lifecycle operation failed with code {code}")
        self.code = code


def _checked_text(value: str, size: int, field: str) -> bytes:
    if not isinstance(value, str) or "\0" in value:
        raise ValueError(f"{field} must be a text value without NUL")
    encoded = value.encode("utf-8")
    if len(encoded) >= size:
        raise ValueError(f"{field} is too long")
    return encoded


def _checked_parent(value: bytes | None) -> bytes:
    if value is None:
        return bytes(HASH_SIZE)
    if not isinstance(value, bytes) or len(value) != HASH_SIZE:
        raise ValueError("parent must be exactly 32 bytes")
    return value


@dataclass(frozen=True)
class State:
    kind: StateKind
    retained_kind: StateKind = StateKind.NO_GENERATION
    generation: int = 0
    cleanup_epoch: int = 0
    authority_epoch: int = 0
    deadline_ns: int = 0
    lease_deadline_ns: int = 0
    completed_steps: int = 0
    process_pid: int = 0
    process_start_ns: int = 0
    process_uid: int = 0
    process_pgid: int = 0
    process_sid: int = 0
    process_identity_flags: int = 0
    executable_dev: int = 0
    executable_ino: int = 0
    boot_id: bytes = bytes(HASH_SIZE)
    executable_hash: bytes = bytes(HASH_SIZE)
    batch_outcome: BatchOutcome = BatchOutcome.NONE
    descriptor_count: int = 0
    normal_limit: int = 0
    hard_limit: int = 0
    workdir_parent_dev: int = 0
    workdir_parent_ino: int = 0
    workdir_dev: int = 0
    workdir_ino: int = 0
    workdir_bound: bool = False
    no_dependent_artifact: bool = False
    candidate: str = ""
    executor: str = ""
    prior_actor: str = ""
    authority: str = ""
    exact_batch: str = ""
    inherited_batch: str = ""
    reason: str = ""
    workdir_name: str = ""
    descriptors: tuple[BatchDescriptor, ...] = ()

    @classmethod
    def no_generation(cls) -> Self:
        return cls(StateKind.NO_GENERATION)

    @classmethod
    def prepared(
        cls,
        generation: int,
        candidate: str,
        *,
        claim_deadline_ns: int,
        inherited_batch: str = "",
    ) -> Self:
        return cls(
            StateKind.PREPARED,
            generation=generation,
            deadline_ns=claim_deadline_ns,
            candidate=candidate,
            inherited_batch=inherited_batch,
        )

    @classmethod
    def active_ready(
        cls,
        generation: int,
        executor: str,
        *,
        lease_deadline_ns: int,
        completed_steps: int = 0,
        process_pid: int = 0,
        inherited_batch: str = "",
    ) -> Self:
        return cls(
            StateKind.ACTIVE_READY,
            generation=generation,
            lease_deadline_ns=lease_deadline_ns,
            completed_steps=completed_steps,
            process_pid=process_pid,
            executor=executor,
            inherited_batch=inherited_batch,
        )

    @classmethod
    def batch_active(
        cls,
        generation: int,
        batch_nonce: str,
        *,
        executor: str = "executor",
        lease_deadline_ns: int = 1,
        completed_steps: int = 0,
        process_pid: int = 0,
    ) -> Self:
        return cls(
            StateKind.BATCH_ACTIVE,
            generation=generation,
            lease_deadline_ns=lease_deadline_ns,
            completed_steps=completed_steps,
            process_pid=process_pid,
            executor=executor,
            exact_batch=batch_nonce,
        )

    @classmethod
    def retiring_idle(
        cls,
        generation: int,
        prior_actor: str,
        *,
        authority: str,
        authority_epoch: int,
        deadline_ns: int,
        exact_batch: str = "",
        batch_outcome: BatchOutcome = BatchOutcome.NONE,
    ) -> Self:
        return cls(
            StateKind.RETIRING_IDLE,
            generation=generation,
            authority_epoch=authority_epoch,
            deadline_ns=deadline_ns,
            batch_outcome=batch_outcome,
            prior_actor=prior_actor,
            authority=authority,
            exact_batch=exact_batch,
        )

    @classmethod
    def retiring_batch(
        cls,
        generation: int,
        batch_nonce: str,
        *,
        prior_executor: str,
        authority: str,
        authority_epoch: int,
        deadline_ns: int,
        completed_steps: int = 0,
        process_pid: int = 0,
    ) -> Self:
        return cls(
            StateKind.RETIRING_BATCH,
            generation=generation,
            authority_epoch=authority_epoch,
            deadline_ns=deadline_ns,
            completed_steps=completed_steps,
            process_pid=process_pid,
            executor=prior_executor,
            prior_actor=prior_executor,
            authority=authority,
            exact_batch=batch_nonce,
        )

    @classmethod
    def done(cls, generation: int, executor: str) -> Self:
        return cls(
            StateKind.DONE,
            generation=generation,
            completed_steps=ALL_COMPLETED_STEPS,
            executor=executor,
        )


@dataclass(frozen=True)
class Record:
    record_kind: RecordKind
    generation: int = 0
    cleanup_epoch: int = 0
    authority_epoch: int = 0
    deadline_ns: int = 0
    lease_deadline_ns: int = 0
    completed_steps: int = 0
    process_pid: int = 0
    process_start_ns: int = 0
    process_uid: int = 0
    process_pgid: int = 0
    process_sid: int = 0
    process_identity_flags: int = 0
    executable_dev: int = 0
    executable_ino: int = 0
    boot_id: bytes = bytes(HASH_SIZE)
    executable_hash: bytes = bytes(HASH_SIZE)
    batch_outcome: BatchOutcome = BatchOutcome.NONE
    descriptor_count: int = 0
    normal_limit: int = 0
    hard_limit: int = 0
    workdir_parent_dev: int = 0
    workdir_parent_ino: int = 0
    workdir_dev: int = 0
    workdir_ino: int = 0
    workdir_bound: bool = False
    no_dependent_artifact: bool = False
    parent: bytes | None = None
    candidate: str = ""
    executor: str = ""
    prior_actor: str = ""
    authority: str = ""
    exact_batch: str = ""
    inherited_batch: str = ""
    reason: str = ""
    workdir_name: str = ""
    descriptors: tuple[BatchDescriptor, ...] = ()

    @classmethod
    def prepared(
        cls,
        generation: int,
        candidate: str,
        *,
        claim_deadline_ns: int,
        parent: bytes | None = None,
    ) -> Self:
        return cls(
            RecordKind.PREPARED,
            generation=generation,
            deadline_ns=claim_deadline_ns,
            parent=parent,
            candidate=candidate,
        )

    @classmethod
    def active_ready(
        cls,
        generation: int,
        executor: str,
        *,
        lease_deadline_ns: int,
        completed_steps: int = 0,
        process_pid: int = 0,
        parent: bytes | None = None,
    ) -> Self:
        return cls(
            RecordKind.ACTIVE_READY,
            generation=generation,
            lease_deadline_ns=lease_deadline_ns,
            completed_steps=completed_steps,
            process_pid=process_pid,
            parent=parent,
            executor=executor,
        )

    @classmethod
    def batch_active(
        cls,
        generation: int,
        batch_nonce: str,
        *,
        executor: str,
        lease_deadline_ns: int,
        completed_steps: int = 0,
        parent: bytes | None = None,
    ) -> Self:
        return cls(
            RecordKind.BATCH_ACTIVE,
            generation=generation,
            lease_deadline_ns=lease_deadline_ns,
            completed_steps=completed_steps,
            parent=parent,
            executor=executor,
            exact_batch=batch_nonce,
        )

    @classmethod
    def batch_done(
        cls,
        generation: int,
        batch_nonce: str,
        *,
        executor: str,
        lease_deadline_ns: int,
        completed_steps: int,
        batch_outcome: BatchOutcome = BatchOutcome.NONE,
        parent: bytes | None = None,
    ) -> Self:
        return cls(
            RecordKind.BATCH_DONE,
            generation=generation,
            lease_deadline_ns=lease_deadline_ns,
            completed_steps=completed_steps,
            batch_outcome=batch_outcome,
            parent=parent,
            executor=executor,
            exact_batch=batch_nonce,
        )

    @classmethod
    def retiring_idle(
        cls,
        generation: int,
        prior_actor: str,
        *,
        authority: str,
        authority_epoch: int,
        deadline_ns: int,
        exact_batch: str = "",
        batch_outcome: BatchOutcome = BatchOutcome.NONE,
        parent: bytes | None = None,
    ) -> Self:
        return cls(
            RecordKind.RETIRING_IDLE,
            generation=generation,
            authority_epoch=authority_epoch,
            deadline_ns=deadline_ns,
            batch_outcome=batch_outcome,
            parent=parent,
            prior_actor=prior_actor,
            authority=authority,
            exact_batch=exact_batch,
        )

    @classmethod
    def retiring_batch(
        cls,
        generation: int,
        batch_nonce: str,
        *,
        prior_executor: str,
        authority: str,
        authority_epoch: int,
        deadline_ns: int,
        parent: bytes | None = None,
    ) -> Self:
        return cls(
            RecordKind.RETIRING_BATCH,
            generation=generation,
            authority_epoch=authority_epoch,
            deadline_ns=deadline_ns,
            parent=parent,
            prior_actor=prior_executor,
            authority=authority,
            exact_batch=batch_nonce,
        )

    @classmethod
    def replace_authority(
        cls,
        *,
        authority: str,
        authority_epoch: int,
        deadline_ns: int,
        parent: bytes | None = None,
    ) -> Self:
        return cls(
            RecordKind.REPLACE_AUTHORITY,
            authority_epoch=authority_epoch,
            deadline_ns=deadline_ns,
            parent=parent,
            authority=authority,
        )

    @classmethod
    def done(
        cls,
        generation: int,
        executor: str,
        *,
        parent: bytes | None = None,
    ) -> Self:
        return cls(
            RecordKind.DONE,
            generation=generation,
            parent=parent,
            executor=executor,
        )

    @classmethod
    def unconfirmed(cls, reason: str, *, parent: bytes | None = None) -> Self:
        return cls(RecordKind.UNCONFIRMED, parent=parent, reason=reason)

    def encode(self) -> bytes:
        """Encode the fixed pointer-free ABI payload for native append."""
        native = _record_to_c(self)
        return ctypes.string_at(ctypes.byref(native), ctypes.sizeof(native))


class _CProcessIdentity(ctypes.Structure):
    _fields_ = [
        ("pid", ctypes.c_int64),
        ("start_ns", ctypes.c_uint64),
        ("uid", ctypes.c_uint32),
        ("pgid", ctypes.c_int32),
        ("sid", ctypes.c_int32),
        ("flags", ctypes.c_uint32),
        ("executable_dev", ctypes.c_uint64),
        ("executable_ino", ctypes.c_uint64),
        ("boot_id", ctypes.c_uint8 * HASH_SIZE),
        ("executable_hash", ctypes.c_uint8 * HASH_SIZE),
    ]


class _CBatchDescriptor(ctypes.Structure):
    _fields_ = [
        ("kind", ctypes.c_uint32),
        ("required_steps", ctypes.c_uint32),
        ("target", _CProcessIdentity),
    ]


class _CState(ctypes.Structure):
    _fields_ = [
        ("kind", ctypes.c_uint32),
        ("retained_kind", ctypes.c_uint32),
        ("generation", ctypes.c_uint64),
        ("cleanup_epoch", ctypes.c_uint64),
        ("authority_epoch", ctypes.c_uint64),
        ("deadline_ns", ctypes.c_uint64),
        ("lease_deadline_ns", ctypes.c_uint64),
        ("completed_steps", ctypes.c_uint64),
        ("process_pid", ctypes.c_int64),
        ("process_start_ns", ctypes.c_uint64),
        ("process_uid", ctypes.c_uint32),
        ("process_pgid", ctypes.c_int32),
        ("process_sid", ctypes.c_int32),
        ("process_identity_flags", ctypes.c_uint32),
        ("executable_dev", ctypes.c_uint64),
        ("executable_ino", ctypes.c_uint64),
        ("batch_outcome", ctypes.c_uint32),
        ("descriptor_count", ctypes.c_uint32),
        ("normal_limit", ctypes.c_uint64),
        ("hard_limit", ctypes.c_uint64),
        ("workdir_parent_dev", ctypes.c_uint64),
        ("workdir_parent_ino", ctypes.c_uint64),
        ("workdir_dev", ctypes.c_uint64),
        ("workdir_ino", ctypes.c_uint64),
        ("workdir_bound", ctypes.c_uint32),
        ("no_dependent_artifact", ctypes.c_uint32),
        ("boot_id", ctypes.c_uint8 * HASH_SIZE),
        ("executable_hash", ctypes.c_uint8 * HASH_SIZE),
        ("candidate", ctypes.c_uint8 * ID_SIZE),
        ("executor", ctypes.c_uint8 * ID_SIZE),
        ("prior_actor", ctypes.c_uint8 * ID_SIZE),
        ("authority", ctypes.c_uint8 * ID_SIZE),
        ("exact_batch", ctypes.c_uint8 * ID_SIZE),
        ("inherited_batch", ctypes.c_uint8 * ID_SIZE),
        ("reason", ctypes.c_uint8 * REASON_SIZE),
        ("workdir_name", ctypes.c_uint8 * WORKDIR_NAME_SIZE),
        ("descriptors", _CBatchDescriptor * MAX_BATCH_DESCRIPTORS),
    ]


class _CRecord(ctypes.Structure):
    _fields_ = [
        ("kind", ctypes.c_uint32),
        ("batch_outcome", ctypes.c_uint32),
        ("generation", ctypes.c_uint64),
        ("cleanup_epoch", ctypes.c_uint64),
        ("authority_epoch", ctypes.c_uint64),
        ("deadline_ns", ctypes.c_uint64),
        ("lease_deadline_ns", ctypes.c_uint64),
        ("completed_steps", ctypes.c_uint64),
        ("process_pid", ctypes.c_int64),
        ("process_start_ns", ctypes.c_uint64),
        ("process_uid", ctypes.c_uint32),
        ("process_pgid", ctypes.c_int32),
        ("process_sid", ctypes.c_int32),
        ("process_identity_flags", ctypes.c_uint32),
        ("executable_dev", ctypes.c_uint64),
        ("executable_ino", ctypes.c_uint64),
        ("descriptor_count", ctypes.c_uint32),
        ("workdir_bound", ctypes.c_uint32),
        ("normal_limit", ctypes.c_uint64),
        ("hard_limit", ctypes.c_uint64),
        ("workdir_parent_dev", ctypes.c_uint64),
        ("workdir_parent_ino", ctypes.c_uint64),
        ("workdir_dev", ctypes.c_uint64),
        ("workdir_ino", ctypes.c_uint64),
        ("no_dependent_artifact", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("parent_hash", ctypes.c_uint8 * HASH_SIZE),
        ("boot_id", ctypes.c_uint8 * HASH_SIZE),
        ("executable_hash", ctypes.c_uint8 * HASH_SIZE),
        ("candidate", ctypes.c_uint8 * ID_SIZE),
        ("executor", ctypes.c_uint8 * ID_SIZE),
        ("prior_actor", ctypes.c_uint8 * ID_SIZE),
        ("authority", ctypes.c_uint8 * ID_SIZE),
        ("exact_batch", ctypes.c_uint8 * ID_SIZE),
        ("inherited_batch", ctypes.c_uint8 * ID_SIZE),
        ("reason", ctypes.c_uint8 * REASON_SIZE),
        ("workdir_name", ctypes.c_uint8 * WORKDIR_NAME_SIZE),
        ("descriptors", _CBatchDescriptor * MAX_BATCH_DESCRIPTORS),
    ]


def _set_bytes(target: ctypes.Array[ctypes.c_uint8], value: bytes) -> None:
    if value:
        ctypes.memmove(target, value, len(value))


def _identity_to_c(identity: ProcessIdentity | None) -> _CProcessIdentity:
    native = _CProcessIdentity()
    if identity is None:
        return native
    native.pid = identity.pid
    native.start_ns = identity.start_ns
    native.uid = identity.uid
    native.pgid = identity.pgid
    native.sid = identity.sid
    native.flags = identity.flags
    native.executable_dev = identity.executable_dev
    native.executable_ino = identity.executable_ino
    _set_bytes(native.boot_id, _checked_parent(identity.boot_id))
    _set_bytes(native.executable_hash, _checked_parent(identity.executable_hash))
    return native


def _identity_from_c(native: _CProcessIdentity) -> ProcessIdentity:
    return ProcessIdentity(
        pid=native.pid,
        start_ns=native.start_ns,
        uid=native.uid,
        pgid=native.pgid,
        sid=native.sid,
        flags=native.flags,
        executable_dev=native.executable_dev,
        executable_ino=native.executable_ino,
        boot_id=bytes(native.boot_id),
        executable_hash=bytes(native.executable_hash),
    )


def _descriptor_to_c(descriptor: BatchDescriptor) -> _CBatchDescriptor:
    native = _CBatchDescriptor()
    native.kind = descriptor.kind
    native.required_steps = descriptor.required_steps
    native.target = _identity_to_c(descriptor.target)
    return native


def _descriptor_from_c(native: _CBatchDescriptor) -> BatchDescriptor:
    target = _identity_from_c(native.target)
    return BatchDescriptor(
        kind=BatchDescriptorKind(native.kind),
        required_steps=native.required_steps,
        target=None if target.pid == 0 else target,
    )


def _state_to_c(state: State) -> _CState:
    native = _CState()
    native.kind = state.kind
    native.retained_kind = state.retained_kind
    native.generation = state.generation
    native.cleanup_epoch = state.cleanup_epoch
    native.authority_epoch = state.authority_epoch
    native.deadline_ns = state.deadline_ns
    native.lease_deadline_ns = state.lease_deadline_ns
    native.completed_steps = state.completed_steps
    native.process_pid = state.process_pid
    native.process_start_ns = state.process_start_ns
    native.process_uid = state.process_uid
    native.process_pgid = state.process_pgid
    native.process_sid = state.process_sid
    native.process_identity_flags = state.process_identity_flags
    native.executable_dev = state.executable_dev
    native.executable_ino = state.executable_ino
    native.batch_outcome = state.batch_outcome
    native.descriptor_count = state.descriptor_count
    native.normal_limit = state.normal_limit
    native.hard_limit = state.hard_limit
    native.workdir_parent_dev = state.workdir_parent_dev
    native.workdir_parent_ino = state.workdir_parent_ino
    native.workdir_dev = state.workdir_dev
    native.workdir_ino = state.workdir_ino
    native.workdir_bound = state.workdir_bound
    native.no_dependent_artifact = state.no_dependent_artifact
    _set_bytes(native.boot_id, _checked_parent(state.boot_id))
    _set_bytes(native.executable_hash, _checked_parent(state.executable_hash))
    _set_bytes(native.candidate, _checked_text(state.candidate, ID_SIZE, "candidate"))
    _set_bytes(native.executor, _checked_text(state.executor, ID_SIZE, "executor"))
    _set_bytes(
        native.prior_actor,
        _checked_text(state.prior_actor, ID_SIZE, "prior_actor"),
    )
    _set_bytes(native.authority, _checked_text(state.authority, ID_SIZE, "authority"))
    _set_bytes(
        native.exact_batch,
        _checked_text(state.exact_batch, ID_SIZE, "exact_batch"),
    )
    _set_bytes(
        native.inherited_batch,
        _checked_text(state.inherited_batch, ID_SIZE, "inherited_batch"),
    )
    _set_bytes(native.reason, _checked_text(state.reason, REASON_SIZE, "reason"))
    _set_bytes(
        native.workdir_name,
        _checked_text(state.workdir_name, WORKDIR_NAME_SIZE, "workdir_name"),
    )
    if len(state.descriptors) > MAX_BATCH_DESCRIPTORS:
        raise ValueError("too many batch descriptors")
    for index, descriptor in enumerate(state.descriptors):
        native.descriptors[index] = _descriptor_to_c(descriptor)
    return native


def _record_to_c(record: Record) -> _CRecord:
    native = _CRecord()
    native.kind = record.record_kind
    native.batch_outcome = record.batch_outcome
    native.generation = record.generation
    native.cleanup_epoch = record.cleanup_epoch
    native.authority_epoch = record.authority_epoch
    native.deadline_ns = record.deadline_ns
    native.lease_deadline_ns = record.lease_deadline_ns
    native.completed_steps = record.completed_steps
    native.process_pid = record.process_pid
    native.process_start_ns = record.process_start_ns
    native.process_uid = record.process_uid
    native.process_pgid = record.process_pgid
    native.process_sid = record.process_sid
    native.process_identity_flags = record.process_identity_flags
    native.executable_dev = record.executable_dev
    native.executable_ino = record.executable_ino
    native.descriptor_count = record.descriptor_count
    native.workdir_bound = record.workdir_bound
    native.normal_limit = record.normal_limit
    native.hard_limit = record.hard_limit
    native.workdir_parent_dev = record.workdir_parent_dev
    native.workdir_parent_ino = record.workdir_parent_ino
    native.workdir_dev = record.workdir_dev
    native.workdir_ino = record.workdir_ino
    native.no_dependent_artifact = record.no_dependent_artifact
    _set_bytes(native.parent_hash, _checked_parent(record.parent))
    _set_bytes(native.boot_id, _checked_parent(record.boot_id))
    _set_bytes(native.executable_hash, _checked_parent(record.executable_hash))
    _set_bytes(
        native.candidate,
        _checked_text(record.candidate, ID_SIZE, "candidate"),
    )
    _set_bytes(native.executor, _checked_text(record.executor, ID_SIZE, "executor"))
    _set_bytes(
        native.prior_actor,
        _checked_text(record.prior_actor, ID_SIZE, "prior_actor"),
    )
    _set_bytes(
        native.authority,
        _checked_text(record.authority, ID_SIZE, "authority"),
    )
    _set_bytes(
        native.exact_batch,
        _checked_text(record.exact_batch, ID_SIZE, "exact_batch"),
    )
    _set_bytes(
        native.inherited_batch,
        _checked_text(record.inherited_batch, ID_SIZE, "inherited_batch"),
    )
    _set_bytes(native.reason, _checked_text(record.reason, REASON_SIZE, "reason"))
    _set_bytes(
        native.workdir_name,
        _checked_text(record.workdir_name, WORKDIR_NAME_SIZE, "workdir_name"),
    )
    if len(record.descriptors) > MAX_BATCH_DESCRIPTORS:
        raise ValueError("too many batch descriptors")
    for index, descriptor in enumerate(record.descriptors):
        native.descriptors[index] = _descriptor_to_c(descriptor)
    return native


def _decode_text(value: ctypes.Array[ctypes.c_uint8]) -> str:
    return bytes(value).split(b"\0", 1)[0].decode("utf-8")


def _state_from_c(native: _CState) -> State:
    return State(
        kind=StateKind(native.kind),
        retained_kind=StateKind(native.retained_kind),
        generation=native.generation,
        cleanup_epoch=native.cleanup_epoch,
        authority_epoch=native.authority_epoch,
        deadline_ns=native.deadline_ns,
        lease_deadline_ns=native.lease_deadline_ns,
        completed_steps=native.completed_steps,
        process_pid=native.process_pid,
        process_start_ns=native.process_start_ns,
        process_uid=native.process_uid,
        process_pgid=native.process_pgid,
        process_sid=native.process_sid,
        process_identity_flags=native.process_identity_flags,
        executable_dev=native.executable_dev,
        executable_ino=native.executable_ino,
        boot_id=bytes(native.boot_id),
        executable_hash=bytes(native.executable_hash),
        batch_outcome=BatchOutcome(native.batch_outcome),
        descriptor_count=native.descriptor_count,
        normal_limit=native.normal_limit,
        hard_limit=native.hard_limit,
        workdir_parent_dev=native.workdir_parent_dev,
        workdir_parent_ino=native.workdir_parent_ino,
        workdir_dev=native.workdir_dev,
        workdir_ino=native.workdir_ino,
        workdir_bound=bool(native.workdir_bound),
        no_dependent_artifact=bool(native.no_dependent_artifact),
        candidate=_decode_text(native.candidate),
        executor=_decode_text(native.executor),
        prior_actor=_decode_text(native.prior_actor),
        authority=_decode_text(native.authority),
        exact_batch=_decode_text(native.exact_batch),
        inherited_batch=_decode_text(native.inherited_batch),
        reason=_decode_text(native.reason),
        workdir_name=_decode_text(native.workdir_name),
        descriptors=tuple(
            _descriptor_from_c(native.descriptors[index])
            for index in range(native.descriptor_count)
        ),
    )


def _default_library_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "build/lib/libclaude_proxy_lifecycle.dylib"
    )


class Lifecycle:
    """Call the sole authoritative transition implementation in native C."""

    @staticmethod
    def apply(state: State, record: Record) -> State:
        library = ctypes.CDLL(str(_default_library_path()), use_errno=True)
        apply = library.cpl_lifecycle_apply
        apply.argtypes = [
            ctypes.POINTER(_CState),
            ctypes.POINTER(_CRecord),
            ctypes.POINTER(_CState),
        ]
        apply.restype = ctypes.c_int
        current = _state_to_c(state)
        transition = _record_to_c(record)
        result = _CState()
        status = int(apply(current, transition, result))
        if status == 14:
            raise IllegalTransition("native lifecycle transition is illegal")
        if status != 0:
            raise NativeLifecycleError(status)
        return _state_from_c(result)
