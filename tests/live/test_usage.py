from __future__ import annotations

import os

import pytest

from claude_sdk_proxy.attestation import (
    ExactModelAliasMap,
    current_attestation_availability,
)
from claude_sdk_proxy.probes import run_usage_probe

pytestmark = pytest.mark.live


def test_live_usage_schema_has_exact_streaming_and_dialect_verdicts() -> None:
    if os.environ.get("RUN_LIVE_CLAUDE_TESTS") != "1":
        pytest.skip("set RUN_LIVE_CLAUDE_TESTS=1 for opt-in live rows")
    if not current_attestation_availability().core_gate_available:
        pytest.fail("usage evidence unavailable because Task 6 core gate is false")
    model_id = os.environ.get("CLAUDE_PROXY_TEST_MODEL_ID")
    if model_id is None:
        pytest.fail("CLAUDE_PROXY_TEST_MODEL_ID must be configured")
    schema = run_usage_probe(
        model_aliases=ExactModelAliasMap({"test-model": model_id}),
        public_alias="test-model",
    )
    assert schema.rows
    assert all(row.sdk_shape_passed for row in schema.rows)
