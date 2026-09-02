"""Fail-closed validation of the pinned policy and local runtime inputs."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
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
SDK_MCP_GENERATED_NAME_ALGORITHM: Final = (
    "mcp__{server_identity}__{caller_name}"
)
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
                raise _sdk_tool_error(
                    "SDK tool evidence string exceeds its byte bound"
                )
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
                raise _sdk_tool_error(
                    "SDK MCP naming observation must be exact text"
                )
            expected = self.derive(caller_name)
            if observed != expected:
                raise _sdk_tool_error(
                    "SDK MCP naming observation is inconsistent with derivation"
                )
            if observed in generated_names:
                raise _sdk_tool_error(
                    "SDK MCP naming observations must be injective"
                )
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
        if (
            type(server["name"]) is not str
            or server["name"] != self.server_identity
        ):
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
            "representative_observations": dict(
                self.representative_observations
            ),
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


@dataclass(frozen=True, slots=True)
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
