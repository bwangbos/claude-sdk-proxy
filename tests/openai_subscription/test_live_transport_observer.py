import asyncio

import httpx
import pytest

from tests.live_openai.transport_observer import ObservedAsyncTransport


class HangingStream(httpx.AsyncByteStream):
    def __init__(self, chunks=(b"data: first\n\n",)):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


class FakeTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.stream = HangingStream()
        self.closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=self.stream)

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.anyio
async def test_observer_tracks_matching_response_close_and_owns_inner_transport():
    inner = FakeTransport()
    observer = ObservedAsyncTransport(inner, observed_host="chatgpt.com")
    async with httpx.AsyncClient(transport=observer) as client:
        async with client.stream("POST", "https://chatgpt.com/fixed") as response:
            assert await anext(response.aiter_bytes()) == b"data: first\n\n"
            assert observer.active == 1
            assert observer.opened.is_set()
        assert observer.active == 0
        assert observer.closed.is_set()
        assert inner.stream.closed
    assert inner.closed


@pytest.mark.anyio
async def test_observer_ignores_nonmatching_auth_host():
    inner = FakeTransport()
    observer = ObservedAsyncTransport(inner, observed_host="chatgpt.com")
    async with httpx.AsyncClient(transport=observer) as client:
        async with client.stream("POST", "https://auth.openai.com/token") as response:
            assert await anext(response.aiter_bytes())
    assert observer.active == 0
    assert not observer.opened.is_set()
    assert not observer.closed.is_set()


@pytest.mark.anyio
async def test_observer_detects_terminal_sse_type_split_across_chunks():
    inner = FakeTransport()
    inner.stream = HangingStream(
        (b'data: {"ty', b'pe":"response.com', b'pleted","response":{}}\r\n\r\n')
    )
    observer = ObservedAsyncTransport(inner, observed_host="chatgpt.com")
    async with httpx.AsyncClient(transport=observer) as client:
        async with client.stream("POST", "https://chatgpt.com/fixed") as response:
            chunks = response.aiter_bytes()
            for _ in range(3):
                await anext(chunks)
            assert observer.terminal_seen


@pytest.mark.anyio
async def test_observer_does_not_treat_ordinary_text_event_as_terminal():
    inner = FakeTransport()
    inner.stream = HangingStream(
        (b'data: {"type":"response.output_text.delta",', b'"delta":"hello"}\n\n')
    )
    observer = ObservedAsyncTransport(inner, observed_host="chatgpt.com")
    async with httpx.AsyncClient(transport=observer) as client:
        async with client.stream("POST", "https://chatgpt.com/fixed") as response:
            chunks = response.aiter_bytes()
            for _ in range(2):
                await anext(chunks)
            assert not observer.terminal_seen
