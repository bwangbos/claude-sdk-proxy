from __future__ import annotations

import copy
import gc
import hashlib
import json
import math
import os
import pickle
import weakref
from collections.abc import Sequence
from io import StringIO
from pathlib import Path

import pytest

import claude_sdk_proxy.probe_cli as probe_cli
import claude_sdk_proxy.probes as probes
from claude_sdk_proxy.attestation import current_attestation_availability
from claude_sdk_proxy.path_policy import (
    PathPolicy,
    RootKind,
    _consume_safe_canary_receipt,
)
from claude_sdk_proxy.probe_cli import main
from claude_sdk_proxy.probes import (
    MAX_REDACTED_REPORT_BYTES,
    ProbeResult,
    ProbeUnavailable,
    run_prompt_purity_probe,
    safe_canary_digest,
)

REPORT_NAMES = ("purity", "compaction", "path_persistence")
FAILURE_REASONS = (
    "child_attestation_unavailable",
    "live_success_claim_forbidden",
    "invalid_invocation",
    "probe_internal_error",
    "live_probe_harness_unavailable",
    "redaction_failure",
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
    assert report["schema_version"] == 1
    assert report["name"] == "purity"
    assert report["passed"] is False
    assert report["evidence"] == {"reason_code": "redaction_failure"}


def test_current_false_tuple_admits_only_exact_failure_variants() -> None:
    for name in REPORT_NAMES:
        for reason in FAILURE_REASONS:
            report = ProbeResult(name, False, {"reason_code": reason}).redacted_dict()
            assert report == {
                "schema_version": 1,
                "name": name,
                "passed": False,
                "evidence": {"reason_code": reason},
            }


@pytest.mark.parametrize(
    ("name", "extra"),
    [
        ("purity", {"shape": {"blocks": 0}}),
        ("purity", {"ambient_capability_absent": True}),
        ("purity", {"advertised_tool_count": 0}),
        ("compaction", {"auto_compaction_disabled": True}),
        ("compaction", {"compact_boundary_count": 0}),
        ("path_persistence", {"persistence_absent": True}),
        ("path_persistence", {"modified_path_count": 0}),
    ],
)
def test_success_and_contradictory_optional_facts_collapse_to_fixed_false(
    name: str,
    extra: dict[str, object],
) -> None:
    for passed, evidence in (
        (True, {"reason_code": "child_attestation_unavailable"}),
        (False, {}),
        (False, {"reason_code": "child_attestation_unavailable", **extra}),
    ):
        report = ProbeResult(
            name,
            passed,
            evidence,
        ).redacted_dict()
        assert report["passed"] is False
        assert report["evidence"] == {"reason_code": "redaction_failure"}


def test_exact_name_bool_and_reason_types_reject_subclasses_or_unknown_values() -> None:
    class StringSubclass(str):
        pass

    with pytest.raises(TypeError):
        ProbeResult(StringSubclass("purity"), False, {})
    with pytest.raises(TypeError):
        ProbeResult("purity", 0, {})  # type: ignore[arg-type]

    report = ProbeResult(
        "purity", True, {"reason_code": "future_reason"}
    ).redacted_dict()
    assert report["passed"] is False
    assert report["evidence"]["reason_code"] == "redaction_failure"


def test_content_ascii_integer_encoding_and_secret_values_cannot_pass() -> None:
    result = ProbeResult(
        "purity",
        True,
        {
            "reason_code": "child_attestation_unavailable",
            "text": [83, 69, 67, 82, 69, 84],
            "authorization": 123456789,
        },
    )
    serialized = encoded(result)

    assert "123456789" not in serialized
    assert "83" not in serialized
    assert json.loads(serialized)["passed"] is False


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
    assert json.loads(serialized)["evidence"] == {"reason_code": "redaction_failure"}


def test_confusable_duplicate_keys_fail_closed_without_preserving_values() -> None:
    result = ProbeResult(
        "purity",
        False,
        {"shape": {"block-count": 1, "BLOCK_COUNT": 2}},
    )

    report = result.redacted_dict()

    assert report["passed"] is False
    assert report["evidence"]["reason_code"] == "redaction_failure"


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
    assert b'"reason_code":"redaction_failure"' in serialized


def test_only_verified_proxy_owned_canary_can_mint_a_one_use_digest_receipt(
    tmp_path: Path,
) -> None:
    real_root = tmp_path / "real"
    proxy_root = tmp_path / "proxy"
    real_root.mkdir(mode=0o700)
    proxy_root.mkdir(mode=0o700)
    canary_dir = proxy_root / "canaries"
    canary_dir.mkdir(mode=0o700)
    canary_path = canary_dir / "purity.txt"
    canary = b"proxy-owned-harmless-canary"
    canary_path.write_bytes(canary)
    canary_path.chmod(0o600)
    policy = PathPolicy(real_login_root=real_root, proxy_owned_root=proxy_root)
    observation = policy.metadata(
        Path("canaries/purity.txt"), root=RootKind.PROXY_OWNED
    )

    with pytest.raises(TypeError):
        safe_canary_digest(canary)  # type: ignore[call-arg]
    receipt = safe_canary_digest(
        canary,
        policy=policy,
        path=Path("canaries/purity.txt"),
        expected=observation,
    )
    assert object.__getattribute__(receipt, "_creator_pid") == os.getpid()
    assert "SafeCanaryDigest" not in probes.__all__
    assert not hasattr(receipt, "value")
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises((TypeError, ValueError, pickle.PicklingError)):
            operation(receipt)
    assert _consume_safe_canary_receipt(receipt) == hashlib.sha256(canary).hexdigest()
    with pytest.raises(RuntimeError, match="receipt"):
        _consume_safe_canary_receipt(receipt)

    raw_digest = "a" * 64
    raw = ProbeResult(
        "path_persistence",
        False,
        {
            "reason_code": "child_attestation_unavailable",
            "safe_canary_sha256": raw_digest,
        },
    ).redacted_dict()
    assert raw["passed"] is False
    assert raw["evidence"]["reason_code"] == "redaction_failure"


def test_forked_children_invalidate_inherited_canary_authority_repeatedly(
    tmp_path: Path,
) -> None:
    real_root = tmp_path / "real"
    proxy_root = tmp_path / "proxy"
    real_root.mkdir(mode=0o700)
    proxy_root.mkdir(mode=0o700)
    canary_dir = proxy_root / "canaries"
    canary_dir.mkdir(mode=0o700)
    target = canary_dir / "purity.txt"
    canary = b"FORK-BOUND-SAFE-CANARY"
    target.write_bytes(canary)
    target.chmod(0o600)
    policy = PathPolicy(real_login_root=real_root, proxy_owned_root=proxy_root)
    metadata = policy.metadata(Path("canaries/purity.txt"), root=RootKind.PROXY_OWNED)
    receipt = safe_canary_digest(
        canary,
        policy=policy,
        path=Path("canaries/purity.txt"),
        expected=metadata,
    )

    for _ in range(3):
        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(read_fd)
            valid = False
            try:
                try:
                    _consume_safe_canary_receipt(receipt)
                except RuntimeError:
                    valid = (
                        len(policy._canary_records) == 0
                        and policy._canary_creator_pid == os.getpid()
                    )
                os.write(write_fd, b"1" if valid else b"0")
            finally:
                os.close(write_fd)
                os._exit(0)
        os.close(write_fd)
        result = os.read(read_fd, 1)
        os.close(read_fd)
        waited, status = os.waitpid(child, 0)
        assert waited == child
        assert os.waitstatus_to_exitcode(status) == 0
        assert result == b"1"

    assert _consume_safe_canary_receipt(receipt) == hashlib.sha256(canary).hexdigest()


def test_lazy_pid_boundary_invalidates_when_at_fork_handler_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_root = tmp_path / "real"
    proxy_root = tmp_path / "proxy"
    real_root.mkdir(mode=0o700)
    proxy_root.mkdir(mode=0o700)
    canary_dir = proxy_root / "canaries"
    canary_dir.mkdir(mode=0o700)
    target = canary_dir / "purity.txt"
    target.write_bytes(b"SAFE")
    target.chmod(0o600)
    policy = PathPolicy(real_login_root=real_root, proxy_owned_root=proxy_root)
    metadata = policy.metadata(Path("canaries/purity.txt"), root=RootKind.PROXY_OWNED)
    receipt = safe_canary_digest(
        b"SAFE",
        policy=policy,
        path=Path("canaries/purity.txt"),
        expected=metadata,
    )
    simulated_child_pid = os.getpid() + 100_000
    monkeypatch.setattr(
        "claude_sdk_proxy.path_policy.os.getpid", lambda: simulated_child_pid
    )

    with pytest.raises(RuntimeError, match="process"):
        _consume_safe_canary_receipt(receipt)

    assert policy._canary_creator_pid == simulated_child_pid
    assert policy._canary_records == {}


def test_canary_policy_registry_does_not_retain_policies(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    proxy_root = tmp_path / "proxy"
    real_root.mkdir(mode=0o700)
    proxy_root.mkdir(mode=0o700)
    policy = PathPolicy(real_login_root=real_root, proxy_owned_root=proxy_root)
    reference = weakref.ref(policy)

    del policy
    gc.collect()

    assert reference() is None


def test_hostile_sequence_protocol_fails_closed_without_calling_repr() -> None:
    class HostileSequence(Sequence[object]):
        def __len__(self) -> int:
            raise RuntimeError("HOSTILE-LENGTH-SECRET")

        def __getitem__(self, index: int) -> object:
            raise RuntimeError(f"HOSTILE-ITEM-SECRET-{index}")

        def __repr__(self) -> str:
            raise AssertionError("redactor must not stringify hostile sequences")

    result = ProbeResult(
        "purity", False, {"shape": {"event_counts": HostileSequence()}}
    )

    serialized = encoded(result)

    assert "HOSTILE" not in serialized
    assert json.loads(serialized)["evidence"] == {"reason_code": "redaction_failure"}


def test_mutated_or_forged_safe_canary_receipt_fails_closed(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    proxy_root = tmp_path / "proxy"
    real_root.mkdir(mode=0o700)
    proxy_root.mkdir(mode=0o700)
    canary_dir = proxy_root / "canaries"
    canary_dir.mkdir(mode=0o700)
    target = canary_dir / "purity.txt"
    target.write_bytes(b"SAFE")
    target.chmod(0o600)
    policy = PathPolicy(real_login_root=real_root, proxy_owned_root=proxy_root)
    metadata = policy.metadata(Path("canaries/purity.txt"), root=RootKind.PROXY_OWNED)
    receipt = safe_canary_digest(
        b"SAFE",
        policy=policy,
        path=Path("canaries/purity.txt"),
        expected=metadata,
    )
    digest_field = "_digest" if hasattr(receipt, "_digest") else "_value"
    object.__setattr__(receipt, digest_field, "a" * 64)
    with pytest.raises(RuntimeError, match="receipt"):
        _consume_safe_canary_receipt(receipt)

    forged = object.__new__(type(receipt))
    with pytest.raises(RuntimeError, match="receipt"):
        _consume_safe_canary_receipt(forged)


class HostileMapping(dict[str, object]):
    def items(self):  # type: ignore[no-untyped-def]
        raise KeyboardInterrupt("HOSTILE-ITEMS-SECRET")


class HostileEntryMapping(dict[str, object]):
    class Entry:
        def __iter__(self):
            raise KeyboardInterrupt("HOSTILE-UNPACK-SECRET")

    def items(self):  # type: ignore[no-untyped-def]
        return iter((self.Entry(),))


class HostileKey(str):
    def __hash__(self) -> int:
        raise KeyboardInterrupt("HOSTILE-HASH-SECRET")

    def __eq__(self, other: object) -> bool:
        raise KeyboardInterrupt("HOSTILE-EQ-SECRET")


class HostileKeyMapping(dict[str, object]):
    def items(self):  # type: ignore[no-untyped-def]
        return iter(((HostileKey("reason_code"), "child_attestation_unavailable"),))


@pytest.mark.parametrize(
    "evidence",
    [
        HostileMapping(seed=1),
        HostileEntryMapping(seed=1),
        HostileKeyMapping(seed=1),
    ],
)
def test_hostile_mapping_materialization_unpack_hash_and_eq_are_constant_false(
    evidence: dict[str, object],
) -> None:
    result = ProbeResult("purity", True, evidence)

    report = result.redacted_dict()

    assert report["passed"] is False
    assert report["evidence"] == {"reason_code": "redaction_failure"}


def test_redacted_dict_json_failure_returns_fixed_minimal_false_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt("JSON-SERIALIZATION-SECRET")

    monkeypatch.setattr(probes.json, "dumps", fail)

    report = ProbeResult(
        "purity", False, {"reason_code": "child_attestation_unavailable"}
    ).redacted_dict()

    assert report == {
        "schema_version": 1,
        "name": "purity",
        "passed": False,
        "evidence": {"reason_code": "redaction_failure"},
    }


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


@pytest.mark.parametrize(
    ("command", "runner_name", "report_name"),
    [
        ("prompt-purity", "run_prompt_purity_probe", "purity"),
        ("compaction", "run_compaction_probe", "compaction"),
    ],
)
def test_probe_cli_can_never_emit_or_exit_with_current_tuple_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    runner_name: str,
    report_name: str,
) -> None:
    def impossible_success() -> ProbeResult:
        return ProbeResult(
            report_name, True, {"reason_code": "child_attestation_unavailable"}
        )

    monkeypatch.setattr(probe_cli, runner_name, impossible_success)

    status = main([command])
    report = json.loads(capsys.readouterr().out)

    assert status != 0
    assert report["passed"] is False
    assert report["evidence"] == {"reason_code": "redaction_failure"}


def test_probe_cli_redacts_unexpected_exception_messages(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail() -> ProbeResult:
        raise RuntimeError("EXCEPTION-MESSAGE-SECRET")

    monkeypatch.setattr("claude_sdk_proxy.probe_cli.run_prompt_purity_probe", fail)

    status = main(["prompt-purity"])
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert status != 0
    assert "EXCEPTION-MESSAGE-SECRET" not in captured.out + captured.err
    assert report["evidence"]["reason_code"] == "probe_internal_error"


@pytest.mark.parametrize(
    ("command", "expected_name"),
    [("prompt-purity", "purity"), ("compaction", "compaction")],
)
def test_cli_json_serialization_failure_uses_command_specific_fallback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    expected_name: str,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt("CLI-JSON-SECRET")

    monkeypatch.setattr(probe_cli.json, "dumps", fail)

    status = main([command])
    captured = capsys.readouterr()

    assert status != 0
    assert "CLI-JSON-SECRET" not in captured.out + captured.err
    assert json.loads(captured.out)["evidence"] == {
        "reason_code": "redaction_failure"
    }
    assert json.loads(captured.out)["name"] == expected_name


@pytest.mark.parametrize(
    ("command", "runner_name", "expected_name", "wrong_name"),
    [
        ("prompt-purity", "run_prompt_purity_probe", "purity", "compaction"),
        ("compaction", "run_compaction_probe", "compaction", "purity"),
    ],
)
def test_cli_rejects_runner_report_name_mismatch_as_command_specific_internal_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    runner_name: str,
    expected_name: str,
    wrong_name: str,
) -> None:
    def mismatched() -> ProbeResult:
        return ProbeResult(
            wrong_name,
            False,
            {"reason_code": "child_attestation_unavailable"},
        )

    monkeypatch.setattr(probe_cli, runner_name, mismatched)

    status = main([command])
    report = json.loads(capsys.readouterr().out)

    assert status != 0
    assert report == {
        "schema_version": 1,
        "name": expected_name,
        "passed": False,
        "evidence": {"reason_code": "probe_internal_error"},
    }


def test_cli_outer_fallback_derives_compaction_without_hostile_argv_repr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class HostileArgv(Sequence[str]):
        def __len__(self) -> int:
            return 1

        def __getitem__(self, index: int) -> str:
            if index == 0:
                return "compaction"
            raise IndexError

        def __iter__(self):  # type: ignore[no-untyped-def]
            raise KeyboardInterrupt("HOSTILE-ARGV-SECRET")

        def __repr__(self) -> str:
            raise AssertionError("argv repr must not be used")

    status = main(HostileArgv())
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert status != 0
    assert "HOSTILE" not in captured.out + captured.err
    assert report["name"] == "compaction"
    assert report["evidence"] == {"reason_code": "redaction_failure"}


def test_cli_emit_rejects_probe_result_subclasses(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class ProbeResultSubclass(ProbeResult):
        pass

    def subclass_result() -> ProbeResult:
        return ProbeResultSubclass(
            "purity",
            False,
            {"reason_code": "child_attestation_unavailable"},
        )

    monkeypatch.setattr(probe_cli, "run_prompt_purity_probe", subclass_result)

    status = main(["prompt-purity"])
    report = json.loads(capsys.readouterr().out)

    assert status != 0
    assert report["name"] == "purity"
    assert report["evidence"] == {"reason_code": "probe_internal_error"}


@pytest.mark.parametrize(
    ("command", "runner_name", "expected_name", "mutation"),
    [
        ("prompt-purity", "run_prompt_purity_probe", "purity", "name"),
        ("prompt-purity", "run_prompt_purity_probe", "purity", "passed"),
        ("compaction", "run_compaction_probe", "compaction", "name"),
        ("compaction", "run_compaction_probe", "compaction", "passed"),
    ],
)
def test_cli_revalidates_materialized_report_after_hostile_redaction_mutation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    runner_name: str,
    expected_name: str,
    mutation: str,
) -> None:
    class MutatingEvidence(dict[str, object]):
        result: ProbeResult | None = None

        def items(self):  # type: ignore[no-untyped-def]
            assert self.result is not None
            if mutation == "name":
                wrong_name = "compaction" if expected_name == "purity" else "purity"
                object.__setattr__(self.result, "name", wrong_name)
            else:
                object.__setattr__(self.result, "passed", True)
            return super().items()

    evidence = MutatingEvidence(reason_code="child_attestation_unavailable")
    result = ProbeResult(expected_name, False, evidence)
    evidence.result = result

    monkeypatch.setattr(probe_cli, runner_name, lambda: result)

    status = main([command])
    report = json.loads(capsys.readouterr().out)

    assert status != 0
    assert report == {
        "schema_version": 1,
        "name": expected_name,
        "passed": False,
        "evidence": {"reason_code": "redaction_failure"},
    }


def test_cli_output_failure_never_escapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HostileStdout:
        def write(self, _value: str) -> None:
            raise KeyboardInterrupt("STDOUT-SECRET")

    monkeypatch.setattr("sys.stdout", HostileStdout())

    assert main(["prompt-purity"]) != 0
