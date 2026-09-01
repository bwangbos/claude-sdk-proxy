from __future__ import annotations

import json
import math
from io import StringIO

import pytest

from claude_sdk_proxy.attestation import current_attestation_availability
from claude_sdk_proxy.probe_cli import main
from claude_sdk_proxy.probes import (
    MAX_REDACTED_REPORT_BYTES,
    REDACTION_MARKER,
    ProbeResult,
    ProbeUnavailable,
    run_prompt_purity_probe,
    safe_canary_digest,
)


def encoded(result: ProbeResult) -> str:
    return json.dumps(
        result.redacted_dict(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def test_report_never_contains_content_or_credentials() -> None:
    result = ProbeResult(
        "purity",
        False,
        {
            "system_prompt": "SYSTEM-CANARY",
            "messages": ["USER-CANARY"],
            "authorization": "Bearer SECRET",
            "shape": {"blocks": 1},
        },
    )

    report = result.redacted_dict()
    serialized = encoded(result)

    assert all(
        value not in serialized
        for value in ("SYSTEM-CANARY", "USER-CANARY", "SECRET")
    )
    assert report == {
        "schema_version": 1,
        "name": "purity",
        "passed": False,
        "evidence": {
            "authorization": REDACTION_MARKER,
            "messages": REDACTION_MARKER,
            "shape": {"blocks": 1},
            "system_prompt": REDACTION_MARKER,
        },
    }


@pytest.mark.parametrize(
    "key",
    [
        "system",
        "SYSTEM-PROMPT",
        "messages",
        "prompt",
        "text",
        "tool.input",
        "tool_result",
        "Authorization",
        "bearer-token",
        "apiKey",
        "API_KEY",
        "client-secret",
        "password",
        "cookie",
        "session_id",
        "credentialPath",
        "ａｕｔｈｏｒｉｚａｔｉｏｎ",
    ],
)
def test_sensitive_keys_are_normalized_and_recursively_redacted(key: str) -> None:
    result = ProbeResult(
        "purity",
        False,
        {"shape": {"nested": {key: {"text": "DO-NOT-LEAK"}}}},
    )

    assert "DO-NOT-LEAK" not in encoded(result)


def test_exceptions_unknown_objects_cycles_and_nonfinite_values_fail_closed() -> None:
    class Hostile:
        def __repr__(self) -> str:
            raise AssertionError("redactor must not stringify hostile objects")

    cycle: list[object] = []
    cycle.append(cycle)
    result = ProbeResult(
        "purity",
        False,
        {
            "shape": {
                "exception": RuntimeError("EXCEPTION-SECRET"),
                "unknown": Hostile(),
                "cycle": cycle,
                "nan": math.nan,
                "infinity": math.inf,
            }
        },
    )

    serialized = encoded(result)

    assert "EXCEPTION-SECRET" not in serialized
    assert "Hostile" not in serialized
    assert "NaN" not in serialized
    assert "Infinity" not in serialized
    assert serialized.count(REDACTION_MARKER) >= 5


def test_confusable_duplicate_keys_fail_closed_without_preserving_values() -> None:
    result = ProbeResult(
        "purity",
        False,
        {"shape": {"block-count": 1, "BLOCK_COUNT": 2}},
    )

    report = result.redacted_dict()

    assert report["evidence"]["shape"] == REDACTION_MARKER


def test_report_depth_item_and_byte_limits_are_hard_bounds() -> None:
    nested: object = {"blocks": 1}
    for _ in range(100):
        nested = {"shape": nested}
    result = ProbeResult(
        "purity",
        False,
        {
            "shape": nested,
            "event_counts": list(range(100_000)),
            "reason_code": "x" * 100_000,
        },
    )

    serialized = encoded(result).encode("utf-8")

    assert len(serialized) <= MAX_REDACTED_REPORT_BYTES
    assert REDACTION_MARKER.encode() in serialized


def test_only_explicit_safe_canary_hash_type_can_preserve_a_digest() -> None:
    digest = safe_canary_digest(b"proxy-owned-harmless-canary")
    raw_digest = "a" * 64
    report = ProbeResult(
        "path_persistence",
        False,
        {
            "safe_canary_sha256": digest,
            "untrusted_sha256": raw_digest,
        },
    ).redacted_dict()

    assert report["evidence"]["safe_canary_sha256"] == digest.value
    assert report["evidence"]["untrusted_sha256"] == REDACTION_MARKER


def test_prompt_purity_probe_is_unavailable_before_any_live_action() -> None:
    availability = current_attestation_availability()
    assert availability.core_gate_available is False

    with pytest.raises(ProbeUnavailable, match="child_attestation_unavailable"):
        run_prompt_purity_probe()


def test_probe_cli_emits_only_redacted_false_evidence_when_gate_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = StringIO()
    monkeypatch.setattr("sys.stdout", stdout)

    status = main(["prompt-purity"])
    report = json.loads(stdout.getvalue())

    assert status != 0
    assert report["passed"] is False
    assert report["evidence"]["reason_code"] == "child_attestation_unavailable"
    assert len(stdout.getvalue().encode("utf-8")) <= MAX_REDACTED_REPORT_BYTES


def test_probe_cli_rejects_a_requested_live_success_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = StringIO()
    monkeypatch.setattr("sys.stdout", stdout)

    status = main(["prompt-purity", "--claim-live-success"])
    report = json.loads(stdout.getvalue())

    assert status != 0
    assert report["passed"] is False
    assert report["evidence"]["reason_code"] == "live_success_claim_forbidden"

