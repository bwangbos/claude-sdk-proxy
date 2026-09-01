"""Retaining-parent group cleanup scenarios using deterministic children."""

from __future__ import annotations

import pytest

from claude_sdk_proxy.supervisor_probe import run_lifecycle_scenario


@pytest.mark.parametrize(
    ("scenario", "expected_kill"),
    [
        ("ordinary_term_success", False),
        ("stubborn_child_kill", True),
        ("confirmed_reap", False),
    ],
)
def test_retaining_supervisor_owns_group_cleanup(
    scenario: str, expected_kill: bool
) -> None:
    """Removing the retaining parent must eliminate every forced-cleanup proof."""
    evidence = run_lifecycle_scenario(scenario)

    assert evidence.outcome == "done"
    assert evidence.supervisor_retained_anchor
    assert evidence.anchor_unreaped_through_absence
    assert evidence.group_enumerated_while_stopped
    assert evidence.stop_used and evidence.term_used
    assert evidence.kill_used is expected_kill
    assert evidence.group_absence_confirmed and evidence.anchor_reaped
    assert evidence.absence_enumerated_with_anchor_unreaped
    assert evidence.workdir_removed and evidence.durable_delete_receipt
    assert set(evidence.signal_authorities) == {"retained_parent_group"}
    assert evidence.unsafe_numeric_signal_count == 0
    assert evidence.cleanup_request_authenticated
    assert evidence.cleanup_task4_admitted
    assert evidence.cleanup_batch_count == 4
    assert evidence.cleanup_completed_steps == 0xF
    assert evidence.cleanup_done_sequence > evidence.canonical_control_sequences[-1]
    assert evidence.same_canonical_journal
    assert evidence.evidence_observed_not_inferred
    assert evidence.group_enumeration_complete
    assert evidence.control_fd_phase_enforced


@pytest.mark.parametrize(
    "scenario",
    ["altered_executable_identity", "reused_pid", "unexpected_descendant"],
)
def test_observed_or_mismatched_identity_never_authorizes_a_signal(
    scenario: str,
) -> None:
    """Replacing a recorded identity must leave observation non-authorizing."""
    evidence = run_lifecycle_scenario(scenario)

    assert evidence.outcome == "unconfirmed"
    assert evidence.identity_mismatch_detected
    assert evidence.stop_used is False
    assert evidence.term_used is False
    assert evidence.kill_used is False
    assert evidence.unsafe_numeric_signal_count == 0
    assert evidence.workdir_removed is False
    assert evidence.same_canonical_journal
    assert evidence.evidence_observed_not_inferred
    assert evidence.cleanup_rejection_authenticated
    assert evidence.observed_rejection_reason == scenario


def test_parent_held_zombie_prevents_anchor_identity_reuse_until_reap() -> None:
    """Reaping the anchor early must make the group-incarnation proof false."""
    evidence = run_lifecycle_scenario("parent_held_zombie_nonreuse")

    assert evidence.outcome == "done"
    assert evidence.anchor_zombie_observed
    assert evidence.anchor_unreaped_through_absence
    assert evidence.group_identity_reuse_before_reap is False
    assert evidence.anchor_reaped
    assert evidence.unsafe_numeric_signal_count == 0


@pytest.mark.parametrize(
    "boundary",
    [
        "cleanup_fail_after_admission",
        "cleanup_fail_after_stop",
        "cleanup_fail_after_enumeration",
        "cleanup_fail_after_cont",
        "cleanup_fail_after_term",
        "cleanup_fail_after_kill",
    ],
)
def test_cleanup_failure_retains_admitted_authority_and_never_freezes_group(
    boundary: str,
) -> None:
    """Losing the pre-effect token or exiting after STOP would orphan authority."""
    evidence = run_lifecycle_scenario(boundary)

    assert evidence.outcome == "done"
    assert evidence.cleanup_failure_injection_observed
    assert evidence.process_batch_admitted_before_signal
    assert evidence.process_batch_target_exact
    assert evidence.action_token_retained_through_signals
    assert evidence.frozen_group_left_behind is False
    if boundary in {"cleanup_fail_after_stop", "cleanup_fail_after_enumeration"}:
        assert evidence.group_resumed_after_failure
    assert evidence.group_absence_confirmed
    assert evidence.anchor_reaped
    assert evidence.cleanup_completed_steps == 0xF
    assert evidence.durable_delete_receipt
    assert evidence.unsafe_numeric_signal_count == 0


def test_anchor_only_stopped_enumeration_is_complete_and_safe() -> None:
    """An already-exited CLI must not turn complete anchor-only state into unknown."""
    evidence = run_lifecycle_scenario("anchor_only_cleanup")

    assert evidence.outcome == "done"
    assert evidence.anchor_only_group_observed
    assert evidence.group_enumerated_while_stopped
    assert evidence.group_enumeration_complete
    assert evidence.group_absence_confirmed
    assert evidence.anchor_reaped
    assert evidence.durable_delete_receipt
