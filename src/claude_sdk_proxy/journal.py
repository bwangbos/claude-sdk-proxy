"""Typed ctypes ownership wrapper for the native bounded lifecycle journal."""

from __future__ import annotations

import ctypes
import os
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Final, Self

from claude_sdk_proxy.lifecycle import (
    HASH_SIZE,
    Record,
    State,
    _CRecord,
    _CState,
    _state_from_c,
)

_HEADER_FORMAT: Final = "<8sHHI32sQQ32sIII"
_HEADER_SIZE: Final = struct.calcsize(_HEADER_FORMAT)
_MAGIC: Final = b"CPLJRN01"
_FORMAT_VERSION: Final = 1
_DEFAULT_OPERATION_NS: Final = 1_000_000_000


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


class JournalError(RuntimeError):
    """Stable native journal failure without caller-controlled content."""

    def __init__(self, code: JournalErrorCode) -> None:
        super().__init__(f"native journal operation failed with code {int(code)}")
        self.code = code


class RecordClass(IntEnum):
    NORMAL = 1
    RECOVERY = 2


@dataclass(frozen=True)
class JournalCreateReceipt:
    intent_parent_dirsynced: bool
    intent_hash: bytes = field(default=b"", compare=False, repr=False)


@dataclass(frozen=True)
class JournalDeleteReceipt:
    journal_unlink_parent_dirsynced: bool

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


_AUTHORITY_TOKEN = object()


class _DeletionAuthority:
    __slots__ = ("_allocation_nonce", "_certified_hash", "_kind")

    def __init__(
        self,
        token: object,
        kind: int,
        allocation_nonce: bytes,
        certified_hash: bytes,
    ) -> None:
        if token is not _AUTHORITY_TOKEN:
            raise TypeError(
                "deletion authorities are constructed by native certification"
            )
        self._kind = kind
        self._allocation_nonce = allocation_nonce
        self._certified_hash = certified_hash

    def _fabricate_for_test(
        self,
        *,
        kind: int | None = None,
        allocation_nonce: bytes | None = None,
        certified_hash: bytes | None = None,
    ) -> _DeletionAuthority:
        authority_type: type[_DeletionAuthority]
        selected_kind = self._kind if kind is None else kind
        if selected_kind == 1:
            authority_type = CertifiedDone
        else:
            authority_type = UnreleasedPartialCreate
        return authority_type(
            _AUTHORITY_TOKEN,
            selected_kind,
            self._allocation_nonce if allocation_nonce is None else allocation_nonce,
            self._certified_hash if certified_hash is None else certified_hash,
        )


class CertifiedDone(_DeletionAuthority):
    """Opaque proof that native certification observed the exact durable DONE head."""


class UnreleasedPartialCreate(_DeletionAuthority):
    """Opaque native reconciliation proof for a create without complete INTENT."""


class _CCreateReceipt(ctypes.Structure):
    _fields_ = [
        ("state", ctypes.c_int),
        ("intent_hash", ctypes.c_uint8 * HASH_SIZE),
    ]


class _CDeleteAuthority(ctypes.Structure):
    _fields_ = [
        ("kind", ctypes.c_int),
        ("allocation_nonce", ctypes.c_uint8 * HASH_SIZE),
        ("certified_hash", ctypes.c_uint8 * HASH_SIZE),
    ]


class _CDeleteReceipt(ctypes.Structure):
    _fields_ = [("state", ctypes.c_int), ("slot_releasable", ctypes.c_bool)]


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


def _default_library_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "build/lib/libclaude_proxy_lifecycle.dylib"
    )


def _set_bytes(target: ctypes.Array[ctypes.c_uint8], value: bytes) -> None:
    if len(value) != len(target):
        raise ValueError("native fixed-size value has the wrong length")
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


def _load_library(path: Path) -> ctypes.CDLL:
    library = ctypes.CDLL(str(path), use_errno=True)
    handle_pointer = ctypes.POINTER(ctypes.c_void_p)
    byte_pointer = ctypes.POINTER(ctypes.c_uint8)

    library.cpl_journal_create_at.argtypes = [
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
        byte_pointer,
    ]
    library.cpl_journal_append.restype = ctypes.c_int
    library.cpl_journal_scan.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_CChain),
    ]
    library.cpl_journal_scan.restype = ctypes.c_int
    library.cpl_journal_certify.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint64,
        ctypes.POINTER(_CCertifiedHead),
    ]
    library.cpl_journal_certify.restype = ctypes.c_int
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
    library.cpl_journal_reconcile_absent_after_crash.argtypes = (
        library.cpl_journal_delete_at.argtypes
    )
    library.cpl_journal_reconcile_absent_after_crash.restype = ctypes.c_int
    library.cpl_journal_close.argtypes = [ctypes.c_void_p]
    library.cpl_journal_close.restype = None
    return library


def _component_bytes(name: str) -> bytes:
    if not isinstance(name, str) or "\0" in name:
        raise JournalError(JournalErrorCode.INVALID_ARGUMENT)
    return name.encode("utf-8")


def _checked_nonce(nonce: bytes) -> ctypes.Array[ctypes.c_uint8]:
    if not isinstance(nonce, bytes) or len(nonce) != HASH_SIZE:
        raise JournalError(JournalErrorCode.INVALID_ARGUMENT)
    return (ctypes.c_uint8 * HASH_SIZE).from_buffer_copy(nonce)


def _crc32c(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            mask = -(crc & 1) & 0xFFFFFFFF
            crc = (crc >> 1) ^ (0x82F63B78 & mask)
    return (~crc) & 0xFFFFFFFF


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
        workdir_parent_dirfd: int | None = None,
        workdir_name: str | None = None,
        library_path: Path,
    ) -> tuple[Self, JournalCreateReceipt]:
        library = _load_library(library_path)
        handle = ctypes.c_void_p()
        receipt = _CCreateReceipt()
        nonce_array = _checked_nonce(nonce)
        status = int(
            library.cpl_journal_create_at(
                parent_dirfd,
                _component_bytes(journal_name),
                nonce_array,
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
        selected_workdir_parent = (
            parent_dirfd if workdir_parent_dirfd is None else workdir_parent_dirfd
        )
        selected_workdir_name = (
            journal_name + ".workdir" if workdir_name is None else workdir_name
        )
        journal = cls(
            library,
            handle,
            parent_dirfd,
            journal_name,
            nonce,
            normal_limit,
            hard_limit,
            selected_workdir_parent,
            selected_workdir_name,
        )
        return journal, JournalCreateReceipt(
            intent_parent_dirsynced=True,
            intent_hash=bytes(receipt.intent_hash),
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
        workdir_parent_dirfd: int | None = None,
        workdir_name: str | None = None,
        library_path: Path,
    ) -> Self:
        library = _load_library(library_path)
        handle = ctypes.c_void_p()
        nonce_array = _checked_nonce(nonce)
        status = int(
            library.cpl_journal_open_at(
                parent_dirfd,
                _component_bytes(journal_name),
                nonce_array,
                normal_limit,
                hard_limit,
                ctypes.byref(handle),
            )
        )
        _raise_status(status)
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
            parent_dirfd if workdir_parent_dirfd is None else workdir_parent_dirfd,
            journal_name + ".workdir" if workdir_name is None else workdir_name,
        )

    @property
    def closed(self) -> bool:
        return not bool(self._handle.value)

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
            self.close()
        except Exception:
            pass

    def _require_open(self) -> None:
        if self.closed:
            raise JournalError(JournalErrorCode.CLOSED)

    def close(self) -> None:
        if self._handle.value:
            self._library.cpl_journal_close(self._handle)
            self._handle = ctypes.c_void_p()

    def scan(self) -> CanonicalChain:
        self._require_open()
        chain = _CChain()
        _raise_status(int(self._library.cpl_journal_scan(self._handle, chain)))
        state = _state_from_c(chain.state)
        return CanonicalChain(
            head=CanonicalRecord(
                hash=bytes(chain.head_hash),
                sequence=chain.head_sequence,
                record=state,
            ),
            physical_eof=chain.physical_eof,
            canonical_records=chain.canonical_records,
            invalid_bytes=chain.invalid_bytes,
            stale_records=chain.stale_records,
            has_intent=chain.has_intent,
            unhealthy=chain.unhealthy,
        )

    def append(
        self,
        record: Record,
        record_class: RecordClass,
        *,
        deadline_ns: int | None = None,
    ) -> CanonicalRecord:
        self._require_open()
        encoded = record.encode()
        payload = (ctypes.c_uint8 * len(encoded)).from_buffer_copy(encoded)
        output = (ctypes.c_uint8 * HASH_SIZE)()
        status = int(
            self._library.cpl_journal_append(
                self._handle,
                payload,
                len(encoded),
                int(record_class),
                _deadline(deadline_ns),
                output,
            )
        )
        _raise_status(status)
        appended_hash = bytes(output)
        head = self.scan().head
        if head.hash != appended_hash:
            return CanonicalRecord(appended_hash, head.sequence, head.record)
        return head

    def certify_head(self, deadline_ns: int | None = None) -> CertifiedHead:
        self._require_open()
        certified = _CCertifiedHead()
        _raise_status(
            int(
                self._library.cpl_journal_certify(
                    self._handle,
                    _deadline(deadline_ns),
                    certified,
                )
            )
        )
        return CertifiedHead(
            hash=bytes(certified.head_hash),
            sequence=certified.head_sequence,
            physical_eof=certified.physical_eof,
            state=_state_from_c(certified.state),
            has_intent=certified.has_intent,
            attempts=certified.attempts,
        )

    def _certified_native(self, deadline_ns: int | None = None) -> _CCertifiedHead:
        certified = _CCertifiedHead()
        _raise_status(
            int(
                self._library.cpl_journal_certify(
                    self._handle,
                    _deadline(deadline_ns),
                    certified,
                )
            )
        )
        return certified

    def certify_done(self, deadline_ns: int | None = None) -> CertifiedDone:
        self._require_open()
        certified = self._certified_native(deadline_ns)
        if not certified.done_authority:
            raise JournalError(JournalErrorCode.AUTHORITY)
        return CertifiedDone(
            _AUTHORITY_TOKEN,
            1,
            self._nonce,
            bytes(certified.head_hash),
        )

    def certify_unreleased_partial_create(
        self, deadline_ns: int | None = None
    ) -> UnreleasedPartialCreate:
        self._require_open()
        certified = self._certified_native(deadline_ns)
        if not certified.partial_create_authority:
            raise JournalError(JournalErrorCode.AUTHORITY)
        return UnreleasedPartialCreate(
            _AUTHORITY_TOKEN,
            2,
            self._nonce,
            bytes(certified.head_hash),
        )

    @staticmethod
    def _authority_to_c(authority: _DeletionAuthority) -> _CDeleteAuthority:
        if not isinstance(authority, _DeletionAuthority):
            raise TypeError("a native typed deletion authority is required")
        native = _CDeleteAuthority()
        native.kind = authority._kind
        _set_bytes(native.allocation_nonce, authority._allocation_nonce)
        _set_bytes(native.certified_hash, authority._certified_hash)
        return native

    def _delete_common(
        self,
        entry_point: Callable[..., int],
        authority: CertifiedDone | UnreleasedPartialCreate,
        deadline_ns: int | None,
    ) -> JournalDeleteReceipt:
        self._require_open()
        native_authority = self._authority_to_c(authority)
        receipt = _CDeleteReceipt()
        status = int(
            entry_point(
                ctypes.byref(self._handle),
                self._parent_dirfd,
                _component_bytes(self._journal_name),
                self._workdir_parent_dirfd,
                _component_bytes(self._workdir_name),
                ctypes.byref(native_authority),
                _deadline(deadline_ns),
                ctypes.byref(receipt),
            )
        )
        _raise_status(status)
        if self._handle.value or receipt.state != 2 or not receipt.slot_releasable:
            raise JournalError(JournalErrorCode.SYSTEM)
        return JournalDeleteReceipt(journal_unlink_parent_dirsynced=True)

    def delete_at(
        self,
        authority: CertifiedDone | UnreleasedPartialCreate,
        deadline_ns: int | None = None,
    ) -> JournalDeleteReceipt:
        return self._delete_common(
            self._library.cpl_journal_delete_at,
            authority,
            deadline_ns,
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

    def encode_physical_for_test(self, record: Record) -> bytes:
        return self._physical_from_payload_for_test(record.encode())

    def _physical_from_payload_for_test(self, payload: bytes) -> bytes:
        if len(payload) != ctypes.sizeof(_CRecord):
            raise ValueError("test payload has an invalid size")
        chain = self.scan()
        parent_offset = _CRecord.parent_hash.offset
        parent = payload[parent_offset : parent_offset + HASH_SIZE]
        if parent == bytes(HASH_SIZE):
            parent = chain.head.hash
        sequence = chain.head.sequence + 1
        if parent != chain.head.hash:
            sequence = chain.head.sequence
        cleanup_epoch = struct.unpack_from(
            "<Q", payload, _CRecord.cleanup_epoch.offset
        )[0]
        payload_type = struct.unpack_from("<I", payload, _CRecord.kind.offset)[0]
        header = struct.pack(
            _HEADER_FORMAT,
            _MAGIC,
            _FORMAT_VERSION,
            _HEADER_SIZE,
            _HEADER_SIZE + len(payload),
            self._nonce,
            sequence,
            cleanup_epoch,
            parent,
            payload_type,
            len(payload),
            0,
        )
        checksum = _crc32c(header + payload)
        return header[:-4] + struct.pack("<I", checksum) + payload

    def raw_append_for_test(self, encoded: bytes) -> None:
        self._require_open()
        physical = encoded
        if not encoded.startswith(_MAGIC):
            if len(encoded) == ctypes.sizeof(_CRecord):
                physical = self._physical_from_payload_for_test(encoded)
        fd = os.open(
            self._journal_name,
            os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=self._parent_dirfd,
        )
        try:
            if os.write(fd, physical) != len(physical):
                raise JournalError(JournalErrorCode.IO_SHORT)
        finally:
            os.close(fd)

    def hold_append_lock_for_test(
        self,
        notify_fd: int,
        wait_fd: int,
        deadline_ns: int | None = None,
    ) -> None:
        self._require_open()
        try:
            function = self._library.cpl_fault_hold_append_lock
        except AttributeError as error:
            raise JournalError(JournalErrorCode.UNSUPPORTED) from error
        function.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
        ]
        function.restype = ctypes.c_int
        _raise_status(
            int(function(self._handle, notify_fd, wait_fd, _deadline(deadline_ns)))
        )

    def certify_head_with_pause_for_test(
        self,
        notify_fd: int,
        wait_fd: int,
        deadline_ns: int | None = None,
    ) -> CertifiedHead:
        self._require_open()
        try:
            configure = self._library.cpl_fault_configure_certify_pause
        except AttributeError as error:
            raise JournalError(JournalErrorCode.UNSUPPORTED) from error
        configure.argtypes = [ctypes.c_int, ctypes.c_int]
        configure.restype = ctypes.c_int
        _raise_status(int(configure(notify_fd, wait_fd)))
        return self.certify_head(deadline_ns)


__all__ = [
    "CanonicalChain",
    "CanonicalRecord",
    "CertifiedDone",
    "CertifiedHead",
    "Journal",
    "JournalCreateReceipt",
    "JournalDeleteReceipt",
    "JournalError",
    "JournalErrorCode",
    "RecordClass",
    "UnreleasedPartialCreate",
]
