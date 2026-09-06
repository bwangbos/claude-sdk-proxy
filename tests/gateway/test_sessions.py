from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from claude_sdk_proxy.domain import (
    CanonicalMessage,
    Completed,
    ConversationEvent,
    Dialect,
    TextDelta,
    TextRequest,
    ToolDefinition,
)
from claude_sdk_proxy.sessions import (
    SessionCapacity,
    SessionConflict,
    SessionMismatch,
    SessionRegistry,
    SessionTimeout,
    TurnLease,
)
from tests.gateway.fakes import FakeConversationSession, FakeSessionFactory


def first_request(
    prompt: str,
    *,
    model: str = "sonnet",
    system: str = "system",
    max_tokens: int | None = 1024,
    stream: bool = True,
    include_usage: bool = False,
) -> TextRequest:
    return TextRequest(
        model=model,
        system=system,
        messages=(CanonicalMessage("user", prompt),),
        max_tokens=max_tokens,
        stream=stream,
        include_usage=include_usage,
    )


def continuation_request(
    first: str,
    answer: str,
    next_prompt: str,
    *,
    model: str = "sonnet",
    system: str = "system",
) -> TextRequest:
    return TextRequest(
        model=model,
        system=system,
        messages=(
            CanonicalMessage("user", first),
            CanonicalMessage("assistant", answer),
            CanonicalMessage("user", next_prompt),
        ),
        max_tokens=1024,
        stream=True,
    )


def completed_events(text: str) -> list[ConversationEvent]:
    return [TextDelta(text), Completed("end_turn", {"output_tokens": 1})]


async def collect(events: AsyncIterator[ConversationEvent]) -> list[ConversationEvent]:
    return [event async for event in events]


class BlockingConversationSession(FakeConversationSession):
    def __init__(
        self, text: str, started: asyncio.Event, release: asyncio.Event
    ) -> None:
        super().__init__(text)
        self._started = started
        self._release = release

    async def stream_generation(
        self, prompt: str
    ) -> AsyncIterator[ConversationEvent]:
        self.prompts.append(prompt)
        self._started.set()
        await self._release.wait()
        yield TextDelta(self._text)
        yield Completed("end_turn", {"output_tokens": 1})


class BlockingSessionFactory:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.sessions: list[BlockingConversationSession] = []

    @property
    def created(self) -> int:
        return len(self.sessions)

    def __call__(
        self,
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
    ) -> BlockingConversationSession:
        del model, system, tools, dialect
        session = BlockingConversationSession("answer", self.started, self.release)
        self.sessions.append(session)
        return session


class BlockingStartSession(FakeConversationSession):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.start_entered = asyncio.Event()

    async def start(self) -> None:
        self.start_count += 1
        self.start_entered.set()
        await asyncio.Event().wait()


class IncompleteSession(FakeConversationSession):
    async def stream_generation(
        self, prompt: str
    ) -> AsyncIterator[ConversationEvent]:
        self.prompts.append(prompt)
        yield TextDelta(self._text)


class FailingStartSession(FakeConversationSession):
    async def start(self) -> None:
        self.start_count += 1
        raise RuntimeError("seeded resume failed")


class RebaseFailureFactory:
    def __init__(self) -> None:
        self.sessions: list[FakeConversationSession] = []
        self.histories: list[tuple[CanonicalMessage, ...]] = []

    def __call__(
        self,
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
        history: tuple[CanonicalMessage, ...] = (),
    ) -> FakeConversationSession:
        del model, system, tools, dialect
        self.histories.append(history)
        session: FakeConversationSession
        if self.sessions:
            session = FailingStartSession("unused")
        else:
            session = FakeConversationSession("answer")
        self.sessions.append(session)
        return session


class RebaseSequenceFactory:
    def __init__(self, sessions: tuple[FakeConversationSession, ...]) -> None:
        self._sessions = iter(sessions)

    def __call__(
        self,
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
        history: tuple[CanonicalMessage, ...] = (),
    ) -> FakeConversationSession:
        del model, system, tools, dialect, history
        return next(self._sessions)


@pytest.mark.anyio
async def test_completed_duplicate_replays_without_new_sdk_turn() -> None:
    factory = FakeSessionFactory(outputs=("answer",))
    registry = SessionRegistry(factory)
    request = first_request("hello")

    first = await registry.open_turn(request, explicit_id=None)
    assert await collect(first.stream()) == completed_events("answer")
    duplicate = await registry.open_turn(request, explicit_id=None)

    assert await collect(duplicate.stream()) == completed_events("answer")
    assert factory.created == 1
    assert factory.sessions[0].prompts == ["hello"]


@pytest.mark.anyio
async def test_replay_lease_reserves_conversation_before_consumption() -> None:
    factory = FakeSessionFactory(outputs=("answer",))
    registry = SessionRegistry(factory)
    request = first_request("hello")
    original = await registry.open_turn(request, explicit_id=None)
    await collect(original.stream())

    replay = await registry.open_turn(request, explicit_id=None)

    with pytest.raises(SessionConflict, match="in flight"):
        await registry.open_turn(request, explicit_id=None)
    await replay.abort()


@pytest.mark.anyio
async def test_second_duplicate_is_rejected_while_replay_is_paused() -> None:
    factory = FakeSessionFactory(outputs=("answer",))
    registry = SessionRegistry(factory)
    request = first_request("hello")
    original = await registry.open_turn(request, explicit_id=None)
    await collect(original.stream())
    replay = await registry.open_turn(request, explicit_id=None)
    replay_stream = replay.stream()
    assert await anext(replay_stream) == TextDelta("answer")

    with pytest.raises(SessionConflict, match="in flight"):
        await registry.open_turn(request, explicit_id=None)
    await replay_stream.aclose()


@pytest.mark.anyio
async def test_continuation_is_rejected_while_replay_is_paused() -> None:
    factory = FakeSessionFactory(outputs=("answer",))
    registry = SessionRegistry(factory)
    request = first_request("hello")
    original = await registry.open_turn(request, explicit_id=None)
    await collect(original.stream())
    replay = await registry.open_turn(request, explicit_id=None)
    replay_stream = replay.stream()
    assert await anext(replay_stream) == TextDelta("answer")

    with pytest.raises(SessionConflict, match="conversation is busy"):
        await registry.open_turn(
            continuation_request("hello", "answer", "next"), explicit_id=None
        )
    await replay_stream.aclose()


@pytest.mark.anyio
async def test_completed_replay_releases_conversation_reservation() -> None:
    factory = FakeSessionFactory(outputs=("answer",))
    registry = SessionRegistry(factory)
    request = first_request("hello")
    original = await registry.open_turn(request, explicit_id=None)
    await collect(original.stream())
    replay = await registry.open_turn(request, explicit_id=None)

    assert await collect(replay.stream()) == completed_events("answer")
    continuation = await registry.open_turn(
        continuation_request("hello", "answer", "next"), explicit_id=None
    )
    await continuation.abort()
    assert factory.sessions[0].close_count == 1


@pytest.mark.anyio
@pytest.mark.parametrize("release", ["close", "abort"])
async def test_premature_replay_release_preserves_healthy_session(
    release: str,
) -> None:
    factory = FakeSessionFactory(outputs=("answer",))
    registry = SessionRegistry(factory)
    request = first_request("hello")
    original = await registry.open_turn(request, explicit_id=None)
    await collect(original.stream())
    replay = await registry.open_turn(request, explicit_id=None)
    replay_stream = replay.stream()

    if release == "close":
        assert await anext(replay_stream) == TextDelta("answer")
        await replay_stream.aclose()
    else:
        await replay.abort()

    later = await registry.open_turn(request, explicit_id=None)
    assert await collect(later.stream()) == completed_events("answer")
    assert factory.created == 1
    assert factory.sessions[0].close_count == 0


@pytest.mark.anyio
async def test_replay_close_before_first_iteration_releases_reservation() -> None:
    factory = FakeSessionFactory(outputs=("answer",))
    registry = SessionRegistry(factory)
    request = first_request("hello")
    original = await registry.open_turn(request, explicit_id=None)
    await collect(original.stream())
    replay = await registry.open_turn(request, explicit_id=None)
    replay_stream = replay.stream()

    await replay_stream.aclose()
    continuation = await registry.open_turn(
        continuation_request("hello", "answer", "next"), explicit_id=None
    )

    assert await collect(continuation.stream()) == completed_events("answer")
    assert factory.created == 1
    assert factory.sessions[0].prompts == ["hello", "next"]
    assert factory.sessions[0].close_count == 0


@pytest.mark.anyio
async def test_cancelled_replay_abort_can_be_retried() -> None:
    factory = FakeSessionFactory(outputs=("answer",))
    registry = SessionRegistry(factory)
    request = first_request("hello")
    original = await registry.open_turn(request, explicit_id=None)
    await collect(original.stream())
    replay = await registry.open_turn(request, explicit_id=None)

    await registry._lock.acquire()
    try:
        abort = asyncio.create_task(replay.abort())
        await asyncio.sleep(0)
        assert not abort.done()
        abort.cancel()
        with pytest.raises(asyncio.CancelledError):
            await abort
    finally:
        registry._lock.release()

    await replay.abort()
    later = await registry.open_turn(request, explicit_id=None)
    assert await collect(later.stream()) == completed_events("answer")
    assert factory.created == 1
    assert factory.sessions[0].close_count == 0


@pytest.mark.anyio
async def test_replay_identity_ignores_rendering_and_advisory_fields() -> None:
    factory = FakeSessionFactory(outputs=("answer",))
    registry = SessionRegistry(factory)
    original = first_request("hello")
    duplicate = first_request(
        "hello", max_tokens=7, stream=False, include_usage=True
    )

    first = await registry.open_turn(original, explicit_id=None)
    await collect(first.stream())
    replay = await registry.open_turn(duplicate, explicit_id=None)

    assert await collect(replay.stream()) == completed_events("answer")
    assert factory.created == 1


@pytest.mark.anyio
async def test_in_flight_duplicate_is_409_conflict() -> None:
    factory = BlockingSessionFactory()
    registry = SessionRegistry(factory)
    lease = await registry.open_turn(first_request("hello"), explicit_id=None)
    consume = asyncio.create_task(collect(lease.stream()))
    await factory.started.wait()

    with pytest.raises(SessionConflict, match="in flight"):
        await registry.open_turn(first_request("hello"), explicit_id=None)

    factory.release.set()
    await consume


@pytest.mark.anyio
async def test_different_fresh_implicit_request_starts_independent_session() -> None:
    factory = BlockingSessionFactory()
    registry = SessionRegistry(factory)
    first = await registry.open_turn(first_request("one"), explicit_id=None)
    consume = asyncio.create_task(collect(first.stream()))
    await factory.started.wait()

    second = await registry.open_turn(first_request("two"), explicit_id=None)

    assert factory.created == 2
    await second.abort()
    factory.release.set()
    await consume


@pytest.mark.anyio
async def test_different_turn_cannot_enter_same_in_flight_conversation() -> None:
    factory = BlockingSessionFactory()
    registry = SessionRegistry(factory)
    first = await registry.open_turn(first_request("hello"), explicit_id="lineage")
    consume = asyncio.create_task(collect(first.stream()))
    await factory.started.wait()

    with pytest.raises(SessionConflict, match="conversation is busy"):
        await registry.open_turn(
            continuation_request("hello", "answer", "next"),
            explicit_id="lineage",
        )

    factory.release.set()
    await consume


@pytest.mark.anyio
async def test_explicit_ids_keep_identical_conversations_independent() -> None:
    factory = FakeSessionFactory(outputs=("a", "b"))
    registry = SessionRegistry(factory)
    request = first_request("same")

    one = await registry.open_turn(request, explicit_id="client-one")
    two = await registry.open_turn(request, explicit_id="client-two")
    await collect(one.stream())
    await collect(two.stream())

    assert factory.created == 2
    assert one.response_headers == {"X-Claude-Proxy-Session": "client-one"}
    assert two.response_headers == {"X-Claude-Proxy-Session": "client-two"}


@pytest.mark.anyio
async def test_implicit_lookup_ignores_identical_explicit_session() -> None:
    factory = FakeSessionFactory(outputs=("explicit", "implicit"))
    registry = SessionRegistry(factory)
    request = first_request("same")

    explicit = await registry.open_turn(request, explicit_id="client-one")
    await collect(explicit.stream())
    implicit = await registry.open_turn(request, explicit_id=None)

    assert await collect(implicit.stream()) == completed_events("implicit")
    assert factory.created == 2


@pytest.mark.anyio
async def test_imported_assistant_history_seeds_a_fresh_sdk_session() -> None:
    factory = FakeSessionFactory(outputs=("continued",))
    registry = SessionRegistry(factory)
    request = continuation_request("imported", "assistant", "new")

    lease = await registry.open_turn(request, explicit_id=None)

    assert await collect(lease.stream()) == completed_events("continued")
    assert factory.created == 1
    assert factory.histories == [request.messages[:-1]]
    assert factory.sessions[0].prompts == ["new"]


@pytest.mark.anyio
async def test_edited_assistant_history_atomically_rebases_explicit_session() -> None:
    factory = FakeSessionFactory(outputs=("answer", "rebased"))
    registry = SessionRegistry(factory)
    first = await registry.open_turn(first_request("hello"), explicit_id="lineage")
    await collect(first.stream())
    request = continuation_request("hello", "edited", "next")

    replacement = await registry.open_turn(request, explicit_id="lineage")

    assert await collect(replacement.stream()) == completed_events("rebased")
    assert factory.created == 2
    assert factory.sessions[0].prompts == ["hello"]
    assert factory.sessions[0].close_count == 1
    assert factory.histories[1] == request.messages[:-1]
    assert factory.sessions[1].prompts == ["next"]


@pytest.mark.anyio
async def test_failed_explicit_rebase_preserves_the_original_session() -> None:
    factory = RebaseFailureFactory()
    registry = SessionRegistry(factory)
    first = await registry.open_turn(first_request("hello"), explicit_id="lineage")
    await collect(first.stream())

    with pytest.raises(RuntimeError, match="seeded resume failed"):
        await registry.open_turn(
            continuation_request("hello", "edited", "next"),
            explicit_id="lineage",
        )

    continuation = await registry.open_turn(
        continuation_request("hello", "answer", "still here"),
        explicit_id="lineage",
    )
    assert await collect(continuation.stream()) == completed_events("answer")
    assert factory.sessions[0].prompts == ["hello", "still here"]
    assert factory.sessions[0].close_count == 0
    assert factory.sessions[1].close_count == 1


@pytest.mark.anyio
async def test_cancelled_explicit_rebase_releases_the_original_session() -> None:
    original = FakeConversationSession("answer")
    candidate = BlockingStartSession("unused")
    registry = SessionRegistry(RebaseSequenceFactory((original, candidate)))
    first = await registry.open_turn(first_request("hello"), explicit_id="lineage")
    await collect(first.stream())
    rebasing = asyncio.create_task(
        registry.open_turn(
            continuation_request("hello", "edited", "next"),
            explicit_id="lineage",
        )
    )
    await candidate.start_entered.wait()

    rebasing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await rebasing

    continuation = await registry.open_turn(
        continuation_request("hello", "answer", "still here"),
        explicit_id="lineage",
    )
    assert await collect(continuation.stream()) == completed_events("answer")
    assert original.prompts == ["hello", "still here"]
    assert original.close_count == 0
    assert candidate.close_count == 1


@pytest.mark.anyio
async def test_timed_out_explicit_rebase_preserves_the_original_session() -> None:
    original = FakeConversationSession("answer")
    candidate = BlockingStartSession("unused")
    registry = SessionRegistry(
        RebaseSequenceFactory((original, candidate)), turn_timeout_seconds=0.01
    )
    first = await registry.open_turn(first_request("hello"), explicit_id="lineage")
    await collect(first.stream())

    with pytest.raises(SessionTimeout, match="timed out"):
        await registry.open_turn(
            continuation_request("hello", "edited", "next"),
            explicit_id="lineage",
        )

    continuation = await registry.open_turn(
        continuation_request("hello", "answer", "still here"),
        explicit_id="lineage",
    )
    assert await collect(continuation.stream()) == completed_events("answer")
    assert original.prompts == ["hello", "still here"]
    assert original.close_count == 0
    assert candidate.start_count == 1
    assert candidate.close_count == 1


@pytest.mark.anyio
@pytest.mark.parametrize("changed", ["model", "system"])
async def test_explicit_session_rejects_configuration_change(changed: str) -> None:
    factory = FakeSessionFactory(outputs=("answer",))
    registry = SessionRegistry(factory)
    first = await registry.open_turn(first_request("hello"), explicit_id="lineage")
    await collect(first.stream())
    kwargs = {changed: "different"}

    with pytest.raises(SessionMismatch, match=changed):
        await registry.open_turn(
            continuation_request("hello", "answer", "next", **kwargs),
            explicit_id="lineage",
        )

    assert factory.sessions[0].prompts == ["hello"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "explicit_id",
    ("", "-leading", "white space", "x" * 129),
)
async def test_invalid_explicit_session_id_is_rejected(explicit_id: str) -> None:
    factory = FakeSessionFactory(outputs=("unused",))
    registry = SessionRegistry(factory)

    with pytest.raises(SessionMismatch, match="session ID"):
        await registry.open_turn(first_request("hello"), explicit_id=explicit_id)

    assert factory.created == 0


@pytest.mark.anyio
async def test_exact_continuation_reuses_the_same_backend() -> None:
    factory = FakeSessionFactory(outputs=("one",))
    registry = SessionRegistry(factory)
    first = await registry.open_turn(first_request("hello"), explicit_id="lineage")
    await collect(first.stream())
    second = await registry.open_turn(
        continuation_request("hello", "one", "next"), explicit_id="lineage"
    )

    assert await collect(second.stream()) == completed_events("one")
    assert factory.created == 1
    assert factory.sessions[0].prompts == ["hello", "next"]


@pytest.mark.anyio
async def test_timeout_during_start_invalidates_and_closes_session() -> None:
    sessions: list[BlockingStartSession] = []

    def factory(
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
    ) -> BlockingStartSession:
        del model, system, tools, dialect
        session = BlockingStartSession("unused")
        sessions.append(session)
        return session

    registry = SessionRegistry(factory, turn_timeout_seconds=0.01)
    lease = await registry.open_turn(first_request("hello"), explicit_id="lineage")

    with pytest.raises(SessionTimeout, match="timed out"):
        await collect(lease.stream())

    retry = await registry.open_turn(first_request("hello"), explicit_id="lineage")
    await retry.abort()
    assert len(sessions) == 2
    assert sessions[0].start_count == 1
    assert sessions[0].close_count == 1


@pytest.mark.anyio
async def test_cancelled_consumer_invalidates_and_closes_session() -> None:
    factory = BlockingSessionFactory()
    registry = SessionRegistry(factory)
    lease = await registry.open_turn(first_request("hello"), explicit_id="lineage")
    consume = asyncio.create_task(collect(lease.stream()))
    await factory.started.wait()

    consume.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consume

    retry = await registry.open_turn(first_request("hello"), explicit_id="lineage")
    await retry.abort()
    assert factory.created == 2
    assert factory.sessions[0].close_count == 1


@pytest.mark.anyio
async def test_generator_close_before_completion_invalidates_session() -> None:
    factory = FakeSessionFactory(outputs=("partial", "retry"))
    registry = SessionRegistry(factory)
    lease = await registry.open_turn(first_request("hello"), explicit_id="lineage")
    stream = lease.stream()

    assert await anext(stream) == TextDelta("partial")
    await stream.aclose()
    retry = await registry.open_turn(first_request("hello"), explicit_id="lineage")

    assert await collect(retry.stream()) == completed_events("retry")
    assert factory.created == 2
    assert factory.sessions[0].close_count == 1


@pytest.mark.anyio
async def test_abort_is_idempotent_and_invalidates_uncommitted_turn() -> None:
    factory = FakeSessionFactory(outputs=("unused", "retry"))
    registry = SessionRegistry(factory)
    lease = await registry.open_turn(first_request("hello"), explicit_id="lineage")

    await lease.abort()
    await lease.abort()
    retry = await registry.open_turn(first_request("hello"), explicit_id="lineage")

    assert await collect(retry.stream()) == completed_events("retry")
    assert factory.created == 2
    assert factory.sessions[0].close_count == 1
    assert factory.sessions[0].start_count == 0


@pytest.mark.anyio
async def test_cancelled_active_abort_can_be_retried() -> None:
    factory = FakeSessionFactory(outputs=("unused", "retry"))
    registry = SessionRegistry(factory)
    lease = await registry.open_turn(first_request("hello"), explicit_id="lineage")

    await registry._lock.acquire()
    try:
        abort = asyncio.create_task(lease.abort())
        await asyncio.sleep(0)
        assert not abort.done()
        abort.cancel()
        with pytest.raises(asyncio.CancelledError):
            await abort
    finally:
        registry._lock.release()

    await lease.abort()
    retry = await registry.open_turn(first_request("hello"), explicit_id="lineage")
    assert await collect(retry.stream()) == completed_events("retry")
    assert factory.sessions[0].close_count == 1


@pytest.mark.anyio
async def test_incomplete_backend_stream_invalidates_session() -> None:
    sessions: list[IncompleteSession] = []

    def factory(
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
    ) -> IncompleteSession:
        del model, system, tools, dialect
        session = IncompleteSession("partial")
        sessions.append(session)
        return session

    registry = SessionRegistry(factory)
    lease = await registry.open_turn(first_request("hello"), explicit_id="lineage")

    with pytest.raises(RuntimeError, match="without completion"):
        await collect(lease.stream())

    retry = await registry.open_turn(first_request("hello"), explicit_id="lineage")
    await retry.abort()
    assert len(sessions) == 2
    assert sessions[0].close_count == 1


@pytest.mark.anyio
async def test_commit_happens_before_terminal_event_is_exposed() -> None:
    factory = FakeSessionFactory(outputs=("answer",))
    registry = SessionRegistry(factory)
    request = first_request("hello")
    first = await registry.open_turn(request, explicit_id=None)
    stream = first.stream()

    assert await anext(stream) == TextDelta("answer")
    assert await anext(stream) == Completed("end_turn", {"output_tokens": 1})
    duplicate = await registry.open_turn(request, explicit_id=None)

    assert await collect(duplicate.stream()) == completed_events("answer")
    assert factory.created == 1
    await stream.aclose()


@pytest.mark.anyio
async def test_abort_after_commit_preserves_replay_and_backend() -> None:
    factory = FakeSessionFactory(outputs=("answer", "unexpected"))
    registry = SessionRegistry(factory)
    request = first_request("hello")
    lease = await registry.open_turn(request, explicit_id="lineage")
    stream = lease.stream()

    assert await anext(stream) == TextDelta("answer")
    assert await anext(stream) == Completed("end_turn", {"output_tokens": 1})
    await lease.abort()
    duplicate = await registry.open_turn(request, explicit_id="lineage")

    assert await collect(duplicate.stream()) == completed_events("answer")
    assert factory.created == 1
    assert factory.sessions[0].close_count == 0
    await stream.aclose()


@pytest.mark.anyio
async def test_completed_usage_is_immutable_across_replay() -> None:
    factory = FakeSessionFactory(outputs=("answer",))
    registry = SessionRegistry(factory)
    request = first_request("hello")
    first = await registry.open_turn(request, explicit_id="lineage")
    first_events = await collect(first.stream())
    completed = first_events[-1]
    assert isinstance(completed, Completed)
    assert completed.usage is not None

    with pytest.raises(TypeError):
        completed.usage["output_tokens"] = 999

    replay = await registry.open_turn(request, explicit_id="lineage")
    replay_events = await collect(replay.stream())
    assert replay_events[-1] == Completed("end_turn", {"output_tokens": 1})


@pytest.mark.anyio
async def test_commit_and_abort_serialize_at_completed_boundary() -> None:
    factory = FakeSessionFactory(outputs=("answer", "unexpected"))
    registry = SessionRegistry(factory)
    request = first_request("hello")
    lease = await registry.open_turn(request, explicit_id="lineage")
    stream = lease.stream()
    assert await anext(stream) == TextDelta("answer")

    await registry._lock.acquire()
    try:
        terminal = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        abort = asyncio.create_task(lease.abort())
        await asyncio.sleep(0)
        assert not terminal.done()
        assert not abort.done()
    finally:
        registry._lock.release()

    assert await terminal == Completed("end_turn", {"output_tokens": 1})
    await abort
    duplicate = await registry.open_turn(request, explicit_id="lineage")

    assert await collect(duplicate.stream()) == completed_events("answer")
    assert factory.created == 1
    assert factory.sessions[0].close_count == 0
    await stream.aclose()


@pytest.mark.anyio
async def test_registry_close_disconnects_every_retained_backend() -> None:
    factory = FakeSessionFactory(outputs=("one", "two"))
    registry = SessionRegistry(factory)
    one = await registry.open_turn(first_request("one"), explicit_id="one")
    two = await registry.open_turn(first_request("two"), explicit_id="two")
    await collect(one.stream())
    await collect(two.stream())

    await registry.close()
    await registry.close()

    assert [session.close_count for session in factory.sessions] == [1, 1]


@pytest.mark.anyio
async def test_capacity_evicts_lru_idle_across_explicit_and_implicit() -> None:
    factory = FakeSessionFactory(outputs=("explicit", "implicit", "fresh"))
    registry = SessionRegistry(factory, max_sessions=2)
    explicit_request = first_request("explicit")
    implicit_request = first_request("implicit")
    explicit = await registry.open_turn(explicit_request, explicit_id="explicit")
    implicit = await registry.open_turn(implicit_request, explicit_id=None)
    await collect(explicit.stream())
    await collect(implicit.stream())

    replay = await registry.open_turn(explicit_request, explicit_id="explicit")
    await collect(replay.stream())
    fresh = await registry.open_turn(first_request("fresh"), explicit_id="fresh")
    await asyncio.sleep(0)

    assert factory.sessions[0].close_count == 0
    assert factory.sessions[1].close_count == 1
    assert factory.created == 3
    await fresh.abort()


@pytest.mark.anyio
async def test_capacity_never_evicts_active_or_replay_reserved_sessions() -> None:
    factory = FakeSessionFactory(outputs=("one", "two"))
    registry = SessionRegistry(factory, max_sessions=2)
    replay_request = first_request("one")
    original = await registry.open_turn(replay_request, explicit_id="one")
    await collect(original.stream())
    replay = await registry.open_turn(replay_request, explicit_id="one")
    active = await registry.open_turn(first_request("two"), explicit_id="two")

    with pytest.raises(SessionCapacity, match="capacity"):
        await registry.open_turn(first_request("three"), explicit_id="three")

    assert factory.created == 2
    assert [session.close_count for session in factory.sessions] == [0, 0]
    await replay.abort()
    await active.abort()


@pytest.mark.anyio
async def test_capacity_serializes_eviction_and_concurrent_fresh_admission() -> None:
    factory = FakeSessionFactory(outputs=("old", "winner"))
    registry = SessionRegistry(factory, max_sessions=1)
    old = await registry.open_turn(first_request("old"), explicit_id="old")
    await collect(old.stream())

    results = await asyncio.gather(
        registry.open_turn(first_request("one"), explicit_id="one"),
        registry.open_turn(first_request("two"), explicit_id="two"),
        return_exceptions=True,
    )
    leases = [result for result in results if isinstance(result, TurnLease)]
    failures = [result for result in results if isinstance(result, SessionCapacity)]
    await asyncio.sleep(0)

    assert len(leases) == len(failures) == 1
    assert factory.created == 2
    assert factory.sessions[0].close_count == 1
    await leases[0].abort()


@pytest.mark.anyio
async def test_only_current_completed_head_remains_replayable() -> None:
    factory = FakeSessionFactory(outputs=("one", "two"))
    registry = SessionRegistry(factory)
    first_request_head = first_request("first")
    first = await registry.open_turn(first_request_head, explicit_id="lineage")
    await collect(first.stream())
    current_request_head = continuation_request("first", "one", "second")
    continuation = await registry.open_turn(
        current_request_head, explicit_id="lineage"
    )
    expected = await collect(continuation.stream())

    with pytest.raises(SessionMismatch, match="transcript"):
        await registry.open_turn(first_request_head, explicit_id="lineage")
    replay = await registry.open_turn(current_request_head, explicit_id="lineage")

    assert await collect(replay.stream()) == expected
    assert factory.sessions[0].prompts == ["first", "second"]


@pytest.mark.parametrize("max_sessions", [0, -1])
def test_session_capacity_must_be_positive(max_sessions: int) -> None:
    with pytest.raises(ValueError, match="max sessions"):
        SessionRegistry(FakeSessionFactory(()), max_sessions=max_sessions)
