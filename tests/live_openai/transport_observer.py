from __future__ import annotations

import asyncio

import httpx


class _ObservedStream(httpx.AsyncByteStream):
    def __init__(self, inner: httpx.AsyncByteStream, owner: ObservedAsyncTransport):
        self._inner = inner
        self._owner = owner
        self._closed = False

    async def __aiter__(self):
        async for chunk in self._inner:
            yield chunk

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._inner.aclose()
        finally:
            self._owner.active -= 1
            self._owner.closed.set()


class ObservedAsyncTransport(httpx.AsyncBaseTransport):
    """Delegate to a real transport while observing one upstream host's streams."""

    def __init__(self, inner: httpx.AsyncBaseTransport, *, observed_host: str) -> None:
        self._inner = inner
        self._observed_host = observed_host
        self.active = 0
        self.opened = asyncio.Event()
        self.closed = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        if request.url.host != self._observed_host:
            return response
        stream = response.stream
        if not isinstance(stream, httpx.AsyncByteStream):
            raise TypeError("async transport returned a non-async response stream")
        self.active += 1
        self.opened.set()
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=_ObservedStream(stream, self),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()
