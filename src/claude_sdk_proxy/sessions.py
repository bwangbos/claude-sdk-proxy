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

_EXPLICIT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SESSION_HEADER = "X-Claude-Proxy-Session"


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


class TurnLease:
    def __init__(
        self,
        registry: SessionRegistry,
        conversation: _Conversation,
        request: TextRequest,
        fingerprint: str,
        deadline: float | None,
        replay: tuple[ConversationEvent, ...] | None,
    ) -> None:
        self._registry = registry
        self._conversation = conversation
        self._request = request
        self._fingerprint = fingerprint
        self._deadline = deadline
        self._replay = replay
        self.response_headers = {_SESSION_HEADER: conversation.external_id}
        self._stream_started = False
        self._committed = False
        self._aborted = False
        self._abort_lock = asyncio.Lock()

    async def stream(self) -> AsyncIterator[ConversationEvent]:
        if self._stream_started:
            raise RuntimeError("turn stream can only be consumed once")
        self._stream_started = True
        if self._aborted:
            raise RuntimeError("turn was aborted")
        if self._replay is not None:
            for event in self._replay:
                if self._aborted:
                    raise RuntimeError("turn was aborted")
                yield event
            return

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
            self._aborted = True
            if self._replay is None:
                await self._registry._invalidate(self._conversation)


class SessionRegistry:
    def __init__(
        self,
        session_factory: SdkSessionFactory,
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

    def _create(
        self, request: TextRequest, external_id: str, *, explicit: bool
    ) -> _Conversation:
        backend = self._session_factory(request.model, request.system)
        return _Conversation(
            external_id, explicit, request.model, request.system, (), backend
        )

    def _new_lease(
        self,
        conversation: _Conversation,
        request: TextRequest,
        fingerprint: str,
        replay: tuple[ConversationEvent, ...] | None = None,
    ) -> TurnLease:
        deadline = None
        if replay is None:
            conversation.in_flight_fingerprint = fingerprint
            deadline = asyncio.get_running_loop().time() + self._turn_timeout_seconds
        return TurnLease(self, conversation, request, fingerprint, deadline, replay)

    @staticmethod
    def _is_continuation(conversation: _Conversation, request: TextRequest) -> bool:
        transcript = conversation.transcript
        return bool(transcript) and (
            len(request.messages) == len(transcript) + 1
            and request.messages[:-1] == transcript
        )

    @staticmethod
    def _reject_busy(conversation: _Conversation, fingerprint: str) -> None:
        active = conversation.in_flight_fingerprint
        if active is None:
            return
        if active == fingerprint:
            raise SessionConflict("request is already in flight")
        raise SessionConflict("conversation is busy")

    async def _commit(
        self,
        conversation: _Conversation,
        request: TextRequest,
        fingerprint: str,
        events: tuple[ConversationEvent, ...],
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
