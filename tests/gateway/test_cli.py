from __future__ import annotations

from typing import Any

import pytest

from claude_sdk_proxy import cli


def test_cli_uses_loopback_defaults_and_configured_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def run(app: object, *, host: str, port: int) -> None:
        captured.update(app=app, host=host, port=port)

    monkeypatch.setattr(cli.uvicorn, "run", run)
    assert cli.main([]) == 0
    assert (captured["host"], captured["port"]) == ("127.0.0.1", 8317)


def test_cli_accepts_repeatable_models(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def create_app(*, models: tuple[str, ...]) -> object:
        captured["models"] = models
        return object()

    monkeypatch.setattr(cli, "create_app", create_app)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: None)
    assert cli.main(["--model", "sonnet", "--model", "opus"]) == 0
    assert captured["models"] == ("sonnet", "opus")


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.2", "localhost"])
def test_cli_rejects_every_nonliteral_loopback_host(host: str) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["--host", host])
    assert error.value.code == 2
