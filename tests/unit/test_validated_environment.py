"""Final Phase 0 evidence composition is strict, canonical, and fail closed."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

import claude_sdk_proxy.probe_cli as probe_cli
import claude_sdk_proxy.validated as validated
from claude_sdk_proxy.usage_evidence import UsageEvidenceSchema
from claude_sdk_proxy.validated import (
    REQUIRED_SDK_TOOL_GATES,
    ManifestError,
    Phase0PrerequisiteDigestResolver,
    SdkToolEvidenceRecord,
    canonical_evidence_json,
    load_manifest,
    load_sdk_tool_evidence,
    load_usage_evidence,
    phase0_prerequisite_digest,
    require_core_gates,
    sdk_tool_gate_passed,
    sdk_tool_record_digest,
)

CORE_GATES = frozenset(
    {
        "personal_subscription_policy",
        "exact_runtime_tuple",
        "darwin_local_apfs",
        "required_sync_primitives",
        "bsd_flock_model",
        "bounded_journal",
        "root_reconciliation_lock",
        "instance_lifetime_lock",
        "owner_record_create",
        "owner_record_replace",
        "durable_head_certification",
        "cleanup_automaton",
        "retaining_supervisor",
        "anchor_unconfirmed_fallback",
        "exact_environment",
        "per_child_auth_attestation",
        "preinput_network_gate",
        "auth_source_lifetime",
        "prompt_isolation",
        "attribution_absent_observable",
        "compaction_disabled",
        "path_safe_persistence",
        "structured_user_input",
        "exact_backend_model",
        "native_session_continuity",
        "streaming_event_contract",
        "exact_usage_schema",
    }
)

_MODEL = "claude-sonnet-4-5-20250929"
_OTHER_MODEL = "claude-opus-4-1-20250805"
_RUNTIME_DIGEST = "11" * 32
_BOUNDARY_NAME = "x" * 64


def _passing_mapping(dialect: str) -> dict[str, object]:
    if dialect == "anthropic":
        bindings = [
            {"sdk_path": ["input_tokens"], "public_path": ["input_tokens"]},
            {"sdk_path": ["output_tokens"], "public_path": ["output_tokens"]},
        ]
        derived: list[dict[str, object]] = []
    else:
        bindings = [
            {"sdk_path": ["input_tokens"], "public_path": ["prompt_tokens"]},
            {
                "sdk_path": ["output_tokens"],
                "public_path": ["completion_tokens"],
            },
        ]
        derived = [
            {
                "public_path": ["total_tokens"],
                "operation": "checked_sum",
                "source_sdk_paths": [["input_tokens"], ["output_tokens"]],
            }
        ]
    return {
        "dialect": dialect,
        "identity_bindings": bindings,
        "derived_fields": derived,
        "passed": True,
        "failure_reasons": [],
    }


def _false_mapping(dialect: str) -> dict[str, object]:
    return {
        "dialect": dialect,
        "identity_bindings": [],
        "derived_fields": [],
        "passed": False,
        "failure_reasons": ["sdk_shape_unstable"],
    }


def _usage_row(
    model: str,
    operation_class: str,
    *,
    runtime_digest: str = _RUNTIME_DIGEST,
    sdk_shape_passed: bool = True,
    public_mappings_passed: bool = True,
) -> dict[str, object]:
    fields: list[dict[str, object]] = []
    mappings = [_false_mapping("anthropic"), _false_mapping("openai")]
    if sdk_shape_passed:
        fields = [
            {
                "sdk_path": ["input_tokens"],
                "kind": "nonnegative_integer",
                "required": True,
                "nullable": False,
            },
            {
                "sdk_path": ["output_tokens"],
                "kind": "nonnegative_integer",
                "required": True,
                "nullable": False,
            },
        ]
        if public_mappings_passed:
            mappings = [_passing_mapping("anthropic"), _passing_mapping("openai")]
    return {
        "key": {
            "runtime_digest": runtime_digest,
            "backend_model_id": model,
            "thinking_mode": "null",
            "effort": None,
            "budget_tokens": None,
            "operation_class": operation_class,
        },
        "fields": fields,
        "sdk_shape_passed": sdk_shape_passed,
        "dialect_mappings": mappings,
    }


def _usage_schema(
    models: tuple[str, ...],
    *,
    runtime_digest: str = _RUNTIME_DIGEST,
    ordinary_passed: bool = True,
    include_tools: bool = True,
    tool_public_mappings_passed: bool = True,
) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    for model in models:
        rows.append(
            _usage_row(
                model,
                "ordinary",
                runtime_digest=runtime_digest,
                sdk_shape_passed=ordinary_passed,
                public_mappings_passed=ordinary_passed,
            )
        )
        if include_tools:
            for operation in ("tool_use_boundary", "post_tool_result"):
                rows.append(
                    _usage_row(
                        model,
                        operation,
                        runtime_digest=runtime_digest,
                        public_mappings_passed=tool_public_mappings_passed,
                    )
                )
    return UsageEvidenceSchema.from_json({"schema_version": 1, "rows": rows}).to_json()


def _naming_rule() -> dict[str, object]:
    representative = ("echo", "snake_case", "dash-name", _BOUNDARY_NAME)
    return {
        "version": 1,
        "server_identity": "caller_tools_v1",
        "caller_name_pattern": "[A-Za-z0-9_-]{1,64}",
        "caller_name_max_bytes": 64,
        "generated_name_template_or_algorithm": (
            "mcp__{server_identity}__{caller_name}"
        ),
        "representative_observations": {
            name: f"mcp__caller_tools_v1__{name}" for name in representative
        },
    }


def _sdk_record(
    model: str = _MODEL,
    *,
    runtime_digest: str = _RUNTIME_DIGEST,
    gates_passed: bool = True,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "key": {
            "runtime_digest": runtime_digest,
            "sdk_version": "0.2.148",
            "cli_version": "2.1.251",
            "cli_executable_device": 7,
            "cli_executable_inode": 11,
            "cli_executable_sha256": "22" * 32,
            "darwin_version": "24.6.0",
            "darwin_build": "24G84",
            "boot_id": "33" * 32,
            "mount_identity": {
                "filesystem_type": "apfs",
                "is_local": True,
                "mount_device": "disk3s1",
                "mount_fsid": "00112233:44556677",
                "mount_flags": ["journaled", "local"],
                "runtime_root_st_dev": 17,
            },
            "backend_class": "agent_sdk_subscription",
            "auth_class": "existing_claude_login",
            "semantic_class": "prompt_isolated_agent_sdk",
            "backend_model_id": model,
        },
        "naming_rule": _naming_rule(),
        "gates": {gate: gates_passed for gate in REQUIRED_SDK_TOOL_GATES},
    }


def _manifest_document(
    *,
    models: Mapping[str, str] | None = None,
    all_core_gates: bool = True,
    ordinary_usage_passed: bool = True,
    include_tool_rows: bool = True,
    tool_gates_passed: bool = True,
    tool_public_mappings_passed: bool = True,
) -> dict[str, object]:
    model_map = dict(models or {"sonnet": _MODEL})
    core_gates = {gate: all_core_gates for gate in CORE_GATES}
    accepted_shape: dict[str, object] | None = {
        "schema": "claude_sdk_proxy.public_child_attestation",
        "version": 1,
        "fields": [
            "auth_source",
            "cli_executable_sha256",
            "endpoint",
            "environment_fingerprint",
            "network_submission_count",
            "provider",
        ],
    }
    reasons: list[str] = []
    if not all_core_gates:
        accepted_shape = None
        reasons = [
            "public_auth_provenance_absent_or_unvalidated",
            "preinput_network_boundary_unproved",
            "per_turn_fresh_provenance_unavailable",
        ]
    records = [
        _sdk_record(
            model,
            gates_passed=tool_gates_passed,
        )
        for model in model_map.values()
    ]
    return {
        "schema_version": 1,
        "validated_at": "2026-09-01T12:00:00Z",
        "core_gates": core_gates,
        "policy_evidence": {
            "personal_local_use_allowed": all_core_gates,
            "sources": [
                {
                    "url": ("https://code.claude.com/docs/en/agent-sdk/overview"),
                    "retrieved_at": "2026-09-01T11:00:00Z",
                    "sha256": "44" * 32,
                },
                {
                    "url": (
                        "https://support.claude.com/en/articles/15036540-use-the-"
                        "claude-agent-sdk-with-your-claude-plan"
                    ),
                    "retrieved_at": "2026-09-01T11:00:01Z",
                    "sha256": "55" * 32,
                },
            ],
        },
        "runtime_evidence": {
            "runtime_digest": _RUNTIME_DIGEST,
            "python_version": "3.14.5",
            "python_executable": "/usr/bin/python3",
            "python_executable_sha256": "66" * 32,
            "sdk_version": "0.2.148",
            "sdk_path": "/opt/sdk/claude_agent_sdk/__init__.py",
            "sdk_sha256": "77" * 32,
            "cli_version": "2.1.251",
            "observed_cli_version": "2.1.251",
            "cli_path": "/usr/local/bin/claude",
            "cli_path_sha256": "88" * 32,
            "cli_executable_device": 7,
            "cli_executable_inode": 11,
            "cli_executable_mode": stat.S_IFREG | 0o755,
            "cli_executable_sha256": "22" * 32,
            "darwin_version": "24.6.0",
            "darwin_build": "24G84",
            "boot_id": "33" * 32,
        },
        "mount_identity": {
            "filesystem_type": "apfs",
            "is_local": True,
            "mount_device": "disk3s1",
            "mount_fsid": "00112233:44556677",
            "mount_flags": ["journaled", "local"],
            "runtime_root_st_dev": 17,
        },
        "sync_lock_lifecycle_evidence": {
            "syscall_matrix": {
                "preallocate": True,
                "fullfsync_file": True,
                "renameat": True,
                "unlinkat": True,
                "fsync_directory": True,
                "proc_pidinfo": True,
            },
            "lock_matrix": {
                "root_reconciliation_lock": True,
                "instance_lifetime_lock": True,
            },
            "owner_record_matrix": {
                "owner_record_create": True,
                "owner_record_replace": True,
            },
            "journal_matrix": {
                "bounded_journal": True,
                "durable_head_certification": True,
                "cleanup_automaton": True,
            },
            "supervisor_matrix": {
                "retaining_supervisor": True,
                "anchor_unconfirmed_fallback": True,
            },
        },
        "environment_evidence": {
            "allowed_names": [
                "CLAUDE_CONFIG_DIR",
                "HOME",
                "LANG",
                "LC_ALL",
                "LC_CTYPE",
                "LOGNAME",
                "PATH",
                "SSL_CERT_DIR",
                "SSL_CERT_FILE",
                "TEMP",
                "TMP",
                "TMPDIR",
                "TZ",
                "USER",
            ],
            "network_proxy_names": [
                "HTTPS_PROXY",
                "HTTP_PROXY",
                "NO_PROXY",
                "http_proxy",
                "https_proxy",
                "no_proxy",
            ],
            "fixed_isolation_names": [
                "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS",
                "CLAUDE_CODE_ATTRIBUTION_HEADER",
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
                "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS",
                "CLAUDE_CODE_DISABLE_CLAUDE_MDS",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
                "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL",
                "CLAUDE_CODE_DISABLE_POLICY_SKILLS",
                "CLAUDE_CODE_DISABLE_TERMINAL_TITLE",
                "CLAUDE_CODE_DISABLE_WORKFLOWS",
                "CLAUDE_CODE_SKIP_PROMPT_HISTORY",
                "DISABLE_COMPACT",
                "ENABLE_CLAUDEAI_MCP_SERVERS",
            ],
            "fingerprint_algorithm": "sha256_sorted_name_nul_value_nul_v1",
        },
        "auth_evidence": {
            "schema": "claude_sdk_proxy.child_attestation_manifest",
            "version": 1,
            "accepted_public_shape": accepted_shape,
            "provider": "anthropic",
            "endpoint": "default",
            "auth_source": "existing_claude_login",
            "revalidation": "each_turn" if all_core_gates else "unavailable",
            "core_gate_available": all_core_gates,
            "reason_codes": reasons,
        },
        "path_policy_evidence": {
            "version": 1,
            "snapshot_root_sentinel": ".",
            "path_safe_persistence": all_core_gates,
        },
        "backend_class": "agent_sdk_subscription",
        "auth_class": "existing_claude_login",
        "semantic_class": "prompt_isolated_agent_sdk",
        "model_map": model_map,
        "thinking_tuples": [],
        "usage_evidence": _usage_schema(
            tuple(model_map.values()),
            ordinary_passed=ordinary_usage_passed,
            include_tools=include_tool_rows,
            tool_public_mappings_passed=tool_public_mappings_passed,
        ),
        "sdk_tool_evidence": {"records": records},
    }


def _write_document(path: Path, document: object) -> None:
    path.write_text(
        json.dumps(document, ensure_ascii=True, allow_nan=False), encoding="utf-8"
    )


def _load_document(tmp_path: Path, document: dict[str, object]):
    path = tmp_path / "manifest.json"
    _write_document(path, document)
    return load_manifest(path)


def test_committed_manifest_has_every_core_domain_and_honest_false_verdict() -> None:
    manifest = load_manifest(Path("docs/feasibility/validated-environment.json"))

    assert set(manifest.core_gates) == CORE_GATES
    assert manifest.core_gates["personal_subscription_policy"] is False
    assert manifest.core_gates["exact_runtime_tuple"] is False
    assert manifest.core_gates["per_child_auth_attestation"] is False
    assert manifest.core_gates["exact_usage_schema"] is False
    assert not all(manifest.core_gates.values())
    with pytest.raises(ManifestError, match="false core gates"):
        require_core_gates(manifest)
    assert load_usage_evidence(manifest).rows
    assert load_sdk_tool_evidence(manifest).records == ()


def test_synthetic_complete_manifest_passes_every_core_gate(tmp_path: Path) -> None:
    manifest = _load_document(tmp_path, _manifest_document())

    require_core_gates(manifest)
    assert all(manifest.core_gates.values())
    assert load_usage_evidence(manifest).rows
    assert sdk_tool_gate_passed(manifest, _MODEL)


@pytest.mark.parametrize("mutation", ["missing", "extra", "integer", "subclass"])
def test_core_gate_map_is_exact_and_uses_literal_booleans(
    tmp_path: Path, mutation: str
) -> None:
    document = _manifest_document()
    gates = document["core_gates"]
    assert isinstance(gates, dict)
    if mutation == "missing":
        del gates["structured_user_input"]
    elif mutation == "extra":
        gates["future_gate"] = True
    elif mutation == "integer":
        gates["structured_user_input"] = 1
    else:

        class BooleanLike(int):
            pass

        gates["structured_user_input"] = BooleanLike(1)

    with pytest.raises(ManifestError, match="core gate"):
        _load_document(tmp_path, document)


@pytest.mark.parametrize(
    "level, required",
    [
        ("top", "schema_version"),
        ("policy", "sources"),
        ("runtime", "sdk_version"),
        ("mount", "mount_fsid"),
        ("lifecycle", "journal_matrix"),
        ("environment", "fingerprint_algorithm"),
        ("auth", "accepted_public_shape"),
        ("path", "version"),
    ],
)
def test_every_manifest_evidence_domain_rejects_missing_and_extra_fields(
    tmp_path: Path, level: str, required: str
) -> None:
    keys = {
        "top": None,
        "policy": "policy_evidence",
        "runtime": "runtime_evidence",
        "mount": "mount_identity",
        "lifecycle": "sync_lock_lifecycle_evidence",
        "environment": "environment_evidence",
        "auth": "auth_evidence",
        "path": "path_policy_evidence",
    }
    for mutation in ("missing", "extra"):
        document = _manifest_document()
        selected = keys[level]
        target = document if selected is None else document[selected]
        assert isinstance(target, dict)
        if mutation == "missing":
            del target[required]
        else:
            target["future_field"] = None
        with pytest.raises(ManifestError, match="field set"):
            _load_document(tmp_path, document)


def test_duplicate_json_keys_are_rejected_before_schema_validation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")

    with pytest.raises(ManifestError, match="duplicate JSON object key"):
        load_manifest(path)


def test_manifest_rejects_float_nan_bool_integer_and_invalid_unicode(
    tmp_path: Path,
) -> None:
    for mutation in ("float", "nan", "bool_integer", "unicode"):
        document = _manifest_document()
        runtime = document["runtime_evidence"]
        assert isinstance(runtime, dict)
        if mutation == "float":
            runtime["cli_executable_inode"] = 1.0
        elif mutation == "nan":
            runtime["cli_executable_inode"] = float("nan")
        elif mutation == "bool_integer":
            runtime["cli_executable_inode"] = True
        else:
            runtime["darwin_build"] = "bad\ud800"
        path = tmp_path / f"{mutation}.json"
        if mutation == "nan":
            path.write_text(json.dumps(document, allow_nan=True), encoding="utf-8")
        else:
            _write_document(path, document)
        with pytest.raises(ManifestError):
            load_manifest(path)


def test_canonical_evidence_json_has_one_minimal_unicode_encoding() -> None:
    assert (
        canonical_evidence_json(
            {"\N{GREEK SMALL LETTER BETA}": "\n", "a": 1, "false": False, "null": None}
        )
        == b'{"a":1,"false":false,"null":null,"\xce\xb2":"\\n"}'
    )
    assert canonical_evidence_json({"b": 2, "a": 1}) == canonical_evidence_json(
        {"a": 1, "b": 2}
    )


@pytest.mark.parametrize(
    "value",
    [
        1.0,
        float("nan"),
        b"bytes",
        "bad\ud800",
    ],
)
def test_canonical_evidence_json_rejects_noncanonical_scalars(value: object) -> None:
    with pytest.raises(ManifestError, match="canonical evidence"):
        canonical_evidence_json(value)


def test_canonical_evidence_json_rejects_subclasses_cycles_depth_and_items() -> None:
    class Text(str):
        pass

    class Integer(int):
        pass

    class Object(dict[str, object]):
        pass

    for value in (Text("text"), Integer(1), Object(a=1)):
        with pytest.raises(ManifestError, match="canonical evidence"):
            canonical_evidence_json(value)

    cycle: list[object] = []
    cycle.append(cycle)
    with pytest.raises(ManifestError, match="cycle"):
        canonical_evidence_json(cycle)

    deep: object = None
    for _ in range(40):
        deep = [deep]
    with pytest.raises(ManifestError, match="depth"):
        canonical_evidence_json(deep)

    with pytest.raises(ManifestError, match="item bound"):
        canonical_evidence_json([None] * 100_001)


def test_sdk_tool_digest_is_stable_for_reordered_maps_and_binds_valid_leaves(
    tmp_path: Path,
) -> None:
    document = _manifest_document()
    manifest = _load_document(tmp_path, document)
    record = load_sdk_tool_evidence(manifest).only_record
    original = sdk_tool_record_digest(record)

    reordered_document = copy.deepcopy(document)
    sdk = reordered_document["sdk_tool_evidence"]
    assert isinstance(sdk, dict)
    records = sdk["records"]
    assert isinstance(records, list)
    records[0] = dict(reversed(list(records[0].items())))  # type: ignore[union-attr]
    reordered = load_sdk_tool_evidence(
        _load_document(tmp_path, reordered_document)
    ).only_record
    assert sdk_tool_record_digest(reordered) == original

    mutations: tuple[tuple[tuple[str, ...], object], ...] = (
        (("key", "runtime_digest"), "aa" * 32),
        (("key", "cli_executable_device"), 8),
        (("key", "cli_executable_inode"), 12),
        (("key", "cli_executable_sha256"), "bb" * 32),
        (("key", "darwin_version"), "24.6.1"),
        (("key", "darwin_build"), "24G85"),
        (("key", "boot_id"), "cc" * 32),
        (("key", "mount_identity", "mount_device"), "disk9s9"),
        (("key", "mount_identity", "mount_fsid"), "ffffffff:eeeeeeee"),
        (("key", "mount_identity", "runtime_root_st_dev"), 99),
        (("key", "backend_model_id"), "claude-sonnet-other-exact"),
    )
    for path, value in mutations:
        changed_document = copy.deepcopy(document)
        changed_sdk = changed_document["sdk_tool_evidence"]
        assert isinstance(changed_sdk, dict)
        changed_records = changed_sdk["records"]
        assert isinstance(changed_records, list)
        target = changed_records[0]
        assert isinstance(target, dict)
        for segment in path[:-1]:
            target = target[segment]  # type: ignore[assignment]
            assert isinstance(target, dict)
        target[path[-1]] = value
        changed_manifest = _load_document(tmp_path, changed_document)
        changed = load_sdk_tool_evidence(changed_manifest).only_record
        assert sdk_tool_record_digest(changed) != original

    for gate in REQUIRED_SDK_TOOL_GATES:
        changed_document = copy.deepcopy(document)
        changed_sdk = changed_document["sdk_tool_evidence"]
        assert isinstance(changed_sdk, dict)
        changed_records = changed_sdk["records"]
        assert isinstance(changed_records, list)
        gates = changed_records[0]["gates"]  # type: ignore[index]
        assert isinstance(gates, dict)
        gates[gate] = False
        changed = load_sdk_tool_evidence(
            _load_document(tmp_path, changed_document)
        ).only_record
        assert sdk_tool_record_digest(changed) != original


def test_sdk_tool_digest_rejects_unloaded_and_tampered_records(tmp_path: Path) -> None:
    direct = SdkToolEvidenceRecord.from_json(_sdk_record())
    with pytest.raises(ManifestError, match="SDK tool record schema"):
        sdk_tool_record_digest(direct)

    for mutation in ("record_digest", "unknown_gate", "wrong_rule"):
        document = _manifest_document()
        sdk = document["sdk_tool_evidence"]
        assert isinstance(sdk, dict)
        records = sdk["records"]
        assert isinstance(records, list)
        record = records[0]
        assert isinstance(record, dict)
        if mutation == "record_digest":
            record["record_digest"] = "00" * 32
        elif mutation == "unknown_gate":
            gates = record["gates"]
            assert isinstance(gates, dict)
            gates["future_gate"] = True
        else:
            naming = record["naming_rule"]
            assert isinstance(naming, dict)
            naming["version"] = 2
        manifest = _load_document(tmp_path, document)
        with pytest.raises(ManifestError, match="SDK tool record schema"):
            load_sdk_tool_evidence(manifest)


def test_sdk_tool_digest_matches_the_exact_v1_projection(tmp_path: Path) -> None:
    record = load_sdk_tool_evidence(
        _load_document(tmp_path, _manifest_document())
    ).only_record
    projection = {
        "digest_schema_version": 1,
        "record": {
            "schema_version": record.schema_version,
            "key": {
                "runtime_digest": record.key.runtime_digest,
                "sdk_version": record.key.sdk_version,
                "cli_version": record.key.cli_version,
                "cli_executable_device": record.key.cli_executable_device,
                "cli_executable_inode": record.key.cli_executable_inode,
                "cli_executable_sha256": record.key.cli_executable_sha256,
                "darwin_version": record.key.darwin_version,
                "darwin_build": record.key.darwin_build,
                "boot_id": record.key.boot_id,
                "mount_identity": {
                    "filesystem_type": record.key.mount_identity.filesystem_type,
                    "is_local": record.key.mount_identity.is_local,
                    "mount_device": record.key.mount_identity.mount_device,
                    "mount_fsid": record.key.mount_identity.mount_fsid,
                    "mount_flags": record.key.mount_identity.mount_flags,
                    "runtime_root_st_dev": (
                        record.key.mount_identity.runtime_root_st_dev
                    ),
                },
                "backend_class": record.key.backend_class,
                "auth_class": record.key.auth_class,
                "semantic_class": record.key.semantic_class,
                "backend_model_id": record.key.backend_model_id,
            },
            "naming_rule": {
                "version": record.naming_rule.version,
                "server_identity": record.naming_rule.server_identity,
                "caller_name_pattern": record.naming_rule.caller_name_pattern,
                "caller_name_max_bytes": record.naming_rule.caller_name_max_bytes,
                "generated_name_template_or_algorithm": (
                    record.naming_rule.generated_name_template_or_algorithm
                ),
                "representative_observations": (
                    record.naming_rule.representative_observations
                ),
            },
            "gates": record.gates,
        },
    }
    expected = hashlib.sha256(
        b"claude-sdk-proxy:sdk-tool-record:v1\0" + canonical_evidence_json(projection)
    ).hexdigest()

    assert sdk_tool_record_digest(record) == expected


def test_phase0_prerequisite_digest_binds_core_runtime_mount_and_model_map(
    tmp_path: Path,
) -> None:
    original_document = _manifest_document()
    original = _load_document(tmp_path, original_document)
    original_digest = phase0_prerequisite_digest(original, "sonnet", _MODEL)

    mutations: tuple[tuple[tuple[str, ...], object], ...] = (
        (("validated_at",), "2026-09-01T12:00:01Z"),
        (("policy_evidence", "sources", "0", "sha256"), "99" * 32),
        (("runtime_evidence", "python_executable"), "/opt/python/bin/python3"),
        (("runtime_evidence", "cli_executable_sha256"), "99" * 32),
        (("runtime_evidence", "darwin_build"), "24G91"),
        (("mount_identity", "mount_device"), "disk9s9"),
        (("mount_identity", "mount_fsid"), "ffffffff:eeeeeeee"),
        (("mount_identity", "runtime_root_st_dev"), 999),
        (
            ("sync_lock_lifecycle_evidence", "journal_matrix", "bounded_journal"),
            False,
        ),
    )
    for path, value in mutations:
        changed_document = copy.deepcopy(original_document)
        target: object = changed_document
        for segment in path[:-1]:
            if isinstance(target, list):
                target = target[int(segment)]
            else:
                assert isinstance(target, dict)
                target = target[segment]
        assert isinstance(target, dict)
        target[path[-1]] = value
        if path[-1] == "bounded_journal":
            gates = changed_document["core_gates"]
            assert isinstance(gates, dict)
            gates["bounded_journal"] = False
        changed = _load_document(tmp_path, changed_document)
        if path[-1] == "bounded_journal":
            with pytest.raises(ManifestError, match="false core gates"):
                phase0_prerequisite_digest(changed, "sonnet", _MODEL)
        else:
            assert phase0_prerequisite_digest(changed, "sonnet", _MODEL) != (
                original_digest
            )

    two_model_document = _manifest_document(
        models={"sonnet": _MODEL, "opus": _OTHER_MODEL}
    )
    two_model = _load_document(tmp_path, two_model_document)
    assert phase0_prerequisite_digest(two_model, "sonnet", _MODEL) != (
        phase0_prerequisite_digest(two_model, "opus", _OTHER_MODEL)
    )


def test_phase0_digest_requires_the_exact_alias_model_pair(tmp_path: Path) -> None:
    manifest = _load_document(tmp_path, _manifest_document())
    for alias, model in (("missing", _MODEL), ("sonnet", _OTHER_MODEL)):
        with pytest.raises(ManifestError, match="exact alias/backend model"):
            phase0_prerequisite_digest(manifest, alias, model)


def test_phase0_digest_resolver_is_immutable_and_rejects_bad_maps(
    tmp_path: Path,
) -> None:
    manifest = _load_document(
        tmp_path,
        _manifest_document(models={"sonnet": _MODEL, "opus": _OTHER_MODEL}),
    )
    resolver = Phase0PrerequisiteDigestResolver.from_manifest(manifest)
    sonnet = resolver.resolve("sonnet", _MODEL)
    opus = resolver.resolve("opus", _OTHER_MODEL)
    assert sonnet != opus
    with pytest.raises(TypeError):
        resolver.digests[("sonnet", _MODEL)] = opus  # type: ignore[index]
    with pytest.raises(ManifestError, match="exact model prerequisite digest"):
        resolver.resolve("sonnet", _OTHER_MODEL)

    expected = dict(resolver.digests)
    bad_maps = (
        {key: value for key, value in expected.items() if key[0] != "opus"},
        {**expected, ("extra", "backend-extra-exact"): "aa" * 32},
        {
            ("sonnet", _MODEL): expected[("opus", _OTHER_MODEL)],
            ("opus", _OTHER_MODEL): expected[("sonnet", _MODEL)],
        },
    )
    for bad in bad_maps:
        corrupted = Phase0PrerequisiteDigestResolver(bad)
        with pytest.raises(ManifestError, match="exact model prerequisite digest"):
            corrupted.validate(manifest)


def test_duplicate_backend_model_aliases_are_ambiguous(tmp_path: Path) -> None:
    document = _manifest_document()
    document["model_map"] = {"sonnet": _MODEL, "second": _MODEL}
    with pytest.raises(ManifestError, match="ambiguous exact backend model"):
        _load_document(tmp_path, document)


def test_usage_loader_requires_exact_runtime_models_and_core_null_mapping(
    tmp_path: Path,
) -> None:
    wrong_runtime = _manifest_document()
    wrong_runtime["usage_evidence"] = _usage_schema((_MODEL,), runtime_digest="aa" * 32)
    with pytest.raises(ManifestError, match="runtime digest"):
        load_usage_evidence(_load_document(tmp_path, wrong_runtime))

    wrong_model = _manifest_document()
    wrong_model["usage_evidence"] = _usage_schema((_OTHER_MODEL,))
    with pytest.raises(ManifestError, match="configured exact backend model"):
        load_usage_evidence(_load_document(tmp_path, wrong_model))

    false_core_row = _manifest_document(ordinary_usage_passed=False)
    with pytest.raises(ManifestError, match="ordinary Anthropic"):
        load_usage_evidence(_load_document(tmp_path, false_core_row))


def test_usage_loader_revalidates_supplied_mapping_row_and_schema_digests(
    tmp_path: Path,
) -> None:
    for target in ("mapping", "row", "schema"):
        document = _manifest_document()
        usage = document["usage_evidence"]
        assert isinstance(usage, dict)
        rows = usage["rows"]
        assert isinstance(rows, list)
        if target == "mapping":
            mappings = rows[0]["dialect_mappings"]  # type: ignore[index]
            assert isinstance(mappings, list)
            mappings[0]["mapping_digest"] = "00" * 32  # type: ignore[index]
        elif target == "row":
            rows[0]["row_digest"] = "00" * 32  # type: ignore[index]
        else:
            usage["schema_digest"] = "00" * 32
        with pytest.raises(ManifestError, match="usage evidence"):
            load_usage_evidence(_load_document(tmp_path, document))


def test_sdk_tool_gate_is_optional_exact_and_independent_of_public_mappings(
    tmp_path: Path,
) -> None:
    false_tools = _load_document(tmp_path, _manifest_document(tool_gates_passed=False))
    assert not sdk_tool_gate_passed(false_tools, _MODEL)
    assert not sdk_tool_gate_passed(false_tools, _OTHER_MODEL)

    missing_rows = _load_document(tmp_path, _manifest_document(include_tool_rows=False))
    assert not sdk_tool_gate_passed(missing_rows, _MODEL)

    false_public = _load_document(
        tmp_path,
        _manifest_document(tool_public_mappings_passed=False),
    )
    assert sdk_tool_gate_passed(false_public, _MODEL)


def test_sdk_tool_loader_rejects_duplicate_exact_records(tmp_path: Path) -> None:
    document = _manifest_document()
    sdk = document["sdk_tool_evidence"]
    assert isinstance(sdk, dict)
    records = sdk["records"]
    assert isinstance(records, list)
    records.append(copy.deepcopy(records[0]))

    with pytest.raises(ManifestError, match="SDK tool record schema"):
        load_sdk_tool_evidence(_load_document(tmp_path, document))


def test_atomic_manifest_writer_uses_canonical_mode_0600_file(tmp_path: Path) -> None:
    output = tmp_path / "evidence.json"
    document = {"z": 2, "a": 1}

    validated._atomic_write_manifest(output, document)

    assert output.read_bytes() == b'{"a":1,"z":2}\n'
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [output]


def test_atomic_writer_failure_before_rename_preserves_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "evidence.json"
    output.write_bytes(b"original\n")
    output.chmod(0o600)

    def fail_fullfsync(_descriptor: int) -> None:
        raise OSError("injected failure")

    monkeypatch.setattr(validated, "_fullfsync_fd", fail_fullfsync)
    with pytest.raises(ManifestError, match="atomic manifest write"):
        validated._atomic_write_manifest(output, {"a": 1})

    assert output.read_bytes() == b"original\n"
    assert list(tmp_path.iterdir()) == [output]


def test_all_command_requires_live_opt_in_and_never_overwrites_false_tuple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "validated.json"
    output.write_bytes(b"original\n")
    output.chmod(0o600)
    argv = [
        "all",
        "--ack-personal-local-use-policy",
        "--output",
        str(output),
    ]

    monkeypatch.delenv("RUN_LIVE_CLAUDE_TESTS", raising=False)
    assert probe_cli.main(argv) != 0
    assert output.read_bytes() == b"original\n"

    monkeypatch.setenv("RUN_LIVE_CLAUDE_TESTS", "1")
    assert probe_cli.main(argv) != 0
    assert output.read_bytes() == b"original\n"


def test_caller_policy_acknowledgment_is_not_policy_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "never-created.json"
    monkeypatch.setenv("RUN_LIVE_CLAUDE_TESTS", "1")

    status = probe_cli.main(
        [
            "all",
            "--ack-personal-local-use-policy",
            "--output",
            str(output),
        ]
    )

    assert status != 0
    assert not output.exists()


def test_hostile_mapping_subclasses_are_rejected_without_iteration(
    tmp_path: Path,
) -> None:
    class HostileMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            raise AssertionError("must not inspect hostile mappings")

        def __iter__(self) -> Iterator[str]:
            raise AssertionError("must not iterate hostile mappings")

        def __len__(self) -> int:
            raise AssertionError("must not size hostile mappings")

    document = _manifest_document()
    document["core_gates"] = HostileMapping()
    with pytest.raises(TypeError):
        json.dumps(document)

    with pytest.raises(ManifestError, match="canonical evidence"):
        canonical_evidence_json(HostileMapping())


def test_committed_manifest_contains_no_forbidden_content_fields() -> None:
    raw = json.loads(
        Path("docs/feasibility/validated-environment.json").read_text(encoding="utf-8")
    )
    forbidden = {
        "prompt",
        "prompts",
        "response",
        "responses",
        "observed_usage_values",
        "session_id",
        "session_ids",
        "credential_path",
        "credential_paths",
        "environment_values",
        "credentials",
        "transport_bytes",
    }
    stack = [raw]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            assert forbidden.isdisjoint(value)
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)


def test_environment_allowed_name_arrays_are_sorted_unique_and_content_free(
    tmp_path: Path,
) -> None:
    for mutation in ("unsorted", "duplicate", "missing", "value_field"):
        document = _manifest_document()
        environment = document["environment_evidence"]
        assert isinstance(environment, dict)
        names = environment["allowed_names"]
        assert isinstance(names, list)
        if mutation == "unsorted":
            environment["allowed_names"] = list(reversed(names))
        elif mutation == "duplicate":
            names.append(names[0])
        elif mutation == "missing":
            names.pop()
        else:
            environment["values"] = {"HOME": "/secret"}
        with pytest.raises(ManifestError):
            _load_document(tmp_path, document)


def test_path_policy_sentinel_is_exact(tmp_path: Path) -> None:
    document = _manifest_document()
    path_policy = document["path_policy_evidence"]
    assert isinstance(path_policy, dict)
    path_policy["snapshot_root_sentinel"] = "root"
    with pytest.raises(ManifestError, match="snapshot root sentinel"):
        _load_document(tmp_path, document)


def test_manifest_loader_has_bounded_file_size(tmp_path: Path) -> None:
    path = tmp_path / "oversized.json"
    path.write_bytes(b" " * (1_048_576 + 1))
    with pytest.raises(ManifestError, match="byte bound"):
        load_manifest(path)


def test_probe_all_rejects_nonexact_invocation_without_writing(tmp_path: Path) -> None:
    output = tmp_path / "output.json"
    invocations = (
        ["all", "--output", str(output)],
        ["all", "--ack-personal-local-use-policy"],
        [
            "all",
            "--ack-personal-local-use-policy",
            "--output",
            str(output),
            "extra",
        ],
    )
    for argv in invocations:
        assert probe_cli.main(argv) != 0
        assert not output.exists()


def test_manifest_file_mode_is_private() -> None:
    path = Path("docs/feasibility/validated-environment.json")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_resolver_rejects_nonexact_digest_values() -> None:
    with pytest.raises(ManifestError, match="prerequisite digest"):
        Phase0PrerequisiteDigestResolver({("sonnet", _MODEL): "not-a-digest"})


def test_canonical_encoder_preserves_array_order() -> None:
    left = canonical_evidence_json({"values": [1, 2]})
    right = canonical_evidence_json({"values": [2, 1]})
    assert left != right


def test_manifest_json_parser_rejects_nonfinite_constants(tmp_path: Path) -> None:
    path = tmp_path / "constant.json"
    path.write_text('{"value":NaN}', encoding="utf-8")
    with pytest.raises(ManifestError, match="non-finite"):
        load_manifest(path)


def test_sdk_gate_requires_exact_builtin_model_text(tmp_path: Path) -> None:
    class Text(str):
        pass

    manifest = _load_document(tmp_path, _manifest_document())
    assert sdk_tool_gate_passed(manifest, "missing-exact") is False
    with pytest.raises(ManifestError, match="exact backend model"):
        sdk_tool_gate_passed(manifest, Text(_MODEL))


def test_manifest_model_map_rejects_moving_alias_backend_ids(tmp_path: Path) -> None:
    document = _manifest_document()
    document["model_map"] = {"sonnet": "sonnet"}
    with pytest.raises(ManifestError, match="exact backend model"):
        _load_document(tmp_path, document)


def test_committed_manifest_is_canonical_json_with_one_trailing_newline() -> None:
    path = Path("docs/feasibility/validated-environment.json")
    manifest = load_manifest(path)
    assert path.read_bytes() == canonical_evidence_json(manifest.to_json()) + b"\n"


def test_atomic_writer_refuses_nonexact_path_and_document_types(
    tmp_path: Path,
) -> None:
    class PathSubclass(type(Path())):  # type: ignore[misc]
        pass

    with pytest.raises(ManifestError, match="output path"):
        validated._atomic_write_manifest(str(tmp_path / "x"), {"a": 1})  # type: ignore[arg-type]
    with pytest.raises(ManifestError, match="canonical evidence"):
        validated._atomic_write_manifest(tmp_path / "x", {"a": object()})


def test_resolver_constructor_copies_caller_mapping() -> None:
    source = {("sonnet", _MODEL): "aa" * 32}
    resolver = Phase0PrerequisiteDigestResolver(source)
    source.clear()
    assert resolver.digests == {("sonnet", _MODEL): "aa" * 32}


def test_manifest_loader_rejects_directory_and_missing_path(tmp_path: Path) -> None:
    for path in (tmp_path, tmp_path / "missing.json"):
        with pytest.raises(ManifestError, match="manifest file"):
            load_manifest(path)


def test_manifest_rejects_forbidden_content_key_even_inside_typed_evidence(
    tmp_path: Path,
) -> None:
    document = _manifest_document()
    auth = document["auth_evidence"]
    assert isinstance(auth, dict)
    auth["session_id"] = "forbidden"
    with pytest.raises(ManifestError, match="field set|forbidden"):
        _load_document(tmp_path, document)


def test_phase0_projection_changes_when_an_unselected_model_mapping_changes(
    tmp_path: Path,
) -> None:
    document = _manifest_document(models={"sonnet": _MODEL, "opus": _OTHER_MODEL})
    original = _load_document(tmp_path, document)
    original_digest = phase0_prerequisite_digest(original, "sonnet", _MODEL)
    changed_document = copy.deepcopy(document)
    model_map = changed_document["model_map"]
    assert isinstance(model_map, dict)
    model_map["opus"] = "claude-opus-other-exact"
    usage = changed_document["usage_evidence"]
    assert isinstance(usage, dict)
    changed_document["usage_evidence"] = _usage_schema(
        (_MODEL, "claude-opus-other-exact")
    )
    sdk = changed_document["sdk_tool_evidence"]
    assert isinstance(sdk, dict)
    records = sdk["records"]
    assert isinstance(records, list)
    records[1] = _sdk_record("claude-opus-other-exact")
    changed = _load_document(tmp_path, changed_document)
    assert phase0_prerequisite_digest(changed, "sonnet", _MODEL) != original_digest


def test_phase0_projection_domain_separator_is_not_plain_json_hash(
    tmp_path: Path,
) -> None:
    manifest = _load_document(tmp_path, _manifest_document())
    digest = phase0_prerequisite_digest(manifest, "sonnet", _MODEL)
    assert (
        digest
        != hashlib.sha256(canonical_evidence_json(manifest.to_json())).hexdigest()
    )


def test_sdk_projection_domain_separator_is_not_plain_record_hash(
    tmp_path: Path,
) -> None:
    record = load_sdk_tool_evidence(
        _load_document(tmp_path, _manifest_document())
    ).only_record
    assert (
        sdk_tool_record_digest(record)
        != hashlib.sha256(canonical_evidence_json(record.to_json())).hexdigest()
    )


def test_false_tool_gate_never_uses_representative_names_as_allowlist(
    tmp_path: Path,
) -> None:
    manifest = _load_document(tmp_path, _manifest_document(tool_gates_passed=False))
    record = load_sdk_tool_evidence(manifest).only_record
    assert record.naming_rule.derive("new-name") == ("mcp__caller_tools_v1__new-name")
    assert not sdk_tool_gate_passed(manifest, _MODEL)


def test_usage_false_optional_tool_mapping_does_not_fail_core_gate(
    tmp_path: Path,
) -> None:
    manifest = _load_document(
        tmp_path,
        _manifest_document(tool_public_mappings_passed=False),
    )
    require_core_gates(manifest)
    assert load_usage_evidence(manifest).rows


def test_manifest_immutable_views_do_not_alias_source(tmp_path: Path) -> None:
    document = _manifest_document()
    manifest = _load_document(tmp_path, document)
    gates = document["core_gates"]
    model_map = document["model_map"]
    assert isinstance(gates, dict)
    assert isinstance(model_map, dict)
    gates["exact_runtime_tuple"] = False
    model_map["sonnet"] = _OTHER_MODEL

    assert manifest.core_gates["exact_runtime_tuple"] is True
    assert manifest.model_map["sonnet"] == _MODEL
    with pytest.raises(TypeError):
        manifest.core_gates["exact_runtime_tuple"] = False  # type: ignore[index]


def test_manifest_usage_and_sdk_snapshots_do_not_alias_source(tmp_path: Path) -> None:
    document = _manifest_document()
    manifest = _load_document(tmp_path, document)
    usage_before = load_usage_evidence(manifest)
    sdk_before = load_sdk_tool_evidence(manifest)
    usage = document["usage_evidence"]
    sdk = document["sdk_tool_evidence"]
    assert isinstance(usage, dict)
    assert isinstance(sdk, dict)
    usage.clear()
    sdk.clear()

    assert load_usage_evidence(manifest) == usage_before
    assert load_sdk_tool_evidence(manifest) == sdk_before


def test_probe_all_does_not_call_atomic_writer_while_policy_is_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def forbidden_writer(_path: Path, _document: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setenv("RUN_LIVE_CLAUDE_TESTS", "1")
    monkeypatch.setattr(validated, "_atomic_write_manifest", forbidden_writer)
    output = tmp_path / "output.json"
    assert (
        probe_cli.main(
            [
                "all",
                "--ack-personal-local-use-policy",
                "--output",
                str(output),
            ]
        )
        != 0
    )
    assert called is False
    assert not output.exists()


def test_manifest_runtime_identity_rejects_partial_cli_identity(tmp_path: Path) -> None:
    for field in (
        "cli_path",
        "cli_path_sha256",
        "cli_executable_device",
        "cli_executable_inode",
        "cli_executable_mode",
        "cli_executable_sha256",
    ):
        document = _manifest_document()
        runtime = document["runtime_evidence"]
        assert isinstance(runtime, dict)
        runtime[field] = None
        with pytest.raises(ManifestError, match="CLI identity"):
            _load_document(tmp_path, document)


def test_manifest_top_level_container_must_be_exact_object(tmp_path: Path) -> None:
    path = tmp_path / "array.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ManifestError, match="exact JSON object"):
        load_manifest(path)


def test_atomic_writer_rejects_non_absolute_parent_ambiguity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ManifestError, match="absolute"):
        validated._atomic_write_manifest(Path("relative.json"), {"a": 1})


def test_sdk_tool_gate_revalidates_usage_runtime_key(tmp_path: Path) -> None:
    document = _manifest_document()
    usage = document["usage_evidence"]
    assert isinstance(usage, dict)
    rows = usage["rows"]
    assert isinstance(rows, list)
    for row in rows:
        key = row["key"]  # type: ignore[index]
        assert isinstance(key, dict)
        if key["operation_class"] != "ordinary":
            key["runtime_digest"] = "aa" * 32
        row.pop("row_digest", None)  # type: ignore[union-attr]
        for mapping in row["dialect_mappings"]:  # type: ignore[index]
            mapping.pop("mapping_digest", None)
    document["usage_evidence"] = UsageEvidenceSchema.from_json(
        {"schema_version": 1, "rows": rows}
    ).to_json()
    manifest = _load_document(tmp_path, document)
    with pytest.raises(ManifestError, match="runtime digest"):
        load_usage_evidence(manifest)
    with pytest.raises(ManifestError, match="runtime digest"):
        sdk_tool_gate_passed(manifest, _MODEL)


def test_core_gate_evidence_matrix_cannot_disagree_with_core_map(
    tmp_path: Path,
) -> None:
    document = _manifest_document()
    lifecycle = document["sync_lock_lifecycle_evidence"]
    assert isinstance(lifecycle, dict)
    journal = lifecycle["journal_matrix"]
    assert isinstance(journal, dict)
    journal["bounded_journal"] = False
    with pytest.raises(ManifestError, match="core gate evidence mismatch"):
        _load_document(tmp_path, document)


def test_false_manifest_can_preserve_proved_deterministic_matrices(
    tmp_path: Path,
) -> None:
    document = _manifest_document(all_core_gates=False, ordinary_usage_passed=False)
    gates = document["core_gates"]
    assert isinstance(gates, dict)
    proved = {
        "darwin_local_apfs",
        "required_sync_primitives",
        "bsd_flock_model",
        "bounded_journal",
        "root_reconciliation_lock",
        "instance_lifetime_lock",
        "owner_record_create",
        "owner_record_replace",
        "durable_head_certification",
        "cleanup_automaton",
        "retaining_supervisor",
        "anchor_unconfirmed_fallback",
        "exact_environment",
        "structured_user_input",
    }
    for gate in proved:
        gates[gate] = True
    path_policy = document["path_policy_evidence"]
    assert isinstance(path_policy, dict)
    path_policy["path_safe_persistence"] = False
    manifest = _load_document(tmp_path, document)

    assert all(manifest.core_gates[gate] for gate in proved)
    with pytest.raises(ManifestError, match="false core gates"):
        require_core_gates(manifest)


def test_policy_source_order_is_canonicalized_but_mount_flag_order_is_validated(
    tmp_path: Path,
) -> None:
    document = _manifest_document()
    policy = document["policy_evidence"]
    assert isinstance(policy, dict)
    sources = policy["sources"]
    assert isinstance(sources, list)
    policy["sources"] = list(reversed(sources))
    manifest = _load_document(tmp_path, document)
    assert tuple(source["url"] for source in manifest.policy_sources) == tuple(
        sorted(source["url"] for source in sources)
    )

    invalid = _manifest_document()
    mount = invalid["mount_identity"]
    assert isinstance(mount, dict)
    mount["mount_flags"] = ["local", "journaled"]
    with pytest.raises(ManifestError, match="mount flags"):
        _load_document(tmp_path, invalid)


def test_phase0_resolver_requires_all_core_gates(tmp_path: Path) -> None:
    document = _manifest_document(all_core_gates=False, ordinary_usage_passed=False)
    manifest = _load_document(tmp_path, document)
    with pytest.raises(ManifestError, match="false core gates"):
        Phase0PrerequisiteDigestResolver.from_manifest(manifest)


def test_usage_key_budget_class_is_rejected_through_manifest_loader(
    tmp_path: Path,
) -> None:
    document = _manifest_document()
    usage = document["usage_evidence"]
    assert isinstance(usage, dict)
    rows = usage["rows"]
    assert isinstance(rows, list)
    key = rows[0]["key"]  # type: ignore[index]
    assert isinstance(key, dict)
    key["budget_class"] = "large"
    with pytest.raises(ManifestError, match="usage evidence"):
        load_usage_evidence(_load_document(tmp_path, document))


def test_usage_exact_budget_change_invalidates_supplied_digests(tmp_path: Path) -> None:
    document = _manifest_document()
    usage = document["usage_evidence"]
    assert isinstance(usage, dict)
    rows = usage["rows"]
    assert isinstance(rows, list)
    key = rows[0]["key"]  # type: ignore[index]
    assert isinstance(key, dict)
    key.update(thinking_mode="enabled", effort="high", budget_tokens=4096)
    with pytest.raises(ManifestError, match="usage evidence"):
        load_usage_evidence(_load_document(tmp_path, document))


def test_sdk_record_projection_does_not_include_container_position(
    tmp_path: Path,
) -> None:
    document = _manifest_document(models={"sonnet": _MODEL, "opus": _OTHER_MODEL})
    manifest = _load_document(tmp_path, document)
    records = load_sdk_tool_evidence(manifest).records
    by_model = {
        record.key.backend_model_id: sdk_tool_record_digest(record)
        for record in records
    }
    sdk = document["sdk_tool_evidence"]
    assert isinstance(sdk, dict)
    source_records = sdk["records"]
    assert isinstance(source_records, list)
    sdk["records"] = list(reversed(source_records))
    reordered = load_sdk_tool_evidence(_load_document(tmp_path, document)).records
    assert {
        record.key.backend_model_id: sdk_tool_record_digest(record)
        for record in reordered
    } == by_model


def test_manifest_rejects_mapping_subclass_directly_in_canonical_encoder() -> None:
    class SafeLookingMapping(dict[str, object]):
        pass

    with pytest.raises(ManifestError, match="canonical evidence"):
        canonical_evidence_json(SafeLookingMapping(a=1))


def test_committed_manifest_loader_and_gate_rejection_are_deterministic() -> None:
    first = load_manifest(Path("docs/feasibility/validated-environment.json"))
    second = load_manifest(Path("docs/feasibility/validated-environment.json"))
    assert first == second
    for manifest in (first, second):
        with pytest.raises(ManifestError) as error:
            require_core_gates(manifest)
        assert str(error.value) == (
            "Phase 0 manifest has false core gates: "
            "attribution_absent_observable, auth_source_lifetime, "
            "compaction_disabled, exact_backend_model, exact_runtime_tuple, "
            "exact_usage_schema, native_session_continuity, "
            "path_safe_persistence, per_child_auth_attestation, "
            "personal_subscription_policy, preinput_network_gate, "
            "prompt_isolation, streaming_event_contract"
        )


def test_atomic_writer_does_not_leave_temp_file_on_open_failure(
    tmp_path: Path,
) -> None:
    missing_parent = tmp_path / "missing" / "evidence.json"
    with pytest.raises(ManifestError, match="atomic manifest write"):
        validated._atomic_write_manifest(missing_parent, {"a": 1})
    assert list(tmp_path.iterdir()) == []


def test_manifest_timestamps_require_canonical_utc_seconds(tmp_path: Path) -> None:
    for value in (
        "2026-09-01T12:00:00+00:00",
        "2026-09-01T12:00:00.000Z",
        "2026-09-01 12:00:00Z",
    ):
        document = _manifest_document()
        document["validated_at"] = value
        with pytest.raises(ManifestError, match="UTC timestamp"):
            _load_document(tmp_path, document)


def test_manifest_policy_sources_are_exact_primary_urls(tmp_path: Path) -> None:
    document = _manifest_document()
    policy = document["policy_evidence"]
    assert isinstance(policy, dict)
    sources = policy["sources"]
    assert isinstance(sources, list)
    sources[0]["url"] = "https://example.test/not-primary"  # type: ignore[index]
    with pytest.raises(ManifestError, match="policy source"):
        _load_document(tmp_path, document)


def test_manifest_runtime_gate_records_observed_cli_mismatch(tmp_path: Path) -> None:
    document = _manifest_document()
    runtime = document["runtime_evidence"]
    gates = document["core_gates"]
    assert isinstance(runtime, dict)
    assert isinstance(gates, dict)
    runtime["observed_cli_version"] = "2.1.252"
    gates["exact_runtime_tuple"] = False
    manifest = _load_document(tmp_path, document)
    assert manifest.cli_version == "2.1.251"
    assert manifest.observed_cli_version == "2.1.252"
    with pytest.raises(ManifestError, match="false core gates"):
        require_core_gates(manifest)


def test_true_runtime_gate_rejects_observed_cli_mismatch(tmp_path: Path) -> None:
    document = _manifest_document()
    runtime = document["runtime_evidence"]
    assert isinstance(runtime, dict)
    runtime["observed_cli_version"] = "2.1.252"
    with pytest.raises(ManifestError, match="runtime gate"):
        _load_document(tmp_path, document)


def test_sdk_loader_validates_task9_schema_but_gate_rejects_wrong_runtime(
    tmp_path: Path,
) -> None:
    for field, value in (
        ("runtime_digest", "aa" * 32),
        ("cli_executable_inode", 99),
        (
            "mount_identity",
            {
                "filesystem_type": "apfs",
                "is_local": True,
                "mount_device": "disk9s9",
                "mount_fsid": "00112233:44556677",
                "mount_flags": ["journaled", "local"],
                "runtime_root_st_dev": 17,
            },
        ),
    ):
        document = _manifest_document()
        sdk = document["sdk_tool_evidence"]
        assert isinstance(sdk, dict)
        records = sdk["records"]
        assert isinstance(records, list)
        key = records[0]["key"]  # type: ignore[index]
        assert isinstance(key, dict)
        key[field] = value
        manifest = _load_document(tmp_path, document)
        assert load_sdk_tool_evidence(manifest).only_record
        assert sdk_tool_gate_passed(manifest, _MODEL) is False


def test_false_core_behavior_does_not_depend_on_mapping_order(tmp_path: Path) -> None:
    document = _manifest_document(all_core_gates=False, ordinary_usage_passed=False)
    gates = document["core_gates"]
    assert isinstance(gates, dict)
    document["core_gates"] = dict(reversed(list(gates.items())))
    manifest = _load_document(tmp_path, document)
    with pytest.raises(ManifestError) as error:
        require_core_gates(manifest)
    assert str(error.value).startswith("Phase 0 manifest has false core gates: ")


def test_sdk_tool_manifest_outer_schema_is_exact(tmp_path: Path) -> None:
    for mutation in ("missing", "extra"):
        document = _manifest_document()
        sdk = document["sdk_tool_evidence"]
        assert isinstance(sdk, dict)
        if mutation == "missing":
            del sdk["records"]
        else:
            sdk["schema_version"] = 1
        manifest = _load_document(tmp_path, document)
        with pytest.raises(ManifestError, match="SDK tool evidence field set"):
            load_sdk_tool_evidence(manifest)


def test_usage_evidence_root_schema_is_exact(tmp_path: Path) -> None:
    document = _manifest_document()
    usage = document["usage_evidence"]
    assert isinstance(usage, dict)
    usage["future"] = None
    with pytest.raises(ManifestError, match="usage evidence"):
        load_usage_evidence(_load_document(tmp_path, document))


def test_sdk_tool_gate_returns_false_for_missing_optional_records(
    tmp_path: Path,
) -> None:
    document = _manifest_document()
    document["sdk_tool_evidence"] = {"records": []}
    manifest = _load_document(tmp_path, document)
    assert load_sdk_tool_evidence(manifest).records == ()
    assert sdk_tool_gate_passed(manifest, _MODEL) is False


def test_optional_sdk_records_do_not_change_phase0_prerequisite_digest(
    tmp_path: Path,
) -> None:
    document = _manifest_document()
    with_tools = _load_document(tmp_path, document)
    digest = phase0_prerequisite_digest(with_tools, "sonnet", _MODEL)
    document["sdk_tool_evidence"] = {"records": []}
    without_tools = _load_document(tmp_path, document)
    assert phase0_prerequisite_digest(without_tools, "sonnet", _MODEL) == digest


def test_usage_evidence_does_not_change_phase0_prerequisite_digest(
    tmp_path: Path,
) -> None:
    document = _manifest_document()
    original = _load_document(tmp_path, document)
    digest = phase0_prerequisite_digest(original, "sonnet", _MODEL)
    document["usage_evidence"] = _usage_schema(
        (_MODEL,), tool_public_mappings_passed=False
    )
    changed = _load_document(tmp_path, document)
    assert phase0_prerequisite_digest(changed, "sonnet", _MODEL) == digest


def test_phase0_digest_rejects_manifest_subclasses() -> None:
    class FakeManifest:
        pass

    with pytest.raises(ManifestError, match="FeasibilityManifest"):
        phase0_prerequisite_digest(FakeManifest(), "sonnet", _MODEL)  # type: ignore[arg-type]


def test_sdk_digest_rejects_record_subclasses(tmp_path: Path) -> None:
    record = load_sdk_tool_evidence(
        _load_document(tmp_path, _manifest_document())
    ).only_record

    class RecordSubclass(SdkToolEvidenceRecord):
        pass

    with pytest.raises(ManifestError, match="SDK tool record schema"):
        sdk_tool_record_digest(record.__class__.__new__(RecordSubclass))


def test_manifest_output_file_is_not_world_readable() -> None:
    mode = Path("docs/feasibility/validated-environment.json").stat().st_mode
    assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0


def test_all_command_rejects_string_subclasses_without_writing(tmp_path: Path) -> None:
    class Text(str):
        pass

    output = tmp_path / "output.json"
    argv = [Text("all"), "--ack-personal-local-use-policy", "--output", str(output)]
    assert probe_cli.main(argv) != 0
    assert not output.exists()


def test_manifest_load_rejects_json_trailing_data(tmp_path: Path) -> None:
    path = tmp_path / "trailing.json"
    path.write_text("{}{}", encoding="utf-8")
    with pytest.raises(ManifestError, match="JSON"):
        load_manifest(path)


def test_core_gate_false_list_is_sorted(tmp_path: Path) -> None:
    document = _manifest_document()
    gates = document["core_gates"]
    assert isinstance(gates, dict)
    gates["streaming_event_contract"] = False
    gates["auth_source_lifetime"] = False
    manifest = _load_document(tmp_path, document)
    with pytest.raises(ManifestError) as error:
        require_core_gates(manifest)
    assert str(error.value).endswith("auth_source_lifetime, streaming_event_contract")


def test_resolver_validate_accepts_its_exact_source_manifest(tmp_path: Path) -> None:
    manifest = _load_document(tmp_path, _manifest_document())
    resolver = Phase0PrerequisiteDigestResolver.from_manifest(manifest)
    resolver.validate(manifest)
    assert resolver.resolve("sonnet", _MODEL) == phase0_prerequisite_digest(
        manifest, "sonnet", _MODEL
    )


def test_atomic_writer_parent_directory_is_fsynced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[int] = []
    real = validated._fsync_directory_fd

    def observe(descriptor: int) -> None:
        observed.append(descriptor)
        real(descriptor)

    monkeypatch.setattr(validated, "_fsync_directory_fd", observe)
    validated._atomic_write_manifest(tmp_path / "output.json", {"a": 1})
    assert len(observed) == 1


def test_atomic_writer_uses_fullfsync_before_directory_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    real_full = validated._fullfsync_fd
    real_dir = validated._fsync_directory_fd

    def full(descriptor: int) -> None:
        order.append("fullfsync")
        real_full(descriptor)

    def directory(descriptor: int) -> None:
        order.append("directory_fsync")
        real_dir(descriptor)

    monkeypatch.setattr(validated, "_fullfsync_fd", full)
    monkeypatch.setattr(validated, "_fsync_directory_fd", directory)
    validated._atomic_write_manifest(tmp_path / "output.json", {"a": 1})
    assert order == ["fullfsync", "directory_fsync"]


def test_no_manifest_api_exports_a_second_sdk_record_digest() -> None:
    exported = {
        name
        for name in dir(validated)
        if "sdk" in name.lower()
        and "digest" in name.lower()
        and not name.startswith("_")
    }
    assert exported == {"sdk_tool_record_digest"}


def test_no_manifest_api_exports_a_second_canonical_encoder() -> None:
    exported = {
        name
        for name in dir(validated)
        if "canonical" in name.lower() and not name.startswith("_")
    }
    assert exported == {"canonical_evidence_json"}


def test_cli_all_without_ack_is_never_live_policy_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUN_LIVE_CLAUDE_TESTS", "1")
    output = tmp_path / "output.json"
    assert probe_cli.main(["all", "--output", str(output)]) != 0
    assert not output.exists()


def test_manifest_model_map_is_sorted_in_canonical_output(tmp_path: Path) -> None:
    document = _manifest_document(models={"sonnet": _MODEL, "opus": _OTHER_MODEL})
    manifest = _load_document(tmp_path, document)
    encoded = canonical_evidence_json(manifest.to_json())
    assert encoded.index(b'"opus"') < encoded.index(b'"sonnet"')


def test_phase0_digest_is_lowercase_sha256(tmp_path: Path) -> None:
    manifest = _load_document(tmp_path, _manifest_document())
    digest = phase0_prerequisite_digest(manifest, "sonnet", _MODEL)
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_sdk_record_digest_is_lowercase_sha256(tmp_path: Path) -> None:
    record = load_sdk_tool_evidence(
        _load_document(tmp_path, _manifest_document())
    ).only_record
    digest = sdk_tool_record_digest(record)
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_manifest_source_arrays_reject_mapping_subclasses_via_direct_constructor() -> (
    None
):
    class ListSubclass(list[object]):
        pass

    with pytest.raises(ManifestError, match="canonical evidence"):
        canonical_evidence_json(ListSubclass([1]))


def test_manifest_schema_version_is_exact_integer_one(tmp_path: Path) -> None:
    for value in (True, 1.0, 2):
        document = _manifest_document()
        document["schema_version"] = value
        with pytest.raises(ManifestError):
            _load_document(tmp_path, document)


def test_policy_negative_verdict_cannot_have_true_policy_gate(tmp_path: Path) -> None:
    document = _manifest_document()
    policy = document["policy_evidence"]
    assert isinstance(policy, dict)
    policy["personal_local_use_allowed"] = False
    with pytest.raises(ManifestError, match="policy gate"):
        _load_document(tmp_path, document)


def test_auth_false_verdict_cannot_have_true_attestation_gates(tmp_path: Path) -> None:
    document = _manifest_document()
    auth = document["auth_evidence"]
    assert isinstance(auth, dict)
    auth["core_gate_available"] = False
    auth["accepted_public_shape"] = None
    auth["revalidation"] = "unavailable"
    auth["reason_codes"] = [
        "public_auth_provenance_absent_or_unvalidated",
        "preinput_network_boundary_unproved",
        "per_turn_fresh_provenance_unavailable",
    ]
    with pytest.raises(ManifestError, match="attestation gate"):
        _load_document(tmp_path, document)


def test_path_evidence_cannot_disagree_with_core_gate(tmp_path: Path) -> None:
    document = _manifest_document()
    path = document["path_policy_evidence"]
    assert isinstance(path, dict)
    path["path_safe_persistence"] = False
    with pytest.raises(ManifestError, match="path policy gate"):
        _load_document(tmp_path, document)


def test_optional_tool_gate_uses_no_public_dialect_admission(
    tmp_path: Path,
) -> None:
    manifest = _load_document(
        tmp_path,
        _manifest_document(tool_public_mappings_passed=False),
    )
    assert sdk_tool_gate_passed(manifest, _MODEL)


def test_committed_optional_tool_gate_is_false() -> None:
    manifest = load_manifest(Path("docs/feasibility/validated-environment.json"))
    for model in manifest.model_map.values():
        assert sdk_tool_gate_passed(manifest, model) is False


def test_committed_manifest_records_installed_cli_mismatch() -> None:
    manifest = load_manifest(Path("docs/feasibility/validated-environment.json"))
    assert manifest.cli_version == "2.1.251"
    assert manifest.observed_cli_version == "2.1.252"
    assert manifest.core_gates["exact_runtime_tuple"] is False


def test_committed_manifest_records_current_policy_digests() -> None:
    manifest = load_manifest(Path("docs/feasibility/validated-environment.json"))
    assert {source["sha256"] for source in manifest.policy_sources} == {
        "2830a3e2b3623aa731e55bede28cf7ba652c524195b3082fce3f56f4aabc75e5",
        "19ee9ebf0bbed7f2b6ec9562e730303269f97c232b3db04506a5c6f7c6d379ca",
    }


def test_manifest_evidence_never_contains_os_environment_values() -> None:
    manifest = load_manifest(Path("docs/feasibility/validated-environment.json"))
    encoded = canonical_evidence_json(manifest.to_json())
    sensitive_terms = ("TOKEN", "KEY", "SECRET", "PASSWORD", "COOKIE")
    for name, value in os.environ.items():
        if (
            any(term in name.upper() for term in sensitive_terms)
            and value
            and len(value.encode("utf-8", errors="ignore")) >= 16
        ):
            assert value.encode("utf-8", errors="ignore") not in encoded
