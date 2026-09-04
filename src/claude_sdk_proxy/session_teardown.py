from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


class SessionTeardown:
    def __init__(self, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("teardown timeout must be positive")
        self._timeout_seconds = timeout_seconds
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    def schedule(self, close: Callable[[], Awaitable[None]]) -> None:
        task = asyncio.create_task(self._close_best_effort(close))
        self._tasks.add(task)
        task.add_done_callback(self._consume)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._tasks:
            return
        _, pending = await asyncio.wait(
            tuple(self._tasks), timeout=self._timeout_seconds
        )
        for task in pending:
            task.cancel()

    async def _close_best_effort(
        self, close: Callable[[], Awaitable[None]]
    ) -> None:
        try:
            await close()
        except BaseException:
            pass

    def _consume(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        try:
            task.exception()
        except BaseException:
            pass
