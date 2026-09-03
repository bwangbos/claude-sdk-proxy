from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

import pytest

from claude_sdk_proxy import capability_cli
from claude_sdk_proxy.domain import (
    BackendEvent,
    BackendFailure,
    CapabilityReport,
    Completed,
    TextDelta,
)


class FakeBackend(Protocol):
    def structural_report(self) -> CapabilityReport: ...

    def stream(self, request: object) -> AsyncIterator[BackendEvent]: ...


def structural_report(backend: str) -> CapabilityReport:
    return CapabilityReport(
        backend=backend,
        authentication="untested",
        streaming="untested",
        prompt_construction="pass",
        multi_turn="fail",
        structured_tools="fail",
        evidence=(f"{backend} structural evidence",),
    )


def test_all_json_reports_literal_structural_capabilities_in_backend_order(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class StructuralBackend:
        def __init__(self, name: str) -> None:
            self.name = name

        def structural_report(self) -> CapabilityReport:
            return structural_report(self.name)

        async def stream(self, request: object) -> AsyncIterator[BackendEvent]:
            del request
            raise AssertionError("structural reporting must not launch a stream")
            yield

    def factory(name: str, path: Path) -> StructuralBackend:
        assert path == Path("claude")
        return StructuralBackend(name)

    assert (
        capability_cli.main(
            ["--backend", "all", "--json"], backend_factory=factory
        )
        == 0
    )

    reports = json.loads(capsys.readouterr().out)
    assert [report["backend"] for report in reports] == ["agent-sdk", "claude-p"]
    for report in reports:
        assert {
            "authentication": report["authentication"],
            "streaming": report["streaming"],
            "prompt_construction": report["prompt_construction"],
            "multi_turn": report["multi_turn"],
            "structured_tools": report["structured_tools"],
            "single_turn_text_viable": report["single_turn_text_viable"],
            "compatibility_proxy_viable": report["compatibility_proxy_viable"],
            "agent_harness_viable": report["agent_harness_viable"],
        } == {
            "authentication": "untested",
            "streaming": "untested",
            "prompt_construction": "pass",
            "multi_turn": "fail",
            "structured_tools": "fail",
            "single_turn_text_viable": False,
            "compatibility_proxy_viable": False,
            "agent_harness_viable": False,
        }
        assert report["metrics"] == {
            "latency_ms": None,
            "time_to_first_delta_ms": None,
            "text_delta_count": 0,
            "subprocess_count": None,
            "output_exact": None,
        }


def test_live_gate_exits_two_before_backend_construction(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_PROXY_LIVE", raising=False)

    def forbidden_factory(name: str, path: Path) -> FakeBackend:
        del name, path
        raise AssertionError("live gate must run before backend construction")

    with pytest.raises(SystemExit) as error:
        capability_cli.main(
            ["--backend", "agent-sdk", "--live", "--model", "test-model"],
            backend_factory=forbidden_factory,
        )

    assert error.value.code == 2
    message = capsys.readouterr().err
    assert "CLAUDE_PROXY_LIVE=1" in message
    assert "--model" in message


def test_live_success_updates_only_observed_statuses_and_metrics(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_PROXY_LIVE", "1")

    class SuccessfulBackend:
        def structural_report(self) -> CapabilityReport:
            return structural_report("agent-sdk")

        async def stream(self, request: object) -> AsyncIterator[BackendEvent]:
            yield TextDelta("proxy-")
            yield TextDelta("ok")
            yield Completed("end_turn", {"output_tokens": 1})

    times = iter((10.0, 10.007, 10.020))

    assert (
        capability_cli.main(
            [
                "--backend",
                "agent-sdk",
                "--live",
                "--model",
                "test-model",
                "--json",
            ],
            backend_factory=lambda name, path: SuccessfulBackend(),
            clock=lambda: next(times),
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    assert report == {
        "backend": "agent-sdk",
        "authentication": "pass",
        "streaming": "pass",
        "prompt_construction": "pass",
        "multi_turn": "fail",
        "structured_tools": "fail",
        "single_turn_text_viable": True,
        "compatibility_proxy_viable": False,
        "agent_harness_viable": False,
        "evidence": [
            "agent-sdk structural evidence",
            "TextDelta",
            "TextDelta",
            "Completed",
        ],
        "metrics": {
            "latency_ms": 20,
            "time_to_first_delta_ms": 7,
            "text_delta_count": 2,
            "subprocess_count": None,
            "output_exact": True,
        },
    }


def test_live_typed_failure_serializes_only_exception_class(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_PROXY_LIVE", "1")
    secret = "sensitive-backend-error"

    class FailingBackend:
        def structural_report(self) -> CapabilityReport:
            return structural_report("claude-p")

        async def stream(self, request: object) -> AsyncIterator[BackendEvent]:
            raise BackendFailure(secret)
            yield

    times = iter((20.0, 20.012))

    assert (
        capability_cli.main(
            [
                "--backend",
                "claude-p",
                "--live",
                "--model",
                "test-model",
                "--claude-path",
                "/custom/claude",
                "--json",
            ],
            backend_factory=lambda name, path: FailingBackend(),
            clock=lambda: next(times),
        )
        == 1
    )

    output = capsys.readouterr().out
    assert secret not in output
    report = json.loads(output)
    assert report["authentication"] == "fail"
    assert report["streaming"] == "fail"
    assert report["evidence"] == ["BackendFailure"]
    assert report["single_turn_text_viable"] is False
    assert report["metrics"] == {
        "latency_ms": 12,
        "time_to_first_delta_ms": None,
        "text_delta_count": 0,
        "subprocess_count": 1,
        "output_exact": None,
    }
