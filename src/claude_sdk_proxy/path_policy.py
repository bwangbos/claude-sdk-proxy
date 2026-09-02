"""Versioned no-follow policy for login metadata and proxy-owned canaries."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from itertools import islice
from pathlib import Path
from typing import Final, Never, SupportsIndex, cast

PATH_POLICY_VERSION: Final = 1
_DEFAULT_MAX_SNAPSHOT_ENTRIES: Final = 2_048
_DEFAULT_MAX_DEPTH: Final = 16
_DEFAULT_MAX_CONTENT_BYTES: Final = 1_048_576
_MAX_ROOT_PARENT_WALK: Final = 256

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
    fingerprint: str


@dataclass(frozen=True, slots=True)
class _SafeCanaryReceipt:
    _policy: PathPolicy
    _authority: object
    _root: RootKind
    _relative_path: str
    _metadata: PathMetadata
    _digest: str
    _fingerprint: str

    def __copy__(self) -> Never:
        raise TypeError("safe canary receipt cannot be copied")

    def __deepcopy__(self, _memo: object) -> Never:
        raise TypeError("safe canary receipt cannot be copied")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> Never:
        raise TypeError("safe canary receipt cannot be pickled")


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
        for value, label in (
            (max_snapshot_entries, "snapshot entry limit"),
            (max_depth, "snapshot depth limit"),
            (max_content_bytes, "content byte limit"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise PathPolicyError(f"{label} must be a positive integer")
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
                os.close(proxy_fd)
        finally:
            os.close(login_fd)
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
        self._canary_records: dict[str, _CanaryRecord] = {}

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
            descriptor = os.open(path, self._root_flags())
        except OSError as error:
            raise PathPolicyError(
                "policy root cannot be opened without following"
            ) from error
        try:
            value = os.fstat(descriptor)
        except BaseException:
            os.close(descriptor)
            raise
        if not stat.S_ISDIR(value.st_mode) or value.st_uid != os.getuid():
            os.close(descriptor)
            raise PathPolicyError("policy root must be a current-user directory")
        permissions = stat.S_IMODE(value.st_mode)
        if kind is RootKind.PROXY_OWNED and permissions != 0o700:
            os.close(descriptor)
            raise PathPolicyError("proxy-owned root mode must be 0700")
        if kind is RootKind.REAL_LOGIN and permissions & 0o022:
            os.close(descriptor)
            raise PathPolicyError("real login root cannot be group/world writable")
        return descriptor, (
            path,
            _RootIdentity(value.st_dev, value.st_ino, value.st_mode, value.st_uid),
        )

    @classmethod
    def _is_ancestor(cls, ancestor: _RootIdentity, descendant_fd: int) -> bool:
        current = os.dup(descendant_fd)
        try:
            for _ in range(_MAX_ROOT_PARENT_WALK):
                value = os.fstat(current)
                if (value.st_dev, value.st_ino) == (
                    ancestor.st_dev,
                    ancestor.st_ino,
                ):
                    return True
                parent = os.open("..", cls._root_flags(), dir_fd=current)
                try:
                    parent_value = os.fstat(parent)
                except BaseException:
                    os.close(parent)
                    raise
                if (parent_value.st_dev, parent_value.st_ino) == (
                    value.st_dev,
                    value.st_ino,
                ):
                    os.close(parent)
                    return False
                os.close(current)
                current = parent
        finally:
            os.close(current)
        raise PathPolicyError("policy root disjointness could not be proved")

    @contextmanager
    def _open_root(self, kind: RootKind) -> Iterator[tuple[int, _RootIdentity]]:
        if not isinstance(kind, RootKind):
            raise PathPolicyError("unknown path-policy root")
        path, expected = self._roots[kind]
        try:
            descriptor = os.open(path, self._root_flags())
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
            os.close(descriptor)

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
        self._require_root(root)
        components = self._components(path)
        if (root, components) in self._approved_new:
            classification = PathClass.NEW_NONCREDENTIAL_SAFE
        else:
            classification = self._classify_components(components, root)
        try:
            self._stat(components, root=root)
        except FileNotFoundError:
            pass
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
        current = os.dup(root_fd)
        try:
            for component in components[:-1]:
                before = os.stat(component, dir_fd=current, follow_symlinks=False)
                if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                    raise PathPolicyError(
                        "symlink or nondirectory traversal is forbidden"
                    )
                if before.st_dev != root_identity.st_dev:
                    raise PathPolicyError("path crossed the verified root device")
                child = os.open(
                    component,
                    self._component_flags(directory=True),
                    dir_fd=current,
                )
                after = os.fstat(child)
                if (after.st_dev, after.st_ino, after.st_mode) != (
                    before.st_dev,
                    before.st_ino,
                    before.st_mode,
                ):
                    os.close(child)
                    raise PathPolicyError("path identity changed during traversal")
                os.close(current)
                current = child
            return current, components[-1]
        except BaseException:
            os.close(current)
            raise

    def _stat(
        self, components: tuple[str, ...], *, root: RootKind
    ) -> os.stat_result:
        with self._open_root(root) as (root_fd, root_identity):
            parent_fd, name = self._walk_parent(root_fd, root_identity, components)
            try:
                value = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            finally:
                os.close(parent_fd)
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
                os.close(parent_fd)
            return True
        self._verify_proxy_file(value)
        return True

    def approve_new_noncredential(
        self, path: Path | str, *, root: RootKind = RootKind.PROXY_OWNED
    ) -> None:
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
            os.close(parent_fd)
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
                descriptor = os.open(
                    name,
                    self._component_flags(directory=False),
                    dir_fd=parent_fd,
                )
            finally:
                os.close(parent_fd)
            try:
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
                os.close(descriptor)

    def _canary_fingerprint(
        self,
        *,
        root: RootKind,
        relative_path: str,
        metadata: PathMetadata,
        digest: str,
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
        )
        if len(self._canary_records) >= self._max_snapshot_entries:
            raise PathPolicyError("safe canary receipt limit exceeded")
        record = _CanaryRecord(
            root=root,
            relative_path=relative_path,
            metadata=current,
            digest=digest,
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
            _fingerprint=fingerprint,
        )

    def _consume_safe_canary_receipt(self, receipt: object) -> str:
        try:
            if type(receipt) is not _SafeCanaryReceipt:
                raise PathPolicyError("safe canary receipt is invalid")
            authority = object.__getattribute__(receipt, "_authority")
            root = object.__getattribute__(receipt, "_root")
            relative_path = object.__getattribute__(receipt, "_relative_path")
            metadata = object.__getattribute__(receipt, "_metadata")
            digest = object.__getattribute__(receipt, "_digest")
            fingerprint = object.__getattribute__(receipt, "_fingerprint")
            policy = object.__getattribute__(receipt, "_policy")
            if (
                policy is not self
                or authority is not self._canary_authority
                or root is not RootKind.PROXY_OWNED
                or type(relative_path) is not str
                or type(metadata) is not PathMetadata
                or type(digest) is not str
                or type(fingerprint) is not str
            ):
                raise PathPolicyError("safe canary receipt is invalid")
            verified_root = cast(RootKind, root)
            verified_path = relative_path
            verified_metadata = metadata
            verified_digest = digest
            verified_fingerprint = fingerprint
            record = self._canary_records.get(verified_fingerprint)
            if record is None or (
                verified_root,
                verified_path,
                verified_metadata,
                verified_digest,
                verified_fingerprint,
            ) != (
                record.root,
                record.relative_path,
                record.metadata,
                record.digest,
                record.fingerprint,
            ):
                raise PathPolicyError("safe canary receipt is invalid")
            expected_fingerprint = self._canary_fingerprint(
                root=verified_root,
                relative_path=verified_path,
                metadata=verified_metadata,
                digest=verified_digest,
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

    def snapshot(
        self, *, root: RootKind = RootKind.REAL_LOGIN
    ) -> tuple[PathMetadata, ...]:
        output: list[PathMetadata] = []
        with self._open_root(root) as (root_fd, root_identity):
            stack: list[tuple[int, tuple[str, ...], int]] = [
                (os.dup(root_fd), (), 0)
            ]
            try:
                while stack:
                    directory_fd, prefix, depth = stack.pop()
                    try:
                        if depth >= self._max_depth:
                            with os.scandir(directory_fd) as entries:
                                has_entry = next(entries, None) is not None
                            if has_entry:
                                raise PathPolicyError("snapshot depth limit exceeded")
                            continue
                        remaining = self._max_snapshot_entries - len(output)
                        with os.scandir(directory_fd) as entries:
                            names = sorted(
                                entry.name
                                for entry in islice(entries, remaining + 1)
                            )
                        if len(names) > remaining:
                            raise PathPolicyError("snapshot entry limit exceeded")
                        for name in names:
                            components = (*prefix, name)
                            if len(output) >= self._max_snapshot_entries:
                                raise PathPolicyError("snapshot entry limit exceeded")
                            captured = os.stat(
                                name, dir_fd=directory_fd, follow_symlinks=False
                            )
                            if stat.S_ISLNK(captured.st_mode):
                                raise PathPolicyError("snapshot encountered a symlink")
                            if not (
                                stat.S_ISREG(captured.st_mode)
                                or stat.S_ISDIR(captured.st_mode)
                            ):
                                raise PathPolicyError(
                                    "snapshot encountered a special filesystem object"
                                )
                            if captured.st_dev != root_identity.st_dev:
                                raise PathPolicyError(
                                    "snapshot crossed the verified root device"
                                )
                            if (
                                stat.S_ISREG(captured.st_mode)
                                and captured.st_nlink != 1
                            ):
                                raise PathPolicyError("hard link escape is forbidden")
                            if (root, components) in self._approved_new:
                                classification = PathClass.NEW_NONCREDENTIAL_SAFE
                            else:
                                classification = self._classify_components(
                                    components, root
                                )
                            if classification is PathClass.UNKNOWN:
                                raise PathPolicyError(
                                    "snapshot encountered an unknown existing path"
                                )
                            final = os.stat(
                                name, dir_fd=directory_fd, follow_symlinks=False
                            )
                            if not _same_stat(captured, final):
                                raise PathPolicyError(
                                    "path identity changed during snapshot"
                                )
                            if stat.S_ISDIR(final.st_mode):
                                child = os.open(
                                    name,
                                    self._component_flags(directory=True),
                                    dir_fd=directory_fd,
                                )
                                opened = os.fstat(child)
                                if not _same_stat(opened, final):
                                    os.close(child)
                                    raise PathPolicyError(
                                        "path identity changed during snapshot"
                                    )
                                stack.append((child, components, depth + 1))
                            output.append(_metadata("/".join(components), final))
                    finally:
                        os.close(directory_fd)
            except BaseException:
                for directory_fd, _, _ in stack:
                    os.close(directory_fd)
                raise
        return tuple(sorted(output, key=lambda item: item.relative_path))


__all__ = [
    "PATH_POLICY_VERSION",
    "PathClass",
    "PathMetadata",
    "PathPolicy",
    "PathPolicyError",
    "RootKind",
]
