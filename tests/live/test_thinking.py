from __future__ import annotations

import os

import pytest

from quaylet.attestation import (
    ExactModelAliasMap,
    current_attestation_availability,
)
from quaylet.probes import run_thinking_probe

pytestmark = pytest.mark.live


def test_live_thinking_matrix_is_exact_and_complete() -> None:
    if os.environ.get("RUN_LIVE_CLAUDE_TESTS") != "1":
        pytest.skip("set RUN_LIVE_CLAUDE_TESTS=1 for opt-in live rows")
    if not current_attestation_availability().core_gate_available:
        pytest.fail("thinking tuples unavailable because Task 6 core gate is false")
    model_id = os.environ.get("QUAYLET_TEST_MODEL_ID")
    if model_id is None:
        pytest.fail("QUAYLET_TEST_MODEL_ID must be configured")
    result = run_thinking_probe(
        model_aliases=ExactModelAliasMap({"test-model": model_id}),
        public_alias="test-model",
    )
    assert result.passed is True
