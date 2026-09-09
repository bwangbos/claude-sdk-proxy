"""Opt-in live gate for the exact public child-init contract."""

from __future__ import annotations

import os
import shutil
import subprocess
from importlib.metadata import version

import pytest
from claude_agent_sdk import SystemMessage

from quaylet.attestation import (
    AttestationError,
    current_attestation_availability,
    extract_child_attestation,
)

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


def test_live_existing_login_gate_reports_actual_unavailable_verdict() -> None:
    verdict = current_attestation_availability()
    if not verdict.core_gate_available:
        pytest.fail(
            "core_gate_available=false: "
            + ",".join(reason.value for reason in verdict.reason_codes)
        )
    raise AssertionError("a future affirmative verdict must install the live harness")


@pytest.mark.parametrize(
    "api_key_source",
    ["environment", "helper", "oauth", "bedrock", "vertex", "unknown", None],
)
def test_live_public_negative_source_never_mints_attestation(
    api_key_source: str | None,
) -> None:
    event = SystemMessage(
        subtype="init",
        data={
            "type": "system",
            "subtype": "init",
            "apiKeySource": api_key_source,
            "claude_code_version": "2.1.251",
        },
    )
    with pytest.raises(AttestationError, match="public auth provenance"):
        extract_child_attestation(event, current_attestation_availability())
