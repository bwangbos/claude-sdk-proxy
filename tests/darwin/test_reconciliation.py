"""Crash and ownership-reconciliation matrices for the native lifecycle."""

from __future__ import annotations

import gc

import pytest

from claude_sdk_proxy import supervisor_probe
from claude_sdk_proxy.supervisor_probe import run_lifecycle_scenario


class _InjectedPythonFailure(RuntimeError):
    pass


@pytest.mark.parametrize(
    ("scenario", "point"),
    [
        ("cleanup_fail_after_admission", "anchor_identity_known"),
        ("cleanup_fail_after_admission", "cleanup_request_prepared"),
        ("cleanup_fail_after_admission", "cleanup_request_sent"),
        ("cleanup_fail_after_admission", "cleanup_ack_accepted"),
        ("cleanup_fail_after_admission", "actor_loss_observed"),
        ("cleanup_fail_after_admission", "actor_loss_record_certified"),
        ("cleanup_fail_after_admission", "recovery_head_observed"),
        ("stale_executor", "dead_executor_reuse_rejected"),
        ("cleanup_fail_after_admission", "executor_lease_expired"),
        ("cleanup_fail_after_admission", "executor_retired"),
        ("retirement_replacement", "retirement_authority_replaced"),
        ("cleanup_fail_after_admission", "executor_reap_confirmed"),
        ("cleanup_fail_after_admission", "interrupted_batch_reconciled"),
        ("stale_executor", "successor_prepared"),
        ("stale_executor", "successor_activated"),
        ("cleanup_fail_after_stop", "group_state_observed"),
        ("cleanup_fail_after_admission", "unconfirmed_persisted"),
        ("cleanup_fail_after_admission", "unconfirmed_certified"),
        ("cleanup_fail_after_admission", "evidence_captured"),
    ],
)
def test_actor_loss_python_exceptions_retain_exact_chain_until_test_teardown(
    scenario: str,
    point: str,
    monkeypatch: pytest.MonkeyPatch,
    recwarn: pytest.WarningsRecorder,
) -> None:
    """Losing Python orchestration must not reap a parent and orphan its group."""
    before = supervisor_probe._retained_actor_chain_count()

    def inject(flow: str, checkpoint: str) -> None:
        if flow == "actor_loss" and checkpoint == point:
            raise _InjectedPythonFailure(point)

    monkeypatch.setattr(supervisor_probe, "_python_exception_checkpoint", inject)
    with pytest.raises(_InjectedPythonFailure, match=point):
        run_lifecycle_scenario(scenario)

    assert supervisor_probe._retained_actor_chain_count() == before + 1
    try:
        retained = supervisor_probe._inspect_last_retained_actor_chain()
        assert retained.flow == "actor_loss"
        assert retained.canonical_state == "UNCONFIRMED"
        assert retained.actor_live_or_task4_reaped
        if point in {"anchor_identity_known", "cleanup_request_prepared"}:
            assert retained.supervisor_task4_reaped is False
        else:
            assert retained.supervisor_task4_reaped
        assert retained.anchor_identity_exact
        assert retained.workdir_retained and retained.journal_retained
    finally:
        teardown = supervisor_probe._test_release_last_retained_actor_chain()
    assert teardown.group_absent
    assert teardown.supervisor_child_reaped
    assert teardown.untracked_orphan_count == 0
    assert teardown.workdir_retained and teardown.journal_retained
    gc.collect()
    assert not [warning for warning in recwarn if warning.category is ResourceWarning]


@pytest.mark.parametrize(
    "point",
    [
        "anchor_identity_known",
        "executor_observed",
        "retirement_blocked",
        "supervisor_identity_observed",
        "unconfirmed_persisted",
        "unconfirmed_certified",
        "evidence_captured",
    ],
)
def test_wedged_python_exceptions_retain_live_chain_until_test_teardown(
    point: str,
    monkeypatch: pytest.MonkeyPatch,
    recwarn: pytest.WarningsRecorder,
) -> None:
    """A wedged-flow assertion error must retain the live supervising parent."""
    before = supervisor_probe._retained_actor_chain_count()

    def inject(flow: str, checkpoint: str) -> None:
        if flow == "wedged" and checkpoint == point:
            raise _InjectedPythonFailure(point)

    monkeypatch.setattr(supervisor_probe, "_python_exception_checkpoint", inject)
    with pytest.raises(_InjectedPythonFailure, match=point):
        run_lifecycle_scenario("wedged_supervisor")

    assert supervisor_probe._retained_actor_chain_count() == before + 1
    try:
        retained = supervisor_probe._inspect_last_retained_actor_chain()
        assert retained.flow == "wedged"
        assert retained.canonical_state == "UNCONFIRMED"
        assert retained.actor_live_or_task4_reaped
        assert retained.supervisor_task4_reaped is False
        assert retained.anchor_identity_exact
        assert retained.workdir_retained and retained.journal_retained
    finally:
        teardown = supervisor_probe._test_release_last_retained_actor_chain()
    assert teardown.group_absent
    assert teardown.supervisor_child_reaped
    assert teardown.untracked_orphan_count == 0
    assert teardown.workdir_retained and teardown.journal_retained
    gc.collect()
    assert not [warning for warning in recwarn if warning.category is ResourceWarning]


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
