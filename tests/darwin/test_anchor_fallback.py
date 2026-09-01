"""Supervisorless anchor behavior stays persistently fail-closed."""

from __future__ import annotations

import tempfile
from pathlib import Path

from claude_sdk_proxy.supervisor_probe import run_lifecycle_scenario


def test_supervisorless_running_anchor_stays_unconfirmed() -> None:
    """A RUNNING anchor without its retaining parent must never gain force authority."""
    before = set(Path(tempfile.gettempdir()).glob("claude-real-fallback-*"))
    result = run_lifecycle_scenario("kill_supervisor_after_running")
    retained = set(Path(tempfile.gettempdir()).glob("claude-real-fallback-*")) - before
    assert result.outcome == "unconfirmed"
    assert result.anchor_alive and not result.stop_or_kill_used
    assert not result.workdir_removed and not result.helper_promoted
    assert result.term_used
    assert set(result.signal_authorities) == {"authenticated_self_control"}
    assert result.unconfirmed_persistent
    assert result.exit_refused
    assert result.artifacts_retained
    assert result.durable_delete_receipt is False
    assert result.unsafe_numeric_signal_count == 0
    assert result.self_term_request_authenticated
    assert result.same_canonical_journal
    assert result.evidence_observed_not_inferred
    assert result.control_fd_phase_enforced
    assert len(retained) == 1
    retained_instance = retained.pop()
    assert (retained_instance / "allocation.journal").is_file()
    assert (retained_instance / "allocation.workdir").is_dir()


def test_pre_armed_anchor_control_loss_fails_dead_without_cli_release() -> None:
    """Fail-dead cleanup is safe only while the durable state proves no CLI release."""
    result = run_lifecycle_scenario("pre_armed_fail_dead")

    assert result.outcome == "done"
    assert result.fail_dead_exit_code == 75
    assert result.cli_exec_count == 0
    assert result.anchor_alive is False
    assert result.workdir_removed
    assert result.unsafe_numeric_signal_count == 0
