"""Darwin/APFS capability evidence is accepted only when every primitive passes."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from claude_sdk_proxy.platform import (
    MountIdentity,
    PlatformUnsupported,
    _parse_platform_evidence,
    darwin_probe_path,
    fullfsync,
    preallocate,
    require_supported_platform,
)


@pytest.fixture
def runtime_root(tmp_path: Path) -> Path:
    """Provide an owned, private APFS runtime root for each real probe."""
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


def test_runtime_root_reports_required_tuple(runtime_root: Path) -> None:
    """A wrong mount identity or missing primitive must fail the platform gate."""
    evidence = require_supported_platform(runtime_root)

    assert evidence.darwin_major >= 23
    assert evidence.filesystem_type == "apfs"
    assert evidence.is_local is True
    assert evidence.fullfsync_file and evidence.fsync_directory
    assert evidence.preallocate and evidence.renameat and evidence.unlinkat
    assert evidence.mount_identity == MountIdentity(
        filesystem_type="apfs",
        is_local=True,
        mount_device=evidence.mount_device,
        mount_fsid=evidence.mount_fsid,
        mount_flags=evidence.mount_flags,
        runtime_root_st_dev=runtime_root.stat().st_dev,
    )


def test_platform_probe_emits_one_path_free_json_object(runtime_root: Path) -> None:
    """A capability report must not expose the caller's runtime-root path."""
    completed = subprocess.run(
        [str(darwin_probe_path()), "platform", str(runtime_root)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert completed.stdout.count("\n") == 1
    assert str(runtime_root) not in completed.stdout
    assert isinstance(json.loads(completed.stdout), dict)


def test_real_preallocation_and_full_sync_succeed(runtime_root: Path) -> None:
    """A stubbed preallocator or full-sync call would not exercise the APFS FD."""
    target = runtime_root / "durable-file"
    with target.open("w+b", buffering=0) as stream:
        preallocate(stream.fileno(), 4096)
        assert os.write(stream.fileno(), b"durable") == len(b"durable")
        fullfsync(stream.fileno())

    assert target.stat().st_size == len(b"durable")


def test_wrong_root_mode_is_rejected(runtime_root: Path) -> None:
    """A group- or world-accessible runtime root cannot pass the ownership gate."""
    runtime_root.chmod(0o755)

    with pytest.raises(PlatformUnsupported, match="unsupported"):
        require_supported_platform(runtime_root)


def test_symlink_runtime_root_is_rejected(tmp_path: Path, runtime_root: Path) -> None:
    """Following a runtime-root symlink would invalidate descriptor-relative checks."""
    link = tmp_path / "runtime-link"
    link.symlink_to(runtime_root, target_is_directory=True)

    with pytest.raises(PlatformUnsupported, match="unsupported"):
        require_supported_platform(link)


def test_injected_fuse_evidence_is_rejected(runtime_root: Path) -> None:
    """A non-APFS filesystem label cannot become a supported tuple in Python."""
    injected = json.loads(json.dumps(asdict(require_supported_platform(runtime_root))))
    injected["filesystem_type"] = "fuse"

    with pytest.raises(PlatformUnsupported, match="unsupported"):
        _parse_platform_evidence(injected, runtime_root.stat().st_dev)


def test_injected_nonlocal_evidence_is_rejected(runtime_root: Path) -> None:
    """A remote mount claim cannot become a supported tuple in Python."""
    injected = json.loads(json.dumps(asdict(require_supported_platform(runtime_root))))
    injected["is_local"] = False

    with pytest.raises(PlatformUnsupported, match="unsupported"):
        _parse_platform_evidence(injected, runtime_root.stat().st_dev)


def test_probe_uses_invocation_exit_64() -> None:
    """A malformed command must not be confused with an unsupported tuple."""
    completed = subprocess.run(
        [str(darwin_probe_path())], capture_output=True, check=False, text=True
    )

    assert completed.returncode == 64
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {"error": "invocation"}


def test_probe_uses_unsupported_exit_65(runtime_root: Path) -> None:
    """An unsafe root mode must be reported as a fail-closed tuple rejection."""
    runtime_root.chmod(0o755)
    completed = subprocess.run(
        [str(darwin_probe_path()), "platform", str(runtime_root)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 65
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {"error": "unsupported"}


def test_probe_uses_required_syscall_exit_74(runtime_root: Path) -> None:
    """A required exclusive create failure cannot be reported as capability success."""
    conflicting_file = runtime_root / ".darwin-probe-platform-current"
    conflicting_file.write_bytes(b"occupied")
    conflicting_file.chmod(0o600)
    completed = subprocess.run(
        [str(darwin_probe_path()), "platform", str(runtime_root)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 74
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {"error": "required_syscall"}
