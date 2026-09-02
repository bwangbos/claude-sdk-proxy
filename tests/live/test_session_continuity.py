from __future__ import annotations

import os

import pytest

from claude_sdk_proxy.attestation import current_attestation_availability
from claude_sdk_proxy.probes import run_session_probe

pytestmark = pytest.mark.live


def test_live_single_client_preserves_native_two_turn_session() -> None:
    if os.environ.get("RUN_LIVE_CLAUDE_TESTS") != "1":
        pytest.skip("set RUN_LIVE_CLAUDE_TESTS=1 for opt-in live rows")
    if not current_attestation_availability().core_gate_available:
        pytest.fail("native sessions unavailable because Task 6 core gate is false")
    result = run_session_probe()
    assert result.passed is True

