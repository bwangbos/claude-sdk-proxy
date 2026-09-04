from __future__ import annotations

import json
import os
from typing import Any

import pytest

from claude_sdk_proxy.capability_cli import main

pytestmark = pytest.mark.live


def _model_or_skip() -> str:
    if os.environ.get("CLAUDE_PROXY_LIVE") != "1":
        pytest.skip("set CLAUDE_PROXY_LIVE=1 to enable the capability probe")
    model = os.environ.get("CLAUDE_PROXY_MODEL")
    if not model:
        pytest.skip("set nonempty CLAUDE_PROXY_MODEL for the capability probe")
    return model


def _assert_passing_probe(report: dict[str, Any], backend: str) -> None:
    assert report["backend"] == backend
    assert report["authentication"] == "pass"
    assert report["streaming"] == "pass"
    assert report["single_turn_text_viable"] is True
    assert report["compatibility_proxy_viable"] is False
    assert report["agent_harness_viable"] is False
    assert report["metrics"]["latency_ms"] >= 0
    assert report["metrics"]["time_to_first_delta_ms"] >= 0
    assert report["metrics"]["text_delta_count"] >= 1
    assert report["metrics"]["output_exact"] is True
    assert report["evidence"][-1] == "Completed"
    assert "TextDelta" in report["evidence"]


def test_agent_sdk_live_single_turn_text_probe(
    capsys: pytest.CaptureFixture[str],
) -> None:
    model = _model_or_skip()

    assert (
        main(
            ["--backend", "agent-sdk", "--live", "--model", model, "--json"]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    _assert_passing_probe(report, "agent-sdk")
    assert report["metrics"]["subprocess_count"] is None


def test_claude_p_live_single_turn_text_probe(
    capsys: pytest.CaptureFixture[str],
) -> None:
    model = _model_or_skip()

    assert (
        main(
            ["--backend", "claude-p", "--live", "--model", model, "--json"]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    _assert_passing_probe(report, "claude-p")
    assert report["metrics"]["subprocess_count"] == 1
