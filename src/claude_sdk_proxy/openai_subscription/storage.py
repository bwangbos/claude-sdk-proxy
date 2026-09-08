from __future__ import annotations

import asyncio
import fcntl
import json
import os
import secrets
import stat
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
            if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
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
        try:
            return self._save_locked_sync(credentials, expected_revision)
        finally:
            self._release_lock_sync(lock_fd)

    def _save_locked_sync(
        self, credentials: Credentials, expected_revision: str | None
    ) -> Credentials:
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

    def _delete_sync(self, expected_revision: str | None) -> bool:
        lock_fd = self._acquire_named_lock_sync(self.state_lock_path)
        try:
            current = self._load_sync()
            if current is None:
                return False
            if expected_revision is not None and current.revision != expected_revision:
                raise CredentialRevisionError("Credentials changed during operation")
            self.path.unlink()
            return True
        finally:
            self._release_lock_sync(lock_fd)

    async def delete(self, *, expected_revision: str | None = None) -> bool:
        return await asyncio.to_thread(self._delete_sync, expected_revision)

    def _acquire_lock_sync(self) -> int:
        return self._acquire_named_lock_sync(self.lock_path)

    def _acquire_named_lock_sync(self, path: Path) -> int:
        self._ensure_root()
        self._check_regular(path, permissions=0o600)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        deadline = time.monotonic() + self.lock_timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
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
        fd = await asyncio.to_thread(self._acquire_lock_sync)
        try:
            yield
        finally:
            await asyncio.to_thread(self._release_lock_sync, fd)
