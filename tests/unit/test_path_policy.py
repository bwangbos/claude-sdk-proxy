from __future__ import annotations

import os
from pathlib import Path

import pytest

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
