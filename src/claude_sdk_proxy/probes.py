"""Content-free Phase 0 probe results and fail-closed live probe entry points."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never, cast

from claude_sdk_proxy.attestation import current_attestation_availability

if TYPE_CHECKING:
    from claude_sdk_proxy.path_policy import PathMetadata, PathPolicy, RootKind

REDACTION_MARKER: Final = "__redacted__"
MAX_REDACTED_REPORT_BYTES: Final = 16_384
_MAX_ITEMS: Final = 128
_MAX_COUNT: Final = 1_000_000
_MAX_CANARY_BYTES: Final = 256
_CANONICAL_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_REPORT_NAMES = frozenset({"purity", "compaction", "path_persistence"})


class _ProbeReasonCode(StrEnum):
    CHILD_ATTESTATION_UNAVAILABLE = "child_attestation_unavailable"
    LIVE_SUCCESS_CLAIM_FORBIDDEN = "live_success_claim_forbidden"
    INVALID_INVOCATION = "invalid_invocation"
    PROBE_INTERNAL_ERROR = "probe_internal_error"
    LIVE_PROBE_HARNESS_UNAVAILABLE = "live_probe_harness_unavailable"
    REDACTION_FAILURE = "redaction_failure"


_REASON_CODES = frozenset(reason.value for reason in _ProbeReasonCode)
_SCHEMAS: Final[dict[str, dict[str, str]]] = {
    "purity": {
        "reason_code": "reason",
        "shape": "shape",
        "attribution_absent_observable": "bool",
        "ambient_capability_absent": "bool",
        "structured_user_input": "bool",
        "advertised_tool_count": "count",
    },
    "compaction": {
        "reason_code": "reason",
        "auto_compaction_disabled": "bool",
        "compact_boundary_count": "count",
        "summary_event_count": "count",
        "context_exhausted": "bool",
    },
    "path_persistence": {
        "reason_code": "reason",
        "persistence_absent": "bool",
        "path_count": "count",
        "modified_path_count": "count",
        "unknown_path_count": "count",
        "safe_canary_sha256": "canary_receipt",
    },
}


class ProbeUnavailable(RuntimeError):
    """Raised when a live probe's prerequisite gate is not affirmative."""


class _RedactionFailure(RuntimeError):
    """Internal sentinel whose details are never exposed."""


def safe_canary_digest(
    canary: bytes,
    *,
    policy: PathPolicy,
    path: Path,
    expected: PathMetadata,
    root: RootKind | None = None,
) -> object:
    """Mint a one-use receipt for a verified proxy-owned benign canary."""
    from claude_sdk_proxy.path_policy import PathPolicy, RootKind

    if type(canary) is not bytes or not canary or len(canary) > _MAX_CANARY_BYTES:
        raise ValueError("safe canary must be 1..256 bytes")
    if type(policy) is not PathPolicy:
        raise TypeError("safe canary digest requires a PathPolicy")
    selected_root = RootKind.PROXY_OWNED if root is None else root
    if selected_root is not RootKind.PROXY_OWNED:
        raise ValueError("safe canary digest requires the proxy-owned root")
    try:
        return policy._mint_safe_canary_receipt(
            path,
            canary,
            expected=expected,
            root=selected_root,
        )
    except BaseException as error:
        if isinstance(error, (TypeError, ValueError)):
            raise
        raise ValueError("safe canary verification failed") from error


def _fixed_false(name: object) -> dict[str, object]:
    safe_name = name if type(name) is str and name in _REPORT_NAMES else "purity"
    return {
        "schema_version": 1,
        "name": safe_name,
        "passed": False,
        "evidence": {
            "reason_code": _ProbeReasonCode.REDACTION_FAILURE.value,
            "redaction": REDACTION_MARKER,
        },
    }


def _materialize_mapping(value: Mapping[str, object]) -> dict[str, object]:
    try:
        entries_method = value.items
        iterator = iter(entries_method())
        entries: list[tuple[str, object]] = []
        for _ in range(_MAX_ITEMS + 1):
            try:
                entry = next(iterator)
            except StopIteration:
                break
            if type(entry) is not tuple or len(entry) != 2:
                raise _RedactionFailure()
            raw_key = entry[0]
            if type(raw_key) is not str or _CANONICAL_KEY.fullmatch(raw_key) is None:
                raise _RedactionFailure()
            entries.append((raw_key, entry[1]))
        else:
            raise _RedactionFailure()
        output: dict[str, object] = {}
        for key, child in entries:
            if key in output:
                raise _RedactionFailure()
            output[key] = child
        return output
    except _RedactionFailure:
        raise
    except BaseException as error:
        raise _RedactionFailure() from error


def _exact_bool(value: object) -> bool:
    if type(value) is not bool:
        raise _RedactionFailure()
    return value


def _exact_count(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_COUNT:
        raise _RedactionFailure()
    return value


def _exact_reason(value: object) -> str:
    if type(value) is not str or value not in _REASON_CODES:
        raise _RedactionFailure()
    return value


def _exact_shape(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise _RedactionFailure()
    materialized = _materialize_mapping(cast("Mapping[str, object]", value))
    if set(materialized) != {"blocks"}:
        raise _RedactionFailure()
    return {"blocks": _exact_count(materialized["blocks"])}


def _consume_canary_receipt(value: object) -> str:
    try:
        from claude_sdk_proxy.path_policy import _consume_safe_canary_receipt

        return _consume_safe_canary_receipt(value)
    except BaseException as error:
        raise _RedactionFailure() from error


def _typed_evidence(name: str, evidence: Mapping[str, object]) -> dict[str, object]:
    materialized = _materialize_mapping(evidence)
    schema = _SCHEMAS[name]
    if not set(materialized).issubset(schema) or "reason_code" not in materialized:
        raise _RedactionFailure()
    output: dict[str, object] = {}
    for key in sorted(materialized):
        kind = schema[key]
        value = materialized[key]
        if kind == "reason":
            output[key] = _exact_reason(value)
        elif kind == "bool":
            output[key] = _exact_bool(value)
        elif kind == "count":
            output[key] = _exact_count(value)
        elif kind == "shape":
            output[key] = _exact_shape(value)
        elif kind == "canary_receipt":
            output[key] = _consume_canary_receipt(value)
        else:
            raise _RedactionFailure()
    return output


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """One bounded, typed, content-free probe verdict."""

    name: str
    passed: bool
    evidence: Mapping[str, object]

    def __post_init__(self) -> None:
        if type(self.name) is not str:
            raise TypeError("probe result name must be exact text")
        if self.name not in _REPORT_NAMES:
            raise ValueError("unsupported probe result name")
        if type(self.passed) is not bool:
            raise TypeError("probe passed field must be boolean")
        if not isinstance(self.evidence, Mapping):
            raise TypeError("probe evidence must be a mapping")

    def redacted_dict(self) -> dict[str, object]:
        """Return a bounded V1 report or the fixed false failure record."""
        try:
            evidence = _typed_evidence(self.name, self.evidence)
            report: dict[str, object] = {
                "schema_version": 1,
                "name": self.name,
                "passed": self.passed,
                "evidence": evidence,
            }
            encoded = json.dumps(
                report,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) > MAX_REDACTED_REPORT_BYTES:
                raise _RedactionFailure()
            return report
        except BaseException:
            return _fixed_false(self.name)


def _require_live_prerequisites() -> Never:
    availability = current_attestation_availability()
    if not availability.core_gate_available:
        raise ProbeUnavailable(_ProbeReasonCode.CHILD_ATTESTATION_UNAVAILABLE.value)
    raise ProbeUnavailable(_ProbeReasonCode.LIVE_PROBE_HARNESS_UNAVAILABLE.value)


def run_prompt_purity_probe() -> ProbeResult:
    """Run the live purity gate only after all Task 6 prerequisites pass."""
    _require_live_prerequisites()


def run_compaction_probe() -> ProbeResult:
    """Run the live compaction gate only after all Task 6 prerequisites pass."""
    _require_live_prerequisites()


__all__ = [
    "MAX_REDACTED_REPORT_BYTES",
    "REDACTION_MARKER",
    "ProbeResult",
    "ProbeUnavailable",
    "run_compaction_probe",
    "run_prompt_purity_probe",
    "safe_canary_digest",
]
