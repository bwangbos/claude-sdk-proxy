from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_sdk_proxy.journal import (
    CertifiedDone,
    Journal,
    JournalDeleteReceipt,
    JournalError,
    JournalErrorCode,
    RecordClass,
    UnreleasedPartialCreate,
)
from claude_sdk_proxy.lifecycle import ALL_COMPLETED_STEPS, Record

NORMAL_LIMIT = 64 * 1024
HARD_LIMIT = 96 * 1024
CREATE_POINTS = (
    "after_openat",
    "after_preallocate",
    "after_intent_append",
    "after_journal_fullfsync",
    "after_intent_parent_fsync",
)
DONE_DELETE_POINTS = (
    "after_authority_revalidated",
    "after_process_absence_verified",
    "after_workdir_absence_verified",
    "after_journal_unlinkat",
    "after_journal_unlink_parent_fsync",
)
PARTIAL_DELETE_POINTS = (
    "after_authority_revalidated",
    "after_workdir_absence_verified",
    "after_journal_unlinkat",
    "after_journal_unlink_parent_fsync",
)


def _fault_library() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "build/lib/libclaude_proxy_lifecycle_fault.dylib"
    )


def _make_lock_files(parent_dirfd: int, journal_name: str) -> None:
    for suffix in (".append.lock", ".action.lock"):
        fd = os.open(
            journal_name + suffix,
            os.O_RDONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_dirfd,
        )
        os.close(fd)


def _open_parent(tmp_path: Path) -> int:
    os.chmod(tmp_path, 0o700)
    return os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)


def _create(
    parent_dirfd: int,
    *,
    library_path: Path | None = None,
) -> Journal:
    if library_path is None:
        journal, _ = Journal.create_at(
            parent_dirfd,
            "allocation.journal",
            b"n" * 32,
            NORMAL_LIMIT,
            HARD_LIMIT,
            workdir_parent_dirfd=parent_dirfd,
            workdir_name="allocation.workdir",
        )
    else:
        journal, _ = Journal._create_at_for_test(
            parent_dirfd,
            "allocation.journal",
            b"n" * 32,
            NORMAL_LIMIT,
            HARD_LIMIT,
            workdir_parent_dirfd=parent_dirfd,
            workdir_name="allocation.workdir",
            library_path=library_path,
        )
    return journal


def _open(
    parent_dirfd: int,
    *,
    library_path: Path | None = None,
) -> Journal:
    if library_path is None:
        return Journal.open_at(
            parent_dirfd,
            "allocation.journal",
            b"n" * 32,
            NORMAL_LIMIT,
            HARD_LIMIT,
            workdir_parent_dirfd=parent_dirfd,
            workdir_name="allocation.workdir",
        )
    return Journal._open_at_for_test(
        parent_dirfd,
        "allocation.journal",
        b"n" * 32,
        NORMAL_LIMIT,
        HARD_LIMIT,
        workdir_parent_dirfd=parent_dirfd,
        workdir_name="allocation.workdir",
        library_path=library_path,
    )


def _append_done_chain(
    journal: Journal,
    *,
    process_pid: int = 0,
    completed_steps: int = ALL_COMPLETED_STEPS,
) -> None:
    journal.append(
        Record.prepared(1, "executor-1", claim_deadline_ns=100),
        RecordClass.NORMAL,
    )
    journal.append(
        Record.active_ready(
            1,
            "executor-1",
            lease_deadline_ns=200,
            completed_steps=completed_steps,
            process_pid=process_pid,
        ),
        RecordClass.NORMAL,
    )
    journal.append(Record.done(1, "executor-1"), RecordClass.NORMAL)


@pytest.mark.parametrize("point", CREATE_POINTS)
def test_create_crashes_never_authorize_workdir_or_spawn(
    tmp_path: Path,
    point: str,
) -> None:
    parent_dirfd = _open_parent(tmp_path)
    _make_lock_files(parent_dirfd, "allocation.journal")
    pid = os.fork()
    if pid == 0:
        os.environ["CPL_FAULT_POINT"] = point
        _create(parent_dirfd, library_path=_fault_library())
        os._exit(0)

    _, status = os.waitpid(pid, 0)
    try:
        assert os.waitstatus_to_exitcode(status) == 91
        assert not (tmp_path / "allocation.workdir").exists()
        assert not (tmp_path / "spawned").exists()
        assert not (tmp_path / "input-released").exists()

        reconciled = _open(parent_dirfd)
        try:
            chain = reconciled.scan()
            if point in {"after_openat", "after_preallocate"}:
                assert chain.has_intent is False
                authority = reconciled.certify_unreleased_partial_create()
                assert isinstance(authority, UnreleasedPartialCreate)
            else:
                assert chain.has_intent is True
                with pytest.raises(JournalError) as caught:
                    reconciled.certify_unreleased_partial_create()
                assert caught.value.code is JournalErrorCode.AUTHORITY
        finally:
            reconciled.close()
    finally:
        os.close(parent_dirfd)


def _prepare_delete_case(
    tmp_path: Path,
    authority_kind: str,
) -> tuple[int, Journal, CertifiedDone | UnreleasedPartialCreate]:
    parent_dirfd = _open_parent(tmp_path)
    _make_lock_files(parent_dirfd, "allocation.journal")
    if authority_kind == "done":
        retained = _create(parent_dirfd)
        _append_done_chain(retained)
        authority: CertifiedDone | UnreleasedPartialCreate = retained.certify_done()
    else:
        fd = os.open(
            "allocation.journal",
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_dirfd,
        )
        os.close(fd)
        retained = _open(parent_dirfd)
        authority = retained.certify_unreleased_partial_create()
    return parent_dirfd, retained, authority


@pytest.mark.parametrize(
    ("authority_kind", "point"),
    [
        *(("done", point) for point in DONE_DELETE_POINTS),
        *(("partial", point) for point in PARTIAL_DELETE_POINTS),
    ],
)
def test_delete_crash_releases_slot_only_after_fresh_absent_reconciliation(
    tmp_path: Path,
    authority_kind: str,
    point: str,
) -> None:
    parent_dirfd, retained, authority = _prepare_delete_case(tmp_path, authority_kind)
    pid = os.fork()
    if pid == 0:
        os.environ["CPL_FAULT_POINT"] = point
        retained.close()
        child = _open(parent_dirfd, library_path=_fault_library())
        child.delete_at(authority)
        os._exit(0)

    _, status = os.waitpid(pid, 0)
    try:
        assert os.waitstatus_to_exitcode(status) == 91
        unlinked = point in {
            "after_journal_unlinkat",
            "after_journal_unlink_parent_fsync",
        }
        assert (tmp_path / "allocation.journal").exists() is not unlinked

        if not unlinked:
            with pytest.raises(JournalError) as caught:
                retained.reconcile_absent_after_crash(authority)
            assert caught.value.code is JournalErrorCode.NOT_ABSENT
            assert retained.closed is False
        else:
            receipt = retained.reconcile_absent_after_crash(authority)
            assert receipt == JournalDeleteReceipt(
                journal_unlink_parent_dirsynced=True
            )
            assert receipt.slot_releasable is True
            assert retained.closed is True
    finally:
        retained.close()
        os.close(parent_dirfd)


def test_reconcile_absent_rejects_fabricated_authority(tmp_path: Path) -> None:
    parent_dirfd, retained, authority = _prepare_delete_case(tmp_path, "done")
    child = _open(parent_dirfd)
    child.delete_at(authority)
    fabricated = authority._fabricate_for_test(certified_hash=b"x" * 32)
    try:
        with pytest.raises(JournalError) as caught:
            retained.reconcile_absent_after_crash(fabricated)
        assert caught.value.code is JournalErrorCode.AUTHORITY
        assert retained.closed is False
    finally:
        retained.close()
        os.close(parent_dirfd)


def test_delete_rejects_false_done_and_missing_reap_proof(tmp_path: Path) -> None:
    parent_dirfd = _open_parent(tmp_path)
    _make_lock_files(parent_dirfd, "allocation.journal")
    journal = _create(parent_dirfd)
    try:
        intent = journal.scan().head
        journal.raw_append_for_test(
            journal.encode_physical_for_test(
                Record.done(1, "executor-1", parent=intent.hash)
            )
        )
        assert journal.scan().head.hash == intent.hash
        with pytest.raises(JournalError) as caught:
            journal.certify_done()
        assert caught.value.code is JournalErrorCode.AUTHORITY

        journal.append(
            Record.prepared(1, "executor-1", claim_deadline_ns=100),
            RecordClass.NORMAL,
        )
        journal.append(
            Record.active_ready(
                1,
                "executor-1",
                lease_deadline_ns=200,
                completed_steps=ALL_COMPLETED_STEPS & ~2,
            ),
            RecordClass.NORMAL,
        )
        with pytest.raises(JournalError) as caught:
            journal.append(Record.done(1, "executor-1"), RecordClass.NORMAL)
        assert caught.value.code is JournalErrorCode.ILLEGAL_TRANSITION
    finally:
        journal.close()
        os.close(parent_dirfd)


def test_certification_rejects_live_recorded_process(tmp_path: Path) -> None:
    parent_dirfd = _open_parent(tmp_path)
    _make_lock_files(parent_dirfd, "allocation.journal")
    journal = _create(parent_dirfd)
    try:
        _append_done_chain(journal, process_pid=os.getpid())
        with pytest.raises(JournalError) as caught:
            journal.certify_done()
        assert caught.value.code is JournalErrorCode.PROCESS_PRESENT
    finally:
        journal.close()
        os.close(parent_dirfd)


def test_delete_rejects_wrong_kind_or_wrong_hash_authority(tmp_path: Path) -> None:
    parent_dirfd, journal, authority = _prepare_delete_case(tmp_path, "done")
    try:
        for fabricated in (
            authority._fabricate_for_test(kind=2),
            authority._fabricate_for_test(certified_hash=b"x" * 32),
            authority._fabricate_for_test(allocation_nonce=b"x" * 32),
        ):
            with pytest.raises(JournalError) as caught:
                journal.delete_at(fabricated)
            assert caught.value.code is JournalErrorCode.AUTHORITY
            assert (tmp_path / "allocation.journal").exists()
    finally:
        journal.close()
        os.close(parent_dirfd)


@pytest.mark.parametrize("workdir_kind", ["directory", "nonempty", "symlink"])
def test_delete_rejects_present_or_symlinked_workdir(
    tmp_path: Path,
    workdir_kind: str,
) -> None:
    parent_dirfd, journal, authority = _prepare_delete_case(tmp_path, "done")
    workdir = tmp_path / "allocation.workdir"
    if workdir_kind == "symlink":
        workdir.symlink_to("missing-target")
    else:
        workdir.mkdir(mode=0o700)
        if workdir_kind == "nonempty":
            (workdir / "dependent").write_bytes(b"dependency")
    try:
        with pytest.raises(JournalError) as caught:
            journal.delete_at(authority)
        assert caught.value.code is JournalErrorCode.WORKDIR_PRESENT
        assert (tmp_path / "allocation.journal").exists()
    finally:
        journal.close()
        os.close(parent_dirfd)


def test_delete_rejects_journal_inode_swap(tmp_path: Path) -> None:
    parent_dirfd, journal, authority = _prepare_delete_case(tmp_path, "done")
    os.rename(
        tmp_path / "allocation.journal",
        tmp_path / "allocation.journal.original",
    )
    replacement = os.open(
        "allocation.journal",
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o600,
        dir_fd=parent_dirfd,
    )
    os.close(replacement)
    try:
        with pytest.raises(JournalError) as caught:
            journal.delete_at(authority)
        assert caught.value.code is JournalErrorCode.IDENTITY_DRIFT
        assert (tmp_path / "allocation.journal").exists()
    finally:
        journal.close()
        os.close(parent_dirfd)


def test_delete_and_absent_reconcile_reject_parent_fd_swap(tmp_path: Path) -> None:
    parent_dirfd, journal, authority = _prepare_delete_case(tmp_path, "done")
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    swapped = os.open(other, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    os.dup2(swapped, parent_dirfd)
    os.close(swapped)
    try:
        with pytest.raises(JournalError) as caught:
            journal.delete_at(authority)
        assert caught.value.code is JournalErrorCode.IDENTITY_DRIFT
        with pytest.raises(JournalError) as caught:
            journal.reconcile_absent_after_crash(authority)
        assert caught.value.code is JournalErrorCode.IDENTITY_DRIFT
    finally:
        journal.close()
        os.close(parent_dirfd)
