from __future__ import annotations

import asyncio
import base64
import json
import socket
import urllib.parse
from pathlib import Path
from typing import Any

import httpx
import pytest

from claude_sdk_proxy.openai_subscription.auth import (
    AUTHORIZATION_URL,
    CLIENT_ID,
    REDIRECT_URI,
    SCOPE,
    AuthenticationError,
    CredentialManager,
    OAuthLogin,
    _extract_account_id,
    create_pkce,
)
from claude_sdk_proxy.openai_subscription.storage import Credentials, CredentialStore


def _jwt(account_id: str = "acct-test") -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
        ).encode()
    ).rstrip(b"=")
    return f"header.{payload.decode()}.signature"


def _token_response(
    *, account_id: str = "acct-test", refresh: str = "refresh-2"
) -> dict[str, Any]:
    return {
        "access_token": _jwt(account_id),
        "refresh_token": refresh,
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": SCOPE,
    }


def test_pkce_is_random_and_s256() -> None:
    first = create_pkce()
    second = create_pkce()

    assert first.verifier != second.verifier
    assert len(first.verifier) >= 43
    assert first.challenge != first.verifier
    assert "=" not in first.challenge


def test_account_identity_requires_well_formed_claim() -> None:
    assert _extract_account_id(_jwt("account-123")) == "account-123"
    for token in ("not-a-jwt", "a.e30.c", _jwt("")):
        with pytest.raises(AuthenticationError, match="account identity"):
            _extract_account_id(token)


@pytest.mark.anyio
async def test_login_binds_before_opening_browser_and_exchanges_valid_callback(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        events.append("exchange")
        form = urllib.parse.parse_qs(request.content.decode())
        assert request.url == httpx.URL("https://auth.openai.com/oauth/token")
        assert form["client_id"] == [CLIENT_ID]
        assert form["grant_type"] == ["authorization_code"]
        assert form["redirect_uri"] == [REDIRECT_URI]
        assert form["code"] == ["synthetic-code"]
        assert form["code_verifier"][0]
        return httpx.Response(200, json=_token_response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:

        async def open_browser(url: str) -> None:
            events.append("browser")
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            assert url.startswith(AUTHORIZATION_URL)
            assert query["scope"] == [SCOPE]
            reader, writer = await asyncio.open_connection("127.0.0.1", 1455)
            writer.write(
                (
                    "GET /auth/callback?code=synthetic-code&state="
                    f"{query['state'][0]} HTTP/1.1\r\nHost: localhost\r\n\r\n"
                ).encode()
            )
            await writer.drain()
            response = await reader.read()
            assert b"200 OK" in response
            writer.close()
            await writer.wait_closed()

        manager = CredentialManager(
            CredentialStore(tmp_path), client=client, clock=lambda: 1_000.0
        )
        result = await manager.login(open_browser=open_browser, timeout=1.0)

    assert result.account_id == "acct-test"
    assert events == ["browser", "exchange"]
    assert (await CredentialStore(tmp_path).load()) is not None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/auth/callback?code=x", b"400 Bad Request"),
        ("/auth/callback?code=x&state=wrong", b"400 Bad Request"),
        ("/wrong?code=x&state=anything", b"404 Not Found"),
    ],
)
async def test_login_rejects_missing_mismatched_state_and_wrong_path(
    tmp_path: Path, target: str, expected: bytes
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: pytest.fail())
    ) as client:

        async def open_browser(url: str) -> None:
            reader, writer = await asyncio.open_connection("127.0.0.1", 1455)
            writer.write(f"GET {target} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
            await writer.drain()
            assert expected in await reader.read()
            writer.close()
            await writer.wait_closed()

        login = OAuthLogin(client=client)
        with pytest.raises(AuthenticationError, match="timed out"):
            await login.run(open_browser=open_browser, timeout=0.05)

    assert not list(tmp_path.iterdir())


@pytest.mark.anyio
async def test_callback_is_single_use(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_token_response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:

        async def open_browser(url: str) -> None:
            state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
            for expected in (b"200 OK", b"409 Conflict"):
                reader, writer = await asyncio.open_connection("127.0.0.1", 1455)
                request = (
                    f"GET /auth/callback?code=x&state={state} HTTP/1.1\r\n"
                    "Host: localhost\r\n\r\n"
                )
                writer.write(request.encode())
                await writer.drain()
                assert expected in await reader.read()
                writer.close()
                await writer.wait_closed()

        await OAuthLogin(client=client).run(open_browser=open_browser, timeout=1)

    assert calls == 1


@pytest.mark.anyio
async def test_authorization_error_fails_immediately_and_closes_listener() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail())
    ) as client:

        async def open_browser(url: str) -> None:
            state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
            reader, writer = await asyncio.open_connection("127.0.0.1", 1455)
            writer.write(
                (
                    "GET /auth/callback?error=access_denied&state="
                    f"{state} HTTP/1.1\r\nHost: localhost\r\n\r\n"
                ).encode()
            )
            await writer.drain()
            assert b"400 Bad Request" in await reader.read()
            writer.close()
            await writer.wait_closed()

        with pytest.raises(AuthenticationError, match="cancelled"):
            await OAuthLogin(client=client).run(open_browser=open_browser, timeout=1.0)


@pytest.mark.anyio
async def test_login_occupied_port_fails_without_opening_browser() -> None:
    browser_opened = False
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", 1455))
    blocker.listen()
    try:
        async with httpx.AsyncClient() as client:

            async def open_browser(url: str) -> None:
                nonlocal browser_opened
                browser_opened = True

            with pytest.raises(AuthenticationError, match="callback port"):
                await OAuthLogin(client=client).run(
                    open_browser=open_browser, timeout=0.05
                )
    finally:
        blocker.close()
    assert not browser_opened


@pytest.mark.anyio
async def test_login_cancellation_closes_callback_listener() -> None:
    async with httpx.AsyncClient() as client:

        async def open_browser(url: str) -> None:
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await OAuthLogin(client=client).run(open_browser=open_browser, timeout=1)

    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 1455)
    server.close()
    await server.wait_closed()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"access_token": _jwt(), "refresh_token": "x", "expires_in": 0},
        {"access_token": "opaque", "refresh_token": "x", "expires_in": 20},
        {"access_token": _jwt(), "refresh_token": "", "expires_in": 20},
    ],
)
async def test_token_exchange_rejects_malformed_response_without_leaking_body(
    body: dict[str, Any],
) -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    async with httpx.AsyncClient(transport=transport) as client:
        login = OAuthLogin(client=client, clock=lambda: 10.0)
        with pytest.raises(AuthenticationError) as caught:
            await login.exchange_code("secret-code", "secret-verifier")
    message = str(caught.value)
    assert "secret" not in message
    assert repr(caught.value) == f"AuthenticationError({message!r})"


@pytest.mark.anyio
async def test_refresh_is_shared_across_store_instances(tmp_path: Path) -> None:
    store1 = CredentialStore(tmp_path)
    store2 = CredentialStore(tmp_path)
    await store1.save(
        Credentials(_jwt(), "refresh-1", 900.0, "acct-test"), expected_revision=None
    )
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.03)
        return httpx.Response(200, json=_token_response(refresh="refresh-2"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        managers = [
            CredentialManager(store1, client=client, clock=lambda: 1_000.0),
            CredentialManager(store2, client=client, clock=lambda: 1_000.0),
        ]
        first, second = await asyncio.gather(
            managers[0].credentials(), managers[1].credentials()
        )

    assert calls == 1
    assert first.access_token == second.access_token == _jwt()


@pytest.mark.anyio
async def test_refresh_once_uses_failed_token_as_double_check(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path)
    stale = await store.save(
        Credentials(_jwt(), "refresh-1", 9_999.0, "acct-test"), expected_revision=None
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = _token_response(refresh="refresh-2")
        body["access_token"] = f"{_jwt()}x"
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        manager = CredentialManager(store, client=client, clock=lambda: 1_000.0)
        refreshed = await manager.refresh_once(stale.access_token)
        same = await manager.refresh_once(stale.access_token)

    assert calls == 1
    assert refreshed == same


@pytest.mark.anyio
async def test_revoked_refresh_clears_credentials_and_sanitizes_error(
    tmp_path: Path,
) -> None:
    store = CredentialStore(tmp_path)
    await store.save(
        Credentials(_jwt(), "private-refresh", 0.0, "acct-test"),
        expected_revision=None,
    )
    transport = httpx.MockTransport(
        lambda r: httpx.Response(
            400, json={"error": "invalid_grant", "detail": "private-refresh"}
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        manager = CredentialManager(store, client=client, clock=lambda: 1_000.0)
        with pytest.raises(AuthenticationError, match="log in again") as caught:
            await manager.credentials()

    assert "private-refresh" not in str(caught.value)
    assert await store.load() is None


@pytest.mark.anyio
async def test_logout_wins_race_with_inflight_refresh(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path)
    await store.save(
        Credentials(_jwt(), "refresh-1", 0.0, "acct-test"), expected_revision=None
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return httpx.Response(200, json=_token_response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        manager = CredentialManager(store, client=client, clock=lambda: 1_000.0)
        refresh_task = asyncio.create_task(manager.credentials())
        await entered.wait()
        await manager.logout()
        release.set()
        with pytest.raises(AuthenticationError, match="changed"):
            await refresh_task

    assert await store.load() is None


@pytest.mark.anyio
async def test_status_is_safe_and_missing_credentials_are_actionable(
    tmp_path: Path,
) -> None:
    manager = CredentialManager(CredentialStore(tmp_path), clock=lambda: 1_000.0)
    with pytest.raises(AuthenticationError, match="login openai"):
        await manager.credentials()
    assert await manager.status() == {
        "authenticated": False,
        "account_id": None,
        "expires_at": None,
        "expired": None,
    }

    await manager.store.save(
        Credentials(_jwt("safe-account"), "do-not-show", 2_000.0, "safe-account"),
        expected_revision=None,
    )
    status = await manager.status()
    assert status == {
        "authenticated": True,
        "account_id": "safe-account",
        "expires_at": 2_000.0,
        "expired": False,
    }
    assert "do-not-show" not in repr(status)
