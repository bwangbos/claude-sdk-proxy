"""Fail-closed child provenance verdict and Task 5 SDK transport boundary."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
import threading
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from subprocess import DEVNULL, PIPE
from typing import Any, Never

import anyio
from anyio.streams.text import TextReceiveStream, TextSendStream
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    SystemMessage,
    Transport,
)

from claude_sdk_proxy.environment import environment_fingerprint
from claude_sdk_proxy.isolation import IsolationConfig, build_agent_options

ATTESTATION_MANIFEST_SCHEMA = "claude_sdk_proxy.child_attestation_manifest"
EXPECTED_SDK_VERSION = "0.2.148"
EXPECTED_CLI_VERSION = "2.1.251"
BOOTSTRAP_DESCRIPTOR_NAMES = (
    "LOCAL_PROXY_ALLOCATION_NONCE",
    "LOCAL_PROXY_INSTANCE_DIR",
    "LOCAL_PROXY_REAL_CLAUDE",
    "LOCAL_PROXY_CONTROL_FD",
)
SUPERVISOR_INTERNAL_RELAY_FD = 198
_NETWORK_PROXY_NAMES = frozenset(
    {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"}
)
_AMBIENT_NETWORK_PROXY_SELECTOR = "LOCAL_PROXY_NETWORK_PROXY"
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
_TEST_ONLY_TOKEN = object()
_CHILD_TOKEN = object()
_PERMIT_TOKEN = object()


class AttestationError(RuntimeError):
    """Raised whenever public child provenance is absent or ambiguous."""


class AttestationReasonCode(StrEnum):
    PUBLIC_AUTH_PROVENANCE_ABSENT_OR_UNVALIDATED = (
        "public_auth_provenance_absent_or_unvalidated"
    )
    PREINPUT_NETWORK_BOUNDARY_UNPROVED = "preinput_network_boundary_unproved"
    PER_TURN_FRESH_PROVENANCE_UNAVAILABLE = "per_turn_fresh_provenance_unavailable"


_CURRENT_REASONS = (
    AttestationReasonCode.PUBLIC_AUTH_PROVENANCE_ABSENT_OR_UNVALIDATED,
    AttestationReasonCode.PREINPUT_NETWORK_BOUNDARY_UNPROVED,
    AttestationReasonCode.PER_TURN_FRESH_PROVENANCE_UNAVAILABLE,
)


@dataclass(frozen=True)
class AttestationAvailability:
    core_gate_available: bool
    reason_codes: tuple[AttestationReasonCode, ...]
    _token: object | None = None

    def __post_init__(self) -> None:
        if self._token is _TEST_ONLY_TOKEN:
            if not self.core_gate_available or self.reason_codes:
                raise AttestationError("test-only availability must be affirmative")
        elif self.core_gate_available or self.reason_codes != _CURRENT_REASONS:
            raise AttestationError("production availability must remain false")


def current_attestation_availability() -> AttestationAvailability:
    return AttestationAvailability(False, _CURRENT_REASONS)


def _test_only_available_verdict() -> AttestationAvailability:
    return AttestationAvailability(True, (), _TEST_ONLY_TOKEN)


def _require_sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise AttestationError(f"{label} must be a lowercase SHA-256 value")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AttestationError(f"{label} must be a positive integer")
    return value


def _digest(domain: bytes, value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(domain + b"\0" + encoded).hexdigest()


@dataclass(frozen=True)
class CliExecutableIdentity:
    version: str
    path_sha256: str
    st_dev: int
    st_ino: int
    mode: int
    sha256: str

    def __post_init__(self) -> None:
        if self.version != EXPECTED_CLI_VERSION:
            raise AttestationError("CLI identity version is not pinned to 2.1.251")
        _require_sha256(self.path_sha256, "CLI path hash")
        for value, label in (
            (self.st_dev, "CLI device"),
            (self.st_ino, "CLI inode"),
            (self.mode, "CLI mode"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AttestationError(f"{label} must be a nonnegative integer")
        if not stat.S_ISREG(self.mode) or not self.mode & stat.S_IXUSR:
            raise AttestationError("CLI mode must identify an executable regular file")
        _require_sha256(self.sha256, "CLI executable hash")

    def digest(self) -> str:
        return _digest(b"claude-sdk-proxy:cli-identity:v1", vars(self))


@dataclass(frozen=True)
class FullSupervisorEvidence:
    control_trace: tuple[str, ...]
    canonical_control_types: tuple[str, ...]
    canonical_control_sequences: tuple[int, ...]
    canonical_control_hashes: tuple[str, ...]
    supervisor_identity_sha256: str
    anchor_identity_sha256: str
    cli_armed_identity_sha256: str
    cli_running_identity_sha256: str
    canonical_head_certified: bool
    same_canonical_journal: bool
    evidence_observed_not_inferred: bool
    ack_after_durable_certification: bool
    exact_canonical_ack_heads: bool
    post_exec_identity_verified: bool
    cli_control_fd_closed_on_exec: bool
    external_control_fd_closed_on_cli_exec: bool
    internal_control_fd_closed_on_cli_exec: bool
    bootstrap_environment_removed: bool
    bootstrap_descriptor_names: tuple[str, ...]
    private_internal_relay_fd: bool
    network_proxy_selector_authenticated: bool
    identity_ack_config_version: int
    identity_ack_bound_to_certified_head: bool
    identity_ack_reserved_zero: bool
    shared_proxy_fallback_channel: bool
    external_control_read_by_supervisor_while_live: bool
    anchor_external_read_count_while_supervisor_live: int
    network_proxy_enabled: bool

    def __post_init__(self) -> None:
        if self.control_trace != _EXPECTED_TRACE:
            raise AttestationError("full Task 5 control trace changed")
        if self.canonical_control_types != _EXPECTED_CANONICAL_TYPES:
            raise AttestationError("full Task 5 canonical types changed")
        if self.canonical_control_sequences != (1, 2, 3, 4):
            raise AttestationError("full Task 5 canonical sequences changed")
        if len(self.canonical_control_hashes) != 4:
            raise AttestationError("full Task 5 canonical hashes are incomplete")
        for value in (
            *self.canonical_control_hashes,
            self.supervisor_identity_sha256,
            self.anchor_identity_sha256,
            self.cli_armed_identity_sha256,
            self.cli_running_identity_sha256,
        ):
            _require_sha256(value, "Task 5 evidence hash")
        flags = (
            self.canonical_head_certified,
            self.same_canonical_journal,
            self.evidence_observed_not_inferred,
            self.ack_after_durable_certification,
            self.exact_canonical_ack_heads,
            self.post_exec_identity_verified,
            self.cli_control_fd_closed_on_exec,
            self.external_control_fd_closed_on_cli_exec,
            self.internal_control_fd_closed_on_cli_exec,
            self.bootstrap_environment_removed,
            self.private_internal_relay_fd,
            self.network_proxy_selector_authenticated,
            self.identity_ack_bound_to_certified_head,
            self.identity_ack_reserved_zero,
            self.shared_proxy_fallback_channel,
            self.external_control_read_by_supervisor_while_live,
        )
        if any(not isinstance(flag, bool) for flag in flags) or not all(flags):
            raise AttestationError("full Task 5 evidence is not affirmative")
        if self.bootstrap_descriptor_names != BOOTSTRAP_DESCRIPTOR_NAMES:
            raise AttestationError("Task 5 bootstrap descriptor set changed")
        if self.identity_ack_config_version != 1:
            raise AttestationError("Task 5 IDENTITY_ACK config version changed")
        if self.anchor_external_read_count_while_supervisor_live != 0:
            raise AttestationError("anchor raced the supervisor control reader")
        if not isinstance(self.network_proxy_enabled, bool):
            raise AttestationError("Task 5 network selector is malformed")


@dataclass(frozen=True)
class AttestationManifest:
    schema: str
    version: int
    sdk_version: str
    cli_version: str
    cli_executable: CliExecutableIdentity
    environment_fingerprint: str
    supervisor_evidence: FullSupervisorEvidence
    availability: AttestationAvailability

    def __post_init__(self) -> None:
        if self.schema != ATTESTATION_MANIFEST_SCHEMA:
            raise AttestationError("unsupported attestation manifest schema")
        if isinstance(self.version, bool) or self.version != 1:
            raise AttestationError("unsupported attestation manifest version")
        if (
            self.sdk_version != EXPECTED_SDK_VERSION
            or self.cli_version != EXPECTED_CLI_VERSION
        ):
            raise AttestationError("manifest runtime tuple is not pinned")
        if self.cli_executable.version != self.cli_version:
            raise AttestationError("manifest CLI identity version changed")
        _require_sha256(self.environment_fingerprint, "environment fingerprint")
        if self.availability != current_attestation_availability():
            raise AttestationError(
                "production manifest must preserve false availability"
            )


@dataclass(frozen=True)
class AttestationBinding:
    allocation_nonce: str
    child_identity_sha256: str
    supervisor_certified_sequence: int
    supervisor_certified_hash: str
    connection_nonce: str

    def __post_init__(self) -> None:
        _require_sha256(self.allocation_nonce, "allocation nonce")
        _require_sha256(self.child_identity_sha256, "child identity")
        _positive_int(self.supervisor_certified_sequence, "supervisor sequence")
        _require_sha256(self.supervisor_certified_hash, "supervisor hash")
        _require_sha256(self.connection_nonce, "connection nonce")

    def digest(self) -> str:
        return _digest(b"claude-sdk-proxy:attestation-binding:v1", vars(self))


class ChildAttestation:
    __slots__ = ("_binding_sha256", "_evidence_sequence", "_evidence_sha256")

    _binding_sha256: str
    _evidence_sequence: int
    _evidence_sha256: str

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("ChildAttestation cannot be constructed publicly")

    @classmethod
    def _create(
        cls, token: object, binding: str, sequence: int, evidence: str
    ) -> ChildAttestation:
        if token is not _CHILD_TOKEN:
            raise AttestationError("child attestation mint authority is invalid")
        value = object.__new__(cls)
        value._binding_sha256 = _require_sha256(binding, "binding hash")
        value._evidence_sequence = _positive_int(sequence, "evidence sequence")
        value._evidence_sha256 = _require_sha256(evidence, "evidence hash")
        return value

    @property
    def binding_sha256(self) -> str:
        return self._binding_sha256

    @property
    def evidence_sequence(self) -> int:
        return self._evidence_sequence

    @property
    def evidence_sha256(self) -> str:
        return self._evidence_sha256


class _AttestationPermit:
    __slots__ = ("binding_sha256", "evidence_sequence", "evidence_sha256", "identity")

    def __init__(
        self, token: object, binding: str, sequence: int, evidence: str
    ) -> None:
        if token is not _PERMIT_TOKEN:
            raise AttestationError("permit constructor is private")
        self.binding_sha256 = binding
        self.evidence_sequence = sequence
        self.evidence_sha256 = evidence
        self.identity = hashlib.sha256(os.urandom(32)).hexdigest()


_CONSUMED: set[str] = set()
_CONSUMED_LOCK = threading.Lock()


def _test_only_mint_permit(
    binding: AttestationBinding, evidence_sequence: int, event: SystemMessage
) -> _AttestationPermit:
    sequence = _positive_int(evidence_sequence, "evidence sequence")
    evidence = _digest(b"claude-sdk-proxy:research-evidence:v1", event.data)
    return _AttestationPermit(_PERMIT_TOKEN, binding.digest(), sequence, evidence)


def _test_only_extract_synthetic_attestation(
    event: SystemMessage, permit: object
) -> ChildAttestation:
    if not isinstance(permit, _AttestationPermit):
        raise AttestationError("research permit is invalid")
    expected = {
        "schema": "claude_sdk_proxy.research_attestation",
        "version": 1,
        "auth_source": "existing_claude_login",
        "provider": "anthropic",
        "endpoint": "default",
        "preinput_model_bytes": 0,
        "preinput_network_bytes": 0,
    }
    if event.subtype != "research_attestation" or event.data != expected:
        raise AttestationError("synthetic research evidence is not exact")
    digest = _digest(b"claude-sdk-proxy:research-evidence:v1", event.data)
    if digest != permit.evidence_sha256:
        raise AttestationError("research evidence does not match permit")
    return ChildAttestation._create(
        _CHILD_TOKEN, permit.binding_sha256, permit.evidence_sequence, digest
    )


def extract_child_attestation(
    event: SystemMessage, availability: AttestationAvailability
) -> Never:
    if (
        not isinstance(event, SystemMessage)
        or event.subtype != "init"
        or not isinstance(event.data, Mapping)
        or event.data.get("type") != "system"
        or event.data.get("subtype") != "init"
    ):
        raise AttestationError("child evidence is not a raw SDK init SystemMessage")
    if not isinstance(availability, AttestationAvailability):
        raise AttestationError("attestation availability verdict is invalid")
    raise AttestationError(
        "public auth provenance is absent or unvalidated; pre-input network boundary "
        "is unproved; per-turn fresh provenance is unavailable"
    )


@dataclass(frozen=True)
class ControlFDIdentity:
    st_dev: int
    st_ino: int
    mode: int

    def __post_init__(self) -> None:
        if not stat.S_ISSOCK(self.mode):
            raise AttestationError("control FD must be an open socket")

    @classmethod
    def from_fd(cls, descriptor: int) -> ControlFDIdentity:
        if isinstance(descriptor, bool) or not isinstance(descriptor, int):
            raise AttestationError("control FD must be an integer")
        try:
            metadata = os.fstat(descriptor)
        except OSError as error:
            raise AttestationError("control FD is not open") from error
        return cls(metadata.st_dev, metadata.st_ino, metadata.st_mode)


@dataclass(frozen=True)
class SupervisorBootstrapDescriptors:
    allocation_nonce: str
    instance_dir: Path
    real_cli: Path
    control_fd: int
    control_identity: ControlFDIdentity

    def __post_init__(self) -> None:
        _require_sha256(self.allocation_nonce, "allocation nonce")
        if (
            not isinstance(self.instance_dir, Path)
            or not self.instance_dir.is_absolute()
        ):
            raise AttestationError("instance directory must be absolute")
        if not isinstance(self.real_cli, Path) or not self.real_cli.is_absolute():
            raise AttestationError("real CLI path must be absolute")
        if isinstance(self.control_fd, bool) or not isinstance(self.control_fd, int):
            raise AttestationError("control FD must be an integer")
        if not isinstance(self.control_identity, ControlFDIdentity):
            raise AttestationError("control FD identity is required")

    def environment(self) -> dict[str, str]:
        return {
            "LOCAL_PROXY_ALLOCATION_NONCE": self.allocation_nonce,
            "LOCAL_PROXY_INSTANCE_DIR": str(self.instance_dir),
            "LOCAL_PROXY_REAL_CLAUDE": str(self.real_cli),
            "LOCAL_PROXY_CONTROL_FD": str(self.control_fd),
        }


@dataclass(frozen=True)
class SupervisorIdentityAckConfig:
    network_proxy_enabled: bool
    certified_sequence: int
    certified_hash: bytes
    version: int = 1
    reserved_byte: int = 0
    reserved_word: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.network_proxy_enabled, bool):
            raise AttestationError("network proxy selector must be a boolean")
        sequence = _positive_int(self.certified_sequence, "certified sequence")
        if sequence >= 1 << 64:
            raise AttestationError("certified sequence exceeds uint64")
        if (
            not isinstance(self.certified_hash, bytes)
            or len(self.certified_hash) != 32
            or self.certified_hash == bytes(32)
        ):
            raise AttestationError("certified hash must be nonzero bytes32")
        if isinstance(self.version, bool) or self.version != 1:
            raise AttestationError("IDENTITY_ACK config version changed")
        if (
            isinstance(self.reserved_byte, bool)
            or self.reserved_byte != 0
            or isinstance(self.reserved_word, bool)
            or self.reserved_word != 0
        ):
            raise AttestationError("IDENTITY_ACK reserved fields changed")

    def payload(self) -> bytes:
        return struct.pack(
            "<HBBIQ32s",
            self.version,
            int(self.network_proxy_enabled),
            self.reserved_byte,
            self.reserved_word,
            self.certified_sequence,
            self.certified_hash,
        )


def _measure_cli(path: Path) -> CliExecutableIdentity:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise AttestationError("CLI executable is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        hasher = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            hasher.update(chunk)
    finally:
        os.close(descriptor)
    return CliExecutableIdentity(
        version=EXPECTED_CLI_VERSION,
        path_sha256=hashlib.sha256(str(path.resolve()).encode()).hexdigest(),
        st_dev=metadata.st_dev,
        st_ino=metadata.st_ino,
        mode=metadata.st_mode,
        sha256=hasher.hexdigest(),
    )


def _require_executable(path: Path, label: str) -> None:
    try:
        metadata = path.stat()
    except OSError as error:
        raise AttestationError(f"{label} is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode) or not metadata.st_mode & stat.S_IXUSR:
        raise AttestationError(f"{label} must be an executable regular file")


def _cli_arguments(options: ClaudeAgentOptions) -> tuple[str, ...]:
    if not isinstance(options.system_prompt, str):
        raise AttestationError("Task 6 requires one plain system prompt")
    if not isinstance(options.tools, list) or options.tools:
        raise AttestationError("Task 6 requires an empty built-in tool list")
    if options.mcp_servers != {}:
        raise AttestationError("Task 6 deterministic transport requires no MCP servers")
    if not options.model:
        raise AttestationError("Task 6 requires an exact model ID")
    return (
        "--output-format",
        "stream-json",
        "--verbose",
        "--system-prompt",
        options.system_prompt,
        "--tools",
        "",
        "--model",
        options.model,
        "--include-partial-messages",
        "--strict-mcp-config",
        "--setting-sources=",
        "--input-format",
        "stream-json",
    )


@dataclass(frozen=True)
class PreparedSupervisorLaunch:
    options: ClaudeAgentOptions
    command: tuple[str, ...]
    _supervisor_environment_items: tuple[tuple[str, str], ...]
    inherited_fds: tuple[int, ...]
    control_identity: ControlFDIdentity
    bootstrap_descriptor_names: tuple[str, ...]
    identity_ack_config: SupervisorIdentityAckConfig
    identity_ack_payload: bytes
    cli_identity: CliExecutableIdentity
    full_supervisor_evidence: FullSupervisorEvidence
    availability: AttestationAvailability
    network_proxy_items: tuple[tuple[str, str], ...]
    supervisor_internal_relay_fd: int = SUPERVISOR_INTERNAL_RELAY_FD

    @property
    def supervisor_environment(self) -> Mapping[str, str]:
        return dict(self._supervisor_environment_items)


def prepare_supervisor_launch(
    config: IsolationConfig,
    descriptors: SupervisorBootstrapDescriptors,
    identity_ack_config: SupervisorIdentityAckConfig,
    manifest: AttestationManifest,
) -> PreparedSupervisorLaunch:
    _require_executable(config.supervisor_path, "verified supervisor")
    measured_cli = _measure_cli(descriptors.real_cli)
    if measured_cli != manifest.cli_executable:
        raise AttestationError("CLI executable does not match manifest identity")
    if descriptors.control_identity != ControlFDIdentity.from_fd(
        descriptors.control_fd
    ):
        raise AttestationError("control FD identity changed before launch")
    evidence = manifest.supervisor_evidence
    if identity_ack_config.network_proxy_enabled != evidence.network_proxy_enabled:
        raise AttestationError("network proxy selector disagrees with Task 5 evidence")
    if (
        identity_ack_config.certified_sequence
        != evidence.canonical_control_sequences[-1]
    ):
        raise AttestationError("IDENTITY_ACK sequence disagrees with Task 5 head")
    if (
        identity_ack_config.certified_hash.hex()
        != evidence.canonical_control_hashes[-1]
    ):
        raise AttestationError("IDENTITY_ACK hash disagrees with Task 5 head")
    if environment_fingerprint(config.environment) != manifest.environment_fingerprint:
        raise AttestationError("effective environment fingerprint changed")
    proxy_items = tuple(
        sorted(
            (name, value)
            for name, value in config.environment.items()
            if name in _NETWORK_PROXY_NAMES
        )
    )
    if identity_ack_config.network_proxy_enabled != bool(proxy_items):
        raise AttestationError(
            "network proxy bit disagrees with effective proxy values"
        )
    forbidden = set(BOOTSTRAP_DESCRIPTOR_NAMES) | {_AMBIENT_NETWORK_PROXY_SELECTOR}
    if set(config.environment) & forbidden:
        raise AttestationError("real CLI environment contains ambient bootstrap state")
    options = build_agent_options(config)
    supervisor_environment = {**dict(config.environment), **descriptors.environment()}
    command = (str(config.supervisor_path), *_cli_arguments(options))
    return PreparedSupervisorLaunch(
        options=replace(options, env=supervisor_environment),
        command=command,
        _supervisor_environment_items=tuple(sorted(supervisor_environment.items())),
        inherited_fds=(descriptors.control_fd,),
        control_identity=descriptors.control_identity,
        bootstrap_descriptor_names=BOOTSTRAP_DESCRIPTOR_NAMES,
        identity_ack_config=identity_ack_config,
        identity_ack_payload=identity_ack_config.payload(),
        cli_identity=measured_cli,
        full_supervisor_evidence=evidence,
        availability=manifest.availability,
        network_proxy_items=proxy_items,
    )


class AttestedSupervisorTransport(Transport):
    """SDK transport using exact environment, pass_fds, and write classification."""

    def __init__(self, launch: PreparedSupervisorLaunch) -> None:
        if not isinstance(launch, PreparedSupervisorLaunch):
            raise AttestationError("validated supervisor launch is required")
        self.launch = launch
        self._process: Any | None = None
        self._stdin: TextSendStream | None = None
        self._stdout: TextReceiveStream | None = None
        self._lock = anyio.Lock()
        self._ready = False
        self._closed = False
        self._initialize_seen = False
        self._buffered: list[str] = []
        self._discarded = 0

    @property
    def buffered_user_write_count(self) -> int:
        return len(self._buffered)

    @property
    def discarded_user_write_count(self) -> int:
        return self._discarded

    async def connect(self) -> None:
        async with self._lock:
            if self._closed:
                raise AttestationError("transport is closed")
            if self._process is not None:
                return
            if self.launch.control_identity != ControlFDIdentity.from_fd(
                self.launch.inherited_fds[0]
            ):
                raise AttestationError("control FD identity changed before spawn")
            self._process = await anyio.open_process(
                self.launch.command,
                stdin=PIPE,
                stdout=PIPE,
                stderr=DEVNULL,
                cwd=self.launch.options.cwd,
                env=self.launch.supervisor_environment,
                pass_fds=self.launch.inherited_fds,
            )
            if self._process.stdin is None or self._process.stdout is None:
                await self._close_locked()
                raise AttestationError("supervisor pipes are unavailable")
            self._stdin = TextSendStream(self._process.stdin)
            self._stdout = TextReceiveStream(self._process.stdout)
            self._ready = True

    @staticmethod
    def _is_initialize(data: str) -> bool:
        try:
            lines = data.splitlines()
            if len(lines) != 1:
                return False
            value = json.loads(lines[0])
        except TypeError, ValueError:
            return False
        if (
            not isinstance(value, dict)
            or set(value) != {"type", "request_id", "request"}
            or value.get("type") != "control_request"
            or not isinstance(value.get("request_id"), str)
            or not isinstance(value.get("request"), dict)
        ):
            return False
        request = value["request"]
        return (
            set(request) in ({"subtype", "hooks"}, {"subtype", "hooks", "skills"})
            and request.get("subtype") == "initialize"
            and request.get("hooks") is None
            and ("skills" not in request or request.get("skills") == [])
        )

    async def write(self, data: str) -> None:
        if not isinstance(data, str):
            raise AttestationError("transport write must be text")
        async with self._lock:
            if not self._ready or self._stdin is None or self._closed:
                raise AttestationError("transport is not ready")
            if not self._initialize_seen and self._is_initialize(data):
                self._initialize_seen = True
                await self._stdin.send(data)
            else:
                self._buffered.append(str(data))

    async def release_buffered(self, permit: object) -> None:
        del permit
        async with self._lock:
            if not self.launch.availability.core_gate_available:
                await self._close_locked()
                raise AttestationError("core attestation gate unavailable")
            raise AttestationError("production permit release is not implemented")

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        if self._stdout is None:
            raise AttestationError("transport is not connected")
        buffer = ""
        async for chunk in self._stdout:
            buffer += chunk
            if len(buffer.encode()) > 1024 * 1024:
                raise AttestationError("supervisor output exceeded transport bound")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise AttestationError("supervisor message must be an object")
                yield value
        if buffer.strip():
            value = json.loads(buffer)
            if not isinstance(value, dict):
                raise AttestationError("supervisor message must be an object")
            yield value

    def is_ready(self) -> bool:
        return self._ready and not self._closed

    async def end_input(self) -> None:
        async with self._lock:
            if self._stdin is not None:
                await self._stdin.aclose()
                self._stdin = None

    async def _close_locked(self) -> None:
        self._closed = True
        self._ready = False
        self._discarded += len(self._buffered)
        self._buffered.clear()
        if self._stdin is not None:
            await self._stdin.aclose()
            self._stdin = None
        process = self._process
        self._process = None
        if process is None:
            return
        if process.returncode is None:
            with anyio.move_on_after(0.25):
                await process.wait()
        if process.returncode is None:
            process.terminate()
            with anyio.move_on_after(2):
                await process.wait()
        if process.returncode is None:
            process.kill()
            with anyio.move_on_after(2):
                await process.wait()
        await process.aclose()

    async def close(self) -> None:
        async with self._lock:
            if self._closed and self._process is None:
                return
            await self._close_locked()


def build_attested_sdk_client(
    launch: PreparedSupervisorLaunch,
    *,
    transport: AttestedSupervisorTransport | None = None,
) -> ClaudeSDKClient:
    if transport is None or not isinstance(transport, AttestedSupervisorTransport):
        raise AttestationError("custom Task 6 transport is mandatory")
    if transport.launch is not launch:
        raise AttestationError("custom Task 6 transport launch identity changed")
    return ClaudeSDKClient(options=launch.options, transport=transport)


@dataclass(frozen=True)
class RelayReceipt:
    model_bytes: int
    network_bytes: int

    def __post_init__(self) -> None:
        for value in (self.model_bytes, self.network_bytes):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AttestationError("relay byte counts must be nonnegative integers")


@dataclass(frozen=True)
class QueuedTurn:
    sequence: int

    def __post_init__(self) -> None:
        _positive_int(self.sequence, "queued turn sequence")


class _GateState(StrEnum):
    OPEN = "open"
    REVALIDATING = "revalidating"
    RELEASING = "releasing"
    CLOSED = "closed"


class AttestationGate:
    """Locked fail-closed per-turn state machine with exact permit binding."""

    def __init__(
        self,
        *,
        availability: AttestationAvailability,
        binding: AttestationBinding,
        revalidate: Callable[[], tuple[ChildAttestation, object]],
        relay: Callable[[bytes], RelayReceipt],
        close_child: Callable[[], None],
    ) -> None:
        self._availability = availability
        self._binding = binding
        self._revalidate_callback = revalidate
        self._relay = relay
        self._close_child = close_child
        self._lock = threading.RLock()
        self._state = _GateState.OPEN
        self._revoked = False
        self._close_called = False
        self._attestation: ChildAttestation | None = None
        self._last_evidence_sequence = 0
        self._pending: dict[int, tuple[QueuedTurn, bytes]] = {}
        self._next_turn_sequence = 1
        self._relayed_model_bytes = 0
        self._relayed_network_bytes = 0

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._state is _GateState.CLOSED

    @property
    def revoked(self) -> bool:
        with self._lock:
            return self._revoked

    @property
    def pending_turn_count(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def queued_content_bytes(self) -> int:
        with self._lock:
            return sum(len(payload) for _, payload in self._pending.values())

    @property
    def last_evidence_sequence(self) -> int:
        with self._lock:
            return self._last_evidence_sequence

    @property
    def relayed_model_bytes(self) -> int:
        with self._lock:
            return self._relayed_model_bytes

    @property
    def relayed_network_bytes(self) -> int:
        with self._lock:
            return self._relayed_network_bytes

    def _fail_closed(self, error: AttestationError) -> Never:
        should_close = False
        with self._lock:
            self._state = _GateState.CLOSED
            self._revoked = True
            self._attestation = None
            self._pending.clear()
            if not self._close_called:
                self._close_called = True
                should_close = True
        if should_close:
            try:
                self._close_child()
            except Exception as close_error:
                raise AttestationError(f"{error}; child close failed") from close_error
        raise error

    def _require_open(self) -> None:
        if self._state is not _GateState.OPEN:
            self._fail_closed(AttestationError("attestation gate is not open"))

    def _consume(
        self, attestation: ChildAttestation, permit: object, expected_sequence: int
    ) -> None:
        if not isinstance(attestation, ChildAttestation) or not isinstance(
            permit, _AttestationPermit
        ):
            raise AttestationError("attestation permit is invalid")
        if permit.binding_sha256 != self._binding.digest():
            raise AttestationError("attestation permit binding changed")
        if attestation.binding_sha256 != permit.binding_sha256:
            raise AttestationError("attestation binding does not match permit")
        if permit.evidence_sequence != expected_sequence:
            raise AttestationError("evidence sequence is stale or nonmonotonic")
        if attestation.evidence_sequence != permit.evidence_sequence:
            raise AttestationError("attestation evidence sequence changed")
        if attestation.evidence_sha256 != permit.evidence_sha256:
            raise AttestationError("attestation evidence hash changed")
        with _CONSUMED_LOCK:
            if permit.identity in _CONSUMED:
                raise AttestationError("attestation permit replay detected")
            _CONSUMED.add(permit.identity)

    def attest(self, attestation: ChildAttestation, permit: object) -> None:
        with self._lock:
            self._require_open()
            if not self._availability.core_gate_available:
                self._fail_closed(AttestationError("core attestation gate unavailable"))
            if self._attestation is not None:
                self._fail_closed(AttestationError("duplicate child attestation"))
            try:
                self._consume(attestation, permit, 1)
            except AttestationError as error:
                self._fail_closed(error)
            self._attestation = attestation
            self._last_evidence_sequence = 1

    def queue_turn(
        self,
        *,
        system: str,
        blocks: Sequence[Mapping[str, object]],
        mcp_schema: Mapping[str, object] | None = None,
    ) -> QueuedTurn:
        with self._lock:
            self._require_open()
            try:
                payload = json.dumps(
                    {
                        "system": system,
                        "blocks": list(blocks),
                        "mcp_schema": dict(mcp_schema)
                        if mcp_schema is not None
                        else None,
                    },
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            except TypeError, ValueError:
                self._fail_closed(AttestationError("turn is not immutable JSON"))
            turn = QueuedTurn(self._next_turn_sequence)
            self._next_turn_sequence += 1
            self._pending[turn.sequence] = (turn, payload)
            return turn

    def revalidate(self) -> None:
        with self._lock:
            self._require_open()
            if not self._availability.core_gate_available:
                self._fail_closed(AttestationError("core attestation gate unavailable"))
            if self._attestation is None:
                self._fail_closed(AttestationError("child is not attested"))
            self._state = _GateState.REVALIDATING
            expected = self._last_evidence_sequence + 1
        try:
            attestation, permit = self._revalidate_callback()
            self._consume(attestation, permit, expected)
        except AttestationError as error:
            self._fail_closed(error)
        except Exception:
            self._fail_closed(AttestationError("public child revalidation failed"))
        with self._lock:
            if self._state is not _GateState.REVALIDATING:
                self._fail_closed(AttestationError("revalidation state changed"))
            self._attestation = attestation
            self._last_evidence_sequence = expected
            self._state = _GateState.OPEN

    def release(self, turn: QueuedTurn) -> None:
        with self._lock:
            self._require_open()
            if not self._availability.core_gate_available:
                self._fail_closed(AttestationError("core attestation gate unavailable"))
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
        with self._lock:
            self._require_open()
            pending = self._pending.pop(turn.sequence, None)
            if pending is None or pending[0] is not turn:
                self._fail_closed(AttestationError("queued turn handle changed"))
            payload = pending[1]
            self._state = _GateState.RELEASING
        try:
            receipt = self._relay(payload)
        except Exception:
            self._fail_closed(AttestationError("turn relay failed"))
        if not isinstance(receipt, RelayReceipt):
            self._fail_closed(AttestationError("turn relay receipt is invalid"))
        with self._lock:
            if self._state is not _GateState.RELEASING:
                self._fail_closed(AttestationError("release state changed"))
            self._relayed_model_bytes += receipt.model_bytes
            self._relayed_network_bytes += receipt.network_bytes
            self._state = _GateState.OPEN

    def close(self) -> None:
        should_close = False
        with self._lock:
            self._state = _GateState.CLOSED
            self._revoked = True
            self._attestation = None
            self._pending.clear()
            if not self._close_called:
                self._close_called = True
                should_close = True
        if should_close:
            self._close_child()


__all__ = [
    "ATTESTATION_MANIFEST_SCHEMA",
    "AttestationAvailability",
    "AttestationBinding",
    "AttestationError",
    "AttestationGate",
    "AttestationManifest",
    "AttestationReasonCode",
    "AttestedSupervisorTransport",
    "ChildAttestation",
    "CliExecutableIdentity",
    "ControlFDIdentity",
    "FullSupervisorEvidence",
    "PreparedSupervisorLaunch",
    "QueuedTurn",
    "RelayReceipt",
    "SupervisorBootstrapDescriptors",
    "SupervisorIdentityAckConfig",
    "build_attested_sdk_client",
    "current_attestation_availability",
    "extract_child_attestation",
    "prepare_supervisor_launch",
]
