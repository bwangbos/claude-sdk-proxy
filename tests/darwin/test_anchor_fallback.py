"""Supervisorless anchor behavior stays persistently fail-closed."""

from __future__ import annotations

import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from claude_sdk_proxy import supervisor_probe
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
    assert result.shared_proxy_fallback_channel
    assert result.private_internal_relay_fd
    assert result.external_control_fd_closed_on_cli_exec
    assert result.internal_control_fd_closed_on_cli_exec
    assert len(retained) == 1
    retained_instance = retained.pop()
    assert (retained_instance / "allocation.journal").is_file()
    assert (retained_instance / "allocation.workdir").is_dir()


def test_pre_armed_anchor_control_loss_fails_dead_without_cli_release() -> None:
    """Fail-dead cleanup is safe only while the durable state proves no CLI release."""
    result = run_lifecycle_scenario("pre_armed_fail_dead")

    assert result.outcome == "unconfirmed"
    assert result.fail_dead_exit_code == 75
    assert result.cli_exec_count == 0
    assert result.anchor_alive is False
    assert result.workdir_removed is False
    assert result.artifacts_retained
    assert result.unsafe_numeric_signal_count == 0
    assert result.same_canonical_journal
    assert result.evidence_observed_not_inferred


@pytest.mark.parametrize(
    "scenario",
    [
        "fallback_while_supervisor_healthy",
        "fallback_early_before_running",
        "fallback_nonempty_payload_after_loss",
        "fallback_wrong_phase_after_loss",
    ],
)
def test_invalid_fallback_request_is_rejected_without_a_signal(scenario: str) -> None:
    """A fallback frame without exact post-loss authority must not signal the group."""
    result = run_lifecycle_scenario(scenario)

    assert result.outcome == "unconfirmed"
    assert result.fallback_request_rejected
    assert result.rejected_request_no_signal
    assert result.term_used is False
    assert result.self_term_request_authenticated is False
    assert result.workdir_removed is False
    assert result.unsafe_numeric_signal_count == 0
    assert result.shared_proxy_fallback_channel


def test_anchor_does_not_read_proxy_channel_while_supervisor_is_live() -> None:
    """The retaining supervisor must be the sole reader before internal EOF."""
    result = run_lifecycle_scenario("fallback_while_supervisor_healthy")

    assert result.external_control_read_by_supervisor_while_live
    assert result.anchor_external_read_count_while_supervisor_live == 0
    assert result.rejected_request_no_signal
    assert result.self_term_signal_count == 0


def test_duplicate_fallback_request_is_ignored_after_one_authenticated_signal() -> None:
    """Replaying SELF_TERM_REQUEST must not append or signal a second time."""
    result = run_lifecycle_scenario("fallback_duplicate_after_loss")

    assert result.outcome == "unconfirmed"
    assert result.supervisor_loss_proven
    assert result.self_term_request_authenticated
    assert result.fallback_payload_exact
    assert result.fallback_request_rejected
    assert result.rejected_request_no_signal
    assert result.self_term_signal_count == 1
    assert result.workdir_removed is False


def test_fixed_relay_fd_collision_preserves_supervisor_loss_fallback() -> None:
    """Relocating proxy FD 198 must preserve the anchor's authenticated endpoint."""
    result = run_lifecycle_scenario("fallback_control_fd_198_after_loss")

    assert result.outcome == "unconfirmed"
    assert result.external_control_relocated_from_fixed_fd
    assert result.shared_proxy_fallback_channel
    assert result.supervisor_loss_proven
    assert result.self_term_request_authenticated
    assert result.fallback_payload_exact
    assert result.self_term_signal_count == 1
    assert result.external_control_fd_closed_on_cli_exec
    assert result.internal_control_fd_closed_on_cli_exec


def test_anchor_group_exits_when_its_last_controller_is_killed(
    tmp_path: Path,
) -> None:
    identity_path = tmp_path / "anchor-identity"
    controller = (
        "import os, signal, sys\n"
        "from claude_sdk_proxy import supervisor_probe as probe\n"
        "def checkpoint(flow, point):\n"
        "    if flow == 'actor_loss' and point == 'actor_loss_observed':\n"
        "        key = probe._retained_actor_chain_keys()[0]\n"
        "        owner = probe._RETAINED_ACTOR_REGISTRY.get(key)\n"
        "        fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | "
        "os.O_EXCL, 0o600)\n"
        "        os.write(fd, f'{owner.anchor[0]} {owner.anchor[3]}'.encode())\n"
        "        os.fsync(fd)\n"
        "        os.close(fd)\n"
        "        os.kill(os.getpid(), signal.SIGKILL)\n"
        "probe._python_exception_checkpoint = checkpoint\n"
        "probe.run_lifecycle_scenario('cleanup_fail_after_admission')\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", controller, str(identity_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    _, diagnostics = process.communicate(timeout=15)
    assert process.returncode == -signal.SIGKILL, diagnostics.decode()
    anchor_pid, pgid = (int(value) for value in identity_path.read_text().split())
    group_absent = False

    try:
        group_absent = supervisor_probe._fresh_group_is_absent(pgid)
    finally:
        if not group_absent:
            assert supervisor_probe._teardown_recorded_fresh_group(pgid)

    assert group_absent, f"orphaned anchor {anchor_pid} in group {pgid}"
