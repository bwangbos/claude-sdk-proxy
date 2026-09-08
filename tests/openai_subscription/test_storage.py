from __future__ import annotations

import asyncio
import json
import math
import os
import stat
import sys
from pathlib import Path

import pytest

from claude_sdk_proxy.openai_subscription.storage import (
    Credentials,
    CredentialStore,
    UnsafeCredentialStorageError,
)


def _credentials() -> Credentials:
    return Credentials("access-secret", "refresh-secret", 1234.0, "account-1")


@pytest.mark.anyio
async def test_atomic_storage_has_owner_only_permissions(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path)
    stored = await store.save(_credentials(), expected_revision=None)

    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert (await store.load()) == stored
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.anyio
@pytest.mark.parametrize("unsafe_kind", ["symlink", "directory", "fifo"])
async def test_storage_rejects_nonregular_credential_path(
    tmp_path: Path, unsafe_kind: str
) -> None:
    store = CredentialStore(tmp_path)
    tmp_path.chmod(0o700)
    if unsafe_kind == "symlink":
        target = tmp_path / "elsewhere"
        target.write_text("untouched")
        store.path.symlink_to(target)
    elif unsafe_kind == "directory":
        store.path.mkdir()
    else:
        os.mkfifo(store.path)

    with pytest.raises(UnsafeCredentialStorageError):
        await store.save(_credentials(), expected_revision=None)


@pytest.mark.anyio
async def test_load_rejects_group_readable_file(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path)
    await store.save(_credentials(), expected_revision=None)
    store.path.chmod(0o640)

    with pytest.raises(UnsafeCredentialStorageError, match="permissions"):
        await store.load()


@pytest.mark.anyio
async def test_corrupt_credentials_do_not_expose_contents(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path)
    tmp_path.chmod(0o700)
    store.path.write_text('{"refresh_token":"very-private"}')
    store.path.chmod(0o600)

    with pytest.raises(UnsafeCredentialStorageError) as caught:
        await store.load()
    assert "very-private" not in str(caught.value)


@pytest.mark.anyio
@pytest.mark.parametrize("expires_at", [math.inf, -math.inf, math.nan])
async def test_storage_rejects_nonfinite_expiry(
    tmp_path: Path, expires_at: float
) -> None:
    store = CredentialStore(tmp_path)
    await store.save(_credentials(), expected_revision=None)
    payload = json.loads(store.path.read_text())
    payload["expires_at"] = expires_at
    store.path.write_text(json.dumps(payload))
    store.path.chmod(0o600)

    with pytest.raises(UnsafeCredentialStorageError, match="invalid"):
        await store.load()


@pytest.mark.anyio
async def test_storage_never_persists_nonfinite_expiry(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path)
    invalid = Credentials("access", "refresh", math.inf, "account")

    with pytest.raises(UnsafeCredentialStorageError, match="expiry"):
        await store.save(invalid, expected_revision=None)
    assert not store.path.exists()


@pytest.mark.anyio
async def test_logout_deletes_only_proxy_openai_credentials(tmp_path: Path) -> None:
    unrelated = tmp_path / "settings.json"
    tmp_path.chmod(0o700)
    unrelated.write_text(json.dumps({"keep": True}))
    store = CredentialStore(tmp_path)
    saved = await store.save(_credentials(), expected_revision=None)

    assert await store.delete(expected_revision=saved.revision)
    assert unrelated.exists()
    assert not store.path.exists()


@pytest.mark.anyio
async def test_refresh_lock_excludes_a_separate_process(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path)
    script = """
import asyncio
import pathlib
import sys
from claude_sdk_proxy.openai_subscription.storage import CredentialStore
async def main():
    try:
        store = CredentialStore(pathlib.Path(sys.argv[1]), lock_timeout=0.05)
        async with store.refresh_lock():
            print('unexpected')
    except TimeoutError:
        print('blocked')
asyncio.run(main())
"""
    async with store.refresh_lock():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            str(tmp_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

    assert process.returncode == 0, stderr.decode()
    assert stdout == b"blocked\n"


@pytest.mark.anyio
async def test_cancelled_refresh_lock_waiter_does_not_leak_lock(
    tmp_path: Path,
) -> None:
    holder = CredentialStore(tmp_path, lock_timeout=0.5)
    waiter = CredentialStore(tmp_path, lock_timeout=0.5)
    follower = CredentialStore(tmp_path, lock_timeout=0.2)

    async with holder.refresh_lock():
        waiter_context = waiter.refresh_lock()
        waiting = asyncio.create_task(waiter_context.__aenter__())
        await asyncio.sleep(0.03)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting

    await asyncio.sleep(0.05)
    async with follower.refresh_lock():
        pass
