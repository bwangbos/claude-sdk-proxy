from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, cast

from claude_sdk_proxy.domain import (
    CanonicalBlock,
    CanonicalMessage,
    Completed,
    ConversationEvent,
    SdkSessionProtocol,
    TextBlock,
    TextDelta,
    TextRequest,
    ThinkingCompleted,
)
from claude_sdk_proxy.replay_stream import ReplayStream
from claude_sdk_proxy.thinking import ThinkingOptions

if TYPE_CHECKING:
    from claude_sdk_proxy.sessions import SessionRegistry
    from claude_sdk_proxy.tool_session_actor import ToolResponse, ToolSessionActor


class SessionConflict(RuntimeError): ...


class SessionMismatch(ValueError):
    pass


class SessionTimeout(TimeoutError):
    pass


class SessionCapacity(RuntimeError):
    pass


class TurnLeaseProtocol(Protocol):
    response_headers: dict[str, str]

    def stream(self) -> AsyncIterator[ConversationEvent]: ...
    async def abort(self) -> None: ...


@dataclass(slots=True)
class Conversation:
    external_id: str
    explicit: bool
    model: str
    system: str
    dialect: str
    thinking: ThinkingOptions
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
        assistant_blocks: list[CanonicalBlock] = []
        try:
            async with asyncio.timeout_at(self._deadline):
                await self._conversation.backend.start()
            backend_stream = self._conversation.backend.stream_generation(
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
                    if assistant_blocks and isinstance(assistant_blocks[-1], TextBlock):
                        assistant_blocks[-1] = TextBlock(
                            assistant_blocks[-1].text + event.text
                        )
                    else:
                        assistant_blocks.append(TextBlock(event.text))
                    yield event
                    continue
                if isinstance(event, ThinkingCompleted):
                    assistant_blocks.append(event.block)
                if isinstance(event, Completed):
                    async with self._abort_lock:
                        if self._aborted:
                            raise RuntimeError("turn was aborted")
                        await self._registry._commit(
                            self._conversation,
                            self._request,
                            self._fingerprint,
                            tuple(events),
                            CanonicalMessage(
                                "assistant", tuple(assistant_blocks) or (TextBlock(""),)
                            ),
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


@dataclass(slots=True)
class ToolTurnLease:
    _registry: SessionRegistry
    _actor: ToolSessionActor
    _fingerprint: str
    _response: ToolResponse | None
    _replay: tuple[ConversationEvent, ...] | None = None
    response_headers: dict[str, str] = field(init=False)
    _stream_started: bool = False
    _aborted: bool = False
    _released: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        self.response_headers = {"X-Claude-Proxy-Session": self._actor.external_id}

    def stream(self) -> AsyncIterator[ConversationEvent]:
        if self._stream_started:
            raise RuntimeError("turn stream can only be consumed once")
        self._stream_started = True
        if self._aborted:
            raise RuntimeError("turn was aborted")
        if self._replay is not None:
            return ReplayStream(
                self._replay, self._release_replay, lambda: self._aborted
            )
        return self._active_stream()

    async def _active_stream(self) -> AsyncIterator[ConversationEvent]:
        from claude_sdk_proxy.tool_session_actor import STREAM_END, StreamFailure

        response = self._response
        if response is None:
            raise RuntimeError("tool turn has no response")
        finished = False
        try:
            while True:
                item = await response.queue.get()
                if item is STREAM_END:
                    finished = True
                    return
                if isinstance(item, StreamFailure):
                    raise item.error
                yield cast(ConversationEvent, item)
        finally:
            if not finished:
                await self._actor.detach(response)

    async def abort(self) -> None:
        async with self._lock:
            if self._aborted:
                return
            if self._replay is not None:
                await self._release_replay_locked()
            elif self._response is not None:
                await self._actor.detach(self._response)
            self._aborted = True

    async def _release_replay(self) -> None:
        async with self._lock:
            await self._release_replay_locked()

    async def _release_replay_locked(self) -> None:
        if self._released:
            return
        await self._registry._release_tool(self._actor, self._fingerprint)
        self._released = True
