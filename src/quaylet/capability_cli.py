from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from quaylet.agent_sdk_backend import AgentSdkBackend
from quaylet.claude_p_backend import ClaudePBackend
from quaylet.domain import (
    BackendEvent,
    BackendFailure,
    CanonicalMessage,
    CanonicalRequest,
    CapabilityReport,
    Completed,
    TextDelta,
)

_BACKENDS = ("agent-sdk", "claude-p")
_PROBE_TEXT = "Reply with exactly: proxy-ok"


class CapabilityBackend(Protocol):
    def structural_report(self) -> CapabilityReport: ...

    def stream(self, request: CanonicalRequest) -> AsyncIterator[BackendEvent]: ...


type BackendFactory = Callable[[str, Path], CapabilityBackend]
type Clock = Callable[[], float]


def report_payload(report: CapabilityReport) -> dict[str, object]:
    return {
        "backend": report.backend,
        "authentication": report.authentication,
        "streaming": report.streaming,
        "prompt_construction": report.prompt_construction,
        "multi_turn": report.multi_turn,
        "structured_tools": report.structured_tools,
        "single_turn_text_viable": report.single_turn_text_viable,
        "compatibility_proxy_viable": report.compatibility_proxy_viable,
        "agent_harness_viable": report.agent_harness_viable,
        "evidence": list(report.evidence),
        "metrics": {
            "latency_ms": None,
            "time_to_first_delta_ms": None,
            "text_delta_count": 0,
            "subprocess_count": None,
            "output_exact": None,
        },
    }


def _production_backend(name: str, claude_path: Path) -> CapabilityBackend:
    if name == "agent-sdk":
        return AgentSdkBackend()
    return ClaudePBackend(claude_path)


def _milliseconds(start: float, end: float) -> int:
    return round((end - start) * 1000)


def _fail_closed_payload(backend: str, error: Exception) -> dict[str, object]:
    return report_payload(
        CapabilityReport(
            backend=backend,
            authentication="fail",
            streaming="fail",
            prompt_construction="fail",
            multi_turn="fail",
            structured_tools="fail",
            evidence=(type(error).__name__,),
        )
    )


async def _live_payload(
    backend: CapabilityBackend,
    report: CapabilityReport,
    model: str,
    clock: Clock,
) -> tuple[dict[str, object], bool]:
    request = CanonicalRequest(
        model=model,
        system="",
        messages=(CanonicalMessage("user", _PROBE_TEXT),),
    )
    start = clock()
    first_delta: float | None = None
    finished: float | None = None
    texts: list[str] = []
    event_types: list[str] = []
    try:
        async for event in backend.stream(request):
            if finished is not None:
                raise BackendFailure("backend event after completion")
            event_types.append(type(event).__name__)
            if isinstance(event, TextDelta):
                if first_delta is None:
                    first_delta = clock()
                texts.append(event.text)
            elif isinstance(event, Completed):
                finished = clock()
        if finished is None:
            raise BackendFailure("backend stream ended without completion")
    except Exception as error:
        failed_at = clock()
        failed = replace(
            report,
            authentication="fail",
            streaming="fail",
            evidence=(type(error).__name__,),
        )
        payload = report_payload(failed)
        payload["metrics"] = {
            "latency_ms": _milliseconds(start, failed_at),
            "time_to_first_delta_ms": (
                None
                if first_delta is None
                else _milliseconds(start, first_delta)
            ),
            "text_delta_count": len(texts),
            "subprocess_count": 1 if report.backend == "claude-p" else None,
            "output_exact": None,
        }
        return payload, True

    observed = replace(
        report,
        authentication="pass",
        streaming="pass" if texts else "fail",
        evidence=(*report.evidence, *event_types),
    )
    payload = report_payload(observed)
    payload["metrics"] = {
        "latency_ms": _milliseconds(start, finished),
        "time_to_first_delta_ms": (
            None if first_delta is None else _milliseconds(start, first_delta)
        ),
        "text_delta_count": len(texts),
        "subprocess_count": 1 if report.backend == "claude-p" else None,
        "output_exact": "".join(texts) == "proxy-ok",
    }
    return payload, False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=(*_BACKENDS, "all"), required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--claude-path", type=Path, default=Path("claude"))
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    backend_factory: BackendFactory = _production_backend,
    clock: Clock = time.monotonic,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.live and not (
        os.environ.get("QUAYLET_LIVE") == "1"
        and args.model is not None
        and args.model.strip()
    ):
        parser.error(
            "--live requires QUAYLET_LIVE=1 and a non-whitespace --model"
        )

    names = _BACKENDS if args.backend == "all" else (args.backend,)
    failed = False
    if args.live:
        results = []
        for name in names:
            try:
                backend = backend_factory(name, args.claude_path)
                payload, backend_failed = asyncio.run(
                    _live_payload(
                        backend, backend.structural_report(), args.model, clock
                    )
                )
            except Exception as error:
                payload, backend_failed = _fail_closed_payload(name, error), True
            results.append(payload)
            failed = failed or backend_failed
    else:
        results = []
        for name in names:
            try:
                backend = backend_factory(name, args.claude_path)
                payload, backend_failed = (
                    report_payload(backend.structural_report()),
                    False,
                )
            except Exception as error:
                payload, backend_failed = _fail_closed_payload(name, error), True
            results.append(payload)
            failed = failed or backend_failed

    output: object = results if args.backend == "all" else results[0]
    print(json.dumps(output, indent=None if args.json else 2))
    return 1 if failed else 0
