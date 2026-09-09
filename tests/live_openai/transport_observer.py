from __future__ import annotations

import asyncio
import re

import httpx

_MAX_EVENT_PREFIX = 4096
_TYPE = re.compile(rb'"type"\s*:\s*"([^"\\]+)"')
_TERMINAL_TYPES = {
    b"response.completed",
    b"response.incomplete",
    b"response.failed",
    b"error",
}


class _BoundedSSETerminalDetector:
    """Retain only a small event prefix, solely long enough to read its type."""

    def __init__(self) -> None:
        self._line = bytearray()
        self._event_prefix = bytearray()
        self.terminal_seen = False

    def feed(self, chunk: bytes) -> None:
        for value in chunk:
            if value == 10:
                self._finish_line()
            elif len(self._line) < _MAX_EVENT_PREFIX:
                self._line.append(value)

    def _finish_line(self) -> None:
        line = bytes(self._line).removesuffix(b"\r")
        self._line.clear()
        if not line:
            self._event_prefix.clear()
            return
        if not line.startswith(b"data:"):
            return
        data = line[5:].removeprefix(b" ")
        remaining = _MAX_EVENT_PREFIX - len(self._event_prefix)
        if remaining > 0:
            self._event_prefix.extend(data[:remaining])
        match = _TYPE.search(self._event_prefix)
        if match is not None and match.group(1) in _TERMINAL_TYPES:
            self.terminal_seen = True


class _ObservedStream(httpx.AsyncByteStream):
    def __init__(self, inner: httpx.AsyncByteStream, owner: ObservedAsyncTransport):
        self._inner = inner
        self._owner = owner
        self._closed = False

    async def __aiter__(self):
        async for chunk in self._inner:
            self._owner._terminal_detector.feed(chunk)
            self._owner.terminal_seen = self._owner._terminal_detector.terminal_seen
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
        self.terminal_seen = False
        self.opened = asyncio.Event()
        self.closed = asyncio.Event()
        self._terminal_detector = _BoundedSSETerminalDetector()

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
