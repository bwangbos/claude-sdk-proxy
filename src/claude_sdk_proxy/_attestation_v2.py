"""Fail-closed child provenance verdict and Task 5 SDK transport boundary."""

from __future__ import annotations

import codecs
import ctypes
import hashlib
import json
import os
import re
import selectors
import signal
import socket
import stat
import struct
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from subprocess import DEVNULL, PIPE
from types import MappingProxyType
from typing import IO, Any, Literal, Never, SupportsIndex

import anyio
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    SystemMessage,
    Transport,
)

from claude_sdk_proxy.environment import (
    EnvironmentConfig,
    build_child_environment,
    environment_fingerprint,
)
from claude_sdk_proxy.isolation import IsolationConfig, build_agent_options
from claude_sdk_proxy.journal import BootstrapHead, Journal, UnconfirmedReason

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

_JOURNAL_NAME = "allocation.journal"
_WORKDIR_NAME = "allocation.workdir"
_NORMAL_LIMIT = 32 * 1024
_HARD_LIMIT = _NORMAL_LIMIT + 1172 * 14
_CONTROL_MAGIC = 0x464C5043
_CONTROL_VERSION = 1
_CONTROL_MAX_PAYLOAD = 4096
_CONTROL_HEADER_SIZE = 44
_CONTROL_CHECKSUM_SIZE = 4
_PROCESS_IDENTITY_SIZE = 112
_CLI_ARMED_IDENTITY_SIZE = 192
_SUPERVISOR_CONFIG_FORMAT = "<HBBIQ32s"
_SUPERVISOR_CONFIG_VERSION = 1
_INITIALIZE_MAX_BYTES = 4096
_REQUEST_ID = re.compile(r"req_[1-9][0-9]*_[0-9a-f]{8}\Z")
_VERSION_STDOUT = b"2.1.251 (Claude Code)\n"
_VERSION_OUTPUT_LIMIT = 256
_VERSION_PROBE_TIMEOUT_SECONDS = 5.0
_VERSION_PROBE_TERM_SECONDS = 0.25
_VERSION_PROBE_KILL_SECONDS = 2.0
_CONTROL_NAMES = {
    1: "SUPERVISOR_IDENTITY",
    2: "IDENTITY_ACK",
    3: "ANCHOR_IDENTITY",
    4: "ANCHOR_ACK",
    5: "CLI_ARMED",
    6: "ARMED_ACK",
    7: "CLI_RUNNING",
    8: "CLEANUP_REQUEST",
    9: "SELF_TERM_REQUEST",
    10: "CLEANUP_RESULT",
    11: "CONTROL_ERROR",
    12: "CLEANUP_ACK",
}
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
_LAUNCH_TOKEN = object()
_RECEIPT_TOKEN = object()
_TURN_TOKEN = object()


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

    def __post_init__(self) -> None:
        if self.core_gate_available or self.reason_codes != _CURRENT_REASONS:
            raise AttestationError("production availability must remain false")


_CURRENT_AVAILABILITY = AttestationAvailability(False, _CURRENT_REASONS)


def current_attestation_availability() -> AttestationAvailability:
    return _CURRENT_AVAILABILITY


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
    ).encode("utf-8")
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
        return _digest(
            b"claude-sdk-proxy:cli-identity:v1",
            {
                "version": self.version,
                "path_sha256": self.path_sha256,
                "st_dev": self.st_dev,
                "st_ino": self.st_ino,
                "mode": self.mode,
                "sha256": self.sha256,
            },
        )


@dataclass(frozen=True)
class AttestationManifest:
    schema: str
    version: int
    sdk_version: str
    cli_version: str
    cli_executable: CliExecutableIdentity
    environment_fingerprint: str
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
        if self.availability is not _CURRENT_AVAILABILITY:
            raise AttestationError("manifest must preserve current false availability")


class ChildAttestation:
    """Opaque future evidence type with no production minting path in this tuple."""

    __slots__ = ()

    def __new__(cls, *_args: object, **_kwargs: object) -> ChildAttestation:
        raise TypeError("ChildAttestation cannot be constructed publicly")

    def __copy__(self) -> Never:
        raise AttestationError("child attestation cannot be copied")

    def __deepcopy__(self, _memo: object) -> Never:
        raise AttestationError("child attestation cannot be copied")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> Never:
        raise AttestationError("child attestation cannot be pickled")


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
    if availability is not _CURRENT_AVAILABILITY:
        raise AttestationError("attestation availability verdict is invalid")
    raise AttestationError(
        "public auth provenance is absent or unvalidated; pre-input network boundary "
        "is unproved; per-turn fresh provenance is unavailable"
    )


@dataclass(frozen=True)
class SupervisorBootstrapDescriptors:
    allocation_nonce: str
    instance_dir: Path
    real_cli: Path
    journal: Journal

    def __post_init__(self) -> None:
        _require_sha256(self.allocation_nonce, "allocation nonce")
        if not self.instance_dir.is_absolute() or not self.real_cli.is_absolute():
            raise AttestationError("bootstrap paths must be absolute")
        if not isinstance(self.journal, Journal):
            raise AttestationError("authoritative Task 4 journal handle is required")


def _snapshot_cli(path: Path) -> CliExecutableIdentity:
    if not path.is_absolute():
        raise AttestationError("CLI executable path must be absolute")
    try:
        metadata = os.lstat(path)
    except OSError as error:
        raise AttestationError("CLI executable is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise AttestationError("CLI executable cannot be a symlink")
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise AttestationError(
            "CLI executable cannot be opened without following"
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
        ) != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
        ):
            raise AttestationError("CLI changed during measurement")
        hasher = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            hasher.update(chunk)
    finally:
        os.close(descriptor)
    return CliExecutableIdentity(
        version=EXPECTED_CLI_VERSION,
        path_sha256=hashlib.sha256(str(path).encode("utf-8")).hexdigest(),
        st_dev=opened.st_dev,
        st_ino=opened.st_ino,
        mode=opened.st_mode,
        sha256=hasher.hexdigest(),
    )


@dataclass(frozen=True)
class _VersionProbeOwner:
    """Exact direct-child/group ownership retained until group absence."""

    process: subprocess.Popen[bytes]
    leader_pid: int
    pgid: int


_VERSION_PROBE_LOCK = threading.Lock()
_RETAINED_VERSION_PROBES: dict[int, _VersionProbeOwner] = {}
_LIBPROC: ctypes.CDLL | None = None


def _libproc() -> ctypes.CDLL:
    global _LIBPROC
    if _LIBPROC is None:
        try:
            library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        except OSError as error:
            raise AttestationError(
                "version probe group enumeration is unavailable"
            ) from error
        library.proc_listpgrppids.argtypes = [
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        library.proc_listpgrppids.restype = ctypes.c_int
        _LIBPROC = library
    return _LIBPROC


def _list_version_probe_group(pgid: int) -> tuple[int, ...]:
    capacity = 16
    while capacity <= 4096:
        first = (ctypes.c_int * capacity)()
        second = (ctypes.c_int * capacity)()
        listed = _libproc().proc_listpgrppids(pgid, first, ctypes.sizeof(first))
        confirmed = _libproc().proc_listpgrppids(pgid, second, ctypes.sizeof(second))
        if listed < 0 or confirmed < 0:
            raise AttestationError("version probe group enumeration failed")
        if listed >= capacity or confirmed >= capacity or confirmed > listed:
            if capacity == 4096:
                raise AttestationError("version probe group enumeration is unbounded")
            capacity *= 2
            continue
        return tuple(sorted(pid for pid in second[:confirmed] if pid > 0))
    raise AttestationError("version probe group enumeration is unavailable")


def _version_probe_leader_state(
    owner: _VersionProbeOwner,
) -> Literal["live", "reapable"]:
    transition_deadline = time.monotonic() + 0.05
    while True:
        try:
            observed = os.waitid(
                os.P_PID,
                owner.leader_pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except ChildProcessError as error:
            raise AttestationError("version probe leader ownership was lost") from error
        if observed is not None and observed.si_pid == owner.leader_pid:
            return "reapable"
        try:
            current_pgid = os.getpgid(owner.leader_pid)
        except ProcessLookupError:
            # The direct child may exit between WNOHANG and getpgid. Retry the
            # non-reaping observation under a short bound while Darwin
            # publishes the waitable state.
            if time.monotonic() >= transition_deadline:
                break
            time.sleep(0.001)
            continue
        if current_pgid != owner.pgid:
            raise AttestationError("version probe process group identity changed")
        return "live"
    raise AttestationError("version probe leader identity became ambiguous")


def _version_probe_group_is_absent(owner: _VersionProbeOwner) -> bool:
    state = _version_probe_leader_state(owner)
    members = _list_version_probe_group(owner.pgid)
    return state == "reapable" and all(member == owner.leader_pid for member in members)


def _wait_for_version_probe_group_absence(
    owner: _VersionProbeOwner, seconds: float
) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if _version_probe_group_is_absent(owner):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.001)


def _signal_version_probe_group(owner: _VersionProbeOwner, signal_number: int) -> None:
    # The exact, unreaped direct child is the group-ID reuse sentinel. Never
    # signal after losing it, and never signal a group not proven to be its own
    # start_new_session group.
    _version_probe_leader_state(owner)
    try:
        os.killpg(owner.pgid, signal_number)
    except ProcessLookupError:
        if not _version_probe_group_is_absent(owner):
            raise AttestationError(
                "version probe process group disappeared ambiguously"
            )


def _reap_version_probe_leader(owner: _VersionProbeOwner) -> int:
    if not _version_probe_group_is_absent(owner):
        raise AttestationError("version probe group absence is unconfirmed")
    try:
        return owner.process.wait(timeout=0.25)
    except subprocess.TimeoutExpired as error:
        raise AttestationError("version probe leader could not be reaped") from error


def _retain_version_probe(owner: _VersionProbeOwner) -> None:
    _RETAINED_VERSION_PROBES[owner.leader_pid] = owner


def _terminate_version_probe(owner: _VersionProbeOwner) -> None:
    try:
        if not _wait_for_version_probe_group_absence(owner, 0):
            _signal_version_probe_group(owner, signal.SIGTERM)
        if not _wait_for_version_probe_group_absence(
            owner, _VERSION_PROBE_TERM_SECONDS
        ):
            _signal_version_probe_group(owner, signal.SIGKILL)
        if not _wait_for_version_probe_group_absence(
            owner, _VERSION_PROBE_KILL_SECONDS
        ):
            raise AttestationError("version probe group cleanup is unconfirmed")
        _reap_version_probe_leader(owner)
    except BaseException:
        # Retaining the unreaped direct-child Popen object preserves the PID /
        # PGID reuse sentinel. Future probes fail closed rather than signal a
        # numeric group whose ownership can no longer be proved.
        _retain_version_probe(owner)
        raise


def _capture_version_probe_owner(
    process: subprocess.Popen[bytes],
) -> _VersionProbeOwner:
    owner = _VersionProbeOwner(process, process.pid, process.pid)
    try:
        try:
            current_pgid = os.getpgid(owner.leader_pid)
        except ProcessLookupError:
            # Successful Popen return proves the start_new_session setsid step
            # completed before exec. An already-reapable direct child remains
            # the exact PID/PGID sentinel even though getpgid no longer reports
            # zombies on Darwin.
            if _version_probe_leader_state(owner) != "reapable":
                raise
        else:
            if current_pgid != owner.pgid:
                raise AttestationError(
                    "version probe did not create an isolated session"
                )
    except BaseException:
        _retain_version_probe(owner)
        raise
    return owner


def _finish_version_probe(owner: _VersionProbeOwner, deadline: float) -> int:
    while _version_probe_leader_state(owner) != "reapable":
        if time.monotonic() >= deadline:
            raise AttestationError("CLI version measurement timed out")
        time.sleep(0.001)
    if not _version_probe_group_is_absent(owner):
        raise AttestationError("CLI version probe retained descendant processes")
    return _reap_version_probe_leader(owner)


def _run_bounded_version_probe(
    path: Path, environment: Mapping[str, str]
) -> tuple[int, bytes, bytes]:
    with _VERSION_PROBE_LOCK:
        if _RETAINED_VERSION_PROBES:
            raise AttestationError("prior version probe cleanup is unconfirmed")
        return _run_bounded_version_probe_locked(path, environment)


def _run_bounded_version_probe_locked(
    path: Path, environment: Mapping[str, str]
) -> tuple[int, bytes, bytes]:
    try:
        process = subprocess.Popen(
            [str(path), "--version"],
            stdin=DEVNULL,
            stdout=PIPE,
            stderr=PIPE,
            env=dict(environment),
            start_new_session=True,
        )
    except OSError as error:
        raise AttestationError("CLI version measurement failed") from error
    owner = _capture_version_probe_owner(process)
    if process.stdout is None or process.stderr is None:
        _terminate_version_probe(owner)
        raise AttestationError("CLI version probe pipes are unavailable")

    streams = (process.stdout, process.stderr)
    outputs = {
        process.stdout.fileno(): bytearray(),
        process.stderr.fileno(): bytearray(),
    }
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + _VERSION_PROBE_TIMEOUT_SECONDS
    try:
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream.fileno(), selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AttestationError("CLI version measurement timed out")
            events = selector.select(remaining)
            if not events:
                raise AttestationError("CLI version measurement timed out")
            for key, _mask in events:
                try:
                    chunk = os.read(key.fd, 4096)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fd)
                    continue
                output = outputs[key.fd]
                output.extend(chunk)
                if len(output) > _VERSION_OUTPUT_LIMIT:
                    raise AttestationError("CLI version probe exceeded output bound")
        returncode = _finish_version_probe(owner, deadline)
        return (
            returncode,
            bytes(outputs[process.stdout.fileno()]),
            bytes(outputs[process.stderr.fileno()]),
        )
    except BaseException:
        _terminate_version_probe(owner)
        raise
    finally:
        selector.close()
        for stream in streams:
            stream.close()


def _measure_cli(
    path: Path,
    environment: Mapping[str, str],
    manifest_identity: CliExecutableIdentity,
) -> CliExecutableIdentity:
    before = _snapshot_cli(path)
    if before != manifest_identity:
        raise AttestationError("CLI executable does not match manifest identity")
    probe_error: AttestationError | None = None
    result: tuple[int, bytes, bytes] | None = None
    try:
        result = _run_bounded_version_probe(path, environment)
    except AttestationError as error:
        probe_error = error
    try:
        after = _snapshot_cli(path)
    except AttestationError as error:
        raise AttestationError("CLI changed during measurement") from error
    if before != after:
        raise AttestationError("CLI changed during measurement")
    if probe_error is not None:
        raise probe_error
    assert result is not None
    returncode, stdout, stderr = result
    if returncode != 0:
        raise AttestationError("CLI version probe did not succeed")
    if stdout != _VERSION_STDOUT or stderr:
        raise AttestationError("CLI version does not match pinned 2.1.251")
    return before


def _require_executable(path: Path, label: str) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as error:
        raise AttestationError(f"{label} is unavailable") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or not metadata.st_mode & stat.S_IXUSR
    ):
        raise AttestationError(f"{label} must be a no-follow executable regular file")


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


def _fd_identity(descriptor: int) -> tuple[int, int, int]:
    try:
        metadata = os.fstat(descriptor)
    except OSError as error:
        raise AttestationError("control FD is not open") from error
    if not stat.S_ISSOCK(metadata.st_mode):
        raise AttestationError("control FD must be an open socket")
    return metadata.st_dev, metadata.st_ino, metadata.st_mode


class PreparedSupervisorLaunch:
    """Sealed, single-consumer launch authority produced by final validation."""

    __slots__ = (
        "_authority",
        "_bootstrap_descriptor_names",
        "_child_control",
        "_child_control_identity",
        "_cli_identity",
        "_command",
        "_config",
        "_descriptors",
        "_fingerprint",
        "_network_proxy_enabled",
        "_parent_control",
        "_parent_control_identity",
        "_sealed",
        "_supervisor_environment_items",
        "_transport_claimed",
        "_transport_identity",
    )
    _authority: object
    _bootstrap_descriptor_names: tuple[str, ...]
    _child_control: socket.socket
    _child_control_identity: tuple[int, int, int]
    _cli_identity: CliExecutableIdentity
    _command: tuple[str, ...]
    _config: IsolationConfig
    _descriptors: SupervisorBootstrapDescriptors
    _fingerprint: str
    _network_proxy_enabled: bool
    _parent_control: socket.socket
    _parent_control_identity: tuple[int, int, int]
    _sealed: bool
    _supervisor_environment_items: tuple[tuple[str, str], ...]
    _transport_claimed: bool
    _transport_identity: object | None

    def __new__(cls, *_args: object, **_kwargs: object) -> PreparedSupervisorLaunch:
        raise TypeError("PreparedSupervisorLaunch cannot be constructed publicly")

    @classmethod
    def _create(
        cls,
        token: object,
        *,
        config: IsolationConfig,
        descriptors: SupervisorBootstrapDescriptors,
        cli_identity: CliExecutableIdentity,
        effective_environment: Mapping[str, str],
        network_proxy_enabled: bool,
    ) -> PreparedSupervisorLaunch:
        if token is not _LAUNCH_TOKEN:
            raise AttestationError("launch authority is invalid")
        value = object.__new__(cls)
        object.__setattr__(value, "_sealed", False)
        immutable_config = IsolationConfig(
            model_id=config.model_id,
            system_prompt=config.system_prompt,
            cwd=config.cwd,
            supervisor_path=config.supervisor_path,
            environment=MappingProxyType(dict(effective_environment)),
        )
        options = build_agent_options(immutable_config)
        parent, child = socket.socketpair()
        child.set_inheritable(True)
        supervisor_environment = {
            **dict(effective_environment),
            "LOCAL_PROXY_ALLOCATION_NONCE": descriptors.allocation_nonce,
            "LOCAL_PROXY_INSTANCE_DIR": str(descriptors.instance_dir),
            "LOCAL_PROXY_REAL_CLAUDE": str(descriptors.real_cli),
            "LOCAL_PROXY_CONTROL_FD": str(child.fileno()),
        }
        command = (str(config.supervisor_path), *_cli_arguments(options))
        object.__setattr__(value, "_authority", _LAUNCH_TOKEN)
        object.__setattr__(
            value, "_bootstrap_descriptor_names", BOOTSTRAP_DESCRIPTOR_NAMES
        )
        object.__setattr__(value, "_child_control", child)
        object.__setattr__(
            value, "_child_control_identity", _fd_identity(child.fileno())
        )
        object.__setattr__(value, "_cli_identity", cli_identity)
        object.__setattr__(value, "_command", command)
        object.__setattr__(value, "_config", immutable_config)
        object.__setattr__(value, "_descriptors", descriptors)
        object.__setattr__(value, "_network_proxy_enabled", network_proxy_enabled)
        object.__setattr__(value, "_parent_control", parent)
        object.__setattr__(
            value, "_parent_control_identity", _fd_identity(parent.fileno())
        )
        object.__setattr__(
            value,
            "_supervisor_environment_items",
            tuple(sorted(supervisor_environment.items())),
        )
        object.__setattr__(value, "_transport_claimed", False)
        object.__setattr__(value, "_transport_identity", None)
        object.__setattr__(value, "_fingerprint", value._current_fingerprint())
        object.__setattr__(value, "_sealed", True)
        return value

    def __setattr__(self, _name: str, _value: object) -> Never:
        raise AttestationError("prepared launch is sealed")

    def __copy__(self) -> Never:
        raise AttestationError("prepared launch cannot be copied")

    def __deepcopy__(self, _memo: object) -> Never:
        raise AttestationError("prepared launch cannot be copied")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> Never:
        raise AttestationError("prepared launch cannot be pickled")

    @property
    def command(self) -> tuple[str, ...]:
        self._validate()
        return self._command

    @property
    def options(self) -> ClaudeAgentOptions:
        self._validate()
        return build_agent_options(self._config)

    @property
    def supervisor_environment(self) -> Mapping[str, str]:
        self._validate()
        return MappingProxyType(dict(self._supervisor_environment_items))

    @property
    def bootstrap_descriptor_names(self) -> tuple[str, ...]:
        self._validate()
        return self._bootstrap_descriptor_names

    @property
    def inherited_fds(self) -> tuple[int, ...]:
        self._validate()
        return (self._child_control.fileno(),)

    @property
    def cli_identity(self) -> CliExecutableIdentity:
        self._validate()
        return self._cli_identity

    @property
    def supervisor_internal_relay_fd(self) -> int:
        return SUPERVISOR_INTERNAL_RELAY_FD

    def _current_fingerprint(self) -> str:
        return _digest(
            b"claude-sdk-proxy:prepared-launch:v1",
            {
                "command": self._command,
                "cwd": str(self._config.cwd),
                "environment": self._supervisor_environment_items,
                "allocation_nonce": self._descriptors.allocation_nonce,
                "instance_dir": str(self._descriptors.instance_dir),
                "real_cli": str(self._descriptors.real_cli),
                "cli_identity": self._cli_identity.digest(),
                "network_proxy_enabled": self._network_proxy_enabled,
                "child_control_identity": self._child_control_identity,
                "parent_control_identity": self._parent_control_identity,
                "journal_identity": id(self._descriptors.journal),
            },
        )

    def _validate(self) -> None:
        if self._authority is not _LAUNCH_TOKEN or not self._sealed:
            raise AttestationError("prepared launch authority is invalid")
        if self._fingerprint != self._current_fingerprint():
            raise AttestationError("prepared launch fields changed")
        if self._child_control.fileno() < 0 or self._parent_control.fileno() < 0:
            raise AttestationError("prepared launch control channel is closed")
        if _fd_identity(self._child_control.fileno()) != self._child_control_identity:
            raise AttestationError("child control FD identity changed")
        if _fd_identity(self._parent_control.fileno()) != self._parent_control_identity:
            raise AttestationError("parent control FD identity changed")

    def _claim(self, token: object, transport: object) -> None:
        if token is not _LAUNCH_TOKEN:
            raise AttestationError("transport claim authority is invalid")
        self._validate()
        if self._transport_claimed:
            raise AttestationError("prepared launch is already claimed")
        object.__setattr__(self, "_transport_claimed", True)
        object.__setattr__(self, "_transport_identity", transport)

    def _require_transport(self, transport: object) -> None:
        self._validate()
        if self._transport_identity is not transport:
            raise AttestationError("prepared launch transport identity changed")

    def close(self) -> None:
        for control in (self._child_control, self._parent_control):
            try:
                control.close()
            except OSError:
                pass


def prepare_supervisor_launch(
    config: IsolationConfig,
    descriptors: SupervisorBootstrapDescriptors,
    manifest: AttestationManifest,
    *,
    source_environment: Mapping[str, str],
    environment_config: EnvironmentConfig,
) -> PreparedSupervisorLaunch:
    if not isinstance(descriptors, SupervisorBootstrapDescriptors):
        raise AttestationError("Task 5 bootstrap descriptors are required")
    if not isinstance(manifest, AttestationManifest):
        raise AttestationError("attestation manifest is required")
    if config.cwd != descriptors.instance_dir / _WORKDIR_NAME:
        raise AttestationError("config cwd is not the Task 5 allocation workdir")
    _require_executable(config.supervisor_path, "verified supervisor")
    if environment_config.pass_names:
        raise AttestationError("native Task 5 environment config forbids pass_names")
    if environment_config.cli_dir != descriptors.real_cli.parent:
        raise AttestationError("environment config CLI directory changed")
    effective = build_child_environment(source_environment, environment_config)
    if dict(config.environment) != effective:
        raise AttestationError(
            "prebuilt environment differs from effective child environment"
        )
    fingerprint = environment_fingerprint(effective)
    if fingerprint != manifest.environment_fingerprint:
        raise AttestationError("effective child environment fingerprint changed")
    measured_cli = _measure_cli(
        descriptors.real_cli, effective, manifest.cli_executable
    )
    forbidden = set(BOOTSTRAP_DESCRIPTOR_NAMES) | {"LOCAL_PROXY_NETWORK_PROXY"}
    if set(effective) & forbidden:
        raise AttestationError("real CLI environment contains ambient bootstrap state")
    return PreparedSupervisorLaunch._create(
        _LAUNCH_TOKEN,
        config=config,
        descriptors=descriptors,
        cli_identity=measured_cli,
        effective_environment=effective,
        network_proxy_enabled=environment_config.network_proxy,
    )


def _crc32c(payload: bytes) -> int:
    checksum = 0xFFFFFFFF
    for byte in payload:
        checksum ^= byte
        for _ in range(8):
            mask = -(checksum & 1) & 0xFFFFFFFF
            checksum = (checksum >> 1) ^ (0x82F63B78 & mask)
    return (~checksum) & 0xFFFFFFFF


def _encode_control_frame(
    message_type: int, nonce: bytes, payload: bytes = b""
) -> bytes:
    if message_type not in _CONTROL_NAMES or len(payload) > _CONTROL_MAX_PAYLOAD:
        raise AttestationError("invalid Task 5 control frame")
    header = struct.pack(
        "<IHHI32s",
        _CONTROL_MAGIC,
        _CONTROL_VERSION,
        message_type,
        len(payload),
        nonce,
    )
    return header + payload + struct.pack("<I", _crc32c(header + payload))


def _receive_exact(control: socket.socket, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        try:
            chunk = control.recv(remaining)
        except (OSError, TimeoutError) as error:
            raise AttestationError("Task 5 control read failed or timed out") from error
        if not chunk:
            raise AttestationError("Task 5 control channel closed early")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _receive_control_frame(
    control: socket.socket,
    nonce: bytes,
    expected_type: int,
) -> bytes:
    header = _receive_exact(control, _CONTROL_HEADER_SIZE)
    magic, version, message_type, payload_length, observed_nonce = struct.unpack(
        "<IHHI32s", header
    )
    expected_size = {
        1: _PROCESS_IDENTITY_SIZE,
        3: _PROCESS_IDENTITY_SIZE,
        5: _CLI_ARMED_IDENTITY_SIZE,
        7: _PROCESS_IDENTITY_SIZE,
        10: 40,
        12: 40,
    }.get(expected_type)
    if (
        magic != _CONTROL_MAGIC
        or version != _CONTROL_VERSION
        or message_type != expected_type
        or observed_nonce != nonce
        or payload_length > _CONTROL_MAX_PAYLOAD
        or (expected_size is not None and payload_length != expected_size)
    ):
        raise AttestationError("Task 5 control frame is invalid")
    body = _receive_exact(control, payload_length + _CONTROL_CHECKSUM_SIZE)
    payload = body[:-_CONTROL_CHECKSUM_SIZE]
    checksum = struct.unpack("<I", body[-_CONTROL_CHECKSUM_SIZE:])[0]
    if checksum != _crc32c(header + payload):
        raise AttestationError("Task 5 control checksum is invalid")
    return payload


@dataclass(frozen=True)
class _ProcessIdentity:
    pid: int
    start_ns: int
    uid: int
    pgid: int
    sid: int
    flags: int
    executable_dev: int
    executable_ino: int
    boot_id: bytes
    executable_hash: bytes


def _parse_process_identity(payload: bytes) -> _ProcessIdentity:
    if len(payload) != _PROCESS_IDENTITY_SIZE:
        raise AttestationError("Task 5 process identity size changed")
    pid, start_ns, uid, pgid, sid, flags, executable_dev, executable_ino = (
        struct.unpack_from("<qQIiiIQQ", payload)
    )
    identity = _ProcessIdentity(
        pid,
        start_ns,
        uid,
        pgid,
        sid,
        flags,
        executable_dev,
        executable_ino,
        payload[48:80],
        payload[80:112],
    )
    if identity.flags != 0x3F or identity.pid <= 0:
        raise AttestationError("Task 5 process identity is incomplete")
    return identity


def _same_incarnation(left: _ProcessIdentity, right: _ProcessIdentity) -> bool:
    return (
        left.pid,
        left.start_ns,
        left.uid,
        left.pgid,
        left.sid,
        left.flags,
        left.boot_id,
    ) == (
        right.pid,
        right.start_ns,
        right.uid,
        right.pgid,
        right.sid,
        right.flags,
        right.boot_id,
    )


def _observed_identity_matches(claimed: _ProcessIdentity, observed: object) -> bool:
    try:
        current = _ProcessIdentity(
            pid=observed.pid,  # type: ignore[attr-defined]
            start_ns=observed.start_ns,  # type: ignore[attr-defined]
            uid=observed.uid,  # type: ignore[attr-defined]
            pgid=observed.pgid,  # type: ignore[attr-defined]
            sid=observed.sid,  # type: ignore[attr-defined]
            flags=observed.flags,  # type: ignore[attr-defined]
            executable_dev=observed.executable_dev,  # type: ignore[attr-defined]
            executable_ino=observed.executable_ino,  # type: ignore[attr-defined]
            boot_id=observed.boot_id,  # type: ignore[attr-defined]
            executable_hash=observed.executable_hash,  # type: ignore[attr-defined]
        )
    except AttributeError, TypeError, ValueError:
        return False
    return (
        _same_incarnation(claimed, current)
        and claimed.executable_dev == current.executable_dev
        and claimed.executable_ino == current.executable_ino
        and claimed.executable_hash == current.executable_hash
    )


class SupervisorHandshakeReceipt:
    """Opaque receipt minted only after the actual Task 5 runtime handshake."""

    __slots__ = (
        "_canonical_control_hashes",
        "_canonical_control_sequences",
        "_canonical_control_types",
        "_control_trace",
        "_fingerprint",
        "_identity_ack_hash",
        "_identity_ack_sequence",
        "_network_proxy_enabled",
        "_token",
    )
    _canonical_control_hashes: tuple[bytes, ...]
    _canonical_control_sequences: tuple[int, ...]
    _canonical_control_types: tuple[str, ...]
    _control_trace: tuple[str, ...]
    _fingerprint: str
    _identity_ack_hash: bytes
    _identity_ack_sequence: int
    _network_proxy_enabled: bool
    _token: object

    def __new__(cls, *_args: object, **_kwargs: object) -> SupervisorHandshakeReceipt:
        raise TypeError("SupervisorHandshakeReceipt cannot be constructed publicly")

    def __setattr__(self, _name: str, _value: object) -> Never:
        raise AttestationError("handshake receipt is sealed")

    @classmethod
    def _create(
        cls,
        token: object,
        *,
        sequences: tuple[int, ...],
        hashes: tuple[bytes, ...],
        ack_sequence: int,
        ack_hash: bytes,
        network_proxy_enabled: bool,
    ) -> SupervisorHandshakeReceipt:
        if token is not _RECEIPT_TOKEN:
            raise AttestationError("handshake receipt authority is invalid")
        if len(sequences) != 4 or tuple(sorted(sequences)) != sequences:
            raise AttestationError("Task 5 certified sequences are invalid")
        if len(hashes) != 4 or len(set(hashes)) != 4:
            raise AttestationError("Task 5 certified hashes are invalid")
        value = object.__new__(cls)
        object.__setattr__(value, "_token", _RECEIPT_TOKEN)
        object.__setattr__(value, "_control_trace", _EXPECTED_TRACE)
        object.__setattr__(value, "_canonical_control_types", _EXPECTED_CANONICAL_TYPES)
        object.__setattr__(value, "_canonical_control_sequences", sequences)
        object.__setattr__(value, "_canonical_control_hashes", hashes)
        object.__setattr__(value, "_identity_ack_sequence", ack_sequence)
        object.__setattr__(value, "_identity_ack_hash", ack_hash)
        object.__setattr__(value, "_network_proxy_enabled", network_proxy_enabled)
        object.__setattr__(value, "_fingerprint", value._current_fingerprint())
        value._validate()
        return value

    def _current_fingerprint(self) -> str:
        return _digest(
            b"claude-sdk-proxy:supervisor-handshake-receipt:v1",
            {
                "control_trace": self._control_trace,
                "canonical_control_types": self._canonical_control_types,
                "canonical_control_sequences": self._canonical_control_sequences,
                "canonical_control_hashes": tuple(
                    value.hex() for value in self._canonical_control_hashes
                ),
                "identity_ack_sequence": self._identity_ack_sequence,
                "identity_ack_hash": self._identity_ack_hash.hex(),
                "network_proxy_enabled": self._network_proxy_enabled,
                "authorizes_authentication": False,
            },
        )

    def _validate(self) -> None:
        try:
            token = self._token
            fingerprint = self._fingerprint
            current = self._current_fingerprint()
        except (AttributeError, TypeError, ValueError) as error:
            raise AttestationError("handshake receipt is invalid") from error
        if token is not _RECEIPT_TOKEN or fingerprint != current:
            raise AttestationError("handshake receipt is invalid")

    @property
    def control_trace(self) -> tuple[str, ...]:
        self._validate()
        return self._control_trace

    @property
    def canonical_control_types(self) -> tuple[str, ...]:
        self._validate()
        return self._canonical_control_types

    @property
    def canonical_control_sequences(self) -> tuple[int, ...]:
        self._validate()
        return self._canonical_control_sequences

    @property
    def canonical_control_hashes(self) -> tuple[bytes, ...]:
        self._validate()
        return self._canonical_control_hashes

    @property
    def identity_ack_sequence(self) -> int:
        self._validate()
        return self._identity_ack_sequence

    @property
    def identity_ack_hash(self) -> bytes:
        self._validate()
        return self._identity_ack_hash

    @property
    def network_proxy_enabled(self) -> bool:
        self._validate()
        return self._network_proxy_enabled

    @property
    def authorizes_authentication(self) -> Literal[False]:
        self._validate()
        return False

    def __copy__(self) -> Never:
        self._validate()
        raise AttestationError("handshake receipt cannot be copied")

    def __deepcopy__(self, _memo: object) -> Never:
        self._validate()
        raise AttestationError("handshake receipt cannot be copied")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> Never:
        self._validate()
        raise AttestationError("handshake receipt cannot be pickled")


def _certify_identity(
    control: socket.socket,
    journal: Journal,
    nonce: bytes,
    expected_type: int,
) -> tuple[bytes, BootstrapHead]:
    payload = _receive_control_frame(control, nonce, expected_type)
    try:
        certified = journal.certify_bootstrap(expected_type, payload)
    except Exception as error:
        raise AttestationError("Task 5 bootstrap identity is not certified") from error
    return payload, certified


def _perform_handshake(
    control: socket.socket,
    journal: Journal,
    nonce: bytes,
    cli_identity: CliExecutableIdentity,
    network_proxy_enabled: bool,
    supervisor_pid: int,
) -> SupervisorHandshakeReceipt:
    control.settimeout(5)
    supervisor_payload, supervisor_head = _certify_identity(control, journal, nonce, 1)
    supervisor = _parse_process_identity(supervisor_payload)
    if supervisor.pid != supervisor_pid:
        raise AttestationError("Task 5 supervisor identity is not the retained child")
    canonical = journal.certify_head()
    config = struct.pack(
        _SUPERVISOR_CONFIG_FORMAT,
        _SUPERVISOR_CONFIG_VERSION,
        int(network_proxy_enabled),
        0,
        0,
        canonical.sequence,
        canonical.hash,
    )
    control.sendall(_encode_control_frame(2, nonce, config))

    anchor_payload, anchor_head = _certify_identity(control, journal, nonce, 3)
    anchor = _parse_process_identity(anchor_payload)
    if (
        anchor.pid != anchor.pgid
        or anchor.pid != anchor.sid
        or (anchor.pgid, anchor.sid) == (supervisor.pgid, supervisor.sid)
    ):
        raise AttestationError("Task 5 anchor identity chain is invalid")
    control.sendall(_encode_control_frame(4, nonce))

    armed_payload, armed_head = _certify_identity(control, journal, nonce, 5)
    armed_member = _parse_process_identity(armed_payload[:112])
    expected_dev, expected_ino = struct.unpack_from("<QQ", armed_payload, 112)
    expected_hash = armed_payload[128:160]
    expected_path_hash = armed_payload[160:192]
    try:
        observed_anchor = journal.observe_process(anchor.pid)
        observed_armed = journal.observe_process(armed_member.pid)
    except Exception as error:
        raise AttestationError("Task 5 armed CLI identity is invalid") from error
    if (
        not _observed_identity_matches(anchor, observed_anchor)
        or not _observed_identity_matches(armed_member, observed_armed)
        or (armed_member.pgid, armed_member.sid) != (anchor.pgid, anchor.sid)
        or expected_dev != cli_identity.st_dev
        or expected_ino != cli_identity.st_ino
        or expected_hash.hex() != cli_identity.sha256
        or expected_path_hash.hex() != cli_identity.path_sha256
    ):
        raise AttestationError("Task 5 armed CLI identity is invalid")
    control.sendall(_encode_control_frame(6, nonce))

    running_payload, running_head = _certify_identity(control, journal, nonce, 7)
    running = _parse_process_identity(running_payload)
    if (
        not _same_incarnation(armed_member, running)
        or running.executable_dev != cli_identity.st_dev
        or running.executable_ino != cli_identity.st_ino
        or running.executable_hash.hex() != cli_identity.sha256
    ):
        raise AttestationError("Task 5 running CLI identity is invalid")
    return SupervisorHandshakeReceipt._create(
        _RECEIPT_TOKEN,
        sequences=(
            supervisor_head.sequence,
            anchor_head.sequence,
            armed_head.sequence,
            running_head.sequence,
        ),
        hashes=(
            supervisor_head.hash,
            anchor_head.hash,
            armed_head.hash,
            running_head.hash,
        ),
        ack_sequence=canonical.sequence,
        ack_hash=canonical.hash,
        network_proxy_enabled=network_proxy_enabled,
    )


class _DuplicateKey(ValueError):
    pass


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKey(key)
        value[key] = item
    return value


def _is_safe_initialize(data: str) -> bool:
    if not isinstance(data, str):
        return False
    try:
        encoded = data.encode("utf-8")
    except UnicodeEncodeError:
        return False
    if (
        len(encoded) > _INITIALIZE_MAX_BYTES
        or not data.endswith("\n")
        or data.count("\n") != 1
        or "\r" in data
    ):
        return False
    try:
        value = json.loads(data[:-1], object_pairs_hook=_object_without_duplicates)
    except TypeError, ValueError, _DuplicateKey:
        return False
    if (
        not isinstance(value, dict)
        or set(value) != {"type", "request_id", "request"}
        or value.get("type") != "control_request"
        or not isinstance(value.get("request_id"), str)
        or _REQUEST_ID.fullmatch(value["request_id"]) is None
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


def _validate_cleanup_result(
    result: bytes,
    *,
    expected_done_sequence: int,
) -> None:
    (
        flags,
        batch_count,
        completed_steps,
        done_sequence,
        process_batch_admission_sequence,
        injection_stage,
        reserved,
    ) = struct.unpack("<IIQQQII", result)
    required_flags = 0xFF7
    allowed_flags = 0x2FFF
    if (
        flags & required_flags != required_flags
        or flags & ~allowed_flags
        or batch_count != 4
        or completed_steps != 0xF
        or done_sequence != expected_done_sequence
        or process_batch_admission_sequence == 0
        or injection_stage != 0
        or reserved != 0
    ):
        raise AttestationError("Task 5 cleanup result is incomplete or ambiguous")


def _prepare_cleanup(control: socket.socket, journal: Journal, nonce: bytes) -> None:
    appended = journal.append_bootstrap(8, b"")
    certified = journal.certify_bootstrap(8, b"")
    if appended.sequence != certified.sequence or appended.hash != certified.hash:
        raise AttestationError("Task 5 cleanup request certification changed")
    control.sendall(
        _encode_control_frame(
            8,
            nonce,
            struct.pack("<Q32s", certified.sequence, certified.hash),
        )
    )
    acknowledgement = _receive_control_frame(control, nonce, 12)
    ack_sequence, ack_hash = struct.unpack("<Q32s", acknowledgement)
    if ack_sequence != certified.sequence or ack_hash != certified.hash:
        raise AttestationError("Task 5 cleanup ACK is not bound to the request")
    result = _receive_control_frame(control, nonce, 10)
    try:
        journal.certify_bootstrap(10, result)
    except Exception as error:
        raise AttestationError("Task 5 cleanup result is not certified") from error
    head = journal.certify_head()
    if head.state.kind.name != "DONE":
        raise AttestationError("Task 5 cleanup result did not reach DONE")
    _validate_cleanup_result(result, expected_done_sequence=head.sequence)


class _OwnedSupervisorProcess:
    """Popen owner that leaves the child reapable for Task 4 confirmation."""

    __slots__ = ("_popen", "_stdin", "_stdout")

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        inherited_fds: tuple[int, ...],
    ) -> None:
        process = subprocess.Popen(
            command,
            stdin=PIPE,
            stdout=PIPE,
            stderr=DEVNULL,
            cwd=cwd,
            env=dict(environment),
            pass_fds=inherited_fds,
        )
        self._popen = process
        self._stdin: IO[bytes] | None = process.stdin
        self._stdout: IO[bytes] | None = process.stdout

    @property
    def pid(self) -> int:
        return self._popen.pid

    @property
    def returncode(self) -> int | None:
        return self._popen.returncode

    @property
    def pipes_available(self) -> bool:
        return self._stdin is not None and self._stdout is not None

    async def send(self, data: str) -> None:
        stream = self._stdin
        if stream is None:
            raise AttestationError("supervisor stdin is closed")
        encoded = data.encode("utf-8")

        def write_all() -> None:
            cursor = 0
            while cursor < len(encoded):
                try:
                    written = os.write(stream.fileno(), encoded[cursor:])
                except OSError as error:
                    raise AttestationError("supervisor stdin write failed") from error
                if written <= 0:
                    raise AttestationError("supervisor stdin write was incomplete")
                cursor += written

        with anyio.fail_after(1):
            await anyio.to_thread.run_sync(write_all, abandon_on_cancel=True)

    async def receive(self) -> bytes:
        stream = self._stdout
        if stream is None:
            return b""
        return await anyio.to_thread.run_sync(
            os.read,
            stream.fileno(),
            65536,
            abandon_on_cancel=True,
        )

    async def close_stdin(self) -> None:
        stream = self._stdin
        if stream is not None:
            await anyio.to_thread.run_sync(stream.close, abandon_on_cancel=True)
            self._stdin = None

    def record_confirmed_exit(self, status: int) -> None:
        if self._popen.returncode is not None:
            raise AttestationError("supervisor was reaped outside Task 4")
        self._popen.returncode = status

    async def aclose(self) -> None:
        def close_streams() -> None:
            for stream in (self._stdin, self._stdout):
                if stream is not None:
                    stream.close()

        await anyio.to_thread.run_sync(close_streams, abandon_on_cancel=True)
        self._stdin = None
        self._stdout = None


def _observe_successful_unreaped_exit(pid: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            observed = os.waitid(
                os.P_PID,
                pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except (ChildProcessError, OSError) as error:
            raise AttestationError(
                "supervisor was not retained for Task 4 reap"
            ) from error
        if observed is None or observed.si_pid == 0:
            time.sleep(0.001)
            continue
        if (
            observed.si_pid != pid
            or observed.si_code != os.CLD_EXITED
            or observed.si_status != 0
        ):
            raise AttestationError("supervisor did not exit successfully after cleanup")
        return
    raise AttestationError("supervisor did not become reapable after cleanup")


def _observe_unreaped_exit(pid: int) -> int | None:
    try:
        observed = os.waitid(
            os.P_PID,
            pid,
            os.WEXITED | os.WNOHANG | os.WNOWAIT,
        )
    except (ChildProcessError, OSError) as error:
        raise AttestationError("supervisor Task 4 ownership is unavailable") from error
    if observed is None or observed.si_pid == 0:
        return None
    if observed.si_pid != pid:
        raise AttestationError("supervisor Task 4 ownership changed")
    if observed.si_code == os.CLD_EXITED:
        return observed.si_status
    if observed.si_code in {os.CLD_KILLED, os.CLD_DUMPED}:
        return -observed.si_status
    raise AttestationError("supervisor exit state is not terminal")


def _reconcile_failed_handshake_exit(
    journal: Journal, process: _OwnedSupervisorProcess
) -> bool:
    """Reap only through the authoritative Task 4 transition, never Popen."""
    head = journal.scan().head
    kind = head.record.kind.name
    proof = None
    if process.returncode is None:
        exit_status = _observe_unreaped_exit(process.pid)
        if exit_status is None:
            return False
        if kind in {"ACTIVE_READY", "BATCH_ACTIVE"}:
            if time.monotonic_ns() <= head.record.lease_deadline_ns:
                return False
            retired = journal.retire_executor(
                authority="task6-handshake-reconciler",
                authority_epoch=1,
                authority_deadline_ns=time.monotonic_ns() + 1_000_000_000,
                deadline_ns=time.monotonic_ns() + 1_000_000_000,
            )
            kind = retired.record.kind.name
        if kind not in {"DONE", "RETIRING_IDLE", "RETIRING_BATCH"}:
            return False
        proof = journal.confirm_executor_reaped(
            deadline_ns=time.monotonic_ns() + 1_000_000_000
        )
        # Task 4 has consumed the zombie and retained its proof. Record that
        # fact immediately so a later reconciliation failure never attempts a
        # second waitid/reap through the Popen facade.
        process.record_confirmed_exit(exit_status)
    elif kind in {"RETIRING_IDLE", "RETIRING_BATCH"}:
        proof = journal.recover_executor_reap_proof(
            deadline_ns=time.monotonic_ns() + 1_000_000_000
        )
    elif kind not in {"DONE", "UNCONFIRMED"}:
        return False
    if kind == "RETIRING_BATCH":
        if proof is None:
            return False
        journal.reconcile_interrupted_batch(
            proof, deadline_ns=time.monotonic_ns() + 1_000_000_000
        )
    certified = journal.certify_head(deadline_ns=time.monotonic_ns() + 1_000_000_000)
    if certified.state.kind.name not in {"DONE", "UNCONFIRMED"}:
        journal.mark_unconfirmed(
            UnconfirmedReason.PROOF_UNAVAILABLE,
            deadline_ns=time.monotonic_ns() + 1_000_000_000,
        )
    return True


class AttestedSupervisorTransport(Transport):
    """SDK transport with exact env/FD control and an actual Task 5 handshake."""

    def __init__(self, launch: PreparedSupervisorLaunch) -> None:
        if not isinstance(launch, PreparedSupervisorLaunch):
            raise AttestationError("validated supervisor launch is required")
        launch._claim(_LAUNCH_TOKEN, self)
        self.launch = launch
        self._control = launch._parent_control
        self._journal = launch._descriptors.journal
        self._nonce = bytes.fromhex(launch._descriptors.allocation_nonce)
        self._network_proxy_enabled = launch._network_proxy_enabled
        self._process: Any | None = None
        self._stdin: _OwnedSupervisorProcess | None = None
        self._stdout: _OwnedSupervisorProcess | None = None
        self._lock = anyio.Lock()
        self._ready = False
        self._closed = False
        self._initialize_seen = False
        self._buffered: list[str] = []
        self._discarded = 0
        self._handshake_receipt: SupervisorHandshakeReceipt | None = None
        self._cleanup_unconfirmed = False

    @property
    def handshake_receipt(self) -> SupervisorHandshakeReceipt | None:
        receipt = self._handshake_receipt
        if receipt is not None:
            receipt._validate()
        return receipt

    @property
    def cleanup_unconfirmed(self) -> bool:
        return self._cleanup_unconfirmed

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
            self.launch._require_transport(self)
            command = self.launch.command
            cli_identity = self.launch.cli_identity
            environment = self.launch.supervisor_environment
            inherited_fds = self.launch.inherited_fds
            process = await anyio.to_thread.run_sync(
                lambda: _OwnedSupervisorProcess(
                    command,
                    cwd=self.launch._config.cwd,
                    environment=environment,
                    inherited_fds=inherited_fds,
                )
            )
            self._process = process
            if not process.pipes_available:
                self._cleanup_unconfirmed = True
                raise AttestationError("supervisor pipes are unavailable")
            self.launch._child_control.close()
            try:
                self._handshake_receipt = await anyio.to_thread.run_sync(
                    _perform_handshake,
                    self._control,
                    self._journal,
                    self._nonce,
                    cli_identity,
                    self._network_proxy_enabled,
                    process.pid,
                )
            except BaseException as error:
                self._cleanup_unconfirmed = True
                if isinstance(error, Exception):
                    raise AttestationError(
                        "Task 5 supervisor handshake failed closed"
                    ) from error
                raise
            self._stdin = self._process
            self._stdout = self._process
            self._ready = True

    async def write(self, data: str) -> None:
        if not isinstance(data, str):
            raise AttestationError("transport write must be text")
        async with self._lock:
            if not self._ready or self._stdin is None or self._closed:
                raise AttestationError("transport is not ready")
            if not self._initialize_seen and _is_safe_initialize(data):
                self._initialize_seen = True
                await self._stdin.send(data)
            else:
                self._buffered.append(str(data))

    async def release_buffered(self) -> Never:
        async with self._lock:
            self._discarded += len(self._buffered)
            self._buffered.clear()
        await self.close()
        raise AttestationError("core attestation gate unavailable")

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        if self._stdout is None:
            raise AttestationError("transport is not connected")
        buffer = ""
        decoder = codecs.getincrementaldecoder("utf-8")()
        while chunk_bytes := await self._stdout.receive():
            chunk = decoder.decode(chunk_bytes)
            buffer += chunk
            if len(buffer.encode("utf-8")) > 1024 * 1024:
                raise AttestationError("supervisor output exceeded transport bound")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise AttestationError("supervisor message must be an object")
                yield value
        buffer += decoder.decode(b"", final=True)
        if buffer.strip():
            value = json.loads(buffer)
            if not isinstance(value, dict):
                raise AttestationError("supervisor message must be an object")
            yield value

    def is_ready(self) -> bool:
        return self._ready and not self._closed

    async def _bounded_stdin_close(self) -> bool:
        stream = self._stdin
        if stream is None:
            return True
        with anyio.move_on_after(0.25) as scope:
            await stream.close_stdin()
        if scope.cancel_called:
            self._cleanup_unconfirmed = True
            return False
        self._stdin = None
        return True

    async def end_input(self) -> None:
        async with self._lock:
            if not await self._bounded_stdin_close():
                raise AttestationError("stdin close timed out; cleanup is unconfirmed")

    async def _bounded_process_aclose(self) -> bool:
        if self._process is None:
            return True
        with anyio.move_on_after(0.25) as scope:
            await self._process.aclose()
        return not scope.cancel_called

    async def _close_failed_handshake_process(self) -> bool:
        process = self._process
        if process is None:
            return True
        if not isinstance(process, _OwnedSupervisorProcess):
            if process.returncode is None:
                return False
            process_closed = await self._bounded_process_aclose()
            if process_closed:
                self._process = None
                self._stdin = None
                self._stdout = None
            return process_closed
        try:
            reconciled = await anyio.to_thread.run_sync(
                _reconcile_failed_handshake_exit,
                self._journal,
                process,
            )
        except Exception:
            return False
        if not reconciled:
            return False
        if not await self._bounded_process_aclose():
            return False
        self._process = None
        self._stdin = None
        self._stdout = None
        return False

    async def _close_running_process(self) -> bool:
        process = self._process
        if process is None:
            return True
        if not isinstance(process, _OwnedSupervisorProcess):
            return False
        try:
            await anyio.to_thread.run_sync(
                _prepare_cleanup,
                self._control,
                self._journal,
                self._nonce,
            )
            await anyio.to_thread.run_sync(
                _observe_successful_unreaped_exit, process.pid
            )
        except Exception:
            return False
        try:
            await anyio.to_thread.run_sync(self._journal.confirm_executor_reaped)
            process.record_confirmed_exit(0)
            authority = await anyio.to_thread.run_sync(self._journal.certify_done)
            await anyio.to_thread.run_sync(self._journal.delete_at, authority)
        except Exception:
            return False
        if not await self._bounded_process_aclose():
            return False
        self._process = None
        self._stdin = None
        self._stdout = None
        return True

    async def _close_transition(self) -> None:
        async with self._lock:
            if self._closed and self._process is None:
                if self._cleanup_unconfirmed:
                    raise AttestationError("transport cleanup is unconfirmed")
                return
            self._closed = True
            self._ready = False
            self._discarded += len(self._buffered)
            self._buffered.clear()
            stdin_closed = await self._bounded_stdin_close()
            receipt = self._handshake_receipt
            if receipt is None:
                process_closed = await self._close_failed_handshake_process()
            else:
                try:
                    receipt._validate()
                except AttestationError:
                    # Receipt integrity is consumer-facing evidence; a caller
                    # mutation cannot be allowed to suppress proven cleanup.
                    pass
                process_closed = await self._close_running_process()
            if stdin_closed and process_closed:
                self._control.close()
                self.launch._child_control.close()
                return
            self._cleanup_unconfirmed = True
        raise AttestationError("transport cleanup is unconfirmed")

    async def close(self) -> None:
        failure: AttestationError | None = None
        with anyio.CancelScope(shield=True):
            try:
                await self._close_transition()
            except AttestationError as error:
                failure = error
            except BaseException:
                self._cleanup_unconfirmed = True
                raise
        # Re-deliver any ambient cancellation only after the shielded state
        # transition has either completed cleanup or retained exact ownership.
        await anyio.lowlevel.checkpoint()
        if failure is not None:
            raise failure


def build_attested_sdk_client(
    launch: PreparedSupervisorLaunch,
    *,
    transport: AttestedSupervisorTransport | None = None,
) -> ClaudeSDKClient:
    if transport is None or not isinstance(transport, AttestedSupervisorTransport):
        raise AttestationError("custom Task 6 transport is mandatory")
    launch._require_transport(transport)
    return ClaudeSDKClient(options=launch.options, transport=transport)


class QueuedTurn:
    __slots__ = ("_identity", "sequence")
    _identity: object
    sequence: int

    def __new__(cls, *_args: object, **_kwargs: object) -> QueuedTurn:
        raise TypeError("QueuedTurn cannot be constructed publicly")

    def __setattr__(self, _name: str, _value: object) -> Never:
        raise AttestationError("queued turn is sealed")

    @classmethod
    def _create(cls, token: object, sequence: int) -> QueuedTurn:
        if token is not _TURN_TOKEN:
            raise AttestationError("queued turn authority is invalid")
        value = object.__new__(cls)
        object.__setattr__(
            value, "sequence", _positive_int(sequence, "queued turn sequence")
        )
        object.__setattr__(value, "_identity", object())
        return value

    def __copy__(self) -> Never:
        raise AttestationError("queued turn cannot be copied")

    def __deepcopy__(self, _memo: object) -> Never:
        raise AttestationError("queued turn cannot be copied")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> Never:
        raise AttestationError("queued turn cannot be pickled")


class AttestationGate:
    """Serialized false gate whose close callbacks always run outside its lock."""

    def __init__(
        self,
        *,
        availability: AttestationAvailability,
        relay: Callable[[bytes], object],
        close_child: Callable[[], None],
    ) -> None:
        if availability is not _CURRENT_AVAILABILITY:
            raise AttestationError("attestation availability verdict is invalid")
        self._availability = availability
        self._relay = relay
        self._close_child = close_child
        self._lock = threading.Lock()
        self._closed = False
        self._revoked = False
        self._close_called = False
        self._pending: dict[int, tuple[QueuedTurn, bytes]] = {}
        self._next_turn_sequence = 1
        self._relayed_model_bytes = 0
        self._relayed_network_bytes = 0

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

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
    def relayed_model_bytes(self) -> int:
        with self._lock:
            return self._relayed_model_bytes

    @property
    def relayed_network_bytes(self) -> int:
        with self._lock:
            return self._relayed_network_bytes

    def _mark_closed(self) -> bool:
        with self._lock:
            self._closed = True
            self._revoked = True
            self._pending.clear()
            if self._close_called:
                return False
            self._close_called = True
            return True

    def _fail_closed(self, error: AttestationError) -> Never:
        should_close = self._mark_closed()
        if should_close:
            try:
                self._close_child()
            except Exception as close_error:
                raise AttestationError(f"{error}; child close failed") from close_error
        raise error

    def queue_turn(
        self,
        *,
        system: str,
        blocks: Sequence[Mapping[str, object]],
        mcp_schema: Mapping[str, object] | None = None,
    ) -> QueuedTurn:
        try:
            payload = json.dumps(
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
        except (TypeError, ValueError) as error:
            self._fail_closed(AttestationError("turn is not immutable JSON"))
            raise AssertionError("unreachable") from error
        with self._lock:
            if self._closed:
                closed = True
            else:
                closed = False
                turn = QueuedTurn._create(_TURN_TOKEN, self._next_turn_sequence)
                self._next_turn_sequence += 1
                self._pending[turn.sequence] = (turn, payload)
        if closed:
            self._fail_closed(AttestationError("attestation gate is not open"))
        return turn

    def attest(self, _attestation: ChildAttestation, _permit: object) -> Never:
        self._fail_closed(AttestationError("core attestation gate unavailable"))

    def revalidate(self) -> Never:
        self._fail_closed(AttestationError("core attestation gate unavailable"))

    def release(self, turn: QueuedTurn) -> Never:
        with self._lock:
            pending = (
                self._pending.get(turn.sequence)
                if isinstance(turn, QueuedTurn)
                else None
            )
            known = pending is not None and pending[0] is turn
        if not known:
            self._fail_closed(AttestationError("queued turn handle is unknown"))
        self._fail_closed(AttestationError("core attestation gate unavailable"))

    def close(self) -> None:
        should_close = self._mark_closed()
        if should_close:
            self._close_child()


__all__ = [
    "ATTESTATION_MANIFEST_SCHEMA",
    "AttestationAvailability",
    "AttestationError",
    "AttestationGate",
    "AttestationManifest",
    "AttestationReasonCode",
    "AttestedSupervisorTransport",
    "ChildAttestation",
    "CliExecutableIdentity",
    "PreparedSupervisorLaunch",
    "QueuedTurn",
    "SupervisorBootstrapDescriptors",
    "SupervisorHandshakeReceipt",
    "build_attested_sdk_client",
    "current_attestation_availability",
    "extract_child_attestation",
    "prepare_supervisor_launch",
]
