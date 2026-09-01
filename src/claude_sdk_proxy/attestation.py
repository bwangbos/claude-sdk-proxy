"""Versioned public child attestation and zero-preinput release gate."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Never, Protocol

from claude_agent_sdk import ClaudeAgentOptions, SystemMessage

from claude_sdk_proxy.isolation import IsolationConfig, build_agent_options

ATTESTATION_MANIFEST_SCHEMA = "claude_sdk_proxy.child_attestation_manifest"
ATTESTATION_MANIFEST_VERSION = 1
PUBLIC_INIT_SCHEMA = "claude_sdk_proxy.public_child_init"
PUBLIC_INIT_VERSION = 1
SUPERVISOR_TRACE_SCHEMA = "claude_sdk_proxy.supervisor_trace"
SUPERVISOR_TRACE_VERSION = 1
EXPECTED_SDK_VERSION = "0.2.148"
EXPECTED_CLI_VERSION = "2.1.251"
EXPECTED_PROVIDER = "anthropic"
EXPECTED_ENDPOINT = "default"
EXPECTED_AUTH_SOURCE = "existing_claude_login"
ENVIRONMENT_FINGERPRINT_ALGORITHM = "sha256-name-nul-value-nul-v1"

_EXPECTED_TRACE = (
    "SUPERVISOR_IDENTITY",
    "IDENTITY_ACK",
    "ANCHOR_IDENTITY",
    "ANCHOR_ACK",
    "CLI_ARMED",
    "ARMED_ACK",
    "CLI_RUNNING",
)
_EXPECTED_CANONICAL_TYPES = (
    "SUPERVISOR_IDENTITY",
    "ANCHOR_IDENTITY",
    "CLI_ARMED",
    "CLI_RUNNING",
)
_EXPECTED_CANONICAL_SEQUENCES = (1, 2, 3, 4)
_REQUIRED_EVIDENCE_FIELDS = (
    "sdk_version",
    "cli_version",
    "cli_executable",
    "environment_fingerprint",
    "provider",
    "endpoint",
    "auth_source",
    "supervisor_trace",
    "preinput_boundary",
)
BOOTSTRAP_DESCRIPTOR_NAMES = (
    "LOCAL_PROXY_ALLOCATION_NONCE",
    "LOCAL_PROXY_INSTANCE_DIR",
    "LOCAL_PROXY_REAL_CLAUDE",
    "LOCAL_PROXY_CONTROL_FD",
)
SUPERVISOR_IDENTITY_ACK_CONFIG_VERSION = 1
SUPERVISOR_INTERNAL_RELAY_FD = 198
_AMBIENT_NETWORK_PROXY_SELECTOR = "LOCAL_PROXY_NETWORK_PROXY"


class AttestationError(RuntimeError):
    """Raised whenever public child provenance is absent or ambiguous."""


def _require_sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise AttestationError(f"{label} must be a lowercase SHA-256 value")
    return value


def _require_nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AttestationError(f"{label} must be a nonnegative integer")
    return value


def _canonical_digest(domain: bytes, value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(domain + b"\0" + encoded).hexdigest()


@dataclass(frozen=True)
class CliExecutableIdentity:
    """Public non-secret identity for the exact verified CLI executable."""

    path_sha256: str
    st_dev: int
    st_ino: int
    mode: int
    sha256: str

    def __post_init__(self) -> None:
        _require_sha256(self.path_sha256, "CLI path hash")
        _require_nonnegative_int(self.st_dev, "CLI device")
        _require_nonnegative_int(self.st_ino, "CLI inode")
        _require_nonnegative_int(self.mode, "CLI mode")
        _require_sha256(self.sha256, "CLI executable hash")
        if not stat.S_ISREG(self.mode) or not self.mode & stat.S_IXUSR:
            raise AttestationError("CLI mode must identify an executable regular file")

    def digest(self) -> str:
        """Return the only executable identity retained by ChildAttestation."""
        return _canonical_digest(
            b"claude-sdk-proxy:cli-executable-identity:v1",
            {
                "path_sha256": self.path_sha256,
                "st_dev": self.st_dev,
                "st_ino": self.st_ino,
                "mode": self.mode,
                "sha256": self.sha256,
            },
        )


@dataclass(frozen=True)
class SupervisorTrace:
    """Exact value-free Task 5 bootstrap trace accepted by Task 6."""

    trace: tuple[str, ...]
    canonical_types: tuple[str, ...]
    canonical_sequences: tuple[int, ...]
    ack_after_durable_certification: bool
    exact_canonical_ack_heads: bool
    post_exec_identity_verified: bool
    cli_control_fd_closed_on_exec: bool
    bootstrap_environment_removed: bool

    def __post_init__(self) -> None:
        if self.trace != _EXPECTED_TRACE:
            raise AttestationError("supervisor trace does not match Task 5")
        if self.canonical_types != _EXPECTED_CANONICAL_TYPES:
            raise AttestationError("supervisor canonical types do not match Task 5")
        if self.canonical_sequences != _EXPECTED_CANONICAL_SEQUENCES:
            raise AttestationError(
                "supervisor canonical sequence does not match Task 5"
            )
        flags = (
            self.ack_after_durable_certification,
            self.exact_canonical_ack_heads,
            self.post_exec_identity_verified,
            self.cli_control_fd_closed_on_exec,
            self.bootstrap_environment_removed,
        )
        if any(not isinstance(flag, bool) for flag in flags) or not all(flags):
            raise AttestationError("supervisor trace is not positively certified")

    def digest(self) -> str:
        """Return a value-safe identity for the accepted supervisor trace."""
        return _canonical_digest(
            b"claude-sdk-proxy:supervisor-trace:v1",
            {
                "schema": SUPERVISOR_TRACE_SCHEMA,
                "version": SUPERVISOR_TRACE_VERSION,
                "trace": self.trace,
                "canonical_types": self.canonical_types,
                "canonical_sequences": self.canonical_sequences,
                "ack_after_durable_certification": (
                    self.ack_after_durable_certification
                ),
                "exact_canonical_ack_heads": self.exact_canonical_ack_heads,
                "post_exec_identity_verified": self.post_exec_identity_verified,
                "cli_control_fd_closed_on_exec": self.cli_control_fd_closed_on_exec,
                "bootstrap_environment_removed": self.bootstrap_environment_removed,
            },
        )


class AttestationManifestProtocol(Protocol):
    """Narrow Task 10 construction boundary consumed by the parser."""

    @property
    def schema(self) -> str: ...

    @property
    def version(self) -> int: ...

    @property
    def sdk_version(self) -> str: ...

    @property
    def cli_version(self) -> str: ...

    @property
    def cli_executable(self) -> CliExecutableIdentity: ...

    @property
    def environment_fingerprint(self) -> str: ...

    @property
    def provider(self) -> str: ...

    @property
    def endpoint(self) -> str: ...

    @property
    def auth_source(self) -> str: ...

    @property
    def supervisor_trace(self) -> SupervisorTrace: ...


@dataclass(frozen=True)
class AttestationManifest:
    """Immutable v1 values needed solely for one child provenance decision."""

    schema: str
    version: int
    sdk_version: str
    cli_version: str
    cli_executable: CliExecutableIdentity
    environment_fingerprint: str
    provider: str
    endpoint: str
    auth_source: str
    supervisor_trace: SupervisorTrace

    def __post_init__(self) -> None:
        if self.schema != ATTESTATION_MANIFEST_SCHEMA:
            raise AttestationError("unsupported attestation manifest schema")
        if (
            isinstance(self.version, bool)
            or self.version != ATTESTATION_MANIFEST_VERSION
        ):
            raise AttestationError("unsupported attestation manifest version")
        if self.sdk_version != EXPECTED_SDK_VERSION:
            raise AttestationError("manifest SDK version is not pinned to 0.2.148")
        if self.cli_version != EXPECTED_CLI_VERSION:
            raise AttestationError("manifest CLI version is not pinned to 2.1.251")
        if not isinstance(self.cli_executable, CliExecutableIdentity):
            raise AttestationError("manifest CLI executable identity is invalid")
        _require_sha256(
            self.environment_fingerprint,
            "manifest environment fingerprint",
        )
        if self.provider != EXPECTED_PROVIDER:
            raise AttestationError("manifest provider must be anthropic")
        if self.endpoint != EXPECTED_ENDPOINT:
            raise AttestationError("manifest endpoint must be default")
        if self.auth_source != EXPECTED_AUTH_SOURCE:
            raise AttestationError(
                "manifest auth source must be existing_claude_login"
            )
        if not isinstance(self.supervisor_trace, SupervisorTrace):
            raise AttestationError("manifest supervisor trace is invalid")


def _snapshot_manifest(
    manifest: AttestationManifestProtocol,
) -> AttestationManifest:
    try:
        return AttestationManifest(
            schema=manifest.schema,
            version=manifest.version,
            sdk_version=manifest.sdk_version,
            cli_version=manifest.cli_version,
            cli_executable=manifest.cli_executable,
            environment_fingerprint=manifest.environment_fingerprint,
            provider=manifest.provider,
            endpoint=manifest.endpoint,
            auth_source=manifest.auth_source,
            supervisor_trace=manifest.supervisor_trace,
        )
    except AttributeError as error:
        raise AttestationError("attestation manifest is incomplete") from error


@dataclass(frozen=True)
class ChildAttestation:
    """Redacted positive provenance retained for one connected child."""

    version: int
    field_names: tuple[str, ...]
    auth_source: str
    provider: str
    endpoint: str
    environment_fingerprint: str
    executable_identity_sha256: str
    supervisor_trace_sha256: str
    evidence_sha256: str


def _require_exact_mapping(
    value: object,
    expected_keys: frozenset[str],
    label: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AttestationError(f"{label} must be an object")
    keys = set(value)
    if keys != expected_keys or any(not isinstance(key, str) for key in value):
        raise AttestationError(
            f"unknown {label} schema fields or required fields are missing"
        )
    return value


def _parse_evidence_records(value: object) -> dict[str, object]:
    if not isinstance(value, list):
        raise AttestationError("init evidence must be a list")
    parsed: dict[str, object] = {}
    ordered_names: list[str] = []
    for record_value in value:
        record = _require_exact_mapping(
            record_value,
            frozenset({"field", "value"}),
            "init evidence record",
        )
        name = record["field"]
        if not isinstance(name, str):
            raise AttestationError("init evidence field name must be a string")
        if name not in _REQUIRED_EVIDENCE_FIELDS:
            raise AttestationError(f"unknown evidence field: {name}")
        if name in parsed:
            raise AttestationError(f"duplicate evidence field: {name}")
        parsed[name] = record["value"]
        ordered_names.append(name)
    missing = set(_REQUIRED_EVIDENCE_FIELDS) - set(parsed)
    if missing:
        raise AttestationError(
            "missing evidence fields: " + ",".join(sorted(missing))
        )
    if tuple(ordered_names) != _REQUIRED_EVIDENCE_FIELDS:
        raise AttestationError("init evidence field order does not match schema v1")
    return parsed


def _parse_cli_executable(value: object) -> CliExecutableIdentity:
    identity = _require_exact_mapping(
        value,
        frozenset({"path_sha256", "st_dev", "st_ino", "mode", "sha256"}),
        "CLI executable schema",
    )
    return CliExecutableIdentity(
        path_sha256=_require_sha256(identity["path_sha256"], "CLI path hash"),
        st_dev=_require_nonnegative_int(identity["st_dev"], "CLI device"),
        st_ino=_require_nonnegative_int(identity["st_ino"], "CLI inode"),
        mode=_require_nonnegative_int(identity["mode"], "CLI mode"),
        sha256=_require_sha256(identity["sha256"], "CLI executable hash"),
    )


def _require_string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AttestationError(f"{label} must be a list of strings")
    return tuple(value)


def _require_int_tuple(value: object, label: str) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise AttestationError(f"{label} must be a list of integers")
    return tuple(_require_nonnegative_int(item, label) for item in value)


def _require_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise AttestationError(f"{label} must be a boolean")
    return value


def _parse_supervisor_trace(value: object) -> SupervisorTrace:
    trace = _require_exact_mapping(
        value,
        frozenset(
            {
                "schema",
                "version",
                "trace",
                "canonical_types",
                "canonical_sequences",
                "ack_after_durable_certification",
                "exact_canonical_ack_heads",
                "post_exec_identity_verified",
                "cli_control_fd_closed_on_exec",
                "bootstrap_environment_removed",
            }
        ),
        "supervisor trace",
    )
    if trace["schema"] != SUPERVISOR_TRACE_SCHEMA:
        raise AttestationError("supervisor trace schema is not supported")
    if (
        isinstance(trace["version"], bool)
        or trace["version"] != SUPERVISOR_TRACE_VERSION
    ):
        raise AttestationError("supervisor trace version is not supported")
    flag_names = (
        "ack_after_durable_certification",
        "exact_canonical_ack_heads",
        "post_exec_identity_verified",
        "cli_control_fd_closed_on_exec",
        "bootstrap_environment_removed",
    )
    if any(not isinstance(trace[name], bool) for name in flag_names):
        raise AttestationError("supervisor trace certification flags are malformed")
    return SupervisorTrace(
        trace=_require_string_tuple(trace["trace"], "supervisor trace"),
        canonical_types=_require_string_tuple(
            trace["canonical_types"], "supervisor canonical types"
        ),
        canonical_sequences=_require_int_tuple(
            trace["canonical_sequences"], "supervisor canonical sequences"
        ),
        ack_after_durable_certification=_require_bool(
            trace["ack_after_durable_certification"],
            "supervisor durable acknowledgement",
        ),
        exact_canonical_ack_heads=_require_bool(
            trace["exact_canonical_ack_heads"],
            "supervisor canonical acknowledgement heads",
        ),
        post_exec_identity_verified=_require_bool(
            trace["post_exec_identity_verified"],
            "supervisor post-exec identity",
        ),
        cli_control_fd_closed_on_exec=_require_bool(
            trace["cli_control_fd_closed_on_exec"],
            "supervisor CLI control close",
        ),
        bootstrap_environment_removed=_require_bool(
            trace["bootstrap_environment_removed"],
            "supervisor bootstrap environment removal",
        ),
    )


def _parse_environment_fingerprint(value: object) -> str:
    evidence = _require_exact_mapping(
        value,
        frozenset({"algorithm", "sha256"}),
        "environment fingerprint",
    )
    if evidence["algorithm"] != ENVIRONMENT_FINGERPRINT_ALGORITHM:
        raise AttestationError("environment fingerprint algorithm is not supported")
    return _require_sha256(evidence["sha256"], "environment fingerprint")


def _require_zero_preinput_boundary(value: object) -> None:
    evidence = _require_exact_mapping(
        value,
        frozenset({"model_bytes", "network_bytes"}),
        "pre-input boundary",
    )
    model_bytes = _require_nonnegative_int(
        evidence["model_bytes"], "pre-input model bytes"
    )
    network_bytes = _require_nonnegative_int(
        evidence["network_bytes"], "pre-input network bytes"
    )
    if model_bytes != 0 or network_bytes != 0:
        raise AttestationError("pre-input model/network byte boundary is not zero")


def extract_child_attestation(
    event: SystemMessage,
    manifest: AttestationManifestProtocol,
) -> ChildAttestation:
    """Validate one exact public init event without inspecting private state."""
    expected = _snapshot_manifest(manifest)
    if not isinstance(event, SystemMessage):
        raise AttestationError("child evidence must be a public SystemMessage")
    if event.subtype != "init":
        raise AttestationError("child evidence subtype must be init")
    data = _require_exact_mapping(
        event.data,
        frozenset({"schema", "version", "evidence"}),
        "init event",
    )
    if data["schema"] != PUBLIC_INIT_SCHEMA:
        raise AttestationError("public init schema is not supported")
    if isinstance(data["version"], bool) or data["version"] != PUBLIC_INIT_VERSION:
        raise AttestationError("public init version is not supported")
    values = _parse_evidence_records(data["evidence"])

    if values["sdk_version"] != expected.sdk_version:
        raise AttestationError("SDK version does not match pinned public evidence")
    if values["cli_version"] != expected.cli_version:
        raise AttestationError("CLI version does not match pinned public evidence")
    executable = _parse_cli_executable(values["cli_executable"])
    if executable != expected.cli_executable:
        raise AttestationError("CLI executable identity changed")
    environment_fingerprint = _parse_environment_fingerprint(
        values["environment_fingerprint"]
    )
    if environment_fingerprint != expected.environment_fingerprint:
        raise AttestationError("environment fingerprint changed")
    if values["provider"] != expected.provider:
        raise AttestationError("provider selection is not anthropic")
    if values["endpoint"] != expected.endpoint:
        raise AttestationError("endpoint selection is not default")
    if values["auth_source"] != expected.auth_source:
        raise AttestationError("auth source is not existing_claude_login")
    supervisor_trace = _parse_supervisor_trace(values["supervisor_trace"])
    if supervisor_trace != expected.supervisor_trace:
        raise AttestationError("supervisor trace changed")
    _require_zero_preinput_boundary(values["preinput_boundary"])

    executable_digest = executable.digest()
    supervisor_digest = supervisor_trace.digest()
    evidence_digest = _canonical_digest(
        b"claude-sdk-proxy:child-attestation:v1",
        {
            "version": PUBLIC_INIT_VERSION,
            "field_names": _REQUIRED_EVIDENCE_FIELDS,
            "sdk_version": expected.sdk_version,
            "cli_version": expected.cli_version,
            "executable_identity_sha256": executable_digest,
            "environment_fingerprint": environment_fingerprint,
            "provider": expected.provider,
            "endpoint": expected.endpoint,
            "auth_source": expected.auth_source,
            "supervisor_trace_sha256": supervisor_digest,
            "preinput_model_bytes": 0,
            "preinput_network_bytes": 0,
        },
    )
    return ChildAttestation(
        version=PUBLIC_INIT_VERSION,
        field_names=_REQUIRED_EVIDENCE_FIELDS,
        auth_source=expected.auth_source,
        provider=expected.provider,
        endpoint=expected.endpoint,
        environment_fingerprint=environment_fingerprint,
        executable_identity_sha256=executable_digest,
        supervisor_trace_sha256=supervisor_digest,
        evidence_sha256=evidence_digest,
    )


@dataclass(frozen=True)
class SupervisorBootstrapDescriptors:
    """Exact Task 5 supervisor-only launch descriptors."""

    allocation_nonce: str
    instance_dir: Path
    real_cli: Path
    control_fd: int

    def __post_init__(self) -> None:
        _require_sha256(self.allocation_nonce, "allocation nonce")
        if (
            not isinstance(self.instance_dir, Path)
            or not self.instance_dir.is_absolute()
        ):
            raise AttestationError("supervisor instance directory must be absolute")
        if not isinstance(self.real_cli, Path) or not self.real_cli.is_absolute():
            raise AttestationError("verified real CLI path must be absolute")
        _require_open_descriptor(self.control_fd, "supervisor control FD")

    def environment(self) -> dict[str, str]:
        """Return exactly the supervisor bootstrap environment additions."""
        return {
            "LOCAL_PROXY_ALLOCATION_NONCE": self.allocation_nonce,
            "LOCAL_PROXY_INSTANCE_DIR": str(self.instance_dir),
            "LOCAL_PROXY_REAL_CLAUDE": str(self.real_cli),
            "LOCAL_PROXY_CONTROL_FD": str(self.control_fd),
        }


@dataclass(frozen=True)
class SupervisorIdentityAckConfig:
    """Authenticated Task 5 launch intent, never an ambient descriptor."""

    network_proxy_enabled: bool
    version: int = SUPERVISOR_IDENTITY_ACK_CONFIG_VERSION
    reserved_byte: int = 0
    reserved_word: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.network_proxy_enabled, bool):
            raise AttestationError("network proxy selector must be a boolean")
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version != SUPERVISOR_IDENTITY_ACK_CONFIG_VERSION
        ):
            raise AttestationError("supervisor configuration version changed")
        if (
            isinstance(self.reserved_byte, bool)
            or not isinstance(self.reserved_byte, int)
            or self.reserved_byte != 0
            or isinstance(self.reserved_word, bool)
            or not isinstance(self.reserved_word, int)
            or self.reserved_word != 0
        ):
            raise AttestationError("supervisor configuration reserved fields changed")

    def payload(self, certified_sequence: int, certified_hash: bytes) -> bytes:
        """Bind the intended selector to the exact certified supervisor head."""
        sequence = _require_nonnegative_int(
            certified_sequence, "certified supervisor sequence"
        )
        if sequence == 0 or sequence >= 1 << 64:
            raise AttestationError(
                "certified supervisor sequence must be a positive uint64"
            )
        if (
            not isinstance(certified_hash, bytes)
            or len(certified_hash) != 32
            or certified_hash == bytes(32)
        ):
            raise AttestationError("certified supervisor hash must be 32 bytes")
        return struct.pack(
            "<HBBIQ32s",
            self.version,
            int(self.network_proxy_enabled),
            self.reserved_byte,
            self.reserved_word,
            sequence,
            certified_hash,
        )


def _require_open_descriptor(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AttestationError(f"{label} must be a nonnegative integer")
    return value


def _require_executable_path(path: Path, label: str) -> None:
    try:
        metadata = path.stat()
    except OSError as error:
        raise AttestationError(f"{label} is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.X_OK):
        raise AttestationError(f"{label} must be an executable regular file")


@dataclass(frozen=True)
class PreparedSupervisorLaunch:
    """Deterministic no-exec contract for a future FD-aware SDK transport."""

    options: ClaudeAgentOptions
    _supervisor_environment_items: tuple[tuple[str, str], ...]
    _real_cli_environment_items: tuple[tuple[str, str], ...]
    inherited_fds: tuple[int, ...]
    bootstrap_descriptor_names: tuple[str, ...]
    identity_ack_config: SupervisorIdentityAckConfig
    supervisor_internal_relay_fd: int = SUPERVISOR_INTERNAL_RELAY_FD
    pre_attestation_model_bytes: int = 0
    pre_attestation_network_bytes: int = 0
    local_option_serialization_only: bool = True

    @property
    def supervisor_environment(self) -> Mapping[str, str]:
        """Return an isolated copy of the supervisor launch environment."""
        return dict(self._supervisor_environment_items)

    @property
    def real_cli_environment(self) -> Mapping[str, str]:
        """Return the descriptor-free environment Task 5 passes to the CLI."""
        return dict(self._real_cli_environment_items)


def prepare_supervisor_launch(
    config: IsolationConfig,
    descriptors: SupervisorBootstrapDescriptors,
    identity_ack_config: SupervisorIdentityAckConfig,
) -> PreparedSupervisorLaunch:
    """Bind Task 2 options to Task 5 descriptors without launching a child."""
    _require_executable_path(config.supervisor_path, "verified supervisor")
    _require_executable_path(descriptors.real_cli, "verified real CLI")
    if not isinstance(identity_ack_config, SupervisorIdentityAckConfig):
        raise AttestationError("supervisor identity ACK configuration is invalid")
    if _AMBIENT_NETWORK_PROXY_SELECTOR in config.environment:
        raise AttestationError(
            "network proxy selection must use authenticated IDENTITY_ACK config"
        )
    overlap = set(config.environment) & set(BOOTSTRAP_DESCRIPTOR_NAMES)
    if overlap:
        raise AttestationError("real CLI environment contains bootstrap descriptors")
    base_options = build_agent_options(config)
    if base_options.cli_path != config.supervisor_path:
        raise AttestationError("Agent SDK options did not select the supervisor")
    bootstrap_environment = descriptors.environment()
    if tuple(bootstrap_environment) != BOOTSTRAP_DESCRIPTOR_NAMES:
        raise AttestationError("Task 5 bootstrap descriptor set changed")
    supervisor_environment = {
        **dict(config.environment),
        **bootstrap_environment,
    }
    options = replace(base_options, env=supervisor_environment)
    return PreparedSupervisorLaunch(
        options=options,
        _supervisor_environment_items=tuple(sorted(supervisor_environment.items())),
        _real_cli_environment_items=tuple(sorted(config.environment.items())),
        inherited_fds=(descriptors.control_fd,),
        bootstrap_descriptor_names=BOOTSTRAP_DESCRIPTOR_NAMES,
        identity_ack_config=identity_ack_config,
    )


@dataclass(frozen=True)
class RelayReceipt:
    """Measured bytes delivered only after a successful release decision."""

    model_bytes: int
    network_bytes: int

    def __post_init__(self) -> None:
        _require_nonnegative_int(self.model_bytes, "relayed model bytes")
        _require_nonnegative_int(self.network_bytes, "relayed network bytes")


@dataclass(frozen=True)
class QueuedTurn:
    """Opaque handle whose caller-visible state contains no turn content."""

    sequence: int

    def __post_init__(self) -> None:
        sequence = _require_nonnegative_int(self.sequence, "queued turn sequence")
        if sequence == 0:
            raise AttestationError("queued turn sequence must be positive")


class AttestationGate:
    """Own queued content until connection and per-turn provenance validate."""

    def __init__(
        self,
        manifest: AttestationManifestProtocol,
        *,
        revalidate: Callable[[], SystemMessage],
        relay: Callable[[bytes], RelayReceipt],
        close_child: Callable[[], None],
    ) -> None:
        self._manifest = _snapshot_manifest(manifest)
        self._revalidate = revalidate
        self._relay = relay
        self._close_child = close_child
        self._attestation: ChildAttestation | None = None
        self._pending: dict[int, tuple[QueuedTurn, bytes]] = {}
        self._next_sequence = 1
        self._closed = False
        self._revoked = False
        self._close_attempted = False
        self._relayed_model_bytes = 0
        self._relayed_network_bytes = 0

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def revoked(self) -> bool:
        return self._revoked

    @property
    def relayed_model_bytes(self) -> int:
        return self._relayed_model_bytes

    @property
    def relayed_network_bytes(self) -> int:
        return self._relayed_network_bytes

    @property
    def pending_turn_count(self) -> int:
        return len(self._pending)

    @property
    def queued_content_bytes(self) -> int:
        return sum(len(value) for _, value in self._pending.values())

    def _require_open(self) -> None:
        if self._closed:
            raise AttestationError("attestation gate is closed and revoked")

    def _fail_closed(self, error: AttestationError) -> Never:
        self._closed = True
        self._revoked = True
        self._attestation = None
        self._pending.clear()
        if not self._close_attempted:
            self._close_attempted = True
            try:
                self._close_child()
            except Exception as close_error:
                raise AttestationError(
                    f"{error}; child close failed during revocation"
                ) from close_error
        raise error

    def attest(self, event: SystemMessage) -> None:
        """Open this connection gate only from one content-free init event."""
        self._require_open()
        if self._attestation is not None:
            self._fail_closed(AttestationError("duplicate child attestation"))
        try:
            self._attestation = extract_child_attestation(event, self._manifest)
        except AttestationError as error:
            self._fail_closed(error)
        except Exception:
            self._fail_closed(
                AttestationError("public child attestation could not be evaluated")
            )

    def queue_turn(
        self,
        *,
        system: str,
        blocks: Sequence[Mapping[str, object]],
        mcp_schema: Mapping[str, object] | None = None,
    ) -> QueuedTurn:
        """Serialize one immutable local snapshot without relaying any bytes."""
        self._require_open()
        if not isinstance(system, str):
            self._fail_closed(AttestationError("turn system value must be a string"))
        try:
            serialized = json.dumps(
                {
                    "system": system,
                    "blocks": list(blocks),
                    "mcp_schema": dict(mcp_schema) if mcp_schema is not None else None,
                },
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            self._fail_closed(
                AttestationError("turn content is not immutable JSON input")
            )
        sequence = self._next_sequence
        self._next_sequence += 1
        turn = QueuedTurn(sequence=sequence)
        self._pending[sequence] = (turn, serialized)
        return turn

    def revalidate(self) -> None:
        """Refresh exact public provenance without mutating or relaying a turn."""
        self._require_open()
        if self._attestation is None:
            self._fail_closed(AttestationError("child is not attested"))
        try:
            current = extract_child_attestation(
                self._revalidate(),
                self._manifest,
            )
        except AttestationError as error:
            self._fail_closed(error)
        except Exception:
            self._fail_closed(
                AttestationError("public child revalidation failed")
            )
        if current.evidence_sha256 != self._attestation.evidence_sha256:
            self._fail_closed(AttestationError("child attestation changed"))

    def release(self, turn: QueuedTurn) -> None:
        """Revalidate immediately before relaying exactly one queued turn."""
        self._require_open()
        if self._attestation is None:
            self._fail_closed(AttestationError("child is not attested"))
        pending = (
            self._pending.get(turn.sequence)
            if isinstance(turn, QueuedTurn)
            else None
        )
        if pending is None or pending[0] is not turn:
            self._fail_closed(AttestationError("queued turn handle is unknown"))
        self.revalidate()
        _, serialized = self._pending.pop(turn.sequence)
        try:
            receipt = self._relay(serialized)
        except Exception:
            self._fail_closed(AttestationError("turn relay failed"))
        if not isinstance(receipt, RelayReceipt):
            self._fail_closed(AttestationError("turn relay receipt is invalid"))
        self._relayed_model_bytes += receipt.model_bytes
        self._relayed_network_bytes += receipt.network_bytes
