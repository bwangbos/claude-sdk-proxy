from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from claude_sdk_proxy.domain import (
    CanonicalMessage,
    Completed,
    ConversationEvent,
    SdkSessionFactory,
    SdkSessionProtocol,
    TextDelta,
    TextRequest,
)
from claude_sdk_proxy.replay_stream import ReplayStream

_EXPLICIT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class SessionConflict(RuntimeError):
    pass


class SessionMismatch(ValueError):
    pass


class SessionTimeout(TimeoutError):
    pass


@dataclass(slots=True)
class _Conversation:
    external_id: str
    explicit: bool
    model: str
    system: str
    transcript: tuple[CanonicalMessage, ...]
    backend: SdkSessionProtocol
    in_flight_fingerprint: str | None = None
    replay: dict[str, tuple[ConversationEvent, ...]] = field(default_factory=dict)


def _fingerprint(request: TextRequest) -> str:
    messages = tuple((item.role, item.content) for item in request.messages)
    identity = (request.model, request.system, messages)
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(slots=True)
class TurnLease:
    _registry: SessionRegistry
    _conversation: _Conversation
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

    def __post_init__(self) -> None:
        conversation = self._conversation
        self.response_headers = {"X-Claude-Proxy-Session": conversation.external_id}

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
                async for event in self._conversation.backend.stream_turn(
                    self._request.next_prompt
                ):
                    if self._aborted:
                        raise RuntimeError("turn was aborted")
                    events.append(event)
                    if isinstance(event, TextDelta):
                        assistant_parts.append(event.text)
                        yield event
                        continue
                    if isinstance(event, Completed):
                        await self._registry._commit(
                            self._conversation, self._request, self._fingerprint,
                            tuple(events), "".join(assistant_parts),
                        )
                        self._committed = True
                        yield event
                        return
                raise RuntimeError("backend stream ended without completion")
        except TimeoutError:
            raise SessionTimeout("SDK turn timed out") from None
        finally:
            if not self._committed:
                await self._registry._invalidate(self._conversation)

    async def abort(self) -> None:
        async with self._abort_lock:
            if self._aborted:
                return
            if self._replay is None:
                self._aborted = True
                await self._registry._invalidate(self._conversation)
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


class SessionRegistry:
    def __init__(self, session_factory: SdkSessionFactory,
        turn_timeout_seconds: float = 300.0,
    ) -> None:
        if turn_timeout_seconds <= 0:
            raise ValueError("turn timeout must be positive")
        self._session_factory = session_factory
        self._turn_timeout_seconds = turn_timeout_seconds
        self._lock = asyncio.Lock()
        self._explicit: dict[str, _Conversation] = {}
        self._implicit: dict[str, _Conversation] = {}
        self._closed = False

    async def open_turn(
        self, request: TextRequest, explicit_id: str | None
    ) -> TurnLease:
        if explicit_id is not None and _EXPLICIT_ID.fullmatch(explicit_id) is None:
            raise SessionMismatch("invalid explicit session ID")
        fingerprint = _fingerprint(request)
        async with self._lock:
            if self._closed:
                raise RuntimeError("session registry is closed")
            if explicit_id is not None:
                return self._open_explicit(request, explicit_id, fingerprint)
            return self._open_implicit(request, fingerprint)

    def _open_explicit(
        self, request: TextRequest, explicit_id: str, fingerprint: str
    ) -> TurnLease:
        conversation = self._explicit.get(explicit_id)
        if conversation is None:
            if len(request.messages) != 1:
                raise SessionMismatch("request transcript is not a fresh conversation")
            conversation = self._create(request, explicit_id, explicit=True)
            self._explicit[explicit_id] = conversation
            return self._new_lease(conversation, request, fingerprint)

        if request.model != conversation.model:
            raise SessionMismatch("session model does not match")
        if request.system != conversation.system:
            raise SessionMismatch("session system does not match")
        self._reject_busy(conversation, fingerprint)
        replay = conversation.replay.get(fingerprint)
        if replay is not None:
            return self._new_lease(conversation, request, fingerprint, replay)
        if not self._is_continuation(conversation, request):
            raise SessionMismatch("request transcript does not match conversation")
        return self._new_lease(conversation, request, fingerprint)

    def _open_implicit(self, request: TextRequest, fingerprint: str) -> TurnLease:
        exact = [
            conversation
            for conversation in self._implicit.values()
            if fingerprint == conversation.in_flight_fingerprint
            or fingerprint in conversation.replay
        ]
        if len(exact) > 1:
            raise SessionMismatch("request transcript matches multiple conversations")
        if exact:
            conversation = exact[0]
            self._reject_busy(conversation, fingerprint)
            replay = conversation.replay[fingerprint]
            return self._new_lease(conversation, request, fingerprint, replay)

        continuations = [
            conversation
            for conversation in self._implicit.values()
            if request.model == conversation.model
            and request.system == conversation.system
            and self._is_continuation(conversation, request)
        ]
        if len(continuations) > 1:
            raise SessionMismatch("request transcript matches multiple conversations")
        if continuations:
            conversation = continuations[0]
            self._reject_busy(conversation, fingerprint)
            return self._new_lease(conversation, request, fingerprint)
        if len(request.messages) != 1:
            raise SessionMismatch("request transcript does not match a conversation")

        external_id = uuid.uuid4().hex
        conversation = self._create(request, external_id, explicit=False)
        self._implicit[external_id] = conversation
        return self._new_lease(conversation, request, fingerprint)

    def _create(self, request: TextRequest, sid: str, explicit: bool) -> _Conversation:
        backend = self._session_factory(request.model, request.system)
        return _Conversation(sid, explicit, request.model, request.system, (), backend)

    def _new_lease(
        self, conversation: _Conversation, request: TextRequest,
        fingerprint: str,
        replay: tuple[ConversationEvent, ...] | None = None,
    ) -> TurnLease:
        deadline = None
        conversation.in_flight_fingerprint = fingerprint
        if replay is None:
            deadline = asyncio.get_running_loop().time() + self._turn_timeout_seconds
        return TurnLease(self, conversation, request, fingerprint, deadline, replay)

    @staticmethod
    def _is_continuation(conversation: _Conversation, request: TextRequest) -> bool:
        transcript = conversation.transcript
        expected = transcript + (request.messages[-1],)
        return bool(transcript) and request.messages == expected

    @staticmethod
    def _reject_busy(conversation: _Conversation, fingerprint: str) -> None:
        active = conversation.in_flight_fingerprint
        if active == fingerprint:
            raise SessionConflict("request is already in flight")
        if active is not None:
            raise SessionConflict("conversation is busy")

    async def _commit(
        self, conversation: _Conversation, request: TextRequest,
        fingerprint: str, events: tuple[ConversationEvent, ...],
        assistant_text: str,
    ) -> None:
        async with self._lock:
            conversations = self._explicit if conversation.explicit else self._implicit
            if conversations.get(conversation.external_id) is not conversation:
                raise RuntimeError("conversation is no longer active")
            if conversation.in_flight_fingerprint != fingerprint:
                raise RuntimeError("turn reservation is no longer active")
            assistant = CanonicalMessage("assistant", assistant_text)
            conversation.transcript = request.messages + (assistant,)
            conversation.replay[fingerprint] = events
            conversation.in_flight_fingerprint = None

    async def _invalidate(self, conversation: _Conversation) -> None:
        should_close = False
        async with self._lock:
            conversations = self._explicit if conversation.explicit else self._implicit
            if conversations.get(conversation.external_id) is conversation:
                del conversations[conversation.external_id]
                conversation.in_flight_fingerprint = None
                should_close = True
        if should_close:
            await conversation.backend.close()

    async def _release(self, conversation: _Conversation, fingerprint: str) -> None:
        async with self._lock:
            if conversation.in_flight_fingerprint == fingerprint:
                conversation.in_flight_fingerprint = None

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            conversations = [*self._explicit.values(), *self._implicit.values()]
            self._explicit.clear()
            self._implicit.clear()
        results = await asyncio.gather(
            *(conversation.backend.close() for conversation in conversations),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
