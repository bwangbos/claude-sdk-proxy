from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Protocol

from starlette.types import Receive, Scope, Send

from claude_sdk_proxy.domain import ConversationEvent
from claude_sdk_proxy.sessions import TurnLease

type EventEncoder = Callable[[ConversationEvent], tuple[bytes, ...]]
type ErrorEncoder = Callable[[Exception], tuple[bytes, ...]]


class ClosableEventStream(AsyncIterator[ConversationEvent], Protocol):
    async def aclose(self) -> None: ...


class EventStreamResponse:
    def __init__(
        self,
        lease: TurnLease,
        stream: ClosableEventStream,
        first: ConversationEvent,
        start_chunks: tuple[bytes, ...],
        encode_event: EventEncoder,
        encode_error: ErrorEncoder,
    ) -> None:
        self._lease = lease
        self._stream = stream
        self._first = first
        self._start_chunks = start_chunks
        self._encode_event = encode_event
        self._encode_error = encode_error

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        del scope
        owner = asyncio.current_task()
        if owner is None:  # pragma: no cover - asyncio always owns an ASGI task
            raise RuntimeError("ASGI response has no owner task")
        disconnected = asyncio.Event()
        listener = asyncio.create_task(
            self._listen_for_disconnect(receive, owner, disconnected)
        )
        try:
            await self._send_response(send)
        except asyncio.CancelledError:
            if disconnected.is_set():
                return
            raise
        finally:
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)

    async def _listen_for_disconnect(
        self,
        receive: Receive,
        owner: asyncio.Task[object],
        disconnected: asyncio.Event,
    ) -> None:
        while True:
            if (await receive())["type"] == "http.disconnect":
                await self._lease.abort()
                disconnected.set()
                owner.cancel()
                return

    async def _send_response(self, send: Send) -> None:
        headers = [
            (b"content-type", b"text/event-stream; charset=utf-8"),
            (b"cache-control", b"no-cache"),
            *[
                (key.lower().encode(), value.encode())
                for key, value in self._lease.response_headers.items()
            ],
        ]
        try:
            await send(
                {"type": "http.response.start", "status": 200, "headers": headers}
            )
            for chunk in self._start_chunks:
                await self._send_chunk(send, chunk)
            await self._send_event(send, self._first)
            async for event in self._stream:
                await self._send_event(send, event)
        except asyncio.CancelledError:
            await self._cleanup_failed_send()
            raise
        except ConnectionError, BrokenPipeError:
            await self._cleanup_failed_send()
            raise
        except Exception as error:
            await self._send_stream_error(send, error)
            return
        finally:
            await self._stream.aclose()
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def _send_event(self, send: Send, event: ConversationEvent) -> None:
        for chunk in self._encode_event(event):
            await self._send_chunk(send, chunk)

    @staticmethod
    async def _send_chunk(send: Send, chunk: bytes) -> None:
        await send({"type": "http.response.body", "body": chunk, "more_body": True})

    async def _cleanup_failed_send(self) -> None:
        await self._lease.abort()
        await self._stream.aclose()

    async def _send_stream_error(self, send: Send, error: Exception) -> None:
        await self._lease.abort()
        await self._stream.aclose()
        for chunk in self._encode_error(error):
            await self._send_chunk(send, chunk)
        await send({"type": "http.response.body", "body": b"", "more_body": False})
