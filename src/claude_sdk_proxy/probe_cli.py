"""Content-free command line interface for Phase 0 probes."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence

from claude_sdk_proxy.probes import (
    MAX_REDACTED_REPORT_BYTES,
    ProbeResult,
    ProbeUnavailable,
    run_compaction_probe,
    run_prompt_purity_probe,
)

_FALLBACK_JSON = (
    '{"evidence":{"reason_code":"redaction_failure"},'
    '"name":"purity","passed":false,"schema_version":1}\n'
)


def _write_fallback() -> bool:
    try:
        sys.stdout.write(_FALLBACK_JSON)
    except BaseException:
        return False
    return False


def _emit(result: ProbeResult) -> bool:
    try:
        payload = json.dumps(
            result.redacted_dict(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        encoded = (payload + "\n").encode("utf-8")
        if len(encoded) > MAX_REDACTED_REPORT_BYTES:
            return _write_fallback()
        sys.stdout.write(payload + "\n")
        return True
    except BaseException:
        return _write_fallback()


def _false_result(name: str, reason_code: str) -> ProbeResult:
    return ProbeResult(name, False, {"reason_code": reason_code})


def _main(argv: Sequence[str] | None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--claim-live-success" in arguments:
        name = "compaction" if arguments[:1] == ["compaction"] else "purity"
        if not _emit(_false_result(name, "live_success_claim_forbidden")):
            return 5
        return 2
    if len(arguments) != 1 or arguments[0] not in {"prompt-purity", "compaction"}:
        if not _emit(_false_result("purity", "invalid_invocation")):
            return 5
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
        if not _emit(_false_result(name, "child_attestation_unavailable")):
            return 5
        return 3
    except BaseException:
        if not _emit(_false_result(name, "probe_internal_error")):
            return 5
        return 4
    if not _emit(result):
        return 5
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run one probe, emitting exactly one redacted JSON object."""
    try:
        return _main(argv)
    except BaseException:
        _write_fallback()
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
