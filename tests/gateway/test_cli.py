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

    def create_app(
        *,
        models: tuple[str, ...],
        max_sessions: int,
        tool_result_timeout_seconds: float,
    ) -> object:
        captured["models"] = models
        captured["max_sessions"] = max_sessions
        captured["tool_result_timeout_seconds"] = tool_result_timeout_seconds
        return object()

    monkeypatch.setattr(cli, "create_app", create_app)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: None)
    assert cli.main(["--model", "sonnet", "--model", "opus"]) == 0
    assert captured["models"] == ("sonnet", "opus")
    assert captured["max_sessions"] == 8
    assert captured["tool_result_timeout_seconds"] == 300.0


def test_cli_threads_configured_max_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def create_app(
        *,
        models: tuple[str, ...],
        max_sessions: int,
        tool_result_timeout_seconds: float,
    ) -> object:
        captured.update(
            models=models,
            max_sessions=max_sessions,
            tool_result_timeout_seconds=tool_result_timeout_seconds,
        )
        return object()

    monkeypatch.setattr(cli, "create_app", create_app)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: None)

    assert cli.main(["--max-sessions", "3"]) == 0
    assert captured == {
        "models": ("sonnet",),
        "max_sessions": 3,
        "tool_result_timeout_seconds": 300.0,
    }


def test_cli_threads_configured_tool_result_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def create_app(
        *,
        models: tuple[str, ...],
        max_sessions: int,
        tool_result_timeout_seconds: float,
    ) -> object:
        captured.update(
            models=models,
            max_sessions=max_sessions,
            tool_result_timeout_seconds=tool_result_timeout_seconds,
        )
        return object()

    monkeypatch.setattr(cli, "create_app", create_app)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: None)

    assert cli.main(["--tool-result-timeout", "12.5"]) == 0
    assert captured["tool_result_timeout_seconds"] == 12.5


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_cli_rejects_nonpositive_or_nonfinite_tool_result_timeout(value: str) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["--tool-result-timeout", value])
    assert error.value.code == 2


@pytest.mark.parametrize("value", ["0", "-1"])
def test_cli_rejects_nonpositive_max_sessions(value: str) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["--max-sessions", value])
    assert error.value.code == 2


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.2", "localhost"])
def test_cli_rejects_every_nonliteral_loopback_host(host: str) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["--host", host])
    assert error.value.code == 2
