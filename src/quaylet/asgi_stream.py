from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Protocol

from starlette.types import ASGIApp, Receive, Scope, Send

from quaylet.domain import ConversationEvent
from quaylet.session_turn import TurnLeaseProtocol

type EventEncoder = Callable[[ConversationEvent], tuple[bytes, ...]]
type ErrorEncoder = Callable[[Exception], tuple[bytes, ...]]


class ClosableEventStream(AsyncIterator[ConversationEvent], Protocol):
    async def aclose(self) -> None: ...


class DisconnectMonitor:
    def __init__(self, receive: Receive) -> None:
        self._receive = receive
        self._task: asyncio.Task[None] | None = None
        self._owner: asyncio.Task[object] | None = None
        self._disconnect_cancels = 0
        self._response: ASGIApp | None = None
        self.disconnected = False

    async def start(self) -> None:
        owner = asyncio.current_task()
        if owner is None:  # pragma: no cover - asyncio always owns an ASGI task
            raise RuntimeError("ASGI request has no owner task")
        self._owner = owner
        self._task = asyncio.create_task(self._listen(owner))
        await asyncio.sleep(0)

    async def _listen(self, owner: asyncio.Task[object]) -> None:
        while (await self._receive())["type"] != "http.disconnect":
            pass
        self.disconnected = True
        before = owner.cancelling()
        owner.cancel()
        self._disconnect_cancels += owner.cancelling() - before

    def consume_disconnect_cancellation(self) -> bool:
        owner = self._owner
        if owner is None:
            return False
        if not self.disconnected or self._disconnect_cancels == 0:
            return False
        current = asyncio.current_task()
        if current is None or owner is not current:
            return False
        while self._disconnect_cancels and current.cancelling():
            current.uncancel()
            self._disconnect_cancels -= 1
        return self._disconnect_cancels == 0 and current.cancelling() == 0

    def wrap(self, response: ASGIApp | None) -> DisconnectMonitor:
        self._response = response
        return self

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            if not self.disconnected and self._response is not None:
                await self._response(scope, receive, send)
        except asyncio.CancelledError:
            if not self.consume_disconnect_cancellation():
                raise
        finally:
            await self.close()

    async def close(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)


class EventStreamResponse:
    def __init__(
        self,
        lease: TurnLeaseProtocol,
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
        del scope, receive
        await self._send_response(send)

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
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except asyncio.CancelledError:
            await cleanup_best_effort(self._lease, self._stream)
            raise
        except ConnectionError, BrokenPipeError:
            await cleanup_best_effort(self._lease, self._stream)
            raise
        except Exception as error:
            if cancellation_pending():
                await cleanup_best_effort(self._lease, self._stream)
                raise asyncio.CancelledError from None
            await self._send_stream_error(send, error)
            return
        finally:
            await close_best_effort(self._stream)

    async def _send_event(self, send: Send, event: ConversationEvent) -> None:
        for chunk in self._encode_event(event):
            await self._send_chunk(send, chunk)

    @staticmethod
    async def _send_chunk(send: Send, chunk: bytes) -> None:
        await send({"type": "http.response.body", "body": chunk, "more_body": True})

    async def _send_stream_error(self, send: Send, error: Exception) -> None:
        await cleanup_best_effort(self._lease, self._stream)
        for chunk in self._encode_error(error):
            await self._send_chunk(send, chunk)
        await send({"type": "http.response.body", "body": b"", "more_body": False})

async def abort_best_effort(lease: TurnLeaseProtocol) -> None:
    try:
        await lease.abort()
    except BaseException:
        pass


async def close_best_effort(stream: ClosableEventStream) -> None:
    try:
        await stream.aclose()
    except BaseException:
        pass


async def cleanup_best_effort(
    lease: TurnLeaseProtocol, stream: ClosableEventStream
) -> None:
    await abort_best_effort(lease)
    await close_best_effort(stream)


def cancellation_pending() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0
