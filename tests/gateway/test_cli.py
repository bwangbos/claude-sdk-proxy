from __future__ import annotations

from typing import Any

import pytest

from quaylet import cli


def test_models_command_lists_catalog_offline(monkeypatch, capsys):
    def unexpected(*args, **kwargs):
        pytest.fail("offline catalog accessed authentication or started the server")

    monkeypatch.setattr(cli, "CredentialManager", unexpected)
    monkeypatch.setattr(cli, "create_app", unexpected)
    monkeypatch.setattr(cli.uvicorn, "run", unexpected)
    assert cli.main(["models"]) == 0
    output = capsys.readouterr()
    rows = {
        line.split()[0]: line.split()[1:]
        for line in output.out.splitlines()
        if line
        and line.split()[0]
        in {
            "sonnet-5",
            "opus-5",
            "opus-4.8",
            "haiku-4.5",
            "fable-5.1",
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-opus-4-5-20251101",
            "claude-sonnet-4-5-20250929",
            "gpt-6-astra",
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-5.5",
            "gpt-5.3-codex-spark",
        }
    }
    assert len(rows) == 16
    assert rows["gpt-5.3-codex-spark"] == ["ChatGPT", "no", "low,medium,high,xhigh"]
    assert rows["gpt-6-astra"] == ["ChatGPT", "yes", "low,medium,high,xhigh,max"]
    assert rows["sonnet-5"] == [
        "Claude",
        "yes",
        "off/adaptive",
        "low,medium,high,xhigh,max",
    ]
    assert rows["fable-5.1"] == [
        "Claude",
        "yes",
        "adaptive(required)",
        "low,medium,high,xhigh,max",
    ]
    assert rows["haiku-4.5"] == ["Claude", "yes", "off/manual-budget"]
    assert rows["claude-opus-4-5-20251101"] == [
        "Claude",
        "yes",
        "off/manual-budget",
        "low,medium,high",
    ]
    assert rows["claude-opus-4-6"] == [
        "Claude",
        "yes",
        "off/adaptive",
        "low,medium,high,max",
    ]
    assert "not account access" in output.out
    assert "--model" in output.out
    assert output.err == ""


def test_models_command_is_discoverable_in_help(capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(["models", "--help"])
    assert error.value.code == 0
    assert "offline" in capsys.readouterr().out.lower()


@pytest.mark.parametrize("argv", [[], ["--port", "8318"]])
def test_cli_requires_models_before_constructing_server(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli, "create_app", lambda **kwargs: calls.append("app"))
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: calls.append("run"))

    with pytest.raises(SystemExit) as error:
        cli.main(argv)

    assert error.value.code == 2
    assert "--model" in capsys.readouterr().err
    assert calls == []


@pytest.mark.parametrize("model", ["sonnet-5", "gpt-6-astra"])
def test_cli_uses_loopback_defaults_and_configured_model(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
) -> None:
    captured: dict[str, Any] = {}

    def run(app: object, *, host: str, port: int) -> None:
        captured.update(app=app, host=host, port=port)

    monkeypatch.setattr(cli.uvicorn, "run", run)
    assert cli.main(["--model", model]) == 0
    assert (captured["host"], captured["port"]) == ("127.0.0.1", 8317)


def test_cli_accepts_repeatable_models(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def create_app(
        *,
        models: tuple[str, ...],
        max_sessions: int,
        tool_result_timeout_seconds: float,
        refusal_fallback: str,
    ) -> object:
        captured["models"] = models
        captured["max_sessions"] = max_sessions
        captured["tool_result_timeout_seconds"] = tool_result_timeout_seconds
        captured["refusal_fallback"] = refusal_fallback
        return object()

    monkeypatch.setattr(cli, "create_app", create_app)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: None)
    assert cli.main(["--model", "sonnet", "--model", "opus"]) == 0
    assert captured["models"] == ("sonnet", "opus")
    assert captured["max_sessions"] == 8
    assert captured["tool_result_timeout_seconds"] == 300.0
    assert captured["refusal_fallback"] == "off"


@pytest.mark.parametrize(
    "argv",
    [
        ["--model", "sonnet-5", "gpt-5.6-terra", "opus-4.8"],
        ["--model", "sonnet-5", "gpt-5.6-terra", "--model", "opus-4.8"],
    ],
)
def test_cli_accepts_multiple_models_per_flag(argv, monkeypatch):
    captured = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: captured.append(app))
    assert cli.main(argv) == 0
    assert captured[0].state.model_order == ("sonnet-5", "gpt-5.6-terra", "opus-4.8")


def test_cli_all_models_exposes_both_providers(monkeypatch):
    captured = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: captured.append(app))
    assert cli.main(["--all-models"]) == 0
    models = captured[0].state.model_order
    assert {
        "sonnet-5",
        "opus-5",
        "opus-4.8",
        "haiku-4.5",
        "gpt-6-astra",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.3-codex-spark",
    } <= set(models)
    assert len(models) == len(set(models))


@pytest.mark.parametrize(
    "argv",
    [
        ["--all-models", "--model", "sonnet-5"],
        ["--model"],
        ["--all-models", "--model", "opus-5", "sonnet-5"],
    ],
)
def test_cli_rejects_conflicting_or_empty_selection(argv, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid selection started the server")

    monkeypatch.setattr(cli.uvicorn, "run", unexpected)
    with pytest.raises(SystemExit) as error:
        cli.main(argv)
    assert error.value.code == 2


def test_cli_threads_configured_max_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def create_app(
        *,
        models: tuple[str, ...],
        max_sessions: int,
        tool_result_timeout_seconds: float,
        refusal_fallback: str,
    ) -> object:
        captured.update(
            models=models,
            max_sessions=max_sessions,
            tool_result_timeout_seconds=tool_result_timeout_seconds,
            refusal_fallback=refusal_fallback,
        )
        return object()

    monkeypatch.setattr(cli, "create_app", create_app)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: None)

    assert cli.main(["--model", "sonnet-5", "--max-sessions", "3"]) == 0
    assert captured == {
        "models": ("sonnet-5",),
        "max_sessions": 3,
        "tool_result_timeout_seconds": 300.0,
        "refusal_fallback": "off",
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
        refusal_fallback: str,
    ) -> object:
        captured.update(
            models=models,
            max_sessions=max_sessions,
            tool_result_timeout_seconds=tool_result_timeout_seconds,
            refusal_fallback=refusal_fallback,
        )
        return object()

    monkeypatch.setattr(cli, "create_app", create_app)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: None)

    assert cli.main(["--model", "sonnet-5", "--tool-result-timeout", "12.5"]) == 0
    assert captured["tool_result_timeout_seconds"] == 12.5


def test_cli_threads_refusal_fallback_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def create_app(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(cli, "create_app", create_app)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: None)
    assert cli.main(["--model", "sonnet-5", "--refusal-fallback", "off"]) == 0
    assert captured["refusal_fallback"] == "off"


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
        cli.main(["--model", "sonnet-5", "--host", host])
    assert error.value.code == 2
