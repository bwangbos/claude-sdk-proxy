"""Content-free command line interface for Phase 0 probes."""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import claude_sdk_proxy.validated as validated_evidence
from claude_sdk_proxy.attestation import current_attestation_availability
from claude_sdk_proxy.probes import (
    MAX_REDACTED_REPORT_BYTES,
    ProbeResult,
    ProbeUnavailable,
    run_compaction_probe,
    run_prompt_purity_probe,
)
from claude_sdk_proxy.validated import (
    CURRENT_POLICY_PERSONAL_LOCAL_USE_ALLOWED,
    CompleteManifestCollection,
    ManifestCollectionRun,
    canonical_evidence_json,
    load_sdk_tool_evidence,
    load_usage_evidence,
    require_core_gates,
)

type ManifestEvidenceCollector = Callable[
    [ManifestCollectionRun], CompleteManifestCollection
]

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
_FALSE_REASON_CODES = frozenset(
    {
        "child_attestation_unavailable",
        "live_success_claim_forbidden",
        "invalid_invocation",
        "probe_internal_error",
        "live_probe_harness_unavailable",
        "redaction_failure",
    }
)


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


def _materialize_final_report(
    expected_name: str, source: object
) -> dict[str, object] | None:
    try:
        if type(source) is not dict or len(source) != 4:
            return None
        fields: dict[str, object] = {}
        for key, value in source.items():
            if type(key) is not str or key not in {
                "schema_version",
                "name",
                "passed",
                "evidence",
            }:
                return None
            fields[key] = value
        if (
            len(fields) != 4
            or type(fields.get("schema_version")) is not int
            or fields["schema_version"] != 1
            or type(fields.get("name")) is not str
            or fields["name"] != expected_name
            or fields.get("passed") is not False
        ):
            return None
        evidence = fields.get("evidence")
        if type(evidence) is not dict or len(evidence) != 1:
            return None
        entries = tuple(evidence.items())
        if len(entries) != 1:
            return None
        key, reason = entries[0]
        if (
            type(key) is not str
            or key != "reason_code"
            or type(reason) is not str
            or reason not in _FALSE_REASON_CODES
        ):
            return None
        return {
            "schema_version": 1,
            "name": expected_name,
            "passed": False,
            "evidence": {"reason_code": reason},
        }
    except BaseException:
        return None


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
        report = _materialize_final_report(expected_name, result.redacted_dict())
        if report is None:
            return _write_fallback(expected_name)
        payload = json.dumps(
            report,
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
    if arguments and arguments[0] == "all":
        return _run_all(arguments)
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


def _production_manifest_collector(
    run: ManifestCollectionRun,
) -> CompleteManifestCollection:
    """Fail at the first unavailable typed production collection boundary."""
    if type(run) is not ManifestCollectionRun:
        raise ProbeUnavailable("manifest collection run is invalid")
    if not CURRENT_POLICY_PERSONAL_LOCAL_USE_ALLOWED:
        raise ProbeUnavailable("personal subscription policy is unavailable")
    if not current_attestation_availability().core_gate_available:
        raise ProbeUnavailable("child attestation is unavailable")
    # The current probe APIs expose individual unavailable live gates, not one
    # complete Tasks 1-9 candidate. Never assemble production evidence from a
    # prior manifest or partially run probes.
    raise ProbeUnavailable("complete Tasks 1-9 collection is unavailable")


def _run_all(
    arguments: list[str], *, collector: ManifestEvidenceCollector | None = None
) -> int:
    """Collect, authorize, persist, and reload one complete Phase 0 manifest."""
    if (
        len(arguments) != 4
        or any(type(argument) is not str for argument in arguments)
        or arguments[0] != "all"
        or arguments[1] != "--ack-personal-local-use-policy"
        or arguments[2] != "--output"
        or not arguments[3]
        or "\x00" in arguments[3]
    ):
        return 2
    if os.environ.get("RUN_LIVE_CLAUDE_TESTS") != "1":
        return 2
    # The acknowledgment is an invocation guard, never policy evidence.
    if not CURRENT_POLICY_PERSONAL_LOCAL_USE_ALLOWED:
        return 3
    if not current_attestation_availability().core_gate_available:
        return 3
    selected_collector = (
        _production_manifest_collector if collector is None else collector
    )
    try:
        run = ManifestCollectionRun.begin()
        collection = selected_collector(run)
        consumed = run.consume(collection)
        output = Path(os.path.abspath(arguments[3]))
        try:
            authorization = validated_evidence._authorize_manifest_output(
                output, consumed
            )
        finally:
            validated_evidence._burn_consumed_collection(consumed)
        document = authorization.manifest.to_json()
        expected = canonical_evidence_json(document) + b"\n"
        try:
            validated_evidence._atomic_write_manifest(
                output,
                document,
                authorization=authorization,
            )
        finally:
            validated_evidence._revoke_manifest_output_authorization(authorization)
        reloaded = validated_evidence.load_manifest(output)
        require_core_gates(reloaded)
        load_usage_evidence(reloaded)
        load_sdk_tool_evidence(reloaded)
        if (
            output.read_bytes() != expected
            or canonical_evidence_json(reloaded.to_json()) + b"\n" != expected
            or stat.S_IMODE(output.stat().st_mode) != 0o600
        ):
            return 4
        return 0
    except ProbeUnavailable:
        return 3
    except BaseException:
        return 4


def main(argv: Sequence[str] | None = None) -> int:
    """Run one probe, emitting exactly one redacted JSON object."""
    expected_name = _expected_name(argv)
    try:
        return _main(argv)
    except BaseException:
        try:
            arguments = list(sys.argv[1:] if argv is None else argv)
            if arguments and arguments[0] == "all":
                return 5
        except BaseException:
            pass
        _write_fallback(expected_name)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
