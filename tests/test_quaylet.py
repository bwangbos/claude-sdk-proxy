from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_quaylet_module_cli_help_exposes_model_option() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "quaylet.cli", "--help"],
        check=False,
        capture_output=True,
        env=os.environ.copy(),
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert "Quaylet" in result.stdout
    assert "--model" in result.stdout


@pytest.mark.anyio
async def test_default_credential_store_uses_quaylet_config_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    import importlib
    import importlib.util

    assert importlib.util.find_spec("quaylet.openai_subscription.storage") is not None
    storage = importlib.import_module("quaylet.openai_subscription.storage")
    credentials = storage.Credentials(
        "dummy-access", "dummy-refresh", 1234.0, "dummy-account"
    )
    store = storage.CredentialStore()

    saved = await store.save(credentials, expected_revision=None)

    assert store.root == tmp_path / ".config" / "quaylet"
    assert await store.load() == saved
    assert store.path == (
        tmp_path / ".config" / "quaylet" / "openai-credentials.json"
    )
