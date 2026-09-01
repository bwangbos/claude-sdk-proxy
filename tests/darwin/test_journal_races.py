from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from claude_sdk_proxy.journal import (
    Journal,
    JournalError,
    JournalErrorCode,
    RecordClass,
)
from claude_sdk_proxy.lifecycle import (
    BatchOutcome,
    Record,
    StateKind,
)

NORMAL_LIMIT = 64 * 1024
HARD_LIMIT = 96 * 1024


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
                        claim_deadline_ns=100,
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
                certifier.certify_head_with_pause_for_test(
                    notified_write, release_read
                )
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
            Record.prepared(1, "candidate-1", claim_deadline_ns=100),
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
    journal, parent_dirfd = _make_journal(tmp_path)
    try:
        journal.append(
            Record.prepared(1, "executor-1", claim_deadline_ns=100),
            RecordClass.NORMAL,
        )
        journal.append(
            Record.active_ready(1, "executor-1", lease_deadline_ns=200),
            RecordClass.NORMAL,
        )
        active = journal.scan().head
        retired = journal.append(
            Record.retiring_idle(
                1,
                "executor-1",
                authority="reconciler-1",
                authority_epoch=1,
                deadline_ns=300,
                parent=active.hash,
            ),
            RecordClass.NORMAL,
        )

        with pytest.raises(JournalError) as caught:
            journal.append(
                Record.batch_active(
                    1,
                    "batch-1",
                    executor="executor-1",
                    lease_deadline_ns=200,
                    parent=active.hash,
                ),
                RecordClass.NORMAL,
            )
        assert caught.value.code is JournalErrorCode.PARENT_MISMATCH
        assert journal.scan().head.hash == retired.hash
    finally:
        journal.close()
        os.close(parent_dirfd)


def test_retirement_preserves_admitted_batch_until_exact_outcome(
    tmp_path: Path,
) -> None:
    journal, parent_dirfd = _make_journal(tmp_path)
    try:
        journal.append(
            Record.prepared(1, "executor-1", claim_deadline_ns=100),
            RecordClass.NORMAL,
        )
        journal.append(
            Record.active_ready(1, "executor-1", lease_deadline_ns=200),
            RecordClass.NORMAL,
        )
        journal.append(
            Record.batch_active(
                1,
                "batch-1",
                executor="executor-1",
                lease_deadline_ns=200,
            ),
            RecordClass.NORMAL,
        )
        retired = journal.append(
            Record.retiring_batch(
                1,
                "batch-1",
                prior_executor="executor-1",
                authority="reconciler-1",
                authority_epoch=1,
                deadline_ns=300,
            ),
            RecordClass.RECOVERY,
        )
        assert retired.record.exact_batch == "batch-1"

        with pytest.raises(JournalError) as caught:
            journal.append(
                Record.batch_done(
                    1,
                    "batch-2",
                    executor="executor-1",
                    lease_deadline_ns=200,
                    completed_steps=1,
                    batch_outcome=BatchOutcome.COMPLETED,
                ),
                RecordClass.RECOVERY,
            )
        assert caught.value.code is JournalErrorCode.ILLEGAL_TRANSITION

        idle = journal.append(
            Record.batch_done(
                1,
                "batch-1",
                executor="executor-1",
                lease_deadline_ns=200,
                completed_steps=1,
                batch_outcome=BatchOutcome.COMPLETED,
            ),
            RecordClass.RECOVERY,
        )
        assert idle.record.kind is StateKind.RETIRING_IDLE
        assert idle.record.exact_batch == "batch-1"
    finally:
        journal.close()
        os.close(parent_dirfd)


def test_executor_death_interrupts_only_the_admitted_batch(tmp_path: Path) -> None:
    journal, parent_dirfd = _make_journal(tmp_path)
    try:
        journal.append(
            Record.prepared(1, "executor-1", claim_deadline_ns=100),
            RecordClass.NORMAL,
        )
        journal.append(
            Record.active_ready(1, "executor-1", lease_deadline_ns=200),
            RecordClass.NORMAL,
        )
        journal.append(
            Record.batch_active(
                1,
                "batch-1",
                executor="executor-1",
                lease_deadline_ns=200,
            ),
            RecordClass.NORMAL,
        )
        journal.append(
            Record.retiring_batch(
                1,
                "batch-1",
                prior_executor="executor-1",
                authority="reconciler-1",
                authority_epoch=1,
                deadline_ns=300,
            ),
            RecordClass.RECOVERY,
        )
        idle = journal.append(
            Record.batch_done(
                1,
                "batch-1",
                executor="executor-1",
                lease_deadline_ns=200,
                completed_steps=0,
                batch_outcome=BatchOutcome.INTERRUPTED,
            ),
            RecordClass.RECOVERY,
        )

        assert idle.record.kind is StateKind.RETIRING_IDLE
        assert idle.record.batch_outcome is BatchOutcome.INTERRUPTED
        next_generation = journal.append(
            Record.prepared(2, "executor-2", claim_deadline_ns=400),
            RecordClass.RECOVERY,
        )
        assert next_generation.record.generation == 2
        assert next_generation.record.inherited_batch == "batch-1"
    finally:
        journal.close()
        os.close(parent_dirfd)
