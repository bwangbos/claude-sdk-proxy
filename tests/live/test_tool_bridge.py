"""Collect-only live matrix for the optional suspended SDK tool gate."""

from __future__ import annotations

import os
from typing import Literal, cast

import pytest

from claude_sdk_proxy.attestation import current_attestation_availability
from claude_sdk_proxy.probes import run_tool_bridge_probe
from claude_sdk_proxy.usage_evidence import (
    UsageEvidenceSchema,
    UsageOperationClass,
    UsageTupleKey,
)

pytestmark = [pytest.mark.live, pytest.mark.anyio]


def _exact_model() -> str:
    if os.environ.get("RUN_LIVE_CLAUDE_TESTS") != "1":
        pytest.fail("set RUN_LIVE_CLAUDE_TESTS=1 for the mandatory live matrix")
    if not current_attestation_availability().core_gate_available:
        pytest.fail("SDK tools unavailable because the Task 6 core gate is false")
    model_id = os.environ.get("CLAUDE_PROXY_TEST_MODEL_ID")
    if model_id is None:
        pytest.fail("CLAUDE_PROXY_TEST_MODEL_ID must be configured")
    return model_id


@pytest.mark.parametrize("delay_seconds", [1, 60, 600])
async def test_callback_survives_response_gap(delay_seconds: int) -> None:
    result = await run_tool_bridge_probe(
        _exact_model(), "delayed_sdk_callback", delay_seconds
    )

    assert result.passed is True
    assert result.evidence["later_user_turn_succeeded"] is True


async def test_identical_parallel_calls_correlate_in_reverse() -> None:
    result = await run_tool_bridge_probe(_exact_model(), "parallel_reverse", 1)

    assert result.passed is True
    assert result.evidence["distinct_public_ids"] == 2
    assert result.evidence["reverse_results_correlated"] is True


@pytest.mark.parametrize("operation_class", ["tool_use_boundary", "post_tool_result"])
async def test_tool_usage_rows_have_exhaustive_dialect_verdicts(
    operation_class: str,
) -> None:
    model_id = _exact_model()
    result = await run_tool_bridge_probe(model_id, "usage_rows", 1)
    schema = cast(UsageEvidenceSchema, result.evidence["usage_schema"])
    expected_key = UsageTupleKey(
        runtime_digest=cast(str, result.evidence["runtime_digest"]),
        backend_model_id=model_id,
        thinking_mode=cast(
            Literal["null", "disabled", "adaptive", "enabled"],
            result.evidence["thinking_mode"],
        ),
        effort=cast(str | None, result.evidence["effort"]),
        budget_tokens=cast(int | None, result.evidence["budget_tokens"]),
        operation_class=UsageOperationClass(operation_class),
    )
    row = schema.only_tool_row(expected_key)

    assert row.sdk_shape_passed is True
    assert {mapping.dialect.value for mapping in row.dialect_mappings} == {
        "anthropic",
        "openai",
    }
    for mapping in row.dialect_mappings:
        if mapping.passed:
            assert {binding.sdk_path for binding in mapping.identity_bindings} == {
                field.sdk_path for field in row.fields
            }


async def test_generated_sdk_mcp_rule_is_recorded_without_override() -> None:
    result = await run_tool_bridge_probe(_exact_model(), "generated_name_rule", 1)

    assert result.evidence["naming_rule_version"] == 1
    assert result.evidence["server_identity"] == "caller_tools_v1"
    assert set(result.evidence["observed_generated_names"]) == {
        "echo",
        "snake_case",
        "dash-name",
        "x" * 64,
    }
    assert result.evidence["naming_rule_consistent"] is True
    assert result.evidence["naming_override_present"] is False
