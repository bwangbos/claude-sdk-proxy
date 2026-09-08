from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import math
import secrets
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

import httpx

from .storage import CredentialRevisionError, Credentials, CredentialStore

CLIENT_ID: Final = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTHORIZATION_URL: Final = "https://auth.openai.com/oauth/authorize"
TOKEN_URL: Final = "https://auth.openai.com/oauth/token"
REDIRECT_URI: Final = "http://localhost:1455/auth/callback"
SCOPE: Final = "openid profile email offline_access"
_ACCOUNT_CLAIM: Final = "https://api.openai.com/auth"


class AuthenticationError(RuntimeError):
    """Sanitized, actionable authentication failure."""


class AuthorizationRevokedError(AuthenticationError):
    """Refresh token is no longer authorized."""


@dataclass(frozen=True)
class PKCE:
    verifier: str
    challenge: str


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def create_pkce() -> PKCE:
    verifier = _base64url(secrets.token_bytes(32))
    return PKCE(verifier, _base64url(hashlib.sha256(verifier.encode()).digest()))


def _extract_account_id(access_token: str) -> str:
    try:
        parts = access_token.split(".")
        if len(parts) != 3:
            raise ValueError
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        account_id = decoded[_ACCOUNT_CLAIM]["chatgpt_account_id"]
        if not isinstance(account_id, str) or not account_id:
            raise ValueError
        return account_id
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise AuthenticationError(
            "OpenAI token did not contain a valid account identity"
        ) from error


BrowserOpener = Callable[[str], Awaitable[None] | None]


class OAuthLogin:
    def __init__(
        self, *, client: httpx.AsyncClient, clock: Callable[[], float] = time.time
    ) -> None:
        self._client = client
        self._clock = clock

    async def exchange_code(self, code: str, verifier: str) -> Credentials:
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": REDIRECT_URI,
            },
            operation="login",
        )

    async def refresh(self, refresh_token: str) -> Credentials:
        return await self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
            },
            operation="refresh",
        )

    async def _token_request(
        self, form: dict[str, str], *, operation: str
    ) -> Credentials:
        try:
            response = await self._client.post(TOKEN_URL, data=form)
        except (TimeoutError, httpx.HTTPError) as error:
            raise AuthenticationError(
                f"OpenAI {operation} could not reach the authentication service"
            ) from error
        if not response.is_success:
            if operation == "refresh" and response.status_code in (400, 401, 403):
                raise AuthorizationRevokedError(
                    "OpenAI authorization expired or was revoked; log in again"
                )
            raise AuthenticationError(f"OpenAI {operation} was rejected")
        try:
            data: Any = response.json()
            access, refresh, expires_in = (
                data["access_token"],
                data["refresh_token"],
                data["expires_in"],
            )
            expires_seconds = float(expires_in)
            expires_at = self._clock() + expires_seconds
            if (
                not isinstance(access, str)
                or not access
                or not isinstance(refresh, str)
                or not refresh
                or not isinstance(expires_in, (int, float))
                or isinstance(expires_in, bool)
                or expires_in <= 0
                or not math.isfinite(expires_seconds)
                or not math.isfinite(expires_at)
            ):
                raise ValueError
            account_id = _extract_account_id(access)
        except (
            KeyError,
            OverflowError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise AuthenticationError(
                f"OpenAI {operation} returned an invalid token response"
            ) from error
        return Credentials(access, refresh, expires_at, account_id)

    async def run(self, *, open_browser: BrowserOpener, timeout: float) -> Credentials:
        pkce, state = create_pkce(), secrets.token_hex(16)
        result: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        used = False

        async def callback(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            nonlocal used
            status, message = "400 Bad Request", "Invalid OAuth callback."
            try:
                request_line = (await asyncio.wait_for(reader.readline(), 1.0)).decode(
                    "ascii", "replace"
                )
                fields = request_line.rstrip("\r\n").split(" ")
                if len(fields) == 3 and fields[0] == "GET":
                    url = urllib.parse.urlsplit(fields[1])
                    query = urllib.parse.parse_qs(url.query)
                    supplied, code = (
                        query.get("state", [""])[0],
                        query.get("code", [""])[0],
                    )
                    authorization_error = query.get("error", [""])[0]
                    if url.path != "/auth/callback":
                        status, message = "404 Not Found", "Callback route not found."
                    elif used:
                        status, message = "409 Conflict", "Callback already used."
                    elif not supplied or not hmac.compare_digest(supplied, state):
                        message = "Invalid OAuth state."
                    elif authorization_error:
                        used = True
                        message = "Authentication was cancelled."
                        if not result.done():
                            result.set_exception(
                                AuthenticationError("OpenAI login was cancelled")
                            )
                    elif not code:
                        message = "Missing authorization code."
                    else:
                        used = True
                        status, message = "200 OK", "Authentication completed."
                        if not result.done():
                            result.set_result(code)
                body = message.encode()
                headers = (
                    f"HTTP/1.1 {status}\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    "Connection: close\r\n\r\n"
                )
                writer.write(headers.encode() + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        try:
            server = await asyncio.start_server(callback, "127.0.0.1", 1455)
        except OSError as error:
            raise AuthenticationError(
                "OpenAI login callback port 1455 is unavailable"
            ) from error
        try:
            query = urllib.parse.urlencode(
                {
                    "response_type": "code",
                    "client_id": CLIENT_ID,
                    "redirect_uri": REDIRECT_URI,
                    "scope": SCOPE,
                    "code_challenge": pkce.challenge,
                    "code_challenge_method": "S256",
                    "state": state,
                    "id_token_add_organizations": "true",
                    "codex_cli_simplified_flow": "true",
                    "originator": "claude-sdk-proxy",
                }
            )
            opened = open_browser(f"{AUTHORIZATION_URL}?{query}")
            if inspect.isawaitable(opened):
                await opened
            try:
                code = await asyncio.wait_for(result, timeout)
            except TimeoutError as error:
                raise AuthenticationError("OpenAI login timed out") from error
            return await self.exchange_code(code, pkce.verifier)
        finally:
            server.close()
            await server.wait_closed()


class CredentialManager:
    """Async credential lifecycle used by later CLI and transport tasks."""

    def __init__(
        self,
        store: CredentialStore | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store or CredentialStore()
        self._client, self._owns_client, self._clock = client, client is None, clock

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def login(
        self, *, open_browser: BrowserOpener, timeout: float = 300.0
    ) -> Credentials:
        generation = await self.store.snapshot_generation()
        credentials = await OAuthLogin(
            client=await self._get_client(), clock=self._clock
        ).run(open_browser=open_browser, timeout=timeout)
        try:
            return await self.store.save_if_generation(
                credentials, expected_generation=generation
            )
        except CredentialRevisionError as error:
            raise AuthenticationError(
                "Credentials changed during login; retry"
            ) from error

    async def credentials(self) -> Credentials:
        current = await self.store.load()
        if current is None:
            raise AuthenticationError(
                "OpenAI is not authenticated; run `claude-proxy login openai`"
            )
        if current.expires_at > self._clock() + 60:
            return current
        return await self._refresh(current, rejected_access_token=None)

    async def refresh_once(self, rejected_access_token: str) -> Credentials:
        current = await self.store.load()
        if current is None:
            raise AuthenticationError(
                "OpenAI is not authenticated; run `claude-proxy login openai`"
            )
        if not hmac.compare_digest(current.access_token, rejected_access_token):
            return current
        return await self._refresh(current, rejected_access_token=rejected_access_token)

    async def _refresh(
        self, initial: Credentials, *, rejected_access_token: str | None
    ) -> Credentials:
        try:
            async with self.store.refresh_lock():
                current = await self.store.load()
                if current is None:
                    raise AuthenticationError("Credentials changed during refresh")
                if current.revision != initial.revision:
                    return current
                if (
                    rejected_access_token is None
                    and current.expires_at > self._clock() + 60
                ):
                    return current
                if rejected_access_token is not None and not hmac.compare_digest(
                    current.access_token, rejected_access_token
                ):
                    return current
                try:
                    refreshed = await OAuthLogin(
                        client=await self._get_client(), clock=self._clock
                    ).refresh(current.refresh_token)
                except AuthorizationRevokedError:
                    try:
                        await self.store.delete(expected_revision=current.revision)
                    except CredentialRevisionError:
                        pass
                    raise
                if refreshed.account_id != current.account_id:
                    raise AuthenticationError(
                        "OpenAI refresh returned a different account identity; "
                        "log in again"
                    )
                try:
                    return await self.store.save(
                        refreshed, expected_revision=current.revision
                    )
                except CredentialRevisionError as error:
                    raise AuthenticationError(
                        "Credentials changed during refresh; retry the request"
                    ) from error
        except TimeoutError as error:
            raise AuthenticationError(
                "Timed out waiting to refresh OpenAI login"
            ) from error

    async def logout(self) -> bool:
        return await self.store.delete()

    async def status(self) -> dict[str, bool | float | str | None]:
        current = await self.store.load()
        if current is None:
            return {
                "authenticated": False,
                "account_id": None,
                "expires_at": None,
                "expired": None,
            }
        return {
            "authenticated": True,
            "account_id": current.account_id,
            "expires_at": current.expires_at,
            "expired": current.expires_at <= self._clock(),
        }
