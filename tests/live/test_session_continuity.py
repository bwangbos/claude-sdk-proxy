from __future__ import annotations

import os

import pytest

from quaylet.attestation import (
    ExactModelAliasMap,
    current_attestation_availability,
)
from quaylet.probes import run_session_probe

pytestmark = pytest.mark.live


def test_live_single_client_preserves_native_two_turn_session() -> None:
    if os.environ.get("RUN_LIVE_CLAUDE_TESTS") != "1":
        pytest.skip("set RUN_LIVE_CLAUDE_TESTS=1 for opt-in live rows")
    if not current_attestation_availability().core_gate_available:
        pytest.fail("native sessions unavailable because Task 6 core gate is false")
    model_id = os.environ.get("QUAYLET_TEST_MODEL_ID")
    if model_id is None:
        pytest.fail("QUAYLET_TEST_MODEL_ID must be configured")
    result = run_session_probe(
        model_aliases=ExactModelAliasMap({"test-model": model_id}),
        public_alias="test-model",
    )
    assert result.passed is True
