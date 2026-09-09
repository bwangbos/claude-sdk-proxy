"""Construction of the fixed string-only Claude Agent SDK option profile."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions


@dataclass(frozen=True)
class IsolationConfig:
    """Proxy-owned values that may enter one isolated Agent SDK client."""

    model_id: str
    system_prompt: str
    cwd: Path
    supervisor_path: Path
    environment: Mapping[str, str]

    def __post_init__(self) -> None:
        """Reject structured prompts and a workdir outside the current user."""
        if not isinstance(self.model_id, str) or not self.model_id:
            raise TypeError("model ID must be a nonempty string")
        if not isinstance(self.system_prompt, str):
            raise TypeError("system prompt must be one plain string")
        _require_owned_absolute_workdir(self.cwd)
        if not self.supervisor_path.is_absolute():
            raise ValueError("verified supervisor path must be absolute")
        for name, value in self.environment.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise TypeError("fixed child environment must contain only strings")


def _require_owned_absolute_workdir(cwd: Path) -> None:
    """Require an existing workdir belonging to the current local user."""
    if not cwd.is_absolute():
        raise ValueError("child workdir must be absolute")
    try:
        metadata = cwd.stat()
    except OSError as error:
        raise ValueError("child workdir must exist") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("child workdir must be a directory")
    if metadata.st_uid != os.getuid():
        raise ValueError("child workdir must be owned by the current user")


def _require_empty_private_workdir(cwd: Path) -> None:
    """Confirm the SDK is launched in an empty mode-0700 directory."""
    _require_owned_absolute_workdir(cwd)
    if stat.S_IMODE(cwd.stat().st_mode) != 0o700:
        raise ValueError("child workdir must have mode 0700")
    try:
        next(cwd.iterdir())
    except StopIteration:
        return
    raise ValueError("child workdir must be empty")


def build_agent_options(config: IsolationConfig) -> ClaudeAgentOptions:
    """Build the immutable prompt-isolated SDK profile for a verified child."""
    _require_empty_private_workdir(config.cwd)
    return ClaudeAgentOptions(
        model=config.model_id,
        system_prompt=config.system_prompt,
        tools=[],
        skills=[],
        setting_sources=[],
        mcp_servers={},
        strict_mcp_config=True,
        agents={},
        plugins=[],
        cwd=config.cwd,
        cli_path=config.supervisor_path,
        env=dict(config.environment),
        include_partial_messages=True,
    )
