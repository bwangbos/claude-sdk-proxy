"""Immutable SDK-level tool evidence must fail closed before live construction."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import FrozenInstanceError, dataclass, fields

import pytest

import claude_sdk_proxy.probes as probes
from claude_sdk_proxy.attestation import current_attestation_availability
from claude_sdk_proxy.platform import MountIdentity
from claude_sdk_proxy.probes import ProbeUnavailable, run_tool_bridge_probe
from claude_sdk_proxy.validated import (
    REQUIRED_SDK_TOOL_GATES,
    SdkMcpNamingRule,
    SdkToolEvidenceError,
    SdkToolEvidenceKey,
    SdkToolEvidenceManifest,
    SdkToolEvidenceRecord,
    ToolNameError,
)

_BOUNDARY_NAME = "x" * 64
_REPRESENTATIVE_NAMES = ("echo", "snake_case", "dash-name", _BOUNDARY_NAME)


def _naming_rule_json() -> dict[str, object]:
    return {
        "version": 1,
        "server_identity": "caller_tools_v1",
        "caller_name_pattern": "[A-Za-z0-9_-]{1,64}",
        "caller_name_max_bytes": 64,
        "generated_name_template_or_algorithm": (
            "mcp__{server_identity}__{caller_name}"
        ),
        "representative_observations": {
            name: f"mcp__caller_tools_v1__{name}" for name in _REPRESENTATIVE_NAMES
        },
    }


def _sdk_tool_key_json() -> dict[str, object]:
    return {
        "runtime_digest": "11" * 32,
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
        "backend_model_id": "claude-sonnet-4-5-20250929",
    }


def _sdk_tool_record_json() -> dict[str, object]:
    return {
        "schema_version": 1,
        "key": _sdk_tool_key_json(),
        "naming_rule": _naming_rule_json(),
        "gates": {gate: False for gate in REQUIRED_SDK_TOOL_GATES},
    }


@pytest.fixture
def validated_naming_rule() -> SdkMcpNamingRule:
    return SdkMcpNamingRule.from_json(_naming_rule_json())


@pytest.fixture
def valid_sdk_tool_record() -> SdkToolEvidenceRecord:
    return SdkToolEvidenceRecord.from_json(_sdk_tool_record_json())


def test_sdk_tool_evidence_schema_has_only_the_exact_sdk_fields(
    valid_sdk_tool_record: SdkToolEvidenceRecord,
) -> None:
    manifest = SdkToolEvidenceManifest.from_records((valid_sdk_tool_record,))

    assert {field.name for field in fields(SdkToolEvidenceKey)} == {
        "runtime_digest",
        "sdk_version",
        "cli_version",
        "cli_executable_device",
        "cli_executable_inode",
        "cli_executable_sha256",
        "darwin_version",
        "darwin_build",
        "boot_id",
        "mount_identity",
        "backend_class",
        "auth_class",
        "semantic_class",
        "backend_model_id",
    }
    assert {field.name for field in fields(SdkToolEvidenceRecord)} == {
        "schema_version",
        "key",
        "naming_rule",
        "gates",
    }
    assert set(manifest.only_record.gates) == REQUIRED_SDK_TOOL_GATES
    assert not hasattr(manifest.only_record.key, "dialect")
    assert not hasattr(manifest.only_record.key, "streaming")
    assert set(manifest.only_record.to_json()) == {
        "schema_version",
        "key",
        "naming_rule",
        "gates",
    }


def test_mount_identity_is_the_complete_immutable_task_3_object(
    valid_sdk_tool_record: SdkToolEvidenceRecord,
) -> None:
    mount = valid_sdk_tool_record.key.mount_identity

    assert type(mount) is MountIdentity
    assert {field.name for field in fields(mount)} == {
        "filesystem_type",
        "is_local",
        "mount_device",
        "mount_fsid",
        "mount_flags",
        "runtime_root_st_dev",
    }
    assert type(mount.mount_flags) is tuple


@pytest.mark.parametrize("caller_name", _REPRESENTATIVE_NAMES)
def test_versioned_sdk_naming_rule_derives_every_representative_name(
    validated_naming_rule: SdkMcpNamingRule, caller_name: str
) -> None:
    generated = validated_naming_rule.derive(caller_name)

    assert generated == validated_naming_rule.representative_observations[caller_name]


def test_representative_observations_are_evidence_not_an_allowlist(
    validated_naming_rule: SdkMcpNamingRule,
) -> None:
    assert "arbitrary_accepted-name" not in (
        validated_naming_rule.representative_observations
    )
    assert validated_naming_rule.derive("arbitrary_accepted-name") == (
        "mcp__caller_tools_v1__arbitrary_accepted-name"
    )


@pytest.mark.parametrize(
    "caller_name",
    ["", "naïve", "工具", "x" * 65, "space name", "slash/name", "nul\0name"],
)
def test_public_tool_name_subset_rejects_unicode_empty_and_over_bound(
    validated_naming_rule: SdkMcpNamingRule, caller_name: str
) -> None:
    with pytest.raises(ToolNameError):
        validated_naming_rule.derive(caller_name)


def test_public_tool_name_subset_requires_exact_builtin_text(
    validated_naming_rule: SdkMcpNamingRule,
) -> None:
    class Text(str):
        pass

    with pytest.raises(ToolNameError, match="exact text"):
        validated_naming_rule.derive(Text("echo"))


def test_name_derivation_rejects_duplicate_caller_and_generated_names(
    validated_naming_rule: SdkMcpNamingRule,
) -> None:
    with pytest.raises(ToolNameError, match="duplicate caller"):
        validated_naming_rule.derive_all(("echo", "echo"))
    with pytest.raises(ToolNameError, match="immutable tuple"):
        validated_naming_rule.derive_all(["echo"])  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class _Definition:
    name: str


def test_generated_names_are_read_from_the_fixed_public_sdk_configuration(
    validated_naming_rule: SdkMcpNamingRule,
) -> None:
    marker = object()
    observed = validated_naming_rule.observe_generated_names(
        {"type": "sdk", "name": "caller_tools_v1", "instance": marker},
        tuple(_Definition(name) for name in ("echo", "new-tool")),
    )

    assert dict(observed) == {
        "echo": "mcp__caller_tools_v1__echo",
        "new-tool": "mcp__caller_tools_v1__new-tool",
    }
    with pytest.raises(TypeError):
        observed["echo"] = "changed"  # type: ignore[index]


def test_generated_name_observation_rejects_wrong_server_or_duplicate_definitions(
    validated_naming_rule: SdkMcpNamingRule,
) -> None:
    with pytest.raises(ToolNameError, match="server identity"):
        validated_naming_rule.observe_generated_names(
            {"type": "sdk", "name": "other", "instance": object()},
            (_Definition("echo"),),
        )
    with pytest.raises(ToolNameError, match="duplicate caller"):
        validated_naming_rule.observe_generated_names(
            {"type": "sdk", "name": "caller_tools_v1", "instance": object()},
            (_Definition("echo"), _Definition("echo")),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", 2),
        ("server_identity", "other"),
        ("caller_name_pattern", ".*"),
        ("caller_name_max_bytes", 65),
        ("generated_name_template_or_algorithm", "unvalidated"),
    ],
)
def test_unknown_or_changed_naming_rule_is_rejected(
    field: str, value: object
) -> None:
    naming = _naming_rule_json()
    naming[field] = value

    with pytest.raises(SdkToolEvidenceError, match="naming rule"):
        SdkMcpNamingRule.from_json(naming)


def test_naming_observations_must_be_complete_injective_and_derived() -> None:
    for mutation in ("missing", "wrong", "duplicate"):
        naming = _naming_rule_json()
        observations = naming["representative_observations"]
        assert isinstance(observations, dict)
        if mutation == "missing":
            del observations["echo"]
        elif mutation == "wrong":
            observations["echo"] = "mcp__wrong__echo"
        else:
            observations["snake_case"] = observations["echo"]
        with pytest.raises(SdkToolEvidenceError, match="observation"):
            SdkMcpNamingRule.from_json(naming)


def test_naming_observations_and_gate_maps_are_copied_and_immutable() -> None:
    source = _sdk_tool_record_json()
    record = SdkToolEvidenceRecord.from_json(source)
    naming = source["naming_rule"]
    gates = source["gates"]
    assert isinstance(naming, dict)
    observations = naming["representative_observations"]
    assert isinstance(observations, dict)
    assert isinstance(gates, dict)

    observations["echo"] = "mutated"
    gates["shutdown"] = True

    assert record.naming_rule.derive("echo") == "mcp__caller_tools_v1__echo"
    assert record.gates["shutdown"] is False
    with pytest.raises(TypeError):
        record.gates["shutdown"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        record.naming_rule.representative_observations["echo"] = "changed"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        record.schema_version = 2  # type: ignore[misc]


@pytest.mark.parametrize("mutation", ["missing", "extra", "integer", "subclass"])
def test_gate_set_is_exact_and_every_value_is_a_literal_boolean(mutation: str) -> None:
    record = _sdk_tool_record_json()
    gates = record["gates"]
    assert isinstance(gates, dict)
    if mutation == "missing":
        del gates["shutdown"]
    elif mutation == "extra":
        gates["future_gate"] = False
    elif mutation == "integer":
        gates["shutdown"] = 0
    else:
        class BooleanLike(int):
            pass

        gates["shutdown"] = BooleanLike(1)

    with pytest.raises(SdkToolEvidenceError, match="gate"):
        SdkToolEvidenceRecord.from_json(record)


@pytest.mark.parametrize("level", ["record", "key", "mount", "naming"])
def test_unknown_and_missing_schema_fields_are_rejected(level: str) -> None:
    for mutation in ("missing", "extra"):
        record = _sdk_tool_record_json()
        target: dict[str, object]
        required: str
        if level == "record":
            target, required = record, "schema_version"
        elif level == "key":
            target = record["key"]  # type: ignore[assignment]
            required = "runtime_digest"
        elif level == "mount":
            key = record["key"]
            assert isinstance(key, dict)
            target = key["mount_identity"]  # type: ignore[assignment]
            required = "filesystem_type"
        else:
            target = record["naming_rule"]  # type: ignore[assignment]
            required = "version"
        assert isinstance(target, dict)
        if mutation == "missing":
            del target[required]
        else:
            target["future_field"] = None
        with pytest.raises(SdkToolEvidenceError, match="field"):
            SdkToolEvidenceRecord.from_json(record)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema_version",), True),
        (("key", "cli_executable_device"), True),
        (("key", "cli_executable_inode"), -1),
        (("key", "runtime_digest"), "f" * 63),
        (("key", "sdk_version"), "0.2.149"),
        (("key", "cli_version"), "2.1.252"),
        (("key", "boot_id"), "00"),
        (("key", "backend_class"), "platform_api_key"),
        (("key", "auth_class"), "api_key"),
        (("key", "semantic_class"), "raw_messages_api"),
        (("key", "backend_model_id"), "sonnet"),
        (("key", "mount_identity", "filesystem_type"), "hfs"),
        (("key", "mount_identity", "is_local"), False),
        (("key", "mount_identity", "mount_flags"), ["local", "journaled"]),
        (("key", "mount_identity", "runtime_root_st_dev"), True),
    ],
)
def test_runtime_process_model_and_mount_types_fail_closed(
    path: tuple[str, ...], value: object
) -> None:
    record = _sdk_tool_record_json()
    target = record
    for segment in path[:-1]:
        child = target[segment]
        assert isinstance(child, dict)
        target = child
    target[path[-1]] = value

    with pytest.raises(SdkToolEvidenceError):
        SdkToolEvidenceRecord.from_json(record)


def test_manifest_rejects_duplicate_exact_keys_but_isolates_every_key_dimension(
    valid_sdk_tool_record: SdkToolEvidenceRecord,
) -> None:
    duplicate_json = valid_sdk_tool_record.to_json()
    duplicate_gates = duplicate_json["gates"]
    assert isinstance(duplicate_gates, dict)
    duplicate_gates["shutdown"] = True
    duplicate = SdkToolEvidenceRecord.from_json(duplicate_json)
    with pytest.raises(SdkToolEvidenceError, match="duplicate exact SDK tool tuple"):
        SdkToolEvidenceManifest.from_records((valid_sdk_tool_record, duplicate))

    isolated: list[SdkToolEvidenceRecord] = [valid_sdk_tool_record]
    for field, value in (
        ("runtime_digest", "44" * 32),
        ("cli_executable_inode", 12),
        ("backend_model_id", "claude-opus-4-1-20250805"),
    ):
        changed = valid_sdk_tool_record.to_json()
        key = changed["key"]
        assert isinstance(key, dict)
        key[field] = value
        isolated.append(SdkToolEvidenceRecord.from_json(changed))
    assert len(SdkToolEvidenceManifest.from_records(tuple(isolated)).records) == 4


def test_manifest_and_direct_aggregate_construction_reject_mutable_aliases(
    valid_sdk_tool_record: SdkToolEvidenceRecord,
) -> None:
    with pytest.raises(SdkToolEvidenceError, match="immutable tuple"):
        SdkToolEvidenceManifest.from_records([valid_sdk_tool_record])  # type: ignore[arg-type]
    with pytest.raises(SdkToolEvidenceError, match="immutable tuple"):
        SdkToolEvidenceManifest(records=[valid_sdk_tool_record])  # type: ignore[arg-type]


def test_hostile_cycle_and_oversized_tree_fail_without_rendering_values() -> None:
    class HostileMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            raise AssertionError("must not read hostile mapping")

        def __iter__(self) -> Iterator[str]:
            raise AssertionError("must not iterate hostile mapping")

        def __len__(self) -> int:
            raise AssertionError("must not size hostile mapping")

    with pytest.raises(SdkToolEvidenceError, match="exact JSON object"):
        SdkToolEvidenceRecord.from_json(HostileMapping())

    cyclic = _sdk_tool_record_json()
    gates = cyclic["gates"]
    assert isinstance(gates, dict)
    gates["cycle"] = cyclic
    with pytest.raises(SdkToolEvidenceError, match="cycle"):
        SdkToolEvidenceRecord.from_json(cyclic)

    oversized = _sdk_tool_record_json()
    naming = oversized["naming_rule"]
    assert isinstance(naming, dict)
    observations = naming["representative_observations"]
    assert isinstance(observations, dict)
    observations["echo"] = "x" * 1025
    with pytest.raises(SdkToolEvidenceError, match="byte bound"):
        SdkToolEvidenceRecord.from_json(oversized)


@pytest.mark.anyio
async def test_tool_probe_is_unavailable_before_sdk_server_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def forbidden_sdk_construction(*_args: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise AssertionError("SDK construction must remain unreachable")

    monkeypatch.setattr(
        "claude_agent_sdk.create_sdk_mcp_server", forbidden_sdk_construction
    )
    assert current_attestation_availability().core_gate_available is False

    with pytest.raises(ProbeUnavailable, match="child_attestation_unavailable"):
        await run_tool_bridge_probe(
            "claude-sonnet-4-5-20250929", "generated_name_rule", 1
        )
    assert calls == 0


@pytest.mark.anyio
async def test_tool_probe_validates_bounded_invocation_before_live_gate() -> None:
    for model_id, scenario, delay_seconds in (
        ("sonnet", "generated_name_rule", 1),
        ("claude-sonnet-4-5-20250929", "unknown", 1),
        ("claude-sonnet-4-5-20250929", "generated_name_rule", 0),
        ("claude-sonnet-4-5-20250929", "generated_name_rule", True),
        ("claude-sonnet-4-5-20250929", "generated_name_rule", 601),
    ):
        with pytest.raises((TypeError, ValueError), match="model|scenario|delay"):
            await probes.run_tool_bridge_probe(model_id, scenario, delay_seconds)
