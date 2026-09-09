"""Tests for string-only Agent SDK isolation options."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from quaylet.isolation import IsolationConfig, build_agent_options


def _supervisor(tmp_path: Path) -> Path:
    supervisor = tmp_path / "claude-supervisor"
    supervisor.write_text("#!/bin/sh\nexit 0\n")
    supervisor.chmod(0o700)
    return supervisor


def _config(tmp_path: Path) -> IsolationConfig:
    cwd = tmp_path / "workdir"
    cwd.mkdir(mode=0o700)
    cwd.chmod(0o700)
    return IsolationConfig(
        model_id="claude-exact",
        system_prompt="caller string",
        cwd=cwd,
        supervisor_path=_supervisor(tmp_path),
        environment={"PATH": "/verified/claude:/usr/bin:/bin"},
    )


def test_system_prompt_must_be_one_string(tmp_path: Path) -> None:
    """Structured and preset system prompts are rejected before SDK construction."""
    with pytest.raises(TypeError, match="plain string"):
        IsolationConfig(
            model_id="claude-exact",
            system_prompt=[{"type": "text"}],  # type: ignore[arg-type]
            cwd=tmp_path,
            supervisor_path=_supervisor(tmp_path),
            environment={},
        )


def test_isolation_options_match_the_exact_child_profile(tmp_path: Path) -> None:
    """The SDK receives only the caller string and proxy-owned empty features."""
    config = _config(tmp_path)

    options = build_agent_options(config)

    assert options.model == "claude-exact"
    assert options.system_prompt == "caller string"
    assert options.tools == []
    assert options.skills == []
    assert options.setting_sources == []
    assert options.mcp_servers == {}
    assert options.strict_mcp_config is True
    assert options.agents == {}
    assert options.plugins == []
    assert options.cwd == config.cwd
    assert options.cli_path == config.supervisor_path
    assert options.env == {"PATH": "/verified/claude:/usr/bin:/bin"}
    assert options.include_partial_messages is True
    assert options.fallback_model is None


def test_isolation_rejects_nonabsolute_or_nonowned_workdirs(tmp_path: Path) -> None:
    """The child workdir is owned by this user and cannot be supplied relatively."""
    with pytest.raises(ValueError, match="absolute"):
        IsolationConfig(
            model_id="claude-exact",
            system_prompt="caller string",
            cwd=Path("relative"),
            supervisor_path=_supervisor(tmp_path),
            environment={},
        )

    if os.getuid() != 0:
        with pytest.raises(ValueError, match="owned"):
            IsolationConfig(
                model_id="claude-exact",
                system_prompt="caller string",
                cwd=Path("/"),
                supervisor_path=_supervisor(tmp_path),
                environment={},
            )


def test_options_require_an_empty_private_workdir(tmp_path: Path) -> None:
    """SDK construction refuses a nonempty or group-readable child directory."""
    config = _config(tmp_path)
    (config.cwd / "ambient-file").write_text("not allowed")

    with pytest.raises(ValueError, match="empty"):
        build_agent_options(config)

    (config.cwd / "ambient-file").unlink()
    config.cwd.chmod(0o755)
    with pytest.raises(ValueError, match="0700"):
        build_agent_options(config)
