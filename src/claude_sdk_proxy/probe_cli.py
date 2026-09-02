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

_FALLBACK_JSON = {
    "purity": (
        '{"evidence":{"reason_code":"redaction_failure"},'
        '"name":"purity","passed":false,"schema_version":1}\n'
    ),
    "compaction": (
        '{"evidence":{"reason_code":"redaction_failure"},'
        '"name":"compaction","passed":false,"schema_version":1}\n'
    ),
}


def _expected_name(argv: Sequence[str] | None) -> str:
    try:
        source = sys.argv[1:] if argv is None else argv
        first = source[0]
        if type(first) is str and first == "compaction":
            return "compaction"
    except BaseException:
        pass
    return "purity"


def _write_fallback(expected_name: str) -> bool:
    selected = (
        "compaction"
        if type(expected_name) is str and expected_name == "compaction"
        else "purity"
    )
    try:
        sys.stdout.write(_FALLBACK_JSON[selected])
    except BaseException:
        return False
    return False


def _emit(expected_name: str, result: ProbeResult) -> bool:
    try:
        if (
            type(expected_name) is not str
            or expected_name not in {"purity", "compaction"}
            or type(result) is not ProbeResult
            or result.name != expected_name
            or result.passed is not False
        ):
            return _write_fallback(expected_name)
        payload = json.dumps(
            result.redacted_dict(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        encoded = (payload + "\n").encode("utf-8")
        if len(encoded) > MAX_REDACTED_REPORT_BYTES:
            return _write_fallback(expected_name)
        sys.stdout.write(payload + "\n")
        return True
    except BaseException:
        return _write_fallback(expected_name)


def _false_result(name: str, reason_code: str) -> ProbeResult:
    return ProbeResult(name, False, {"reason_code": reason_code})


def _main(argv: Sequence[str] | None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(type(argument) is not str for argument in arguments):
        name = _expected_name(arguments)
        if not _emit(name, _false_result(name, "invalid_invocation")):
            return 5
        return 2
    name = _expected_name(arguments)
    if "--claim-live-success" in arguments:
        if not _emit(name, _false_result(name, "live_success_claim_forbidden")):
            return 5
        return 2
    if len(arguments) != 1 or arguments[0] not in {"prompt-purity", "compaction"}:
        if not _emit(name, _false_result(name, "invalid_invocation")):
            return 5
        return 2

    command = arguments[0]
    runner = (
        run_prompt_purity_probe
        if command == "prompt-purity"
        else run_compaction_probe
    )
    try:
        result = runner()
    except ProbeUnavailable:
        if not _emit(name, _false_result(name, "child_attestation_unavailable")):
            return 5
        return 3
    except BaseException:
        if not _emit(name, _false_result(name, "probe_internal_error")):
            return 5
        return 4
    if (
        type(result) is not ProbeResult
        or result.name != name
    ):
        if not _emit(name, _false_result(name, "probe_internal_error")):
            return 5
        return 4
    if not _emit(name, result):
        return 5
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run one probe, emitting exactly one redacted JSON object."""
    expected_name = _expected_name(argv)
    try:
        return _main(argv)
    except BaseException:
        _write_fallback(expected_name)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
