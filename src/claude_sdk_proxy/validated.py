"""Fail-closed validation of the pinned policy and local runtime inputs."""

from __future__ import annotations

import hashlib
import re
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

POLICY_URLS = (
    "https://code.claude.com/docs/en/agent-sdk/overview",
    "https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan",
)
EXPECTED_CLI_VERSION = "2.1.251"
_CLI_VERSION_OUTPUT = re.compile(
    r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)(?:\s+\(Claude Code\))?"
)


class RuntimeMismatch(ValueError):
    """Raised when policy or runtime evidence differs from the supported tuple."""


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

    try:
        output = completed.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise RuntimeMismatch("CLI version output is not UTF-8") from error
    matched = _CLI_VERSION_OUTPUT.fullmatch(output)
    if matched is None or matched["version"] != EXPECTED_CLI_VERSION:
        raise RuntimeMismatch("CLI version does not match the validated version")

    return CliIdentity(
        path=resolved_path,
        st_dev=metadata.st_dev,
        st_ino=metadata.st_ino,
        mode=metadata.st_mode,
        sha256=hashlib.sha256(resolved_path.read_bytes()).hexdigest(),
        version=matched["version"],
    )


def validate_policy(evidence: PolicyEvidence) -> None:
    """Raise when the policy evidence cannot authorize personal local use."""
    evidence.require_allowed()
