"""Tests for the fail-closed Agent SDK child environment."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from claude_sdk_proxy.environment import (
    EnvironmentAmbiguityError,
    EnvironmentConfig,
    build_child_environment,
    environment_fingerprint,
)

FIXED_ISOLATION_ENV = {
    "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
    "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
    "DISABLE_COMPACT": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1",
    "CLAUDE_CODE_DISABLE_POLICY_SKILLS": "1",
    "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1",
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    "CLAUDE_CODE_DISABLE_WORKFLOWS": "1",
    "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
}


def test_child_environment_is_deny_by_default(tmp_path: Path) -> None:
    """Only named safe values and proxy-owned fixed isolation controls survive."""
    source = {
        "HOME": str(tmp_path),
        "LANG": "en_US.UTF-8",
        "PATH": "/host/bin",
        "LOCAL_PROXY_API_KEY": "proxy-only",
        "LOCAL_PROXY_RUN_TOKEN": "proxy-only",
        "CLAUDE_UNKNOWN": "unrelated",
        "RANDOM_HOST_VALUE": "unrelated",
    }

    env = build_child_environment(
        source, EnvironmentConfig(cli_dir=Path("/opt/claude"))
    )

    assert env == {
        "HOME": str(tmp_path),
        "LANG": "en_US.UTF-8",
        "PATH": "/opt/claude:/usr/bin:/bin:/usr/sbin:/sbin",
        **FIXED_ISOLATION_ENV,
    }


def test_safe_inherited_names_include_existing_login_location(tmp_path: Path) -> None:
    """The exact inherited-name allowlist retains existing-login metadata paths."""
    source = {
        "HOME": str(tmp_path),
        "USER": "local-user",
        "LOGNAME": "local-login",
        "TMPDIR": "/tmp/one",
        "TMP": "/tmp/two",
        "TEMP": "/tmp/three",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "LC_CTYPE": "UTF-8",
        "TZ": "UTC",
        "SSL_CERT_FILE": "/etc/ssl/cert.pem",
        "SSL_CERT_DIR": "/etc/ssl/certs",
        "CLAUDE_CONFIG_DIR": str(tmp_path / ".claude"),
    }

    env = build_child_environment(
        source, EnvironmentConfig(cli_dir=Path("/opt/claude"))
    )

    for name, value in source.items():
        assert env[name] == value


@pytest.mark.parametrize(
    "name",
    [
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_LITELLM",
        "ANTHROPIC_EXTRA_API_KEY",
        "CLAUDE_OAUTH_OVERRIDE",
        "AZURE_OPENAI_ENDPOINT",
        "VERTEXAI_PROJECT",
    ],
)
def test_auth_provider_and_custom_endpoint_overrides_reject(
    name: str, tmp_path: Path
) -> None:
    """Authentication provenance-changing variables fail closed before filtering."""
    with pytest.raises(EnvironmentAmbiguityError, match="authentication or provider"):
        build_child_environment(
            {"HOME": str(tmp_path), name: "override"},
            EnvironmentConfig(cli_dir=Path("/opt/claude")),
        )


@pytest.mark.parametrize(
    "name",
    [
        "LOCAL_PROXY_API_KEY",
        "LOCAL_PROXY_MASTER_KEY",
        "LOCAL_PROXY_RUN_TOKEN",
        "LOCAL_PROXY_ALLOCATION_ID",
        "CLAUDE_UNKNOWN",
        "ANTHROPIC_UNRELATED",
        "LOCAL_PROXY_UNRELATED",
        "RANDOM_HOST_VALUE",
    ],
)
def test_proxy_only_and_unrelated_values_are_stripped(
    name: str, tmp_path: Path
) -> None:
    """Non-authoritative proxy and ambient values do not enter the child."""
    env = build_child_environment(
        {"HOME": str(tmp_path), name: "host-value"},
        EnvironmentConfig(cli_dir=Path("/opt/claude")),
    )

    assert name not in env


def test_network_proxy_values_require_explicit_opt_in(tmp_path: Path) -> None:
    """Both proxy casing variants are retained only by the documented opt-in."""
    source = {
        "HOME": str(tmp_path),
        "HTTP_PROXY": "http://upper.example",
        "HTTPS_PROXY": "http://upper-secure.example",
        "NO_PROXY": "localhost",
        "http_proxy": "http://lower.example",
        "https_proxy": "http://lower-secure.example",
        "no_proxy": "127.0.0.1",
    }

    disabled = build_child_environment(
        source, EnvironmentConfig(cli_dir=Path("/opt/claude"))
    )
    enabled = build_child_environment(
        source, EnvironmentConfig(cli_dir=Path("/opt/claude"), network_proxy=True)
    )

    assert set(disabled).isdisjoint(set(source) - {"HOME"})
    assert {name: enabled[name] for name in source if name != "HOME"} == {
        name: value for name, value in source.items() if name != "HOME"
    }


def test_explicit_pass_names_are_the_only_additional_values(tmp_path: Path) -> None:
    """Configured pass-through preserves a named value without ambient expansion."""
    source = {
        "HOME": str(tmp_path),
        "EXPLICIT_SAFE_SETTING": "enabled",
        "OTHER_HOST_SETTING": "not-forwarded",
    }

    env = build_child_environment(
        source,
        EnvironmentConfig(
            cli_dir=Path("/opt/claude"), pass_names=("EXPLICIT_SAFE_SETTING",)
        ),
    )

    assert env["EXPLICIT_SAFE_SETTING"] == "enabled"
    assert "OTHER_HOST_SETTING" not in env


@pytest.mark.parametrize("pass_name", ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"])
def test_pass_names_cannot_admit_authentication_overrides(
    pass_name: str, tmp_path: Path
) -> None:
    """Explicit pass-through never weakens the authentication provenance gate."""
    with pytest.raises(EnvironmentAmbiguityError, match="authentication or provider"):
        build_child_environment(
            {"HOME": str(tmp_path), pass_name: "override"},
            EnvironmentConfig(cli_dir=Path("/opt/claude"), pass_names=(pass_name,)),
        )


def test_environment_fingerprint_is_sorted_and_value_sensitive() -> None:
    """Attestation hashes a stable binary representation without exposing values."""
    first = {"B": "two", "A": "one"}
    second = {"A": "one", "B": "two"}
    changed = {"A": "one", "B": "three"}
    expected = hashlib.sha256(b"A\x00one\x00B\x00two\x00").hexdigest()

    assert environment_fingerprint(first) == expected
    assert environment_fingerprint(second) == expected
    assert environment_fingerprint(changed) != expected
