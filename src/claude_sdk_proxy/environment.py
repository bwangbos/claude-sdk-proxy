"""Fail-closed construction of the Claude Agent SDK child environment."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_INHERITED_NAMES = frozenset(
    {
        "HOME",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "CLAUDE_CONFIG_DIR",
    }
)
_NETWORK_PROXY_NAMES = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
_PROXY_OWNED_PREFIX = "LOCAL_PROXY_"
_FIXED_ISOLATION_ENV = {
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
_CLOUD_PROVIDER_PREFIXES = (
    "AWS_",
    "GOOGLE_",
    "GOOGLE_CLOUD_",
    "AZURE_",
    "VERTEX",
    "VERTEXAI_",
    "BEDROCK_",
    "CLOUDSDK_",
    "CLOUD_ML_",
)
_AUTH_OR_PROVIDER_TERMS = (
    "API_KEY",
    "OAUTH",
    "AUTH_TOKEN",
    "ACCESS_TOKEN",
    "SECRET_KEY",
    "ACCESS_KEY",
    "USE_",
    "USE_BEDROCK",
    "USE_VERTEX",
    "USE_FOUNDRY",
    "BEDROCK",
    "VERTEX",
    "FOUNDRY",
    "LITELLM",
    "PROVIDER",
    "BASE_URL",
    "ENDPOINT",
    "API_HOST",
    "CUSTOM_HEADERS",
    "PROFILE",
)


class EnvironmentAmbiguityError(ValueError):
    """Raised when an inherited value could select a different auth provider."""


@dataclass(frozen=True)
class EnvironmentConfig:
    """Explicit child-environment controls supplied by the local proxy."""

    cli_dir: Path
    network_proxy: bool = False
    pass_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Reject configuration that could bypass the fixed child profile."""
        if not self.cli_dir.is_absolute():
            raise ValueError("verified CLI directory must be absolute")
        for name in self.pass_names:
            if not isinstance(name, str) or not name:
                raise ValueError("pass-through names must be nonempty strings")
            if "\x00" in name:
                raise ValueError("pass-through names cannot contain NUL")
            if name.startswith(_PROXY_OWNED_PREFIX):
                raise EnvironmentAmbiguityError(
                    "proxy-owned names cannot be passed through"
                )
            if _is_authentication_or_provider_override(name):
                raise EnvironmentAmbiguityError(
                    "authentication or provider override cannot be passed through"
                )
            if name in _NETWORK_PROXY_NAMES and not self.network_proxy:
                raise ValueError("network proxy values require explicit opt-in")
            if name == "PATH" or name in _FIXED_ISOLATION_ENV:
                raise ValueError(
                    "pass-through name conflicts with fixed child environment"
                )


def _is_authentication_or_provider_override(name: str) -> bool:
    """Recognize values that can alter the SDK's credential or endpoint choice."""
    upper_name = name.upper()
    if upper_name.startswith(_CLOUD_PROVIDER_PREFIXES):
        return True
    if upper_name.startswith(("ANTHROPIC_", "CLAUDE_", "OPENAI_")):
        return any(term in upper_name for term in _AUTH_OR_PROVIDER_TERMS)
    return False


def _validate_source(source: Mapping[str, str]) -> None:
    """Fail before allowlisting whenever provenance-changing ambient state exists."""
    for name, value in source.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise TypeError("child environment names and values must be strings")
        if "\x00" in name or "\x00" in value:
            raise ValueError("child environment names and values cannot contain NUL")
        if name.startswith(_PROXY_OWNED_PREFIX):
            continue
        if _is_authentication_or_provider_override(name):
            raise EnvironmentAmbiguityError(
                "authentication or provider override is present in the child source"
            )


def build_child_environment(
    source: Mapping[str, str], config: EnvironmentConfig
) -> dict[str, str]:
    """Return the exact deny-by-default environment for one SDK child process."""
    _validate_source(source)

    environment = {name: source[name] for name in _INHERITED_NAMES if name in source}
    environment["PATH"] = f"{config.cli_dir}:/usr/bin:/bin:/usr/sbin:/sbin"

    if config.network_proxy:
        environment.update(
            {name: source[name] for name in _NETWORK_PROXY_NAMES if name in source}
        )
    environment.update(
        {
            name: source[name]
            for name in config.pass_names
            if name in source and not name.startswith(_PROXY_OWNED_PREFIX)
        }
    )
    environment.update(_FIXED_ISOLATION_ENV)
    return environment


def environment_fingerprint(env: Mapping[str, str]) -> str:
    """Return a value-safe, stable SHA-256 attestation for a child environment."""
    hasher = hashlib.sha256()
    for name in sorted(env):
        value = env[name]
        if not isinstance(name, str) or not isinstance(value, str):
            raise TypeError("child environment names and values must be strings")
        if "\x00" in name or "\x00" in value:
            raise ValueError("child environment names and values cannot contain NUL")
        hasher.update(name.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(value.encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()
