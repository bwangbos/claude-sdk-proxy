from __future__ import annotations

import asyncio
import fcntl
import json
import math
import os
import secrets
import stat
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path


class UnsafeCredentialStorageError(RuntimeError):
    """Credential storage cannot be used without weakening local safety."""


class CredentialRevisionError(RuntimeError):
    """Credentials changed while an operation was in flight."""


@dataclass(frozen=True, repr=False)
class Credentials:
    access_token: str
    refresh_token: str
    expires_at: float
    account_id: str
    revision: str = ""

    def __repr__(self) -> str:
        return (
            "Credentials(access_token=<redacted>, refresh_token=<redacted>, "
            f"expires_at={self.expires_at!r}, account_id={self.account_id!r}, "
            f"revision={self.revision!r})"
        )


class CredentialStore:
    """Atomic proxy-owned credential storage and cross-process refresh lock."""

    def __init__(self, root: Path | None = None, *, lock_timeout: float = 5.0) -> None:
        self.root = root or Path.home() / ".config" / "claude-sdk-proxy"
        self.path = self.root / "openai-credentials.json"
        self.lock_path = self.root / "openai-refresh.lock"
        self.state_lock_path = self.root / "openai-state.lock"
        self.generation_path = self.root / "openai-generation"
        self.lock_timeout = lock_timeout

    def _ensure_root(self) -> None:
        try:
            info = self.root.lstat()
        except FileNotFoundError:
            self.root.mkdir(parents=True, mode=0o700)
            info = self.root.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise UnsafeCredentialStorageError("Credential directory is unsafe")
        os.chmod(self.root, 0o700)

    @staticmethod
    def _check_regular(path: Path, *, permissions: int | None = None) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise UnsafeCredentialStorageError("Credential storage path is unsafe")
        if permissions is not None and stat.S_IMODE(info.st_mode) != permissions:
            raise UnsafeCredentialStorageError("Credential file permissions are unsafe")

    def _load_sync(self) -> Credentials | None:
        self._ensure_root()
        self._check_regular(self.path, permissions=0o600)
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        try:
            raw = os.read(fd, 65_537)
        finally:
            os.close(fd)
        try:
            if len(raw) > 65_536:
                raise ValueError
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {
                "access_token",
                "refresh_token",
                "expires_at",
                "account_id",
                "revision",
            }:
                raise ValueError
            strings = [
                value[k]
                for k in ("access_token", "refresh_token", "account_id", "revision")
            ]
            if not all(isinstance(item, str) and item for item in strings):
                raise ValueError
            expires_at = value["expires_at"]
            if (
                not isinstance(expires_at, (int, float))
                or isinstance(expires_at, bool)
                or not math.isfinite(expires_at)
            ):
                raise ValueError
            return Credentials(
                strings[0], strings[1], float(expires_at), strings[2], strings[3]
            )
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            raise UnsafeCredentialStorageError("Credential file is invalid") from error

    async def load(self) -> Credentials | None:
        return await asyncio.to_thread(self._load_sync)

    def _save_sync(
        self, credentials: Credentials, expected_revision: str | None
    ) -> Credentials:
        lock_fd = self._acquire_named_lock_sync(self.state_lock_path)
        assert lock_fd is not None
        try:
            if not math.isfinite(credentials.expires_at):
                raise UnsafeCredentialStorageError("Credential expiry is invalid")
            current = self._load_sync()
            if (current.revision if current else None) != expected_revision:
                raise CredentialRevisionError("Credentials changed during operation")
            self._write_generation_locked(secrets.token_hex(16))
            return self._save_locked_sync(credentials, expected_revision)
        finally:
            self._release_lock_sync(lock_fd)

    def _save_locked_sync(
        self, credentials: Credentials, expected_revision: str | None
    ) -> Credentials:
        if not math.isfinite(credentials.expires_at):
            raise UnsafeCredentialStorageError("Credential expiry is invalid")
        self._ensure_root()
        self._check_regular(self.path, permissions=0o600)
        current = self._load_sync()
        if (current.revision if current else None) != expected_revision:
            raise CredentialRevisionError("Credentials changed during operation")
        stored = replace(credentials, revision=secrets.token_hex(16))
        payload = json.dumps(
            {
                "access_token": stored.access_token,
                "refresh_token": stored.refresh_token,
                "expires_at": stored.expires_at,
                "account_id": stored.account_id,
                "revision": stored.revision,
            },
            separators=(",", ":"),
        ).encode()
        temp = self.root / f".openai-credentials.{secrets.token_hex(8)}.tmp"
        fd = -1
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, payload)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(temp, self.path)
            dir_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
        return stored

    async def save(
        self, credentials: Credentials, *, expected_revision: str | None
    ) -> Credentials:
        return await asyncio.to_thread(self._save_sync, credentials, expected_revision)

    def _snapshot_generation_sync(self) -> str:
        lock_fd = self._acquire_named_lock_sync(self.state_lock_path)
        assert lock_fd is not None
        try:
            return self._load_or_create_generation_locked()
        finally:
            self._release_lock_sync(lock_fd)

    async def snapshot_generation(self) -> str:
        """Return an opaque lifecycle generation for a later conditional save."""
        return await asyncio.to_thread(self._snapshot_generation_sync)

    def _save_if_generation_sync(
        self, credentials: Credentials, expected_generation: str
    ) -> Credentials:
        lock_fd = self._acquire_named_lock_sync(self.state_lock_path)
        assert lock_fd is not None
        try:
            if self._load_or_create_generation_locked() != expected_generation:
                raise CredentialRevisionError("Credentials changed during operation")
            current = self._load_sync()
            if not math.isfinite(credentials.expires_at):
                raise UnsafeCredentialStorageError("Credential expiry is invalid")
            self._write_generation_locked(secrets.token_hex(16))
            return self._save_locked_sync(
                credentials, current.revision if current is not None else None
            )
        finally:
            self._release_lock_sync(lock_fd)

    async def save_if_generation(
        self, credentials: Credentials, *, expected_generation: str
    ) -> Credentials:
        return await asyncio.to_thread(
            self._save_if_generation_sync, credentials, expected_generation
        )

    def _load_or_create_generation_locked(self) -> str:
        self._check_regular(self.generation_path, permissions=0o600)
        try:
            value = self.generation_path.read_text(encoding="ascii")
        except FileNotFoundError:
            value = secrets.token_hex(16)
            self._write_generation_locked(value)
            return value
        if len(value) != 32 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise UnsafeCredentialStorageError("Credential generation is invalid")
        return value

    def _write_generation_locked(self, value: str) -> None:
        temp = self.root / f".openai-generation.{secrets.token_hex(8)}.tmp"
        fd = -1
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, value.encode("ascii"))
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(temp, self.generation_path)
            dir_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def _delete_sync(self, expected_revision: str | None) -> bool:
        lock_fd = self._acquire_named_lock_sync(self.state_lock_path)
        assert lock_fd is not None
        try:
            current = self._load_sync()
            if current is None:
                if expected_revision is not None:
                    raise CredentialRevisionError(
                        "Credentials changed during operation"
                    )
                self._write_generation_locked(secrets.token_hex(16))
                return False
            if expected_revision is not None and current.revision != expected_revision:
                raise CredentialRevisionError("Credentials changed during operation")
            self._write_generation_locked(secrets.token_hex(16))
            self.path.unlink()
            return True
        finally:
            self._release_lock_sync(lock_fd)

    async def delete(self, *, expected_revision: str | None = None) -> bool:
        return await asyncio.to_thread(self._delete_sync, expected_revision)

    def _acquire_lock_sync(
        self, cancelled: threading.Event | None = None
    ) -> int | None:
        return self._acquire_named_lock_sync(self.lock_path, cancelled=cancelled)

    def _acquire_named_lock_sync(
        self, path: Path, *, cancelled: threading.Event | None = None
    ) -> int | None:
        self._ensure_root()
        self._check_regular(path, permissions=0o600)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        deadline = time.monotonic() + self.lock_timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if cancelled is not None and cancelled.is_set():
                    os.close(fd)
                    return None
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise TimeoutError("Timed out waiting for credential refresh lock")
                time.sleep(0.01)

    @staticmethod
    def _release_lock_sync(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    @asynccontextmanager
    async def refresh_lock(self) -> AsyncIterator[None]:
        cancelled = threading.Event()
        acquisition = asyncio.create_task(
            asyncio.to_thread(self._acquire_lock_sync, cancelled)
        )
        try:
            fd = await asyncio.shield(acquisition)
        except asyncio.CancelledError as cancellation:
            cancelled.set()
            self._clear_cancellation()

            async def finish_acquisition() -> None:
                try:
                    acquired_fd = await acquisition
                except TimeoutError:
                    return
                if acquired_fd is not None:
                    await asyncio.to_thread(self._release_lock_sync, acquired_fd)

            cleanup = asyncio.create_task(finish_acquisition())
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    self._clear_cancellation()
            cleanup.result()
            raise cancellation
        if fd is None:
            raise RuntimeError("Unexpected cancelled lock acquisition")
        try:
            yield
        finally:
            await asyncio.to_thread(self._release_lock_sync, fd)

    @staticmethod
    def _clear_cancellation() -> None:
        task = asyncio.current_task()
        if task is not None:
            while task.uncancel():
                pass
