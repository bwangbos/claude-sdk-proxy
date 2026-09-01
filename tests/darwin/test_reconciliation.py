"""Crash and ownership-reconciliation matrices for the native lifecycle."""

from __future__ import annotations

import pytest

from claude_sdk_proxy.supervisor_probe import run_lifecycle_scenario


@pytest.mark.parametrize(
    "boundary",
    [
        "supervisor_before_identity",
        "anchor_before_identity",
        "cli_before_armed",
        "after_armed_before_exec",
        "after_running",
        "during_term_batch",
        "during_kill_batch",
    ],
)
def test_crash_boundary_is_reconcilable(boundary: str) -> None:
    """A process crash at any named boundary must retain a safe terminal prefix."""
    result = run_lifecycle_scenario(boundary)
    assert result.outcome in {"done", "unconfirmed"}
    assert result.unsafe_numeric_signal_count == 0
    assert result.canonical_head_certified
    assert result.same_canonical_journal
    assert result.evidence_observed_not_inferred
    if boundary in {"after_running", "during_term_batch", "during_kill_batch"}:
        assert result.outcome == "unconfirmed"
        assert result.actor_loss_observed
        assert result.actor_loss_exit_code == 86
        assert result.recovery_executor_reaped
        assert result.production_recovery_signal_count == 0
        assert result.test_teardown_group_absent
        assert result.untracked_orphan_count == 0
        assert result.cleanup_failure_injection_observed


@pytest.mark.parametrize(
    "scenario",
    [
        "stale_executor",
        "retirement_replacement",
        "interrupted_batch_replay",
    ],
)
def test_reconciliation_uses_task4_generation_and_batch_authority(
    scenario: str,
) -> None:
    """Bypassing Task 4 handoff or exact-batch retention must break reconciliation."""
    result = run_lifecycle_scenario(scenario)

    assert result.outcome == "unconfirmed"
    assert result.task4_authority_used
    assert result.stale_executor_blocked
    assert result.exact_batch_preserved
    assert result.canonical_head_certified
    assert result.unsafe_numeric_signal_count == 0
    assert result.same_canonical_journal
    assert result.original_supervisor_actor_chain
    assert result.evidence_observed_not_inferred
    assert result.observed_handoff_records >= 2


def test_wedged_supervisor_blocks_successor_and_destructive_actions() -> None:
    """A live ambiguous supervisor must consume capacity instead of being replaced."""
    result = run_lifecycle_scenario("wedged_supervisor")

    assert result.outcome == "unconfirmed"
    assert result.successor_activated is False
    assert result.anchor_alive
    assert result.workdir_removed is False
    assert result.helper_promoted is False
    assert result.unsafe_numeric_signal_count == 0
    assert result.same_canonical_journal
    assert result.original_supervisor_actor_chain
    assert result.evidence_observed_not_inferred
    assert result.observed_live_executor
    assert result.production_recovery_signal_count == 0
    assert result.test_teardown_group_absent
    assert result.untracked_orphan_count == 0
