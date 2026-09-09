import pytest

from claude_sdk_proxy import cli
from claude_sdk_proxy.openai_subscription.auth import AuthenticationError


@pytest.mark.parametrize("command", ["login", "logout", "auth-status"])
def test_auth_commands_use_proxy_manager_without_starting_server(
    monkeypatch, capsys, command
):
    calls = []

    class Manager:
        async def login(self, *, open_browser):
            calls.append("login")
            open_browser("https://auth.openai.com/synthetic")

        async def logout(self):
            calls.append("logout")
            return True

        async def status(self):
            calls.append("status")
            return {
                "authenticated": True,
                "account_id": "acct",
                "expired": False,
                "expires_at": 123,
            }

        async def close(self):
            calls.append("close")

    monkeypatch.setattr(cli, "CredentialManager", Manager, raising=False)
    monkeypatch.setattr("webbrowser.open", lambda url: calls.append("browser"))
    monkeypatch.setattr(
        cli.uvicorn, "run", lambda *a, **k: pytest.fail("server started")
    )
    assert cli.main([command, "openai"]) == 0
    assert (
        calls
        == (
            {
                "login": ["login", "browser", "close"],
                "logout": ["logout", "close"],
                "auth-status": ["status", "close"],
            }[command]
        )
    )
    assert "access" not in capsys.readouterr().out


def test_auth_command_errors_are_sanitized_and_close_manager(monkeypatch, capsys):
    closed = []

    class Manager:
        async def login(self, **kwargs):
            raise AuthenticationError("SECRET token")

        async def close(self):
            closed.append(True)

    monkeypatch.setattr(cli, "CredentialManager", Manager, raising=False)
    assert cli.main(["login", "openai"]) == 1
    output = capsys.readouterr()
    assert "SECRET" not in output.err + output.out
    assert closed
