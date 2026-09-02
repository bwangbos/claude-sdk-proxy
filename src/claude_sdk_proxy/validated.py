"""Fail-closed validation of the pinned policy and local runtime inputs."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, cast

from claude_sdk_proxy.model_validation import (
    ExactBackendModelError,
    require_exact_backend_model,
)
from claude_sdk_proxy.platform import MountIdentity
from claude_sdk_proxy.usage_evidence import (
    EvidenceSchemaError,
    UsageDialect,
    UsageEvidenceSchema,
    UsageOperationClass,
    UsageTupleKey,
)

POLICY_URLS = (
    "https://code.claude.com/docs/en/agent-sdk/overview",
    "https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan",
)
EXPECTED_CLI_VERSION = "2.1.251"
EXPECTED_SDK_VERSION = "0.2.148"
_CLI_VERSION_OUTPUT = re.compile(
    r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)(?:\s+\(Claude Code\))?"
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_DARWIN_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,2}\Z")
_FSID = re.compile(r"[0-9a-f]{8}:[0-9a-f]{8}\Z")
_SDK_TOOL_MAX_JSON_DEPTH: Final = 16
_SDK_TOOL_MAX_JSON_NODES: Final = 100_000
_SDK_TOOL_MAX_STRING_BYTES: Final = 1024
_SDK_TOOL_MAX_RECORDS: Final = 4096
_SDK_TOOL_MAX_DEFINITIONS: Final = 128
_MAPPING_PROXY_TYPE: Final[type[object]] = type(MappingProxyType({}))
_CANONICAL_MOUNT_FLAGS = frozenset(
    {
        "async",
        "automounted",
        "cprotect",
        "defwrite",
        "dontbrowse",
        "dovolfs",
        "exported",
        "ignore_ownership",
        "journaled",
        "local",
        "multilabel",
        "noatime",
        "nodev",
        "noexec",
        "nosuid",
        "nouserxattr",
        "quarantine",
        "quota",
        "rdonly",
        "rootfs",
        "snapshot",
        "strictatime",
        "synchronous",
        "union",
    }
)

SDK_MCP_NAMING_RULE_VERSION: Final = 1
SDK_MCP_SERVER_IDENTITY: Final = "caller_tools_v1"
SDK_MCP_CALLER_NAME_PATTERN: Final = "[A-Za-z0-9_-]{1,64}"
SDK_MCP_CALLER_NAME_MAX_BYTES: Final = 64
SDK_MCP_GENERATED_NAME_ALGORITHM: Final = "mcp__{server_identity}__{caller_name}"
_SDK_MCP_CALLER_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z", re.ASCII)
_SDK_MCP_BOUNDARY_NAME: Final = "x" * SDK_MCP_CALLER_NAME_MAX_BYTES
REQUIRED_SDK_TOOL_REPRESENTATIVE_NAMES: Final = frozenset(
    {"echo", "snake_case", "dash-name", _SDK_MCP_BOUNDARY_NAME}
)
REQUIRED_SDK_TOOL_GATES: Final = frozenset(
    {
        "generated_name_deterministic",
        "generated_name_injective",
        "caller_name_rejection",
        "caller_name_bounds",
        "callback_round_trip",
        "distinct_public_id_propagation",
        "parallel_reverse_correlation",
        "single_receive_loop_suspension",
        "cancellation",
        "interrupt",
        "shutdown",
        "sdk_event_ordering",
        "sdk_result_usage_shape",
        "argument_delta_fidelity",
        "schema_immutability",
        "suspension_600_seconds",
    }
)
_SDK_TOOL_BACKEND_CLASS: Final = "agent_sdk_subscription"
_SDK_TOOL_AUTH_CLASS: Final = "existing_claude_login"
_SDK_TOOL_SEMANTIC_CLASS: Final = "prompt_isolated_agent_sdk"


class RuntimeMismatch(ValueError):
    """Raised when policy or runtime evidence differs from the supported tuple."""


class SdkToolEvidenceError(ValueError):
    """Raised when SDK-level tool evidence is ambiguous or noncanonical."""


class ToolNameError(SdkToolEvidenceError):
    """Raised before SDK construction for an unsupported public tool name."""


def _sdk_tool_error(message: str) -> SdkToolEvidenceError:
    return SdkToolEvidenceError(message)


def _validate_sdk_tool_json_tree(value: object) -> None:
    remaining = _SDK_TOOL_MAX_JSON_NODES
    active: set[int] = set()
    stack: list[tuple[object, int, bool]] = [(value, 0, False)]
    while stack:
        current, depth, leaving = stack.pop()
        if leaving:
            active.remove(id(current))
            continue
        remaining -= 1
        if remaining < 0:
            raise _sdk_tool_error("SDK tool evidence exceeds its JSON item bound")
        if depth > _SDK_TOOL_MAX_JSON_DEPTH:
            raise _sdk_tool_error("SDK tool evidence exceeds its JSON depth bound")
        if current is None or type(current) in {bool, int, float}:
            continue
        if type(current) is str:
            try:
                encoded = current.encode("utf-8")
            except UnicodeEncodeError as error:
                raise _sdk_tool_error(
                    "SDK tool evidence string must be valid Unicode"
                ) from error
            if len(encoded) > _SDK_TOOL_MAX_STRING_BYTES:
                raise _sdk_tool_error("SDK tool evidence string exceeds its byte bound")
            continue
        if type(current) not in {list, dict}:
            raise _sdk_tool_error(
                "SDK tool gate/evidence must use exact JSON built-in types"
            )
        identity = id(current)
        if identity in active:
            raise _sdk_tool_error("SDK tool evidence contains a cycle")
        active.add(identity)
        stack.append((current, depth, True))
        if type(current) is list:
            stack.extend((child, depth + 1, False) for child in current)
        else:
            for key, child in cast(dict[object, object], current).items():
                if type(key) is not str:
                    raise _sdk_tool_error(
                        "SDK tool evidence JSON object keys must be exact text"
                    )
                stack.append((key, depth + 1, False))
                stack.append((child, depth + 1, False))


def _sdk_object(
    value: object, *, required: frozenset[str], label: str
) -> dict[str, object]:
    if type(value) is not dict:
        raise _sdk_tool_error(f"{label} must be an exact JSON object")
    result = cast(dict[str, object], value)
    if set(result) != required:
        raise _sdk_tool_error(f"{label} has an invalid field set")
    return result


def _sdk_text(
    value: object, label: str, *, maximum: int = _SDK_TOOL_MAX_STRING_BYTES
) -> str:
    if type(value) is not str:
        raise _sdk_tool_error(f"{label} must be exact text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _sdk_tool_error(f"{label} must be valid Unicode") from error
    if not encoded or len(encoded) > maximum:
        raise _sdk_tool_error(f"{label} is outside its byte bound")
    return value


def _visible_ascii(value: object, label: str, *, maximum: int) -> str:
    text = _sdk_text(value, label, maximum=maximum)
    if not text.isascii() or any(
        character < "!" or character > "~" for character in text
    ):
        raise _sdk_tool_error(f"{label} must be visible ASCII")
    return text


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise _sdk_tool_error(f"{label} must be a lowercase SHA-256 digest")
    return value


def _nonnegative_integer(value: object, label: str) -> int:
    if type(value) is not int or value < 0 or value > 2**64 - 1:
        raise _sdk_tool_error(f"{label} must be a bounded nonnegative integer")
    return value


def _exact_backend_model(value: object) -> str:
    try:
        return require_exact_backend_model(value)
    except (TypeError, ExactBackendModelError) as error:
        raise _sdk_tool_error("backend model ID must be exact bounded text") from error


def _validate_mount_identity(value: object) -> MountIdentity:
    if type(value) is not MountIdentity:
        raise _sdk_tool_error("mount identity must be the exact Task 3 type")
    mount = value
    if mount.filesystem_type != "apfs" or type(mount.filesystem_type) is not str:
        raise _sdk_tool_error("mount identity filesystem must be APFS")
    if type(mount.is_local) is not bool or not mount.is_local:
        raise _sdk_tool_error("mount identity must be local")
    _visible_ascii(mount.mount_device, "mount device", maximum=256)
    if type(mount.mount_fsid) is not str or _FSID.fullmatch(mount.mount_fsid) is None:
        raise _sdk_tool_error("mount identity FSID is not canonical")
    if type(mount.mount_flags) is not tuple:
        raise _sdk_tool_error("mount flags must be an immutable tuple")
    if (
        any(type(flag) is not str for flag in mount.mount_flags)
        or mount.mount_flags != tuple(sorted(mount.mount_flags))
        or len(set(mount.mount_flags)) != len(mount.mount_flags)
        or not set(mount.mount_flags).issubset(_CANONICAL_MOUNT_FLAGS)
        or "local" not in mount.mount_flags
    ):
        raise _sdk_tool_error("mount flags are not canonical local flags")
    _nonnegative_integer(mount.runtime_root_st_dev, "runtime root device")
    return mount


def _mount_identity_from_json(value: object) -> MountIdentity:
    raw = _sdk_object(
        value,
        required=frozenset(
            {
                "filesystem_type",
                "is_local",
                "mount_device",
                "mount_fsid",
                "mount_flags",
                "runtime_root_st_dev",
            }
        ),
        label="mount identity",
    )
    flags = raw["mount_flags"]
    if type(flags) is not list:
        raise _sdk_tool_error("mount flags must be a JSON array")
    return _validate_mount_identity(
        MountIdentity(
            filesystem_type=cast(str, raw["filesystem_type"]),
            is_local=cast(bool, raw["is_local"]),
            mount_device=cast(str, raw["mount_device"]),
            mount_fsid=cast(str, raw["mount_fsid"]),
            mount_flags=tuple(cast(list[object], flags)),  # type: ignore[arg-type]
            runtime_root_st_dev=cast(int, raw["runtime_root_st_dev"]),
        )
    )


def _mount_identity_to_json(value: MountIdentity) -> dict[str, object]:
    return {
        "filesystem_type": value.filesystem_type,
        "is_local": value.is_local,
        "mount_device": value.mount_device,
        "mount_fsid": value.mount_fsid,
        "mount_flags": list(value.mount_flags),
        "runtime_root_st_dev": value.runtime_root_st_dev,
    }


@dataclass(frozen=True)
class RuntimeTuple:
    sdk_version: str
    cli_version: str
    darwin_major: int
    filesystem: str

    def require(self, actual: RuntimeTuple) -> None:
        """Require every component of a runtime attestation to match exactly."""
        if self != actual:
            raise RuntimeMismatch("runtime tuple does not match the validated tuple")


@dataclass(frozen=True, slots=True)
class SdkMcpNamingRule:
    """Versioned default SDK MCP-name derivation backed by exact observations."""

    version: int
    server_identity: str
    caller_name_pattern: str
    caller_name_max_bytes: int
    generated_name_template_or_algorithm: str
    representative_observations: Mapping[str, str]

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != SDK_MCP_NAMING_RULE_VERSION:
            raise _sdk_tool_error("unsupported SDK MCP naming rule version")
        if (
            type(self.server_identity) is not str
            or self.server_identity != SDK_MCP_SERVER_IDENTITY
        ):
            raise _sdk_tool_error("SDK MCP naming rule server identity changed")
        if (
            type(self.caller_name_pattern) is not str
            or self.caller_name_pattern != SDK_MCP_CALLER_NAME_PATTERN
        ):
            raise _sdk_tool_error("SDK MCP naming rule caller-name pattern changed")
        if (
            type(self.caller_name_max_bytes) is not int
            or self.caller_name_max_bytes != SDK_MCP_CALLER_NAME_MAX_BYTES
        ):
            raise _sdk_tool_error("SDK MCP naming rule caller-name bound changed")
        if (
            type(self.generated_name_template_or_algorithm) is not str
            or self.generated_name_template_or_algorithm
            != SDK_MCP_GENERATED_NAME_ALGORITHM
        ):
            raise _sdk_tool_error("SDK MCP naming rule algorithm changed")
        if type(self.representative_observations) not in {
            dict,
            _MAPPING_PROXY_TYPE,
        }:
            raise _sdk_tool_error(
                "SDK MCP naming observations must be an exact immutable mapping"
            )
        observations = dict(self.representative_observations)
        if set(observations) != REQUIRED_SDK_TOOL_REPRESENTATIVE_NAMES:
            raise _sdk_tool_error(
                "SDK MCP naming observations are not the complete representative set"
            )
        ordered: dict[str, str] = {}
        generated_names: set[str] = set()
        for caller_name in sorted(observations):
            observed = observations[caller_name]
            if type(observed) is not str:
                raise _sdk_tool_error("SDK MCP naming observation must be exact text")
            expected = self.derive(caller_name)
            if observed != expected:
                raise _sdk_tool_error(
                    "SDK MCP naming observation is inconsistent with derivation"
                )
            if observed in generated_names:
                raise _sdk_tool_error("SDK MCP naming observations must be injective")
            generated_names.add(observed)
            ordered[caller_name] = observed
        object.__setattr__(
            self, "representative_observations", MappingProxyType(ordered)
        )

    def derive(self, caller_name: str) -> str:
        """Validate one public name and return its exact default SDK MCP name."""
        if type(caller_name) is not str:
            raise ToolNameError("caller tool name must be exact text")
        try:
            encoded = caller_name.encode("ascii")
        except (UnicodeEncodeError, UnicodeDecodeError) as error:
            raise ToolNameError("caller tool name must be ASCII") from error
        if (
            not encoded
            or len(encoded) > self.caller_name_max_bytes
            or _SDK_MCP_CALLER_NAME.fullmatch(caller_name) is None
        ):
            raise ToolNameError("caller tool name is outside the V1 public subset")
        return f"mcp__{self.server_identity}__{caller_name}"

    def derive_all(self, caller_names: tuple[str, ...]) -> Mapping[str, str]:
        """Derive one immutable injective mapping before SDK construction."""
        if type(caller_names) is not tuple:
            raise ToolNameError("caller tool definitions must be an immutable tuple")
        if not caller_names or len(caller_names) > _SDK_TOOL_MAX_DEFINITIONS:
            raise ToolNameError("caller tool definition count is outside its bound")
        output: dict[str, str] = {}
        generated: set[str] = set()
        for caller_name in caller_names:
            if caller_name in output:
                raise ToolNameError("duplicate caller tool name")
            derived = self.derive(caller_name)
            if derived in generated:
                raise ToolNameError("duplicate generated SDK tool name")
            output[caller_name] = derived
            generated.add(derived)
        return MappingProxyType(output)

    def observe_generated_names(
        self, server: Mapping[str, object], definitions: tuple[object, ...]
    ) -> Mapping[str, str]:
        """Read the documented SDK server/definition surface and derive names."""
        if type(server) is not dict or set(server) != {"type", "name", "instance"}:
            raise ToolNameError("SDK server must expose the exact public configuration")
        if server["type"] != "sdk" or type(server["type"]) is not str:
            raise ToolNameError("SDK server public configuration type changed")
        if type(server["name"]) is not str or server["name"] != self.server_identity:
            raise ToolNameError("SDK server identity does not match naming evidence")
        if type(definitions) is not tuple:
            raise ToolNameError("SDK tool definitions must be an immutable tuple")
        names: list[str] = []
        for definition in definitions:
            try:
                name = definition.name  # type: ignore[attr-defined]
            except AttributeError as error:
                raise ToolNameError(
                    "SDK tool definition lacks its public caller name"
                ) from error
            if type(name) is not str:
                raise ToolNameError("SDK tool definition name must be exact text")
            names.append(name)
        return self.derive_all(tuple(names))

    @classmethod
    def from_json(cls, value: object) -> SdkMcpNamingRule:
        _validate_sdk_tool_json_tree(value)
        raw = _sdk_object(
            value,
            required=frozenset(
                {
                    "version",
                    "server_identity",
                    "caller_name_pattern",
                    "caller_name_max_bytes",
                    "generated_name_template_or_algorithm",
                    "representative_observations",
                }
            ),
            label="SDK MCP naming rule",
        )
        observations = raw["representative_observations"]
        if type(observations) is not dict:
            raise _sdk_tool_error(
                "SDK MCP naming observations must be an exact JSON object"
            )
        return cls(
            version=cast(int, raw["version"]),
            server_identity=cast(str, raw["server_identity"]),
            caller_name_pattern=cast(str, raw["caller_name_pattern"]),
            caller_name_max_bytes=cast(int, raw["caller_name_max_bytes"]),
            generated_name_template_or_algorithm=cast(
                str, raw["generated_name_template_or_algorithm"]
            ),
            representative_observations=cast(dict[str, str], observations),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "version": self.version,
            "server_identity": self.server_identity,
            "caller_name_pattern": self.caller_name_pattern,
            "caller_name_max_bytes": self.caller_name_max_bytes,
            "generated_name_template_or_algorithm": (
                self.generated_name_template_or_algorithm
            ),
            "representative_observations": dict(self.representative_observations),
        }


@dataclass(frozen=True, slots=True)
class SdkToolEvidenceKey:
    """Exact SDK/runtime/process/model identity for one tool evidence row."""

    runtime_digest: str
    sdk_version: str
    cli_version: str
    cli_executable_device: int
    cli_executable_inode: int
    cli_executable_sha256: str
    darwin_version: str
    darwin_build: str
    boot_id: str
    mount_identity: MountIdentity
    backend_class: str
    auth_class: str
    semantic_class: str
    backend_model_id: str

    def __post_init__(self) -> None:
        _sha256(self.runtime_digest, "runtime digest")
        if (
            type(self.sdk_version) is not str
            or self.sdk_version != EXPECTED_SDK_VERSION
        ):
            raise _sdk_tool_error("SDK version is not the pinned Task 9 version")
        if (
            type(self.cli_version) is not str
            or self.cli_version != EXPECTED_CLI_VERSION
        ):
            raise _sdk_tool_error("CLI version is not the pinned Task 9 version")
        _nonnegative_integer(self.cli_executable_device, "CLI executable device")
        _nonnegative_integer(self.cli_executable_inode, "CLI executable inode")
        _sha256(self.cli_executable_sha256, "CLI executable digest")
        if (
            type(self.darwin_version) is not str
            or _DARWIN_VERSION.fullmatch(self.darwin_version) is None
        ):
            raise _sdk_tool_error("Darwin version is not canonical")
        _visible_ascii(self.darwin_build, "Darwin build", maximum=64)
        _sha256(self.boot_id, "boot identity")
        _validate_mount_identity(self.mount_identity)
        for actual, expected, label in (
            (self.backend_class, _SDK_TOOL_BACKEND_CLASS, "backend class"),
            (self.auth_class, _SDK_TOOL_AUTH_CLASS, "auth class"),
            (self.semantic_class, _SDK_TOOL_SEMANTIC_CLASS, "semantic class"),
        ):
            if type(actual) is not str or actual != expected:
                raise _sdk_tool_error(f"SDK tool {label} changed")
        _exact_backend_model(self.backend_model_id)

    @classmethod
    def from_json(cls, value: object) -> SdkToolEvidenceKey:
        raw = _sdk_object(
            value,
            required=frozenset(
                {
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
            ),
            label="SDK tool evidence key",
        )
        return cls(
            runtime_digest=cast(str, raw["runtime_digest"]),
            sdk_version=cast(str, raw["sdk_version"]),
            cli_version=cast(str, raw["cli_version"]),
            cli_executable_device=cast(int, raw["cli_executable_device"]),
            cli_executable_inode=cast(int, raw["cli_executable_inode"]),
            cli_executable_sha256=cast(str, raw["cli_executable_sha256"]),
            darwin_version=cast(str, raw["darwin_version"]),
            darwin_build=cast(str, raw["darwin_build"]),
            boot_id=cast(str, raw["boot_id"]),
            mount_identity=_mount_identity_from_json(raw["mount_identity"]),
            backend_class=cast(str, raw["backend_class"]),
            auth_class=cast(str, raw["auth_class"]),
            semantic_class=cast(str, raw["semantic_class"]),
            backend_model_id=cast(str, raw["backend_model_id"]),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "runtime_digest": self.runtime_digest,
            "sdk_version": self.sdk_version,
            "cli_version": self.cli_version,
            "cli_executable_device": self.cli_executable_device,
            "cli_executable_inode": self.cli_executable_inode,
            "cli_executable_sha256": self.cli_executable_sha256,
            "darwin_version": self.darwin_version,
            "darwin_build": self.darwin_build,
            "boot_id": self.boot_id,
            "mount_identity": _mount_identity_to_json(self.mount_identity),
            "backend_class": self.backend_class,
            "auth_class": self.auth_class,
            "semantic_class": self.semantic_class,
            "backend_model_id": self.backend_model_id,
        }


def _immutable_gates(value: object) -> Mapping[str, bool]:
    if type(value) not in {dict, _MAPPING_PROXY_TYPE}:
        raise _sdk_tool_error("SDK tool gates must be an exact immutable mapping")
    gates = dict(cast(Mapping[str, object], value))
    if set(gates) != REQUIRED_SDK_TOOL_GATES:
        raise _sdk_tool_error("SDK tool gate field set is incomplete or unknown")
    if any(type(verdict) is not bool for verdict in gates.values()):
        raise _sdk_tool_error("every SDK tool gate must be a literal boolean")
    return MappingProxyType({gate: cast(bool, gates[gate]) for gate in sorted(gates)})


@dataclass(frozen=True, slots=True, weakref_slot=True)
class SdkToolEvidenceRecord:
    """One exact immutable SDK-tool capability record with boolean gates."""

    schema_version: Literal[1]
    key: SdkToolEvidenceKey
    naming_rule: SdkMcpNamingRule
    gates: Mapping[str, bool]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise _sdk_tool_error("SDK tool record schema_version must be integer 1")
        if type(self.key) is not SdkToolEvidenceKey:
            raise _sdk_tool_error("SDK tool record key type is invalid")
        if type(self.naming_rule) is not SdkMcpNamingRule:
            raise _sdk_tool_error("SDK tool record naming rule type is invalid")
        object.__setattr__(self, "gates", _immutable_gates(self.gates))

    @classmethod
    def from_json(cls, value: object) -> SdkToolEvidenceRecord:
        if type(value) is not dict:
            raise _sdk_tool_error("SDK tool record must be an exact JSON object")
        _validate_sdk_tool_json_tree(value)
        raw = _sdk_object(
            value,
            required=frozenset({"schema_version", "key", "naming_rule", "gates"}),
            label="SDK tool evidence record",
        )
        return cls(
            schema_version=cast(Literal[1], raw["schema_version"]),
            key=SdkToolEvidenceKey.from_json(raw["key"]),
            naming_rule=SdkMcpNamingRule.from_json(raw["naming_rule"]),
            gates=_immutable_gates(raw["gates"]),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "key": self.key.to_json(),
            "naming_rule": self.naming_rule.to_json(),
            "gates": dict(self.gates),
        }


def _sdk_tool_key_sort_value(key: SdkToolEvidenceKey) -> tuple[object, ...]:
    mount = key.mount_identity
    return (
        key.runtime_digest,
        key.sdk_version,
        key.cli_version,
        key.cli_executable_device,
        key.cli_executable_inode,
        key.cli_executable_sha256,
        key.darwin_version,
        key.darwin_build,
        key.boot_id,
        mount.filesystem_type,
        mount.is_local,
        mount.mount_device,
        mount.mount_fsid,
        mount.mount_flags,
        mount.runtime_root_st_dev,
        key.backend_class,
        key.auth_class,
        key.semantic_class,
        key.backend_model_id,
    )


@dataclass(frozen=True, slots=True)
class SdkToolEvidenceManifest:
    """Canonical bounded collection of exact SDK-tool evidence records."""

    records: tuple[SdkToolEvidenceRecord, ...]

    def __post_init__(self) -> None:
        if type(self.records) is not tuple:
            raise _sdk_tool_error("SDK tool records must be an immutable tuple")
        if len(self.records) > _SDK_TOOL_MAX_RECORDS:
            raise _sdk_tool_error("SDK tool record count exceeds its bound")
        if any(type(record) is not SdkToolEvidenceRecord for record in self.records):
            raise _sdk_tool_error("SDK tool manifest contains an invalid record type")
        keys = tuple(record.key for record in self.records)
        if len(set(keys)) != len(keys):
            raise _sdk_tool_error(
                "SDK tool manifest contains a duplicate exact SDK tool tuple"
            )
        object.__setattr__(
            self,
            "records",
            tuple(
                sorted(
                    self.records,
                    key=lambda record: _sdk_tool_key_sort_value(record.key),
                )
            ),
        )

    @classmethod
    def from_records(
        cls, records: tuple[SdkToolEvidenceRecord, ...]
    ) -> SdkToolEvidenceManifest:
        if type(records) is not tuple:
            raise _sdk_tool_error("SDK tool records must be an immutable tuple")
        return cls(records=records)

    @property
    def only_record(self) -> SdkToolEvidenceRecord:
        if len(self.records) != 1:
            raise _sdk_tool_error(
                "SDK tool manifest does not contain exactly one record"
            )
        return self.records[0]


@dataclass(frozen=True)
class PolicyEvidence:
    checked_at: datetime
    source_urls: tuple[str, ...]
    personal_local_use_allowed: bool

    def require_allowed(self) -> None:
        """Require both primary policy pages and an affirmative local-use verdict."""
        if (
            len(self.source_urls) != len(POLICY_URLS)
            or set(self.source_urls) != set(POLICY_URLS)
            or not self.personal_local_use_allowed
        ):
            raise RuntimeMismatch("policy evidence is incomplete or not affirmative")


@dataclass(frozen=True)
class CliIdentity:
    path: Path
    st_dev: int
    st_ino: int
    mode: int
    sha256: str
    version: str


def _require_unchanged_cli_identity(path: Path, expected: os.stat_result) -> None:
    try:
        current = path.stat()
    except OSError as error:
        raise RuntimeMismatch("CLI identity changed during inspection") from error
    if (
        current.st_dev,
        current.st_ino,
        current.st_mode,
    ) != (
        expected.st_dev,
        expected.st_ino,
        expected.st_mode,
    ):
        raise RuntimeMismatch("CLI identity changed during inspection")


def read_cli_identity(path: Path) -> CliIdentity:
    """Read and validate identity evidence for the exact pinned CLI executable."""
    try:
        resolved_path = path.resolve(strict=True)
        metadata = resolved_path.stat()
    except OSError as error:
        raise RuntimeMismatch("CLI path cannot be resolved") from error

    is_regular_executable = stat.S_ISREG(metadata.st_mode) and bool(
        metadata.st_mode & stat.S_IXUSR
    )
    if not is_regular_executable:
        raise RuntimeMismatch("CLI path is not a regular executable")

    completed = subprocess.run(
        [str(resolved_path), "--version"],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeMismatch("CLI --version did not succeed")
    _require_unchanged_cli_identity(resolved_path, metadata)

    try:
        output = completed.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise RuntimeMismatch("CLI version output is not UTF-8") from error
    matched = _CLI_VERSION_OUTPUT.fullmatch(output)
    if matched is None or matched["version"] != EXPECTED_CLI_VERSION:
        raise RuntimeMismatch("CLI version does not match the validated version")

    digest = hashlib.sha256(resolved_path.read_bytes()).hexdigest()
    _require_unchanged_cli_identity(resolved_path, metadata)
    return CliIdentity(
        path=resolved_path,
        st_dev=metadata.st_dev,
        st_ino=metadata.st_ino,
        mode=metadata.st_mode,
        sha256=digest,
        version=matched["version"],
    )


def validate_policy(evidence: PolicyEvidence) -> None:
    """Raise when the policy evidence cannot authorize personal local use."""
    evidence.require_allowed()


class ManifestError(ValueError):
    """Raised when Phase 0 evidence is malformed, stale, or unavailable."""


CORE_GATE_NAMES: Final = frozenset(
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
CURRENT_POLICY_PERSONAL_LOCAL_USE_ALLOWED: Final = False
_EXPECTED_ALLOWED_ENVIRONMENT_NAMES: Final = (
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
)
_EXPECTED_NETWORK_PROXY_NAMES: Final = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)
_EXPECTED_FIXED_ISOLATION_NAMES: Final = (
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
)
_MANIFEST_MAX_BYTES: Final = 1_048_576
_CANONICAL_MAX_DEPTH: Final = 32
_CANONICAL_MAX_ITEMS: Final = 100_000
_CANONICAL_MAX_STRING_BYTES: Final = 1_048_576
_UTC_SECONDS = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z\Z"
)
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
_PUBLIC_ALIAS = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z", re.ASCII)
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,255}\Z", re.ASCII)
_TOP_LEVEL_FIELDS: Final = frozenset(
    {
        "schema_version",
        "validated_at",
        "core_gates",
        "policy_evidence",
        "runtime_evidence",
        "mount_identity",
        "sync_lock_lifecycle_evidence",
        "environment_evidence",
        "auth_evidence",
        "path_policy_evidence",
        "backend_class",
        "auth_class",
        "semantic_class",
        "model_map",
        "thinking_tuples",
        "usage_evidence",
        "sdk_tool_evidence",
    }
)
_FORBIDDEN_CONTENT_FIELDS: Final = frozenset(
    {
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
)
_SYSCALL_CASES: Final = frozenset(
    {
        "preallocate",
        "fullfsync_file",
        "renameat",
        "unlinkat",
        "fsync_directory",
        "proc_pidinfo",
    }
)
_LOCK_CASES: Final = frozenset({"root_reconciliation_lock", "instance_lifetime_lock"})
_OWNER_RECORD_CASES: Final = frozenset({"owner_record_create", "owner_record_replace"})
_JOURNAL_CASES: Final = frozenset(
    {"bounded_journal", "durable_head_certification", "cleanup_automaton"}
)
_SUPERVISOR_CASES: Final = frozenset(
    {"retaining_supervisor", "anchor_unconfirmed_fallback"}
)
_AUTH_REASON_CODES: Final = (
    "public_auth_provenance_absent_or_unvalidated",
    "preinput_network_boundary_unproved",
    "per_turn_fresh_provenance_unavailable",
)
_AUTH_SHAPE_FIELDS: Final = frozenset(
    {
        "auth_source",
        "cli_executable_sha256",
        "endpoint",
        "environment_fingerprint",
        "network_submission_count",
        "provider",
    }
)


def _manifest_error(message: str) -> ManifestError:
    return ManifestError(message)


def _validate_canonical_tree(value: object) -> None:
    remaining = _CANONICAL_MAX_ITEMS
    active: set[int] = set()
    stack: list[tuple[object, int, bool]] = [(value, 0, False)]
    while stack:
        current, depth, leaving = stack.pop()
        if leaving:
            active.remove(id(current))
            continue
        remaining -= 1
        if remaining < 0:
            raise _manifest_error("canonical evidence exceeds its item bound")
        if depth > _CANONICAL_MAX_DEPTH:
            raise _manifest_error("canonical evidence exceeds its depth bound")
        if current is None or type(current) in {bool, int}:
            continue
        if type(current) is str:
            try:
                encoded = current.encode("utf-8")
            except UnicodeEncodeError as error:
                raise _manifest_error(
                    "canonical evidence strings must be valid Unicode"
                ) from error
            if len(encoded) > _CANONICAL_MAX_STRING_BYTES:
                raise _manifest_error(
                    "canonical evidence string exceeds its byte bound"
                )
            continue
        if type(current) not in {dict, list, tuple, _MAPPING_PROXY_TYPE}:
            raise _manifest_error("canonical evidence uses a noncanonical JSON type")
        identity = id(current)
        if identity in active:
            raise _manifest_error("canonical evidence contains a cycle")
        active.add(identity)
        stack.append((current, depth, True))
        if type(current) in {list, tuple}:
            stack.extend(
                (child, depth + 1, False)
                for child in reversed(cast(list[object] | tuple[object, ...], current))
            )
            continue
        mapping = cast(Mapping[object, object], current)
        children: list[tuple[object, int, bool]] = []
        for key, child in mapping.items():
            if type(key) is not str:
                raise _manifest_error(
                    "canonical evidence object keys must be exact text"
                )
            children.append((child, depth + 1, False))
            children.append((key, depth + 1, False))
        stack.extend(reversed(children))


def _thaw_json(value: object) -> object:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) in {list, tuple}:
        return [
            _thaw_json(item) for item in cast(list[object] | tuple[object, ...], value)
        ]
    if type(value) in {dict, _MAPPING_PROXY_TYPE}:
        return {
            key: _thaw_json(child)
            for key, child in cast(Mapping[str, object], value).items()
        }
    raise _manifest_error("canonical evidence uses a noncanonical JSON type")


def canonical_evidence_json(value: object) -> bytes:
    """Encode one bounded exact JSON tree for every cross-phase digest."""
    _validate_canonical_tree(value)
    try:
        return json.dumps(
            _thaw_json(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise _manifest_error("canonical evidence cannot be encoded") from error


def _freeze_json(value: object) -> object:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is list:
        return tuple(_freeze_json(item) for item in cast(list[object], value))
    if type(value) is dict:
        return MappingProxyType(
            {
                key: _freeze_json(child)
                for key, child in cast(dict[str, object], value).items()
            }
        )
    raise _manifest_error("manifest evidence must use exact JSON built-in types")


def _object_fields(
    value: object, *, fields: frozenset[str], label: str
) -> dict[str, object]:
    if type(value) is not dict:
        raise _manifest_error(f"{label} must be an exact JSON object")
    result = cast(dict[str, object], value)
    if set(result) != fields:
        raise _manifest_error(f"{label} field set is invalid")
    return result


def _exact_manifest_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise _manifest_error(f"{label} must be a literal boolean")
    return value


def _manifest_text(
    value: object, label: str, *, maximum: int = 1024, visible: bool = False
) -> str:
    if type(value) is not str:
        raise _manifest_error(f"{label} must be exact text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _manifest_error(f"{label} must be valid Unicode") from error
    if not encoded or len(encoded) > maximum:
        raise _manifest_error(f"{label} is outside its byte bound")
    if visible and (
        not value.isascii()
        or any(character < "!" or character > "~" for character in value)
    ):
        raise _manifest_error(f"{label} must be visible ASCII")
    return value


def _manifest_integer(value: object, label: str) -> int:
    if type(value) is not int or value < 0 or value > 2**64 - 1:
        raise _manifest_error(f"{label} must be a nonnegative bounded integer")
    return value


def _manifest_digest(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise _manifest_error(f"{label} must be a lowercase SHA-256 digest")
    return value


def _utc_timestamp(value: object, label: str) -> str:
    if type(value) is not str or _UTC_SECONDS.fullmatch(value) is None:
        raise _manifest_error(f"{label} must be a canonical UTC timestamp")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise _manifest_error(f"{label} must be a canonical UTC timestamp") from error
    return value


def _array(value: object, label: str, *, maximum: int = 4096) -> list[object]:
    if type(value) is not list:
        raise _manifest_error(f"{label} must be a JSON array")
    result = cast(list[object], value)
    if len(result) > maximum:
        raise _manifest_error(f"{label} exceeds its item bound")
    return result


def _bool_matrix(
    value: object, *, names: frozenset[str], label: str
) -> Mapping[str, bool]:
    raw = _object_fields(value, fields=names, label=label)
    output = {
        name: _exact_manifest_bool(raw[name], f"{label} verdict")
        for name in sorted(names)
    }
    return MappingProxyType(output)


def _sorted_name_tuple(value: object, label: str) -> tuple[str, ...]:
    raw = _array(value, label, maximum=256)
    names: list[str] = []
    for item in raw:
        name = _manifest_text(item, label, maximum=256, visible=True)
        if _ENVIRONMENT_NAME.fullmatch(name) is None:
            raise _manifest_error(f"{label} contains an invalid environment name")
        names.append(name)
    if names != sorted(names) or len(names) != len(set(names)):
        raise _manifest_error(f"{label} must be sorted and unique")
    return tuple(names)


def _forbid_content_fields(value: object) -> None:
    stack = [value]
    while stack:
        current = stack.pop()
        if type(current) is dict:
            mapping = cast(dict[str, object], current)
            if _FORBIDDEN_CONTENT_FIELDS.intersection(mapping):
                raise _manifest_error("manifest contains a forbidden content field")
            stack.extend(mapping.values())
        elif type(current) is list:
            stack.extend(cast(list[object], current))


def _validate_model_map(value: object) -> Mapping[str, str]:
    if type(value) is not dict:
        raise _manifest_error("model map must be an exact JSON object")
    raw = cast(dict[object, object], value)
    if not raw or len(raw) > 32:
        raise _manifest_error("model map count is outside its bound")
    result: dict[str, str] = {}
    backend_ids: set[str] = set()
    for alias_value, backend_value in raw.items():
        if type(alias_value) is not str or _PUBLIC_ALIAS.fullmatch(alias_value) is None:
            raise _manifest_error("model map public alias is not canonical")
        try:
            backend = require_exact_backend_model(backend_value)
        except (TypeError, ExactBackendModelError) as error:
            raise _manifest_error(
                "model map requires an exact backend model ID"
            ) from error
        if backend in backend_ids:
            raise _manifest_error("model map has an ambiguous exact backend model ID")
        result[alias_value] = backend
        backend_ids.add(backend)
    return MappingProxyType({key: result[key] for key in sorted(result)})


@dataclass(frozen=True, slots=True, init=False)
class FeasibilityManifest:
    """Immutable, content-free Phase 0 evidence composed from Tasks 1-9."""

    schema_version: Literal[1]
    validated_at: str
    core_gates: Mapping[str, bool]
    policy_evidence: Mapping[str, object]
    runtime_evidence: Mapping[str, object]
    mount_identity: MountIdentity
    sync_lock_lifecycle_evidence: Mapping[str, object]
    environment_evidence: Mapping[str, object]
    auth_evidence: Mapping[str, object]
    path_policy_evidence: Mapping[str, object]
    backend_class: str
    auth_class: str
    semantic_class: str
    model_map: Mapping[str, str]
    thinking_tuples: tuple[Mapping[str, object], ...]
    _usage_evidence: Mapping[str, object]
    _sdk_tool_evidence: Mapping[str, object]

    @property
    def policy_sources(self) -> tuple[Mapping[str, object], ...]:
        return cast(tuple[Mapping[str, object], ...], self.policy_evidence["sources"])

    @property
    def runtime_digest(self) -> str:
        return cast(str, self.runtime_evidence["runtime_digest"])

    @property
    def sdk_version(self) -> str:
        return cast(str, self.runtime_evidence["sdk_version"])

    @property
    def cli_version(self) -> str:
        return cast(str, self.runtime_evidence["cli_version"])

    @property
    def observed_cli_version(self) -> str:
        return cast(str, self.runtime_evidence["observed_cli_version"])

    @property
    def cli_hash(self) -> str:
        return cast(str, self.runtime_evidence["cli_executable_sha256"])

    @property
    def os_build(self) -> str:
        return cast(str, self.runtime_evidence["darwin_build"])

    @property
    def auth_revalidation(self) -> str:
        return cast(str, self.auth_evidence["revalidation"])

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "validated_at": self.validated_at,
            "core_gates": dict(self.core_gates),
            "policy_evidence": cast(
                dict[str, object], _thaw_json(self.policy_evidence)
            ),
            "runtime_evidence": cast(
                dict[str, object], _thaw_json(self.runtime_evidence)
            ),
            "mount_identity": _mount_identity_to_json(self.mount_identity),
            "sync_lock_lifecycle_evidence": cast(
                dict[str, object], _thaw_json(self.sync_lock_lifecycle_evidence)
            ),
            "environment_evidence": cast(
                dict[str, object], _thaw_json(self.environment_evidence)
            ),
            "auth_evidence": cast(dict[str, object], _thaw_json(self.auth_evidence)),
            "path_policy_evidence": cast(
                dict[str, object], _thaw_json(self.path_policy_evidence)
            ),
            "backend_class": self.backend_class,
            "auth_class": self.auth_class,
            "semantic_class": self.semantic_class,
            "model_map": dict(self.model_map),
            "thinking_tuples": cast(list[object], _thaw_json(self.thinking_tuples)),
            "usage_evidence": cast(dict[str, object], _thaw_json(self._usage_evidence)),
            "sdk_tool_evidence": cast(
                dict[str, object], _thaw_json(self._sdk_tool_evidence)
            ),
        }


def _parse_policy(value: object, gates: Mapping[str, bool]) -> Mapping[str, object]:
    raw = _object_fields(
        value,
        fields=frozenset({"personal_local_use_allowed", "sources"}),
        label="policy evidence",
    )
    allowed = _exact_manifest_bool(
        raw["personal_local_use_allowed"], "personal local use policy"
    )
    source_items = _array(raw["sources"], "policy sources", maximum=2)
    sources: list[Mapping[str, object]] = []
    for item in source_items:
        source = _object_fields(
            item,
            fields=frozenset({"url", "retrieved_at", "sha256"}),
            label="policy source",
        )
        sources.append(
            MappingProxyType(
                {
                    "url": _manifest_text(
                        source["url"], "policy source URL", maximum=512
                    ),
                    "retrieved_at": _utc_timestamp(
                        source["retrieved_at"], "policy source retrieval"
                    ),
                    "sha256": _manifest_digest(
                        source["sha256"], "policy source digest"
                    ),
                }
            )
        )
    if {cast(str, source["url"]) for source in sources} != set(POLICY_URLS):
        raise _manifest_error("policy sources must be the exact primary URLs")
    if len(sources) != len(POLICY_URLS):
        raise _manifest_error("policy source set is incomplete")
    sources.sort(key=lambda source: cast(str, source["url"]))
    if gates["personal_subscription_policy"] and not allowed:
        raise _manifest_error("policy gate cannot exceed its evidence verdict")
    return MappingProxyType(
        {
            "personal_local_use_allowed": allowed,
            "sources": tuple(sources),
        }
    )


_RUNTIME_FIELDS: Final = frozenset(
    {
        "runtime_digest",
        "python_version",
        "python_executable",
        "python_executable_sha256",
        "sdk_version",
        "sdk_path",
        "sdk_sha256",
        "cli_version",
        "observed_cli_version",
        "cli_path",
        "cli_path_sha256",
        "cli_executable_device",
        "cli_executable_inode",
        "cli_executable_mode",
        "cli_executable_sha256",
        "darwin_version",
        "darwin_build",
        "boot_id",
    }
)


def _parse_runtime(value: object, gates: Mapping[str, bool]) -> Mapping[str, object]:
    raw = _object_fields(value, fields=_RUNTIME_FIELDS, label="runtime evidence")
    if any(
        raw[field] is None
        for field in (
            "cli_path",
            "cli_path_sha256",
            "cli_executable_device",
            "cli_executable_inode",
            "cli_executable_mode",
            "cli_executable_sha256",
        )
    ):
        raise _manifest_error("CLI identity is incomplete")
    text_fields = {
        "python_version": _manifest_text(
            raw["python_version"], "Python version", maximum=64, visible=True
        ),
        "python_executable": _manifest_text(
            raw["python_executable"], "Python executable", maximum=1024
        ),
        "sdk_version": _manifest_text(
            raw["sdk_version"], "SDK version", maximum=64, visible=True
        ),
        "sdk_path": _manifest_text(raw["sdk_path"], "SDK path", maximum=1024),
        "cli_version": _manifest_text(
            raw["cli_version"], "CLI version", maximum=64, visible=True
        ),
        "observed_cli_version": _manifest_text(
            raw["observed_cli_version"],
            "observed CLI version",
            maximum=64,
            visible=True,
        ),
        "cli_path": _manifest_text(raw["cli_path"], "CLI path", maximum=1024),
        "darwin_version": _manifest_text(
            raw["darwin_version"], "Darwin version", maximum=32, visible=True
        ),
        "darwin_build": _manifest_text(
            raw["darwin_build"], "Darwin build", maximum=64, visible=True
        ),
    }
    for field in (
        "python_version",
        "sdk_version",
        "cli_version",
        "observed_cli_version",
    ):
        if _VERSION.fullmatch(text_fields[field]) is None:
            raise _manifest_error(f"{field} is not a canonical version")
    if _DARWIN_VERSION.fullmatch(text_fields["darwin_version"]) is None:
        raise _manifest_error("Darwin version is not canonical")
    for field in ("python_executable", "sdk_path", "cli_path"):
        if not Path(text_fields[field]).is_absolute():
            raise _manifest_error(f"{field} must be absolute")
    integers = {
        "cli_executable_device": _manifest_integer(
            raw["cli_executable_device"], "CLI executable device"
        ),
        "cli_executable_inode": _manifest_integer(
            raw["cli_executable_inode"], "CLI executable inode"
        ),
        "cli_executable_mode": _manifest_integer(
            raw["cli_executable_mode"], "CLI executable mode"
        ),
    }
    if not stat.S_ISREG(integers["cli_executable_mode"]) or not (
        integers["cli_executable_mode"] & stat.S_IXUSR
    ):
        raise _manifest_error("CLI identity must name an executable regular file")
    digests = {
        field: _manifest_digest(raw[field], field)
        for field in (
            "runtime_digest",
            "python_executable_sha256",
            "sdk_sha256",
            "cli_path_sha256",
            "cli_executable_sha256",
            "boot_id",
        )
    }
    if gates["exact_runtime_tuple"] and (
        text_fields["sdk_version"] != EXPECTED_SDK_VERSION
        or text_fields["cli_version"] != EXPECTED_CLI_VERSION
        or text_fields["observed_cli_version"] != EXPECTED_CLI_VERSION
    ):
        raise _manifest_error("true exact runtime gate contradicts runtime evidence")
    return MappingProxyType({**text_fields, **integers, **digests})


def _parse_lifecycle(value: object, gates: Mapping[str, bool]) -> Mapping[str, object]:
    raw = _object_fields(
        value,
        fields=frozenset(
            {
                "syscall_matrix",
                "lock_matrix",
                "owner_record_matrix",
                "journal_matrix",
                "supervisor_matrix",
            }
        ),
        label="sync/lock/lifecycle evidence",
    )
    syscalls = _bool_matrix(
        raw["syscall_matrix"], names=_SYSCALL_CASES, label="syscall matrix"
    )
    locks = _bool_matrix(raw["lock_matrix"], names=_LOCK_CASES, label="lock matrix")
    owners = _bool_matrix(
        raw["owner_record_matrix"],
        names=_OWNER_RECORD_CASES,
        label="owner record matrix",
    )
    journal = _bool_matrix(
        raw["journal_matrix"], names=_JOURNAL_CASES, label="journal matrix"
    )
    supervisor = _bool_matrix(
        raw["supervisor_matrix"],
        names=_SUPERVISOR_CASES,
        label="supervisor matrix",
    )
    expected = {
        "required_sync_primitives": all(syscalls.values()),
        "bsd_flock_model": all(locks.values()),
        **dict(locks),
        **dict(owners),
        **dict(journal),
        **dict(supervisor),
    }
    if any(gates[name] and not verdict for name, verdict in expected.items()):
        raise _manifest_error("core gate evidence mismatch")
    return MappingProxyType(
        {
            "syscall_matrix": syscalls,
            "lock_matrix": locks,
            "owner_record_matrix": owners,
            "journal_matrix": journal,
            "supervisor_matrix": supervisor,
        }
    )


def _parse_environment(value: object) -> Mapping[str, object]:
    raw = _object_fields(
        value,
        fields=frozenset(
            {
                "allowed_names",
                "network_proxy_names",
                "fixed_isolation_names",
                "fingerprint_algorithm",
            }
        ),
        label="environment evidence",
    )
    algorithm = _manifest_text(
        raw["fingerprint_algorithm"],
        "environment fingerprint algorithm",
        maximum=128,
        visible=True,
    )
    if algorithm != "sha256_sorted_name_nul_value_nul_v1":
        raise _manifest_error("environment fingerprint algorithm is unsupported")
    allowed_names = _sorted_name_tuple(
        raw["allowed_names"], "allowed environment names"
    )
    network_names = _sorted_name_tuple(
        raw["network_proxy_names"], "network proxy names"
    )
    fixed_names = _sorted_name_tuple(
        raw["fixed_isolation_names"], "fixed isolation names"
    )
    if allowed_names != _EXPECTED_ALLOWED_ENVIRONMENT_NAMES:
        raise _manifest_error("allowed environment names are not the exact contract")
    if network_names != _EXPECTED_NETWORK_PROXY_NAMES:
        raise _manifest_error("network proxy names are not the exact contract")
    if fixed_names != _EXPECTED_FIXED_ISOLATION_NAMES:
        raise _manifest_error("fixed isolation names are not the exact contract")
    return MappingProxyType(
        {
            "allowed_names": allowed_names,
            "network_proxy_names": network_names,
            "fixed_isolation_names": fixed_names,
            "fingerprint_algorithm": algorithm,
        }
    )


def _parse_auth(value: object, gates: Mapping[str, bool]) -> Mapping[str, object]:
    raw = _object_fields(
        value,
        fields=frozenset(
            {
                "schema",
                "version",
                "accepted_public_shape",
                "provider",
                "endpoint",
                "auth_source",
                "revalidation",
                "core_gate_available",
                "reason_codes",
            }
        ),
        label="auth evidence",
    )
    if raw["version"] != 1 or type(raw["version"]) is not int:
        raise _manifest_error("auth evidence version must be exact integer 1")
    schema = _manifest_text(raw["schema"], "auth schema", maximum=128, visible=True)
    if schema != "claude_sdk_proxy.child_attestation_manifest":
        raise _manifest_error("auth evidence schema is unsupported")
    provider = _manifest_text(raw["provider"], "auth provider", visible=True)
    endpoint = _manifest_text(raw["endpoint"], "auth endpoint", visible=True)
    auth_source = _manifest_text(raw["auth_source"], "auth source", visible=True)
    if (provider, endpoint, auth_source) != (
        "anthropic",
        "default",
        "existing_claude_login",
    ):
        raise _manifest_error("auth evidence identity changed")
    revalidation = _manifest_text(
        raw["revalidation"], "auth revalidation", maximum=32, visible=True
    )
    if revalidation not in {"unavailable", "fixed_for_lifetime", "each_turn"}:
        raise _manifest_error("auth revalidation is unsupported")
    available = _exact_manifest_bool(raw["core_gate_available"], "auth availability")
    reasons = tuple(
        _manifest_text(item, "auth reason code", maximum=128, visible=True)
        for item in _array(raw["reason_codes"], "auth reason codes", maximum=3)
    )
    shape_value = raw["accepted_public_shape"]
    shape: Mapping[str, object] | None
    if shape_value is None:
        shape = None
    else:
        shape_raw = _object_fields(
            shape_value,
            fields=frozenset({"schema", "version", "fields"}),
            label="accepted public auth shape",
        )
        if type(shape_raw["version"]) is not int or shape_raw["version"] != 1:
            raise _manifest_error("accepted public auth shape version changed")
        shape_fields = tuple(
            _manifest_text(item, "accepted auth field", maximum=128, visible=True)
            for item in _array(shape_raw["fields"], "accepted auth fields", maximum=32)
        )
        if (
            set(shape_fields) != _AUTH_SHAPE_FIELDS
            or tuple(sorted(shape_fields)) != shape_fields
        ):
            raise _manifest_error("accepted public auth shape fields changed")
        shape = MappingProxyType(
            {
                "schema": _manifest_text(
                    shape_raw["schema"], "accepted auth shape schema", maximum=128
                ),
                "version": 1,
                "fields": shape_fields,
            }
        )
    if available:
        if shape is None or reasons or revalidation == "unavailable":
            raise _manifest_error("available auth evidence is incomplete")
    elif (
        shape is not None
        or reasons != _AUTH_REASON_CODES
        or revalidation != "unavailable"
    ):
        raise _manifest_error(
            "unavailable auth evidence is not the exact false verdict"
        )
    for gate in (
        "per_child_auth_attestation",
        "preinput_network_gate",
        "auth_source_lifetime",
    ):
        if gates[gate] and not available:
            raise _manifest_error("attestation gate contradicts false auth evidence")
    return MappingProxyType(
        {
            "schema": schema,
            "version": 1,
            "accepted_public_shape": shape,
            "provider": provider,
            "endpoint": endpoint,
            "auth_source": auth_source,
            "revalidation": revalidation,
            "core_gate_available": available,
            "reason_codes": reasons,
        }
    )


def _parse_path_policy(
    value: object, gates: Mapping[str, bool]
) -> Mapping[str, object]:
    raw = _object_fields(
        value,
        fields=frozenset(
            {"version", "snapshot_root_sentinel", "path_safe_persistence"}
        ),
        label="path policy evidence",
    )
    if type(raw["version"]) is not int or raw["version"] != 1:
        raise _manifest_error("path policy version must be exact integer 1")
    sentinel = _manifest_text(
        raw["snapshot_root_sentinel"], "snapshot root sentinel", maximum=64
    )
    if sentinel != ".":
        raise _manifest_error("snapshot root sentinel must be exact value '.'")
    passed = _exact_manifest_bool(raw["path_safe_persistence"], "path safe persistence")
    if gates["path_safe_persistence"] and not passed:
        raise _manifest_error("path policy gate contradicts false evidence")
    return MappingProxyType(
        {
            "version": 1,
            "snapshot_root_sentinel": sentinel,
            "path_safe_persistence": passed,
        }
    )


def _parse_thinking_tuples(
    value: object,
    *,
    runtime_digest: str,
    models: frozenset[str],
) -> tuple[Mapping[str, object], ...]:
    raw = _array(value, "thinking tuples", maximum=4096)
    output: list[Mapping[str, object]] = []
    identities: set[tuple[object, ...]] = set()
    for item in raw:
        entry = _object_fields(
            item,
            fields=frozenset(
                {
                    "backend_model_id",
                    "thinking_mode",
                    "effort",
                    "budget_tokens",
                    "passed",
                }
            ),
            label="thinking tuple",
        )
        try:
            key = UsageTupleKey.from_json(
                {
                    "runtime_digest": runtime_digest,
                    "backend_model_id": entry["backend_model_id"],
                    "thinking_mode": entry["thinking_mode"],
                    "effort": entry["effort"],
                    "budget_tokens": entry["budget_tokens"],
                    "operation_class": "ordinary",
                }
            )
        except EvidenceSchemaError as error:
            raise _manifest_error("thinking tuple schema is invalid") from error
        if key.backend_model_id not in models or key.thinking_mode == "null":
            raise _manifest_error("thinking tuple is not a configured probed tuple")
        identity = (
            key.backend_model_id,
            key.thinking_mode,
            key.effort,
            key.budget_tokens,
        )
        if identity in identities:
            raise _manifest_error("thinking tuple evidence is duplicated")
        identities.add(identity)
        output.append(
            MappingProxyType(
                {
                    "backend_model_id": key.backend_model_id,
                    "thinking_mode": key.thinking_mode,
                    "effort": key.effort,
                    "budget_tokens": key.budget_tokens,
                    "passed": _exact_manifest_bool(
                        entry["passed"], "thinking tuple verdict"
                    ),
                }
            )
        )
    output.sort(
        key=lambda entry: (
            cast(str, entry["backend_model_id"]),
            cast(str, entry["thinking_mode"]),
            entry["effort"] is not None,
            cast(str | None, entry["effort"]) or "",
            entry["budget_tokens"] is not None,
            cast(int | None, entry["budget_tokens"]) or 0,
        )
    )
    return tuple(output)


def _manifest_from_json(value: object) -> FeasibilityManifest:
    if type(value) is not dict:
        raise _manifest_error("manifest must be an exact JSON object")
    _validate_canonical_tree(value)
    _forbid_content_fields(value)
    raw = _object_fields(value, fields=_TOP_LEVEL_FIELDS, label="manifest")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise _manifest_error("manifest schema_version must be exact integer 1")
    validated_at = _utc_timestamp(raw["validated_at"], "manifest validation time")
    gates = _bool_matrix(
        raw["core_gates"], names=CORE_GATE_NAMES, label="core gate map"
    )
    policy = _parse_policy(raw["policy_evidence"], gates)
    runtime = _parse_runtime(raw["runtime_evidence"], gates)
    try:
        mount = _mount_identity_from_json(raw["mount_identity"])
    except SdkToolEvidenceError as error:
        raise _manifest_error(
            "manifest mount identity field set or mount flags are invalid"
        ) from error
    lifecycle = _parse_lifecycle(raw["sync_lock_lifecycle_evidence"], gates)
    environment = _parse_environment(raw["environment_evidence"])
    auth = _parse_auth(raw["auth_evidence"], gates)
    path_policy = _parse_path_policy(raw["path_policy_evidence"], gates)
    backend_class = _manifest_text(
        raw["backend_class"], "backend class", maximum=64, visible=True
    )
    auth_class = _manifest_text(
        raw["auth_class"], "auth class", maximum=64, visible=True
    )
    semantic_class = _manifest_text(
        raw["semantic_class"], "semantic class", maximum=64, visible=True
    )
    if (backend_class, auth_class, semantic_class) != (
        _SDK_TOOL_BACKEND_CLASS,
        _SDK_TOOL_AUTH_CLASS,
        _SDK_TOOL_SEMANTIC_CLASS,
    ):
        raise _manifest_error("backend/auth/semantic class tuple changed")
    model_map = _validate_model_map(raw["model_map"])
    thinking = _parse_thinking_tuples(
        raw["thinking_tuples"],
        runtime_digest=cast(str, runtime["runtime_digest"]),
        models=frozenset(model_map.values()),
    )
    usage_raw = raw["usage_evidence"]
    sdk_raw = raw["sdk_tool_evidence"]
    if type(usage_raw) is not dict or type(sdk_raw) is not dict:
        raise _manifest_error("embedded evidence domains must be exact JSON objects")
    instance = object.__new__(FeasibilityManifest)
    object.__setattr__(instance, "schema_version", 1)
    object.__setattr__(instance, "validated_at", validated_at)
    object.__setattr__(instance, "core_gates", gates)
    object.__setattr__(instance, "policy_evidence", policy)
    object.__setattr__(instance, "runtime_evidence", runtime)
    object.__setattr__(instance, "mount_identity", mount)
    object.__setattr__(instance, "sync_lock_lifecycle_evidence", lifecycle)
    object.__setattr__(instance, "environment_evidence", environment)
    object.__setattr__(instance, "auth_evidence", auth)
    object.__setattr__(instance, "path_policy_evidence", path_policy)
    object.__setattr__(instance, "backend_class", backend_class)
    object.__setattr__(instance, "auth_class", auth_class)
    object.__setattr__(instance, "semantic_class", semantic_class)
    object.__setattr__(instance, "model_map", model_map)
    object.__setattr__(instance, "thinking_tuples", thinking)
    object.__setattr__(
        instance, "_usage_evidence", cast(Mapping[str, object], _freeze_json(usage_raw))
    )
    object.__setattr__(
        instance,
        "_sdk_tool_evidence",
        cast(Mapping[str, object], _freeze_json(sdk_raw)),
    )
    return instance


class _DuplicateManifestKey(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateManifestKey
        result[key] = value
    return result


def _reject_json_float(_value: str) -> object:
    raise _manifest_error("manifest JSON contains a float")


def _reject_json_constant(_value: str) -> object:
    raise _manifest_error("manifest JSON contains a non-finite constant")


def load_manifest(path: Path) -> FeasibilityManifest:
    """Load one bounded duplicate-free immutable Phase 0 manifest."""
    if type(path) is not type(Path()):
        raise _manifest_error("manifest file path must be an exact platform Path")
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError
        if metadata.st_size > _MANIFEST_MAX_BYTES:
            raise _manifest_error("manifest file exceeds its byte bound")
        encoded = path.read_bytes()
    except ManifestError:
        raise
    except OSError as error:
        raise _manifest_error("manifest file is unavailable") from error
    if len(encoded) > _MANIFEST_MAX_BYTES:
        raise _manifest_error("manifest file exceeds its byte bound")
    try:
        text = encoded.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=_reject_json_float,
            parse_constant=_reject_json_constant,
        )
    except _DuplicateManifestKey as error:
        raise _manifest_error(
            "manifest contains a duplicate JSON object key"
        ) from error
    except ManifestError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _manifest_error("manifest JSON is invalid") from error
    return _manifest_from_json(value)


def require_core_gates(manifest: FeasibilityManifest) -> None:
    """Require the exact Phase 0 core conjunction without optional tools."""
    if type(manifest) is not FeasibilityManifest:
        raise _manifest_error("core gate check requires a FeasibilityManifest")
    if set(manifest.core_gates) != CORE_GATE_NAMES:
        raise _manifest_error("Phase 0 manifest core gate set changed")
    false_gates = sorted(
        gate for gate, verdict in manifest.core_gates.items() if verdict is False
    )
    if false_gates:
        raise _manifest_error(
            "Phase 0 manifest has false core gates: " + ", ".join(false_gates)
        )


def load_usage_evidence(manifest: FeasibilityManifest) -> UsageEvidenceSchema:
    """Revalidate the embedded Task 8/9 usage schema and exact core rows."""
    if type(manifest) is not FeasibilityManifest:
        raise _manifest_error("usage evidence requires a FeasibilityManifest")
    try:
        schema = UsageEvidenceSchema.from_json(_thaw_json(manifest._usage_evidence))
    except (EvidenceSchemaError, TypeError, ValueError) as error:
        raise _manifest_error("embedded usage evidence is invalid") from error
    configured_models = frozenset(manifest.model_map.values())
    for row in schema.rows:
        if row.key.runtime_digest != manifest.runtime_digest:
            raise _manifest_error("usage evidence runtime digest does not match")
        if row.key.backend_model_id not in configured_models:
            raise _manifest_error(
                "usage evidence does not name a configured exact backend model"
            )
    if manifest.core_gates["exact_usage_schema"]:
        for model in configured_models:
            key = UsageTupleKey(
                runtime_digest=manifest.runtime_digest,
                backend_model_id=model,
                thinking_mode="null",
                effort=None,
                budget_tokens=None,
                operation_class=UsageOperationClass.ORDINARY,
            )
            try:
                schema.require_mapping(key, UsageDialect.ANTHROPIC)
            except EvidenceSchemaError as error:
                raise _manifest_error(
                    "exact usage core lacks a passing ordinary Anthropic mapping"
                ) from error
    for entry in manifest.thinking_tuples:
        key = UsageTupleKey(
            runtime_digest=manifest.runtime_digest,
            backend_model_id=cast(str, entry["backend_model_id"]),
            thinking_mode=cast(
                Literal["null", "disabled", "adaptive", "enabled"],
                entry["thinking_mode"],
            ),
            effort=cast(str | None, entry["effort"]),
            budget_tokens=cast(int | None, entry["budget_tokens"]),
            operation_class=UsageOperationClass.ORDINARY,
        )
        matching = next((row for row in schema.rows if row.key == key), None)
        if matching is None:
            raise _manifest_error("probed thinking tuple lacks its exact usage row")
        if entry["passed"] is True:
            try:
                schema.require_mapping(key, UsageDialect.ANTHROPIC)
            except EvidenceSchemaError as error:
                raise _manifest_error(
                    "passing thinking tuple lacks its exact Anthropic mapping"
                ) from error
    return schema


_DIGESTIBLE_SDK_RECORDS: dict[int, weakref.ReferenceType[SdkToolEvidenceRecord]] = {}


def _mark_digestible_sdk_record(record: SdkToolEvidenceRecord) -> None:
    identity = id(record)

    def discard(_reference: weakref.ReferenceType[SdkToolEvidenceRecord]) -> None:
        _DIGESTIBLE_SDK_RECORDS.pop(identity, None)

    _DIGESTIBLE_SDK_RECORDS[identity] = weakref.ref(record, discard)


def _record_is_digestible(record: SdkToolEvidenceRecord) -> bool:
    reference = _DIGESTIBLE_SDK_RECORDS.get(id(record))
    return reference is not None and reference() is record


def load_sdk_tool_evidence(
    manifest: FeasibilityManifest,
) -> SdkToolEvidenceManifest:
    """Load only the exact Task 9 SDK-tool record schema, without supplied digests."""
    if type(manifest) is not FeasibilityManifest:
        raise _manifest_error("SDK tool evidence requires a FeasibilityManifest")
    raw = _thaw_json(manifest._sdk_tool_evidence)
    try:
        outer = _object_fields(
            raw,
            fields=frozenset({"records"}),
            label="SDK tool evidence",
        )
        records_raw = _array(
            outer["records"], "SDK tool evidence records", maximum=_SDK_TOOL_MAX_RECORDS
        )
        records = tuple(SdkToolEvidenceRecord.from_json(item) for item in records_raw)
        result = SdkToolEvidenceManifest.from_records(records)
    except ManifestError:
        raise
    except (SdkToolEvidenceError, TypeError, ValueError) as error:
        raise _manifest_error("SDK tool record schema is invalid") from error
    for record in result.records:
        _mark_digestible_sdk_record(record)
    return result


def sdk_tool_record_digest(record: SdkToolEvidenceRecord) -> str:
    """Return the sole V1 cross-phase identity of one loaded SDK-tool record."""
    if type(record) is not SdkToolEvidenceRecord or not _record_is_digestible(record):
        raise _manifest_error("SDK tool record schema provenance is invalid")
    try:
        revalidated = SdkToolEvidenceRecord.from_json(record.to_json())
    except (SdkToolEvidenceError, TypeError, ValueError) as error:
        raise _manifest_error("SDK tool record schema is invalid") from error
    if revalidated != record:
        raise _manifest_error("SDK tool record schema changed after loading")
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
    return hashlib.sha256(
        b"claude-sdk-proxy:sdk-tool-record:v1\0" + canonical_evidence_json(projection)
    ).hexdigest()


def _sdk_record_matches_manifest(
    manifest: FeasibilityManifest, record: SdkToolEvidenceRecord
) -> bool:
    runtime = manifest.runtime_evidence
    key = record.key
    return (
        key.runtime_digest == manifest.runtime_digest
        and key.sdk_version == manifest.sdk_version
        and key.cli_version == manifest.cli_version
        and key.cli_executable_device == runtime["cli_executable_device"]
        and key.cli_executable_inode == runtime["cli_executable_inode"]
        and key.cli_executable_sha256 == runtime["cli_executable_sha256"]
        and key.darwin_version == runtime["darwin_version"]
        and key.darwin_build == runtime["darwin_build"]
        and key.boot_id == runtime["boot_id"]
        and key.mount_identity == manifest.mount_identity
        and key.backend_class == manifest.backend_class
        and key.auth_class == manifest.auth_class
        and key.semantic_class == manifest.semantic_class
    )


def sdk_tool_gate_passed(manifest: FeasibilityManifest, exact_model_id: str) -> bool:
    """Return the exact optional SDK-tool verdict without dialect admission."""
    if type(manifest) is not FeasibilityManifest:
        raise _manifest_error("SDK tool gate requires a FeasibilityManifest")
    try:
        model = require_exact_backend_model(exact_model_id)
    except (TypeError, ExactBackendModelError) as error:
        raise _manifest_error(
            "SDK tool gate requires an exact backend model ID"
        ) from error
    if model not in manifest.model_map.values():
        return False
    evidence = load_sdk_tool_evidence(manifest)
    matching = tuple(
        record
        for record in evidence.records
        if record.key.backend_model_id == model
        and _sdk_record_matches_manifest(manifest, record)
    )
    if len(matching) != 1 or not all(matching[0].gates.values()):
        return False
    schema = load_usage_evidence(manifest)
    for operation in (
        UsageOperationClass.TOOL_USE_BOUNDARY,
        UsageOperationClass.POST_TOOL_RESULT,
    ):
        key = UsageTupleKey(
            runtime_digest=matching[0].key.runtime_digest,
            backend_model_id=model,
            thinking_mode="null",
            effort=None,
            budget_tokens=None,
            operation_class=operation,
        )
        try:
            row = schema.only_tool_row(key)
        except EvidenceSchemaError:
            return False
        if not row.sdk_shape_passed:
            return False
    return True


def _phase0_projection(
    manifest: FeasibilityManifest,
    public_alias: str,
    exact_backend_model_id: str,
) -> dict[str, object]:
    return {
        "digest_schema_version": 1,
        "manifest_schema_version": manifest.schema_version,
        "validated_at": manifest.validated_at,
        "core_gates": dict(manifest.core_gates),
        "policy_evidence": _thaw_json(manifest.policy_evidence),
        "runtime_evidence": _thaw_json(manifest.runtime_evidence),
        "mount_identity": _mount_identity_to_json(manifest.mount_identity),
        "sync_lock_lifecycle_evidence": _thaw_json(
            manifest.sync_lock_lifecycle_evidence
        ),
        "environment_evidence": _thaw_json(manifest.environment_evidence),
        "auth_evidence": _thaw_json(manifest.auth_evidence),
        "path_policy_evidence": _thaw_json(manifest.path_policy_evidence),
        "backend_class": manifest.backend_class,
        "auth_class": manifest.auth_class,
        "semantic_class": manifest.semantic_class,
        "model_map": dict(manifest.model_map),
        "selected_model": {
            "public_alias": public_alias,
            "exact_backend_model_id": exact_backend_model_id,
        },
    }


def _exact_alias_model_pair(
    manifest: FeasibilityManifest,
    public_alias: object,
    exact_backend_model_id: object,
) -> tuple[str, str]:
    if type(public_alias) is not str or _PUBLIC_ALIAS.fullmatch(public_alias) is None:
        raise _manifest_error("exact alias/backend model pair is invalid")
    try:
        model = require_exact_backend_model(exact_backend_model_id)
    except (TypeError, ExactBackendModelError) as error:
        raise _manifest_error("exact alias/backend model pair is invalid") from error
    if manifest.model_map.get(public_alias) != model:
        raise _manifest_error("exact alias/backend model pair is not configured")
    return public_alias, model


def phase0_prerequisite_digest(
    manifest: FeasibilityManifest,
    public_alias: str,
    exact_backend_model_id: str,
) -> str:
    """Bind every Phase 0 prerequisite for one exact alias/model selection."""
    if type(manifest) is not FeasibilityManifest:
        raise _manifest_error("phase0 digest requires a FeasibilityManifest")
    require_core_gates(manifest)
    alias, model = _exact_alias_model_pair(
        manifest, public_alias, exact_backend_model_id
    )
    return hashlib.sha256(
        b"claude-sdk-proxy:phase0-prerequisite:v1\0"
        + canonical_evidence_json(_phase0_projection(manifest, alias, model))
    ).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class Phase0PrerequisiteDigestResolver:
    """Immutable exact alias/model to Phase 0 prerequisite digest resolver."""

    digests: Mapping[tuple[str, str], str]

    def __init__(self, digests: Mapping[tuple[str, str], str]) -> None:
        if type(digests) not in {dict, _MAPPING_PROXY_TYPE}:
            raise _manifest_error("prerequisite digest map must be an exact mapping")
        snapshot = dict(digests)
        output: dict[tuple[str, str], str] = {}
        for pair, digest in snapshot.items():
            if (
                type(pair) is not tuple
                or len(pair) != 2
                or type(pair[0]) is not str
                or type(pair[1]) is not str
                or _PUBLIC_ALIAS.fullmatch(pair[0]) is None
            ):
                raise _manifest_error("prerequisite digest key is invalid")
            try:
                require_exact_backend_model(pair[1])
            except (TypeError, ExactBackendModelError) as error:
                raise _manifest_error("prerequisite digest key is invalid") from error
            output[pair] = _manifest_digest(digest, "prerequisite digest")
        object.__setattr__(
            self, "digests", MappingProxyType(dict(sorted(output.items())))
        )

    @classmethod
    def from_manifest(
        cls, manifest: FeasibilityManifest
    ) -> Phase0PrerequisiteDigestResolver:
        require_core_gates(manifest)
        return cls(
            {
                (alias, model): phase0_prerequisite_digest(manifest, alias, model)
                for alias, model in manifest.model_map.items()
            }
        )

    def resolve(self, public_alias: str, exact_backend_model_id: str) -> str:
        if type(public_alias) is not str or type(exact_backend_model_id) is not str:
            raise _manifest_error("exact model prerequisite digest is missing")
        try:
            return self.digests[(public_alias, exact_backend_model_id)]
        except KeyError as error:
            raise _manifest_error(
                "exact model prerequisite digest is missing"
            ) from error

    def validate(self, manifest: FeasibilityManifest) -> None:
        expected = Phase0PrerequisiteDigestResolver.from_manifest(manifest)
        if self.digests != expected.digests:
            raise _manifest_error("exact model prerequisite digest map changed")


_F_FULLFSYNC: Final = 51


def _fullfsync_fd(descriptor: int) -> None:
    fcntl.fcntl(descriptor, _F_FULLFSYNC)


def _fsync_directory_fd(descriptor: int) -> None:
    os.fsync(descriptor)


def _atomic_write_manifest(path: Path, document: object) -> None:
    """Write canonical evidence by same-directory full-synced rename."""
    if type(path) is not type(Path()):
        raise _manifest_error("manifest output path must be an exact platform Path")
    if not path.is_absolute():
        raise _manifest_error("manifest output path must be absolute")
    payload = canonical_evidence_json(document) + b"\n"
    parent = path.parent
    parent_fd = -1
    temp_fd = -1
    temp_name: str | None = None
    renamed = False
    try:
        parent_fd = os.open(
            parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        for _ in range(8):
            candidate = f".{path.name}.{secrets.token_hex(16)}.tmp"
            try:
                temp_fd = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                continue
            temp_name = candidate
            break
        if temp_fd < 0 or temp_name is None:
            raise OSError
        os.fchmod(temp_fd, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(temp_fd, view)
            if written <= 0:
                raise OSError
            view = view[written:]
        _fullfsync_fd(temp_fd)
        os.close(temp_fd)
        temp_fd = -1
        os.rename(
            temp_name,
            path.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        renamed = True
        _fsync_directory_fd(parent_fd)
    except (OSError, ManifestError) as error:
        if isinstance(error, ManifestError):
            raise
        raise _manifest_error("atomic manifest write failed") from error
    finally:
        if temp_fd >= 0:
            try:
                os.close(temp_fd)
            except OSError:
                pass
        if temp_name is not None and not renamed and parent_fd >= 0:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except OSError:
                pass
        if parent_fd >= 0:
            try:
                os.close(parent_fd)
            except OSError:
                pass


__all__ = [
    "CORE_GATE_NAMES",
    "CliIdentity",
    "FeasibilityManifest",
    "ManifestError",
    "POLICY_URLS",
    "Phase0PrerequisiteDigestResolver",
    "PolicyEvidence",
    "REQUIRED_SDK_TOOL_GATES",
    "REQUIRED_SDK_TOOL_REPRESENTATIVE_NAMES",
    "RuntimeMismatch",
    "RuntimeTuple",
    "SdkMcpNamingRule",
    "SdkToolEvidenceError",
    "SdkToolEvidenceKey",
    "SdkToolEvidenceManifest",
    "SdkToolEvidenceRecord",
    "ToolNameError",
    "canonical_evidence_json",
    "load_manifest",
    "load_sdk_tool_evidence",
    "load_usage_evidence",
    "phase0_prerequisite_digest",
    "read_cli_identity",
    "require_core_gates",
    "sdk_tool_gate_passed",
    "sdk_tool_record_digest",
    "validate_policy",
]
