"""Root ownership lock and owner-record probes execute their real Darwin sequences."""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest

from quaylet.platform import darwin_probe_path


@pytest.fixture
def runtime_root(tmp_path: Path) -> Path:
    """Provide an owned, mode-0700 runtime root for one fixed probe sequence."""
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


@pytest.mark.parametrize(
    "subcommand",
    ["root_reconciliation_lock", "instance_lifetime_lock"],
)
def test_canonical_bsd_lock_is_exclusive_and_released_at_process_exit(
    runtime_root: Path, subcommand: str
) -> None:
    """An inherited or noncanonical lock FD would fail the child contention proof."""
    completed = subprocess.run(
        [str(darwin_probe_path()), subcommand, str(runtime_root)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {
        subcommand: True,
        "process_exit_release": True,
    }


@pytest.mark.parametrize("subcommand", ["owner_record_create", "owner_record_replace"])
def test_owner_record_sequences_preserve_private_mode(
    runtime_root: Path, subcommand: str
) -> None:
    """Create and replacement must retain an owner-only record after real syncing."""
    completed = subprocess.run(
        [str(darwin_probe_path()), subcommand, str(runtime_root)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {subcommand: True}
    assert stat.S_IMODE((runtime_root / "owner.record").stat().st_mode) == 0o600
    if subcommand == "owner_record_replace":
        assert (runtime_root / "owner.record").read_bytes() == b"owner-record-replace"
        assert not (runtime_root / ".owner.record.tmp").exists()
