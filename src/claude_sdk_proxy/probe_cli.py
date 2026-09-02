"""Content-free command line interface for Phase 0 probes."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence

from claude_sdk_proxy.probes import (
    ProbeResult,
    ProbeUnavailable,
    run_compaction_probe,
    run_prompt_purity_probe,
)


def _emit(result: ProbeResult) -> None:
    payload = json.dumps(
        result.redacted_dict(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    sys.stdout.write(payload + "\n")


def _false_result(name: str, reason_code: str) -> ProbeResult:
    return ProbeResult(name, False, {"reason_code": reason_code})


def main(argv: Sequence[str] | None = None) -> int:
    """Run one probe, emitting exactly one redacted JSON object."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--claim-live-success" in arguments:
        name = "compaction" if arguments[:1] == ["compaction"] else "purity"
        _emit(_false_result(name, "live_success_claim_forbidden"))
        return 2
    if len(arguments) != 1 or arguments[0] not in {"prompt-purity", "compaction"}:
        _emit(_false_result("purity", "invalid_invocation"))
        return 2

    command = arguments[0]
    name = "purity" if command == "prompt-purity" else "compaction"
    runner = (
        run_prompt_purity_probe
        if command == "prompt-purity"
        else run_compaction_probe
    )
    try:
        result = runner()
    except ProbeUnavailable:
        _emit(_false_result(name, "child_attestation_unavailable"))
        return 3
    except Exception:
        _emit(_false_result(name, "probe_internal_error"))
        return 4
    _emit(result)
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
