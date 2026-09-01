"""Crash and ownership-reconciliation matrices for the native lifecycle."""

from __future__ import annotations

import gc
import subprocess

import pytest

from claude_sdk_proxy import supervisor_probe
from claude_sdk_proxy.supervisor_probe import run_lifecycle_scenario


class _InjectedPythonFailure(RuntimeError):
    pass


def _only_new_retained_key(before: set[object]) -> object:
    after = set(supervisor_probe._retained_actor_chain_keys())
    new = after - before
    assert len(new) == 1
    return new.pop()


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
    before = set(supervisor_probe._retained_actor_chain_keys())

    def inject(flow: str, checkpoint: str) -> None:
        if flow == "actor_loss" and checkpoint == point:
            raise _InjectedPythonFailure(point)

    monkeypatch.setattr(supervisor_probe, "_python_exception_checkpoint", inject)
    with pytest.raises(_InjectedPythonFailure, match=point):
        run_lifecycle_scenario(scenario)

    key = _only_new_retained_key(before)
    try:
        retained = supervisor_probe._inspect_retained_actor_chain(key)
        assert retained.flow == "actor_loss"
        if point in {"cleanup_request_sent", "cleanup_ack_accepted"}:
            assert retained.canonical_state in {"BATCH_ACTIVE", "UNCONFIRMED"}
        else:
            assert retained.canonical_state == "UNCONFIRMED"
        assert retained.actor_live_or_task4_reaped
        if point in {"anchor_identity_known", "cleanup_request_prepared"}:
            assert retained.supervisor_task4_reaped is False
        elif point in {"cleanup_request_sent", "cleanup_ack_accepted"}:
            # A complete request or ACK proves possible delivery, not that the
            # injected native actor has reached its deterministic exit yet.
            pass
        else:
            assert retained.supervisor_task4_reaped
        assert retained.anchor_identity_exact
        assert retained.workdir_retained and retained.journal_retained
    finally:
        teardown = supervisor_probe._test_release_retained_actor_chain(key)
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
    before = set(supervisor_probe._retained_actor_chain_keys())

    def inject(flow: str, checkpoint: str) -> None:
        if flow == "wedged" and checkpoint == point:
            raise _InjectedPythonFailure(point)

    monkeypatch.setattr(supervisor_probe, "_python_exception_checkpoint", inject)
    with pytest.raises(_InjectedPythonFailure, match=point):
        run_lifecycle_scenario("wedged_supervisor")

    key = _only_new_retained_key(before)
    try:
        retained = supervisor_probe._inspect_retained_actor_chain(key)
        assert retained.flow == "wedged"
        assert retained.canonical_state == "UNCONFIRMED"
        assert retained.actor_live_or_task4_reaped
        assert retained.supervisor_task4_reaped is False
        assert retained.anchor_identity_exact
        assert retained.workdir_retained and retained.journal_retained
    finally:
        teardown = supervisor_probe._test_release_retained_actor_chain(key)
    assert teardown.group_absent
    assert teardown.supervisor_child_reaped
    assert teardown.untracked_orphan_count == 0
    assert teardown.workdir_retained and teardown.journal_retained
    gc.collect()
    assert not [warning for warning in recwarn if warning.category is ResourceWarning]


@pytest.mark.parametrize(
    "point",
    [
        "owner_registered",
        "cleanup_write_before_first_byte",
        "cleanup_write_after_partial",
        "cleanup_write_after_full",
        "cleanup_ack_received_before_accept",
    ],
)
def test_transfer_and_recovery_edge_failures_keep_one_keyed_owner(
    point: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every transfer/write/recovery exception leaves one retryable owner."""
    before = set(supervisor_probe._retained_actor_chain_keys())

    def inject(flow: str, checkpoint: str) -> None:
        if flow == "actor_loss" and checkpoint == point:
            raise _InjectedPythonFailure(point)

    monkeypatch.setattr(supervisor_probe, "_python_exception_checkpoint", inject)
    with pytest.raises(_InjectedPythonFailure, match=point):
        run_lifecycle_scenario("cleanup_fail_after_admission")

    key = _only_new_retained_key(before)
    inspection = supervisor_probe._inspect_retained_actor_chain(key)
    if point in {"cleanup_write_after_full", "cleanup_ack_received_before_accept"}:
        assert inspection.canonical_state in {"BATCH_ACTIVE", "UNCONFIRMED"}
    else:
        assert inspection.canonical_state == "UNCONFIRMED"
    assert inspection.owner_count_for_key == 1
    assert inspection.journal_reopened_and_certified

    monkeypatch.setattr(
        supervisor_probe,
        "_python_exception_checkpoint",
        lambda flow, checkpoint: None,
    )
    supervisor_probe._reconcile_retained_actor_chain(key)
    supervisor_probe._reconcile_retained_actor_chain(key)
    inspection = supervisor_probe._inspect_retained_actor_chain(key)
    assert inspection.owner_count_for_key == 1
    assert inspection.canonical_state == "UNCONFIRMED"
    teardown = supervisor_probe._test_release_retained_actor_chain(key)
    assert teardown.group_absent
    assert teardown.supervisor_child_reaped
    assert teardown.untracked_orphan_count == 0


@pytest.mark.parametrize(
    "point",
    [
        "recovery_head_certified",
        "recovery_exit_observed",
        "recovery_executor_retired",
        "recovery_reap_receipt_retained",
        "recovery_batch_reconciled",
        "recovery_unconfirmed_persisted",
        "recovery_unconfirmed_certified",
    ],
)
def test_recovery_edge_failure_keeps_owner_and_retry_converges(
    point: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nested recovery failure never masks or discards the retained owner."""
    before = set(supervisor_probe._retained_actor_chain_keys())
    recovery_fault_seen = False

    def inject(flow: str, checkpoint: str) -> None:
        nonlocal recovery_fault_seen
        if flow != "actor_loss":
            return
        if checkpoint == "actor_loss_observed":
            raise _InjectedPythonFailure("trigger-recovery")
        if checkpoint == point:
            recovery_fault_seen = True
            raise RuntimeError(point)

    monkeypatch.setattr(supervisor_probe, "_python_exception_checkpoint", inject)
    with pytest.raises(_InjectedPythonFailure, match="trigger-recovery"):
        run_lifecycle_scenario(
            "cleanup_fail_after_stop"
            if point == "recovery_batch_reconciled"
            else "cleanup_fail_after_admission"
        )
    assert recovery_fault_seen

    key = _only_new_retained_key(before)
    assert supervisor_probe._inspect_retained_actor_chain(key).owner_count_for_key == 1
    monkeypatch.setattr(
        supervisor_probe,
        "_python_exception_checkpoint",
        lambda flow, checkpoint: None,
    )
    supervisor_probe._reconcile_retained_actor_chain(key)
    supervisor_probe._reconcile_retained_actor_chain(key)
    inspection = supervisor_probe._inspect_retained_actor_chain(key)
    assert inspection.canonical_state == "UNCONFIRMED"
    assert inspection.supervisor_task4_reaped
    teardown = supervisor_probe._test_release_retained_actor_chain(key)
    assert teardown.group_absent
    assert teardown.supervisor_child_reaped
    assert teardown.untracked_orphan_count == 0


@pytest.mark.parametrize(
    "point",
    [
        "release_journal_certified",
        "release_group_enumerated",
        "release_absence_certified",
    ],
)
def test_keyed_release_failure_is_retryable_without_double_teardown(
    point: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = set(supervisor_probe._retained_actor_chain_keys())

    def retain_after_registration(flow: str, checkpoint: str) -> None:
        if flow == "actor_loss" and checkpoint == "owner_registered":
            raise _InjectedPythonFailure("owner_registered")

    monkeypatch.setattr(
        supervisor_probe, "_python_exception_checkpoint", retain_after_registration
    )
    with pytest.raises(_InjectedPythonFailure, match="owner_registered"):
        run_lifecycle_scenario("cleanup_fail_after_admission")
    key = _only_new_retained_key(before)

    def inject_release(flow: str, checkpoint: str) -> None:
        if flow == "actor_loss" and checkpoint == point:
            raise _InjectedPythonFailure(point)

    monkeypatch.setattr(
        supervisor_probe, "_python_exception_checkpoint", inject_release
    )
    with pytest.raises(_InjectedPythonFailure, match=point):
        supervisor_probe._test_release_retained_actor_chain(key)
    assert supervisor_probe._inspect_retained_actor_chain(key).owner_count_for_key == 1

    monkeypatch.setattr(
        supervisor_probe,
        "_python_exception_checkpoint",
        lambda flow, checkpoint: None,
    )
    teardown = supervisor_probe._test_release_retained_actor_chain(key)
    assert teardown.group_absent
    assert teardown.supervisor_child_reaped
    assert teardown.untracked_orphan_count == 0
    assert key not in supervisor_probe._retained_actor_chain_keys()


def test_recovery_uses_canonical_request_and_process_not_delivery_boolean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = set(supervisor_probe._retained_actor_chain_keys())

    def inject(flow: str, checkpoint: str) -> None:
        if flow != "actor_loss":
            return
        if checkpoint == "cleanup_write_after_full":
            raise _InjectedPythonFailure(checkpoint)
        if checkpoint == "recovery_head_certified":
            raise RuntimeError(checkpoint)

    monkeypatch.setattr(supervisor_probe, "_python_exception_checkpoint", inject)
    with pytest.raises(_InjectedPythonFailure, match="cleanup_write_after_full"):
        run_lifecycle_scenario("cleanup_fail_after_admission")
    key = _only_new_retained_key(before)
    owner = supervisor_probe._RETAINED_ACTOR_REGISTRY.get(key)
    owner.cleanup_request_may_have_been_delivered = False

    monkeypatch.setattr(
        supervisor_probe,
        "_python_exception_checkpoint",
        lambda flow, checkpoint: None,
    )
    supervisor_probe._reconcile_retained_actor_chain(key)
    inspection = supervisor_probe._inspect_retained_actor_chain(key)
    assert inspection.canonical_state == "UNCONFIRMED"
    assert inspection.journal_reopened_and_certified
    assert inspection.actor_live_or_task4_reaped
    teardown = supervisor_probe._test_release_retained_actor_chain(key)
    assert teardown.group_absent and teardown.supervisor_child_reaped


def test_retained_registry_is_fixed_capacity_and_releases_by_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full ownership registry rejects before any process is spawned."""
    registry = supervisor_probe._RetainedActorRegistry(capacity=1)
    reservation = registry.reserve(bytes.fromhex("01" * 32))
    monkeypatch.setattr(supervisor_probe, "_RETAINED_ACTOR_REGISTRY", registry)

    spawned = False

    def forbidden_spawn(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        del args, kwargs
        nonlocal spawned
        spawned = True
        raise AssertionError("capacity rejection occurred after spawn")

    monkeypatch.setattr(supervisor_probe.subprocess, "Popen", forbidden_spawn)
    with pytest.raises(supervisor_probe.SupervisorProbeError, match="capacity"):
        run_lifecycle_scenario("wedged_supervisor")
    assert spawned is False
    reservation.cancel()
    assert registry.count == 0


def test_pre_spawn_instance_failure_rolls_back_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = supervisor_probe._RetainedActorRegistry(capacity=1)
    monkeypatch.setattr(supervisor_probe, "_RETAINED_ACTOR_REGISTRY", registry)

    def fail_instance(*args: object, **kwargs: object) -> str:
        del args, kwargs
        raise OSError("injected instance failure")

    monkeypatch.setattr(supervisor_probe.tempfile, "mkdtemp", fail_instance)
    with pytest.raises(OSError, match="injected instance failure"):
        run_lifecycle_scenario("wedged_supervisor")
    assert registry.count == 0


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
