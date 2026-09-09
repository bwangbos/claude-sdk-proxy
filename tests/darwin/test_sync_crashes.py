"""Process-crash evidence remains limited to prefixes that completed synchronization."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from quaylet.platform import darwin_probe_path


@pytest.fixture
def runtime_root(tmp_path: Path) -> Path:
    """Provide a private APFS directory so a crash case cannot overlap another case."""
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


@pytest.mark.parametrize(
    ("boundary", "authorized_prefix", "current_exists", "renamed_exists"),
    [
        ("after_create", [], True, False),
        ("after_preallocate", [], True, False),
        ("after_append", [], True, False),
        (
            "after_fullfsync",
            ["create", "preallocate", "append", "fullfsync"],
            True,
            False,
        ),
        (
            "after_renameat",
            ["create", "preallocate", "append", "fullfsync"],
            False,
            True,
        ),
        (
            "after_unlinkat",
            ["create", "preallocate", "append", "fullfsync"],
            False,
            False,
        ),
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
            False,
            False,
        ),
    ],
)
def test_process_crash_reports_only_the_completed_sync_prefix(
    runtime_root: Path,
    boundary: str,
    authorized_prefix: list[str],
    current_exists: bool,
    renamed_exists: bool,
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
    assert (runtime_root / ".darwin-probe-crash-current").exists() is current_exists
    assert (runtime_root / ".darwin-probe-crash-renamed").exists() is renamed_exists
