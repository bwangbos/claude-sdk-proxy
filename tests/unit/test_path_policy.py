from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

import claude_sdk_proxy.path_policy as path_policy_impl
from claude_sdk_proxy.path_policy import (
    PathClass,
    PathPolicy,
    PathPolicyError,
    RootKind,
)


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    real_login = tmp_path / "real-login"
    proxy_owned = tmp_path / "proxy-owned"
    real_login.mkdir(mode=0o700)
    proxy_owned.mkdir(mode=0o700)
    return real_login, proxy_owned


@pytest.fixture
def policy(roots: tuple[Path, Path]) -> PathPolicy:
    return PathPolicy(real_login_root=roots[0], proxy_owned_root=roots[1])


def test_roots_must_be_descriptor_proved_disjoint(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    child = root / "child"
    child.mkdir(mode=0o700)
    sibling = tmp_path / "sibling"
    sibling.mkdir(mode=0o700)
    alias_parent = tmp_path / "alias-parent"
    alias_parent.symlink_to(tmp_path, target_is_directory=True)
    alias = alias_parent / "root"

    for real_login, proxy_owned in (
        (root, root),
        (root, child),
        (child, root),
        (root, alias),
    ):
        with pytest.raises(PathPolicyError, match="disjoint"):
            PathPolicy(
                real_login_root=real_login,
                proxy_owned_root=proxy_owned,
            )

    PathPolicy(real_login_root=root, proxy_owned_root=sibling)


def test_credential_paths_are_metadata_only(
    policy: PathPolicy, roots: tuple[Path, Path]
) -> None:
    credential = roots[0] / ".credentials.json"
    credential.write_bytes(b"CREDENTIAL-CONTENT-MUST-NOT-BE-OPENED")
    credential.chmod(0o600)

    assert (
        policy.classify(Path(".credentials.json"))
        is PathClass.CREDENTIAL_METADATA_ONLY
    )
    assert policy.may_open_content(Path(".credentials.json")) is False
    metadata = policy.metadata(Path(".credentials.json"))
    stat_result = credential.lstat()
    assert metadata.relative_path == ".credentials.json"
    assert (
        metadata.st_dev,
        metadata.st_ino,
        metadata.mode,
        metadata.size,
        metadata.mtime_ns,
    ) == (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_mode,
        stat_result.st_size,
        stat_result.st_mtime_ns,
    )
    assert not hasattr(metadata, "content")


@pytest.mark.parametrize(
    "relative",
    [
        "CLAUDE.md",
        "settings.json",
        "settings.local.json",
        ".mcp.json",
        "projects/project-a/memory/notes.md",
        "plugins/plugin-a/config.json",
        "hooks/pre-tool.sh",
        "skills/ambient/SKILL.md",
        "agents/ambient.md",
        "connectors/ambient.json",
        "commands/ambient.md",
        "workflows/ambient.json",
    ],
)
def test_real_login_prompt_and_capability_inputs_are_prohibited(
    policy: PathPolicy, relative: str
) -> None:
    assert policy.classify(Path(relative)) is PathClass.PROHIBITED_CONTENT
    assert policy.may_open_content(Path(relative)) is False


@pytest.mark.parametrize(
    "relative",
    [
        Path(),
        Path("."),
        Path("../escape"),
        Path("a/../escape"),
        Path("/absolute"),
        "a//ambiguous",
        "a/./ambiguous",
        "nul\0component",
    ],
)
def test_ambiguous_and_escaping_paths_are_rejected(
    policy: PathPolicy, relative: Path | str
) -> None:
    with pytest.raises(PathPolicyError, match="relative path"):
        policy.classify(relative)  # type: ignore[arg-type]


def test_invalid_root_selector_and_overlong_component_sets_are_rejected(
    policy: PathPolicy,
) -> None:
    with pytest.raises(PathPolicyError, match="root"):
        policy.classify(Path("settings.json"), root="real_login")  # type: ignore[arg-type]
    with pytest.raises(PathPolicyError, match="relative path"):
        policy.classify(Path(*(["component"] * 65)))


def test_unknown_existing_or_new_paths_are_never_openable(
    policy: PathPolicy, roots: tuple[Path, Path]
) -> None:
    (roots[0] / "future-runtime-state.bin").write_bytes(b"UNKNOWN")

    assert policy.classify(Path("future-runtime-state.bin")) is PathClass.UNKNOWN
    assert policy.may_open_content(Path("future-runtime-state.bin")) is False
    assert (
        policy.classify(Path("new.bin"), root=RootKind.PROXY_OWNED)
        is PathClass.UNKNOWN
    )
    assert policy.may_open_content(Path("new.bin"), root=RootKind.PROXY_OWNED) is False


def test_proxy_owned_canary_path_is_content_safe_and_identity_checked(
    policy: PathPolicy, roots: tuple[Path, Path]
) -> None:
    canaries = roots[1] / "canaries"
    canaries.mkdir(mode=0o700)
    canary = canaries / "purity.txt"
    canary.write_bytes(b"HARMLESS-CANARY")
    canary.chmod(0o600)

    assert (
        policy.classify(Path("canaries/purity.txt"), root=RootKind.PROXY_OWNED)
        is PathClass.PROXY_OWNED_SAFE
    )
    assert policy.may_open_content(
        Path("canaries/purity.txt"), root=RootKind.PROXY_OWNED
    )
    observation = policy.metadata(
        Path("canaries/purity.txt"), root=RootKind.PROXY_OWNED
    )
    assert policy.scan_for_canary(
        Path("canaries/purity.txt"),
        b"HARMLESS-CANARY",
        expected=observation,
        root=RootKind.PROXY_OWNED,
    )


def test_explicit_new_noncredential_classification_requires_absence_and_proxy_root(
    policy: PathPolicy, roots: tuple[Path, Path]
) -> None:
    relative = Path("probe-output/new-artifact.txt")
    (roots[1] / "probe-output").mkdir(mode=0o700)

    policy.approve_new_noncredential(relative, root=RootKind.PROXY_OWNED)
    assert (
        policy.classify(relative, root=RootKind.PROXY_OWNED)
        is PathClass.NEW_NONCREDENTIAL_SAFE
    )
    assert policy.may_open_content(relative, root=RootKind.PROXY_OWNED)

    with pytest.raises(PathPolicyError, match="real login root"):
        policy.approve_new_noncredential(Path("new.txt"), root=RootKind.REAL_LOGIN)


def test_symlink_traversal_and_special_files_are_rejected(
    policy: PathPolicy, roots: tuple[Path, Path]
) -> None:
    outside = roots[0] / ".credentials.json"
    outside.write_bytes(b"SECRET")
    canaries = roots[1] / "canaries"
    canaries.mkdir(mode=0o700)
    (canaries / "link").symlink_to(outside)
    fifo = canaries / "fifo"
    os.mkfifo(fifo, 0o600)

    for relative in (Path("canaries/link"), Path("canaries/fifo")):
        with pytest.raises(PathPolicyError):
            policy.classify(relative, root=RootKind.PROXY_OWNED)
        with pytest.raises(PathPolicyError):
            policy.may_open_content(relative, root=RootKind.PROXY_OWNED)


def test_hard_link_escape_is_rejected(
    policy: PathPolicy, roots: tuple[Path, Path]
) -> None:
    source = roots[0] / ".credentials.json"
    source.write_bytes(b"SECRET")
    canaries = roots[1] / "canaries"
    canaries.mkdir(mode=0o700)
    os.link(source, canaries / "linked.txt")

    with pytest.raises(PathPolicyError, match="hard link"):
        policy.may_open_content(
            Path("canaries/linked.txt"), root=RootKind.PROXY_OWNED
        )


def test_metadata_identity_change_is_detected_before_content_scan(
    policy: PathPolicy, roots: tuple[Path, Path]
) -> None:
    canaries = roots[1] / "canaries"
    canaries.mkdir(mode=0o700)
    target = canaries / "purity.txt"
    target.write_bytes(b"HARMLESS-CANARY")
    target.chmod(0o600)
    observation = policy.metadata(
        Path("canaries/purity.txt"), root=RootKind.PROXY_OWNED
    )
    target.unlink()
    target.write_bytes(b"REPLACEMENT")
    target.chmod(0o600)

    with pytest.raises(PathPolicyError, match="identity changed"):
        policy.scan_for_canary(
            Path("canaries/purity.txt"),
            b"HARMLESS-CANARY",
            expected=observation,
            root=RootKind.PROXY_OWNED,
        )


def test_snapshot_is_metadata_only_bounded_and_rejects_unknown_paths(
    policy: PathPolicy, roots: tuple[Path, Path]
) -> None:
    credential = roots[0] / ".credentials.json"
    credential.write_bytes(b"DO-NOT-READ")
    credential.chmod(0o600)
    (roots[0] / "settings.json").write_bytes(b"PROHIBITED")

    snapshot = policy.snapshot(root=RootKind.REAL_LOGIN)

    assert {entry.relative_path for entry in snapshot} == {
        ".",
        ".credentials.json",
        "settings.json",
    }
    assert all(not hasattr(entry, "content") for entry in snapshot)

    (roots[0] / "unknown.bin").write_bytes(b"UNKNOWN")
    with pytest.raises(PathPolicyError, match="unknown existing path"):
        policy.snapshot(root=RootKind.REAL_LOGIN)


def test_snapshot_enforces_entry_bound(roots: tuple[Path, Path]) -> None:
    policy = PathPolicy(
        real_login_root=roots[0], proxy_owned_root=roots[1], max_snapshot_entries=2
    )
    for name in ("settings.json", "settings.local.json", "CLAUDE.md"):
        (roots[0] / name).write_bytes(b"PROHIBITED")

    with pytest.raises(PathPolicyError, match="snapshot entry limit"):
        policy.snapshot(root=RootKind.REAL_LOGIN)


def test_snapshot_returns_certified_root_sentinel_and_counts_it(
    roots: tuple[Path, Path],
) -> None:
    policy = PathPolicy(
        real_login_root=roots[0],
        proxy_owned_root=roots[1],
        max_snapshot_entries=2,
    )

    empty = policy.snapshot(root=RootKind.REAL_LOGIN)
    assert len(empty) == 1
    root = empty[0]
    value = roots[0].lstat()
    assert root.relative_path == "."
    assert (
        root.st_dev,
        root.st_ino,
        root.mode,
        root.nlink,
        root.size,
        root.mtime_ns,
    ) == (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
    )

    (roots[0] / "settings.json").write_bytes(b"ONE")
    assert {item.relative_path for item in policy.snapshot()} == {
        ".",
        "settings.json",
    }
    (roots[0] / "CLAUDE.md").write_bytes(b"TWO")
    with pytest.raises(PathPolicyError, match="snapshot entry limit"):
        policy.snapshot()


@pytest.mark.parametrize(
    ("kind", "operation"),
    [
        ("validation_root", "construct"),
        ("ancestor_dup", "construct"),
        ("ancestor_parent", "construct"),
        ("reopened_root", "metadata"),
        ("component_dup", "metadata"),
        ("component_child", "nested_metadata"),
        ("content_file", "content_scan"),
        ("snapshot_child", "snapshot"),
        ("snapshot_scandir", "snapshot"),
    ],
)
def test_every_owned_resource_close_is_one_shot_and_owner_scoped(
    policy: PathPolicy,
    roots: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    operation: str,
) -> None:
    (roots[0] / "settings.json").write_bytes(b"SAFE")
    plugins = roots[0] / "plugins"
    plugins.mkdir(mode=0o700)
    (plugins / "settings.json").write_bytes(b"NESTED")
    canaries = roots[1] / "canaries"
    canaries.mkdir(mode=0o700)
    canary_path = canaries / "purity.txt"
    canary = b"SAFE-CANARY"
    canary_path.write_bytes(canary)
    canary_path.chmod(0o600)
    canary_metadata = policy.metadata(
        "canaries/purity.txt", root=RootKind.PROXY_OWNED
    )

    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        original_close = os.close
        original_scandir = os.scandir
        target_token: object | None = None
        target_resource: object | None = None
        target_close_calls = 0

        def ambiguous_record_for(resource: object):  # type: ignore[no-untyped-def]
            matches = [
                record
                for record in path_policy_impl._RESOURCE_REGISTRY.values()
                if record.resource is resource
                or (
                    type(resource) is int
                    and type(record.resource) is int
                    and record.resource == resource
                )
            ]
            return matches[-1] if matches else None

        def effect_then_raise_close(descriptor: int) -> None:
            nonlocal target_close_calls, target_resource, target_token
            record = ambiguous_record_for(descriptor)
            if (
                target_token is None
                and record is not None
                and record.kind == kind
            ):
                target_token = record.token
                target_resource = descriptor
            if record is not None and record.token is target_token:
                target_close_calls += 1
                original_close(descriptor)
                raise KeyboardInterrupt("AMBIGUOUS-RESOURCE-CLOSE")
            original_close(descriptor)

        class EffectThenRaiseScandir:
            def __init__(self, descriptor: int) -> None:
                self._inner = original_scandir(descriptor)

            def __iter__(self):  # type: ignore[no-untyped-def]
                return iter(self._inner)

            def __next__(self):  # type: ignore[no-untyped-def]
                return next(self._inner)

            def close(self) -> None:
                nonlocal target_close_calls, target_resource, target_token
                record = ambiguous_record_for(self)
                if (
                    target_token is None
                    and record is not None
                    and record.kind == kind
                ):
                    target_token = record.token
                    target_resource = self
                self._inner.close()
                if record is not None and record.token is target_token:
                    target_close_calls += 1
                    raise KeyboardInterrupt("AMBIGUOUS-RESOURCE-CLOSE")

        monkeypatch.setattr(
            "claude_sdk_proxy.path_policy.os.close", effect_then_raise_close
        )
        monkeypatch.setattr(
            "claude_sdk_proxy.path_policy.os.scandir", EffectThenRaiseScandir
        )
        verified = False
        try:
            with pytest.raises(
                KeyboardInterrupt, match="AMBIGUOUS-RESOURCE-CLOSE"
            ):
                if operation == "construct":
                    PathPolicy(
                        real_login_root=roots[0],
                        proxy_owned_root=roots[1],
                    )
                elif operation == "metadata":
                    policy.metadata("settings.json")
                elif operation == "nested_metadata":
                    policy.metadata("plugins/settings.json")
                elif operation == "content_scan":
                    policy.scan_for_canary(
                        "canaries/purity.txt",
                        canary,
                        expected=canary_metadata,
                        root=RootKind.PROXY_OWNED,
                    )
                else:
                    policy.snapshot()

            retained = path_policy_impl._RESOURCE_REGISTRY[target_token]
            calls_after_failure = target_close_calls
            if type(target_resource) is int:
                with pytest.raises(PathPolicyError):
                    path_policy_impl._close_fd(retained.owner, target_resource)
            else:
                with pytest.raises(PathPolicyError):
                    path_policy_impl._close_closeable(
                        retained.owner, target_resource
                    )
            independent = PathPolicy(
                real_login_root=roots[0], proxy_owned_root=roots[1]
            )
            independent.metadata("settings.json")
            verified = (
                retained.state is path_policy_impl._ResourceState.AMBIGUOUS
                and target_close_calls == calls_after_failure == 1
            )
            os.write(write_fd, b"1" if verified else b"0")
            original_close(write_fd)
        finally:
            os._exit(0)
    os.close(write_fd)
    result = os.read(read_fd, 1)
    os.close(read_fd)
    waited, status = os.waitpid(child, 0)
    assert waited == child
    assert os.waitstatus_to_exitcode(status) == 0
    assert result == b"1"


def test_classify_never_succeeds_after_missing_path_cleanup_becomes_ambiguous(
    policy: PathPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        original_close = os.close
        injected = False
        close_calls = 0

        def effect_then_raise_missing(descriptor: int) -> None:
            nonlocal close_calls, injected
            record = next(
                (
                    item
                    for item in path_policy_impl._RESOURCE_REGISTRY.values()
                    if item.owner is policy._resource_owner
                    and item.resource == descriptor
                    and item.kind == "component_dup"
                ),
                None,
            )
            if record is not None and not injected:
                injected = True
                close_calls += 1
                original_close(descriptor)
                raise FileNotFoundError("AMBIGUOUS-CLEANUP-MUST-NOT-MEAN-ABSENT")
            original_close(descriptor)

        monkeypatch.setattr(
            "claude_sdk_proxy.path_policy.os.close", effect_then_raise_missing
        )
        failed_closed = False
        try:
            try:
                policy.classify("missing/settings.json")
            except PathPolicyError:
                failed_closed = True
            retained = [
                item
                for item in path_policy_impl._RESOURCE_REGISTRY.values()
                if item.owner is policy._resource_owner
            ]
            verified = (
                failed_closed
                and close_calls == 1
                and len(retained) == 1
                and retained[0].kind == "component_dup"
                and retained[0].state is path_policy_impl._ResourceState.AMBIGUOUS
            )
            os.write(write_fd, b"1" if verified else b"0")
            original_close(write_fd)
        finally:
            os._exit(0)
    os.close(write_fd)
    result = os.read(read_fd, 1)
    os.close(read_fd)
    waited, status = os.waitpid(child, 0)
    assert waited == child
    assert os.waitstatus_to_exitcode(status) == 0
    assert result == b"1"


@pytest.mark.parametrize(
    ("operation", "closing_kind", "acquired_kind"),
    [
        ("component_transfer", "component_dup", "component_child"),
        ("content_transfer", "component_child", "content_file"),
    ],
)
def test_cleanup_failure_after_acquisition_never_orphans_the_new_owner(
    policy: PathPolicy,
    roots: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    closing_kind: str,
    acquired_kind: str,
) -> None:
    plugins = roots[0] / "plugins"
    plugins.mkdir(mode=0o700)
    (plugins / "settings.json").write_bytes(b"NESTED")
    canaries = roots[1] / "canaries"
    canaries.mkdir(mode=0o700)
    canary_path = canaries / "purity.txt"
    canary = b"SAFE-CANARY"
    canary_path.write_bytes(canary)
    canary_path.chmod(0o600)
    canary_metadata = policy.metadata("canaries/purity.txt", root=RootKind.PROXY_OWNED)
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        original_close = os.close
        injected = False
        close_calls = 0

        def effect_then_raise_after_acquisition(descriptor: int) -> None:
            nonlocal close_calls, injected
            records = [
                item
                for item in path_policy_impl._RESOURCE_REGISTRY.values()
                if item.owner is policy._resource_owner
            ]
            closing = next(
                (
                    item
                    for item in records
                    if item.resource == descriptor and item.kind == closing_kind
                ),
                None,
            )
            acquired = any(item.kind == acquired_kind for item in records)
            if closing is not None and acquired and not injected:
                injected = True
                close_calls += 1
                original_close(descriptor)
                raise KeyboardInterrupt("TRANSFER-CLEANUP-AMBIGUOUS")
            original_close(descriptor)

        monkeypatch.setattr(
            "claude_sdk_proxy.path_policy.os.close",
            effect_then_raise_after_acquisition,
        )
        failed_closed = False
        try:
            try:
                if operation == "component_transfer":
                    policy.metadata("plugins/settings.json")
                else:
                    policy.scan_for_canary(
                        "canaries/purity.txt",
                        canary,
                        expected=canary_metadata,
                        root=RootKind.PROXY_OWNED,
                    )
            except KeyboardInterrupt:
                failed_closed = True
            retained = [
                item
                for item in path_policy_impl._RESOURCE_REGISTRY.values()
                if item.owner is policy._resource_owner
            ]
            verified = (
                failed_closed
                and close_calls == 1
                and len(retained) == 1
                and retained[0].kind == closing_kind
                and retained[0].state is path_policy_impl._ResourceState.AMBIGUOUS
                and all(item.kind != acquired_kind for item in retained)
            )
            os.write(write_fd, b"1" if verified else b"0")
            original_close(write_fd)
        finally:
            os._exit(0)
    os.close(write_fd)
    result = os.read(read_fd, 1)
    os.close(read_fd)
    waited, status = os.waitpid(child, 0)
    assert waited == child
    assert os.waitstatus_to_exitcode(status) == 0
    assert result == b"1"


def test_fd_capacity_is_bounded_per_owner_and_globally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_one = object()
    owner_two = object()
    owner_three = object()
    monkeypatch.setattr(path_policy_impl, "_MAX_OWNER_RESOURCES", 1)
    monkeypatch.setattr(path_policy_impl, "_MAX_GLOBAL_RESOURCES", 2)
    first = path_policy_impl._acquire_fd(
        owner_one, "capacity", lambda: os.open("/dev/null", os.O_RDONLY)
    )
    second: int | None = None
    try:
        with pytest.raises(PathPolicyError, match="capacity exceeded"):
            path_policy_impl._acquire_fd(
                owner_one,
                "capacity",
                lambda: os.open("/dev/null", os.O_RDONLY),
            )
        second = path_policy_impl._acquire_fd(
            owner_two, "capacity", lambda: os.open("/dev/null", os.O_RDONLY)
        )
        with pytest.raises(PathPolicyError, match="capacity exceeded"):
            path_policy_impl._acquire_fd(
                owner_three,
                "capacity",
                lambda: os.open("/dev/null", os.O_RDONLY),
            )
    finally:
        if second is not None:
            path_policy_impl._close_fd(owner_two, second)
        path_policy_impl._close_fd(owner_one, first)


def test_acquisition_preregistration_rejects_same_owner_reentrancy(
    policy: PathPolicy,
    roots: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (roots[0] / "settings.json").write_bytes(b"SAFE")
    original_open = os.open
    inspected = False
    reentry_rejected = False

    def inspecting_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal inspected, reentry_rejected
        if path == roots[0] and not inspected:
            inspected = any(
                record.owner is policy._resource_owner
                and record.kind == "reopened_root"
                and record.state is path_policy_impl._ResourceState.RESERVED
                for record in path_policy_impl._RESOURCE_REGISTRY.values()
            )
            try:
                policy.metadata("settings.json")
            except PathPolicyError:
                reentry_rejected = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("claude_sdk_proxy.path_policy.os.open", inspecting_open)

    policy.metadata("settings.json")

    assert inspected
    assert reentry_rejected


def test_forked_pending_resource_becomes_nonactionable_in_child(
    policy: PathPolicy,
) -> None:
    descriptor = path_policy_impl._acquire_fd(
        policy._resource_owner,
        "fork_pending",
        lambda: os.open("/dev/null", os.O_RDONLY),
    )
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        original_close = os.close
        close_calls = 0

        def tracking_close(value: int) -> None:
            nonlocal close_calls
            if value == descriptor:
                close_calls += 1
            original_close(value)

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr("claude_sdk_proxy.path_policy.os.close", tracking_close)
        verified = False
        try:
            with pytest.raises(PathPolicyError, match="ownership is ambiguous"):
                policy.classify("settings.json")
            with pytest.raises(PathPolicyError):
                path_policy_impl._close_fd(policy._resource_owner, descriptor)
            verified = close_calls == 0
            os.write(write_fd, b"1" if verified else b"0")
            original_close(write_fd)
        finally:
            os._exit(0)
    os.close(write_fd)
    result = os.read(read_fd, 1)
    os.close(read_fd)
    waited, status = os.waitpid(child, 0)
    path_policy_impl._close_fd(policy._resource_owner, descriptor)
    assert waited == child
    assert os.waitstatus_to_exitcode(status) == 0
    assert result == b"1"


def test_fork_inside_close_cannot_certify_inherited_obligation_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = object()
    descriptor = path_policy_impl._acquire_fd(
        owner,
        "fork_during_close",
        lambda: os.open("/dev/null", os.O_RDONLY),
    )
    record = next(
        item
        for item in path_policy_impl._RESOURCE_REGISTRY.values()
        if item.owner is owner and item.resource == descriptor
    )
    read_fd, write_fd = os.pipe()
    original_close = os.close
    nested_child = False
    nested_pid: int | None = None

    def forking_close(value: int) -> None:
        nonlocal nested_child, nested_pid
        if value != descriptor:
            original_close(value)
            return
        nested_pid = os.fork()
        if nested_pid == 0:
            nested_child = True
            return
        original_close(value)

    monkeypatch.setattr("claude_sdk_proxy.path_policy.os.close", forking_close)

    try:
        path_policy_impl._close_fd(owner, descriptor)
    except PathPolicyError:
        pass
    if nested_child:
        retained = path_policy_impl._RESOURCE_REGISTRY.get(record.token)
        verified = (
            retained is record
            and retained.state is path_policy_impl._ResourceState.AMBIGUOUS
        )
        os.write(write_fd, b"1" if verified else b"0")
        original_close(write_fd)
        os._exit(0)

    assert nested_pid is not None
    original_close(write_fd)
    result = os.read(read_fd, 1)
    original_close(read_fd)
    waited, status = os.waitpid(nested_pid, 0)
    assert waited == nested_pid
    assert os.waitstatus_to_exitcode(status) == 0
    assert result == b"1"


def test_concurrent_operation_waits_for_close_disposition_then_fails_closed(
    policy: PathPolicy,
    roots: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (roots[0] / "settings.json").write_bytes(b"SAFE")
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        original_close = os.close
        entered_close = threading.Event()
        release_close = threading.Event()
        second_started = threading.Event()
        target_token: object | None = None
        target_close_calls = 0
        outcomes: list[str] = []

        def effect_then_raise_close(descriptor: int) -> None:
            nonlocal target_close_calls, target_token
            matches = [
                record
                for record in path_policy_impl._RESOURCE_REGISTRY.values()
                if type(record.resource) is int
                and record.resource == descriptor
            ]
            record = matches[-1] if matches else None
            if (
                target_token is None
                and record is not None
                and record.kind == "component_dup"
            ):
                target_token = record.token
            if record is not None and record.token is target_token:
                target_close_calls += 1
                entered_close.set()
                if not release_close.wait(2):
                    raise AssertionError("concurrent close release timed out")
                original_close(descriptor)
                raise KeyboardInterrupt("CONCURRENT-AMBIGUOUS-CLOSE")
            original_close(descriptor)

        monkeypatch.setattr(
            "claude_sdk_proxy.path_policy.os.close", effect_then_raise_close
        )

        def first_operation() -> None:
            try:
                policy.metadata("settings.json")
            except KeyboardInterrupt:
                outcomes.append("first_ambiguous")

        def second_operation() -> None:
            second_started.set()
            try:
                policy.metadata("settings.json")
            except PathPolicyError:
                outcomes.append("second_failed_closed")

        first = threading.Thread(target=first_operation)
        second = threading.Thread(target=second_operation)
        first.start()
        verified = entered_close.wait(2)
        second.start()
        verified = verified and second_started.wait(2)
        second.join(0.05)
        verified = verified and second.is_alive()
        release_close.set()
        first.join(2)
        second.join(2)
        verified = verified and not first.is_alive() and not second.is_alive()
        verified = verified and sorted(outcomes) == [
            "first_ambiguous",
            "second_failed_closed",
        ]
        verified = verified and target_close_calls == 1
        os.write(write_fd, b"1" if verified else b"0")
        original_close(write_fd)
        os._exit(0)
    os.close(write_fd)
    result = os.read(read_fd, 1)
    os.close(read_fd)
    waited, status = os.waitpid(child, 0)
    assert waited == child
    assert os.waitstatus_to_exitcode(status) == 0
    assert result == b"1"


def test_ambiguous_fd_close_poison_is_retained_without_numeric_retry(
    policy: PathPolicy,
    roots: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = roots[0] / "settings.json"
    target.write_bytes(b"SAFE")
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        original_close = os.close
        close_calls = 0

        def effect_then_raise(descriptor: int) -> None:
            nonlocal close_calls
            original_close(descriptor)
            close_calls += 1
            if close_calls == 1:
                raise KeyboardInterrupt("AMBIGUOUS-FD-CLOSE")

        monkeypatch.setattr("claude_sdk_proxy.path_policy.os.close", effect_then_raise)
        poisoned = False
        try:
            with pytest.raises(KeyboardInterrupt, match="AMBIGUOUS-FD-CLOSE"):
                policy.metadata(Path("settings.json"))
            calls_after_failure = close_calls
            try:
                policy.metadata(Path("settings.json"))
            except PathPolicyError:
                poisoned = close_calls == calls_after_failure
            os.write(
                write_fd,
                b"1" if poisoned else b"0",
            )
            original_close(write_fd)
        finally:
            os._exit(0)
    os.close(write_fd)
    result = os.read(read_fd, 1)
    os.close(read_fd)
    waited, status = os.waitpid(child, 0)
    assert waited == child
    assert os.waitstatus_to_exitcode(status) == 0
    assert result == b"1"


def test_ambiguous_scandir_close_poison_is_retained_without_retry(
    policy: PathPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        original_scandir = os.scandir
        injected = False
        close_calls = 0

        class EffectThenRaiseScandir:
            def __init__(self, descriptor: int) -> None:
                self._inner = original_scandir(descriptor)

            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *_args: object) -> None:
                self.close()

            def __iter__(self):  # type: ignore[no-untyped-def]
                return iter(self._inner)

            def __next__(self):  # type: ignore[no-untyped-def]
                return next(self._inner)

            def close(self) -> None:
                nonlocal injected, close_calls
                self._inner.close()
                close_calls += 1
                if not injected:
                    injected = True
                    raise KeyboardInterrupt("AMBIGUOUS-SCANDIR-CLOSE")

        monkeypatch.setattr(
            "claude_sdk_proxy.path_policy.os.scandir", EffectThenRaiseScandir
        )
        poisoned = False
        try:
            with pytest.raises(KeyboardInterrupt, match="AMBIGUOUS-SCANDIR-CLOSE"):
                policy.snapshot()
            calls_after_failure = close_calls
            try:
                policy.snapshot()
            except PathPolicyError:
                poisoned = close_calls == calls_after_failure
            os.write(write_fd, b"1" if poisoned else b"0")
            os.close(write_fd)
        finally:
            os._exit(0)
    os.close(write_fd)
    result = os.read(read_fd, 1)
    os.close(read_fd)
    waited, status = os.waitpid(child, 0)
    assert waited == child
    assert os.waitstatus_to_exitcode(status) == 0
    assert result == b"1"


def test_snapshot_rejects_leaf_replacement_before_admitting_metadata(
    policy: PathPolicy,
    roots: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = roots[0] / "settings.json"
    target.write_bytes(b"FIRST")
    original_stat = os.stat
    calls = 0

    def swapping_stat(
        path: os.PathLike[str] | str | int,
        *args: object,
        **kwargs: object,
    ) -> os.stat_result:
        nonlocal calls
        if path == "settings.json":
            calls += 1
            if calls == 2:
                target.unlink()
                target.write_bytes(b"SECOND-LONGER")
        return original_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("claude_sdk_proxy.path_policy.os.stat", swapping_stat)

    with pytest.raises(PathPolicyError, match="identity changed"):
        policy.snapshot(root=RootKind.REAL_LOGIN)


def test_snapshot_binds_complete_opened_directory_metadata(
    policy: PathPolicy,
    roots: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = roots[0] / "plugins"
    directory.mkdir(mode=0o700)
    original_open = os.open
    changed = False

    def changing_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal changed
        if path == "plugins" and dir_fd is not None and not changed:
            changed = True
            current = directory.stat().st_mtime_ns
            os.utime(directory, ns=(current + 1_000_000, current + 1_000_000))
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("claude_sdk_proxy.path_policy.os.open", changing_open)

    with pytest.raises(PathPolicyError, match="identity changed"):
        policy.snapshot(root=RootKind.REAL_LOGIN)


@pytest.mark.parametrize("mutation", ["insert", "delete", "replace", "rename"])
def test_snapshot_certifies_root_across_descendant_scan_churn(
    policy: PathPolicy,
    roots: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    descendant = roots[0] / "plugins"
    descendant.mkdir(mode=0o700)
    target = roots[0] / "settings.json"
    target.write_bytes(b"ORIGINAL")
    descendant_inode = descendant.stat().st_ino
    original_scandir = os.scandir
    mutated = False

    def churning_scandir(
        path: os.PathLike[str] | str | bytes | int = ".",
    ) -> os.ScandirIterator[str]:
        nonlocal mutated
        if (
            isinstance(path, int)
            and os.fstat(path).st_ino == descendant_inode
            and not mutated
        ):
            mutated = True
            if mutation == "insert":
                (roots[0] / "CLAUDE.md").write_bytes(b"INSERTED")
            elif mutation == "delete":
                target.unlink()
            elif mutation == "replace":
                target.unlink()
                target.write_bytes(b"REPLACEMENT-LONGER")
            else:
                target.rename(roots[0] / "settings.local.json")
        return original_scandir(path)

    monkeypatch.setattr("claude_sdk_proxy.path_policy.os.scandir", churning_scandir)

    with pytest.raises(PathPolicyError, match="changed during snapshot"):
        policy.snapshot(root=RootKind.REAL_LOGIN)


def test_snapshot_root_sentinel_detects_transient_root_create_delete(
    policy: PathPolicy,
    roots: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descendant = roots[0] / "plugins"
    descendant.mkdir(mode=0o700)
    descendant_inode = descendant.stat().st_ino
    original_scandir = os.scandir
    mutated = False

    def churning_scandir(
        path: os.PathLike[str] | str | bytes | int = ".",
    ) -> os.ScandirIterator[str]:
        nonlocal mutated
        if (
            isinstance(path, int)
            and os.fstat(path).st_ino == descendant_inode
            and not mutated
        ):
            mutated = True
            transient = roots[0] / "CLAUDE.md"
            before = roots[0].stat()
            transient.write_bytes(b"TRANSIENT")
            transient.unlink()
            after = roots[0].stat()
            if after.st_mtime_ns == before.st_mtime_ns:
                os.utime(
                    roots[0],
                    ns=(after.st_atime_ns, after.st_mtime_ns + 1_000_000),
                )
        return original_scandir(path)

    monkeypatch.setattr("claude_sdk_proxy.path_policy.os.scandir", churning_scandir)

    with pytest.raises(PathPolicyError, match="changed during snapshot"):
        policy.snapshot(root=RootKind.REAL_LOGIN)


def test_snapshot_certifies_each_nested_directory_across_its_descendants(
    policy: PathPolicy,
    roots: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = roots[0] / "plugins"
    parent.mkdir(mode=0o700)
    descendant = parent / "nested"
    descendant.mkdir(mode=0o700)
    descendant_inode = descendant.stat().st_ino
    original_scandir = os.scandir
    mutated = False

    def churning_scandir(
        path: os.PathLike[str] | str | bytes | int = ".",
    ) -> os.ScandirIterator[str]:
        nonlocal mutated
        if (
            isinstance(path, int)
            and os.fstat(path).st_ino == descendant_inode
            and not mutated
        ):
            mutated = True
            (parent / "hooks").mkdir(mode=0o700)
        return original_scandir(path)

    monkeypatch.setattr("claude_sdk_proxy.path_policy.os.scandir", churning_scandir)

    with pytest.raises(PathPolicyError, match="changed during snapshot"):
        policy.snapshot(root=RootKind.REAL_LOGIN)


def test_snapshot_recertifies_earlier_entries_after_later_descendants(
    policy: PathPolicy,
    roots: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = roots[0] / "settings.json"
    target.write_bytes(b"ORIGINAL")
    later = roots[0] / "workflows"
    later.mkdir(mode=0o700)
    later_inode = later.stat().st_ino
    root_value = roots[0].stat()
    original_scandir = os.scandir
    mutated = False

    def churning_scandir(
        path: os.PathLike[str] | str | bytes | int = ".",
    ) -> os.ScandirIterator[str]:
        nonlocal mutated
        if (
            isinstance(path, int)
            and os.fstat(path).st_ino == later_inode
            and not mutated
        ):
            mutated = True
            target.unlink()
            target.write_bytes(b"REPLACED")
            os.utime(
                roots[0],
                ns=(root_value.st_atime_ns, root_value.st_mtime_ns),
            )
        return original_scandir(path)

    monkeypatch.setattr("claude_sdk_proxy.path_policy.os.scandir", churning_scandir)

    with pytest.raises(PathPolicyError, match="changed during snapshot"):
        policy.snapshot(root=RootKind.REAL_LOGIN)


def test_snapshot_depth_is_capped_for_bounded_descriptor_recursion(
    roots: tuple[Path, Path],
) -> None:
    with pytest.raises(PathPolicyError, match="depth limit.*at most"):
        PathPolicy(
            real_login_root=roots[0],
            proxy_owned_root=roots[1],
            max_depth=65,
        )


def test_snapshot_returns_certified_final_directory_metadata(
    policy: PathPolicy,
    roots: tuple[Path, Path],
) -> None:
    parent = roots[0] / "plugins"
    parent.mkdir(mode=0o700)
    child = parent / "nested"
    child.mkdir(mode=0o700)

    snapshot = {item.relative_path: item for item in policy.snapshot()}

    for relative, path in (("plugins", parent), ("plugins/nested", child)):
        value = path.lstat()
        metadata = snapshot[relative]
        assert (
            metadata.st_dev,
            metadata.st_ino,
            metadata.mode,
            metadata.nlink,
            metadata.size,
            metadata.mtime_ns,
        ) == (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_nlink,
            value.st_size,
            value.st_mtime_ns,
        )


@pytest.mark.parametrize("failure", ["stat", "scandir"])
def test_snapshot_unwind_closes_every_opened_authority(
    policy: PathPolicy,
    roots: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    parent = roots[0] / "plugins"
    parent.mkdir(mode=0o700)
    child = parent / "nested"
    child.mkdir(mode=0o700)
    child_inode = child.stat().st_ino
    original_open = os.open
    original_close = os.close
    original_stat = os.stat
    original_scandir = os.scandir
    opened: set[int] = set()

    def tracking_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        opened.add(descriptor)
        return descriptor

    def tracking_close(descriptor: int) -> None:
        try:
            original_close(descriptor)
        finally:
            opened.discard(descriptor)

    def failing_stat(
        path: os.PathLike[str] | str | int,
        *args: object,
        **kwargs: object,
    ) -> os.stat_result:
        if failure == "stat" and path == "nested":
            raise KeyboardInterrupt("STAT-UNWIND")
        return original_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    def failing_scandir(
        path: os.PathLike[str] | str | bytes | int = ".",
    ) -> os.ScandirIterator[str]:
        if (
            failure == "scandir"
            and isinstance(path, int)
            and os.fstat(path).st_ino == child_inode
        ):
            raise KeyboardInterrupt("SCANDIR-UNWIND")
        return original_scandir(path)

    monkeypatch.setattr("claude_sdk_proxy.path_policy.os.open", tracking_open)
    monkeypatch.setattr("claude_sdk_proxy.path_policy.os.close", tracking_close)
    monkeypatch.setattr("claude_sdk_proxy.path_policy.os.stat", failing_stat)
    monkeypatch.setattr("claude_sdk_proxy.path_policy.os.scandir", failing_scandir)

    with pytest.raises(KeyboardInterrupt, match="UNWIND"):
        policy.snapshot(root=RootKind.REAL_LOGIN)

    assert opened == set()


def test_snapshot_effect_then_raise_close_unwinds_remaining_authority(
    policy: PathPolicy,
    roots: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (roots[0] / "plugins").mkdir(mode=0o700)
    original_open = os.open
    original_close = os.close
    opened: set[int] = set()
    child_descriptor: int | None = None
    injected = False

    def tracking_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal child_descriptor
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        opened.add(descriptor)
        if path == "plugins":
            child_descriptor = descriptor
        return descriptor

    def effect_then_raise_close(descriptor: int) -> None:
        nonlocal injected
        try:
            original_close(descriptor)
        finally:
            opened.discard(descriptor)
        if descriptor == child_descriptor and not injected:
            injected = True
            raise KeyboardInterrupt("CLOSE-UNWIND")

    monkeypatch.setattr("claude_sdk_proxy.path_policy.os.open", tracking_open)
    monkeypatch.setattr(
        "claude_sdk_proxy.path_policy.os.close", effect_then_raise_close
    )

    with pytest.raises(KeyboardInterrupt, match="CLOSE-UNWIND"):
        policy.snapshot(root=RootKind.REAL_LOGIN)

    assert opened == set()
