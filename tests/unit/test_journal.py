from __future__ import annotations

import copy
import os
import time
from pathlib import Path

import pytest

from claude_sdk_proxy.journal import (
    CertifiedDone,
    Journal,
    JournalCreateReceipt,
    JournalDeleteReceipt,
    JournalError,
    JournalErrorCode,
    RecordClass,
    UnreleasedPartialCreate,
)
from claude_sdk_proxy.lifecycle import (
    Record,
    StateKind,
)

NORMAL_LIMIT = 32 * 1024
HARD_LIMIT = 48 * 1024


def _make_lock_files(parent_dirfd: int, journal_name: str) -> None:
    for suffix in (".append.lock", ".action.lock"):
        fd = os.open(
            journal_name + suffix,
            os.O_RDONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_dirfd,
        )
        os.close(fd)


def make_journal(
    tmp_path: Path,
    *,
    nonce: bytes = b"n" * 32,
    normal_limit: int = NORMAL_LIMIT,
    hard_limit: int = HARD_LIMIT,
    fault: bool = False,
) -> tuple[Journal, JournalCreateReceipt, int]:
    os.chmod(tmp_path, 0o700)
    parent_dirfd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    _make_lock_files(parent_dirfd, "allocation.journal")
    arguments = (
        parent_dirfd,
        "allocation.journal",
        nonce,
        normal_limit,
        hard_limit,
    )
    keywords = {
        "workdir_parent_dirfd": parent_dirfd,
        "workdir_name": "allocation.workdir",
    }
    if fault:
        journal, receipt = Journal._create_at_for_test(
            *arguments,
            **keywords,
            library_path=(
                Path(__file__).resolve().parents[2]
                / "build/lib/libclaude_proxy_lifecycle_fault.dylib"
            ),
        )
    else:
        journal, receipt = Journal.create_at(*arguments, **keywords)
    return journal, receipt, parent_dirfd


def test_create_returns_receipt_only_after_durable_intent(tmp_path: Path) -> None:
    journal, receipt, parent_dirfd = make_journal(tmp_path)
    try:
        assert receipt == JournalCreateReceipt(intent_parent_dirsynced=True)
        assert receipt.intent_hash == journal.scan().head.hash
        assert journal.scan().head.record.kind is StateKind.NO_GENERATION
        assert not (tmp_path / "allocation.workdir").exists()
    finally:
        journal.close()
        os.close(parent_dirfd)


def test_create_never_recreates_or_truncates_existing_journal(tmp_path: Path) -> None:
    journal, _, parent_dirfd = make_journal(tmp_path)
    before = (tmp_path / "allocation.journal").read_bytes()
    try:
        with pytest.raises(JournalError) as caught:
            Journal.create_at(
                parent_dirfd,
                "allocation.journal",
                b"n" * 32,
                NORMAL_LIMIT,
                HARD_LIMIT,
            )
        assert caught.value.code is JournalErrorCode.EXISTS
        assert (tmp_path / "allocation.journal").read_bytes() == before
    finally:
        journal.close()
        os.close(parent_dirfd)


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "a/b", "/absolute", "allocation.journal/child"],
)
def test_names_are_single_validated_components(tmp_path: Path, name: str) -> None:
    os.chmod(tmp_path, 0o700)
    parent_dirfd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        with pytest.raises(JournalError) as caught:
            Journal.create_at(
                parent_dirfd,
                name,
                b"n" * 32,
                NORMAL_LIMIT,
                HARD_LIMIT,
            )
        assert caught.value.code is JournalErrorCode.INVALID_ARGUMENT
    finally:
        os.close(parent_dirfd)


def test_open_rejects_symlink_wrong_owner_mode_and_nonce(tmp_path: Path) -> None:
    journal, _, parent_dirfd = make_journal(tmp_path)
    journal.close()
    try:
        with pytest.raises(JournalError) as caught:
            Journal.open_at(
                parent_dirfd,
                "allocation.journal",
                b"x" * 32,
                NORMAL_LIMIT,
                HARD_LIMIT,
            )
        assert caught.value.code is JournalErrorCode.NONCE_MISMATCH

        os.chmod(tmp_path / "allocation.journal", 0o640)
        with pytest.raises(JournalError) as caught:
            Journal.open_at(
                parent_dirfd,
                "allocation.journal",
                b"n" * 32,
                NORMAL_LIMIT,
                HARD_LIMIT,
            )
        assert caught.value.code is JournalErrorCode.UNSAFE_FILE

        os.chmod(tmp_path / "allocation.journal", 0o600)
        os.rename(
            tmp_path / "allocation.journal",
            tmp_path / "allocation.journal.real",
        )
        os.symlink("allocation.journal.real", tmp_path / "allocation.journal")
        with pytest.raises(JournalError) as caught:
            Journal.open_at(
                parent_dirfd,
                "allocation.journal",
                b"n" * 32,
                NORMAL_LIMIT,
                HARD_LIMIT,
            )
        assert caught.value.code in {
            JournalErrorCode.SYMLINK,
            JournalErrorCode.UNSAFE_FILE,
        }
    finally:
        os.close(parent_dirfd)


def test_first_valid_child_wins(tmp_path: Path) -> None:
    journal, _, parent_dirfd = make_journal(tmp_path, fault=True)
    try:
        intent = journal.scan().head
        future = time.monotonic_ns() + 1_000_000_000
        winner = journal.append(
            Record.prepared(
                1,
                "candidate-1",
                claim_deadline_ns=future,
                parent=intent.hash,
            ),
            RecordClass.NORMAL,
        )
        journal.raw_append_for_test(
            journal.encode_physical_for_test(
                Record.prepared(
                    1,
                    "candidate-2",
                    claim_deadline_ns=future,
                    parent=intent.hash,
                )
            )
        )

        chain = journal.scan()
        assert chain.head.hash == winner.hash
        assert chain.stale_records == 1
    finally:
        journal.close()
        os.close(parent_dirfd)


@pytest.mark.parametrize("damage", ["short", "torn", "checksum", "garbage"])
def test_scanner_resynchronizes_after_invalid_physical_bytes(
    tmp_path: Path,
    damage: str,
) -> None:
    journal, _, parent_dirfd = make_journal(tmp_path, fault=True)
    try:
        intent = journal.scan().head
        valid = Record.prepared(
            1,
            "candidate-1",
            claim_deadline_ns=time.monotonic_ns() + 1_000_000_000,
            parent=intent.hash,
        )
        encoded = journal.encode_physical_for_test(valid)
        if damage == "short":
            damaged = encoded[:12]
        elif damage == "torn":
            damaged = encoded[:-7]
        elif damage == "checksum":
            damaged = encoded[:-1] + bytes([encoded[-1] ^ 0xFF])
        else:
            damaged = b"not-a-record"
        journal.raw_append_for_test(damaged)
        accepted = journal.append(valid, RecordClass.NORMAL)

        chain = journal.scan()
        assert chain.head.hash == accepted.hash
        assert chain.invalid_bytes >= len(damaged)
    finally:
        journal.close()
        os.close(parent_dirfd)


def test_physical_eof_enforces_normal_recovery_and_hard_ceilings(
    tmp_path: Path,
) -> None:
    journal, _, parent_dirfd = make_journal(
        tmp_path,
        normal_limit=4096,
        hard_limit=16 * 1024,
        fault=True,
    )
    try:
        journal.raw_append_for_test(b"x" * 2000)
        with pytest.raises(JournalError) as caught:
            journal.append(
                Record.prepared(
                    1,
                    "candidate-1",
                    claim_deadline_ns=time.monotonic_ns() + 1_000_000_000,
                ),
                RecordClass.NORMAL,
            )
        assert caught.value.code is JournalErrorCode.NORMAL_LIMIT

        unconfirmed = journal.append(
            Record.unconfirmed("normal region exhausted"),
            RecordClass.RECOVERY,
        )
        assert unconfirmed.record.kind is StateKind.UNCONFIRMED
        assert journal.scan().head.record.kind is StateKind.UNCONFIRMED
    finally:
        journal.close()
        os.close(parent_dirfd)

    hard_path = tmp_path / "hard"
    hard_path.mkdir(mode=0o700)
    hard_journal, _, hard_parent_dirfd = make_journal(
        hard_path,
        normal_limit=4096,
        hard_limit=16 * 1024,
        fault=True,
    )
    try:
        hard_journal.raw_append_for_test(b"y" * 15_000)
        with pytest.raises(JournalError) as caught:
            hard_journal.append(
                Record.unconfirmed("hard region exhausted"),
                RecordClass.RECOVERY,
            )
        assert caught.value.code in {
            JournalErrorCode.HARD_LIMIT,
            JournalErrorCode.CORRUPT,
        }
        assert hard_journal.unhealthy is True
    finally:
        hard_journal.close()
        os.close(hard_parent_dirfd)


def test_recovery_tail_rejects_non_allowlisted_transition(tmp_path: Path) -> None:
    journal, _, parent_dirfd = make_journal(tmp_path)
    try:
        with pytest.raises(JournalError) as caught:
            journal.append(
                Record.prepared(
                    1,
                    "candidate-1",
                    claim_deadline_ns=time.monotonic_ns() + 1_000_000_000,
                ),
                RecordClass.RECOVERY,
            )
        assert caught.value.code is JournalErrorCode.RECORD_CLASS
    finally:
        journal.close()
        os.close(parent_dirfd)


def test_done_authority_cannot_be_forged_from_caller_authored_records(
    tmp_path: Path,
) -> None:
    journal, _, parent_dirfd = make_journal(tmp_path, fault=True)
    try:
        with pytest.raises(TypeError):
            CertifiedDone(b"n" * 32, b"h" * 32)
        with pytest.raises(JournalError) as caught:
            journal.certify_done()
        assert caught.value.code is JournalErrorCode.REAP_REQUIRED
    finally:
        journal.close()
        os.close(parent_dirfd)


def test_partial_create_cleanup_requires_native_authority(tmp_path: Path) -> None:
    os.chmod(tmp_path, 0o700)
    parent_dirfd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    _make_lock_files(parent_dirfd, "allocation.journal")
    fd = os.open(
        "allocation.journal",
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_CLOEXEC,
        0o600,
        dir_fd=parent_dirfd,
    )
    os.close(fd)
    journal = Journal.open_at(
        parent_dirfd,
        "allocation.journal",
        b"n" * 32,
        NORMAL_LIMIT,
        HARD_LIMIT,
        workdir_parent_dirfd=parent_dirfd,
        workdir_name="allocation.workdir",
    )
    authority = journal.certify_unreleased_partial_create()
    assert isinstance(authority, UnreleasedPartialCreate)

    with pytest.raises(TypeError):
        UnreleasedPartialCreate(b"n" * 32, b"\0" * 32)

    receipt = journal.delete_at(authority)
    assert receipt == JournalDeleteReceipt(journal_unlink_parent_dirsynced=True)
    os.close(parent_dirfd)


def test_complete_intent_needs_native_no_dependent_transition(tmp_path: Path) -> None:
    journal, _, parent_dirfd = make_journal(tmp_path)
    try:
        assert journal.scan().head.record.no_dependent_artifact is False
        authority = journal.certify_unreleased_partial_create()
        assert isinstance(authority, UnreleasedPartialCreate)
        assert journal.scan().head.record.no_dependent_artifact is True
    finally:
        journal.close()
        os.close(parent_dirfd)


def test_journal_handle_has_sole_close_and_cannot_be_copied(tmp_path: Path) -> None:
    journal, _, parent_dirfd = make_journal(tmp_path, fault=True)
    try:
        with pytest.raises(TypeError):
            copy.copy(journal)
        with pytest.raises(TypeError):
            copy.deepcopy(journal)
        assert not hasattr(journal, "fileno")
        journal.close()
        journal.close()
        with pytest.raises(JournalError) as caught:
            journal.scan()
        assert caught.value.code is JournalErrorCode.CLOSED
    finally:
        journal.close()
        os.close(parent_dirfd)


def test_reopen_cannot_widen_or_change_durable_journal_limits(tmp_path: Path) -> None:
    journal, _, parent_dirfd = make_journal(tmp_path)
    journal.close()
    try:
        with pytest.raises(JournalError) as caught:
            Journal.open_at(
                parent_dirfd,
                "allocation.journal",
                b"n" * 32,
                NORMAL_LIMIT + 4096,
                HARD_LIMIT + 4096,
                workdir_parent_dirfd=parent_dirfd,
                workdir_name="allocation.workdir",
            )
        assert caught.value.code is JournalErrorCode.IDENTITY_DRIFT
    finally:
        os.close(parent_dirfd)


def test_expired_deadline_fails_even_when_append_lock_is_available(
    tmp_path: Path,
) -> None:
    journal, _, parent_dirfd = make_journal(tmp_path)
    try:
        with pytest.raises(JournalError) as caught:
            journal.append(
                Record.prepared(
                    1,
                    "candidate-1",
                    claim_deadline_ns=time.monotonic_ns() + 1_000_000_000,
                ),
                RecordClass.NORMAL,
                deadline_ns=1,
            )
        assert caught.value.code is JournalErrorCode.LOCK_TIMEOUT
        assert journal.scan().head.sequence == 0
    finally:
        journal.close()
        os.close(parent_dirfd)


def test_unhealthy_or_hard_exhausted_journal_cannot_certify_authority(
    tmp_path: Path,
) -> None:
    journal, _, parent_dirfd = make_journal(tmp_path, fault=True)
    try:
        journal.raw_append_for_test(b"x" * HARD_LIMIT)
        with pytest.raises(JournalError) as caught:
            journal.certify_head()
        assert caught.value.code in {
            JournalErrorCode.CORRUPT,
            JournalErrorCode.HARD_LIMIT,
        }
    finally:
        journal.close()
        os.close(parent_dirfd)


def test_reopen_rejects_caller_selected_workdir_alias(tmp_path: Path) -> None:
    journal, _, parent_dirfd = make_journal(tmp_path)
    journal.close()
    try:
        with pytest.raises(JournalError) as caught:
            Journal.open_at(
                parent_dirfd,
                "allocation.journal",
                b"n" * 32,
                NORMAL_LIMIT,
                HARD_LIMIT,
                workdir_parent_dirfd=parent_dirfd,
                workdir_name="absent-alias",
            )
        assert caught.value.code is JournalErrorCode.IDENTITY_DRIFT
    finally:
        os.close(parent_dirfd)


def test_complete_intent_rollback_requires_native_absence_transition(
    tmp_path: Path,
) -> None:
    journal, _, parent_dirfd = make_journal(tmp_path)
    try:
        authority = journal.certify_no_dependent_artifacts()
        assert isinstance(authority, UnreleasedPartialCreate)
        assert journal.scan().head.record.no_dependent_artifact is True
        receipt = journal.delete_at(authority)
        assert receipt.slot_releasable is True
    finally:
        journal.close()
        os.close(parent_dirfd)


def test_production_library_has_no_raw_corruption_write_escape(tmp_path: Path) -> None:
    journal, _, parent_dirfd = make_journal(tmp_path)
    try:
        with pytest.raises(JournalError) as caught:
            journal.raw_append_for_test(b"bypass")
        assert caught.value.code is JournalErrorCode.UNSUPPORTED
        assert journal.scan().physical_eof < HARD_LIMIT
    finally:
        journal.close()
        os.close(parent_dirfd)


def test_corrupt_partial_create_cannot_mint_deletion_authority(
    tmp_path: Path,
) -> None:
    os.chmod(tmp_path, 0o700)
    parent_dirfd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    _make_lock_files(parent_dirfd, "allocation.journal")
    fd = os.open(
        "allocation.journal",
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_CLOEXEC,
        0o600,
        dir_fd=parent_dirfd,
    )
    os.close(fd)
    fault_library = (
        Path(__file__).resolve().parents[2]
        / "build/lib/libclaude_proxy_lifecycle_fault.dylib"
    )
    journal = Journal._open_at_for_test(
        parent_dirfd,
        "allocation.journal",
        b"n" * 32,
        NORMAL_LIMIT,
        HARD_LIMIT,
        workdir_parent_dirfd=parent_dirfd,
        workdir_name="allocation.workdir",
        library_path=fault_library,
    )
    try:
        journal.raw_append_for_test(b"not-an-intent")
        with pytest.raises(JournalError) as caught:
            journal.certify_unreleased_partial_create()
        assert caught.value.code is JournalErrorCode.CORRUPT
    finally:
        journal.close()
        os.close(parent_dirfd)


@pytest.mark.parametrize(
    "field",
    ["cleanup_epoch", "payload_type", "duplicate_parent"],
)
def test_header_payload_mismatch_is_never_canonical(
    tmp_path: Path,
    field: str,
) -> None:
    fault_library = (
        Path(__file__).resolve().parents[2]
        / "build/lib/libclaude_proxy_lifecycle_fault.dylib"
    )
    os.chmod(tmp_path, 0o700)
    parent_dirfd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    _make_lock_files(parent_dirfd, "allocation.journal")
    journal, _ = Journal._create_at_for_test(
        parent_dirfd,
        "allocation.journal",
        b"n" * 32,
        NORMAL_LIMIT,
        HARD_LIMIT,
        workdir_parent_dirfd=parent_dirfd,
        workdir_name="allocation.workdir",
        library_path=fault_library,
    )
    try:
        intent = journal.scan().head
        journal.inject_header_mismatch_for_test(
            Record.prepared(
                1,
                "candidate-1",
                claim_deadline_ns=time.monotonic_ns() + 1_000_000_000,
                parent=intent.hash,
            ),
            field,
        )
        chain = journal.scan()
        assert chain.head.hash == intent.hash
        assert chain.invalid_bytes > 0
    finally:
        journal.close()
        os.close(parent_dirfd)
