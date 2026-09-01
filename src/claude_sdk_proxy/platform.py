"""Fail-closed Python access to the Darwin/APFS primitive probe."""

from __future__ import annotations

import ctypes
import fcntl
import json
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

_F_PREALLOCATE: Final = 42
_F_FULLFSYNC: Final = 51
_F_ALLOCATECONTIG: Final = 0x00000002
_F_ALLOCATEALL: Final = 0x00000004
_F_PEOFPOSMODE: Final = 3
_FSID_PATTERN: Final = re.compile(r"[0-9a-f]{8}:[0-9a-f]{8}")
_REQUIRED_BOOLEAN_FIELDS: Final = (
    "preallocate",
    "fullfsync_file",
    "renameat",
    "unlinkat",
    "fsync_directory",
    "proc_pidinfo",
)


class PlatformUnsupported(RuntimeError):
    """Raised when the host or runtime root cannot satisfy the required tuple."""


class PlatformProbeError(RuntimeError):
    """Raised when a required native probe operation cannot be completed."""


@dataclass(frozen=True)
class MountIdentity:
    """Canonical identity of the local mount containing the runtime root."""

    filesystem_type: str
    is_local: bool
    mount_device: str
    mount_fsid: str
    mount_flags: tuple[str, ...]
    runtime_root_st_dev: int


@dataclass(frozen=True)
class PlatformEvidence:
    """Validated, JSON-backed evidence for every required Darwin primitive."""

    darwin_major: int
    filesystem_type: str
    is_local: bool
    mount_device: str
    mount_fsid: str
    mount_flags: tuple[str, ...]
    runtime_root_st_dev: int
    os_build: str
    boot_time: int
    preallocate: bool
    fullfsync_file: bool
    renameat: bool
    unlinkat: bool
    fsync_directory: bool
    proc_pidinfo: bool

    @property
    def mount_identity(self) -> MountIdentity:
        """Return the immutable canonical mount identity bound to this root."""
        return MountIdentity(
            filesystem_type=self.filesystem_type,
            is_local=self.is_local,
            mount_device=self.mount_device,
            mount_fsid=self.mount_fsid,
            mount_flags=self.mount_flags,
            runtime_root_st_dev=self.runtime_root_st_dev,
        )


class _Fstore(ctypes.Structure):
    _fields_ = [
        ("fst_flags", ctypes.c_uint32),
        ("fst_posmode", ctypes.c_int),
        ("fst_offset", ctypes.c_longlong),
        ("fst_length", ctypes.c_longlong),
        ("fst_bytesalloc", ctypes.c_longlong),
    ]


def darwin_probe_path() -> Path:
    """Return the fixed native probe path without accepting an ambient override."""
    return Path(__file__).resolve().parents[2] / "build" / "bin" / "darwin-probe"


def _require_exact_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PlatformProbeError(f"invalid {field} evidence")
    return value


def _require_exact_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise PlatformProbeError(f"invalid {field} evidence")
    return value


def _require_nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise PlatformProbeError(f"invalid {field} evidence")
    return value


def _require_mount_flags(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(flag, str) or not flag for flag in value
    ):
        raise PlatformProbeError("invalid mount_flags evidence")
    flags = tuple(cast(list[str], value))
    if flags != tuple(sorted(flags)) or len(set(flags)) != len(flags):
        raise PlatformProbeError("mount flags are not canonical")
    return flags


def _parse_platform_evidence(
    payload: Mapping[str, object], runtime_root_st_dev: int
) -> PlatformEvidence:
    """Validate native JSON before converting it into typed platform evidence."""
    expected_fields = {
        "darwin_major",
        "filesystem_type",
        "is_local",
        "mount_device",
        "mount_fsid",
        "mount_flags",
        "runtime_root_st_dev",
        "os_build",
        "boot_time",
        *_REQUIRED_BOOLEAN_FIELDS,
    }
    if set(payload) != expected_fields:
        raise PlatformProbeError("native platform evidence has an invalid schema")

    darwin_major = _require_exact_int(payload["darwin_major"], "darwin_major")
    filesystem_type = _require_nonempty_string(
        payload["filesystem_type"], "filesystem_type"
    )
    is_local = _require_exact_bool(payload["is_local"], "is_local")
    mount_device = _require_nonempty_string(payload["mount_device"], "mount_device")
    mount_fsid = _require_nonempty_string(payload["mount_fsid"], "mount_fsid")
    mount_flags = _require_mount_flags(payload["mount_flags"])
    observed_st_dev = _require_exact_int(
        payload["runtime_root_st_dev"], "runtime_root_st_dev"
    )
    os_build = _require_nonempty_string(payload["os_build"], "os_build")
    boot_time = _require_exact_int(payload["boot_time"], "boot_time")
    booleans = {
        field: _require_exact_bool(payload[field], field)
        for field in _REQUIRED_BOOLEAN_FIELDS
    }

    if (
        darwin_major < 23
        or filesystem_type != "apfs"
        or not is_local
        or _FSID_PATTERN.fullmatch(mount_fsid) is None
        or observed_st_dev != runtime_root_st_dev
        or not all(booleans.values())
    ):
        raise PlatformUnsupported("Darwin platform tuple is unsupported")

    return PlatformEvidence(
        darwin_major=darwin_major,
        filesystem_type=filesystem_type,
        is_local=is_local,
        mount_device=mount_device,
        mount_fsid=mount_fsid,
        mount_flags=mount_flags,
        runtime_root_st_dev=observed_st_dev,
        os_build=os_build,
        boot_time=boot_time,
        preallocate=booleans["preallocate"],
        fullfsync_file=booleans["fullfsync_file"],
        renameat=booleans["renameat"],
        unlinkat=booleans["unlinkat"],
        fsync_directory=booleans["fsync_directory"],
        proc_pidinfo=booleans["proc_pidinfo"],
    )


def require_supported_platform(root: Path) -> PlatformEvidence:
    """Run the real native probe and reject every non-supported evidence tuple."""
    probe = darwin_probe_path()
    if not probe.is_file() or not os.access(probe, os.X_OK):
        raise PlatformProbeError("Darwin probe executable is unavailable")

    try:
        completed = subprocess.run(
            [str(probe), "platform", os.fspath(root)],
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise PlatformProbeError("Darwin probe cannot be executed") from error

    if completed.stderr:
        raise PlatformProbeError("Darwin probe emitted unexpected stderr")
    if completed.returncode == 65:
        raise PlatformUnsupported("Darwin platform tuple is unsupported")
    if completed.returncode != 0:
        raise PlatformProbeError(
            f"Darwin probe failed with exit status {completed.returncode}"
        )
    try:
        decoded = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PlatformProbeError("Darwin probe emitted invalid JSON") from error
    if not isinstance(decoded, dict):
        raise PlatformProbeError("Darwin probe emitted a non-object JSON value")
    return _parse_platform_evidence(decoded, root.stat().st_dev)


def preallocate(fd: int, length: int) -> None:
    """Reserve physical APFS space through the real Darwin F_PREALLOCATE API."""
    if isinstance(length, bool) or length <= 0:
        raise ValueError("preallocation length must be a positive integer")

    allocation = _Fstore(
        fst_flags=_F_ALLOCATECONTIG,
        fst_posmode=_F_PEOFPOSMODE,
        fst_offset=0,
        fst_length=length,
        fst_bytesalloc=0,
    )
    payload = bytearray(
        ctypes.string_at(ctypes.byref(allocation), ctypes.sizeof(allocation))
    )
    try:
        fcntl.fcntl(fd, _F_PREALLOCATE, payload)
        return
    except OSError:
        allocation.fst_flags = _F_ALLOCATEALL
        payload = bytearray(
            ctypes.string_at(ctypes.byref(allocation), ctypes.sizeof(allocation))
        )
        fcntl.fcntl(fd, _F_PREALLOCATE, payload)


def fullfsync(fd: int) -> None:
    """Durably synchronize a regular APFS file through Darwin F_FULLFSYNC."""
    fcntl.fcntl(fd, _F_FULLFSYNC)
