from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import claude_sdk_proxy.journal as journal_implementation
from claude_sdk_proxy.journal import (
    AdmittedBatch,
    Journal,
    JournalError,
    JournalErrorCode,
    RecordClass,
)
from claude_sdk_proxy.lifecycle import Record

NORMAL_LIMIT = 32 * 1024
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


def _open_second(parent_dirfd: int, *, fault: bool = True) -> Journal:
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


def _hold_lock(
    journal: Journal,
) -> tuple[threading.Thread, int, list[BaseException]]:
    notified_read, notified_write = os.pipe()
    release_read, release_write = os.pipe()
    errors: list[BaseException] = []

    def hold() -> None:
        try:
            journal.hold_append_lock_for_test(notified_write, release_read)
        except BaseException as error:
            errors.append(error)
        finally:
            os.close(notified_write)
            os.close(release_read)

    thread = threading.Thread(
        target=hold,
        daemon=True,
    )
    thread.start()
    assert os.read(notified_read, 1) == b"1"
    os.close(notified_read)
    return thread, release_write, errors


def test_same_handle_tasks_share_one_mutex(tmp_path: Path) -> None:
    journal, parent_dirfd = _make_journal(tmp_path, fault=True)
    thread, release_write, errors = _hold_lock(journal)
    try:
        with pytest.raises(JournalError) as caught:
            journal.append(
                Record.prepared(1, "candidate-1", claim_deadline_ns=_future()),
                RecordClass.NORMAL,
                deadline_ns=time.monotonic_ns() + 20_000_000,
            )
        assert caught.value.code is JournalErrorCode.LOCK_TIMEOUT
    finally:
        os.write(release_write, b"1")
        os.close(release_write)
        thread.join(timeout=2)
        journal.close()
        os.close(parent_dirfd)
    assert not thread.is_alive()
    assert errors == []


def test_separate_opens_contend_on_bsd_flock(tmp_path: Path) -> None:
    owner, parent_dirfd = _make_journal(tmp_path, fault=True)
    contender = _open_second(parent_dirfd)
    thread, release_write, errors = _hold_lock(owner)
    try:
        with pytest.raises(JournalError) as caught:
            contender.append(
                Record.prepared(1, "candidate-1", claim_deadline_ns=_future()),
                RecordClass.NORMAL,
                deadline_ns=time.monotonic_ns() + 20_000_000,
            )
        assert caught.value.code is JournalErrorCode.LOCK_TIMEOUT
        os.write(release_write, b"1")
        os.close(release_write)
        thread.join(timeout=2)
        assert errors == []
        contender.append(
            Record.prepared(1, "candidate-1", claim_deadline_ns=_future()),
            RecordClass.NORMAL,
        )
    finally:
        if thread.is_alive():
            os.write(release_write, b"1")
            os.close(release_write)
            thread.join(timeout=2)
        contender.close()
        owner.close()
        os.close(parent_dirfd)


def test_closing_unrelated_fd_does_not_release_library_lock(tmp_path: Path) -> None:
    owner, parent_dirfd = _make_journal(tmp_path, fault=True)
    contender = _open_second(parent_dirfd)
    unrelated = os.open(
        "allocation.journal.append.lock",
        os.O_RDONLY | os.O_CLOEXEC,
        dir_fd=parent_dirfd,
    )
    thread, release_write, errors = _hold_lock(owner)
    try:
        os.close(unrelated)
        with pytest.raises(JournalError) as caught:
            contender.append(
                Record.prepared(1, "candidate-1", claim_deadline_ns=_future()),
                RecordClass.NORMAL,
                deadline_ns=time.monotonic_ns() + 20_000_000,
            )
        assert caught.value.code is JournalErrorCode.LOCK_TIMEOUT
    finally:
        os.write(release_write, b"1")
        os.close(release_write)
        thread.join(timeout=2)
        contender.close()
        owner.close()
        os.close(parent_dirfd)
    assert errors == []


def test_forked_child_rejects_inherited_handle(tmp_path: Path) -> None:
    journal, parent_dirfd = _make_journal(tmp_path)
    pid = os.fork()
    if pid == 0:
        try:
            journal.scan()
        except JournalError as error:
            os._exit(0 if error.code is JournalErrorCode.FORK_INHERITED else 2)
        os._exit(3)

    _, status = os.waitpid(pid, 0)
    try:
        assert os.waitstatus_to_exitcode(status) == 0
        assert journal.scan().head.sequence == 1
    finally:
        journal.close()
        os.close(parent_dirfd)


def _wait_for_child_bounded(pid: int, timeout: float = 2.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        waited, status = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return os.waitstatus_to_exitcode(status)
        time.sleep(0.005)
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    raise AssertionError("fork child did not reject the inherited object promptly")


@pytest.mark.parametrize(
    ("handle_state", "operation"),
    [
        ("active", "scan"),
        ("open", "append"),
        ("pending", "close"),
        ("closed", "closed_property"),
        ("open", "unhealthy_property"),
        ("open", "admit_batch"),
        ("open", "batch_execute"),
        ("open", "batch_complete"),
        ("open", "batch_abandon"),
    ],
)
def test_fork_child_rejects_before_held_python_condition_for_every_operation_kind(
    tmp_path: Path,
    handle_state: str,
    operation: str,
) -> None:
    """A child must not wait on or release a lock belonging to the parent."""
    journal, parent_dirfd = _make_journal(tmp_path)
    if handle_state == "closed":
        journal.close()
    elif handle_state == "pending":
        journal._handle_state = journal_implementation._HandleState.CLOSING
        journal._closing_thread_id = -1
    elif handle_state == "active":
        journal._active_operations = 1

    batch = AdmittedBatch(
        journal_implementation._BATCH_TOKEN,
        journal,
        journal_implementation._CActionToken(),
    )
    condition_held = threading.Event()
    release_condition = threading.Event()

    def hold_condition() -> None:
        with journal._operation_condition:
            condition_held.set()
            release_condition.wait()

    holder = threading.Thread(target=hold_condition)
    holder.start()
    assert condition_held.wait(timeout=1)
    pid = os.fork()
    if pid == 0:
        signal.signal(signal.SIGALRM, lambda _signum, _frame: os._exit(124))
        signal.alarm(1)
        try:
            if operation == "scan":
                journal.scan()
            elif operation == "append":
                journal.append(
                    Record.prepared(1, "candidate-1", claim_deadline_ns=_future()),
                    RecordClass.NORMAL,
                )
            elif operation == "close":
                journal.close()
            elif operation == "closed_property":
                _ = journal.closed
            elif operation == "unhealthy_property":
                _ = journal.unhealthy
            elif operation == "admit_batch":
                journal.admit_batch(
                    1,
                    "executor-1",
                    "batch-1",
                    (Journal.terminal_checks_descriptor(),),
                )
            elif operation == "batch_execute":
                batch.execute()
            elif operation == "batch_complete":
                batch.complete()
            elif operation == "batch_abandon":
                batch.abandon()
            else:
                os._exit(125)
        except JournalError as error:
            os._exit(0 if error.code is JournalErrorCode.FORK_INHERITED else 2)
        except BaseException:
            os._exit(3)
        os._exit(4)

    try:
        assert _wait_for_child_bounded(pid) == 0
        assert holder.is_alive()
    finally:
        release_condition.set()
        holder.join(timeout=1)
        assert not holder.is_alive()
        if handle_state == "pending":
            journal._handle_state = journal_implementation._HandleState.OPEN
            journal._closing_thread_id = None
        elif handle_state == "active":
            journal._active_operations = 0
        journal.close()
        os.close(parent_dirfd)


def test_journal_creator_guard_is_exact_type_and_tamper_fail_closed(
    tmp_path: Path,
) -> None:
    journal, parent_dirfd = _make_journal(tmp_path)
    creator_pid = os.getpid()
    object.__setattr__(journal, "_creator_pid", "hostile")
    try:
        with pytest.raises(JournalError) as caught:
            journal.scan()
        assert caught.value.code is JournalErrorCode.FORK_INHERITED
    finally:
        object.__setattr__(journal, "_creator_pid", creator_pid)

    class HostileJournal(Journal):
        def __getattribute__(self, _name: str) -> object:
            raise AssertionError("subclass attributes must not be traversed")

    forged = object.__new__(HostileJournal)
    try:
        with pytest.raises(JournalError) as caught:
            Journal.scan(forged)
        assert caught.value.code is JournalErrorCode.FORK_INHERITED
    finally:
        journal.close()
        os.close(parent_dirfd)


def test_exec_does_not_inherit_any_lock_descriptor(tmp_path: Path) -> None:
    journal, parent_dirfd = _make_journal(tmp_path)
    lock_stat = os.stat(
        "allocation.journal.append.lock",
        dir_fd=parent_dirfd,
        follow_symlinks=False,
    )
    script = """
import os, sys
target = (int(sys.argv[1]), int(sys.argv[2]))
for name in os.listdir('/dev/fd'):
    try:
        st = os.fstat(int(name))
    except (OSError, ValueError):
        continue
    if (st.st_dev, st.st_ino) == target:
        raise SystemExit(9)
"""
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(lock_stat.st_dev),
                str(lock_stat.st_ino),
            ],
            check=False,
            close_fds=False,
        )
        assert completed.returncode == 0
    finally:
        journal.close()
        os.close(parent_dirfd)


def test_process_exit_releases_append_lock(tmp_path: Path) -> None:
    journal, parent_dirfd = _make_journal(tmp_path, fault=True)
    journal.close()
    notified_read, notified_write = os.pipe()
    release_read, release_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(notified_read)
        os.close(release_write)
        child = _open_second(parent_dirfd)
        try:
            child.hold_append_lock_for_test(notified_write, release_read)
        finally:
            child.close()
        os._exit(0)

    os.close(notified_write)
    os.close(release_read)
    contender = _open_second(parent_dirfd)
    try:
        assert os.read(notified_read, 1) == b"1"
        with pytest.raises(JournalError) as caught:
            contender.append(
                Record.prepared(1, "candidate-1", claim_deadline_ns=_future()),
                RecordClass.NORMAL,
                deadline_ns=time.monotonic_ns() + 20_000_000,
            )
        assert caught.value.code is JournalErrorCode.LOCK_TIMEOUT
        os.kill(pid, 9)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == -9
        os.close(release_write)
        contender.append(
            Record.prepared(1, "candidate-1", claim_deadline_ns=_future()),
            RecordClass.NORMAL,
        )
    finally:
        os.close(notified_read)
        contender.close()
        os.close(parent_dirfd)


def test_atfork_child_closes_every_inherited_journal_and_lock_fd(
    tmp_path: Path,
) -> None:
    journal, parent_dirfd = _make_journal(tmp_path)
    protected = {
        (
            os.stat(name, dir_fd=parent_dirfd, follow_symlinks=False).st_dev,
            os.stat(name, dir_fd=parent_dirfd, follow_symlinks=False).st_ino,
        )
        for name in (
            "allocation.journal",
            "allocation.journal.append.lock",
            "allocation.journal.action.lock",
        )
    }
    observed_read, observed_write = os.pipe()
    release_read, release_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(observed_read)
        os.close(release_write)
        inherited = 0
        for entry in os.listdir("/dev/fd"):
            try:
                metadata = os.fstat(int(entry))
            except OSError, ValueError:
                continue
            if (metadata.st_dev, metadata.st_ino) in protected:
                inherited += 1
        os.write(observed_write, str(inherited).encode("ascii"))
        os.read(release_read, 1)
        os._exit(0)

    os.close(observed_write)
    os.close(release_read)
    try:
        assert os.read(observed_read, 16) == b"0"
    finally:
        os.write(release_write, b"1")
        os.close(release_write)
        os.close(observed_read)
        os.waitpid(pid, 0)
        journal.close()
        os.close(parent_dirfd)


def test_public_scan_waits_for_append_admission(tmp_path: Path) -> None:
    journal, parent_dirfd = _make_journal(tmp_path, fault=True)
    notified_read, notified_write = os.pipe()
    release_read, release_write = os.pipe()
    lock_thread = threading.Thread(
        target=journal.hold_append_lock_for_test,
        args=(notified_write, release_read),
    )
    result: list[object] = []
    scan_thread = threading.Thread(target=lambda: result.append(journal.scan()))
    lock_thread.start()
    try:
        assert os.read(notified_read, 1) == b"1"
        scan_thread.start()
        time.sleep(0.03)
        assert scan_thread.is_alive()
        assert result == []
    finally:
        os.write(release_write, b"1")
        lock_thread.join(timeout=2)
        scan_thread.join(timeout=2)
        os.close(notified_read)
        os.close(notified_write)
        os.close(release_read)
        os.close(release_write)
        journal.close()
        os.close(parent_dirfd)


def test_close_waits_for_inflight_ctypes_operation(tmp_path: Path) -> None:
    script = r'''
import os
import sys
import threading
import time
from pathlib import Path
from claude_sdk_proxy.journal import Journal

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
    32768,
    49176,
    workdir_parent_dirfd=parent,
    workdir_name="allocation.workdir",
    library_path=library,
)
journal.create_workdir(receipt)
notified_read, notified_write = os.pipe()
release_read, release_write = os.pipe()
operation = threading.Thread(
    target=journal.hold_append_lock_for_test,
    args=(notified_write, release_read),
)
operation.start()
if os.read(notified_read, 1) != b"1":
    os._exit(6)
closers = [threading.Thread(target=journal.close) for _ in range(2)]
for closer in closers:
    closer.start()
for closer in closers:
    closer.join(timeout=0.05)
if not all(closer.is_alive() for closer in closers):
    os._exit(7)
os.write(release_write, b"1")
operation.join(timeout=2)
for closer in closers:
    closer.join(timeout=2)
if (
    operation.is_alive()
    or any(closer.is_alive() for closer in closers)
    or not journal.closed
):
    os._exit(8)
os._exit(0)
'''
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "close-race"),
            str(_fault_library()),
        ],
        check=False,
        timeout=5,
    )
    assert completed.returncode == 0


def test_same_thread_recursive_close_is_idempotent(tmp_path: Path) -> None:
    script = r'''
import os
import sys
from pathlib import Path
from claude_sdk_proxy.journal import Journal

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
journal, _ = Journal._create_at_for_test(
    parent,
    "allocation.journal",
    b"n" * 32,
    32768,
    49176,
    library_path=Path(sys.argv[2]),
)
native_close = journal._library.cpl_journal_close

def reentrant_close(handle):
    journal.close()
    native_close(handle)

journal._library.cpl_journal_close = reentrant_close
journal.close()
raise SystemExit(0 if journal.closed else 9)
'''
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "reentrant-close"),
            str(_fault_library()),
        ],
        check=False,
        timeout=5,
    )
    assert completed.returncode == 0


def test_owner_death_releases_lock_while_fork_child_remains_alive(
    tmp_path: Path,
) -> None:
    journal, parent_dirfd = _make_journal(tmp_path, fault=True)
    journal.close()
    notified_read, notified_write = os.pipe()
    child_pid_read, child_pid_write = os.pipe()
    hold_read, hold_write = os.pipe()
    owner_pid = os.fork()
    if owner_pid == 0:
        os.close(notified_read)
        os.close(child_pid_read)
        os.close(hold_write)
        owner = _open_second(parent_dirfd)
        sleeper_pid = os.fork()
        if sleeper_pid == 0:
            os.close(notified_write)
            os.close(child_pid_write)
            os.read(hold_read, 1)
            os._exit(0)
        os.write(child_pid_write, str(sleeper_pid).encode("ascii"))
        owner.hold_append_lock_for_test(notified_write, hold_read)
        os._exit(0)

    os.close(notified_write)
    os.close(child_pid_write)
    os.close(hold_read)
    sleeper_pid = int(os.read(child_pid_read, 32).decode("ascii"))
    contender = _open_second(parent_dirfd)
    try:
        assert os.read(notified_read, 1) == b"1"
        os.kill(owner_pid, 9)
        _, status = os.waitpid(owner_pid, 0)
        assert os.waitstatus_to_exitcode(status) == -9
        os.kill(sleeper_pid, 0)
        contender.append(
            Record.prepared(1, "candidate-1", claim_deadline_ns=_future()),
            RecordClass.NORMAL,
        )
    finally:
        os.write(hold_write, b"1")
        os.close(hold_write)
        os.close(notified_read)
        os.close(child_pid_read)
        contender.close()
        os.close(parent_dirfd)


def test_executor_death_releases_destructive_action_lock(tmp_path: Path) -> None:
    journal, parent_dirfd = _make_journal(tmp_path, fault=True)
    journal.close()
    notified_read, notified_write = os.pipe()
    release_read, release_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(notified_read)
        os.close(release_write)
        child = _open_second(parent_dirfd)
        child.hold_action_lock_for_test(notified_write, release_read)
        os._exit(0)

    os.close(notified_write)
    os.close(release_read)
    contender = _open_second(parent_dirfd)
    try:
        assert os.read(notified_read, 1) == b"1"
        os.kill(pid, 9)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == -9
        contender.probe_action_lock_for_test()
    finally:
        os.close(notified_read)
        os.close(release_write)
        contender.close()
        os.close(parent_dirfd)
