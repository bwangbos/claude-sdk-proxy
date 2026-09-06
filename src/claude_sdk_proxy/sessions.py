from __future__ import annotations

import asyncio
import re
import uuid

from claude_sdk_proxy.domain import (
    CanonicalMessage,
    ConversationEvent,
    SdkSessionFactory,
    TextRequest,
)
from claude_sdk_proxy.session_identity import (
    messages_equal,
    request_fingerprint,
)
from claude_sdk_proxy.session_teardown import SessionTeardown
from claude_sdk_proxy.session_turn import Conversation
from claude_sdk_proxy.session_turn import SessionCapacity as SessionCapacity
from claude_sdk_proxy.session_turn import SessionConflict as SessionConflict
from claude_sdk_proxy.session_turn import SessionMismatch as SessionMismatch
from claude_sdk_proxy.session_turn import SessionTimeout as SessionTimeout
from claude_sdk_proxy.session_turn import ToolTurnLease as ToolTurnLease
from claude_sdk_proxy.session_turn import TurnLease as TurnLease
from claude_sdk_proxy.tool_session_actor import ToolSessionActor

_EXPLICIT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
type SessionEntry = Conversation | ToolSessionActor


class SessionRegistry:
    def __init__(
        self,
        session_factory: SdkSessionFactory,
        turn_timeout_seconds: float = 300.0,
        teardown_timeout_seconds: float = 5.0,
        max_sessions: int = 8,
        tool_result_timeout_seconds: float = 300.0,
    ) -> None:
        if turn_timeout_seconds <= 0:
            raise ValueError("turn timeout must be positive")
        if tool_result_timeout_seconds <= 0:
            raise ValueError("tool result timeout must be positive")
        if max_sessions <= 0:
            raise ValueError("max sessions must be positive")
        self._session_factory = session_factory
        self._turn_timeout_seconds = turn_timeout_seconds
        self._tool_result_timeout_seconds = tool_result_timeout_seconds
        self._teardown = SessionTeardown(teardown_timeout_seconds)
        self._max_sessions = max_sessions
        self._use_counter = 0
        self._lock = asyncio.Lock()
        self._explicit: dict[str, SessionEntry] = {}
        self._implicit: dict[str, SessionEntry] = {}
        self._closed = False

    async def open_turn(
        self, request: TextRequest, explicit_id: str | None
    ) -> TurnLease | ToolTurnLease:
        if explicit_id is not None and _EXPLICIT_ID.fullmatch(explicit_id) is None:
            raise SessionMismatch("invalid explicit session ID")
        fingerprint = request_fingerprint(request)
        replaced: SessionEntry | None = None
        async with self._lock:
            if self._closed:
                raise RuntimeError("session registry is closed")
            entry, replay, replaced = self._select(
                request, explicit_id, fingerprint
            )
            self._reserve(replaced or entry, fingerprint)
            if replaced is not None:
                entry.in_flight_fingerprint = fingerprint
            elif isinstance(entry, Conversation):
                return self._text_lease(entry, request, fingerprint, replay)
            elif replay is not None:
                return ToolTurnLease(self, entry, fingerprint, None, replay)
        if replaced is not None:
            await self._install_rebase(entry, replaced, fingerprint)
            if isinstance(entry, Conversation):
                return self._text_lease(entry, request, fingerprint, None)
        actor = entry
        assert isinstance(actor, ToolSessionActor)
        try:
            response = await actor.admit(request, fingerprint)
        except BaseException:
            await self._release_tool(actor, fingerprint)
            raise
        return ToolTurnLease(self, actor, fingerprint, response)

    def _select(
        self, request: TextRequest, explicit_id: str | None, fingerprint: str
    ) -> tuple[
        SessionEntry,
        tuple[ConversationEvent, ...] | None,
        SessionEntry | None,
    ]:
        if explicit_id is not None:
            entry = self._explicit.get(explicit_id)
            if entry is None:
                return self._fresh(request, explicit_id, True), None, None
            self._validate_config(entry, request)
            try:
                replay = self._existing(entry, request, fingerprint)
            except SessionMismatch:
                if self._is_stale_request(entry, request):
                    raise
                if not self._evictable(entry):
                    raise SessionMismatch(
                        "request transcript does not match conversation"
                    ) from None
                if isinstance(request.next_input, tuple):
                    raise
                return self._new_entry(request, explicit_id, True), None, entry
            return entry, replay, None
        entries = tuple(self._implicit.values())
        exact = [
            item
            for item in entries
            if item.in_flight_fingerprint == fingerprint or fingerprint in item.replay
        ]
        if len(exact) > 1:
            raise SessionMismatch("request transcript matches multiple conversations")
        if exact:
            entry = exact[0]
            return entry, self._existing(entry, request, fingerprint), None
        continuations = [
            item
            for item in entries
            if self._config_matches(item, request)
            and self._is_continuation(item, request)
        ]
        if len(continuations) > 1:
            raise SessionMismatch("request transcript matches multiple conversations")
        if continuations:
            entry = continuations[0]
            return entry, self._existing(entry, request, fingerprint), None
        return self._fresh(request, uuid.uuid4().hex, False), None, None

    def _existing(
        self, entry: SessionEntry, request: TextRequest, fingerprint: str
    ) -> tuple[ConversationEvent, ...] | None:
        self._reject_busy(entry, fingerprint)
        replay = entry.replay.get(fingerprint)
        if replay is not None:
            return replay
        if not self._is_continuation(entry, request):
            raise SessionMismatch("request transcript does not match conversation")
        if isinstance(entry, ToolSessionActor):
            entry.validate_continuation(request)
        return None

    def _fresh(self, request: TextRequest, sid: str, explicit: bool) -> SessionEntry:
        if isinstance(request.next_input, tuple):
            raise SessionMismatch("request transcript is not a fresh conversation")
        self._make_room_for_fresh()
        entry = self._new_entry(request, sid, explicit)
        target = self._explicit if explicit else self._implicit
        target[sid] = entry
        return entry

    def _new_entry(
        self, request: TextRequest, sid: str, explicit: bool
    ) -> SessionEntry:
        history = request.messages[:-1]
        if history:
            backend = self._session_factory(
                request.model,
                request.system,
                tools=request.tools,
                dialect=request.dialect,
                history=history,
            )
        else:
            backend = self._session_factory(
                request.model,
                request.system,
                tools=request.tools,
                dialect=request.dialect,
            )
        if not request.tools:
            entry: SessionEntry = Conversation(
                sid,
                explicit,
                request.model,
                request.system,
                request.dialect,
                history,
                backend,
            )
        else:
            entry = ToolSessionActor(
                sid,
                explicit,
                request.model,
                request.system,
                request.dialect,
                request.tools,
                backend,
                self._turn_timeout_seconds,
                self._tool_result_timeout_seconds,
                self._remove_tool,
                transcript=history,
            )
        return entry

    async def _install_rebase(
        self,
        candidate: SessionEntry,
        replaced: SessionEntry,
        fingerprint: str,
    ) -> None:
        installed = False
        try:
            try:
                async with asyncio.timeout(self._turn_timeout_seconds):
                    await candidate.backend.start()
            except TimeoutError:
                raise SessionTimeout("SDK turn timed out") from None
            async with self._lock:
                target = self._explicit if replaced.explicit else self._implicit
                if (
                    not self._closed
                    and target.get(replaced.external_id) is replaced
                    and replaced.in_flight_fingerprint == fingerprint
                ):
                    target[replaced.external_id] = candidate
                    replaced.in_flight_fingerprint = None
                    self._use_counter += 1
                    candidate.last_used = self._use_counter
                    candidate.in_flight_fingerprint = fingerprint
                    if isinstance(candidate, ToolSessionActor):
                        candidate.mark_started()
                    installed = True
        except BaseException:
            cleanup = asyncio.create_task(
                self._cancel_rebase(candidate, replaced, fingerprint)
            )
            await asyncio.shield(cleanup)
            raise
        if not installed:
            await self._discard_entry(candidate)
            raise RuntimeError("conversation changed during rebase")
        cleanup = asyncio.create_task(self._discard_entry(replaced))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cleanup.add_done_callback(self._consume_task)
            async with self._lock:
                if candidate.in_flight_fingerprint == fingerprint:
                    candidate.in_flight_fingerprint = None
            raise

    async def _cancel_rebase(
        self,
        candidate: SessionEntry,
        replaced: SessionEntry,
        fingerprint: str,
    ) -> None:
        async with self._lock:
            if replaced.in_flight_fingerprint == fingerprint:
                replaced.in_flight_fingerprint = None
        await self._discard_entry(candidate)

    async def _discard_entry(self, entry: SessionEntry) -> None:
        if isinstance(entry, ToolSessionActor):
            await entry.shutdown()
        self._teardown.schedule(entry.backend.close)
        await asyncio.sleep(0)

    @staticmethod
    def _consume_task(task: asyncio.Task[None]) -> None:
        try:
            task.exception()
        except BaseException:
            pass

    def _reserve(self, entry: SessionEntry, fingerprint: str) -> None:
        self._use_counter += 1
        entry.last_used = self._use_counter
        entry.in_flight_fingerprint = fingerprint

    def _text_lease(
        self,
        entry: Conversation,
        request: TextRequest,
        fingerprint: str,
        replay: tuple[ConversationEvent, ...] | None,
    ) -> TurnLease:
        deadline = (
            None
            if replay is not None
            else asyncio.get_running_loop().time() + self._turn_timeout_seconds
        )
        return TurnLease(self, entry, request, fingerprint, deadline, replay)

    def _make_room_for_fresh(self) -> None:
        entries = [*self._explicit.values(), *self._implicit.values()]
        if len(entries) < self._max_sessions:
            return
        idle = [item for item in entries if self._evictable(item)]
        if not idle:
            raise SessionCapacity("session capacity is exhausted")
        victim = min(idle, key=lambda item: item.last_used)
        target = self._explicit if victim.explicit else self._implicit
        del target[victim.external_id]
        if isinstance(victim, ToolSessionActor):
            asyncio.create_task(victim.shutdown())
        self._teardown.schedule(victim.backend.close)

    @staticmethod
    def _evictable(entry: SessionEntry) -> bool:
        if entry.in_flight_fingerprint is not None:
            return False
        return not isinstance(entry, ToolSessionActor) or entry.evictable

    @staticmethod
    def _reject_busy(entry: SessionEntry, fingerprint: str) -> None:
        active = entry.in_flight_fingerprint
        if active == fingerprint:
            raise SessionConflict("request is already in flight")
        if active is not None:
            raise SessionConflict("conversation is busy")

    @staticmethod
    def _is_continuation(entry: SessionEntry, request: TextRequest) -> bool:
        return bool(entry.transcript) and messages_equal(
            request.messages[:-1], entry.transcript
        )

    @staticmethod
    def _is_stale_request(entry: SessionEntry, request: TextRequest) -> bool:
        count = len(request.messages)
        return count <= len(entry.transcript) and messages_equal(
            request.messages, entry.transcript[:count]
        )

    @staticmethod
    def _config_matches(entry: SessionEntry, request: TextRequest) -> bool:
        if request.model != entry.model or request.system != entry.system:
            return False
        if isinstance(entry, Conversation):
            return not request.tools and request.dialect == entry.dialect
        return entry.matches_config(request)

    def _validate_config(self, entry: SessionEntry, request: TextRequest) -> None:
        if request.model != entry.model:
            raise SessionMismatch("session model does not match")
        if request.system != entry.system:
            raise SessionMismatch("session system does not match")
        if not self._config_matches(entry, request):
            raise SessionMismatch("session tool configuration does not match")

    async def _commit(
        self,
        conversation: Conversation,
        request: TextRequest,
        fingerprint: str,
        events: tuple[ConversationEvent, ...],
        assistant_text: str,
    ) -> None:
        async with self._lock:
            target = self._explicit if conversation.explicit else self._implicit
            if target.get(conversation.external_id) is not conversation:
                raise RuntimeError("conversation is no longer active")
            if conversation.in_flight_fingerprint != fingerprint:
                raise RuntimeError("turn reservation is no longer active")
            conversation.transcript = request.messages + (
                CanonicalMessage.assistant_text(assistant_text),
            )
            conversation.replay.clear()
            conversation.replay[fingerprint] = events
            conversation.in_flight_fingerprint = None

    async def _invalidate(self, conversation: Conversation) -> None:
        removed = False
        async with self._lock:
            target = self._explicit if conversation.explicit else self._implicit
            if target.get(conversation.external_id) is conversation:
                del target[conversation.external_id]
                conversation.in_flight_fingerprint = None
                removed = True
        if removed:
            self._teardown.schedule(conversation.backend.close)
            await asyncio.sleep(0)

    async def _release(self, conversation: Conversation, fingerprint: str) -> None:
        async with self._lock:
            if conversation.in_flight_fingerprint == fingerprint:
                conversation.in_flight_fingerprint = None

    async def _release_tool(self, actor: ToolSessionActor, fingerprint: str) -> None:
        async with self._lock:
            if actor.in_flight_fingerprint == fingerprint:
                actor.in_flight_fingerprint = None

    async def _remove_tool(self, actor: ToolSessionActor) -> None:
        removed = False
        async with self._lock:
            target = self._explicit if actor.explicit else self._implicit
            if target.get(actor.external_id) is actor:
                del target[actor.external_id]
                actor.in_flight_fingerprint = None
                removed = True
        if removed:
            self._teardown.schedule(actor.backend.close)
            await asyncio.sleep(0)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            entries = [*self._explicit.values(), *self._implicit.values()]
            self._explicit.clear()
            self._implicit.clear()
        actors = [item for item in entries if isinstance(item, ToolSessionActor)]
        await asyncio.gather(*(actor.shutdown() for actor in actors))
        for entry in entries:
            self._teardown.schedule(entry.backend.close)
        await self._teardown.close()
