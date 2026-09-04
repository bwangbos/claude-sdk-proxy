from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_NATIVE_OUTPUTS = (
    "build/bin/darwin-probe",
    "build/lib/libclaude_proxy_lifecycle.dylib",
    "build/lib/libclaude_proxy_lifecycle_fault.dylib",
    "build/bin/claude-proxy-supervisor",
    "build/bin/claude-proxy-supervisor-probe",
    "build/bin/claude-proxy-anchor",
    "build/bin/claude-proxy-probe-child",
    "build/bin/claude-proxy-task6-test-cli",
)
_NATIVE_SOURCES = (
    "darwin_probe.c",
    "lifecycle.c",
    "lifecycle.h",
    "claude_supervisor.c",
    "claude_anchor.c",
    "claude_probe_child.c",
    "claude_task6_test_cli.c",
)


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\nset -eu\n" + body)
    path.chmod(0o700)


def _run_make_target(tmp_path: Path, target: str) -> list[str]:
    native = tmp_path / "native"
    native.mkdir()
    for name in _NATIVE_SOURCES:
        (native / name).touch()
    tools = tmp_path / "tools"
    tools.mkdir()
    trace = tmp_path / "trace"
    fake_clang = tools / "fake-clang"
    _executable(
        fake_clang,
        """
output=
while [ "$#" -gt 0 ]; do
    if [ "$1" = "-o" ]; then
        shift
        output=$1
    fi
    shift
done
mkdir -p "$(dirname "$output")"
: > "$output"
printf 'native:%s\\n' "$output" >> "$MAKE_TRACE"
""",
    )
    _executable(
        tools / "uv",
        "printf 'uv:%s\\n' \"$*\" >> \"$MAKE_TRACE\"\n",
    )
    environment = {
        **os.environ,
        "MAKE_TRACE": str(trace),
        "PATH": f"{tools}:{os.environ['PATH']}",
    }
    makefile = Path(__file__).resolve().parents[2] / "Makefile"
    completed = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "-B",
            "-f",
            str(makefile),
            f"CLANG={fake_clang}",
            "SDKROOT=/synthetic-sdk",
            target,
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return trace.read_text().splitlines()


@pytest.mark.parametrize(
    ("target", "test_path"),
    [("unit", "tests/unit"), ("darwin", "tests/darwin")],
)
def test_required_test_target_builds_native_first(
    tmp_path: Path,
    target: str,
    test_path: str,
) -> None:
    trace = _run_make_target(tmp_path, target)
    native_entries = [f"native:{path}" for path in _NATIVE_OUTPUTS]
    assert trace[: len(native_entries)] == native_entries
    assert trace[len(native_entries) :] == [
        "uv:run pytest --strict-markers --forbid-skips -W error " + test_path
    ]


def test_check_executes_native_unit_and_darwin_once_before_static_analysis(
    tmp_path: Path,
) -> None:
    trace = _run_make_target(tmp_path, "check")
    native_entries = [f"native:{path}" for path in _NATIVE_OUTPUTS]
    assert trace == [
        *native_entries,
        "uv:run pytest --strict-markers --forbid-skips -W error tests/unit",
        "uv:run pytest --strict-markers --forbid-skips -W error tests/darwin",
        "uv:run pytest --strict-markers --forbid-skips -W error tests/gateway",
        "uv:run ruff check .",
        "uv:run mypy src/claude_sdk_proxy",
    ]


def test_gateway_target_runs_only_gateway_tests(tmp_path: Path) -> None:
    assert _run_make_target(tmp_path, "gateway") == [
        "uv:run pytest --strict-markers --forbid-skips -W error tests/gateway"
    ]


def test_offline_release_includes_real_pi_integration_before_static_checks(
    tmp_path: Path,
) -> None:
    trace = _run_make_target(tmp_path, "release-offline")
    native_entries = [f"native:{path}" for path in _NATIVE_OUTPUTS]
    assert trace == [
        *native_entries,
        "uv:run pytest --strict-markers --forbid-skips -W error tests/unit",
        "uv:run pytest --strict-markers --forbid-skips -W error tests/darwin",
        "uv:run pytest --strict-markers --forbid-skips -W error tests/gateway",
        "uv:run pytest --strict-markers --forbid-skips -W error tests/integration",
        "uv:run ruff check .",
        "uv:run mypy src/claude_sdk_proxy",
    ]
