from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("component", ["LIFECYCLE", "SUPERVISOR", "ANCHOR"])
def test_native_deadlines_use_python_uptime_after_system_sleep(
    tmp_path: Path, component: str
) -> None:
    """Choosing sleep-inclusive time must not expire an uptime-based deadline."""
    root = Path(__file__).resolve().parents[2]
    executable = tmp_path / "clock-probe"
    compiled = subprocess.run(
        [
            "xcrun",
            "clang",
            "-std=c17",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            f"-DTEST_{component}",
            str(root / "tests/fixtures/native_clock_probe.c"),
            "-L" + str(root / "build/lib"),
            "-lclaude_proxy_lifecycle",
            "-Wl,-rpath," + str(root / "build/lib"),
            "-lproc",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    result = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
