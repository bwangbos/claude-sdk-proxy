from __future__ import annotations

import os
import subprocess
import sys
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
from claude_sdk_proxy.lifecycle import Record

NORMAL_LIMIT = 32 * 1024
HARD_LIMIT = 48 * 1024


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
                Record.prepared(1, "candidate-1", claim_deadline_ns=100),
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
                Record.prepared(1, "candidate-1", claim_deadline_ns=100),
                RecordClass.NORMAL,
                deadline_ns=time.monotonic_ns() + 20_000_000,
            )
        assert caught.value.code is JournalErrorCode.LOCK_TIMEOUT
        os.write(release_write, b"1")
        os.close(release_write)
        thread.join(timeout=2)
        assert errors == []
        contender.append(
            Record.prepared(1, "candidate-1", claim_deadline_ns=100),
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
                Record.prepared(1, "candidate-1", claim_deadline_ns=100),
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
        assert journal.scan().head.sequence == 0
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
                Record.prepared(1, "candidate-1", claim_deadline_ns=100),
                RecordClass.NORMAL,
                deadline_ns=time.monotonic_ns() + 20_000_000,
            )
        assert caught.value.code is JournalErrorCode.LOCK_TIMEOUT
        os.kill(pid, 9)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == -9
        os.close(release_write)
        contender.append(
            Record.prepared(1, "candidate-1", claim_deadline_ns=100),
            RecordClass.NORMAL,
        )
    finally:
        os.close(notified_read)
        contender.close()
        os.close(parent_dirfd)
