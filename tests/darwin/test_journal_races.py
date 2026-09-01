from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from claude_sdk_proxy.journal import (
    AdmittedBatch,
    Journal,
    JournalError,
    JournalErrorCode,
    RecordClass,
)
from claude_sdk_proxy.lifecycle import Record, StateKind

NORMAL_LIMIT = 64 * 1024
PHYSICAL_RECORD_SIZE = 108 + 1064
RECOVERY_RECORD_COUNT = 14
HARD_LIMIT = NORMAL_LIMIT + PHYSICAL_RECORD_SIZE * RECOVERY_RECORD_COUNT


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
        journal, receipt = Journal._create_at_for_test(
            parent_dirfd,
            "allocation.journal",
            b"n" * 32,
            NORMAL_LIMIT,
            HARD_LIMIT,
            library_path=_fault_library(),
        )
    else:
        journal, receipt = Journal.create_at(
            parent_dirfd,
            "allocation.journal",
            b"n" * 32,
            NORMAL_LIMIT,
            HARD_LIMIT,
        )
    journal.create_workdir(receipt)
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


def _admit_absence_batch(journal: Journal) -> tuple[AdmittedBatch, int]:
    future = _future()
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
    target_pid = os.fork()
    if target_pid == 0:
        time.sleep(30)
        os._exit(0)
    target = journal.observe_process(target_pid)
    os.kill(target_pid, 9)
    batch = journal.admit_batch(
        1,
        "executor-1",
        "batch-1",
        [journal.process_absent_descriptor(target)],
    )
    return batch, target_pid


def _reap_target(pid: int) -> None:
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def test_cross_thread_close_waits_for_admitted_batch_completion(
    tmp_path: Path,
) -> None:
    journal, parent_dirfd = _make_journal(tmp_path, fault=True)
    batch, target_pid = _admit_absence_batch(journal)
    close_errors: list[BaseException] = []

    def close() -> None:
        try:
            journal.close()
        except BaseException as error:
            close_errors.append(error)

    closer = threading.Thread(target=close)
    closer.start()
    try:
        closer.join(timeout=0.05)
        assert closer.is_alive()
        assert batch.execute() == 1
        assert batch.complete().record.completed_steps == 1
        closer.join(timeout=2)
        assert not closer.is_alive()
        assert close_errors == []
        assert journal.closed is True
    finally:
        _reap_target(target_pid)
        journal.close()
        os.close(parent_dirfd)


def test_owner_close_with_admitted_batch_fails_without_deadlock(
    tmp_path: Path,
) -> None:
    journal, parent_dirfd = _make_journal(tmp_path, fault=True)
    batch, target_pid = _admit_absence_batch(journal)
    try:
        started = time.monotonic_ns()
        with pytest.raises(JournalError) as caught:
            journal.close()
        assert caught.value.code is JournalErrorCode.BATCH_TOKEN
        assert time.monotonic_ns() - started < 100_000_000
        assert journal.closed is False
        batch.abandon()
        journal.close()
        assert journal.closed is True
    finally:
        _reap_target(target_pid)
        journal.close()
        os.close(parent_dirfd)


def test_wrong_thread_completion_does_not_consume_admitted_batch(
    tmp_path: Path,
) -> None:
    journal, parent_dirfd = _make_journal(tmp_path, fault=True)
    batch, target_pid = _admit_absence_batch(journal)
    results: list[object] = []
    try:
        assert batch.execute() == 1

        def complete() -> None:
            try:
                results.append(batch.complete())
            except BaseException as error:
                results.append(error)

        wrong_thread = threading.Thread(target=complete)
        wrong_thread.start()
        wrong_thread.join(timeout=2)
        assert not wrong_thread.is_alive()
        assert len(results) == 1
        assert isinstance(results[0], JournalError)
        assert results[0].code is JournalErrorCode.BATCH_TOKEN
        assert batch.complete().record.completed_steps == 1
    finally:
        _reap_target(target_pid)
        journal.close()
        os.close(parent_dirfd)


def test_owner_abandon_invalidates_batch_once_and_releases_waiting_close(
    tmp_path: Path,
) -> None:
    journal, parent_dirfd = _make_journal(tmp_path, fault=True)
    batch, target_pid = _admit_absence_batch(journal)
    close_errors: list[BaseException] = []

    def close() -> None:
        try:
            journal.close()
        except BaseException as error:
            close_errors.append(error)

    closer = threading.Thread(target=close)
    closer.start()
    try:
        closer.join(timeout=0.05)
        assert closer.is_alive()
        batch.abandon()
        with pytest.raises(JournalError) as caught:
            batch.abandon()
        assert caught.value.code is JournalErrorCode.BATCH_TOKEN
        with pytest.raises(JournalError) as caught:
            batch.complete()
        assert caught.value.code is JournalErrorCode.BATCH_TOKEN
        closer.join(timeout=2)
        assert not closer.is_alive()
        assert close_errors == []
        assert journal.closed is True
    finally:
        _reap_target(target_pid)
        journal.close()
        os.close(parent_dirfd)


def test_abandon_rejects_unrecorded_batch_effects_and_preserves_completion(
    tmp_path: Path,
) -> None:
    journal, parent_dirfd = _make_journal(tmp_path, fault=True)
    batch, target_pid = _admit_absence_batch(journal)
    try:
        assert batch.execute() == 1
        with pytest.raises(JournalError) as caught:
            batch.abandon()
        assert caught.value.code is JournalErrorCode.PRECONDITION
        assert batch.complete().record.completed_steps == 1
    finally:
        _reap_target(target_pid)
        journal.close()
        os.close(parent_dirfd)


@pytest.mark.parametrize(
    "pause_point",
    ["before_batch_completion_append", "after_batch_completion_append"],
)
def test_completion_failure_retains_batch_until_exact_retry(
    tmp_path: Path,
    pause_point: str,
) -> None:
    journal, parent_dirfd = _make_journal(tmp_path, fault=True)
    batch, target_pid = _admit_absence_batch(journal)
    notify_read, notify_write = os.pipe()
    wait_read, wait_write = os.pipe()
    close_errors: list[BaseException] = []
    close_threads: list[threading.Thread] = []
    close_was_blocked: list[bool] = []
    deadline = time.monotonic_ns() + 250_000_000

    def close() -> None:
        try:
            journal.close()
        except BaseException as error:
            close_errors.append(error)

    def release_after_deadline() -> None:
        assert os.read(notify_read, 1) == b"1"
        closer = threading.Thread(target=close)
        close_threads.append(closer)
        closer.start()
        closer.join(timeout=0.05)
        close_was_blocked.append(closer.is_alive())
        while time.monotonic_ns() <= deadline:
            time.sleep(0.001)
        os.write(wait_write, b"x")

    try:
        assert batch.execute() == 1
        sequence_before_completion = journal.scan().head.sequence
        journal.configure_lifecycle_pause_for_test(
            pause_point, notify_write, wait_read
        )
        coordinator = threading.Thread(target=release_after_deadline)
        coordinator.start()
        with pytest.raises(JournalError) as caught:
            batch.complete(deadline_ns=deadline)
        assert caught.value.code is JournalErrorCode.LOCK_TIMEOUT
        coordinator.join(timeout=2)
        assert not coordinator.is_alive()
        assert close_was_blocked == [True]
        assert len(close_threads) == 1
        closer = close_threads[0]
        closer.join(timeout=0.05)
        assert closer.is_alive()

        completed = batch.complete(deadline_ns=_future())
        assert completed.sequence == sequence_before_completion + 1
        assert completed.record.completed_steps == 1
        with pytest.raises(JournalError) as consumed:
            batch.complete(deadline_ns=_future())
        assert consumed.value.code is JournalErrorCode.BATCH_TOKEN

        closer.join(timeout=2)
        assert not closer.is_alive()
        assert close_errors == []
        assert journal.closed is True
    finally:
        for fd in (notify_read, notify_write, wait_read, wait_write):
            os.close(fd)
        _reap_target(target_pid)
        try:
            journal.close()
        except JournalError:
            pass
        os.close(parent_dirfd)


def test_remove_workdir_effect_attempt_blocks_abandon_until_retry(
    tmp_path: Path,
) -> None:
    journal, parent_dirfd = _make_journal(tmp_path, fault=True)
    target_pid = os.fork()
    if target_pid == 0:
        time.sleep(30)
        os._exit(0)
    future = _future()
    close_errors: list[BaseException] = []

    def close() -> None:
        try:
            journal.close()
        except BaseException as error:
            close_errors.append(error)

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
        prerequisite = journal.admit_batch(
            1,
            "executor-1",
            "batch-prerequisite",
            [
                journal.process_absent_descriptor(target),
                journal.reap_process_descriptor(target),
            ],
        )
        assert prerequisite.execute() == 3
        assert prerequisite.complete().record.completed_steps == 3

        removal = journal.admit_batch(
            1,
            "executor-1",
            "batch-remove",
            [journal.remove_bound_workdir_descriptor()],
        )
        journal.fail_next_workdir_parent_fsync_for_test()
        with pytest.raises(JournalError) as failed_effect:
            removal.execute()
        assert failed_effect.value.code is JournalErrorCode.SYSTEM
        assert not (tmp_path / "allocation.workdir").exists()

        closer = threading.Thread(target=close)
        closer.start()
        closer.join(timeout=0.05)
        assert closer.is_alive()
        with pytest.raises(JournalError) as abandon:
            removal.abandon()
        assert abandon.value.code is JournalErrorCode.PRECONDITION
        closer.join(timeout=0.05)
        assert closer.is_alive()

        assert removal.execute() == 7
        assert removal.complete().record.completed_steps == 7
        closer.join(timeout=2)
        assert not closer.is_alive()
        assert close_errors == []
    finally:
        _reap_target(target_pid)
        try:
            journal.close()
        except JournalError:
            pass
        os.close(parent_dirfd)


def test_reap_effect_attempt_blocks_abandon_until_retry(tmp_path: Path) -> None:
    journal, parent_dirfd = _make_journal(tmp_path, fault=True)
    target_pid = os.fork()
    if target_pid == 0:
        time.sleep(30)
        os._exit(0)
    future = _future()
    close_errors: list[BaseException] = []

    def close() -> None:
        try:
            journal.close()
        except BaseException as error:
            close_errors.append(error)

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
        absence = journal.admit_batch(
            1,
            "executor-1",
            "batch-absence",
            [journal.process_absent_descriptor(target)],
        )
        assert absence.execute() == 1
        assert absence.complete().record.completed_steps == 1

        reaping = journal.admit_batch(
            1,
            "executor-1",
            "batch-reap",
            [journal.reap_process_descriptor(target)],
        )
        journal.fail_batch_after_effect_for_test(2)
        with pytest.raises(JournalError) as failed_effect:
            reaping.execute()
        assert failed_effect.value.code is JournalErrorCode.SYSTEM

        closer = threading.Thread(target=close)
        closer.start()
        closer.join(timeout=0.05)
        assert closer.is_alive()
        with pytest.raises(JournalError) as abandon:
            reaping.abandon()
        assert abandon.value.code is JournalErrorCode.PRECONDITION
        closer.join(timeout=0.05)
        assert closer.is_alive()

        assert reaping.execute() == 3
        assert reaping.complete().record.completed_steps == 3
        closer.join(timeout=2)
        assert not closer.is_alive()
        assert close_errors == []
    finally:
        _reap_target(target_pid)
        try:
            journal.close()
        except JournalError:
            pass
        os.close(parent_dirfd)


def test_foreign_thread_last_reference_does_not_finalize_admitted_batch(
    tmp_path: Path,
) -> None:
    journal, parent_dirfd = _make_journal(tmp_path, fault=True)
    batch, target_pid = _admit_absence_batch(journal)
    contender = _open(parent_dirfd, fault=True)
    retained: list[object] = [journal, batch]
    del batch
    del journal

    def drop_last_references() -> None:
        retained.clear()

    dropper = threading.Thread(target=drop_last_references, daemon=True)
    try:
        dropper.start()
        dropper.join(timeout=0.5)
        assert not dropper.is_alive()
        with pytest.raises(JournalError) as blocked:
            contender.probe_action_lock_for_test(
                deadline_ns=time.monotonic_ns() + 100_000_000
            )
        assert blocked.value.code is JournalErrorCode.LOCK_TIMEOUT
    finally:
        _reap_target(target_pid)
        contender.close()
        os.close(parent_dirfd)


def test_native_close_cannot_consume_outstanding_batch(tmp_path: Path) -> None:
    script = r'''
import ctypes
import os
import sys
import time
from pathlib import Path

from claude_sdk_proxy.journal import (
    Journal,
    JournalError,
    JournalErrorCode,
    RecordClass,
)
from claude_sdk_proxy.lifecycle import Record

root = Path(sys.argv[1])
root.mkdir(mode=0o700)
parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
for suffix in (".append.lock", ".action.lock"):
    fd = os.open(
        "allocation.journal" + suffix,
        os.O_RDONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o600,
        dir_fd=parent,
    )
    os.close(fd)
library = Path(sys.argv[2])
journal, receipt = Journal._create_at_for_test(
    parent,
    "allocation.journal",
    b"n" * 32,
    65536,
    81944,
    library_path=library,
)
journal.create_workdir(receipt)
future = time.monotonic_ns() + 5_000_000_000
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
target_pid = os.fork()
if target_pid == 0:
    time.sleep(30)
    os._exit(0)
target = journal.observe_process(target_pid)
os.kill(target_pid, 9)
batch = journal.admit_batch(
    1,
    "executor-1",
    "batch-1",
    [journal.process_absent_descriptor(target)],
)
contender = Journal._open_at_for_test(
    parent,
    "allocation.journal",
    b"n" * 32,
    65536,
    81944,
    library_path=library,
)
journal._library.cpl_journal_close(ctypes.c_void_p(journal._handle.value))
try:
    contender.probe_action_lock_for_test(
        deadline_ns=time.monotonic_ns() + 100_000_000
    )
except JournalError as error:
    if error.code is not JournalErrorCode.LOCK_TIMEOUT:
        os._exit(11)
else:
    os._exit(12)
if batch.execute() != 1 or batch.complete().record.completed_steps != 1:
    os._exit(13)
journal.close()
contender.close()
os.waitpid(target_pid, 0)
os.close(parent)
raise SystemExit(0)
'''
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "native-close-retained"),
            str(_fault_library()),
        ],
        check=False,
        timeout=5,
    )
    assert completed.returncode == 0


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


def test_initial_retirement_authority_epoch_is_fixed(tmp_path: Path) -> None:
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
        while time.monotonic_ns() <= lease:
            time.sleep(0.001)
        before = journal.scan()
        with pytest.raises(JournalError) as caught:
            journal.retire_executor(
                authority="reconciler-2",
                authority_epoch=2,
                authority_deadline_ns=_future(),
            )
        assert caught.value.code is JournalErrorCode.AUTHORITY
        assert journal.scan().physical_eof == before.physical_eof
        assert journal.scan().head.hash == before.head.hash
    finally:
        journal.close()
        os.close(parent_dirfd)


@pytest.mark.parametrize("expiring_deadline", ["claim", "requested_lease"])
def test_activation_rechecks_deadlines_at_final_append_boundary(
    tmp_path: Path,
    expiring_deadline: str,
) -> None:
    journal, parent_dirfd = _make_journal(tmp_path, fault=True)
    selected_deadline = time.monotonic_ns() + 200_000_000
    claim_deadline = selected_deadline if expiring_deadline == "claim" else _future()
    lease_deadline = (
        selected_deadline if expiring_deadline == "requested_lease" else _future()
    )
    prepared = journal.append(
        Record.prepared(
            1,
            "executor-1",
            claim_deadline_ns=claim_deadline,
        ),
        RecordClass.NORMAL,
    )
    notified_read, notified_write = os.pipe()
    release_read, release_write = os.pipe()
    journal.configure_lifecycle_pause_for_test(
        "before_activation_append", notified_write, release_read
    )
    results: list[object] = []

    def activate() -> None:
        try:
            results.append(
                journal.activate_executor(
                    1,
                    "executor-1",
                    os.getpid(),
                    lease_deadline_ns=lease_deadline,
                )
            )
        except BaseException as error:
            results.append(error)

    thread = threading.Thread(target=activate)
    thread.start()
    try:
        assert os.read(notified_read, 1) == b"1"
        while time.monotonic_ns() <= selected_deadline:
            time.sleep(0.001)
        os.write(release_write, b"1")
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert len(results) == 1
        assert isinstance(results[0], JournalError)
        assert results[0].code is JournalErrorCode.AUTHORITY
        assert journal.scan().head.hash == prepared.hash
    finally:
        os.close(notified_read)
        os.close(notified_write)
        os.close(release_read)
        os.close(release_write)
        journal.close()
        os.close(parent_dirfd)


def test_retirement_rechecks_new_authority_deadline_before_append(
    tmp_path: Path,
) -> None:
    journal, parent_dirfd = _make_journal(tmp_path, fault=True)
    lease = time.monotonic_ns() + 20_000_000
    journal.append(
        Record.prepared(1, "executor-1", claim_deadline_ns=_future()),
        RecordClass.NORMAL,
    )
    active = journal.activate_executor(
        1,
        "executor-1",
        os.getpid(),
        lease_deadline_ns=lease,
    )
    while time.monotonic_ns() <= lease:
        time.sleep(0.001)
    authority_deadline = time.monotonic_ns() + 200_000_000
    notified_read, notified_write = os.pipe()
    release_read, release_write = os.pipe()
    journal.configure_lifecycle_pause_for_test(
        "before_retirement_append", notified_write, release_read
    )
    results: list[object] = []

    def retire() -> None:
        try:
            results.append(
                journal.retire_executor(
                    authority="reconciler-1",
                    authority_epoch=1,
                    authority_deadline_ns=authority_deadline,
                )
            )
        except BaseException as error:
            results.append(error)

    thread = threading.Thread(target=retire)
    thread.start()
    try:
        assert os.read(notified_read, 1) == b"1"
        while time.monotonic_ns() <= authority_deadline:
            time.sleep(0.001)
        os.write(release_write, b"1")
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert len(results) == 1
        assert isinstance(results[0], JournalError)
        assert results[0].code is JournalErrorCode.AUTHORITY
        assert journal.scan().head.hash == active.hash
    finally:
        os.close(notified_read)
        os.close(notified_write)
        os.close(release_read)
        os.close(release_write)
        journal.close()
        os.close(parent_dirfd)


def test_replacement_rechecks_new_authority_deadline_before_append(
    tmp_path: Path,
) -> None:
    journal, parent_dirfd = _make_journal(tmp_path, fault=True)
    lease = time.monotonic_ns() + 20_000_000
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
    while time.monotonic_ns() <= lease:
        time.sleep(0.001)
    initial_deadline = time.monotonic_ns() + 30_000_000
    retired = journal.retire_executor(
        authority="reconciler-1",
        authority_epoch=1,
        authority_deadline_ns=initial_deadline,
    )
    while time.monotonic_ns() <= initial_deadline:
        time.sleep(0.001)
    replacement_deadline = time.monotonic_ns() + 200_000_000
    notified_read, notified_write = os.pipe()
    release_read, release_write = os.pipe()
    journal.configure_lifecycle_pause_for_test(
        "before_authority_replacement_append", notified_write, release_read
    )
    results: list[object] = []

    def replace() -> None:
        try:
            results.append(
                journal.replace_retirement_authority(
                    authority="reconciler-2",
                    authority_epoch=2,
                    authority_deadline_ns=replacement_deadline,
                )
            )
        except BaseException as error:
            results.append(error)

    thread = threading.Thread(target=replace)
    thread.start()
    try:
        assert os.read(notified_read, 1) == b"1"
        while time.monotonic_ns() <= replacement_deadline:
            time.sleep(0.001)
        os.write(release_write, b"1")
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert len(results) == 1
        assert isinstance(results[0], JournalError)
        assert results[0].code is JournalErrorCode.AUTHORITY
        assert journal.scan().head.hash == retired.hash
    finally:
        os.close(notified_read)
        os.close(notified_write)
        os.close(release_read)
        os.close(release_write)
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
        lease = time.monotonic_ns() + 40_000_000
        journal.activate_executor(
            1,
            "executor-1",
            os.getpid(),
            lease_deadline_ns=lease,
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
        while time.monotonic_ns() <= lease:
            time.sleep(0.001)
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
            lease_deadline_ns=time.monotonic_ns() + 40_000_000,
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
        time.sleep(0.05)
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


def test_batch_active_cannot_retire_before_executor_lease_expiry(
    tmp_path: Path,
) -> None:
    journal, parent_dirfd = _make_journal(tmp_path, fault=True)
    lease = time.monotonic_ns() + 5_000_000_000
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
        admitted = journal.admit_batch(
            1,
            "executor-1",
            "batch-1",
            [journal.process_absent_descriptor(journal.observe_process(os.getpid()))],
        )
        with pytest.raises(JournalError) as caught:
            journal.retire_executor(
                authority="reconciler-1",
                authority_epoch=1,
                authority_deadline_ns=_future(),
            )
        assert caught.value.code is JournalErrorCode.AUTHORITY
        assert journal.scan().head.record.kind is StateKind.BATCH_ACTIVE
    finally:
        admitted.abandon()
        journal.close()
        os.close(parent_dirfd)


@pytest.mark.parametrize("release_after_expiry", [False, True])
def test_batch_retirement_rechecks_expiry_at_barrier(
    tmp_path: Path,
    release_after_expiry: bool,
) -> None:
    executor, parent_dirfd = _make_journal(tmp_path, fault=True)
    reconciler = _open(parent_dirfd, fault=True)
    lease = time.monotonic_ns() + 200_000_000
    executor.append(
        Record.prepared(1, "executor-1", claim_deadline_ns=_future()),
        RecordClass.NORMAL,
    )
    executor.activate_executor(
        1,
        "executor-1",
        os.getpid(),
        lease_deadline_ns=lease,
    )
    admitted = executor.admit_batch(
        1,
        "executor-1",
        "batch-1",
        [executor.process_absent_descriptor(executor.observe_process(os.getpid()))],
    )
    notified_read, notified_write = os.pipe()
    release_read, release_write = os.pipe()
    reconciler.configure_lifecycle_pause_for_test(
        "before_retirement_expiry_check",
        notified_write,
        release_read,
    )
    results: list[object] = []

    def retire() -> None:
        try:
            results.append(
                reconciler.retire_executor(
                    authority="reconciler-1",
                    authority_epoch=1,
                    authority_deadline_ns=_future(),
                )
            )
        except BaseException as error:
            results.append(error)

    thread = threading.Thread(target=retire)
    thread.start()
    try:
        assert os.read(notified_read, 1) == b"1"
        if release_after_expiry:
            while time.monotonic_ns() <= lease:
                time.sleep(0.001)
        os.write(release_write, b"1")
        thread.join(timeout=2)
        assert not thread.is_alive()
        if release_after_expiry:
            assert not isinstance(results[0], BaseException)
            assert results[0].record.kind is StateKind.RETIRING_BATCH  # type: ignore[union-attr]
        else:
            assert isinstance(results[0], JournalError)
            assert results[0].code is JournalErrorCode.AUTHORITY
    finally:
        os.close(notified_read)
        os.close(notified_write)
        os.close(release_read)
        os.close(release_write)
        reconciler.close()
        admitted.abandon()
        executor.close()
        os.close(parent_dirfd)


def test_batch_admission_barrier_wins_before_retirement(tmp_path: Path) -> None:
    executor, parent_dirfd = _make_journal(tmp_path, fault=True)
    reconciler = _open(parent_dirfd, fault=True)
    lease = time.monotonic_ns() + 40_000_000
    target_pid = os.fork()
    if target_pid == 0:
        time.sleep(30)
        os._exit(0)
    target = executor.observe_process(target_pid)
    executor.append(
        Record.prepared(1, "executor-1", claim_deadline_ns=_future()),
        RecordClass.NORMAL,
    )
    executor.activate_executor(
        1,
        "executor-1",
        os.getpid(),
        lease_deadline_ns=lease,
    )
    os.kill(target_pid, 9)
    notified_read, notified_write = os.pipe()
    release_read, release_write = os.pipe()
    executor.configure_lifecycle_pause_for_test(
        "after_batch_admission_append",
        notified_write,
        release_read,
    )
    admitted: list[object] = []

    def admit() -> None:
        try:
            batch = executor.admit_batch(
                1,
                "executor-1",
                "batch-1",
                [executor.process_absent_descriptor(target)],
            )
            admitted.append(batch)
            batch.execute()
            batch.complete()
        except BaseException as error:
            admitted.append(error)

    thread = threading.Thread(target=admit)
    thread.start()
    try:
        assert os.read(notified_read, 1) == b"1"
        while time.monotonic_ns() <= lease:
            time.sleep(0.001)
        retired = reconciler.retire_executor(
            authority="reconciler-1",
            authority_epoch=1,
            authority_deadline_ns=_future(),
        )
        assert retired.record.kind is StateKind.RETIRING_BATCH
        assert retired.record.exact_batch == "batch-1"
        os.write(release_write, b"1")
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert admitted and not isinstance(admitted[0], BaseException)
        assert executor.scan().head.record.kind is StateKind.RETIRING_IDLE
    finally:
        os.close(notified_read)
        os.close(notified_write)
        os.close(release_read)
        os.close(release_write)
        try:
            os.waitpid(target_pid, os.WNOHANG)
        except ChildProcessError:
            pass
        reconciler.close()
        executor.close()
        os.close(parent_dirfd)


def test_retirement_barrier_wins_before_batch_admission(tmp_path: Path) -> None:
    executor, parent_dirfd = _make_journal(tmp_path, fault=True)
    reconciler = _open(parent_dirfd, fault=True)
    lease = time.monotonic_ns() + 40_000_000
    executor.append(
        Record.prepared(1, "executor-1", claim_deadline_ns=_future()),
        RecordClass.NORMAL,
    )
    executor.activate_executor(
        1,
        "executor-1",
        os.getpid(),
        lease_deadline_ns=lease,
    )
    while time.monotonic_ns() <= lease:
        time.sleep(0.001)
    notified_read, notified_write = os.pipe()
    release_read, release_write = os.pipe()
    reconciler.configure_lifecycle_pause_for_test(
        "before_retirement_append",
        notified_write,
        release_read,
    )
    retired: list[object] = []
    admitted: list[object] = []

    def retire() -> None:
        try:
            retired.append(
                reconciler.retire_executor(
                    authority="reconciler-1",
                    authority_epoch=1,
                    authority_deadline_ns=_future(),
                )
            )
        except BaseException as error:
            retired.append(error)

    def admit() -> None:
        try:
            admitted.append(
                executor.admit_batch(
                    1,
                    "executor-1",
                    "batch-1",
                    [
                        executor.process_absent_descriptor(
                            executor.observe_process(os.getpid())
                        )
                    ],
                )
            )
        except BaseException as error:
            admitted.append(error)

    retire_thread = threading.Thread(target=retire)
    admit_thread = threading.Thread(target=admit)
    retire_thread.start()
    try:
        assert os.read(notified_read, 1) == b"1"
        admit_thread.start()
        time.sleep(0.03)
        assert admit_thread.is_alive()
        os.write(release_write, b"1")
        retire_thread.join(timeout=2)
        admit_thread.join(timeout=2)
        assert not retire_thread.is_alive()
        assert not admit_thread.is_alive()
        assert retired and not isinstance(retired[0], BaseException)
        assert isinstance(admitted[0], JournalError)
        assert admitted[0].code is JournalErrorCode.AUTHORITY
        assert executor.scan().head.record.kind is StateKind.RETIRING_IDLE
    finally:
        os.close(notified_read)
        os.close(notified_write)
        os.close(release_read)
        os.close(release_write)
        reconciler.close()
        executor.close()
        os.close(parent_dirfd)
