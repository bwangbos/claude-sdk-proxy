"""Versioned no-follow policy for login metadata and proxy-owned canaries."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
import threading
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from itertools import islice
from pathlib import Path
from typing import Final, Never, SupportsIndex, cast

PATH_POLICY_VERSION: Final = 1
SNAPSHOT_ROOT_SENTINEL: Final = "."
_DEFAULT_MAX_SNAPSHOT_ENTRIES: Final = 2_048
_DEFAULT_MAX_DEPTH: Final = 16
_DEFAULT_MAX_CONTENT_BYTES: Final = 1_048_576
_MAX_ROOT_PARENT_WALK: Final = 256
_MAX_SNAPSHOT_DEPTH: Final = 64
_MAX_GLOBAL_RESOURCES: Final = 4_096
_MAX_OWNER_RESOURCES: Final = 128

_CREDENTIAL_FILES = frozenset(
    {
        ".credentials.json",
        "credentials.json",
        ".oauth.json",
        "oauth.json",
        "auth.json",
        "authentication.json",
        "api-key.json",
    }
)
_CREDENTIAL_DIRECTORIES = frozenset(
    {"credentials", ".credentials", "auth", "authentication", "oauth", "sessions"}
)
_PROHIBITED_FILES = frozenset(
    {
        "claude.md",
        "settings.json",
        "settings.local.json",
        ".mcp.json",
        "history.jsonl",
    }
)
_PROHIBITED_DIRECTORIES = frozenset(
    {
        "projects",
        "memory",
        "memories",
        "plugins",
        "hooks",
        "skills",
        "agents",
        "mcp",
        "connectors",
        "commands",
        "workflows",
    }
)
_PROXY_SAFE_DIRECTORIES = frozenset({"canaries", "probe-output", "workdir"})


class PathPolicyError(RuntimeError):
    """Raised when a path cannot be handled without following or ambiguity."""


class RootKind(StrEnum):
    REAL_LOGIN = "real_login"
    PROXY_OWNED = "proxy_owned"


class PathClass(StrEnum):
    CREDENTIAL_METADATA_ONLY = "credential_metadata_only"
    PROHIBITED_CONTENT = "prohibited_content"
    PROXY_OWNED_SAFE = "proxy_owned_safe"
    NEW_NONCREDENTIAL_SAFE = "new_noncredential_safe"
    UNKNOWN = "unknown"


class _ResourceState(StrEnum):
    RESERVED = "reserved"
    PENDING = "pending"
    AMBIGUOUS = "ambiguous"
    CLOSED = "closed"


@dataclass(slots=True)
class _OwnedResource:
    token: object
    owner: object
    kind: str
    creator_pid: int
    owner_pid: int
    state: _ResourceState
    resource: object | None = None
    identity: tuple[int, int, int] | None = None


_RESOURCE_LOCK = threading.RLock()
_RESOURCE_PROCESS_PID = os.getpid()
_RESOURCE_REGISTRY: dict[object, _OwnedResource] = {}


def _synchronize_resource_pid() -> None:
    global _RESOURCE_LOCK, _RESOURCE_PROCESS_PID
    current_pid = os.getpid()
    if current_pid == _RESOURCE_PROCESS_PID:
        return
    _RESOURCE_LOCK = threading.RLock()
    _RESOURCE_PROCESS_PID = current_pid
    for record in _RESOURCE_REGISTRY.values():
        if record.state is not _ResourceState.CLOSED:
            record.owner_pid = current_pid
            record.state = _ResourceState.AMBIGUOUS


def _require_resource_health(owner: object) -> None:
    _synchronize_resource_pid()
    with _RESOURCE_LOCK:
        if any(
            record.owner is owner
            and record.state in {_ResourceState.RESERVED, _ResourceState.AMBIGUOUS}
            for record in _RESOURCE_REGISTRY.values()
        ):
            raise PathPolicyError("path-policy resource ownership is ambiguous")


def _reserve_resource(owner: object, kind: str) -> _OwnedResource:
    _require_resource_health(owner)
    with _RESOURCE_LOCK:
        owner_count = sum(
            record.owner is owner for record in _RESOURCE_REGISTRY.values()
        )
        if (
            len(_RESOURCE_REGISTRY) >= _MAX_GLOBAL_RESOURCES
            or owner_count >= _MAX_OWNER_RESOURCES
        ):
            raise PathPolicyError("path-policy resource capacity exceeded")
        token = object()
        record = _OwnedResource(
            token=token,
            owner=owner,
            kind=kind,
            creator_pid=os.getpid(),
            owner_pid=os.getpid(),
            state=_ResourceState.RESERVED,
        )
        _RESOURCE_REGISTRY[token] = record
        return record


def _mark_acquisition_ambiguous(record: _OwnedResource) -> None:
    with _RESOURCE_LOCK:
        record.state = _ResourceState.AMBIGUOUS


def _acquire_fd(owner: object, kind: str, opener: Callable[[], int]) -> int:
    record = _reserve_resource(owner, kind)
    try:
        descriptor = opener()
    except BaseException:
        _mark_acquisition_ambiguous(record)
        raise
    try:
        if type(descriptor) is not int or descriptor < 0:
            raise PathPolicyError("path-policy fd acquisition is invalid")
        with _RESOURCE_LOCK:
            record.resource = descriptor
            record.state = _ResourceState.PENDING
        value = os.fstat(descriptor)
        with _RESOURCE_LOCK:
            record.identity = (value.st_dev, value.st_ino, value.st_mode)
        return descriptor
    except BaseException:
        if record.state is _ResourceState.PENDING:
            _close_fd(owner, descriptor)
        else:
            _mark_acquisition_ambiguous(record)
        raise


def _acquire_closeable[T](
    owner: object, kind: str, opener: Callable[[], T]
) -> T:
    record = _reserve_resource(owner, kind)
    try:
        resource = opener()
    except BaseException:
        _mark_acquisition_ambiguous(record)
        raise
    with _RESOURCE_LOCK:
        record.resource = resource
        record.state = _ResourceState.PENDING
    return resource


def _find_fd_record(owner: object, descriptor: int) -> _OwnedResource:
    matches = [
        record
        for record in _RESOURCE_REGISTRY.values()
        if record.owner is owner
        and type(record.resource) is int
        and record.resource == descriptor
        and record.state is _ResourceState.PENDING
    ]
    if len(matches) != 1:
        raise PathPolicyError("path-policy fd ownership is invalid")
    return matches[0]


def _find_closeable_record(owner: object, resource: object) -> _OwnedResource:
    matches = [
        record
        for record in _RESOURCE_REGISTRY.values()
        if record.owner is owner
        and record.resource is resource
        and record.state is _ResourceState.PENDING
    ]
    if len(matches) != 1:
        raise PathPolicyError("path-policy closeable ownership is invalid")
    return matches[0]


def _one_shot_close(record: _OwnedResource, closer: Callable[[], None]) -> None:
    _synchronize_resource_pid()
    with _RESOURCE_LOCK:
        action_pid = os.getpid()
        if (
            record.owner_pid != action_pid
            or record.state is not _ResourceState.PENDING
        ):
            raise PathPolicyError("path-policy resource close is not authorized")
        record.state = _ResourceState.AMBIGUOUS
        try:
            closer()
        except BaseException:
            raise
        if (
            os.getpid() != action_pid
            or record.owner_pid != action_pid
            or record.state is not _ResourceState.AMBIGUOUS
            or _RESOURCE_REGISTRY.get(record.token) is not record
        ):
            raise PathPolicyError(
                "path-policy resource close crossed an authority boundary"
            )
        record.state = _ResourceState.CLOSED
        del _RESOURCE_REGISTRY[record.token]


def _close_fd(owner: object, descriptor: int) -> None:
    with _RESOURCE_LOCK:
        record = _find_fd_record(owner, descriptor)
    _one_shot_close(record, lambda: os.close(descriptor))


def _close_closeable(owner: object, resource: object) -> None:
    with _RESOURCE_LOCK:
        record = _find_closeable_record(owner, resource)
    _one_shot_close(record, lambda: resource.close())  # type: ignore[attr-defined]


@dataclass(frozen=True, slots=True)
class PathMetadata:
    relative_path: str
    st_dev: int
    st_ino: int
    mode: int
    nlink: int
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class _RootIdentity:
    st_dev: int
    st_ino: int
    mode: int
    uid: int


@dataclass(frozen=True, slots=True)
class _CanaryRecord:
    root: RootKind
    relative_path: str
    metadata: PathMetadata
    digest: str
    creator_pid: int
    fingerprint: str


@dataclass(frozen=True, slots=True)
class _SafeCanaryReceipt:
    _policy: PathPolicy
    _authority: object
    _root: RootKind
    _relative_path: str
    _metadata: PathMetadata
    _digest: str
    _creator_pid: int
    _fingerprint: str

    def __copy__(self) -> Never:
        raise TypeError("safe canary receipt cannot be copied")

    def __deepcopy__(self, _memo: object) -> Never:
        raise TypeError("safe canary receipt cannot be copied")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> Never:
        raise TypeError("safe canary receipt cannot be pickled")


_CANARY_POLICIES: weakref.WeakSet[PathPolicy] = weakref.WeakSet()


def _before_path_policy_fork() -> None:
    _synchronize_resource_pid()
    _RESOURCE_LOCK.acquire()


def _after_path_policy_fork_parent() -> None:
    _RESOURCE_LOCK.release()


def _after_path_policy_fork_child() -> None:
    global _RESOURCE_PROCESS_PID
    child_pid = os.getpid()
    _RESOURCE_PROCESS_PID = child_pid
    try:
        for record in _RESOURCE_REGISTRY.values():
            if record.state is not _ResourceState.CLOSED:
                record.owner_pid = child_pid
                record.state = _ResourceState.AMBIGUOUS
        try:
            for policy in _CANARY_POLICIES:
                try:
                    policy._rotate_canary_authority(child_pid)
                except BaseException:
                    # Lazy PID validation repeats fail-closed on first child use.
                    pass
        except BaseException:
            # Weak-set iteration failure is also covered by lazy PID validation.
            pass
    finally:
        _RESOURCE_LOCK.release()


try:
    os.register_at_fork(
        before=_before_path_policy_fork,
        after_in_parent=_after_path_policy_fork_parent,
        after_in_child=_after_path_policy_fork_child,
    )
except (AttributeError, OSError):
    # Non-POSIX or registration-denied runtimes use the lazy PID boundary.
    pass


def _metadata(relative: str, value: os.stat_result) -> PathMetadata:
    return PathMetadata(
        relative_path=relative,
        st_dev=value.st_dev,
        st_ino=value.st_ino,
        mode=value.st_mode,
        nlink=value.st_nlink,
        size=value.st_size,
        mtime_ns=value.st_mtime_ns,
    )


def _same_metadata(left: PathMetadata, right: PathMetadata) -> bool:
    return left == right


def _same_stat(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_nlink,
        left.st_size,
        left.st_mtime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_nlink,
        right.st_size,
        right.st_mtime_ns,
    )


def _consume_safe_canary_receipt(receipt: object) -> str:
    try:
        if type(receipt) is not _SafeCanaryReceipt:
            raise PathPolicyError("safe canary receipt is invalid")
        policy = cast("PathPolicy", object.__getattribute__(receipt, "_policy"))
        if type(policy) is not PathPolicy:
            raise PathPolicyError("safe canary receipt is invalid")
        return policy._consume_safe_canary_receipt(receipt)
    except PathPolicyError:
        raise
    except BaseException as error:
        raise PathPolicyError("safe canary receipt is invalid") from error


class PathPolicy:
    """Classify relative names against verified real-login and proxy roots."""

    def __init__(
        self,
        *,
        real_login_root: Path,
        proxy_owned_root: Path,
        max_snapshot_entries: int = _DEFAULT_MAX_SNAPSHOT_ENTRIES,
        max_depth: int = _DEFAULT_MAX_DEPTH,
        max_content_bytes: int = _DEFAULT_MAX_CONTENT_BYTES,
    ) -> None:
        self._resource_owner = object()
        _require_resource_health(self._resource_owner)
        for value, label in (
            (max_snapshot_entries, "snapshot entry limit"),
            (max_depth, "snapshot depth limit"),
            (max_content_bytes, "content byte limit"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise PathPolicyError(f"{label} must be a positive integer")
        if max_depth > _MAX_SNAPSHOT_DEPTH:
            raise PathPolicyError(
                f"snapshot depth limit must be at most {_MAX_SNAPSHOT_DEPTH}"
            )
        login_fd, login_root = self._validate_root(
            real_login_root, RootKind.REAL_LOGIN
        )
        try:
            proxy_fd, proxy_root = self._validate_root(
                proxy_owned_root, RootKind.PROXY_OWNED
            )
            try:
                login_identity = login_root[1]
                proxy_identity = proxy_root[1]
                if (
                    login_identity.st_dev,
                    login_identity.st_ino,
                ) == (proxy_identity.st_dev, proxy_identity.st_ino):
                    raise PathPolicyError("policy roots must be disjoint")
                if self._is_ancestor(login_identity, proxy_fd) or self._is_ancestor(
                    proxy_identity, login_fd
                ):
                    raise PathPolicyError("policy roots must be disjoint")
            finally:
                _close_fd(self._resource_owner, proxy_fd)
        finally:
            _close_fd(self._resource_owner, login_fd)
        self._roots = {
            RootKind.REAL_LOGIN: login_root,
            RootKind.PROXY_OWNED: proxy_root,
        }
        self._max_snapshot_entries = max_snapshot_entries
        self._max_depth = max_depth
        self._max_content_bytes = max_content_bytes
        self._approved_new: set[tuple[RootKind, tuple[str, ...]]] = set()
        self._canary_authority = object()
        self._canary_key = secrets.token_bytes(32)
        self._canary_creator_pid = os.getpid()
        self._canary_records: dict[str, _CanaryRecord] = {}
        _CANARY_POLICIES.add(self)

    def _rotate_canary_authority(self, creator_pid: int) -> None:
        self._canary_records.clear()
        self._canary_authority = object()
        self._canary_key = b""
        self._canary_creator_pid = creator_pid
        self._canary_key = secrets.token_bytes(32)

    def _require_canary_creator(self) -> None:
        current_pid = os.getpid()
        if (
            current_pid != self._canary_creator_pid
            or len(self._canary_key) != 32
        ):
            self._rotate_canary_authority(current_pid)
            raise PathPolicyError("safe canary authority changed process")

    @staticmethod
    def _root_flags() -> int:
        return (
            os.O_RDONLY
            | os.O_CLOEXEC
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )

    def _validate_root(
        self, path: Path, kind: RootKind
    ) -> tuple[int, tuple[Path, _RootIdentity]]:
        if not isinstance(path, Path) or not path.is_absolute():
            raise PathPolicyError("policy roots must be absolute paths")
        try:
            descriptor = _acquire_fd(
                self._resource_owner,
                "validation_root",
                lambda: os.open(path, self._root_flags()),
            )
        except OSError as error:
            raise PathPolicyError(
                "policy root cannot be opened without following"
            ) from error
        try:
            value = os.fstat(descriptor)
        except BaseException:
            _close_fd(self._resource_owner, descriptor)
            raise
        if not stat.S_ISDIR(value.st_mode) or value.st_uid != os.getuid():
            _close_fd(self._resource_owner, descriptor)
            raise PathPolicyError("policy root must be a current-user directory")
        permissions = stat.S_IMODE(value.st_mode)
        if kind is RootKind.PROXY_OWNED and permissions != 0o700:
            _close_fd(self._resource_owner, descriptor)
            raise PathPolicyError("proxy-owned root mode must be 0700")
        if kind is RootKind.REAL_LOGIN and permissions & 0o022:
            _close_fd(self._resource_owner, descriptor)
            raise PathPolicyError("real login root cannot be group/world writable")
        return descriptor, (
            path,
            _RootIdentity(value.st_dev, value.st_ino, value.st_mode, value.st_uid),
        )

    def _is_ancestor(self, ancestor: _RootIdentity, descendant_fd: int) -> bool:
        current = _acquire_fd(
            self._resource_owner,
            "ancestor_dup",
            lambda: os.dup(descendant_fd),
        )
        try:
            for _ in range(_MAX_ROOT_PARENT_WALK):
                value = os.fstat(current)
                if (value.st_dev, value.st_ino) == (
                    ancestor.st_dev,
                    ancestor.st_ino,
                ):
                    return True
                parent = _acquire_fd(
                    self._resource_owner,
                    "ancestor_parent",
                    lambda: os.open("..", self._root_flags(), dir_fd=current),
                )
                try:
                    parent_value = os.fstat(parent)
                except BaseException:
                    _close_fd(self._resource_owner, parent)
                    raise
                if (parent_value.st_dev, parent_value.st_ino) == (
                    value.st_dev,
                    value.st_ino,
                ):
                    _close_fd(self._resource_owner, parent)
                    return False
                previous = current
                current = parent
                _close_fd(self._resource_owner, previous)
        finally:
            _close_fd(self._resource_owner, current)
        raise PathPolicyError("policy root disjointness could not be proved")

    @contextmanager
    def _open_root(self, kind: RootKind) -> Iterator[tuple[int, _RootIdentity]]:
        if not isinstance(kind, RootKind):
            raise PathPolicyError("unknown path-policy root")
        path, expected = self._roots[kind]
        try:
            descriptor = _acquire_fd(
                self._resource_owner,
                "reopened_root",
                lambda: os.open(path, self._root_flags()),
            )
        except OSError as error:
            raise PathPolicyError("policy root identity changed") from error
        try:
            actual = os.fstat(descriptor)
            if (
                actual.st_dev,
                actual.st_ino,
                actual.st_mode,
                actual.st_uid,
            ) != (expected.st_dev, expected.st_ino, expected.mode, expected.uid):
                raise PathPolicyError("policy root identity changed")
            yield descriptor, expected
        finally:
            _close_fd(self._resource_owner, descriptor)

    @staticmethod
    def _components(path: Path | str) -> tuple[str, ...]:
        try:
            raw = os.fspath(path)
        except TypeError as error:
            raise PathPolicyError("relative path must be a path value") from error
        if not isinstance(raw, str):
            raise PathPolicyError("relative path must be text")
        if (
            not raw
            or raw == "."
            or raw.startswith("/")
            or "\0" in raw
            or "//" in raw
        ):
            raise PathPolicyError("relative path is empty, absolute, or ambiguous")
        components = tuple(raw.split("/"))
        if (
            any(component in {"", ".", ".."} for component in components)
            or len(components) > 64
            or len(raw.encode("utf-8")) > 4_096
            or any(len(component.encode("utf-8")) > 255 for component in components)
        ):
            raise PathPolicyError("relative path contains an ambiguous component")
        return components

    @staticmethod
    def _require_root(root: RootKind) -> None:
        if not isinstance(root, RootKind):
            raise PathPolicyError("unknown path-policy root")

    @staticmethod
    def _classify_components(
        components: tuple[str, ...], kind: RootKind
    ) -> PathClass:
        lowered = tuple(component.casefold() for component in components)
        if kind is RootKind.REAL_LOGIN:
            if any(component in _PROHIBITED_DIRECTORIES for component in lowered):
                return PathClass.PROHIBITED_CONTENT
            if lowered[-1] in _PROHIBITED_FILES:
                return PathClass.PROHIBITED_CONTENT
            if (
                lowered[-1] in _CREDENTIAL_FILES
                or any(component in _CREDENTIAL_DIRECTORIES for component in lowered)
            ):
                return PathClass.CREDENTIAL_METADATA_ONLY
            return PathClass.UNKNOWN
        if (
            lowered[-1] in _CREDENTIAL_FILES
            or lowered[-1] in _PROHIBITED_FILES
            or any(
                component in _CREDENTIAL_DIRECTORIES | _PROHIBITED_DIRECTORIES
                for component in lowered
            )
        ):
            return PathClass.PROHIBITED_CONTENT
        if lowered[0] in _PROXY_SAFE_DIRECTORIES:
            return PathClass.PROXY_OWNED_SAFE
        return PathClass.UNKNOWN

    def classify(
        self, path: Path | str, *, root: RootKind = RootKind.REAL_LOGIN
    ) -> PathClass:
        _require_resource_health(self._resource_owner)
        self._require_root(root)
        components = self._components(path)
        if (root, components) in self._approved_new:
            classification = PathClass.NEW_NONCREDENTIAL_SAFE
        else:
            classification = self._classify_components(components, root)
        try:
            self._stat(components, root=root)
        except FileNotFoundError:
            _require_resource_health(self._resource_owner)
        return classification

    @staticmethod
    def _component_flags(*, directory: bool) -> int:
        flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        if directory:
            flags |= getattr(os, "O_DIRECTORY", 0)
        return flags

    def _walk_parent(
        self,
        root_fd: int,
        root_identity: _RootIdentity,
        components: tuple[str, ...],
    ) -> tuple[int, str]:
        current = _acquire_fd(
            self._resource_owner,
            "component_dup",
            lambda: os.dup(root_fd),
        )
        try:
            for component in components[:-1]:
                before = os.stat(component, dir_fd=current, follow_symlinks=False)
                if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                    raise PathPolicyError(
                        "symlink or nondirectory traversal is forbidden"
                    )
                if before.st_dev != root_identity.st_dev:
                    raise PathPolicyError("path crossed the verified root device")
                child = _acquire_fd(
                    self._resource_owner,
                    "component_child",
                    lambda: os.open(
                        component,
                        self._component_flags(directory=True),
                        dir_fd=current,
                    ),
                )
                try:
                    after = os.fstat(child)
                    if (after.st_dev, after.st_ino, after.st_mode) != (
                        before.st_dev,
                        before.st_ino,
                        before.st_mode,
                    ):
                        raise PathPolicyError(
                            "path identity changed during traversal"
                        )
                except BaseException:
                    _close_fd(self._resource_owner, child)
                    raise
                previous = current
                current = child
                _close_fd(self._resource_owner, previous)
            return current, components[-1]
        except BaseException:
            _close_fd(self._resource_owner, current)
            raise

    def _stat(
        self, components: tuple[str, ...], *, root: RootKind
    ) -> os.stat_result:
        with self._open_root(root) as (root_fd, root_identity):
            parent_fd, name = self._walk_parent(root_fd, root_identity, components)
            try:
                value = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            finally:
                _close_fd(self._resource_owner, parent_fd)
        if stat.S_ISLNK(value.st_mode):
            raise PathPolicyError("symlinks are forbidden")
        if not (stat.S_ISREG(value.st_mode) or stat.S_ISDIR(value.st_mode)):
            raise PathPolicyError("special filesystem objects are forbidden")
        if value.st_dev != root_identity.st_dev:
            raise PathPolicyError("path crossed the verified root device")
        if stat.S_ISREG(value.st_mode) and value.st_nlink != 1:
            raise PathPolicyError("hard link escape is forbidden")
        return value

    def metadata(
        self, path: Path | str, *, root: RootKind = RootKind.REAL_LOGIN
    ) -> PathMetadata:
        _require_resource_health(self._resource_owner)
        components = self._components(path)
        value = self._stat(components, root=root)
        return _metadata("/".join(components), value)

    def _verify_proxy_file(self, value: os.stat_result) -> None:
        if not stat.S_ISREG(value.st_mode):
            raise PathPolicyError("content path must be a regular file")
        if value.st_uid != os.getuid():
            raise PathPolicyError("content path owner changed")
        if stat.S_IMODE(value.st_mode) & 0o077:
            raise PathPolicyError("content path cannot grant group/world access")
        if value.st_nlink != 1:
            raise PathPolicyError("hard link escape is forbidden")

    def may_open_content(
        self, path: Path | str, *, root: RootKind = RootKind.REAL_LOGIN
    ) -> bool:
        _require_resource_health(self._resource_owner)
        components = self._components(path)
        classification = self.classify(path, root=root)
        if root is not RootKind.PROXY_OWNED or classification not in {
            PathClass.PROXY_OWNED_SAFE,
            PathClass.NEW_NONCREDENTIAL_SAFE,
        }:
            return False
        try:
            value = self._stat(components, root=root)
        except FileNotFoundError:
            if classification is not PathClass.NEW_NONCREDENTIAL_SAFE:
                return False
            with self._open_root(root) as (root_fd, identity):
                parent_fd, _ = self._walk_parent(root_fd, identity, components)
                _close_fd(self._resource_owner, parent_fd)
            return True
        self._verify_proxy_file(value)
        return True

    def approve_new_noncredential(
        self, path: Path | str, *, root: RootKind = RootKind.PROXY_OWNED
    ) -> None:
        _require_resource_health(self._resource_owner)
        self._require_root(root)
        components = self._components(path)
        if root is RootKind.REAL_LOGIN:
            raise PathPolicyError("new content is forbidden under the real login root")
        lexical = self._classify_components(components, root)
        if lexical is PathClass.PROHIBITED_CONTENT:
            raise PathPolicyError("credential or prompt-like new path is forbidden")
        try:
            self._stat(components, root=root)
        except FileNotFoundError:
            pass
        else:
            raise PathPolicyError("new noncredential path must not already exist")
        with self._open_root(root) as (root_fd, identity):
            parent_fd, _ = self._walk_parent(root_fd, identity, components)
            _close_fd(self._resource_owner, parent_fd)
        if len(self._approved_new) >= self._max_snapshot_entries:
            raise PathPolicyError("new path classification limit exceeded")
        self._approved_new.add((root, components))

    def scan_for_canary(
        self,
        path: Path | str,
        canary: bytes,
        *,
        expected: PathMetadata,
        root: RootKind = RootKind.PROXY_OWNED,
    ) -> bool:
        _require_resource_health(self._resource_owner)
        components = self._components(path)
        if not isinstance(canary, bytes) or not canary or len(canary) > 256:
            raise PathPolicyError("canary must be 1..256 bytes")
        if type(expected) is not PathMetadata:
            raise PathPolicyError("canary metadata must be an exact observation")
        if not self.may_open_content(path, root=root):
            raise PathPolicyError("path is not content-safe")
        current = self.metadata(path, root=root)
        if not _same_metadata(current, expected):
            raise PathPolicyError("path identity changed before content scan")
        with self._open_root(root) as (root_fd, root_identity):
            parent_fd, name = self._walk_parent(root_fd, root_identity, components)
            try:
                descriptor = _acquire_fd(
                    self._resource_owner,
                    "content_file",
                    lambda: os.open(
                        name,
                        self._component_flags(directory=False),
                        dir_fd=parent_fd,
                    ),
                )
            except BaseException:
                _close_fd(self._resource_owner, parent_fd)
                raise
            try:
                _close_fd(self._resource_owner, parent_fd)
                opened = os.fstat(descriptor)
                opened_metadata = _metadata("/".join(components), opened)
                self._verify_proxy_file(opened)
                if not _same_metadata(opened_metadata, expected):
                    raise PathPolicyError("path identity changed before content scan")
                if opened.st_size > self._max_content_bytes:
                    raise PathPolicyError("content path exceeds scan byte limit")
                remaining = self._max_content_bytes + 1
                chunks: list[bytes] = []
                while remaining > 0:
                    chunk = os.read(descriptor, min(65_536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if remaining == 0 and os.read(descriptor, 1):
                    raise PathPolicyError("content path exceeds scan byte limit")
                after = os.fstat(descriptor)
                if not _same_metadata(
                    _metadata("/".join(components), after), expected
                ):
                    raise PathPolicyError("path identity changed during content scan")
                return canary in b"".join(chunks)
            finally:
                _close_fd(self._resource_owner, descriptor)

    def _canary_fingerprint(
        self,
        *,
        root: RootKind,
        relative_path: str,
        metadata: PathMetadata,
        digest: str,
        creator_pid: int,
    ) -> str:
        fields = (
            root.value,
            relative_path,
            str(metadata.st_dev),
            str(metadata.st_ino),
            str(metadata.mode),
            str(metadata.nlink),
            str(metadata.size),
            str(metadata.mtime_ns),
            digest,
            str(creator_pid),
        )
        payload = "\0".join(fields).encode("utf-8")
        return hmac.new(self._canary_key, payload, hashlib.sha256).hexdigest()

    def _mint_safe_canary_receipt(
        self,
        path: Path,
        canary: bytes,
        *,
        expected: PathMetadata,
        root: RootKind,
    ) -> object:
        _require_resource_health(self._resource_owner)
        self._require_canary_creator()
        if root is not RootKind.PROXY_OWNED:
            raise PathPolicyError("safe canary requires the proxy-owned root")
        components = self._components(path)
        relative_path = "/".join(components)
        if expected.relative_path != relative_path:
            raise PathPolicyError("safe canary metadata path changed")
        if not self.scan_for_canary(path, canary, expected=expected, root=root):
            raise PathPolicyError("safe canary was not found")
        current = self.metadata(path, root=root)
        if current != expected:
            raise PathPolicyError("safe canary identity changed")
        digest = hashlib.sha256(canary).hexdigest()
        fingerprint = self._canary_fingerprint(
            root=root,
            relative_path=relative_path,
            metadata=current,
            digest=digest,
            creator_pid=self._canary_creator_pid,
        )
        if len(self._canary_records) >= self._max_snapshot_entries:
            raise PathPolicyError("safe canary receipt limit exceeded")
        record = _CanaryRecord(
            root=root,
            relative_path=relative_path,
            metadata=current,
            digest=digest,
            creator_pid=self._canary_creator_pid,
            fingerprint=fingerprint,
        )
        self._canary_records[fingerprint] = record
        return _SafeCanaryReceipt(
            _policy=self,
            _authority=self._canary_authority,
            _root=root,
            _relative_path=relative_path,
            _metadata=current,
            _digest=digest,
            _creator_pid=self._canary_creator_pid,
            _fingerprint=fingerprint,
        )

    def _consume_safe_canary_receipt(self, receipt: object) -> str:
        try:
            _require_resource_health(self._resource_owner)
            self._require_canary_creator()
            if type(receipt) is not _SafeCanaryReceipt:
                raise PathPolicyError("safe canary receipt is invalid")
            authority = object.__getattribute__(receipt, "_authority")
            root = object.__getattribute__(receipt, "_root")
            relative_path = object.__getattribute__(receipt, "_relative_path")
            metadata = object.__getattribute__(receipt, "_metadata")
            digest = object.__getattribute__(receipt, "_digest")
            creator_pid = object.__getattribute__(receipt, "_creator_pid")
            fingerprint = object.__getattribute__(receipt, "_fingerprint")
            policy = object.__getattribute__(receipt, "_policy")
            if (
                policy is not self
                or authority is not self._canary_authority
                or root is not RootKind.PROXY_OWNED
                or type(relative_path) is not str
                or type(metadata) is not PathMetadata
                or type(digest) is not str
                or type(creator_pid) is not int
                or type(fingerprint) is not str
                or creator_pid != os.getpid()
                or creator_pid != self._canary_creator_pid
            ):
                raise PathPolicyError("safe canary receipt is invalid")
            verified_root = cast(RootKind, root)
            verified_path = relative_path
            verified_metadata = metadata
            verified_digest = digest
            verified_creator_pid = creator_pid
            verified_fingerprint = fingerprint
            record = self._canary_records.get(verified_fingerprint)
            if record is None or (
                verified_root,
                verified_path,
                verified_metadata,
                verified_digest,
                verified_creator_pid,
                verified_fingerprint,
            ) != (
                record.root,
                record.relative_path,
                record.metadata,
                record.digest,
                record.creator_pid,
                record.fingerprint,
            ):
                raise PathPolicyError("safe canary receipt is invalid")
            expected_fingerprint = self._canary_fingerprint(
                root=verified_root,
                relative_path=verified_path,
                metadata=verified_metadata,
                digest=verified_digest,
                creator_pid=verified_creator_pid,
            )
            if not hmac.compare_digest(verified_fingerprint, expected_fingerprint):
                raise PathPolicyError("safe canary receipt is invalid")
            current = self.metadata(Path(verified_path), root=verified_root)
            if current != verified_metadata:
                raise PathPolicyError("safe canary receipt is invalid")
            del self._canary_records[verified_fingerprint]
            return cast(str, record.digest)
        except PathPolicyError:
            raise
        except BaseException as error:
            raise PathPolicyError("safe canary receipt is invalid") from error

    def _snapshot_names(
        self, directory_fd: int, *, limit: int, overflow_message: str
    ) -> tuple[str, ...]:
        entries = _acquire_closeable(
            self._resource_owner,
            "snapshot_scandir",
            lambda: os.scandir(directory_fd),
        )
        try:
            captured = list(islice(entries, limit + 1))
        finally:
            _close_closeable(self._resource_owner, entries)
        if len(captured) > limit:
            raise PathPolicyError(overflow_message)
        names: list[str] = []
        for entry in captured:
            name = entry.name
            if type(name) is not str or name in {"", ".", ".."} or "/" in name:
                raise PathPolicyError("snapshot encountered an ambiguous name")
            names.append(name)
        if len(set(names)) != len(names):
            raise PathPolicyError("snapshot encountered duplicate names")
        return tuple(sorted(names))

    @staticmethod
    def _verify_snapshot_entry(
        value: os.stat_result, root_identity: _RootIdentity
    ) -> None:
        if stat.S_ISLNK(value.st_mode):
            raise PathPolicyError("snapshot encountered a symlink")
        if not (stat.S_ISREG(value.st_mode) or stat.S_ISDIR(value.st_mode)):
            raise PathPolicyError(
                "snapshot encountered a special filesystem object"
            )
        if value.st_dev != root_identity.st_dev:
            raise PathPolicyError("snapshot crossed the verified root device")
        if stat.S_ISREG(value.st_mode) and value.st_nlink != 1:
            raise PathPolicyError("hard link escape is forbidden")

    @staticmethod
    def _snapshot_stat(directory_fd: int, name: str) -> os.stat_result:
        try:
            return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as error:
            raise PathPolicyError(
                "snapshot directory changed during snapshot"
            ) from error

    def _snapshot_directory(
        self,
        directory_fd: int,
        *,
        prefix: tuple[str, ...],
        depth: int,
        root: RootKind,
        root_identity: _RootIdentity,
        output: list[PathMetadata],
    ) -> os.stat_result:
        before = os.fstat(directory_fd)
        if not stat.S_ISDIR(before.st_mode) or before.st_dev != root_identity.st_dev:
            raise PathPolicyError("snapshot directory identity is invalid")
        remaining = self._max_snapshot_entries - len(output)
        before_names = self._snapshot_names(
            directory_fd,
            limit=remaining,
            overflow_message="snapshot entry limit exceeded",
        )
        if depth >= self._max_depth and before_names:
            raise PathPolicyError("snapshot depth limit exceeded")

        admitted: dict[str, os.stat_result] = {}
        for name in before_names:
            if len(output) >= self._max_snapshot_entries:
                raise PathPolicyError("snapshot entry limit exceeded")
            components = (*prefix, name)
            captured = self._snapshot_stat(directory_fd, name)
            self._verify_snapshot_entry(captured, root_identity)
            if (root, components) in self._approved_new:
                classification = PathClass.NEW_NONCREDENTIAL_SAFE
            else:
                classification = self._classify_components(components, root)
            if classification is PathClass.UNKNOWN:
                raise PathPolicyError(
                    "snapshot encountered an unknown existing path"
                )
            final = self._snapshot_stat(directory_fd, name)
            if not _same_stat(captured, final):
                raise PathPolicyError("path identity changed during snapshot")
            relative = "/".join(components)
            if stat.S_ISREG(final.st_mode):
                output.append(_metadata(relative, final))
                admitted[name] = final
                continue

            try:
                child = _acquire_fd(
                    self._resource_owner,
                    "snapshot_child",
                    lambda: os.open(
                        name,
                        self._component_flags(directory=True),
                        dir_fd=directory_fd,
                    ),
                )
            except OSError as error:
                raise PathPolicyError(
                    "snapshot directory changed during snapshot"
                ) from error
            try:
                opened = os.fstat(child)
                if not _same_stat(opened, final):
                    raise PathPolicyError("path identity changed during snapshot")
                index = len(output)
                output.append(_metadata(relative, opened))
                certified = self._snapshot_directory(
                    child,
                    prefix=components,
                    depth=depth + 1,
                    root=root,
                    root_identity=root_identity,
                    output=output,
                )
                linked = self._snapshot_stat(directory_fd, name)
                if not _same_stat(certified, linked):
                    raise PathPolicyError("snapshot directory changed during snapshot")
                output[index] = _metadata(relative, certified)
                admitted[name] = certified
            finally:
                _close_fd(self._resource_owner, child)

        after_names = self._snapshot_names(
            directory_fd,
            limit=len(before_names),
            overflow_message="snapshot directory changed during snapshot",
        )
        if before_names != after_names:
            raise PathPolicyError("snapshot directory changed during snapshot")
        for name in after_names:
            if not _same_stat(admitted[name], self._snapshot_stat(directory_fd, name)):
                raise PathPolicyError("snapshot directory changed during snapshot")
        after = os.fstat(directory_fd)
        if not _same_stat(before, after):
            raise PathPolicyError("snapshot directory changed during snapshot")
        return after

    def snapshot(
        self, *, root: RootKind = RootKind.REAL_LOGIN
    ) -> tuple[PathMetadata, ...]:
        _require_resource_health(self._resource_owner)
        output: list[PathMetadata] = []
        with self._open_root(root) as (root_fd, root_identity):
            output.append(_metadata(SNAPSHOT_ROOT_SENTINEL, os.fstat(root_fd)))
            certified_root = self._snapshot_directory(
                root_fd,
                prefix=(),
                depth=0,
                root=root,
                root_identity=root_identity,
                output=output,
            )
            output[0] = _metadata(SNAPSHOT_ROOT_SENTINEL, certified_root)
        return tuple(sorted(output, key=lambda item: item.relative_path))


__all__ = [
    "PATH_POLICY_VERSION",
    "SNAPSHOT_ROOT_SENTINEL",
    "PathClass",
    "PathMetadata",
    "PathPolicy",
    "PathPolicyError",
    "RootKind",
]
