from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest

from claude_sdk_proxy.domain import (
    CanonicalMessage,
    Completed,
    ConversationEvent,
    Dialect,
    TextBlock,
    TextDelta,
    TextRequest,
    ThinkingBlock,
    ThinkingCompleted,
    ToolCall,
    ToolDefinition,
    ToolResultBlock,
)
from claude_sdk_proxy.sessions import (
    SessionCapacity,
    SessionConflict,
    SessionMismatch,
    SessionRegistry,
)
from claude_sdk_proxy.thinking import ThinkingOptions
from tests.gateway.fakes import FakeConversationSession
from tests.gateway.test_sessions import collect, completed_events, first_request
from tests.gateway.test_tool_sessions import (
    ToolFactory,
    ToolSession,
    call_boundary,
    continuation,
    echo_tool,
    final_boundary,
)
from tests.gateway.test_tool_sessions import (
    collect as collect_tool,
)
from tests.gateway.test_tool_sessions import (
    first_request as first_tool_request,
)


def continued(
    request: TextRequest,
    *,
    answer: str = "saved answer",
    prompt: str = "continue",
    model: str | None = None,
    thinking: ThinkingOptions | None = None,
) -> TextRequest:
    return replace(
        request,
        model=model or request.model,
        thinking=request.thinking if thinking is None else thinking,
        messages=(
            *request.messages,
            CanonicalMessage.assistant_text(answer),
            CanonicalMessage.user_text(prompt),
        ),
    )


class RecordingFactory:
    def __init__(self, sessions: tuple[FakeConversationSession, ...]) -> None:
        self._sessions = iter(sessions)
        self.sessions: list[FakeConversationSession] = []
        self.models: list[str] = []
        self.histories: list[tuple[CanonicalMessage, ...]] = []
        self.thinking: list[ThinkingOptions] = []

    def __call__(
        self,
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
        history: tuple[CanonicalMessage, ...] = (),
        thinking: ThinkingOptions = ThinkingOptions(),
    ) -> FakeConversationSession:
        del system, tools, dialect
        session = next(self._sessions)
        self.sessions.append(session)
        self.models.append(model)
        self.histories.append(history)
        self.thinking.append(thinking)
        return session


class BlockingStartSession(FakeConversationSession):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def start(self) -> None:
        self.start_count += 1
        self.entered.set()
        await self.release.wait()


class FailingStartSession(FakeConversationSession):
    async def start(self) -> None:
        self.start_count += 1
        raise RuntimeError("replacement startup failed")


class BlockingGenerationSession(FakeConversationSession):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def stream_generation(
        self, prompt: str
    ) -> AsyncIterator[ConversationEvent]:
        self.prompts.append(prompt)
        self.entered.set()
        await self.release.wait()
        yield TextDelta(self._text)
        yield Completed("end_turn", {"output_tokens": 1})


class NativeThinkingSession(FakeConversationSession):
    async def stream_generation(
        self, prompt: str
    ) -> AsyncIterator[ConversationEvent]:
        self.prompts.append(prompt)
        yield ThinkingCompleted(0, ThinkingBlock("native reasoning", "signed"))
        yield TextDelta(self._text)
        yield Completed("end_turn", {"output_tokens": 1})


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("first_model", "first_thinking", "next_model", "next_thinking"),
    [
        ("sonnet", ThinkingOptions(), "opus", ThinkingOptions()),
        (
            "sonnet",
            ThinkingOptions(mode="adaptive", effort="low"),
            "sonnet",
            ThinkingOptions(mode="adaptive", effort="high"),
        ),
    ],
    ids=("model", "thinking-effort"),
)
async def test_completed_explicit_turn_replaces_generation_settings(
    first_model: str,
    first_thinking: ThinkingOptions,
    next_model: str,
    next_thinking: ThinkingOptions,
) -> None:
    old = FakeConversationSession("saved answer")
    replacement = FakeConversationSession("continued answer")
    factory = RecordingFactory((old, replacement))
    registry = SessionRegistry(factory)
    first = replace(
        first_request("hello"), model=first_model, thinking=first_thinking
    )
    try:
        first_lease = await registry.open_turn(first, explicit_id="lineage")
        await collect(first_lease.stream())
        changed = continued(
            first, model=next_model, thinking=next_thinking
        )

        second_lease = await registry.open_turn(changed, explicit_id="lineage")

        assert second_lease.response_headers == {
            "X-Claude-Proxy-Session": "lineage"
        }
        assert factory.models == [first_model, next_model]
        assert factory.thinking == [first_thinking, next_thinking]
        assert factory.histories[-1] == changed.messages[:-1]
        assert old.close_count == 1
        assert await collect(second_lease.stream()) == completed_events(
            "continued answer"
        )
    finally:
        await registry.close()


@pytest.mark.anyio
async def test_openai_setting_switch_seeds_stored_native_history() -> None:
    old = NativeThinkingSession("saved answer")
    replacement = FakeConversationSession("continued answer")
    factory = RecordingFactory((old, replacement))
    registry = SessionRegistry(factory)
    first = replace(first_request("hello"), dialect="openai")
    try:
        first_lease = await registry.open_turn(first, explicit_id="lineage")
        await collect(first_lease.stream())
        changed = continued(first, model="opus")

        second_lease = await registry.open_turn(changed, explicit_id="lineage")

        assert factory.histories[-1] == (
            CanonicalMessage.user_text("hello"),
            CanonicalMessage(
                "assistant",
                (
                    ThinkingBlock("native reasoning", "signed"),
                    TextBlock("saved answer"),
                ),
            ),
        )
        assert await collect(second_lease.stream()) == completed_events(
            "continued answer"
        )
    finally:
        await registry.close()


@pytest.mark.anyio
async def test_setting_switch_retry_replays_without_another_replacement() -> None:
    old = FakeConversationSession("saved answer")
    replacement = FakeConversationSession("continued answer")
    factory = RecordingFactory((old, replacement))
    registry = SessionRegistry(factory)
    first = first_request("hello")
    try:
        await collect((await registry.open_turn(first, "lineage")).stream())
        changed = continued(first, model="opus")
        assert await collect(
            (await registry.open_turn(changed, "lineage")).stream()
        ) == completed_events("continued answer")

        retry = await registry.open_turn(changed, "lineage")

        assert await collect(retry.stream()) == completed_events("continued answer")
        assert factory.models == ["sonnet", "opus"]
        assert old.close_count == 1
    finally:
        await registry.close()


@pytest.mark.anyio
async def test_changed_setting_stale_retry_is_rejected_and_original_is_usable(
) -> None:
    backend = FakeConversationSession("saved answer")
    factory = RecordingFactory((backend,))
    registry = SessionRegistry(factory)
    first = first_request("hello")
    try:
        await collect((await registry.open_turn(first, "lineage")).stream())

        with pytest.raises(SessionMismatch, match="stale"):
            await registry.open_turn(replace(first, model="opus"), "lineage")

        current = continued(first, prompt="still here")
        assert await collect(
            (await registry.open_turn(current, "lineage")).stream()
        ) == completed_events("saved answer")
        assert backend.close_count == 0
    finally:
        await registry.close()


@pytest.mark.anyio
@pytest.mark.parametrize("fixed_field", ("system", "dialect", "tools"))
async def test_setting_switch_cannot_change_fixed_configuration(
    fixed_field: str,
) -> None:
    backend = FakeConversationSession("saved answer")
    factory = RecordingFactory((backend,))
    registry = SessionRegistry(factory)
    first = first_request("hello")
    try:
        await collect((await registry.open_turn(first, "lineage")).stream())
        changed = continued(first, model="opus")
        if fixed_field == "system":
            changed = replace(changed, system="different")
        elif fixed_field == "dialect":
            changed = replace(changed, dialect="openai")
        else:
            changed = replace(changed, tools=(echo_tool(),))

        expected = "tool" if fixed_field == "tools" else fixed_field
        with pytest.raises(SessionMismatch, match=expected):
            await registry.open_turn(changed, "lineage")

        current = continued(first, prompt="still here")
        assert await collect(
            (await registry.open_turn(current, "lineage")).stream()
        ) == completed_events("saved answer")
        assert backend.close_count == 0
    finally:
        await registry.close()


@pytest.mark.anyio
async def test_setting_switch_is_rejected_while_assistant_turn_is_active() -> None:
    backend = BlockingGenerationSession("saved answer")
    factory = RecordingFactory((backend,))
    registry = SessionRegistry(factory)
    first = first_request("hello")
    try:
        lease = await registry.open_turn(first, "lineage")
        consume = asyncio.create_task(collect(lease.stream()))
        await asyncio.wait_for(backend.entered.wait(), 2)

        with pytest.raises(SessionConflict, match="busy"):
            await registry.open_turn(continued(first, model="opus"), "lineage")

        backend.release.set()
        await consume
    finally:
        backend.release.set()
        await registry.close()


@pytest.mark.anyio
async def test_simultaneous_setting_switches_have_one_owner() -> None:
    old = FakeConversationSession("saved answer")
    candidate = BlockingStartSession("continued answer")
    factory = RecordingFactory((old, candidate))
    registry = SessionRegistry(factory)
    first = first_request("hello")
    try:
        await collect((await registry.open_turn(first, "lineage")).stream())
        changed = continued(first, model="opus")
        switching = asyncio.create_task(registry.open_turn(changed, "lineage"))
        await asyncio.wait_for(candidate.entered.wait(), 2)

        with pytest.raises(SessionConflict, match="in flight"):
            await registry.open_turn(changed, "lineage")

        candidate.release.set()
        lease = await switching
        assert lease.response_headers["X-Claude-Proxy-Session"] == "lineage"
        await collect(lease.stream())
    finally:
        candidate.release.set()
        await registry.close()


@pytest.mark.anyio
async def test_implicit_setting_switch_rejects_multiple_exact_predecessors() -> None:
    sonnet = FakeConversationSession("same answer")
    opus = FakeConversationSession("same answer")
    factory = RecordingFactory((sonnet, opus))
    registry = SessionRegistry(factory)
    first = first_request("hello")
    try:
        await collect((await registry.open_turn(first, None)).stream())
        await collect(
            (await registry.open_turn(replace(first, model="opus"), None)).stream()
        )

        with pytest.raises(SessionMismatch, match="multiple"):
            await registry.open_turn(
                continued(first, answer="same answer", model="opus"), None
            )

        assert sonnet.close_count == opus.close_count == 0
    finally:
        await registry.close()


@pytest.mark.anyio
async def test_implicit_setting_switch_replaces_one_exact_predecessor() -> None:
    old = FakeConversationSession("saved answer")
    replacement = FakeConversationSession("continued answer")
    factory = RecordingFactory((old, replacement))
    registry = SessionRegistry(factory)
    first = first_request("hello")
    try:
        original = await registry.open_turn(first, None)
        await collect(original.stream())

        switched = await registry.open_turn(continued(first, model="opus"), None)

        assert switched.response_headers == original.response_headers
        assert factory.models == ["sonnet", "opus"]
        assert old.close_count == 1
        assert await collect(switched.stream()) == completed_events(
            "continued answer"
        )
    finally:
        await registry.close()


@pytest.mark.anyio
async def test_setting_switch_requires_the_exact_completed_transcript() -> None:
    old = FakeConversationSession("saved answer")
    factory = RecordingFactory((old,))
    registry = SessionRegistry(factory)
    first = first_request("hello")
    try:
        await collect((await registry.open_turn(first, "lineage")).stream())
        edited = continued(first, answer="edited answer", model="opus")

        with pytest.raises(SessionMismatch, match="transcript"):
            await registry.open_turn(edited, "lineage")

        current = continued(first, prompt="still here")
        assert await collect(
            (await registry.open_turn(current, "lineage")).stream()
        ) == completed_events("saved answer")
        assert old.close_count == 0
    finally:
        await registry.close()


@pytest.mark.anyio
async def test_unrelated_implicit_conversation_does_not_replace_a_session() -> None:
    old = FakeConversationSession("saved answer")
    unrelated = FakeConversationSession("independent answer")
    factory = RecordingFactory((old, unrelated))
    registry = SessionRegistry(factory)
    try:
        first = await registry.open_turn(first_request("hello"), None)
        await collect(first.stream())

        new = await registry.open_turn(first_request("different", model="opus"), None)

        assert new.response_headers != first.response_headers
        assert old.close_count == 0
        assert factory.histories[-1] == ()
        await collect(new.stream())
    finally:
        await registry.close()


@pytest.mark.anyio
async def test_pending_tool_results_cannot_change_generation_settings() -> None:
    backend = ToolSession(
        (call_boundary("toolu_one"), final_boundary("finished answer"))
    )
    factory = ToolFactory((backend,))
    registry = SessionRegistry(factory)
    first = first_tool_request(tools=(echo_tool(),))
    try:
        boundary = await collect_tool(
            (await registry.open_turn(first, "lineage")).stream()
        )
        calls = tuple(event for event in boundary if isinstance(event, ToolCall))
        results = continuation(
            first,
            calls,
            (ToolResultBlock("toolu_one", ("saved",), False),),
        )

        with pytest.raises(SessionMismatch, match="pending tools"):
            await registry.open_turn(replace(results, model="opus"), "lineage")

        lease = await registry.open_turn(results, "lineage")
        assert await collect_tool(lease.stream()) == list(
            final_boundary("finished answer")
        )
        assert backend.close_count == 0
    finally:
        await registry.close()


@pytest.mark.anyio
async def test_completed_tool_turn_can_switch_model() -> None:
    old = ToolSession((final_boundary("saved answer"),))
    replacement = ToolSession((final_boundary("continued answer"),))
    factory = ToolFactory((old, replacement))
    registry = SessionRegistry(factory)
    first = first_tool_request(tools=(echo_tool(),))
    try:
        await collect_tool((await registry.open_turn(first, "lineage")).stream())
        changed = continued(first, model="opus")

        lease = await registry.open_turn(changed, "lineage")

        assert lease.response_headers["X-Claude-Proxy-Session"] == "lineage"
        assert factory.calls[-1][0] == "opus"
        assert factory.histories[-1] == changed.messages[:-1]
        assert old.close_count == 1
        assert await collect_tool(lease.stream()) == list(
            final_boundary("continued answer")
        )
    finally:
        await registry.close()


@pytest.mark.anyio
async def test_failed_setting_switch_preserves_original_session() -> None:
    old = FakeConversationSession("saved answer")
    candidate = FailingStartSession("unused")
    factory = RecordingFactory((old, candidate))
    registry = SessionRegistry(factory)
    first = first_request("hello")
    try:
        await collect((await registry.open_turn(first, "lineage")).stream())

        with pytest.raises(RuntimeError, match="startup failed"):
            await registry.open_turn(continued(first, model="opus"), "lineage")

        current = continued(first, prompt="still here")
        assert await collect(
            (await registry.open_turn(current, "lineage")).stream()
        ) == completed_events("saved answer")
        assert old.close_count == 0
        assert candidate.close_count == 1
    finally:
        await registry.close()


@pytest.mark.anyio
async def test_cancelled_setting_switch_preserves_original_session() -> None:
    old = FakeConversationSession("saved answer")
    candidate = BlockingStartSession("unused")
    factory = RecordingFactory((old, candidate))
    registry = SessionRegistry(factory)
    first = first_request("hello")
    try:
        await collect((await registry.open_turn(first, "lineage")).stream())
        switching = asyncio.create_task(
            registry.open_turn(continued(first, model="opus"), "lineage")
        )
        await asyncio.wait_for(candidate.entered.wait(), 2)

        switching.cancel()
        with pytest.raises(asyncio.CancelledError):
            await switching

        current = continued(first, prompt="still here")
        assert await collect(
            (await registry.open_turn(current, "lineage")).stream()
        ) == completed_events("saved answer")
        assert old.close_count == 0
        assert candidate.close_count == 1
    finally:
        candidate.release.set()
        await registry.close()


@pytest.mark.anyio
async def test_invalidated_predecessor_is_not_selected_for_setting_switch() -> None:
    old = FakeConversationSession("unused")
    replacement = FakeConversationSession("imported answer")
    factory = RecordingFactory((old, replacement))
    registry = SessionRegistry(factory)
    first = first_request("hello")
    try:
        lease = await registry.open_turn(first, "lineage")
        await lease.abort()
        changed = continued(first, model="opus")

        imported = await registry.open_turn(changed, "lineage")

        assert imported.response_headers["X-Claude-Proxy-Session"] == "lineage"
        assert factory.histories[-1] == changed.messages[:-1]
        assert old.close_count == 1
        assert await collect(imported.stream()) == completed_events("imported answer")
    finally:
        await registry.close()


@pytest.mark.anyio
@pytest.mark.parametrize("same_id", (False, True), ids=("capacity", "new-owner"))
async def test_setting_switch_startup_cannot_overwrite_a_new_owner(
    same_id: bool,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    newer_release = asyncio.Event()
    old = ToolSession((final_boundary("saved answer"),))
    candidate = ToolSession(
        (final_boundary("candidate"),),
        start_entered=entered,
        start_release=release,
    )
    newer = ToolSession(
        (final_boundary("new owner"),), generation_release=newer_release
    )
    factory = ToolFactory((old, candidate, newer))
    registry = SessionRegistry(factory, max_sessions=1)
    first = first_tool_request(tools=(echo_tool(),))
    try:
        await collect_tool((await registry.open_turn(first, "lineage")).stream())
        switching = asyncio.create_task(
            registry.open_turn(continued(first, model="opus"), "lineage")
        )
        await asyncio.wait_for(entered.wait(), 2)
        old.fail_bridge()
        await asyncio.wait_for(old.closed.wait(), 2)
        new_id = "lineage" if same_id else "different"
        new_owner = await registry.open_turn(first, new_id)

        release.set()
        with pytest.raises(SessionConflict if same_id else SessionCapacity):
            await switching
        await asyncio.wait_for(candidate.closed.wait(), 2)
        assert new_owner.response_headers["X-Claude-Proxy-Session"] == new_id
        assert newer.close_count == 0
    finally:
        release.set()
        newer_release.set()
        await registry.close()
