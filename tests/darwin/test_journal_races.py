from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from claude_sdk_proxy.journal import (
    Journal,
    JournalError,
    JournalErrorCode,
    RecordClass,
)
from claude_sdk_proxy.lifecycle import Record, StateKind

NORMAL_LIMIT = 64 * 1024
HARD_LIMIT = 96 * 1024


def _future() -> int:
    return time.monotonic_ns() + 5_000_000_000


def _fault_library() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "build/lib/libclaude_proxy_lifecycle_fault.dylib"
    )


def _make_journal(tmp_path: Path, *, fault: bool = False) -> tuple[Journal, int]:
    os.chmod(tmp_path, 0o700)
    parent_dirfd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    for suffix in (".append.lock", ".action.lock"):
        fd = os.open(
            "allocation.journal" + suffix,
            os.O_RDONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_dirfd,
        )
        os.close(fd)
    if fault:
        journal, _ = Journal._create_at_for_test(
            parent_dirfd,
            "allocation.journal",
            b"n" * 32,
            NORMAL_LIMIT,
            HARD_LIMIT,
            library_path=_fault_library(),
        )
    else:
        journal, _ = Journal.create_at(
            parent_dirfd,
            "allocation.journal",
            b"n" * 32,
            NORMAL_LIMIT,
            HARD_LIMIT,
        )
    return journal, parent_dirfd


def _open(parent_dirfd: int, *, fault: bool = False) -> Journal:
    if fault:
        return Journal._open_at_for_test(
            parent_dirfd,
            "allocation.journal",
            b"n" * 32,
            NORMAL_LIMIT,
            HARD_LIMIT,
            library_path=_fault_library(),
        )
    return Journal.open_at(
        parent_dirfd,
        "allocation.journal",
        b"n" * 32,
        NORMAL_LIMIT,
        HARD_LIMIT,
    )


def test_concurrent_sibling_appends_admit_exactly_one_child(tmp_path: Path) -> None:
    first, parent_dirfd = _make_journal(tmp_path)
    second = _open(parent_dirfd)
    parent = first.scan().head.hash
    barrier = threading.Barrier(3)
    successes: list[bytes] = []
    failures: list[JournalErrorCode] = []

    def append(journal: Journal, candidate: str) -> None:
        barrier.wait()
        try:
            successes.append(
                journal.append(
                    Record.prepared(
                        1,
                        candidate,
                        claim_deadline_ns=_future(),
                        parent=parent,
                    ),
                    RecordClass.NORMAL,
                ).hash
            )
        except JournalError as error:
            failures.append(error.code)

    threads = [
        threading.Thread(target=append, args=(first, "candidate-1")),
        threading.Thread(target=append, args=(second, "candidate-2")),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)
    try:
        assert len(successes) == 1
        assert failures == [JournalErrorCode.PARENT_MISMATCH]
        assert first.scan().head.hash == successes[0]
    finally:
        second.close()
        first.close()
        os.close(parent_dirfd)


def test_certification_retries_when_head_and_eof_change(tmp_path: Path) -> None:
    certifier, parent_dirfd = _make_journal(tmp_path, fault=True)
    appender = _open(parent_dirfd, fault=True)
    notified_read, notified_write = os.pipe()
    release_read, release_write = os.pipe()
    result: list[object] = []
    errors: list[BaseException] = []

    def certify() -> None:
        try:
            result.append(
                certifier.certify_head_with_pause_for_test(notified_write, release_read)
            )
        except BaseException as error:
            errors.append(error)
        finally:
            os.close(notified_write)
            os.close(release_read)

    thread = threading.Thread(target=certify)
    thread.start()
    released = False
    try:
        assert os.read(notified_read, 1) == b"1"
        appended = appender.append(
            Record.prepared(1, "candidate-1", claim_deadline_ns=_future()),
            RecordClass.NORMAL,
        )
        os.write(release_write, b"1")
        released = True
        os.close(release_write)
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert errors == []
        certified = result[0]
        assert certified.hash == appended.hash
        assert certified.physical_eof == certifier.scan().physical_eof
        assert certified.attempts >= 2
    finally:
        os.close(notified_read)
        if thread.is_alive():
            if not released:
                os.write(release_write, b"1")
            os.close(release_write)
            thread.join(timeout=2)
        appender.close()
        certifier.close()
        os.close(parent_dirfd)


def test_retirement_winning_before_batch_admission_blocks_batch(tmp_path: Path) -> None:
    journal, parent_dirfd = _make_journal(tmp_path, fault=True)
    lease = time.monotonic_ns() + 20_000_000
    try:
        journal.append(
            Record.prepared(1, "executor-1", claim_deadline_ns=_future()),
            RecordClass.NORMAL,
        )
        journal.activate_executor(
            1,
            "executor-1",
            os.getpid(),
            lease_deadline_ns=lease,
        )
        while time.monotonic_ns() <= journal.scan().head.record.lease_deadline_ns:
            time.sleep(0.005)
        retired = journal.retire_executor(
            authority="reconciler-1",
            authority_epoch=1,
            authority_deadline_ns=_future(),
        )

        with pytest.raises(JournalError) as caught:
            journal.admit_batch(
                1,
                "executor-1",
                "batch-1",
                [
                    journal.process_absent_descriptor(
                        journal.observe_process(os.getpid())
                    )
                ],
            )
        assert caught.value.code is JournalErrorCode.AUTHORITY
        assert journal.scan().head.hash == retired.hash
    finally:
        journal.close()
        os.close(parent_dirfd)


def test_retirement_preserves_admitted_batch_until_exact_outcome(
    tmp_path: Path,
) -> None:
    journal, parent_dirfd = _make_journal(tmp_path, fault=True)
    reconciler = _open(parent_dirfd, fault=True)
    target_pid = os.fork()
    if target_pid == 0:
        time.sleep(30)
        os._exit(0)
    try:
        target = journal.observe_process(target_pid)
        journal.append(
            Record.prepared(1, "executor-1", claim_deadline_ns=_future()),
            RecordClass.NORMAL,
        )
        journal.activate_executor(
            1,
            "executor-1",
            os.getpid(),
            lease_deadline_ns=_future(),
        )
        os.kill(target_pid, 9)
        admitted = journal.admit_batch(
            1,
            "executor-1",
            "batch-1",
            [
                journal.process_absent_descriptor(target),
                journal.reap_process_descriptor(target),
            ],
        )
        retired = reconciler.retire_executor(
            authority="reconciler-1",
            authority_epoch=1,
            authority_deadline_ns=_future(),
        )
        assert retired.record.kind is StateKind.RETIRING_BATCH
        assert retired.record.exact_batch == "batch-1"
        admitted.execute()
        idle = admitted.complete()
        assert idle.record.kind is StateKind.RETIRING_IDLE
        assert idle.record.exact_batch == "batch-1"
    finally:
        try:
            os.kill(target_pid, 9)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(target_pid, os.WNOHANG)
        except ChildProcessError:
            pass
        reconciler.close()
        journal.close()
        os.close(parent_dirfd)


def test_executor_death_interrupts_only_the_admitted_batch(tmp_path: Path) -> None:
    journal, parent_dirfd = _make_journal(tmp_path, fault=True)
    journal.append(
        Record.prepared(1, "executor-1", claim_deadline_ns=_future()),
        RecordClass.NORMAL,
    )
    notified_read, notified_write = os.pipe()
    executor_pid = os.fork()
    if executor_pid == 0:
        os.close(notified_read)
        journal.close()
        executor = _open(parent_dirfd, fault=True)
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
        executor.admit_batch(
            1,
            "executor-1",
            "batch-1",
            [executor.process_absent_descriptor(target)],
        )
        os.write(notified_write, str(target_pid).encode("ascii"))
        time.sleep(30)
        os._exit(0)

    os.close(notified_write)
    target_pid = 0
    try:
        target_pid = int(os.read(notified_read, 32).decode("ascii"))
        os.kill(executor_pid, 9)
        retired = journal.retire_executor(
            authority="reconciler-1",
            authority_epoch=1,
            authority_deadline_ns=_future(),
        )
        assert retired.record.kind is StateKind.RETIRING_BATCH
        proof = journal.confirm_executor_reaped(deadline_ns=_future())
        idle = journal.reconcile_interrupted_batch(proof)
        assert idle.record.kind is StateKind.RETIRING_IDLE
        next_generation = journal.prepare_successor(
            proof,
            2,
            "executor-2",
            claim_deadline_ns=_future(),
        )
        assert next_generation.record.generation == 2
        assert next_generation.record.inherited_batch == "batch-1"
    finally:
        os.close(notified_read)
        for pid in (executor_pid, target_pid):
            if pid <= 0:
                continue
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
        try:
            os.waitpid(executor_pid, os.WNOHANG)
        except ChildProcessError:
            pass
        journal.close()
        os.close(parent_dirfd)


def test_caller_authored_process_and_batch_proof_is_rejected(tmp_path: Path) -> None:
    journal, parent_dirfd = _make_journal(tmp_path, fault=True)
    future = time.monotonic_ns() + 5_000_000_000
    try:
        journal.append(
            Record.prepared(1, "executor-1", claim_deadline_ns=future),
            RecordClass.NORMAL,
        )
        with pytest.raises(JournalError) as caught:
            journal.append(
                Record.active_ready(
                    1,
                    "executor-1",
                    lease_deadline_ns=future,
                    completed_steps=15,
                    process_pid=0,
                ),
                RecordClass.NORMAL,
            )
        assert caught.value.code is JournalErrorCode.AUTHORITY
        assert journal.scan().head.record.kind is StateKind.PREPARED
    finally:
        journal.close()
        os.close(parent_dirfd)


def test_retirement_cannot_win_before_the_executor_lease_expires(
    tmp_path: Path,
) -> None:
    journal, parent_dirfd = _make_journal(tmp_path, fault=True)
    future = time.monotonic_ns() + 5_000_000_000
    try:
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
        with pytest.raises(JournalError) as caught:
            journal.retire_executor(
                authority="reconciler-1",
                authority_epoch=1,
                authority_deadline_ns=future,
            )
        assert caught.value.code is JournalErrorCode.AUTHORITY
        assert journal.scan().head.record.kind is StateKind.ACTIVE_READY
    finally:
        journal.close()
        os.close(parent_dirfd)


def test_only_native_lock_backed_batch_can_advance_cleanup_proofs(
    tmp_path: Path,
) -> None:
    journal, parent_dirfd = _make_journal(tmp_path, fault=True)
    future = time.monotonic_ns() + 5_000_000_000
    target_pid = os.fork()
    if target_pid == 0:
        time.sleep(30)
        os._exit(0)
    try:
        target_identity = journal.observe_process(target_pid)
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
        admitted = journal.admit_batch(
            1,
            "executor-1",
            "batch-1",
            [
                journal.process_absent_descriptor(target_identity),
                journal.reap_process_descriptor(target_identity),
            ],
        )
        admitted.execute()
        completed = admitted.complete()
        assert completed.record.completed_steps & 3 == 3
        with pytest.raises(JournalError) as caught:
            admitted.complete()
        assert caught.value.code is JournalErrorCode.BATCH_TOKEN
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
