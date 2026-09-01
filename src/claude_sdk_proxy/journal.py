"""Typed sole-owner wrapper for the authoritative native lifecycle journal."""

from __future__ import annotations

import ctypes
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Final, Self

from claude_sdk_proxy.lifecycle import (
    HASH_SIZE,
    ID_SIZE,
    BatchDescriptor,
    BatchDescriptorKind,
    ProcessIdentity,
    Record,
    State,
    _CBatchDescriptor,
    _CProcessIdentity,
    _CState,
    _descriptor_to_c,
    _identity_from_c,
    _identity_to_c,
    _record_to_c,
    _state_from_c,
)

_DEFAULT_OPERATION_NS: Final = 1_000_000_000
_AUTHORITY_TOKEN = object()
_RECEIPT_TOKEN = object()
_BATCH_TOKEN = object()


class JournalErrorCode(IntEnum):
    INVALID_ARGUMENT = 1
    SYSTEM = 2
    EXISTS = 3
    NOT_FOUND = 4
    SYMLINK = 5
    UNSAFE_FILE = 6
    IDENTITY_DRIFT = 7
    NONCE_MISMATCH = 8
    LOCK_TIMEOUT = 9
    NORMAL_LIMIT = 10
    HARD_LIMIT = 11
    RECORD_CLASS = 12
    PARENT_MISMATCH = 13
    ILLEGAL_TRANSITION = 14
    CORRUPT = 15
    CERTIFY_TIMEOUT = 16
    AUTHORITY = 17
    PROCESS_PRESENT = 18
    WORKDIR_PRESENT = 19
    NOT_ABSENT = 20
    FORK_INHERITED = 21
    CLOSED = 22
    IO_SHORT = 23
    UNSUPPORTED = 24
    PROCESS_IDENTITY = 25
    ACTION_LOCK = 26
    BATCH_TOKEN = 27
    PRECONDITION = 28
    REAP_REQUIRED = 29
    RECEIPT = 30
    CONTROL_FRAME = 31
    CONTROL_PHASE = 32
    CONTROL_PAYLOAD = 33


class JournalError(RuntimeError):
    """Stable native journal failure without caller-controlled content."""

    def __init__(self, code: JournalErrorCode) -> None:
        super().__init__(f"native journal operation failed with code {int(code)}")
        self.code = code


class RecordClass(IntEnum):
    NORMAL = 1
    RECOVERY = 2


class UnconfirmedReason(IntEnum):
    PROOF_UNAVAILABLE = 1
    NORMAL_REGION_EXHAUSTED = 2
    IDENTITY_UNAVAILABLE = 3


class _HandleState(IntEnum):
    OPEN = 1
    CLOSING = 2
    CLOSED = 3


class _ActionTokenState(IntEnum):
    RETAINED = 1
    CONSUMED = 2


@dataclass(frozen=True)
class JournalCreateReceipt:
    intent_parent_dirsynced: bool
    intent_hash: bytes = field(default=b"", compare=False, repr=False)
    _capability: bytes = field(default=b"", compare=False, repr=False)


@dataclass(frozen=True)
class JournalWorkdirReceipt:
    workdir_parent_dirsynced: bool
    bound_hash: bytes = field(default=b"", compare=False, repr=False)
    workdir_dev: int = field(default=0, compare=False, repr=False)
    workdir_ino: int = field(default=0, compare=False, repr=False)
    _capability: bytes = field(default=b"", compare=False, repr=False)


@dataclass(frozen=True)
class JournalDeleteReceipt:
    journal_unlink_parent_dirsynced: bool
    _capability: bytes = field(default=b"", compare=False, repr=False)

    @property
    def slot_releasable(self) -> bool:
        return self.journal_unlink_parent_dirsynced


@dataclass(frozen=True)
class CanonicalRecord:
    hash: bytes
    sequence: int
    record: State


@dataclass(frozen=True)
class CanonicalChain:
    head: CanonicalRecord
    physical_eof: int
    canonical_records: int
    invalid_bytes: int
    stale_records: int
    has_intent: bool
    unhealthy: bool


@dataclass(frozen=True)
class CertifiedHead:
    hash: bytes
    sequence: int
    physical_eof: int
    state: State
    has_intent: bool
    attempts: int


@dataclass(frozen=True)
class BootstrapHead:
    hash: bytes
    sequence: int
    physical_eof: int
    message_type: int
    payload: bytes = field(repr=False)
    attempts: int = 0


@dataclass(frozen=True)
class ReapProof:
    allocation_nonce: bytes
    generation: int
    authority_epoch: int
    identity: ProcessIdentity
    _certified_hash: bytes = field(default=b"", compare=False, repr=False)
    _capability: bytes = field(default=b"", compare=False, repr=False)

    @property
    def pid(self) -> int:
        return self.identity.pid


class _DeletionAuthority:
    __slots__ = ("_allocation_nonce", "_capability", "_certified_hash", "_kind")

    def __init__(
        self,
        token: object,
        kind: int,
        allocation_nonce: bytes,
        certified_hash: bytes,
        capability: bytes,
    ) -> None:
        if token is not _AUTHORITY_TOKEN:
            raise TypeError(
                "deletion authorities are constructed by native certification"
            )
        self._kind = kind
        self._allocation_nonce = allocation_nonce
        self._certified_hash = certified_hash
        self._capability = capability

    def _fabricate_for_test(
        self,
        *,
        kind: int | None = None,
        allocation_nonce: bytes | None = None,
        certified_hash: bytes | None = None,
        capability: bytes | None = None,
    ) -> _DeletionAuthority:
        selected_kind = self._kind if kind is None else kind
        authority_type = (
            CertifiedDone if selected_kind == 1 else UnreleasedPartialCreate
        )
        return authority_type(
            _AUTHORITY_TOKEN,
            selected_kind,
            self._allocation_nonce if allocation_nonce is None else allocation_nonce,
            self._certified_hash if certified_hash is None else certified_hash,
            self._capability if capability is None else capability,
        )


class CertifiedDone(_DeletionAuthority):
    """Opaque native authority for an exact, reaped, durable DONE head."""


class UnreleasedPartialCreate(_DeletionAuthority):
    """Opaque native authority for a create with no dependent artifacts."""


class _CCreateReceipt(ctypes.Structure):
    _fields_ = [
        ("state", ctypes.c_int),
        ("intent_hash", ctypes.c_uint8 * HASH_SIZE),
        ("capability", ctypes.c_uint8 * HASH_SIZE),
    ]


class _CWorkdirReceipt(ctypes.Structure):
    _fields_ = [
        ("state", ctypes.c_int),
        ("bound_hash", ctypes.c_uint8 * HASH_SIZE),
        ("capability", ctypes.c_uint8 * HASH_SIZE),
        ("workdir_dev", ctypes.c_uint64),
        ("workdir_ino", ctypes.c_uint64),
    ]


class _CDeleteAuthority(ctypes.Structure):
    _fields_ = [
        ("kind", ctypes.c_int),
        ("allocation_nonce", ctypes.c_uint8 * HASH_SIZE),
        ("certified_hash", ctypes.c_uint8 * HASH_SIZE),
        ("capability", ctypes.c_uint8 * HASH_SIZE),
    ]


class _CDeleteReceipt(ctypes.Structure):
    _fields_ = [
        ("state", ctypes.c_int),
        ("slot_releasable", ctypes.c_bool),
        ("reserved", ctypes.c_uint8 * 3),
        ("capability", ctypes.c_uint8 * HASH_SIZE),
    ]


class _CAppendResult(ctypes.Structure):
    _fields_ = [
        ("state", _CState),
        ("hash", ctypes.c_uint8 * HASH_SIZE),
        ("sequence", ctypes.c_uint64),
    ]


class _CActionToken(ctypes.Structure):
    _fields_ = [
        ("capability", ctypes.c_uint8 * HASH_SIZE),
        ("batch_nonce", ctypes.c_uint8 * ID_SIZE),
        ("generation", ctypes.c_uint64),
    ]


class _CReapProof(ctypes.Structure):
    _fields_ = [
        ("allocation_nonce", ctypes.c_uint8 * HASH_SIZE),
        ("certified_hash", ctypes.c_uint8 * HASH_SIZE),
        ("capability", ctypes.c_uint8 * HASH_SIZE),
        ("generation", ctypes.c_uint64),
        ("authority_epoch", ctypes.c_uint64),
        ("identity", _CProcessIdentity),
    ]


class _CChain(ctypes.Structure):
    _fields_ = [
        ("state", _CState),
        ("head_hash", ctypes.c_uint8 * HASH_SIZE),
        ("head_sequence", ctypes.c_uint64),
        ("physical_eof", ctypes.c_uint64),
        ("canonical_records", ctypes.c_uint64),
        ("invalid_bytes", ctypes.c_uint64),
        ("stale_records", ctypes.c_uint64),
        ("has_intent", ctypes.c_bool),
        ("unhealthy", ctypes.c_bool),
        ("reserved", ctypes.c_uint8 * 6),
    ]


class _CCertifiedHead(ctypes.Structure):
    _fields_ = [
        ("state", _CState),
        ("head_hash", ctypes.c_uint8 * HASH_SIZE),
        ("head_sequence", ctypes.c_uint64),
        ("physical_eof", ctypes.c_uint64),
        ("attempts", ctypes.c_uint32),
        ("has_intent", ctypes.c_bool),
        ("done_authority", ctypes.c_bool),
        ("partial_create_authority", ctypes.c_bool),
        ("reserved", ctypes.c_uint8),
    ]


class _CBootstrapHead(ctypes.Structure):
    _fields_ = [
        ("sequence", ctypes.c_uint64),
        ("physical_eof", ctypes.c_uint64),
        ("type", ctypes.c_uint16),
        ("reserved_type", ctypes.c_uint16),
        ("payload_length", ctypes.c_uint32),
        ("attempts", ctypes.c_uint32),
        ("present", ctypes.c_bool),
        ("reserved", ctypes.c_uint8 * 3),
        ("hash", ctypes.c_uint8 * HASH_SIZE),
        ("payload", ctypes.c_uint8 * 256),
    ]


_JOURNAL_ABI_SIZES: Final = {
    _CCreateReceipt: 68,
    _CWorkdirReceipt: 88,
    _CDeleteAuthority: 100,
    _CDeleteReceipt: 40,
    _CAppendResult: 1072,
    _CActionToken: 72,
    _CReapProof: 224,
    _CChain: 1112,
    _CCertifiedHead: 1088,
}
for _abi_type, _abi_size in _JOURNAL_ABI_SIZES.items():
    if ctypes.sizeof(_abi_type) != _abi_size:
        raise RuntimeError(f"ctypes ABI size mismatch for {_abi_type.__name__}")


def _default_library_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "build/lib/libclaude_proxy_lifecycle.dylib"
    )


def _set_bytes(target: ctypes.Array[ctypes.c_uint8], value: bytes) -> None:
    if len(value) != len(target):
        raise JournalError(JournalErrorCode.INVALID_ARGUMENT)
    ctypes.memmove(target, value, len(value))


def _raise_status(status: int) -> None:
    if status == 0:
        return
    try:
        code = JournalErrorCode(status)
    except ValueError:
        code = JournalErrorCode.SYSTEM
    raise JournalError(code)


def _deadline(value: int | None) -> int:
    if value is not None:
        if isinstance(value, bool) or value <= 0:
            raise ValueError("deadline_ns must be a positive absolute deadline")
        return value
    return time.monotonic_ns() + _DEFAULT_OPERATION_NS


def _component_bytes(name: str) -> bytes:
    if not isinstance(name, str) or "\0" in name:
        raise JournalError(JournalErrorCode.INVALID_ARGUMENT)
    return name.encode("utf-8")


def _derived_workdir_name(journal_name: str) -> str:
    suffix = ".journal"
    if not journal_name.endswith(suffix) or len(journal_name) == len(suffix):
        raise JournalError(JournalErrorCode.INVALID_ARGUMENT)
    return journal_name[: -len(suffix)] + ".workdir"


def _fixed_id(value: str) -> ctypes.Array[ctypes.c_uint8]:
    encoded = _component_bytes(value)
    if len(encoded) >= ID_SIZE:
        raise JournalError(JournalErrorCode.INVALID_ARGUMENT)
    result = (ctypes.c_uint8 * ID_SIZE)()
    ctypes.memmove(result, encoded, len(encoded))
    return result


def _checked_nonce(nonce: bytes) -> ctypes.Array[ctypes.c_uint8]:
    if not isinstance(nonce, bytes) or len(nonce) != HASH_SIZE:
        raise JournalError(JournalErrorCode.INVALID_ARGUMENT)
    return (ctypes.c_uint8 * HASH_SIZE).from_buffer_copy(nonce)


def _canonical(native: _CAppendResult) -> CanonicalRecord:
    return CanonicalRecord(
        bytes(native.hash), native.sequence, _state_from_c(native.state)
    )


def _load_library(path: Path) -> ctypes.CDLL:
    library = ctypes.CDLL(str(path), use_errno=True)
    handle_pointer = ctypes.POINTER(ctypes.c_void_p)
    byte_pointer = ctypes.POINTER(ctypes.c_uint8)
    library.cpl_journal_create_at.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        byte_pointer,
        ctypes.c_uint64,
        ctypes.c_uint64,
        handle_pointer,
        ctypes.POINTER(_CCreateReceipt),
    ]
    library.cpl_journal_create_at.restype = ctypes.c_int
    library.cpl_journal_open_at.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        byte_pointer,
        ctypes.c_uint64,
        ctypes.c_uint64,
        handle_pointer,
    ]
    library.cpl_journal_open_at.restype = ctypes.c_int
    library.cpl_journal_append.argtypes = [
        ctypes.c_void_p,
        byte_pointer,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint64,
        ctypes.POINTER(_CAppendResult),
    ]
    library.cpl_journal_append.restype = ctypes.c_int
    library.cpl_journal_scan.argtypes = [ctypes.c_void_p, ctypes.POINTER(_CChain)]
    library.cpl_journal_scan.restype = ctypes.c_int
    library.cpl_journal_certify.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint64,
        ctypes.POINTER(_CCertifiedHead),
    ]
    library.cpl_journal_certify.restype = ctypes.c_int
    library.cpl_journal_bootstrap_append.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint16,
        byte_pointer,
        ctypes.c_uint32,
        ctypes.c_uint64,
        ctypes.POINTER(_CBootstrapHead),
    ]
    library.cpl_journal_bootstrap_append.restype = ctypes.c_int
    library.cpl_journal_bootstrap_certify.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint16,
        byte_pointer,
        ctypes.c_uint32,
        ctypes.c_uint64,
        ctypes.POINTER(_CBootstrapHead),
    ]
    library.cpl_journal_bootstrap_certify.restype = ctypes.c_int
    library.cpl_journal_create_workdir.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.POINTER(_CCreateReceipt),
        ctypes.c_uint64,
        ctypes.POINTER(_CWorkdirReceipt),
    ]
    library.cpl_journal_create_workdir.restype = ctypes.c_int
    library.cpl_journal_certify_no_dependent_artifact.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.POINTER(_CDeleteAuthority),
    ]
    library.cpl_journal_certify_no_dependent_artifact.restype = ctypes.c_int
    library.cpl_journal_make_delete_authority.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint64,
        ctypes.POINTER(_CDeleteAuthority),
    ]
    library.cpl_journal_make_delete_authority.restype = ctypes.c_int
    library.cpl_process_observe.argtypes = [
        ctypes.c_int64,
        ctypes.POINTER(_CProcessIdentity),
    ]
    library.cpl_process_observe.restype = ctypes.c_int
    library.cpl_journal_activate_executor.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint64,
        byte_pointer,
        ctypes.c_int64,
        ctypes.c_uint64,
        ctypes.c_uint64,
        ctypes.POINTER(_CAppendResult),
    ]
    library.cpl_journal_activate_executor.restype = ctypes.c_int
    library.cpl_journal_admit_batch.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint64,
        byte_pointer,
        byte_pointer,
        ctypes.POINTER(_CBatchDescriptor),
        ctypes.c_uint32,
        ctypes.c_uint64,
        ctypes.POINTER(_CActionToken),
        ctypes.POINTER(_CAppendResult),
    ]
    library.cpl_journal_admit_batch.restype = ctypes.c_int
    library.cpl_journal_execute_batch.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_CActionToken),
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.POINTER(ctypes.c_uint64),
    ]
    library.cpl_journal_execute_batch.restype = ctypes.c_int
    library.cpl_journal_complete_batch.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_CActionToken),
        ctypes.c_uint64,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(_CAppendResult),
    ]
    library.cpl_journal_complete_batch.restype = ctypes.c_int
    library.cpl_journal_abandon_batch.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_CActionToken),
    ]
    library.cpl_journal_abandon_batch.restype = ctypes.c_int
    library.cpl_journal_finish_done.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint64,
        byte_pointer,
        ctypes.c_uint64,
        ctypes.POINTER(_CAppendResult),
    ]
    library.cpl_journal_finish_done.restype = ctypes.c_int
    library.cpl_journal_retire_executor.argtypes = [
        ctypes.c_void_p,
        byte_pointer,
        ctypes.c_uint64,
        ctypes.c_uint64,
        ctypes.c_uint64,
        ctypes.POINTER(_CAppendResult),
    ]
    library.cpl_journal_retire_executor.restype = ctypes.c_int
    library.cpl_journal_replace_retirement_authority.argtypes = [
        ctypes.c_void_p,
        byte_pointer,
        ctypes.c_uint64,
        ctypes.c_uint64,
        ctypes.c_uint64,
        ctypes.POINTER(_CAppendResult),
    ]
    library.cpl_journal_replace_retirement_authority.restype = ctypes.c_int
    library.cpl_journal_mark_unconfirmed.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint64,
        ctypes.POINTER(_CAppendResult),
    ]
    library.cpl_journal_mark_unconfirmed.restype = ctypes.c_int
    library.cpl_journal_confirm_executor_reaped.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint64,
        ctypes.POINTER(_CReapProof),
    ]
    library.cpl_journal_confirm_executor_reaped.restype = ctypes.c_int
    library.cpl_journal_recover_executor_reap_proof.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint64,
        ctypes.POINTER(_CReapProof),
    ]
    library.cpl_journal_recover_executor_reap_proof.restype = ctypes.c_int
    library.cpl_journal_reconcile_interrupted_batch.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_CReapProof),
        ctypes.c_uint64,
        ctypes.POINTER(_CAppendResult),
    ]
    library.cpl_journal_reconcile_interrupted_batch.restype = ctypes.c_int
    library.cpl_journal_prepare_successor.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_CReapProof),
        ctypes.c_uint64,
        byte_pointer,
        ctypes.c_uint64,
        ctypes.c_uint64,
        ctypes.POINTER(_CAppendResult),
    ]
    library.cpl_journal_prepare_successor.restype = ctypes.c_int
    library.cpl_journal_delete_at.argtypes = [
        handle_pointer,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.POINTER(_CDeleteAuthority),
        ctypes.c_uint64,
        ctypes.POINTER(_CDeleteReceipt),
    ]
    library.cpl_journal_delete_at.restype = ctypes.c_int
    library.cpl_journal_reconcile_absent_after_crash.argtypes = [
        handle_pointer,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.POINTER(_CDeleteAuthority),
        ctypes.c_uint64,
        ctypes.POINTER(_CDeleteReceipt),
    ]
    library.cpl_journal_reconcile_absent_after_crash.restype = ctypes.c_int
    library.cpl_journal_close.argtypes = [ctypes.c_void_p]
    library.cpl_journal_close.restype = None
    return library


class AdmittedBatch:
    """A native action-lock capability; only its admitting thread may use it."""

    __slots__ = ("_journal", "_owner_thread_id", "_token", "_valid")

    def __init__(self, token: object, journal: Journal, native: _CActionToken) -> None:
        if token is not _BATCH_TOKEN:
            raise TypeError("batch capabilities are created by native admission")
        self._journal = journal
        self._token = native
        self._owner_thread_id = threading.get_ident()
        self._valid = True

    def execute(self, deadline_ns: int | None = None) -> int:
        completed = ctypes.c_uint64()
        _raise_status(
            self._journal._batch_native_call(
                self,
                self._journal._library.cpl_journal_execute_batch,
                ctypes.byref(self._token),
                self._journal._workdir_parent_dirfd,
                _deadline(deadline_ns),
                ctypes.byref(completed),
            )
        )
        return completed.value

    def complete(self, deadline_ns: int | None = None) -> CanonicalRecord:
        output = _CAppendResult()
        token_state = ctypes.c_uint32()
        status = self._journal._batch_native_call(
            self,
            self._journal._library.cpl_journal_complete_batch,
            ctypes.byref(self._token),
            _deadline(deadline_ns),
            ctypes.byref(token_state),
            ctypes.byref(output),
        )
        if token_state.value == _ActionTokenState.CONSUMED:
            self._journal._release_batch_lease(self)
        elif token_state.value != _ActionTokenState.RETAINED:
            raise JournalError(JournalErrorCode.SYSTEM)
        if status == 0 and token_state.value != _ActionTokenState.CONSUMED:
            raise JournalError(JournalErrorCode.SYSTEM)
        _raise_status(status)
        return _canonical(output)

    def abandon(self) -> None:
        status = self._journal._batch_native_call(
            self,
            self._journal._library.cpl_journal_abandon_batch,
            ctypes.byref(self._token),
        )
        if status == 0:
            self._journal._release_batch_lease(self)
        _raise_status(status)


class Journal:
    """One owning native journal handle and its canonical lock domains."""

    def __init__(
        self,
        library: ctypes.CDLL,
        handle: ctypes.c_void_p,
        parent_dirfd: int,
        journal_name: str,
        nonce: bytes,
        normal_limit: int,
        hard_limit: int,
        workdir_parent_dirfd: int,
        workdir_name: str,
    ) -> None:
        self._library = library
        self._handle = handle
        self._parent_dirfd = parent_dirfd
        self._journal_name = journal_name
        self._nonce = nonce
        self._normal_limit = normal_limit
        self._hard_limit = hard_limit
        self._workdir_parent_dirfd = workdir_parent_dirfd
        self._workdir_name = workdir_name
        self._operation_condition = threading.Condition()
        self._active_operations = 0
        self._handle_state = _HandleState.OPEN
        self._closing_thread_id: int | None = None
        self._outstanding_batch_owner: int | None = None

    @classmethod
    def create_at(
        cls,
        parent_dirfd: int,
        journal_name: str,
        nonce: bytes,
        normal_limit: int,
        hard_limit: int,
        *,
        workdir_parent_dirfd: int | None = None,
        workdir_name: str | None = None,
    ) -> tuple[Self, JournalCreateReceipt]:
        return cls._create_at_with_library(
            parent_dirfd,
            journal_name,
            nonce,
            normal_limit,
            hard_limit,
            workdir_parent_dirfd=workdir_parent_dirfd,
            workdir_name=workdir_name,
            library_path=_default_library_path(),
        )

    @classmethod
    def _create_at_for_test(
        cls,
        parent_dirfd: int,
        journal_name: str,
        nonce: bytes,
        normal_limit: int,
        hard_limit: int,
        *,
        workdir_parent_dirfd: int | None = None,
        workdir_name: str | None = None,
        library_path: Path,
    ) -> tuple[Self, JournalCreateReceipt]:
        return cls._create_at_with_library(
            parent_dirfd,
            journal_name,
            nonce,
            normal_limit,
            hard_limit,
            workdir_parent_dirfd=workdir_parent_dirfd,
            workdir_name=workdir_name,
            library_path=library_path,
        )

    @classmethod
    def _create_at_with_library(
        cls,
        parent_dirfd: int,
        journal_name: str,
        nonce: bytes,
        normal_limit: int,
        hard_limit: int,
        *,
        workdir_parent_dirfd: int | None,
        workdir_name: str | None,
        library_path: Path,
    ) -> tuple[Self, JournalCreateReceipt]:
        selected_parent = (
            parent_dirfd if workdir_parent_dirfd is None else workdir_parent_dirfd
        )
        selected_name = (
            _derived_workdir_name(journal_name)
            if workdir_name is None
            else workdir_name
        )
        library = _load_library(library_path)
        handle = ctypes.c_void_p()
        receipt = _CCreateReceipt()
        status = int(
            library.cpl_journal_create_at(
                parent_dirfd,
                _component_bytes(journal_name),
                selected_parent,
                _component_bytes(selected_name),
                _checked_nonce(nonce),
                normal_limit,
                hard_limit,
                ctypes.byref(handle),
                ctypes.byref(receipt),
            )
        )
        _raise_status(status)
        if not handle.value or receipt.state != 1:
            if handle.value:
                library.cpl_journal_close(handle)
            raise JournalError(JournalErrorCode.SYSTEM)
        journal = cls(
            library,
            handle,
            parent_dirfd,
            journal_name,
            nonce,
            normal_limit,
            hard_limit,
            selected_parent,
            selected_name,
        )
        return journal, JournalCreateReceipt(
            True, bytes(receipt.intent_hash), bytes(receipt.capability)
        )

    @classmethod
    def open_at(
        cls,
        parent_dirfd: int,
        journal_name: str,
        nonce: bytes,
        normal_limit: int,
        hard_limit: int,
        *,
        workdir_parent_dirfd: int | None = None,
        workdir_name: str | None = None,
    ) -> Self:
        return cls._open_at_with_library(
            parent_dirfd,
            journal_name,
            nonce,
            normal_limit,
            hard_limit,
            workdir_parent_dirfd=workdir_parent_dirfd,
            workdir_name=workdir_name,
            library_path=_default_library_path(),
        )

    @classmethod
    def _open_at_for_test(
        cls,
        parent_dirfd: int,
        journal_name: str,
        nonce: bytes,
        normal_limit: int,
        hard_limit: int,
        *,
        workdir_parent_dirfd: int | None = None,
        workdir_name: str | None = None,
        library_path: Path,
    ) -> Self:
        return cls._open_at_with_library(
            parent_dirfd,
            journal_name,
            nonce,
            normal_limit,
            hard_limit,
            workdir_parent_dirfd=workdir_parent_dirfd,
            workdir_name=workdir_name,
            library_path=library_path,
        )

    @classmethod
    def _open_at_with_library(
        cls,
        parent_dirfd: int,
        journal_name: str,
        nonce: bytes,
        normal_limit: int,
        hard_limit: int,
        *,
        workdir_parent_dirfd: int | None,
        workdir_name: str | None,
        library_path: Path,
    ) -> Self:
        selected_parent = (
            parent_dirfd if workdir_parent_dirfd is None else workdir_parent_dirfd
        )
        selected_name = (
            _derived_workdir_name(journal_name)
            if workdir_name is None
            else workdir_name
        )
        library = _load_library(library_path)
        handle = ctypes.c_void_p()
        _raise_status(
            int(
                library.cpl_journal_open_at(
                    parent_dirfd,
                    _component_bytes(journal_name),
                    selected_parent,
                    _component_bytes(selected_name),
                    _checked_nonce(nonce),
                    normal_limit,
                    hard_limit,
                    ctypes.byref(handle),
                )
            )
        )
        if not handle.value:
            raise JournalError(JournalErrorCode.SYSTEM)
        return cls(
            library,
            handle,
            parent_dirfd,
            journal_name,
            nonce,
            normal_limit,
            hard_limit,
            selected_parent,
            selected_name,
        )

    @property
    def closed(self) -> bool:
        with self._operation_condition:
            return self._handle_state is _HandleState.CLOSED

    @property
    def unhealthy(self) -> bool:
        return self.scan().unhealthy

    def __copy__(self) -> Self:
        raise TypeError("Journal is a sole-owner native handle")

    def __deepcopy__(self, memo: object) -> Self:
        del memo
        raise TypeError("Journal is a sole-owner native handle")

    def __enter__(self) -> Self:
        self._require_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close()

    def __del__(self) -> None:
        try:
            self._finalize_nonblocking()
        except Exception:
            pass

    def _finalize_nonblocking(self) -> None:
        condition = getattr(self, "_operation_condition", None)
        if condition is None or not condition.acquire(blocking=False):
            return
        try:
            if (
                self._handle_state is not _HandleState.OPEN
                or self._active_operations != 0
                or self._outstanding_batch_owner is not None
            ):
                return
            self._handle_state = _HandleState.CLOSING
            self._closing_thread_id = threading.get_ident()
            handle = ctypes.c_void_p(self._handle.value)
            try:
                self._library.cpl_journal_close(handle)
            finally:
                self._handle = ctypes.c_void_p()
                self._handle_state = _HandleState.CLOSED
                self._closing_thread_id = None
                self._operation_condition.notify_all()
        finally:
            condition.release()

    def _require_open(self) -> None:
        with self._operation_condition:
            if self._handle_state is not _HandleState.OPEN:
                raise JournalError(JournalErrorCode.CLOSED)

    @contextmanager
    def _operation(self) -> Iterator[ctypes.c_void_p]:
        handle = self._acquire_operation()
        try:
            yield handle
        finally:
            self._release_operation()

    def _acquire_operation(self) -> ctypes.c_void_p:
        with self._operation_condition:
            if self._handle_state is not _HandleState.OPEN:
                raise JournalError(JournalErrorCode.CLOSED)
            self._active_operations += 1
            return ctypes.c_void_p(self._handle.value)

    def _acquire_batch_lease(self, deadline_ns: int) -> ctypes.c_void_p:
        current_thread = threading.get_ident()
        with self._operation_condition:
            while True:
                if self._handle_state is not _HandleState.OPEN:
                    raise JournalError(JournalErrorCode.CLOSED)
                if self._outstanding_batch_owner is None:
                    self._outstanding_batch_owner = current_thread
                    self._active_operations += 1
                    return ctypes.c_void_p(self._handle.value)
                if self._outstanding_batch_owner == current_thread:
                    raise JournalError(JournalErrorCode.BATCH_TOKEN)
                remaining_ns = deadline_ns - time.monotonic_ns()
                if remaining_ns <= 0:
                    raise JournalError(JournalErrorCode.LOCK_TIMEOUT)
                self._operation_condition.wait(remaining_ns / 1_000_000_000)

    def _release_operation(self) -> None:
        with self._operation_condition:
            self._active_operations -= 1
            if self._active_operations == 0:
                self._operation_condition.notify_all()

    def _batch_native_call(
        self,
        batch: AdmittedBatch,
        function: Callable[..., int],
        *args: object,
    ) -> int:
        with self._operation_condition:
            current_thread = threading.get_ident()
            if (
                not batch._valid
                or batch._owner_thread_id != current_thread
                or self._outstanding_batch_owner != current_thread
                or self._handle_state is _HandleState.CLOSED
            ):
                raise JournalError(JournalErrorCode.BATCH_TOKEN)
            handle = ctypes.c_void_p(self._handle.value)
        return int(function(handle, *args))

    def _release_batch_lease(self, batch: AdmittedBatch) -> None:
        with self._operation_condition:
            if (
                not batch._valid
                or self._outstanding_batch_owner != threading.get_ident()
            ):
                raise JournalError(JournalErrorCode.BATCH_TOKEN)
            batch._valid = False
            self._outstanding_batch_owner = None
            self._active_operations -= 1
            self._operation_condition.notify_all()

    def _rollback_batch_lease(self) -> None:
        with self._operation_condition:
            if self._outstanding_batch_owner != threading.get_ident():
                raise JournalError(JournalErrorCode.BATCH_TOKEN)
            self._outstanding_batch_owner = None
            self._active_operations -= 1
            self._operation_condition.notify_all()

    def _native_call(self, function: Callable[..., int], *args: object) -> int:
        with self._operation() as handle:
            return int(function(handle, *args))

    def _exclusive_pointer_call(
        self, function: Callable[..., int], *args: object
    ) -> int:
        current_thread = threading.get_ident()
        with self._operation_condition:
            if self._handle_state is not _HandleState.OPEN:
                raise JournalError(JournalErrorCode.CLOSED)
            if self._outstanding_batch_owner == current_thread:
                raise JournalError(JournalErrorCode.BATCH_TOKEN)
            self._handle_state = _HandleState.CLOSING
            self._closing_thread_id = current_thread
            while self._active_operations:
                self._operation_condition.wait()
        try:
            return int(function(ctypes.byref(self._handle), *args))
        finally:
            with self._operation_condition:
                self._handle_state = (
                    _HandleState.OPEN
                    if self._handle.value
                    else _HandleState.CLOSED
                )
                self._closing_thread_id = None
                self._operation_condition.notify_all()

    def close(self) -> None:
        current_thread = threading.get_ident()
        with self._operation_condition:
            while self._handle_state is _HandleState.CLOSING:
                if self._closing_thread_id == current_thread:
                    return
                if self._outstanding_batch_owner == current_thread:
                    raise JournalError(JournalErrorCode.BATCH_TOKEN)
                self._operation_condition.wait()
            if self._handle_state is _HandleState.CLOSED:
                return
            if self._outstanding_batch_owner == current_thread:
                raise JournalError(JournalErrorCode.BATCH_TOKEN)
            self._handle_state = _HandleState.CLOSING
            self._closing_thread_id = current_thread
            while self._active_operations:
                self._operation_condition.wait()
            handle = ctypes.c_void_p(self._handle.value)
        try:
            self._library.cpl_journal_close(handle)
        finally:
            with self._operation_condition:
                self._handle = ctypes.c_void_p()
                self._handle_state = _HandleState.CLOSED
                self._closing_thread_id = None
                self._operation_condition.notify_all()

    def scan(self) -> CanonicalChain:
        chain = _CChain()
        _raise_status(
            self._native_call(self._library.cpl_journal_scan, ctypes.byref(chain))
        )
        state = _state_from_c(chain.state)
        return CanonicalChain(
            CanonicalRecord(bytes(chain.head_hash), chain.head_sequence, state),
            chain.physical_eof,
            chain.canonical_records,
            chain.invalid_bytes,
            chain.stale_records,
            chain.has_intent,
            chain.unhealthy,
        )

    def append(
        self,
        record: Record,
        record_class: RecordClass,
        *,
        deadline_ns: int | None = None,
    ) -> CanonicalRecord:
        native_record = _record_to_c(record)
        payload = ctypes.cast(
            ctypes.byref(native_record), ctypes.POINTER(ctypes.c_uint8)
        )
        output = _CAppendResult()
        _raise_status(
            self._native_call(
                self._library.cpl_journal_append,
                payload,
                ctypes.sizeof(native_record),
                int(record_class),
                _deadline(deadline_ns),
                ctypes.byref(output),
            )
        )
        return _canonical(output)

    def mark_unconfirmed(
        self,
        reason: UnconfirmedReason,
        deadline_ns: int | None = None,
    ) -> CanonicalRecord:
        if not isinstance(reason, UnconfirmedReason):
            raise JournalError(JournalErrorCode.INVALID_ARGUMENT)
        output = _CAppendResult()
        _raise_status(
            self._native_call(
                self._library.cpl_journal_mark_unconfirmed,
                int(reason),
                _deadline(deadline_ns),
                ctypes.byref(output),
            )
        )
        return _canonical(output)

    def certify_head(self, deadline_ns: int | None = None) -> CertifiedHead:
        native = self._certified_native(deadline_ns)
        return CertifiedHead(
            bytes(native.head_hash),
            native.head_sequence,
            native.physical_eof,
            _state_from_c(native.state),
            native.has_intent,
            native.attempts,
        )

    def certify_bootstrap(
        self,
        message_type: int,
        payload: bytes,
        deadline_ns: int | None = None,
    ) -> BootstrapHead:
        if (
            isinstance(message_type, bool)
            or not 1 <= message_type <= 11
            or not isinstance(payload, bytes)
            or len(payload) > 256
        ):
            raise JournalError(JournalErrorCode.INVALID_ARGUMENT)
        storage = (ctypes.c_uint8 * max(1, len(payload)))()
        if payload:
            ctypes.memmove(storage, payload, len(payload))
        native = _CBootstrapHead()
        _raise_status(
            self._native_call(
                self._library.cpl_journal_bootstrap_certify,
                message_type,
                storage,
                len(payload),
                _deadline(deadline_ns),
                ctypes.byref(native),
            )
        )
        if not native.present:
            raise JournalError(JournalErrorCode.AUTHORITY)
        return BootstrapHead(
            bytes(native.hash),
            native.sequence,
            native.physical_eof,
            native.type,
            bytes(native.payload[: native.payload_length]),
            native.attempts,
        )

    def append_bootstrap(
        self,
        message_type: int,
        payload: bytes,
        deadline_ns: int | None = None,
    ) -> BootstrapHead:
        """Append one authenticated bootstrap event to the canonical journal."""
        if (
            isinstance(message_type, bool)
            or not 1 <= message_type <= 11
            or not isinstance(payload, bytes)
            or len(payload) > 256
        ):
            raise JournalError(JournalErrorCode.INVALID_ARGUMENT)
        storage = (ctypes.c_uint8 * max(1, len(payload)))()
        if payload:
            ctypes.memmove(storage, payload, len(payload))
        native = _CBootstrapHead()
        _raise_status(
            self._native_call(
                self._library.cpl_journal_bootstrap_append,
                message_type,
                storage,
                len(payload),
                _deadline(deadline_ns),
                ctypes.byref(native),
            )
        )
        return BootstrapHead(
            bytes(native.hash),
            native.sequence,
            native.physical_eof,
            native.type,
            bytes(native.payload[: native.payload_length]),
            native.attempts,
        )

    def _certified_native(self, deadline_ns: int | None = None) -> _CCertifiedHead:
        native = _CCertifiedHead()
        _raise_status(
            self._native_call(
                self._library.cpl_journal_certify,
                _deadline(deadline_ns),
                ctypes.byref(native),
            )
        )
        return native

    @staticmethod
    def _create_receipt_to_c(receipt: JournalCreateReceipt) -> _CCreateReceipt:
        native = _CCreateReceipt()
        native.state = 1 if receipt.intent_parent_dirsynced else 0
        _set_bytes(native.intent_hash, receipt.intent_hash)
        _set_bytes(native.capability, receipt._capability)
        return native

    def create_workdir(
        self,
        receipt: JournalCreateReceipt,
        deadline_ns: int | None = None,
    ) -> JournalWorkdirReceipt:
        if not isinstance(receipt, JournalCreateReceipt):
            raise TypeError("a native create receipt is required")
        create_receipt = self._create_receipt_to_c(receipt)
        native = _CWorkdirReceipt()
        _raise_status(
            self._native_call(
                self._library.cpl_journal_create_workdir,
                self._workdir_parent_dirfd,
                ctypes.byref(create_receipt),
                _deadline(deadline_ns),
                ctypes.byref(native),
            )
        )
        if native.state != 3:
            raise JournalError(JournalErrorCode.SYSTEM)
        return JournalWorkdirReceipt(
            True,
            bytes(native.bound_hash),
            native.workdir_dev,
            native.workdir_ino,
            bytes(native.capability),
        )

    @staticmethod
    def _authority_from_c(native: _CDeleteAuthority) -> _DeletionAuthority:
        authority_type = CertifiedDone if native.kind == 1 else UnreleasedPartialCreate
        return authority_type(
            _AUTHORITY_TOKEN,
            native.kind,
            bytes(native.allocation_nonce),
            bytes(native.certified_hash),
            bytes(native.capability),
        )

    def certify_done(self, deadline_ns: int | None = None) -> CertifiedDone:
        native = _CDeleteAuthority()
        _raise_status(
            self._native_call(
                self._library.cpl_journal_make_delete_authority,
                1,
                _deadline(deadline_ns),
                ctypes.byref(native),
            )
        )
        authority = self._authority_from_c(native)
        assert isinstance(authority, CertifiedDone)
        return authority

    def certify_no_dependent_artifacts(
        self,
        deadline_ns: int | None = None,
    ) -> UnreleasedPartialCreate:
        native = _CDeleteAuthority()
        _raise_status(
            self._native_call(
                self._library.cpl_journal_certify_no_dependent_artifact,
                self._workdir_parent_dirfd,
                _deadline(deadline_ns),
                ctypes.byref(native),
            )
        )
        authority = self._authority_from_c(native)
        assert isinstance(authority, UnreleasedPartialCreate)
        return authority

    def certify_unreleased_partial_create(
        self,
        deadline_ns: int | None = None,
    ) -> UnreleasedPartialCreate:
        return self.certify_no_dependent_artifacts(deadline_ns)

    @staticmethod
    def _authority_to_c(authority: _DeletionAuthority) -> _CDeleteAuthority:
        if not isinstance(authority, _DeletionAuthority):
            raise TypeError("a native typed deletion authority is required")
        native = _CDeleteAuthority()
        native.kind = authority._kind
        _set_bytes(native.allocation_nonce, authority._allocation_nonce)
        _set_bytes(native.certified_hash, authority._certified_hash)
        _set_bytes(native.capability, authority._capability)
        return native

    def _delete_common(
        self,
        entry_point: Callable[..., int],
        authority: CertifiedDone | UnreleasedPartialCreate,
        deadline_ns: int | None,
    ) -> JournalDeleteReceipt:
        native_authority = self._authority_to_c(authority)
        native = _CDeleteReceipt()
        _raise_status(
            self._exclusive_pointer_call(
                entry_point,
                self._parent_dirfd,
                _component_bytes(self._journal_name),
                self._workdir_parent_dirfd,
                _component_bytes(self._workdir_name),
                ctypes.byref(native_authority),
                _deadline(deadline_ns),
                ctypes.byref(native),
            )
        )
        if self._handle.value or native.state != 2 or not native.slot_releasable:
            raise JournalError(JournalErrorCode.SYSTEM)
        return JournalDeleteReceipt(True, bytes(native.capability))

    def delete_at(
        self,
        authority: CertifiedDone | UnreleasedPartialCreate,
        deadline_ns: int | None = None,
    ) -> JournalDeleteReceipt:
        return self._delete_common(
            self._library.cpl_journal_delete_at, authority, deadline_ns
        )

    def reconcile_absent_after_crash(
        self,
        authority: CertifiedDone | UnreleasedPartialCreate,
        deadline_ns: int | None = None,
    ) -> JournalDeleteReceipt:
        return self._delete_common(
            self._library.cpl_journal_reconcile_absent_after_crash,
            authority,
            deadline_ns,
        )

    def observe_process(self, pid: int) -> ProcessIdentity:
        native = _CProcessIdentity()
        _raise_status(int(self._library.cpl_process_observe(pid, ctypes.byref(native))))
        return _identity_from_c(native)

    @staticmethod
    def process_absent_descriptor(identity: ProcessIdentity) -> BatchDescriptor:
        return BatchDescriptor(BatchDescriptorKind.PROCESS_ABSENT, 0, identity)

    @staticmethod
    def reap_process_descriptor(identity: ProcessIdentity) -> BatchDescriptor:
        return BatchDescriptor(BatchDescriptorKind.REAP_PROCESS, 1, identity)

    @staticmethod
    def remove_bound_workdir_descriptor() -> BatchDescriptor:
        return BatchDescriptor(BatchDescriptorKind.REMOVE_WORKDIR, 3)

    @staticmethod
    def terminal_checks_descriptor() -> BatchDescriptor:
        return BatchDescriptor(BatchDescriptorKind.TERMINAL_CHECKS, 7)

    def activate_executor(
        self,
        generation: int,
        executor: str,
        pid: int,
        *,
        lease_deadline_ns: int,
        deadline_ns: int | None = None,
    ) -> CanonicalRecord:
        output = _CAppendResult()
        _raise_status(
            self._native_call(
                self._library.cpl_journal_activate_executor,
                generation,
                _fixed_id(executor),
                pid,
                lease_deadline_ns,
                _deadline(deadline_ns),
                ctypes.byref(output),
            )
        )
        return _canonical(output)

    def admit_batch(
        self,
        generation: int,
        executor: str,
        batch_nonce: str,
        descriptors: Sequence[BatchDescriptor],
        *,
        deadline_ns: int | None = None,
    ) -> AdmittedBatch:
        if not descriptors:
            raise JournalError(JournalErrorCode.INVALID_ARGUMENT)
        array_type = _CBatchDescriptor * len(descriptors)
        descriptor_array = array_type(*(_descriptor_to_c(item) for item in descriptors))
        executor_id = _fixed_id(executor)
        batch_id = _fixed_id(batch_nonce)
        selected_deadline = _deadline(deadline_ns)
        token = _CActionToken()
        output = _CAppendResult()
        handle = self._acquire_batch_lease(selected_deadline)
        try:
            _raise_status(
                int(
                    self._library.cpl_journal_admit_batch(
                        handle,
                        generation,
                        executor_id,
                        batch_id,
                        descriptor_array,
                        len(descriptors),
                        selected_deadline,
                        ctypes.byref(token),
                        ctypes.byref(output),
                    )
                )
            )
            batch = AdmittedBatch(_BATCH_TOKEN, self, token)
        except BaseException:
            if any(token.capability):
                abandon_status = int(
                    self._library.cpl_journal_abandon_batch(
                        handle, ctypes.byref(token)
                    )
                )
                if abandon_status != 0:
                    _raise_status(abandon_status)
            self._rollback_batch_lease()
            raise
        return batch

    def finish_done(
        self,
        generation: int,
        executor: str,
        deadline_ns: int | None = None,
    ) -> CanonicalRecord:
        output = _CAppendResult()
        _raise_status(
            self._native_call(
                self._library.cpl_journal_finish_done,
                generation,
                _fixed_id(executor),
                _deadline(deadline_ns),
                ctypes.byref(output),
            )
        )
        return _canonical(output)

    def retire_executor(
        self,
        *,
        authority: str,
        authority_epoch: int,
        authority_deadline_ns: int,
        deadline_ns: int | None = None,
    ) -> CanonicalRecord:
        output = _CAppendResult()
        _raise_status(
            self._native_call(
                self._library.cpl_journal_retire_executor,
                _fixed_id(authority),
                authority_epoch,
                authority_deadline_ns,
                _deadline(deadline_ns),
                ctypes.byref(output),
            )
        )
        return _canonical(output)

    def replace_retirement_authority(
        self,
        *,
        authority: str,
        authority_epoch: int,
        authority_deadline_ns: int,
        deadline_ns: int | None = None,
    ) -> CanonicalRecord:
        output = _CAppendResult()
        _raise_status(
            self._native_call(
                self._library.cpl_journal_replace_retirement_authority,
                _fixed_id(authority),
                authority_epoch,
                authority_deadline_ns,
                _deadline(deadline_ns),
                ctypes.byref(output),
            )
        )
        return _canonical(output)

    def confirm_executor_reaped(
        self,
        deadline_ns: int | None = None,
    ) -> ReapProof:
        native = _CReapProof()
        _raise_status(
            self._native_call(
                self._library.cpl_journal_confirm_executor_reaped,
                _deadline(deadline_ns),
                ctypes.byref(native),
            )
        )
        return ReapProof(
            bytes(native.allocation_nonce),
            native.generation,
            native.authority_epoch,
            _identity_from_c(native.identity),
            bytes(native.certified_hash),
            bytes(native.capability),
        )

    def recover_executor_reap_proof(
        self,
        deadline_ns: int | None = None,
    ) -> ReapProof:
        native = _CReapProof()
        _raise_status(
            self._native_call(
                self._library.cpl_journal_recover_executor_reap_proof,
                _deadline(deadline_ns),
                ctypes.byref(native),
            )
        )
        return ReapProof(
            bytes(native.allocation_nonce),
            native.generation,
            native.authority_epoch,
            _identity_from_c(native.identity),
            bytes(native.certified_hash),
            bytes(native.capability),
        )

    @staticmethod
    def _proof_to_c(proof: ReapProof) -> _CReapProof:
        if not isinstance(proof, ReapProof):
            raise TypeError("a native reap proof is required")
        native = _CReapProof()
        _set_bytes(native.allocation_nonce, proof.allocation_nonce)
        _set_bytes(native.certified_hash, proof._certified_hash)
        _set_bytes(native.capability, proof._capability)
        native.generation = proof.generation
        native.authority_epoch = proof.authority_epoch
        native.identity = _identity_to_c(proof.identity)
        return native

    def reconcile_interrupted_batch(
        self,
        proof: ReapProof,
        deadline_ns: int | None = None,
    ) -> CanonicalRecord:
        native_proof = self._proof_to_c(proof)
        output = _CAppendResult()
        _raise_status(
            self._native_call(
                self._library.cpl_journal_reconcile_interrupted_batch,
                ctypes.byref(native_proof),
                _deadline(deadline_ns),
                ctypes.byref(output),
            )
        )
        return _canonical(output)

    def prepare_successor(
        self,
        proof: ReapProof,
        generation: int,
        candidate: str,
        *,
        claim_deadline_ns: int,
        deadline_ns: int | None = None,
    ) -> CanonicalRecord:
        native_proof = self._proof_to_c(proof)
        output = _CAppendResult()
        _raise_status(
            self._native_call(
                self._library.cpl_journal_prepare_successor,
                ctypes.byref(native_proof),
                generation,
                _fixed_id(candidate),
                claim_deadline_ns,
                _deadline(deadline_ns),
                ctypes.byref(output),
            )
        )
        return _canonical(output)

    def _fault(self, name: str) -> Any:
        self._require_open()
        try:
            return getattr(self._library, name)
        except AttributeError as error:
            raise JournalError(JournalErrorCode.UNSUPPORTED) from error

    @staticmethod
    def force_atfork_registration_failure_for_test(
        library_path: Path, enabled: bool
    ) -> None:
        library = _load_library(library_path)
        try:
            function = library.cpl_fault_force_atfork_registration_failure
        except AttributeError as error:
            raise JournalError(JournalErrorCode.UNSUPPORTED) from error
        function.argtypes = [ctypes.c_bool]
        function.restype = ctypes.c_int
        _raise_status(int(function(enabled)))

    def configure_lifecycle_pause_for_test(
        self, point: str, notify_fd: int, wait_fd: int
    ) -> None:
        points = {
            "before_workdir_bound_append": 1,
            "before_retirement_expiry_check": 2,
            "after_batch_admission_append": 3,
            "before_retirement_append": 4,
            "after_first_dependent_scan": 5,
            "before_activation_append": 6,
            "before_successor_append": 7,
            "before_authority_replacement_append": 8,
            "before_batch_completion_append": 9,
            "after_batch_completion_append": 10,
        }
        try:
            selected = points[point]
        except KeyError as error:
            raise ValueError("unknown lifecycle pause point") from error
        function = self._fault("cpl_fault_configure_lifecycle_pause")
        function.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_int,
        ]
        function.restype = ctypes.c_int
        _raise_status(self._native_call(function, selected, notify_fd, wait_fd))

    @staticmethod
    def configure_create_pause_for_test(
        library_path: Path,
        point: str,
        notify_fd: int,
        wait_fd: int,
        deadline_ns: int,
    ) -> None:
        points = {
            "before_create_openat": 1,
            "before_create_preallocate": 2,
            "before_create_intent_write": 3,
            "before_create_fullfsync": 4,
            "before_create_parent_fsync": 5,
        }
        try:
            selected = points[point]
        except KeyError as error:
            raise ValueError("unknown create pause point") from error
        library = _load_library(library_path)
        try:
            function = library.cpl_fault_configure_create_pause
        except AttributeError as error:
            raise JournalError(JournalErrorCode.UNSUPPORTED) from error
        function.argtypes = [
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
        ]
        function.restype = ctypes.c_int
        _raise_status(
            int(function(selected, notify_fd, wait_fd, deadline_ns))
        )

    def fail_batch_after_step_for_test(self, step: int) -> None:
        function = self._fault("cpl_fault_fail_batch_after_step")
        function.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        function.restype = ctypes.c_int
        _raise_status(self._native_call(function, step))

    def fail_batch_after_effect_for_test(self, step: int) -> None:
        function = self._fault("cpl_fault_fail_batch_after_effect")
        function.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        function.restype = ctypes.c_int
        _raise_status(self._native_call(function, step))

    def fail_next_workdir_parent_fsync_for_test(self) -> None:
        function = self._fault("cpl_fault_fail_next_workdir_parent_fsync")
        function.argtypes = [ctypes.c_void_p]
        function.restype = ctypes.c_int
        _raise_status(self._native_call(function))

    def raw_append_for_test(self, encoded: bytes) -> None:
        function = self._fault("cpl_fault_append_bytes")
        function.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_uint32,
        ]
        function.restype = ctypes.c_int
        payload = (ctypes.c_uint8 * len(encoded)).from_buffer_copy(encoded)
        _raise_status(self._native_call(function, payload, len(encoded)))

    def encode_physical_for_test(self, record: Record) -> bytes:
        function = self._fault("cpl_fault_encode_record")
        function.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        function.restype = ctypes.c_int
        native_record = _record_to_c(record)
        payload = ctypes.cast(
            ctypes.byref(native_record), ctypes.POINTER(ctypes.c_uint8)
        )
        capacity = ctypes.sizeof(native_record) + 256
        output = (ctypes.c_uint8 * capacity)()
        length = ctypes.c_uint32()
        _raise_status(
            self._native_call(
                function,
                payload,
                ctypes.sizeof(native_record),
                output,
                capacity,
                ctypes.byref(length),
            )
        )
        return bytes(output[: length.value])

    def inject_header_mismatch_for_test(self, record: Record, field: str) -> None:
        mismatches = {"cleanup_epoch": 1, "payload_type": 2, "duplicate_parent": 3}
        try:
            mismatch = mismatches[field]
        except KeyError as error:
            raise ValueError("unknown mismatch field") from error
        function = self._fault("cpl_fault_inject_header_mismatch")
        function.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        function.restype = ctypes.c_int
        native_record = _record_to_c(record)
        payload = ctypes.cast(
            ctypes.byref(native_record), ctypes.POINTER(ctypes.c_uint8)
        )
        _raise_status(
            self._native_call(
                function,
                payload,
                ctypes.sizeof(native_record),
                mismatch,
            )
        )

    def hold_append_lock_for_test(
        self,
        notify_fd: int,
        wait_fd: int,
        deadline_ns: int | None = None,
    ) -> None:
        function = self._fault("cpl_fault_hold_append_lock")
        function.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
        ]
        function.restype = ctypes.c_int
        _raise_status(
            self._native_call(
                function, notify_fd, wait_fd, _deadline(deadline_ns)
            )
        )

    def hold_action_lock_for_test(
        self,
        notify_fd: int,
        wait_fd: int,
        deadline_ns: int | None = None,
    ) -> None:
        function = self._fault("cpl_fault_hold_action_lock")
        function.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
        ]
        function.restype = ctypes.c_int
        _raise_status(
            self._native_call(
                function, notify_fd, wait_fd, _deadline(deadline_ns)
            )
        )

    def probe_action_lock_for_test(self, deadline_ns: int | None = None) -> None:
        function = self._fault("cpl_fault_probe_action_lock")
        function.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        function.restype = ctypes.c_int
        _raise_status(self._native_call(function, _deadline(deadline_ns)))

    def certify_head_with_pause_for_test(
        self,
        notify_fd: int,
        wait_fd: int,
        deadline_ns: int | None = None,
    ) -> CertifiedHead:
        function = self._fault("cpl_fault_configure_certify_pause")
        function.argtypes = [ctypes.c_int, ctypes.c_int]
        function.restype = ctypes.c_int
        _raise_status(int(function(notify_fd, wait_fd)))
        return self.certify_head(deadline_ns)

    def try_gated_marker_for_test(
        self,
        gate_kind: str,
        receipt: JournalWorkdirReceipt | None,
        marker_parent_dirfd: int,
        marker_name: str,
    ) -> bool:
        gates = {"spawn": 1, "execute": 2}
        try:
            kind = gates[gate_kind]
        except KeyError as error:
            raise ValueError("unknown gate kind") from error
        function = self._fault("cpl_fault_try_bound_gate")
        function.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_CWorkdirReceipt),
            ctypes.c_int,
            ctypes.c_char_p,
        ]
        function.restype = ctypes.c_int
        native = _CWorkdirReceipt()
        pointer = None
        if receipt is not None:
            native.state = 3 if receipt.workdir_parent_dirsynced else 0
            _set_bytes(native.bound_hash, receipt.bound_hash)
            _set_bytes(native.capability, receipt._capability)
            native.workdir_dev = receipt.workdir_dev
            native.workdir_ino = receipt.workdir_ino
            pointer = ctypes.byref(native)
        status = self._native_call(
            function,
            kind,
            pointer,
            marker_parent_dirfd,
            _component_bytes(marker_name),
        )
        if status == JournalErrorCode.RECEIPT:
            return False
        _raise_status(status)
        return True

    @staticmethod
    def try_cleanup_slot_marker_for_test(
        library_path: Path,
        receipt: JournalDeleteReceipt,
        marker_parent_dirfd: int,
        marker_name: str,
    ) -> bool:
        library = _load_library(library_path)
        try:
            function = library.cpl_fault_try_cleanup_gate
        except AttributeError as error:
            raise JournalError(JournalErrorCode.UNSUPPORTED) from error
        function.argtypes = [
            ctypes.POINTER(_CDeleteReceipt),
            ctypes.c_int,
            ctypes.c_char_p,
        ]
        function.restype = ctypes.c_int
        native = _CDeleteReceipt()
        native.state = 2 if receipt.journal_unlink_parent_dirsynced else 0
        native.slot_releasable = receipt.slot_releasable
        if len(receipt._capability) == HASH_SIZE:
            _set_bytes(native.capability, receipt._capability)
        status = int(
            function(
                ctypes.byref(native),
                marker_parent_dirfd,
                _component_bytes(marker_name),
            )
        )
        if status == JournalErrorCode.RECEIPT:
            return False
        _raise_status(status)
        return True


__all__ = [
    "AdmittedBatch",
    "CanonicalChain",
    "CanonicalRecord",
    "CertifiedDone",
    "CertifiedHead",
    "Journal",
    "JournalCreateReceipt",
    "JournalDeleteReceipt",
    "JournalError",
    "JournalErrorCode",
    "JournalWorkdirReceipt",
    "ReapProof",
    "RecordClass",
    "UnreleasedPartialCreate",
    "UnconfirmedReason",
]
