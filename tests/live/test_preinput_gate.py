"""Opt-in live result for the unproved model/network pre-input boundary."""

from __future__ import annotations

import os
import shutil
import subprocess
from importlib.metadata import version

import pytest

from claude_sdk_proxy.attestation import current_attestation_availability

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _require_affirmative_policy_and_exact_runtime() -> None:
    if os.environ.get("RUN_LIVE_CLAUDE_TESTS") != "1":
        pytest.skip("set RUN_LIVE_CLAUDE_TESTS=1 for opt-in live rows")
    if os.environ.get("CLAUDE_EXISTING_LOGIN_POLICY_AFFIRMATIVE") != "1":
        pytest.fail("live policy evidence is non-affirmative")
    if version("claude-agent-sdk") != "0.2.148":
        pytest.fail("installed SDK does not match pinned 0.2.148")
    cli = shutil.which("claude")
    if cli is None:
        pytest.fail("Claude CLI is unavailable")
    result = subprocess.run(
        [cli, "--version"],
        check=False,
        capture_output=True,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        text=True,
        timeout=5,
    )
    if result.returncode != 0 or result.stdout.strip() != "2.1.251 (Claude Code)":
        pytest.fail("installed CLI does not match pinned 2.1.251")


@pytest.mark.parametrize(
    "change_boundary", ["startup", "connection", "first_turn", "later_turn"]
)
def test_live_profile_change_gate_is_explicitly_false(
    change_boundary: str,
) -> None:
    verdict = current_attestation_availability()
    assert change_boundary
    if not verdict.core_gate_available:
        pytest.fail(
            "profile-change live gate false: "
            + ",".join(reason.value for reason in verdict.reason_codes)
        )
    raise AssertionError("a future affirmative verdict must run the packet trace")


def test_live_system_mcp_user_and_tool_boundary_is_explicitly_false() -> None:
    verdict = current_attestation_availability()
    if not verdict.core_gate_available:
        pytest.fail(
            "pre-input live gate false for system,mcp,user,tool canaries: "
            + ",".join(reason.value for reason in verdict.reason_codes)
        )
    raise AssertionError("a future affirmative verdict must run the canary trace")
