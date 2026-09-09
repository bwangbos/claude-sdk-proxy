"""Stateless HTTP/SSE subscription backend. No session actor or parked tools."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Protocol

import httpx

from quaylet.diagnostics import record
from quaylet.domain import ConversationEvent, ResponseIdentity, TextRequest

from .auth import AuthenticationError, CredentialManager
from .events import EventTranslator, SubscriptionFailure, parse_sse, safe_identifier
from .replay import ReplayCache
from .storage import Credentials
from .translation import build_body, validate_request

ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"


class CredentialProvider(Protocol):
    async def credentials(self) -> Credentials: ...
    async def refresh_once(self, rejected_access_token: str) -> Credentials: ...


class Backend:
    def __init__(
        self,
        auth: CredentialProvider | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        max_sessions: int = 128,
        timeout: float = 300.0,
    ):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=False
        )
        self._owns_client = client is None
        self._auth = auth or CredentialManager(client=self._client)
        self.cache = ReplayCache(max_entries=max_sessions)
        self.timeout = timeout
        self._leases: set[TurnLease] = set()
        self._closed = False

    async def open_turn(self, request: TextRequest) -> TurnLease:
        validate_request(request)
        if self._closed:
            raise SubscriptionFailure("closed")
        lease = TurnLease(self, request)
        self._leases.add(lease)
        return lease

    async def close(self) -> None:
        self._closed = True
        for lease in tuple(self._leases):
            await lease.abort()
        self.cache.clear()
        if self._owns_client:
            await self._client.aclose()


class TurnLease:
    def __init__(self, backend: Backend, request: TextRequest):
        self.backend = backend
        self.request = request
        self.response_headers: dict[str, str] = {
            "x-proxy-provider": "openai-subscription"
        }
        self.diagnostics: dict[str, Any] = {
            "provider": "openai-subscription",
            "requested_model": request.model,
        }
        self._response: httpx.Response | None = None
        self._closing: asyncio.Task[None] | None = None
        self._owner: asyncio.Task[Any] | None = None
        self._started = False
        self._aborted = False

    async def abort(self) -> None:
        self._aborted = True
        owner = self._owner
        if (
            owner is not None
            and owner is not asyncio.current_task()
            and not owner.done()
        ):
            owner.cancel()
        await self._close_response()
        self.backend._leases.discard(self)

    async def _close_response(self) -> None:
        response, self._response = self._response, None
        if response is not None:
            self._closing = asyncio.create_task(response.aclose())
        closing = self._closing
        if closing is not None:
            cancelled = False
            while not closing.done():
                try:
                    await asyncio.shield(closing)
                except asyncio.CancelledError:
                    cancelled = True
            closing.result()
            self._closing = None
            if cancelled:
                raise asyncio.CancelledError

    async def stream(self) -> AsyncIterator[ConversationEvent]:
        if self._started or self._aborted:
            raise SubscriptionFailure("closed")
        self._started = True
        self._owner = asyncio.current_task()
        try:
            async with asyncio.timeout(self.backend.timeout):
                async for event in self._run():
                    yield event
        except TimeoutError:
            self.diagnostics["category"] = "timeout"
            raise SubscriptionFailure("timeout") from None
        except AuthenticationError:
            self.diagnostics["category"] = "authentication"
            raise SubscriptionFailure("authentication") from None
        except SubscriptionFailure as error:
            self.diagnostics["category"] = error.category
            raise
        except asyncio.CancelledError, GeneratorExit:
            self.diagnostics["category"] = "cancelled"
            raise
        finally:
            try:
                await self._close_response()
            finally:
                self._owner = None
                self.backend._leases.discard(self)
                record("openai_subscription_turn", **self.diagnostics)

    async def _run(self) -> AsyncIterator[ConversationEvent]:
        credential = await self.backend._auth.credentials()
        yield ResponseIdentity(
            self.request.model, self.request.model, False, verified=False
        )
        refreshed = retried = emitted = False
        while True:
            if self.diagnostics.pop("reported_model", None) is not None:
                yield ResponseIdentity(
                    self.request.model, self.request.model, False, verified=False
                )
            self.diagnostics.pop("upstream_request_id", None)
            translator = EventTranslator(credential.account_id, self.request)
            replay = (
                self.backend.cache.get(self.request, credential.account_id)
                if self.request.dialect == "openai"
                else None
            )
            body = build_body(self.request, credential.account_id, replay)
            headers = {
                "Authorization": f"Bearer {credential.access_token}",
                "chatgpt-account-id": credential.account_id,
                "OpenAI-Beta": "responses=experimental",
                "Accept": "text/event-stream",
                "Content-Type": "application/json",
                "User-Agent": "quaylet/0.0.0",
                "originator": "quaylet",
            }
            try:
                outbound = self.backend._client.build_request(
                    "POST", ENDPOINT, headers=headers, json=body
                )
                self._response = await self.backend._client.send(
                    outbound, stream=True, follow_redirects=False
                )
                response = self._response
                status = response.status_code
                self.diagnostics["status"] = status
                request_id = safe_identifier(response.headers.get("x-request-id"))
                if request_id:
                    self.diagnostics["upstream_request_id"] = request_id
                if status == 401 and not refreshed and not emitted:
                    await self._close_response()
                    credential = await self.backend._auth.refresh_once(
                        credential.access_token
                    )
                    refreshed = True
                    continue
                if status in {429, 500, 502, 503, 504} and not retried and not emitted:
                    await self._close_response()
                    retried = True
                    continue
                if status != 200:
                    category = {
                        401: "authentication",
                        403: "access_denied",
                        429: "rate_limit",
                    }.get(status, "upstream_error")
                    raise SubscriptionFailure(category, status=status)
                async for raw in parse_sse(response.aiter_bytes()):
                    evidence = raw.get("response", {})
                    reported = (
                        safe_identifier(evidence.get("model"))
                        if isinstance(evidence, dict)
                        else None
                    )
                    if reported and reported != self.diagnostics.get("reported_model"):
                        self.diagnostics["reported_model"] = reported
                        yield ResponseIdentity(
                            self.request.model, reported, False, verified=True
                        )
                    for event in translator.consume(raw):
                        emitted = True
                        yield event
                    if translator.finished:
                        self.diagnostics["category"] = (
                            "completed" if translator.cacheable else "incomplete"
                        )
                        if (
                            translator.cacheable
                            and not self._aborted
                            and self.request.dialect == "openai"
                        ):
                            self.backend.cache.put(
                                self.request,
                                credential.account_id,
                                self.request.messages
                                + (translator.assistant_message(),),
                                body["input"] + translator.output,
                            )
                        return
                raise SubscriptionFailure("missing_completion")
            except SubscriptionFailure as error:
                if error.transient and not emitted and not retried:
                    retried = True
                    continue
                raise
            except httpx.TransportError as error:
                if not emitted and not retried:
                    retried = True
                    continue
                raise SubscriptionFailure(
                    "timeout"
                    if isinstance(error, httpx.TimeoutException)
                    else "transport"
                ) from None
            finally:
                await self._close_response()


__all__ = ["Backend", "TurnLease", "SubscriptionFailure"]
