from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

from quaylet.domain import ConversationEvent


class ReplayStream(AsyncIterator[ConversationEvent]):
    def __init__(
        self,
        events: tuple[ConversationEvent, ...],
        close: Callable[[], Awaitable[None]],
        is_aborted: Callable[[], bool],
    ) -> None:
        self._events = events
        self._close = close
        self._is_aborted = is_aborted
        self._index = 0
        self._closed = False

    def __aiter__(self) -> ReplayStream:
        return self

    async def __anext__(self) -> ConversationEvent:
        if self._closed:
            raise StopAsyncIteration
        if self._is_aborted():
            await self.aclose()
            raise RuntimeError("turn was aborted")
        if self._index == len(self._events):
            await self.aclose()
            raise StopAsyncIteration
        event = self._events[self._index]
        self._index += 1
        return event

    async def aclose(self) -> None:
        if self._closed:
            return
        await self._close()
        self._closed = True
