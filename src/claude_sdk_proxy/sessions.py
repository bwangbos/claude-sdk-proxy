from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid

from claude_sdk_proxy.domain import (
    CanonicalMessage,
    ConversationEvent,
    SdkSessionFactory,
    TextRequest,
)
from claude_sdk_proxy.session_teardown import SessionTeardown
from claude_sdk_proxy.session_turn import Conversation
from claude_sdk_proxy.session_turn import SessionCapacity as SessionCapacity
from claude_sdk_proxy.session_turn import SessionConflict as SessionConflict
from claude_sdk_proxy.session_turn import SessionMismatch as SessionMismatch
from claude_sdk_proxy.session_turn import SessionTimeout as SessionTimeout
from claude_sdk_proxy.session_turn import TurnLease as TurnLease

_EXPLICIT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _fingerprint(request: TextRequest) -> str:
    messages = tuple((item.role, item.content) for item in request.messages)
    identity = (request.model, request.system, messages)
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class SessionRegistry:
    def __init__(
        self, session_factory: SdkSessionFactory,
        turn_timeout_seconds: float = 300.0,
        teardown_timeout_seconds: float = 5.0,
        max_sessions: int = 8,
    ) -> None:
        if turn_timeout_seconds <= 0:
            raise ValueError("turn timeout must be positive")
        if max_sessions <= 0:
            raise ValueError("max sessions must be positive")
        self._session_factory = session_factory
        self._turn_timeout_seconds = turn_timeout_seconds
        self._teardown = SessionTeardown(teardown_timeout_seconds)
        self._max_sessions = max_sessions
        self._use_counter = 0
        self._lock = asyncio.Lock()
        self._explicit: dict[str, Conversation] = {}
        self._implicit: dict[str, Conversation] = {}
        self._closed = False
    async def open_turn(
        self, request: TextRequest, explicit_id: str | None) -> TurnLease:
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
            self._make_room_for_fresh()
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
        self._make_room_for_fresh()
        conversation = self._create(request, uuid.uuid4().hex, explicit=False)
        self._implicit[conversation.external_id] = conversation
        return self._new_lease(conversation, request, fingerprint)
    def _create(self, request: TextRequest, sid: str, explicit: bool) -> Conversation:
        backend = self._session_factory(request.model, request.system)
        return Conversation(sid, explicit, request.model, request.system, (), backend)
    def _new_lease(
        self, conversation: Conversation, request: TextRequest, fingerprint: str,
        replay: tuple[ConversationEvent, ...] | None = None,
    ) -> TurnLease:
        self._use_counter += 1
        conversation.last_used = self._use_counter
        conversation.in_flight_fingerprint = fingerprint
        now = asyncio.get_running_loop().time()
        deadline = None if replay is not None else now + self._turn_timeout_seconds
        return TurnLease(self, conversation, request, fingerprint, deadline, replay)
    def _make_room_for_fresh(self) -> None:
        conversations = [*self._explicit.values(), *self._implicit.values()]
        if len(conversations) < self._max_sessions:
            return
        idle = [item for item in conversations if item.in_flight_fingerprint is None]
        if not idle:
            raise SessionCapacity("session capacity is exhausted")
        victim = min(idle, key=lambda item: item.last_used)
        retained = self._explicit if victim.explicit else self._implicit
        del retained[victim.external_id]
        self._teardown.schedule(victim.backend.close)
    @staticmethod
    def _is_continuation(conversation: Conversation, request: TextRequest) -> bool:
        transcript = conversation.transcript
        return bool(transcript) and request.messages == transcript + (
            request.messages[-1],)
    @staticmethod
    def _reject_busy(conversation: Conversation, fingerprint: str) -> None:
        active = conversation.in_flight_fingerprint
        if active == fingerprint:
            raise SessionConflict("request is already in flight")
        if active is not None:
            raise SessionConflict("conversation is busy")
    async def _commit(
        self, conversation: Conversation, request: TextRequest,
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
            conversation.replay.clear()
            conversation.replay[fingerprint] = events
            conversation.in_flight_fingerprint = None
    async def _invalidate(self, conversation: Conversation) -> None:
        scheduled = False
        async with self._lock:
            conversations = self._explicit if conversation.explicit else self._implicit
            if conversations.get(conversation.external_id) is conversation:
                del conversations[conversation.external_id]
                conversation.in_flight_fingerprint = None
                self._teardown.schedule(conversation.backend.close)
                scheduled = True
        if scheduled:
            await asyncio.sleep(0)
    async def _release(self, conversation: Conversation, fingerprint: str) -> None:
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
        for conversation in conversations:
            self._teardown.schedule(conversation.backend.close)
        await self._teardown.close()
