"""Opt-in model/network pre-input boundary rows.

The current policy and runtime tuple prohibit executing a Claude child.  This
file therefore fails closed before construction when opt-in is requested and
does not count as passing feasibility evidence.
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
        "live pre-input trace is blocked before child construction: policy "
        "evidence is non-affirmative and installed CLI 2.1.252 does not match "
        "pinned CLI 2.1.251"
    )


@pytest.mark.parametrize(
    "change_boundary",
    ["startup", "connection", "first_turn", "later_turn"],
)
def test_live_profile_change_releases_zero_model_turns(
    change_boundary: str,
) -> None:
    """Reserved for a gated packet/control trace at the named boundary."""
    raise AssertionError(
        f"live prerequisite fixture must block {change_boundary} before content"
    )


def test_live_system_mcp_user_and_tool_canaries_wait_for_attestation() -> None:
    """Reserved for a zero-byte packet/control trace under the exact tuple."""
    raise AssertionError("live prerequisite fixture must run before caller content")
