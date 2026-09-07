from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum, auto

from claude_sdk_proxy.diagnostics import (
    bind_task_request_id,
    current_request_id,
    record_backend_failure,
)
from claude_sdk_proxy.domain import (
    BackendFailure,
    CanonicalBlock,
    CanonicalMessage,
    Completed,
    ConversationEvent,
    InputUsage,
    ModelFallbackDisabled,
    Prompt,
    ResponseIdentity,
    SdkSessionProtocol,
    TextBlock,
    TextDelta,
    TextRequest,
    ThinkingCompleted,
    ToolCall,
    ToolCallBlock,
    ToolDefinition,
)
from claude_sdk_proxy.session_identity import fixed_config_matches
from claude_sdk_proxy.session_turn import (
    SessionConflict,
    SessionMismatch,
    SessionTimeout,
)
from claude_sdk_proxy.thinking import ThinkingOptions


class ToolSessionState(Enum):
    READY = auto()
    GENERATING = auto()
    WAITING_FOR_TOOLS = auto()
    CLOSED = auto()


@dataclass(slots=True)
class StreamFailure:
    error: BaseException


STREAM_END = object()
type StreamItem = ConversationEvent | StreamFailure | object


@dataclass(slots=True)
class ToolResponse:
    request: TextRequest
    fingerprint: str
    request_id: str | None = field(default_factory=current_request_id)
    queue: asyncio.Queue[StreamItem] = field(default_factory=asyncio.Queue)
    durable: bool = False
    detached: bool = False


@dataclass(slots=True)
class ToolSessionActor:
    external_id: str
    explicit: bool
    model: str
    system: str
    dialect: str
    thinking: ThinkingOptions
    tools: tuple[ToolDefinition, ...]
    backend: SdkSessionProtocol
    generation_timeout: float
    result_timeout: float
    on_close: Callable[[ToolSessionActor], Awaitable[None]]
    transcript: tuple[CanonicalMessage, ...] = ()
    in_flight_fingerprint: str | None = None
    replay: dict[str, tuple[ConversationEvent, ...]] = field(default_factory=dict)
    last_used: int = 0
    state: ToolSessionState = ToolSessionState.READY
    pending_call_ids: frozenset[str] = frozenset()
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _current: ToolResponse | None = None
    _runner: asyncio.Task[None] | None = None
    _watcher: asyncio.Task[None] | None = None
    _submitter: asyncio.Task[None] | None = None
    _cleanup: asyncio.Task[None] | None = None
    _started: bool = False
    _generation_deadline: float | None = None
    _wait_deadline: float | None = None
    _wait_token: int = 0
    _resume: asyncio.Event = field(default_factory=asyncio.Event)

    def matches_fixed_config(self, request: TextRequest) -> bool:
        return fixed_config_matches(
            request,
            system=self.system,
            dialect=self.dialect,
            tools=self.tools,
        )

    def matches_generation_config(self, request: TextRequest) -> bool:
        return request.model == self.model and request.thinking == self.thinking

    def matches_config(self, request: TextRequest) -> bool:
        return self.matches_fixed_config(request) and self.matches_generation_config(
            request
        )

    def validate_continuation(self, request: TextRequest) -> None:
        if self.state is ToolSessionState.WAITING_FOR_TOOLS:
            value = request.next_input
            if not isinstance(value, tuple):
                raise SessionMismatch("tool results are required")
            if {item.tool_call_id for item in value} != self.pending_call_ids:
                raise SessionMismatch("tool results do not match pending calls")
        elif self.state is not ToolSessionState.READY:
            raise SessionConflict("conversation is busy")

    @property
    def evictable(self) -> bool:
        return (
            self.in_flight_fingerprint is None and self.state is ToolSessionState.READY
        )

    def mark_started(self) -> None:
        if self.state is not ToolSessionState.READY or self._started:
            raise RuntimeError("tool session cannot adopt a started backend")
        self._started = True

    async def admit(self, request: TextRequest, fingerprint: str) -> ToolResponse:
        response = ToolResponse(request, fingerprint)
        expired = False
        async with self._lock:
            if self.state is ToolSessionState.CLOSED:
                raise SessionMismatch("conversation is no longer active")
            if self.state is ToolSessionState.WAITING_FOR_TOOLS:
                expired = self._admit_results_locked(response)
            elif self.state is ToolSessionState.READY:
                self._admit_prompt_locked(response)
            else:
                raise RuntimeError("conversation admission was not reserved")
        if expired:
            await self._fail(SessionTimeout("tool result wait timed out"))
            raise SessionTimeout("tool result wait timed out")
        return response

    def _admit_prompt_locked(self, response: ToolResponse) -> None:
        request = response.request
        self.state = ToolSessionState.GENERATING
        self._current = response
        self._generation_deadline = self._now() + self.generation_timeout
        if self._watcher is None:
            self._watcher = asyncio.create_task(self._watch_failures())
        self._runner = asyncio.create_task(self._run(request.next_prompt))

    def _admit_results_locked(self, response: ToolResponse) -> bool:
        request = response.request
        value = request.next_input
        if not isinstance(value, tuple):
            raise SessionMismatch("tool results are required")
        if self._wait_deadline is None or self._now() >= self._wait_deadline:
            return True
        ids = {result.tool_call_id for result in value}
        if ids != self.pending_call_ids:
            raise SessionMismatch("tool results do not match pending calls")
        response.durable = True
        self.transcript += request.messages[-1:]
        self.replay.clear()
        self._current = response
        self.state = ToolSessionState.GENERATING
        self.pending_call_ids = frozenset()
        self._wait_deadline = None
        self._wait_token += 1
        self._generation_deadline = self._now() + self.generation_timeout
        self._resume.clear()
        # submit_tool_results has no suspension point in the production adapter.
        self._submitter = asyncio.create_task(self.backend.submit_tool_results(value))
        self._submitter.add_done_callback(self._submitted)
        return False

    def _submitted(self, task: asyncio.Task[None]) -> None:
        try:
            error = task.exception()
        except BaseException as caught:
            error = caught
        if error is None and self.state is not ToolSessionState.CLOSED:
            self._resume.set()
        elif error is not None and self.state is not ToolSessionState.CLOSED:
            asyncio.create_task(self._fail(_redact_backend(error)))

    async def _run(self, prompt: Prompt) -> None:
        try:
            if not self._started:
                await self._with_generation_deadline(self.backend.start())
                self._started = True
            stream = self.backend.stream_generation(prompt)
            events: list[ConversationEvent] = []
            tool_suffix_started = False
            while True:
                if self._current is not None:
                    bind_task_request_id(self._current.request_id)
                event = await self._next_event(stream)
                events.append(event)
                response = self._current
                if response is None:
                    raise RuntimeError("tool generation has no response sink")
                if not isinstance(event, (ToolCall, Completed)):
                    if not response.detached and not tool_suffix_started:
                        response.queue.put_nowait(event)
                    continue
                if isinstance(event, ToolCall):
                    tool_suffix_started = True
                    continue
                if event.stop_reason == "tool_use":
                    await self._commit_tool_boundary(response, tuple(events))
                    events = []
                    tool_suffix_started = False
                    if not await self._wait_for_results():
                        return
                    continue
                await self._commit_terminal(response, tuple(events))
                return
        except StopAsyncIteration:
            await self._fail(BackendFailure("Agent SDK stream ended without result"))
        except asyncio.CancelledError:
            if self.state is not ToolSessionState.CLOSED:
                await self._fail(BackendFailure("Agent SDK query failed"))
        except TimeoutError:
            await self._fail(SessionTimeout("SDK turn timed out"))
        except BaseException as error:
            if self._current is not None:
                bind_task_request_id(self._current.request_id)
            await self._fail(_redact_backend(error))

    async def _next_event(
        self, stream: AsyncIterator[ConversationEvent]
    ) -> ConversationEvent:
        deadline = self._generation_deadline
        async with asyncio.timeout_at(deadline):
            return await anext(stream)

    async def _with_generation_deadline(self, awaitable: Awaitable[None]) -> None:
        async with asyncio.timeout_at(self._generation_deadline):
            await awaitable

    async def _commit_tool_boundary(
        self, response: ToolResponse, events: tuple[ConversationEvent, ...]
    ) -> None:
        calls = tuple(event for event in events if isinstance(event, ToolCall))
        if not calls:
            raise BackendFailure("Agent SDK query failed")
        assistant = _assistant_message(events)
        async with self._lock:
            if self.state is not ToolSessionState.GENERATING:
                raise RuntimeError("tool session is not generating")
            self.transcript += response.request.messages[len(self.transcript) :] + (
                assistant,
            )
            self.replay.clear()
            self.replay[response.fingerprint] = events
            self.in_flight_fingerprint = None
            self.pending_call_ids = frozenset(call.id for call in calls)
            self.state = ToolSessionState.WAITING_FOR_TOOLS
            self._current = None
            self._wait_token += 1
            self._wait_deadline = self._now() + self.result_timeout
            self._resume.clear()
            response.durable = True
            if not response.detached:
                suffix_started = False
                for event in events:
                    if isinstance(event, ToolCall):
                        suffix_started = True
                    if suffix_started:
                        response.queue.put_nowait(event)
                response.queue.put_nowait(STREAM_END)

    async def _commit_terminal(
        self, response: ToolResponse, events: tuple[ConversationEvent, ...]
    ) -> None:
        assistant = _assistant_message(events)
        async with self._lock:
            if self.state is not ToolSessionState.GENERATING:
                raise RuntimeError("tool session is not generating")
            self.transcript += response.request.messages[len(self.transcript) :] + (
                assistant,
            )
            self.replay.clear()
            self.replay[response.fingerprint] = events
            self.in_flight_fingerprint = None
            self.pending_call_ids = frozenset()
            self.state = ToolSessionState.READY
            self._current = None
            self._generation_deadline = None
            response.durable = True
            if not response.detached:
                response.queue.put_nowait(events[-1])
                response.queue.put_nowait(STREAM_END)

    async def _wait_for_results(self) -> bool:
        token = self._wait_token
        deadline = self._wait_deadline
        while True:
            try:
                async with asyncio.timeout_at(deadline):
                    await self._resume.wait()
            except TimeoutError:
                retry, error = await self._classify_wait_timeout(token)
                if retry:
                    token = self._wait_token
                    deadline = self._generation_deadline
                    continue
                if error is not None:
                    await self._fail(error)
                return False
            return self.state is ToolSessionState.GENERATING

    async def _classify_wait_timeout(
        self, token: int
    ) -> tuple[bool, SessionTimeout | None]:
        async with self._lock:
            now = self._now()
            if self.state is ToolSessionState.WAITING_FOR_TOOLS:
                if (
                    token == self._wait_token
                    and self._wait_deadline is not None
                    and now >= self._wait_deadline
                ):
                    return False, SessionTimeout("tool result wait timed out")
                return False, None
            if self.state is ToolSessionState.GENERATING:
                if token != self._wait_token:
                    return True, None
                if (
                    self._generation_deadline is not None
                    and now >= self._generation_deadline
                ):
                    return False, SessionTimeout("SDK turn timed out")
            return False, None

    async def _watch_failures(self) -> None:
        try:
            await self.backend.wait_failure()
        except asyncio.CancelledError:
            return
        except BaseException as error:
            if self._current is not None:
                bind_task_request_id(self._current.request_id)
            await self._fail(_redact_backend(error))
        else:
            await self._fail(BackendFailure("Agent SDK query failed"))

    async def detach(self, response: ToolResponse) -> None:
        close = False
        async with self._lock:
            if response.detached:
                return
            response.detached = True
            close = not response.durable and self.state is not ToolSessionState.CLOSED
        if close:
            await self._fail(RuntimeError("turn was aborted"))

    async def _fail(self, error: BaseException) -> None:
        current = asyncio.current_task()
        tasks: tuple[asyncio.Task[None], ...] = ()
        cleanup: asyncio.Task[None] | None = None
        async with self._lock:
            if self.state is ToolSessionState.CLOSED:
                cleanup = self._cleanup
            else:
                self.state = ToolSessionState.CLOSED
                self.in_flight_fingerprint = None
                self.pending_call_ids = frozenset()
                self._wait_deadline = None
                self._wait_token += 1
                self._resume.set()
                response, self._current = self._current, None
                if response is not None and not response.detached:
                    response.queue.put_nowait(StreamFailure(error))
                    response.queue.put_nowait(STREAM_END)
                tasks = tuple(
                    task
                    for task in (self._runner, self._watcher, self._submitter)
                    if task is not None and task is not current and not task.done()
                )
                self._runner = None
                self._watcher = None
                self._submitter = None
                for task in tasks:
                    task.add_done_callback(self._consume_worker)
                    task.cancel()
                cleanup = asyncio.create_task(self._notify_close())
                cleanup.add_done_callback(self._consume_worker)
                self._cleanup = cleanup
        if cleanup is not None:
            await asyncio.shield(cleanup)

    async def shutdown(self) -> None:
        await self._fail(RuntimeError("session registry is closed"))

    async def _notify_close(self) -> None:
        await self.on_close(self)

    @staticmethod
    def _now() -> float:
        return asyncio.get_running_loop().time()

    @staticmethod
    def _consume_worker(task: asyncio.Task[None]) -> None:
        try:
            task.exception()
        except BaseException:
            pass


def _assistant_message(events: tuple[ConversationEvent, ...]) -> CanonicalMessage:
    blocks: list[CanonicalBlock] = []
    for event in events:
        if isinstance(event, TextDelta):
            if blocks and isinstance(blocks[-1], TextBlock):
                blocks[-1] = TextBlock(blocks[-1].text + event.text)
            else:
                blocks.append(TextBlock(event.text))
        elif isinstance(event, ThinkingCompleted):
            blocks.append(event.block)
        elif isinstance(event, ToolCall):
            blocks.append(ToolCallBlock(event.id, event.name, event.arguments))
    if not blocks:
        if (
            events
            and isinstance(events[-1], Completed)
            and events[-1].stop_reason == "refusal"
            and all(
                isinstance(event, (InputUsage, ResponseIdentity))
                for event in events[:-1]
            )
        ):
            # The SDK validates the native refusal transaction. No answer was
            # emitted, but its terminal history must remain replayable.
            return CanonicalMessage.assistant_text("")
        raise BackendFailure("Agent SDK query failed")
    return CanonicalMessage("assistant", tuple(blocks))


def _redact_backend(error: BaseException) -> BaseException:
    record_backend_failure(error, "tool_session")
    if isinstance(error, ModelFallbackDisabled):
        return error
    if isinstance(error, SessionTimeout):
        return error
    return BackendFailure("Agent SDK query failed")


__all__ = [
    "STREAM_END",
    "StreamFailure",
    "ToolResponse",
    "ToolSessionActor",
    "ToolSessionState",
]
