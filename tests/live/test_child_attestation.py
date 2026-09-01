"""Opt-in per-child existing-login attestation rows.

These rows intentionally remain blocked at the execution-time policy/runtime
gates.  Deterministic public ``SystemMessage(subtype="init")`` substitutes live
in ``tests/unit/test_attestation.py`` and are not promoted to live evidence.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _require_exact_runtime_and_affirmative_policy() -> None:
    if os.environ.get("RUN_LIVE_CLAUDE_TESTS") != "1":
        pytest.skip("set RUN_LIVE_CLAUDE_TESTS=1 for opt-in live rows")
    pytest.fail(
        "live child attestation is blocked before child construction: "
        "policy evidence is non-affirmative and installed CLI 2.1.252 does not "
        "match pinned CLI 2.1.251"
    )


def test_existing_login_init_opens_each_live_child_gate() -> None:
    """Reserved for an exact-runtime, affirmative-policy public init trace."""
    raise AssertionError("live prerequisite fixture must run before caller content")


@pytest.mark.parametrize(
    "negative_mode",
    [
        "api_key",
        "api_key_helper",
        "oauth",
        "cloud_provider",
        "custom_endpoint",
        "missing",
        "duplicate",
        "unknown",
    ],
)
def test_negative_live_auth_modes_close_without_a_model_turn(
    negative_mode: str,
) -> None:
    """Reserved for gated live public evidence; no mode may release content."""
    raise AssertionError(
        f"live prerequisite fixture must block {negative_mode} before content"
    )
