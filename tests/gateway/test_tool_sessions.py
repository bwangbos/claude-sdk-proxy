from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable

import pytest

from claude_sdk_proxy.domain import (
    BackendFailure,
    CanonicalMessage,
    Completed,
    ConversationEvent,
    Dialect,
    TextBlock,
    TextDelta,
    TextRequest,
    ToolCall,
    ToolCallBlock,
    ToolDefinition,
    ToolResultBlock,
)
from claude_sdk_proxy.session_identity import request_fingerprint
from claude_sdk_proxy.sessions import (
    SessionCapacity,
    SessionConflict,
    SessionMismatch,
    SessionRegistry,
    SessionTimeout,
    ToolSessionActor,
)
from claude_sdk_proxy.thinking import ThinkingOptions


def echo_tool(*, description: str = "echo") -> ToolDefinition:
    return ToolDefinition(
        "echo",
        description,
        {"type": "object", "properties": {"value": {"type": "string"}}},
    )


def first_request(
    *,
    tools: tuple[ToolDefinition, ...] | None = None,
    dialect: str = "anthropic",
) -> TextRequest:
    return TextRequest(
        "sonnet",
        "system",
        (CanonicalMessage.user_text("go"),),
        1024,
        True,
        dialect=dialect,  # type: ignore[arg-type]
        tools=tools or (),
    )


def continuation(
    first: TextRequest,
    calls: tuple[ToolCall, ...],
    results: tuple[ToolResultBlock, ...],
) -> TextRequest:
    assistant = CanonicalMessage(
        "assistant",
        tuple(
            ToolCallBlock(call.id, call.name, call.arguments) for call in calls
        ),
    )
    return TextRequest(
        first.model,
        first.system,
        (*first.messages, assistant, CanonicalMessage("user", results)),
        first.max_tokens,
        first.stream,
        dialect=first.dialect,
        tools=first.tools,
    )


async def collect(stream: AsyncIterator[ConversationEvent]) -> list[ConversationEvent]:
    return [event async for event in stream]


class ToolSession:
    def __init__(
        self,
        boundaries: tuple[tuple[ConversationEvent, ...], ...],
        generation_entered: asyncio.Event | None = None,
        generation_release: asyncio.Event | None = None,
        *,
        start_entered: asyncio.Event | None = None,
        start_release: asyncio.Event | None = None,
        submit_entered: asyncio.Event | None = None,
        submit_release: asyncio.Event | None = None,
    ) -> None:
        self.boundaries = boundaries
        self.generation_entered = generation_entered
        self.generation_release = generation_release
        self.start_entered = start_entered
        self.start_release = start_release
        self.submit_entered = submit_entered
        self.submit_release = submit_release
        self.prompts: list[str] = []
        self.results: list[tuple[ToolResultBlock, ...]] = []
        self.close_count = 0
        self.start_count = 0
        self._resume = asyncio.Event()
        self._failure = asyncio.Event()
        self.closed = asyncio.Event()
        self.watching = asyncio.Event()
        self.submitted = asyncio.Event()
        self.wait_failure_count = 0

    async def start(self) -> None:
        self.start_count += 1
        if self.start_entered is not None:
            self.start_entered.set()
        if self.start_release is not None:
            await self.start_release.wait()

    async def stream_generation(
        self, prompt: str
    ) -> AsyncIterator[ConversationEvent]:
        self.prompts.append(prompt)
        for index, boundary in enumerate(self.boundaries):
            if index:
                await self._resume.wait()
                self._resume.clear()
            if self.generation_entered is not None:
                self.generation_entered.set()
            if self.generation_release is not None:
                await self.generation_release.wait()
            for event in boundary:
                yield event

    async def submit_tool_results(
        self, results: Iterable[ToolResultBlock]
    ) -> None:
        if self.submit_entered is not None:
            self.submit_entered.set()
        if self.submit_release is not None:
            await self.submit_release.wait()
        self.results.append(tuple(results))
        self.submitted.set()
        self._resume.set()

    async def wait_failure(self) -> None:
        self.wait_failure_count += 1
        self.watching.set()
        await self._failure.wait()
        raise BackendFailure("SDK tool protocol failure")

    async def close(self) -> None:
        self.close_count += 1
        self.closed.set()

    def fail_bridge(self) -> None:
        self._failure.set()


class ToolFactory:
    def __init__(self, sessions: tuple[ToolSession, ...]) -> None:
        self._sessions = iter(sessions)
        self.sessions: list[ToolSession] = []
        self.calls: list[tuple[str, str, tuple[ToolDefinition, ...], str]] = []
        self.histories: list[tuple[CanonicalMessage, ...]] = []

    def __call__(
        self,
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
        history: tuple[CanonicalMessage, ...] = (),
        thinking: ThinkingOptions = ThinkingOptions(),
    ) -> ToolSession:
        del thinking
        self.calls.append((model, system, tools, dialect))
        self.histories.append(history)
        session = next(self._sessions)
        self.sessions.append(session)
        return session


@pytest.mark.anyio
async def test_idle_tool_session_rebases_with_the_same_tools() -> None:
    original = ToolSession((final_boundary("original"),))
    rebased = ToolSession((final_boundary("rebased"),))
    factory = ToolFactory((original, rebased))
    registry = SessionRegistry(factory)
    first = first_request(tools=(echo_tool(),))
    first_lease = await registry.open_turn(first, explicit_id="lineage")
    await collect(first_lease.stream())
    rewritten = TextRequest(
        first.model,
        first.system,
        (
            CanonicalMessage.user_text("compacted summary"),
            CanonicalMessage.assistant_text("kept answer"),
            CanonicalMessage.user_text("continue"),
        ),
        first.max_tokens,
        first.stream,
        dialect=first.dialect,
        tools=first.tools,
    )

    lease = await registry.open_turn(rewritten, explicit_id="lineage")

    assert await collect(lease.stream()) == list(final_boundary("rebased"))
    assert factory.histories == [(), rewritten.messages[:-1]]
    assert rebased.start_count == 1
    assert rebased.prompts == ["continue"]
    assert original.close_count == 1


class BoundaryBarrierSession(ToolSession):
    def __init__(self) -> None:
        super().__init__(())
        self.boundary_entered = asyncio.Event()
        self.boundary_release = asyncio.Event()

    async def stream_generation(
        self, prompt: str
    ) -> AsyncIterator[ConversationEvent]:
        self.prompts.append(prompt)
        yield ToolCall("toolu_one", "echo", {"value": "same"})
        self.boundary_entered.set()
        await self.boundary_release.wait()
        yield Completed("tool_use", {"input_tokens": 3, "output_tokens": 2})


class SecretFailureSession(ToolSession):
    async def wait_failure(self) -> None:
        self.wait_failure_count += 1
        self.watching.set()
        await self._failure.wait()
        raise BackendFailure("credential=/Users/alice/private-token")

    async def close(self) -> None:
        self.close_count += 1
        self.closed.set()
        raise BackendFailure("cleanup=/Users/alice/.claude/private-token")


class CancellationResistantSession(ToolSession):
    def __init__(self) -> None:
        super().__init__(())
        self.cancelled = asyncio.Event()

    async def stream_generation(
        self, prompt: str
    ) -> AsyncIterator[ConversationEvent]:
        self.prompts.append(prompt)
        if self.generation_entered is not None:
            self.generation_entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled.set()
            while not self.closed.is_set():
                try:
                    await self.closed.wait()
                except asyncio.CancelledError:
                    continue
        if False:
            yield TextDelta("unreachable")


def call_boundary(*ids: str) -> tuple[ConversationEvent, ...]:
    return (
        *(ToolCall(call_id, "echo", {"value": "same"}) for call_id in ids),
        Completed("tool_use", {"input_tokens": 3, "output_tokens": 2}),
    )


def final_boundary(text: str) -> tuple[ConversationEvent, ...]:
    return (TextDelta(text), Completed("end_turn", {"output_tokens": 1}))


@pytest.mark.anyio
async def test_one_tool_round_keeps_the_sdk_generation_alive() -> None:
    backend = ToolSession((call_boundary("toolu_one"), final_boundary("done")))
    factory = ToolFactory((backend,))
    registry = SessionRegistry(factory)
    first = first_request(tools=(echo_tool(),))

    boundary = await collect((await registry.open_turn(first, None)).stream())
    calls = tuple(event for event in boundary if isinstance(event, ToolCall))
    request = continuation(
        first, calls, (ToolResultBlock(calls[0].id, ("one",), False),)
    )

    assert await collect((await registry.open_turn(request, None)).stream()) == list(
        final_boundary("done")
    )
    assert backend.prompts == ["go"]
    assert backend.results == [
        (ToolResultBlock("toolu_one", ("one",), False),)
    ]
    assert factory.calls == [
        ("sonnet", "system", (echo_tool(),), "anthropic")
    ]


@pytest.mark.anyio
async def test_reverse_order_results_and_completed_replay_keep_ids() -> None:
    backend = ToolSession(
        (call_boundary("toolu_left", "toolu_right"), final_boundary("left/right"))
    )
    registry = SessionRegistry(ToolFactory((backend,)))
    first = first_request(tools=(echo_tool(),))
    original = await collect((await registry.open_turn(first, None)).stream())
    calls = tuple(event for event in original if isinstance(event, ToolCall))
    request = continuation(
        first,
        calls,
        (
            ToolResultBlock(calls[1].id, ("right",), False),
            ToolResultBlock(calls[0].id, ("left",), False),
        ),
    )

    expected = await collect((await registry.open_turn(request, None)).stream())
    replay = await collect((await registry.open_turn(request, None)).stream())

    assert expected == replay == list(final_boundary("left/right"))
    assert backend.results == [request.messages[-1].blocks]
    actor = next(iter(registry._implicit.values()))
    assert isinstance(actor, ToolSessionActor)
    committed_results = actor.transcript[-2].blocks
    assert [item.tool_call_id for item in committed_results] == [
        "toolu_left",
        "toolu_right",
    ]


@pytest.mark.anyio
async def test_tool_configuration_and_dialect_are_frozen_per_conversation() -> None:
    backend = ToolSession((call_boundary("toolu_one"), final_boundary("unused")))
    registry = SessionRegistry(ToolFactory((backend,)))
    first = first_request(tools=(echo_tool(),))
    boundary = await collect(
        (await registry.open_turn(first, explicit_id="lineage")).stream()
    )
    calls = tuple(event for event in boundary if isinstance(event, ToolCall))
    base = continuation(
        first, calls, (ToolResultBlock(calls[0].id, ("one",), False),)
    )

    for changed in (
        TextRequest(
            base.model,
            base.system,
            base.messages,
            base.max_tokens,
            base.stream,
            dialect="openai",
            tools=base.tools,
        ),
        TextRequest(
            base.model,
            base.system,
            base.messages,
            base.max_tokens,
            base.stream,
            dialect=base.dialect,
            tools=(echo_tool(description="changed"),),
        ),
    ):
        with pytest.raises(SessionMismatch):
            await registry.open_turn(changed, explicit_id="lineage")

    assert backend.results == []


@pytest.mark.anyio
async def test_omitted_tools_on_continuation_is_mismatch_without_losing_wait() -> None:
    backend = ToolSession((call_boundary("toolu_one"), final_boundary("done")))
    registry = SessionRegistry(ToolFactory((backend,)))
    first = first_request(tools=(echo_tool(),))
    boundary = await collect(
        (await registry.open_turn(first, explicit_id="lineage")).stream()
    )
    call = next(event for event in boundary if isinstance(event, ToolCall))
    valid = continuation(
        first, (call,), (ToolResultBlock(call.id, ("one",), False),)
    )
    omitted = TextRequest(
        valid.model,
        valid.system,
        valid.messages,
        valid.max_tokens,
        valid.stream,
        dialect=valid.dialect,
        tools=(),
    )

    with pytest.raises(SessionMismatch):
        await registry.open_turn(omitted, explicit_id="lineage")

    lease = await registry.open_turn(valid, explicit_id="lineage")
    assert await collect(lease.stream()) == list(final_boundary("done"))


@pytest.mark.anyio
async def test_waiting_tool_session_is_not_capacity_evictable() -> None:
    backend = ToolSession((call_boundary("toolu_one"), final_boundary("unused")))
    registry = SessionRegistry(ToolFactory((backend,)), max_sessions=1)
    first = first_request(tools=(echo_tool(),))
    await collect((await registry.open_turn(first, explicit_id="tools")).stream())

    with pytest.raises(SessionCapacity):
        await registry.open_turn(
            first_request(tools=(echo_tool(description="other"),)),
            explicit_id="other",
        )

    assert backend.close_count == 0


@pytest.mark.anyio
async def test_disconnect_after_result_commit_buffers_boundary_for_replay() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    backend = ToolSession(
        (call_boundary("toolu_one"), final_boundary("done")), entered, release
    )
    registry = SessionRegistry(ToolFactory((backend,)))
    first = first_request(tools=(echo_tool(),))
    initial = asyncio.create_task(
        collect((await registry.open_turn(first, explicit_id="lineage")).stream())
    )
    await entered.wait()
    release.set()
    calls = tuple(
        event for event in await initial if isinstance(event, ToolCall)
    )
    entered.clear()
    release.clear()
    request = continuation(
        first, calls, (ToolResultBlock(calls[0].id, ("one",), False),)
    )
    lease = await registry.open_turn(request, explicit_id="lineage")
    stream = lease.stream()
    consume = asyncio.create_task(anext(stream))
    await entered.wait()
    consume.cancel()
    await asyncio.gather(consume, return_exceptions=True)
    await lease.abort()
    release.set()
    await asyncio.gather(consume, return_exceptions=True)
    actor = registry._explicit["lineage"]
    assert isinstance(actor, ToolSessionActor)
    assert actor._runner is not None
    await actor._runner

    replay = await registry.open_turn(request, explicit_id="lineage")
    assert await collect(replay.stream()) == list(final_boundary("done"))
    assert backend.close_count == 0


def test_fingerprint_binds_every_tool_transcript_field() -> None:
    tool = echo_tool()
    base = first_request(tools=(tool,))
    call = ToolCall("toolu_one", "echo", {"value": "same"})
    result_request = continuation(
        base, (call,), (ToolResultBlock(call.id, ("one",), False),)
    )
    changed = (
        first_request(tools=(echo_tool(description="changed"),)),
        first_request(tools=(tool,), dialect="openai"),
        continuation(
            base,
            (ToolCall("toolu_other", "echo", {"value": "same"}),),
            (ToolResultBlock("toolu_other", ("one",), False),),
        ),
        continuation(
            base, (call,), (ToolResultBlock(call.id, ("changed",), False),)
        ),
        continuation(
            base, (call,), (ToolResultBlock(call.id, ("one",), True),)
        ),
    )

    fingerprints = {
        request_fingerprint(item) for item in (base, result_request, *changed)
    }
    assert len(fingerprints) == 7


@pytest.mark.anyio
async def test_stale_result_transcript_does_not_consume_waiting_session() -> None:
    backend = ToolSession((call_boundary("toolu_one"), final_boundary("done")))
    registry = SessionRegistry(ToolFactory((backend,)))
    first = first_request(tools=(echo_tool(),))
    boundary = await collect(
        (await registry.open_turn(first, explicit_id="lineage")).stream()
    )
    call = next(event for event in boundary if isinstance(event, ToolCall))
    stale_call = ToolCall("toolu_stale", call.name, call.arguments)
    stale = continuation(
        first,
        (stale_call,),
        (ToolResultBlock(stale_call.id, ("wrong",), False),),
    )

    with pytest.raises(SessionMismatch):
        await registry.open_turn(stale, explicit_id="lineage")

    corrected = continuation(
        first, (call,), (ToolResultBlock(call.id, ("right",), False),)
    )
    assert await collect(
        (await registry.open_turn(corrected, explicit_id="lineage")).stream()
    ) == list(final_boundary("done"))


@pytest.mark.anyio
async def test_repeated_tool_rounds_reuse_one_receive_iterator() -> None:
    backend = ToolSession(
        (
            call_boundary("toolu_first"),
            call_boundary("toolu_second"),
            final_boundary("done"),
        )
    )
    registry = SessionRegistry(ToolFactory((backend,)))
    first = first_request(tools=(echo_tool(),))
    first_events = await collect((await registry.open_turn(first, None)).stream())
    first_call = next(event for event in first_events if isinstance(event, ToolCall))
    second_request = continuation(
        first,
        (first_call,),
        (ToolResultBlock(first_call.id, ("one",), False),),
    )
    second_events = await collect(
        (await registry.open_turn(second_request, None)).stream()
    )
    second_call = next(event for event in second_events if isinstance(event, ToolCall))
    third_request = TextRequest(
        first.model,
        first.system,
        (
            *second_request.messages,
            CanonicalMessage(
                "assistant",
                (
                    ToolCallBlock(
                        second_call.id, second_call.name, second_call.arguments
                    ),
                ),
            ),
            CanonicalMessage(
                "user",
                (ToolResultBlock(second_call.id, ("two",), False),),
            ),
        ),
        1024,
        True,
        tools=first.tools,
    )

    lease = await registry.open_turn(third_request, None)
    assert await collect(lease.stream()) == list(final_boundary("done"))
    assert backend.prompts == ["go"]
    assert len(backend.results) == 2


@pytest.mark.anyio
async def test_in_flight_tool_duplicate_is_rejected() -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    backend = ToolSession((call_boundary("toolu_one"),), entered, release)
    registry = SessionRegistry(ToolFactory((backend,)))
    request = first_request(tools=(echo_tool(),))
    lease = await registry.open_turn(request, None)
    consume = asyncio.create_task(collect(lease.stream()))
    await entered.wait()

    with pytest.raises(SessionConflict, match="in flight"):
        await registry.open_turn(request, None)

    release.set()
    await consume


@pytest.mark.anyio
async def test_disconnect_before_first_commit_closes_session() -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    first_backend = ToolSession((final_boundary("late"),), entered, release)
    retry_backend = ToolSession((final_boundary("retry"),))
    registry = SessionRegistry(ToolFactory((first_backend, retry_backend)))
    request = first_request(tools=(echo_tool(),))
    lease = await registry.open_turn(request, explicit_id="lineage")
    stream = lease.stream()
    consume = asyncio.create_task(anext(stream))
    await entered.wait()
    consume.cancel()
    await asyncio.gather(consume, return_exceptions=True)
    await lease.abort()
    await first_backend.closed.wait()

    retry = await registry.open_turn(request, explicit_id="lineage")
    assert await collect(retry.stream()) == list(final_boundary("retry"))
    assert first_backend.close_count == 1


@pytest.mark.anyio
async def test_late_bridge_failure_closes_parked_session() -> None:
    first_backend = ToolSession((call_boundary("toolu_one"),))
    retry_backend = ToolSession((final_boundary("retry"),))
    registry = SessionRegistry(ToolFactory((first_backend, retry_backend)))
    request = first_request(tools=(echo_tool(),))
    await collect(
        (await registry.open_turn(request, explicit_id="lineage")).stream()
    )

    first_backend.fail_bridge()
    await first_backend.closed.wait()
    retry = await registry.open_turn(request, explicit_id="lineage")

    assert await collect(retry.stream()) == list(final_boundary("retry"))
    assert first_backend.close_count == 1


@pytest.mark.anyio
async def test_generation_deadline_closes_blocked_actor() -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    backend = ToolSession((final_boundary("late"),), entered, release)
    registry = SessionRegistry(ToolFactory((backend,)), turn_timeout_seconds=0.001)
    request = first_request(tools=(echo_tool(),))
    lease = await registry.open_turn(request, explicit_id="lineage")
    consume = asyncio.create_task(collect(lease.stream()))
    await entered.wait()
    with pytest.raises(SessionTimeout):
        await consume
    await backend.closed.wait()
    assert backend.close_count == 1


@pytest.mark.anyio
async def test_expired_tool_result_deadline_wins_before_commit() -> None:
    backend = ToolSession((call_boundary("toolu_one"), final_boundary("unused")))
    registry = SessionRegistry(ToolFactory((backend,)))
    first = first_request(tools=(echo_tool(),))
    boundary = await collect(
        (await registry.open_turn(first, explicit_id="lineage")).stream()
    )
    call = next(event for event in boundary if isinstance(event, ToolCall))
    actor = registry._explicit["lineage"]
    assert isinstance(actor, ToolSessionActor)
    actor._wait_deadline = asyncio.get_running_loop().time()
    request = continuation(
        first, (call,), (ToolResultBlock(call.id, ("too late",), False),)
    )

    with pytest.raises(SessionTimeout):
        await registry.open_turn(request, explicit_id="lineage")

    await backend.closed.wait()
    assert backend.results == []


@pytest.mark.anyio
async def test_shutdown_cancels_waiting_actor_and_closes_once() -> None:
    backend = ToolSession((call_boundary("toolu_one"),))
    registry = SessionRegistry(ToolFactory((backend,)))
    request = first_request(tools=(echo_tool(),))
    await collect((await registry.open_turn(request, None)).stream())

    await registry.close()
    await registry.close()

    assert backend.close_count == 1


@pytest.mark.anyio
async def test_parked_tool_result_timeout_closes_session() -> None:
    backend = ToolSession((call_boundary("toolu_one"),))
    registry = SessionRegistry(
        ToolFactory((backend,)), tool_result_timeout_seconds=0.001
    )
    request = first_request(tools=(echo_tool(),))
    await collect(
        (await registry.open_turn(request, explicit_id="lineage")).stream()
    )

    await backend.closed.wait()

    assert backend.close_count == 1
    assert backend.results == []


@pytest.mark.anyio
async def test_result_commit_invalidates_old_wait_timeout_while_submit_blocks() -> None:
    submit_entered, submit_release = asyncio.Event(), asyncio.Event()
    backend = ToolSession(
        (call_boundary("toolu_one"), final_boundary("done")),
        submit_entered=submit_entered,
        submit_release=submit_release,
    )
    registry = SessionRegistry(
        ToolFactory((backend,)), tool_result_timeout_seconds=0.02
    )
    first = first_request(tools=(echo_tool(),))
    boundary = await collect(
        (await registry.open_turn(first, explicit_id="lineage")).stream()
    )
    call = next(event for event in boundary if isinstance(event, ToolCall))
    actor = registry._explicit["lineage"]
    assert isinstance(actor, ToolSessionActor)
    old_deadline = actor._wait_deadline
    assert old_deadline is not None
    request = continuation(
        first, (call,), (ToolResultBlock(call.id, ("one",), False),)
    )
    lease = await registry.open_turn(request, explicit_id="lineage")
    consume = asyncio.create_task(collect(lease.stream()))
    await submit_entered.wait()

    old_timeout_processed = asyncio.Event()
    asyncio.get_running_loop().call_at(
        old_deadline + 0.01, old_timeout_processed.set
    )
    await old_timeout_processed.wait()

    assert actor._runner is not None
    assert not actor._runner.done()
    submit_release.set()
    assert await consume == list(final_boundary("done"))


@pytest.mark.anyio
async def test_bridge_watcher_is_active_while_sdk_start_is_blocked() -> None:
    start_entered, start_release = asyncio.Event(), asyncio.Event()
    backend = ToolSession(
        (final_boundary("unused"),),
        start_entered=start_entered,
        start_release=start_release,
    )
    registry = SessionRegistry(ToolFactory((backend,)))
    request = first_request(tools=(echo_tool(),))
    lease = await registry.open_turn(request, explicit_id="lineage")
    consume = asyncio.create_task(collect(lease.stream()))
    try:
        await start_entered.wait()
        assert backend.watching.is_set()
        backend.fail_bridge()
        await backend.closed.wait()
        with pytest.raises(BackendFailure, match="Agent SDK query failed"):
            await consume
        assert backend.close_count == 1
        assert backend.wait_failure_count == 1
    finally:
        start_release.set()
        await registry.close()


@pytest.mark.anyio
@pytest.mark.parametrize("winner", ("close", "boundary"))
async def test_close_and_tool_boundary_have_one_linear_winner(winner: str) -> None:
    backend = BoundaryBarrierSession()
    registry = SessionRegistry(ToolFactory((backend,)))
    request = first_request(tools=(echo_tool(),))
    lease = await registry.open_turn(request, explicit_id="lineage")
    actor = registry._explicit["lineage"]
    assert isinstance(actor, ToolSessionActor)
    consume = asyncio.create_task(collect(lease.stream()))
    await backend.boundary_entered.wait()

    if winner == "close":
        await registry.close()
        backend.boundary_release.set()
        with pytest.raises(RuntimeError, match="registry is closed"):
            await consume
        assert actor.replay == {}
        assert actor.transcript == ()
    else:
        backend.boundary_release.set()
        assert await consume == list(call_boundary("toolu_one"))
        assert len(actor.replay) == 1
        assert len(actor.transcript) == 2
        await registry.close()

    assert backend.close_count == 1
    assert actor.state.name == "CLOSED"


@pytest.mark.anyio
@pytest.mark.parametrize("winner", ("shutdown", "result"))
async def test_shutdown_and_tool_result_have_one_linear_winner(winner: str) -> None:
    generation_entered, generation_release = asyncio.Event(), asyncio.Event()
    submit_entered, submit_release = asyncio.Event(), asyncio.Event()
    backend = ToolSession(
        (call_boundary("toolu_one"), final_boundary("unused")),
        generation_entered,
        generation_release,
        submit_entered=submit_entered,
        submit_release=submit_release,
    )
    registry = SessionRegistry(ToolFactory((backend,)))
    first = first_request(tools=(echo_tool(),))
    initial = asyncio.create_task(
        collect((await registry.open_turn(first, explicit_id="lineage")).stream())
    )
    await generation_entered.wait()
    generation_release.set()
    boundary = await initial
    generation_entered.clear()
    generation_release.clear()
    call = next(event for event in boundary if isinstance(event, ToolCall))
    result_request = continuation(
        first, (call,), (ToolResultBlock(call.id, ("one",), False),)
    )
    actor = registry._explicit["lineage"]
    assert isinstance(actor, ToolSessionActor)

    if winner == "shutdown":
        await registry.close()
        with pytest.raises(RuntimeError, match="registry is closed"):
            await registry.open_turn(result_request, explicit_id="lineage")
        assert actor.transcript == first.messages + (
            CanonicalMessage(
                "assistant",
                (ToolCallBlock(call.id, call.name, call.arguments),),
            ),
        )
        assert backend.results == []
    else:
        lease = await registry.open_turn(result_request, explicit_id="lineage")
        consume = asyncio.create_task(collect(lease.stream()))
        await submit_entered.wait()
        submit_release.set()
        await backend.submitted.wait()
        await generation_entered.wait()
        assert actor.transcript == result_request.messages
        await registry.close()
        with pytest.raises(RuntimeError, match="registry is closed"):
            await consume
        assert backend.results == [result_request.messages[-1].blocks]

    assert backend.close_count == 1
    assert len(actor.transcript) in {2, 3}


@pytest.mark.anyio
async def test_mixed_text_and_calls_commit_ordered_assistant_blocks() -> None:
    boundary = (
        TextDelta("thinking"),
        ToolCall("toolu_one", "echo", {"value": "same"}),
        Completed("tool_use", {"input_tokens": 3, "output_tokens": 2}),
    )
    backend = ToolSession((boundary,))
    registry = SessionRegistry(ToolFactory((backend,)))
    request = first_request(tools=(echo_tool(),))

    assert await collect((await registry.open_turn(request, None)).stream()) == list(
        boundary
    )
    actor = next(iter(registry._implicit.values()))
    assert isinstance(actor, ToolSessionActor)
    assert actor.transcript[-1].blocks == (
        TextBlock("thinking"),
        ToolCallBlock("toolu_one", "echo", {"value": "same"}),
    )
    await registry.close()


@pytest.mark.anyio
@pytest.mark.parametrize("busy_state", ("generating", "waiting", "replay"))
async def test_every_busy_tool_state_exhausts_capacity(busy_state: str) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    if busy_state == "generating":
        backend = ToolSession((final_boundary("done"),), entered, release)
    elif busy_state == "waiting":
        backend = ToolSession((call_boundary("toolu_one"),))
    else:
        backend = ToolSession((final_boundary("done"),))
    registry = SessionRegistry(ToolFactory((backend,)), max_sessions=1)
    request = first_request(tools=(echo_tool(),))
    lease = await registry.open_turn(request, explicit_id="busy")
    consume: asyncio.Task[list[ConversationEvent]] | None = None
    if busy_state == "generating":
        consume = asyncio.create_task(collect(lease.stream()))
        await entered.wait()
    else:
        await collect(lease.stream())
        if busy_state == "replay":
            lease = await registry.open_turn(request, explicit_id="busy")

    with pytest.raises(SessionCapacity):
        await registry.open_turn(
            first_request(tools=(echo_tool(description="other"),)),
            explicit_id="other",
        )

    if consume is not None:
        consume.cancel()
        await asyncio.gather(consume, return_exceptions=True)
    await lease.abort()
    await registry.close()
    assert backend.close_count == 1


@pytest.mark.anyio
async def test_actor_and_teardown_failures_are_redacted_and_close_once() -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    backend = SecretFailureSession((final_boundary("unused"),), entered, release)
    registry = SessionRegistry(ToolFactory((backend,)))
    request = first_request(tools=(echo_tool(),))
    lease = await registry.open_turn(request, explicit_id="lineage")
    consume = asyncio.create_task(collect(lease.stream()))
    await entered.wait()
    await backend.watching.wait()

    backend.fail_bridge()
    with pytest.raises(BackendFailure) as captured:
        await consume
    await backend.closed.wait()
    await registry.close()

    assert str(captured.value) == "Agent SDK query failed"
    assert "alice" not in str(captured.value)
    assert backend.close_count == 1
    assert backend.wait_failure_count == 1


@pytest.mark.anyio
async def test_registry_close_reaches_backend_before_waiting_for_actor_workers(
) -> None:
    entered = asyncio.Event()
    backend = CancellationResistantSession()
    backend.generation_entered = entered
    registry = SessionRegistry(
        ToolFactory((backend,)), teardown_timeout_seconds=0.05
    )
    lease = await registry.open_turn(
        first_request(tools=(echo_tool(),)), explicit_id="lineage"
    )
    consume = asyncio.create_task(collect(lease.stream()))
    await entered.wait()
    close = asyncio.create_task(registry.close())

    try:
        await asyncio.wait_for(asyncio.shield(close), timeout=0.2)
    finally:
        backend.closed.set()
        await asyncio.gather(close, return_exceptions=True)
        await asyncio.gather(consume, return_exceptions=True)

    assert backend.cancelled.is_set()
    assert backend.close_count == 1


@pytest.mark.anyio
async def test_cancelled_shutdown_keeps_one_registry_cleanup_for_retry() -> None:
    backend = ToolSession((call_boundary("toolu_one"),))
    registry = SessionRegistry(ToolFactory((backend,)))
    lease = await registry.open_turn(
        first_request(tools=(echo_tool(),)), explicit_id="lineage"
    )
    actor = registry._explicit["lineage"]
    assert isinstance(actor, ToolSessionActor)
    await registry._lock.acquire()
    shutdown = asyncio.create_task(actor.shutdown())

    try:
        while actor.state.name != "CLOSED":
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        shutdown.cancel()
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        assert registry._explicit["lineage"] is actor
    finally:
        registry._lock.release()

    await actor.shutdown()
    await asyncio.wait_for(backend.closed.wait(), timeout=0.2)
    assert "lineage" not in registry._explicit
    assert backend.close_count == 1

    await lease.abort()
    await registry.close()
    assert backend.close_count == 1
