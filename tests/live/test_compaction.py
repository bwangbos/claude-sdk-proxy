from __future__ import annotations

import os

import pytest

from claude_sdk_proxy.attestation import current_attestation_availability
from claude_sdk_proxy.probes import run_compaction_probe

pytestmark = pytest.mark.live


def test_live_compaction_suppression_requires_an_affirmative_task6_gate() -> None:
    if os.environ.get("RUN_LIVE_CLAUDE_TESTS") != "1":
        pytest.skip("set RUN_LIVE_CLAUDE_TESTS=1 for opt-in live rows")
    verdict = current_attestation_availability()
    if not verdict.core_gate_available:
        pytest.fail("compaction probe unavailable because Task 6 core gate is false")
    result = run_compaction_probe()
    assert result.passed is True

