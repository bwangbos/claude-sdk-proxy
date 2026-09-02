from __future__ import annotations

import os
import select
import threading
import time
from pathlib import Path

import pytest

from claude_sdk_proxy.journal import (
    Journal,
    JournalDeleteReceipt,
    JournalError,
    JournalErrorCode,
    RecordClass,
    UnreleasedPartialCreate,
)
from claude_sdk_proxy.lifecycle import Record, StateKind

NORMAL_LIMIT = 64 * 1024
PHYSICAL_RECORD_SIZE = 108 + 1064
MAX_RECOVERY_CLEANUP_BATCHES = 4
RECOVERY_RECORD_COUNT = 5 + (2 * MAX_RECOVERY_CLEANUP_BATCHES) + 1
RECOVERY_BYTES = PHYSICAL_RECORD_SIZE * RECOVERY_RECORD_COUNT
HARD_LIMIT = NORMAL_LIMIT + RECOVERY_BYTES
CREATE_POINTS = (
    "after_openat",
    "after_preallocate",
    "after_intent_append",
    "after_journal_fullfsync",
    "after_intent_parent_fsync",
)
CREATE_DEADLINE_POINTS = (
    "before_create_openat",
    "before_create_preallocate",
    "before_create_intent_write",
    "before_create_fullfsync",
    "before_create_parent_fsync",
)
PARTIAL_DELETE_POINTS = (
    "after_authority_revalidated",
    "after_workdir_absence_verified",
    "after_journal_unlinkat",
    "after_journal_unlink_parent_fsync",
)
DONE_DELETE_POINTS = (
    "after_authority_revalidated",
    "after_process_absence_verified",
    "after_workdir_absence_verified",
    "after_journal_unlinkat",
    "after_journal_unlink_parent_fsync",
)


def _future() -> int:
    return time.monotonic_ns() + 5_000_000_000


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


@pytest.mark.parametrize("point", CREATE_DEADLINE_POINTS)
def test_create_expiry_at_final_boundary_stops_next_mutation(
    tmp_path: Path,
    point: str,
) -> None:
    parent_dirfd = _open_parent(tmp_path)
    _make_lock_files(parent_dirfd, "allocation.journal")
    notified_read, notified_write = os.pipe()
    release_read, release_write = os.pipe()
    deadline = time.monotonic_ns() + 200_000_000
    Journal.configure_create_pause_for_test(
        _fault_library(), point, notified_write, release_read, deadline
    )
    results: list[object] = []

    def create() -> None:
        try:
            results.append(_create(parent_dirfd, library_path=_fault_library()))
        except BaseException as error:
            results.append(error)

    thread = threading.Thread(target=create)
    thread.start()
    try:
        assert os.read(notified_read, 1) == b"1"
        while time.monotonic_ns() <= deadline:
            time.sleep(0.001)
        os.write(release_write, b"1")
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert len(results) == 1
        assert isinstance(results[0], JournalError)
        assert results[0].code is JournalErrorCode.LOCK_TIMEOUT
        journal_path = tmp_path / "allocation.journal"
        if point == "before_create_openat":
            assert not journal_path.exists()
        elif point in {
            "before_create_preallocate",
            "before_create_intent_write",
        }:
            assert journal_path.stat().st_size == 0
        else:
            assert journal_path.stat().st_size == PHYSICAL_RECORD_SIZE
    finally:
        os.close(notified_read)
        os.close(notified_write)
        os.close(release_read)
        os.close(release_write)
        for result in results:
            if isinstance(result, Journal):
                result.close()
        os.close(parent_dirfd)


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
                authority = reconciled.certify_unreleased_partial_create()
                assert isinstance(authority, UnreleasedPartialCreate)
                assert reconciled.scan().head.record.no_dependent_artifact is True
        finally:
            reconciled.close()
    finally:
        os.close(parent_dirfd)


def _prepare_delete_case(
    tmp_path: Path,
    authority_kind: str = "partial",
) -> tuple[int, Journal, UnreleasedPartialCreate]:
    assert authority_kind == "partial"
    parent_dirfd = _open_parent(tmp_path)
    _make_lock_files(parent_dirfd, "allocation.journal")
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
    "point",
    PARTIAL_DELETE_POINTS,
)
def test_delete_crash_releases_slot_only_after_fresh_absent_reconciliation(
    tmp_path: Path,
    point: str,
) -> None:
    parent_dirfd, retained, authority = _prepare_delete_case(tmp_path)
    pid = os.fork()
    if pid == 0:
        os.environ["CPL_FAULT_POINT"] = point
        # The native at-fork hook already invalidated and closed this handle.
        child = _open(parent_dirfd, library_path=_fault_library())
        child_authority = child.certify_unreleased_partial_create()
        child.delete_at(child_authority)
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
            assert receipt == JournalDeleteReceipt(journal_unlink_parent_dirsynced=True)
            assert receipt.slot_releasable is True
            assert retained.closed is True
    finally:
        retained.close()
        os.close(parent_dirfd)


@pytest.mark.parametrize("point", DONE_DELETE_POINTS)
def test_certified_done_delete_crash_never_returns_slot_receipt(
    tmp_path: Path,
    point: str,
) -> None:
    parent_dirfd = _open_parent(tmp_path)
    _make_lock_files(parent_dirfd, "allocation.journal")
    worker_pid = os.fork()
    if worker_pid == 0:
        journal, create_receipt = Journal._create_at_for_test(
            parent_dirfd,
            "allocation.journal",
            b"n" * 32,
            NORMAL_LIMIT,
            HARD_LIMIT,
            workdir_parent_dirfd=parent_dirfd,
            workdir_name="allocation.workdir",
            library_path=_fault_library(),
        )
        journal.create_workdir(create_receipt)
        journal.append(
            Record.prepared(1, "executor-1", claim_deadline_ns=_future()),
            RecordClass.NORMAL,
        )
        ready_read, ready_write = os.pipe()
        executor_pid = os.fork()
        if executor_pid == 0:
            os.close(ready_read)
            # The native at-fork hook already invalidated and closed this handle.
            executor = _open(parent_dirfd, library_path=_fault_library())
            executor.activate_executor(
                1,
                "executor-1",
                os.getpid(),
                lease_deadline_ns=_future(),
            )
            target_pid = os.fork()
            if target_pid == 0:
                time.sleep(30)
                os._exit(0)
            target = executor.observe_process(target_pid)
            os.kill(target_pid, 9)
            batch = executor.admit_batch(
                1,
                "executor-1",
                "cleanup-1",
                [
                    executor.process_absent_descriptor(target),
                    executor.reap_process_descriptor(target),
                    executor.remove_bound_workdir_descriptor(),
                    executor.terminal_checks_descriptor(),
                ],
            )
            batch.execute()
            batch.complete()
            executor.finish_done(1, "executor-1")
            os.write(ready_write, b"1")
            os.close(ready_write)
            executor.close()
            os._exit(0)
        os.close(ready_write)
        if os.read(ready_read, 1) != b"1":
            os._exit(92)
        os.close(ready_read)
        journal.confirm_executor_reaped(deadline_ns=_future())
        authority = journal.certify_done()
        os.environ["CPL_FAULT_POINT"] = point
        journal.delete_at(authority)
        marker = os.open(
            "slot-released",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_dirfd,
        )
        os.close(marker)
        os._exit(0)

    _, status = os.waitpid(worker_pid, 0)
    try:
        assert os.waitstatus_to_exitcode(status) == 91
        assert not (tmp_path / "slot-released").exists()
        unlinked = point in {
            "after_journal_unlinkat",
            "after_journal_unlink_parent_fsync",
        }
        assert (tmp_path / "allocation.journal").exists() is not unlinked
        assert not (tmp_path / "allocation.workdir").exists()
    finally:
        os.close(parent_dirfd)


def test_reconcile_absent_rejects_fabricated_authority(tmp_path: Path) -> None:
    parent_dirfd, retained, authority = _prepare_delete_case(tmp_path)
    child = _open(parent_dirfd)
    child_authority = child.certify_unreleased_partial_create()
    child.delete_at(child_authority)
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
    journal = _create(parent_dirfd, library_path=_fault_library())
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
        assert caught.value.code is JournalErrorCode.REAP_REQUIRED
    finally:
        journal.close()
        os.close(parent_dirfd)


def test_done_authority_denies_live_executor_and_missing_reap(
    tmp_path: Path,
) -> None:
    parent_dirfd = _open_parent(tmp_path)
    _make_lock_files(parent_dirfd, "allocation.journal")
    journal, create_receipt = Journal._create_at_for_test(
        parent_dirfd,
        "allocation.journal",
        b"n" * 32,
        NORMAL_LIMIT,
        HARD_LIMIT,
        workdir_parent_dirfd=parent_dirfd,
        workdir_name="allocation.workdir",
        library_path=_fault_library(),
    )
    journal.create_workdir(create_receipt)
    journal.append(
        Record.prepared(1, "executor-1", claim_deadline_ns=_future()),
        RecordClass.NORMAL,
    )
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    executor_pid = os.fork()
    if executor_pid == 0:
        os.close(ready_read)
        os.close(release_write)
        # The native at-fork hook already invalidated and closed this handle.
        executor = _open(parent_dirfd, library_path=_fault_library())
        executor.activate_executor(
            1,
            "executor-1",
            os.getpid(),
            lease_deadline_ns=_future(),
        )
        target_pid = os.fork()
        if target_pid == 0:
            time.sleep(30)
            os._exit(0)
        target = executor.observe_process(target_pid)
        os.kill(target_pid, 9)
        batch = executor.admit_batch(
            1,
            "executor-1",
            "cleanup-1",
            [
                executor.process_absent_descriptor(target),
                executor.reap_process_descriptor(target),
                executor.remove_bound_workdir_descriptor(),
                executor.terminal_checks_descriptor(),
            ],
        )
        batch.execute()
        batch.complete()
        executor.finish_done(1, "executor-1")
        os.write(ready_write, b"1")
        os.read(release_read, 1)
        executor.close()
        os._exit(0)

    os.close(ready_write)
    os.close(release_read)
    try:
        assert os.read(ready_read, 1) == b"1"
        with pytest.raises(JournalError) as caught:
            journal.confirm_executor_reaped(
                deadline_ns=time.monotonic_ns() + 20_000_000
            )
        assert caught.value.code is JournalErrorCode.REAP_REQUIRED
        with pytest.raises(JournalError) as caught:
            journal.certify_done()
        assert caught.value.code is JournalErrorCode.REAP_REQUIRED
        os.write(release_write, b"1")
        proof = journal.confirm_executor_reaped(deadline_ns=_future())
        assert proof.pid == executor_pid
        assert journal.certify_done()
    finally:
        os.close(ready_read)
        os.close(release_write)
        try:
            os.kill(executor_pid, 9)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(executor_pid, os.WNOHANG)
        except ChildProcessError:
            pass
        journal.close()
        os.close(parent_dirfd)


def test_delete_rejects_wrong_kind_or_wrong_hash_authority(tmp_path: Path) -> None:
    parent_dirfd, journal, authority = _prepare_delete_case(tmp_path)
    try:
        for fabricated in (
            authority._fabricate_for_test(kind=1),
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
    parent_dirfd, journal, authority = _prepare_delete_case(tmp_path)
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
    parent_dirfd, journal, authority = _prepare_delete_case(tmp_path)
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
    parent_dirfd, journal, authority = _prepare_delete_case(tmp_path)
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


def test_controlled_create_gate_requires_durable_bound_workdir_receipt(
    tmp_path: Path,
) -> None:
    parent_dirfd = _open_parent(tmp_path)
    _make_lock_files(parent_dirfd, "allocation.journal")
    journal, create_receipt = Journal._create_at_for_test(
        parent_dirfd,
        "allocation.journal",
        b"n" * 32,
        NORMAL_LIMIT,
        HARD_LIMIT,
        workdir_parent_dirfd=parent_dirfd,
        workdir_name="allocation.workdir",
        library_path=_fault_library(),
    )
    try:
        assert (
            journal.try_gated_marker_for_test(
                "spawn",
                None,
                parent_dirfd,
                "spawned",
            )
            is False
        )
        assert not (tmp_path / "spawned").exists()

        bound_receipt = journal.create_workdir(create_receipt)
        assert (tmp_path / "allocation.workdir").is_dir()
        assert journal.scan().head.record.workdir_bound is True
        assert journal.try_gated_marker_for_test(
            "spawn",
            bound_receipt,
            parent_dirfd,
            "spawned",
        )
        assert (tmp_path / "spawned").exists()
    finally:
        journal.close()
        os.close(parent_dirfd)


def test_workdir_name_must_still_resolve_to_opened_inode_before_bound_append(
    tmp_path: Path,
) -> None:
    parent_dirfd = _open_parent(tmp_path)
    _make_lock_files(parent_dirfd, "allocation.journal")
    journal, create_receipt = Journal._create_at_for_test(
        parent_dirfd,
        "allocation.journal",
        b"n" * 32,
        NORMAL_LIMIT,
        HARD_LIMIT,
        workdir_parent_dirfd=parent_dirfd,
        workdir_name="allocation.workdir",
        library_path=_fault_library(),
    )
    notified_read, notified_write = os.pipe()
    release_read, release_write = os.pipe()
    journal.configure_lifecycle_pause_for_test(
        "before_workdir_bound_append",
        notified_write,
        release_read,
    )
    results: list[object] = []

    def create_workdir() -> None:
        try:
            results.append(journal.create_workdir(create_receipt))
        except BaseException as error:
            results.append(error)

    thread = threading.Thread(target=create_workdir)
    thread.start()
    try:
        assert os.read(notified_read, 1) == b"1"
        os.rename(
            tmp_path / "allocation.workdir",
            tmp_path / "allocation.workdir.original",
        )
        (tmp_path / "allocation.workdir").mkdir(mode=0o700)
        os.write(release_write, b"1")
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert isinstance(results[0], JournalError)
        assert results[0].code is JournalErrorCode.IDENTITY_DRIFT
        assert journal.scan().head.record.workdir_bound is False
    finally:
        os.close(notified_read)
        os.close(notified_write)
        os.close(release_read)
        os.close(release_write)
        journal.close()
        os.close(parent_dirfd)


def test_dependent_artifact_inserted_between_absence_scans_blocks_authority(
    tmp_path: Path,
) -> None:
    parent_dirfd = _open_parent(tmp_path)
    _make_lock_files(parent_dirfd, "allocation.journal")
    journal_fd = os.open(
        "allocation.journal",
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_CLOEXEC,
        0o600,
        dir_fd=parent_dirfd,
    )
    os.close(journal_fd)
    journal = _open(parent_dirfd, library_path=_fault_library())
    notified_read, notified_write = os.pipe()
    release_read, release_write = os.pipe()
    journal.configure_lifecycle_pause_for_test(
        "after_first_dependent_scan",
        notified_write,
        release_read,
    )
    results: list[object] = []

    def certify() -> None:
        try:
            results.append(journal.certify_unreleased_partial_create())
        except BaseException as error:
            results.append(error)

    thread = threading.Thread(target=certify)
    thread.start()
    try:
        assert os.read(notified_read, 1) == b"1"
        (tmp_path / "allocation.spawn").write_bytes(b"")
        os.write(release_write, b"1")
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert len(results) == 1
        assert isinstance(results[0], JournalError)
        assert results[0].code is JournalErrorCode.WORKDIR_PRESENT
        assert (tmp_path / "allocation.journal").exists()
    finally:
        os.close(notified_read)
        os.close(notified_write)
        os.close(release_read)
        os.close(release_write)
        journal.close()
        os.close(parent_dirfd)


def test_controlled_cleanup_slot_gate_consumes_only_native_delete_receipt(
    tmp_path: Path,
) -> None:
    parent_dirfd = _open_parent(tmp_path)
    _make_lock_files(parent_dirfd, "allocation.journal")
    fd = os.open(
        "allocation.journal",
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_CLOEXEC,
        0o600,
        dir_fd=parent_dirfd,
    )
    os.close(fd)
    journal = _open(parent_dirfd, library_path=_fault_library())
    try:
        assert not Journal.try_cleanup_slot_marker_for_test(
            _fault_library(),
            JournalDeleteReceipt(journal_unlink_parent_dirsynced=False),
            parent_dirfd,
            "slot-released",
        )
        assert not (tmp_path / "slot-released").exists()
        authority = journal.certify_unreleased_partial_create()
        receipt = journal.delete_at(authority)
        assert Journal.try_cleanup_slot_marker_for_test(
            _fault_library(),
            receipt,
            parent_dirfd,
            "slot-released",
        )
        assert (tmp_path / "slot-released").exists()
    finally:
        journal.close()
        os.close(parent_dirfd)


def test_real_executor_identity_batches_reap_and_done_authority(
    tmp_path: Path,
) -> None:
    parent_dirfd = _open_parent(tmp_path)
    _make_lock_files(parent_dirfd, "allocation.journal")
    journal, create_receipt = Journal._create_at_for_test(
        parent_dirfd,
        "allocation.journal",
        b"n" * 32,
        NORMAL_LIMIT,
        HARD_LIMIT,
        workdir_parent_dirfd=parent_dirfd,
        workdir_name="allocation.workdir",
        library_path=_fault_library(),
    )
    bound = journal.create_workdir(create_receipt)
    assert bound.workdir_parent_dirsynced is True
    future = time.monotonic_ns() + 5_000_000_000
    journal.append(
        Record.prepared(1, "executor-1", claim_deadline_ns=future),
        RecordClass.NORMAL,
    )
    ready_read, ready_write = os.pipe()
    executor_pid = os.fork()
    if executor_pid == 0:
        os.close(ready_read)
        # The native at-fork hook already invalidated and closed this handle.
        executor = _open(parent_dirfd, library_path=_fault_library())
        executor.activate_executor(
            1,
            "executor-1",
            os.getpid(),
            lease_deadline_ns=future,
        )
        target_pid = os.fork()
        if target_pid == 0:
            time.sleep(30)
            os._exit(0)
        target = executor.observe_process(target_pid)
        os.kill(target_pid, 9)
        batch = executor.admit_batch(
            1,
            "executor-1",
            "cleanup-1",
            [
                executor.process_absent_descriptor(target),
                executor.reap_process_descriptor(target),
                executor.remove_bound_workdir_descriptor(),
                executor.terminal_checks_descriptor(),
            ],
        )
        batch.execute()
        batch.complete()
        executor.finish_done(1, "executor-1")
        os.write(ready_write, b"1")
        os.close(ready_write)
        executor.close()
        os._exit(0)

    os.close(ready_write)
    try:
        readable, _, _ = select.select([ready_read], [], [], 5.0)
        assert readable == [ready_read]
        assert os.read(ready_read, 1) == b"1"
        proof = journal.confirm_executor_reaped(deadline_ns=future)
        assert proof.pid == executor_pid
        authority = journal.certify_done()
        receipt = journal.delete_at(authority)
        assert receipt.slot_releasable is True
        assert not (tmp_path / "allocation.workdir").exists()
        assert not (tmp_path / "allocation.journal").exists()
    finally:
        os.close(ready_read)
        try:
            os.kill(executor_pid, 9)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(executor_pid, os.WNOHANG)
        except ChildProcessError:
            pass
        journal.close()
        os.close(parent_dirfd)


def test_batch_retry_skips_already_completed_waitpid_step(tmp_path: Path) -> None:
    parent_dirfd = _open_parent(tmp_path)
    _make_lock_files(parent_dirfd, "allocation.journal")
    journal, create_receipt = Journal._create_at_for_test(
        parent_dirfd,
        "allocation.journal",
        b"n" * 32,
        NORMAL_LIMIT,
        HARD_LIMIT,
        workdir_parent_dirfd=parent_dirfd,
        workdir_name="allocation.workdir",
        library_path=_fault_library(),
    )
    journal.create_workdir(create_receipt)
    future = time.monotonic_ns() + 5_000_000_000
    target_pid = os.fork()
    if target_pid == 0:
        time.sleep(30)
        os._exit(0)
    try:
        target = journal.observe_process(target_pid)
        journal.append(
            Record.prepared(1, "executor-1", claim_deadline_ns=future),
            RecordClass.NORMAL,
        )
        journal.activate_executor(
            1,
            "executor-1",
            os.getpid(),
            lease_deadline_ns=future,
        )
        os.kill(target_pid, 9)
        batch = journal.admit_batch(
            1,
            "executor-1",
            "batch-1",
            [
                journal.process_absent_descriptor(target),
                journal.reap_process_descriptor(target),
            ],
        )
        journal.fail_batch_after_step_for_test(2)
        with pytest.raises(JournalError) as caught:
            batch.execute()
        assert caught.value.code is JournalErrorCode.SYSTEM
        assert batch.execute() & 3 == 3
        assert batch.complete().record.completed_steps & 3 == 3
    finally:
        try:
            os.kill(target_pid, 9)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(target_pid, os.WNOHANG)
        except ChildProcessError:
            pass
        journal.close()
        os.close(parent_dirfd)


def test_batch_retry_reconciles_unlink_with_fresh_parent_fsync(
    tmp_path: Path,
) -> None:
    parent_dirfd = _open_parent(tmp_path)
    _make_lock_files(parent_dirfd, "allocation.journal")
    journal, create_receipt = Journal._create_at_for_test(
        parent_dirfd,
        "allocation.journal",
        b"n" * 32,
        NORMAL_LIMIT,
        HARD_LIMIT,
        workdir_parent_dirfd=parent_dirfd,
        workdir_name="allocation.workdir",
        library_path=_fault_library(),
    )
    journal.create_workdir(create_receipt)
    future = time.monotonic_ns() + 5_000_000_000
    target_pid = os.fork()
    if target_pid == 0:
        time.sleep(30)
        os._exit(0)
    try:
        target = journal.observe_process(target_pid)
        journal.append(
            Record.prepared(1, "executor-1", claim_deadline_ns=future),
            RecordClass.NORMAL,
        )
        journal.activate_executor(
            1,
            "executor-1",
            os.getpid(),
            lease_deadline_ns=future,
        )
        os.kill(target_pid, 9)
        batch = journal.admit_batch(
            1,
            "executor-1",
            "batch-1",
            [
                journal.process_absent_descriptor(target),
                journal.reap_process_descriptor(target),
                journal.remove_bound_workdir_descriptor(),
                journal.terminal_checks_descriptor(),
            ],
        )
        journal.fail_next_workdir_parent_fsync_for_test()
        with pytest.raises(JournalError) as caught:
            batch.execute()
        assert caught.value.code is JournalErrorCode.SYSTEM
        assert not (tmp_path / "allocation.workdir").exists()

        assert batch.execute() == 15
        assert batch.complete().record.completed_steps == 15
    finally:
        try:
            os.kill(target_pid, 9)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(target_pid, os.WNOHANG)
        except ChildProcessError:
            pass
        journal.close()
        os.close(parent_dirfd)


def test_exact_recovery_tail_completes_four_batches_and_rejects_fifth(
    tmp_path: Path,
) -> None:
    normal_limit = PHYSICAL_RECORD_SIZE * 12
    hard_limit = normal_limit + RECOVERY_BYTES
    parent_dirfd = _open_parent(tmp_path)
    _make_lock_files(parent_dirfd, "allocation.journal")
    journal, create_receipt = Journal._create_at_for_test(
        parent_dirfd,
        "allocation.journal",
        b"n" * 32,
        normal_limit,
        hard_limit,
        workdir_parent_dirfd=parent_dirfd,
        workdir_name="allocation.workdir",
        library_path=_fault_library(),
    )
    journal.create_workdir(create_receipt)
    first_target_pid = os.fork()
    if first_target_pid == 0:
        time.sleep(30)
        os._exit(0)
    first_target = journal.observe_process(first_target_pid)
    lease = time.monotonic_ns() + 200_000_000
    journal.append(
        Record.prepared(1, "executor-1", claim_deadline_ns=_future()),
        RecordClass.NORMAL,
    )
    admitted_read, admitted_write = os.pipe()
    executor_pid = os.fork()
    if executor_pid == 0:
        os.close(admitted_read)
        # The native at-fork hook already invalidated and closed this handle.
        executor = Journal._open_at_for_test(
            parent_dirfd,
            "allocation.journal",
            b"n" * 32,
            normal_limit,
            hard_limit,
            workdir_parent_dirfd=parent_dirfd,
            workdir_name="allocation.workdir",
            library_path=_fault_library(),
        )
        executor.activate_executor(
            1,
            "executor-1",
            os.getpid(),
            lease_deadline_ns=lease,
        )
        os.kill(first_target_pid, 9)
        executor.admit_batch(
            1,
            "executor-1",
            "interrupted-1",
            [executor.process_absent_descriptor(first_target)],
        )
        os.write(admitted_write, b"1")
        os.close(admitted_write)
        time.sleep(30)
        os._exit(0)

    os.close(admitted_write)
    second_target_pid = 0
    try:
        assert os.read(admitted_read, 1) == b"1"
        physical_eof = journal.scan().physical_eof
        journal.raw_append_for_test(b"x" * (normal_limit - physical_eof))
        assert journal.scan().physical_eof == normal_limit
        while time.monotonic_ns() <= lease:
            time.sleep(0.001)
        authority_deadline = time.monotonic_ns() + 40_000_000
        assert journal.retire_executor(
            authority="reconciler-1",
            authority_epoch=1,
            authority_deadline_ns=authority_deadline,
        ).record.kind is StateKind.RETIRING_BATCH
        os.kill(executor_pid, 9)
        while time.monotonic_ns() <= authority_deadline:
            time.sleep(0.001)
        replacement_deadline = time.monotonic_ns() + 40_000_000
        journal.replace_retirement_authority(
            authority="reconciler-2",
            authority_epoch=2,
            authority_deadline_ns=replacement_deadline,
        )
        replacement_eof = journal.scan().physical_eof
        while time.monotonic_ns() <= replacement_deadline:
            time.sleep(0.001)
        with pytest.raises(JournalError) as caught:
            journal.replace_retirement_authority(
                authority="reconciler-3",
                authority_epoch=3,
                authority_deadline_ns=_future(),
            )
        assert caught.value.code is JournalErrorCode.AUTHORITY
        assert journal.scan().physical_eof == replacement_eof
        proof = journal.confirm_executor_reaped(deadline_ns=_future())
        journal.reconcile_interrupted_batch(proof)
        claim_deadline = time.monotonic_ns() + 200_000_000
        notified_read, notified_write = os.pipe()
        release_read, release_write = os.pipe()
        journal.configure_lifecycle_pause_for_test(
            "before_successor_append", notified_write, release_read
        )
        successor_results: list[object] = []

        def prepare_expiring_successor() -> None:
            try:
                successor_results.append(
                    journal.prepare_successor(
                        proof,
                        2,
                        "executor-2",
                        claim_deadline_ns=claim_deadline,
                    )
                )
            except BaseException as error:
                successor_results.append(error)

        successor_thread = threading.Thread(target=prepare_expiring_successor)
        successor_thread.start()
        assert os.read(notified_read, 1) == b"1"
        while time.monotonic_ns() <= claim_deadline:
            time.sleep(0.001)
        os.write(release_write, b"1")
        successor_thread.join(timeout=2)
        assert not successor_thread.is_alive()
        assert len(successor_results) == 1
        assert isinstance(successor_results[0], JournalError)
        assert successor_results[0].code is JournalErrorCode.AUTHORITY
        os.close(notified_read)
        os.close(notified_write)
        os.close(release_read)
        os.close(release_write)
        journal.prepare_successor(
            proof,
            2,
            "executor-2",
            claim_deadline_ns=_future(),
        )
        journal.activate_executor(
            2,
            "executor-2",
            os.getpid(),
            lease_deadline_ns=_future(),
        )

        second_target_pid = os.fork()
        if second_target_pid == 0:
            time.sleep(30)
            os._exit(0)
        second_target = journal.observe_process(second_target_pid)
        os.kill(second_target_pid, 9)
        cleanup_descriptors = (
            journal.process_absent_descriptor(second_target),
            journal.reap_process_descriptor(second_target),
            journal.remove_bound_workdir_descriptor(),
            journal.terminal_checks_descriptor(),
        )
        for index, descriptor in enumerate(cleanup_descriptors, start=1):
            cleanup = journal.admit_batch(
                2,
                "executor-2",
                f"cleanup-{index}",
                [descriptor],
            )
            assert cleanup.execute() == (1 << index) - 1
            assert cleanup.complete().record.completed_steps == (1 << index) - 1

        before_rejected_batch = journal.scan()
        with pytest.raises(JournalError) as caught:
            journal.admit_batch(
                2,
                "executor-2",
                "cleanup-5",
                [journal.terminal_checks_descriptor()],
            )
        assert caught.value.code is JournalErrorCode.PRECONDITION
        after_rejected_batch = journal.scan()
        assert after_rejected_batch.physical_eof == before_rejected_batch.physical_eof
        assert after_rejected_batch.head.hash == before_rejected_batch.head.hash
        done = journal.finish_done(2, "executor-2")
        assert done.record.kind is StateKind.DONE
        assert journal.scan().physical_eof == hard_limit
    finally:
        os.close(admitted_read)
        for pid in (executor_pid, first_target_pid, second_target_pid):
            if pid <= 0:
                continue
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
        for pid in (executor_pid, first_target_pid, second_target_pid):
            if pid <= 0:
                continue
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
        journal.close()
        os.close(parent_dirfd)
