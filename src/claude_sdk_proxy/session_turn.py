from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from claude_sdk_proxy.domain import (
    CanonicalMessage,
    Completed,
    ConversationEvent,
    SdkSessionProtocol,
    TextDelta,
    TextRequest,
)
from claude_sdk_proxy.replay_stream import ReplayStream

if TYPE_CHECKING:
    from claude_sdk_proxy.sessions import SessionRegistry


class SessionConflict(RuntimeError): ...


class SessionMismatch(ValueError):
    pass


class SessionTimeout(TimeoutError):
    pass


class SessionCapacity(RuntimeError):
    pass


@dataclass(slots=True)
class Conversation:
    external_id: str
    explicit: bool
    model: str
    system: str
    transcript: tuple[CanonicalMessage, ...]
    backend: SdkSessionProtocol
    in_flight_fingerprint: str | None = None
    replay: dict[str, tuple[ConversationEvent, ...]] = field(default_factory=dict)
    last_used: int = 0


@dataclass(slots=True)
class TurnLease:
    _registry: SessionRegistry
    _conversation: Conversation
    _request: TextRequest
    _fingerprint: str
    _deadline: float | None
    _replay: tuple[ConversationEvent, ...] | None
    response_headers: dict[str, str] = field(init=False)
    _stream_started: bool = False
    _committed: bool = False
    _aborted: bool = False
    _replay_released: bool = False
    _abort_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _invalidation: asyncio.Task[None] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.response_headers = {
            "X-Claude-Proxy-Session": self._conversation.external_id
        }

    def stream(self) -> AsyncIterator[ConversationEvent]:
        if self._stream_started:
            raise RuntimeError("turn stream can only be consumed once")
        self._stream_started = True
        if self._aborted:
            raise RuntimeError("turn was aborted")
        replay = self._replay
        if replay is not None:
            return ReplayStream(replay, self._release_replay, lambda: self._aborted)
        return self._active_stream()

    async def _active_stream(self) -> AsyncIterator[ConversationEvent]:
        events: list[ConversationEvent] = []
        assistant_parts: list[str] = []
        try:
            async with asyncio.timeout_at(self._deadline):
                await self._conversation.backend.start()
            backend_stream = self._conversation.backend.stream_turn(
                self._request.next_prompt
            )
            while True:
                try:
                    async with asyncio.timeout_at(self._deadline):
                        event = await anext(backend_stream)
                except StopAsyncIteration:
                    break
                if self._aborted:
                    raise RuntimeError("turn was aborted")
                events.append(event)
                if isinstance(event, TextDelta):
                    assistant_parts.append(event.text)
                    yield event
                    continue
                if isinstance(event, Completed):
                    async with self._abort_lock:
                        if self._aborted:
                            raise RuntimeError("turn was aborted")
                        await self._registry._commit(
                            self._conversation,
                            self._request,
                            self._fingerprint,
                            tuple(events),
                            "".join(assistant_parts),
                        )
                        self._committed = True
                    yield event
                    return
                yield event
            raise RuntimeError("backend stream ended without completion")
        except TimeoutError:
            await self._invalidate_best_effort()
            raise SessionTimeout("SDK turn timed out") from None
        except BaseException:
            await self._invalidate_best_effort()
            raise

    async def _invalidate_best_effort(self) -> None:
        if self._committed:
            return
        try:
            await self._registry._invalidate(self._conversation)
        except BaseException:
            pass

    async def abort(self) -> None:
        async with self._abort_lock:
            if self._aborted or self._committed:
                return
            if self._replay is None:
                if self._invalidation is None:
                    self._invalidation = asyncio.create_task(
                        self._registry._invalidate(self._conversation)
                    )
                try:
                    await asyncio.shield(self._invalidation)
                except asyncio.CancelledError:
                    raise
                except BaseException:
                    self._invalidation = None
                    raise
                self._aborted = True
            else:
                await self._release_replay_locked()
                self._aborted = True

    async def _release_replay(self) -> None:
        async with self._abort_lock:
            await self._release_replay_locked()

    async def _release_replay_locked(self) -> None:
        if self._replay_released:
            return
        await self._registry._release(self._conversation, self._fingerprint)
        self._replay_released = True
