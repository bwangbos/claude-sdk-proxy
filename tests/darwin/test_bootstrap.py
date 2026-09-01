"""Native supervisor bootstrap and bounded control-frame proofs."""

from __future__ import annotations

import hashlib
from pathlib import Path

from claude_sdk_proxy.environment import (
    EnvironmentConfig,
    build_child_environment,
)
from claude_sdk_proxy.supervisor_probe import (
    run_bootstrap_environment,
    run_lifecycle_scenario,
)


def _fingerprints(environment: dict[str, str]) -> dict[str, str]:
    return {
        name: hashlib.sha256(value.encode("utf-8")).hexdigest()
        for name, value in environment.items()
    }


def test_native_bootstrap_environment_matches_python_allowlist(tmp_path: Path) -> None:
    """A native allowlist drift must change the independently derived fingerprint."""
    cli_dir = Path(__file__).resolve().parents[2] / "build/bin"
    source = {
        "HOME": str(tmp_path),
        "USER": "native-probe-user",
        "LANG": "en_US.UTF-8",
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-config"),
        "HTTP_PROXY": "http://127.0.0.1:8123",
        "NO_PROXY": "127.0.0.1",
        "RANDOM_HOST_VALUE": "must-not-survive",
        "LOCAL_PROXY_MASTER_KEY": "must-not-survive",
    }
    config = EnvironmentConfig(cli_dir=cli_dir, network_proxy=True)

    evidence = run_bootstrap_environment(source, config)
    expected = build_child_environment(source, config)

    assert dict(evidence.child_environment_fingerprints) == _fingerprints(expected)
    assert evidence.bootstrap_environment_removed
    assert evidence.environment_values_recorded is False
    assert evidence.anchor_invocation_exact
    assert evidence.cli_exec_count == 1
    assert evidence.control_trace == (
        "SUPERVISOR_IDENTITY",
        "IDENTITY_ACK",
        "ANCHOR_IDENTITY",
        "ANCHOR_ACK",
        "CLI_ARMED",
        "ARMED_ACK",
        "CLI_RUNNING",
    )
    assert evidence.ack_after_durable_certification
    assert evidence.exact_canonical_ack_heads
    assert evidence.canonical_control_types == (
        "SUPERVISOR_IDENTITY",
        "ANCHOR_IDENTITY",
        "CLI_ARMED",
        "CLI_RUNNING",
    )
    assert evidence.canonical_control_sequences == (1, 2, 3, 4)
    assert evidence.post_exec_identity_verified
    assert evidence.cli_control_fd_closed_on_exec
    assert evidence.network_proxy_selector_authenticated
    assert evidence.probe_mode_collision_impossible


def test_control_frame_round_trip_and_validation_are_bounded() -> None:
    """A malformed or reordered frame must fail before advancing a child gate."""
    evidence = run_lifecycle_scenario("control_frame_validation")

    assert evidence.outcome == "unconfirmed"
    assert evidence.control_payload_limit == 4096
    assert set(evidence.control_rejections) == {
        "unknown_type",
        "wrong_nonce",
        "duplicate_phase",
        "phase_regression",
        "oversize_payload",
        "bad_checksum",
    }
    assert evidence.ack_without_certification_rejected
    assert evidence.same_canonical_journal
    assert evidence.evidence_observed_not_inferred
    assert evidence.next_stage_spawned is False
    assert evidence.unsafe_numeric_signal_count == 0


def test_cleanup_ack_is_exactly_once_and_bound_to_the_callers_request() -> None:
    """Replay, stale, or altered ACK proof must never release cleanup action."""
    evidence = run_lifecycle_scenario("control_frame_validation")

    assert set(evidence.cleanup_ack_rejections) == {
        "duplicate",
        "stale_sequence",
        "wrong_hash",
    }
    assert evidence.cleanup_ack_accept_count == 1
    assert evidence.cleanup_action_release_count == 1
    assert evidence.cleanup_ack_phase_latched
    assert evidence.rejected_cleanup_ack_no_action
    assert set(evidence.cleanup_request_binding_rejections) == {
        "wrong_hash",
        "wrong_sequence",
    }
    assert evidence.rejected_cleanup_request_no_signal


def test_bootstrap_control_eof_is_fail_dead_before_next_stage() -> None:
    """Losing bootstrap control before identity ACK must not spawn the anchor."""
    evidence = run_lifecycle_scenario("supervisor_before_identity")

    assert evidence.outcome == "unconfirmed"
    assert evidence.fail_dead_exit_code == 75
    assert evidence.next_stage_spawned is False
    assert evidence.cli_exec_count == 0
    assert evidence.same_canonical_journal
    assert evidence.evidence_observed_not_inferred
    assert evidence.artifacts_retained
    assert evidence.workdir_removed is False
    assert evidence.unsafe_numeric_signal_count == 0


def test_ordinary_argv_cannot_select_a_test_probe_mode() -> None:
    """Untrusted ordinary CLI arguments must never intercept the shim itself."""
    evidence = run_lifecycle_scenario("ordinary_probe_argument_collision")

    assert evidence.probe_mode_collision_impossible
    assert evidence.outcome == "done"
    assert evidence.cli_exec_count == 1
    assert evidence.canonical_control_sequences == (1, 2, 3, 4)
    assert evidence.cleanup_request_authenticated
    assert evidence.cleanup_completed_steps == 0xF
    assert evidence.durable_delete_receipt
    assert evidence.same_canonical_journal
