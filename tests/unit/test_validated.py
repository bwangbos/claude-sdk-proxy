import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_sdk_proxy.validated import (
    POLICY_URLS,
    CliIdentity,
    PolicyEvidence,
    RuntimeMismatch,
    RuntimeTuple,
    read_cli_identity,
    validate_policy,
)


def test_runtime_tuple_is_exact() -> None:
    """A changed SDK, CLI, Darwin major version, or filesystem cannot pass."""
    expected = RuntimeTuple("0.2.148", "2.1.251", 14, "apfs")
    with pytest.raises(RuntimeMismatch):
        expected.require(RuntimeTuple("0.2.148", "2.1.250", 14, "apfs"))


def test_runtime_evidence_is_immutable_after_validation() -> None:
    """A recorded runtime tuple cannot be mutated into a different attestation."""
    tuple_ = RuntimeTuple("0.2.148", "2.1.251", 14, "apfs")
    with pytest.raises(AttributeError):
        tuple_.cli_version = "2.1.252"  # type: ignore[misc]


@pytest.mark.parametrize(
    "actual",
    [
        RuntimeTuple("0.2.147", "2.1.251", 14, "apfs"),
        RuntimeTuple("0.2.148", "2.1.252", 14, "apfs"),
        RuntimeTuple("0.2.148", "2.1.251", 15, "apfs"),
        RuntimeTuple("0.2.148", "2.1.251", 14, "hfs"),
    ],
)
def test_runtime_tuple_rejects_a_change_to_each_component(
    actual: RuntimeTuple,
) -> None:
    """The accepted runtime is a four-part tuple, not a partial match."""
    with pytest.raises(RuntimeMismatch):
        RuntimeTuple("0.2.148", "2.1.251", 14, "apfs").require(actual)


def test_policy_requires_two_current_primary_sources() -> None:
    """Missing sources or a non-affirmative policy verdict blocks live evidence."""
    evidence = PolicyEvidence(
        checked_at=datetime.now(UTC), source_urls=(), personal_local_use_allowed=False
    )
    with pytest.raises(RuntimeMismatch, match="policy"):
        evidence.require_allowed()


def test_policy_accepts_the_complete_affirmative_primary_evidence() -> None:
    """Both required official pages and an affirmative interpretation are needed."""
    evidence = PolicyEvidence(
        checked_at=datetime.now(UTC),
        source_urls=POLICY_URLS,
        personal_local_use_allowed=True,
    )
    evidence.require_allowed()
    validate_policy(evidence)


@pytest.mark.parametrize(
    "source_urls, allowed",
    [
        (POLICY_URLS[:1], True),
        (POLICY_URLS + ("https://example.test/unapproved",), True),
        (POLICY_URLS, False),
    ],
)
def test_policy_rejects_incomplete_extra_or_negative_evidence(
    source_urls: tuple[str, ...], allowed: bool
) -> None:
    """An extra source cannot substitute for the exact official evidence set."""
    evidence = PolicyEvidence(
        checked_at=datetime.now(UTC),
        source_urls=source_urls,
        personal_local_use_allowed=allowed,
    )
    with pytest.raises(RuntimeMismatch, match="policy"):
        validate_policy(evidence)


def test_read_cli_identity_records_the_resolved_regular_executable(
    tmp_path: Path,
) -> None:
    """Identity comes from one pinned executable invoked only with --version."""
    cli = tmp_path / "claude"
    contents = (
        b"#!/bin/sh\n"
        b'if [ "$#" -ne 1 ] || [ "$1" != "--version" ]; then exit 99; fi\n'
        b"printf '2.1.251 (Claude Code)\\n'\n"
    )
    cli.write_bytes(contents)
    cli.chmod(0o755)

    identity = read_cli_identity(cli)
    expected_stat = cli.stat()

    assert identity == CliIdentity(
        path=cli.resolve(),
        st_dev=expected_stat.st_dev,
        st_ino=expected_stat.st_ino,
        mode=expected_stat.st_mode,
        sha256=hashlib.sha256(contents).hexdigest(),
        version="2.1.251",
    )


@pytest.mark.parametrize(
    "contents, mode",
    [
        (b"#!/bin/sh\nprintf '2.1.250 (Claude Code)\\n'\n", 0o755),
        (b"#!/bin/sh\nprintf '2.1.251 (Claude Code)\\n'\n", 0o644),
    ],
)
def test_read_cli_identity_rejects_wrong_or_nonexecutable_cli(
    tmp_path: Path, contents: bytes, mode: int
) -> None:
    """A version drift or an ineligible executable blocks a runtime attestation."""
    cli = tmp_path / "claude"
    cli.write_bytes(contents)
    cli.chmod(mode)

    with pytest.raises(RuntimeMismatch, match="CLI"):
        read_cli_identity(cli)


def test_read_cli_identity_rejects_a_nonregular_path(tmp_path: Path) -> None:
    """Directories and other non-files cannot be trusted as the CLI identity."""
    with pytest.raises(RuntimeMismatch, match="CLI"):
        read_cli_identity(tmp_path)
