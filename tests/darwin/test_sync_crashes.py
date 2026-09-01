"""Process-crash evidence remains limited to prefixes that completed synchronization."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from claude_sdk_proxy.platform import darwin_probe_path


@pytest.fixture
def runtime_root(tmp_path: Path) -> Path:
    """Provide a private APFS directory so a crash case cannot overlap another case."""
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


@pytest.mark.parametrize(
    ("boundary", "authorized_prefix"),
    [
        ("after_create", []),
        ("after_preallocate", []),
        ("after_append", []),
        ("after_fullfsync", ["create", "preallocate", "append", "fullfsync"]),
        ("after_renameat", ["create", "preallocate", "append", "fullfsync"]),
        ("after_unlinkat", ["create", "preallocate", "append", "fullfsync"]),
        (
            "after_directory_fsync",
            [
                "create",
                "preallocate",
                "append",
                "fullfsync",
                "renameat",
                "unlinkat",
                "directory_fsync",
            ],
        ),
    ],
)
def test_process_crash_reports_only_the_completed_sync_prefix(
    runtime_root: Path, boundary: str, authorized_prefix: list[str]
) -> None:
    """Unsynced visibility is never treated as reboot or power-loss evidence."""
    completed = subprocess.run(
        [str(darwin_probe_path()), "crash", str(runtime_root), boundary],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert completed.stdout.endswith("\n")
    assert json.loads(completed.stdout) == {
        "crash_boundary": boundary,
        "child_exit_status": 91,
        "authorized_prefix": authorized_prefix,
    }
