from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ConversationResetMessage,
    DeferredToolUse,
    HookEventMessage,
    MirrorErrorMessage,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    StreamEvent,
    SystemMessage,
    TaskStartedMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk import (
    ToolResultBlock as SdkToolResultBlock,
)

from claude_sdk_proxy.domain import (
    BackendFailure,
    Completed,
    InputUsage,
    TextDelta,
    ToolCall,
    ToolDefinition,
    ToolResultBlock,
)
from claude_sdk_proxy.sdk_session import SdkSession
from claude_sdk_proxy.sdk_tool_protocol import RawSdkMessageValidator
from tests.gateway.fakes import (
    FakeSdkClient,
    FixedTemporaryDirectory,
    raw_text_events,
    raw_tool_events,
    sdk_response,
)


@pytest.mark.anyio
async def test_sdk_session_reuses_one_client_for_two_turns(tmp_path: Path) -> None:
    client = FakeSdkClient(
        responses=(
            sdk_response("one", session_id="sdk-1"),
            sdk_response("two", session_id="sdk-1"),
        )
    )
    session = SdkSession(
        model="sonnet",
        system="system",
        directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
        client_factory=lambda options: client.capture_options(options),
    )

    await session.start()
    first = [event async for event in session.stream_turn("first")]
    second = [event async for event in session.stream_turn("second")]
    await session.close()

    assert first == [
        InputUsage(2),
        TextDelta("one"),
        Completed("end_turn", {"input_tokens": 2, "output_tokens": 1}),
    ]
    assert second == [
        InputUsage(2),
        TextDelta("two"),
        Completed("end_turn", {"input_tokens": 2, "output_tokens": 1}),
    ]
    assert client.connect_count == 1
    assert client.prompts == ["first", "second"]
    assert client.disconnect_count == 1


@pytest.mark.anyio
async def test_sdk_session_excludes_safe_mode_from_restricted_options(
    tmp_path: Path,
) -> None:
    client = FakeSdkClient(responses=(sdk_response("one", session_id="sdk-1"),))
    session = SdkSession(
        model="sonnet",
        system="system",
        directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
        client_factory=lambda options: client.capture_options(options),
    )

    await session.start()
    await session.close()

    assert client.options is not None
    assert client.options.extra_args == {
        "restricted": None,
        "disable-slash-commands": None,
        "no-session-persistence": None,
    }


@pytest.mark.anyio
async def test_sdk_session_rejects_built_in_tool_output(tmp_path: Path) -> None:
    client = FakeSdkClient(
        responses=(
            (
                AssistantMessage(
                    content=[ToolUseBlock(id="tool-1", name="Bash", input={})],
                    model="sonnet",
                ),
            ),
        )
    )
    session = SdkSession(
        model="sonnet",
        system="system",
        directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
        client_factory=lambda options: client.capture_options(options),
    )

    await session.start()
    with pytest.raises(BackendFailure, match="protocol"):
        _ = [event async for event in session.stream_turn("first")]
    await session.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "block_type",
    ("tool_use", "server_tool_use", "tool_result", "server_tool_result"),
)
async def test_sdk_session_rejects_raw_tool_blocks_before_completed(
    tmp_path: Path, block_type: str
) -> None:
    client = FakeSdkClient(
        responses=(
            (
                StreamEvent(
                    uuid="event-1",
                    session_id="sdk-1",
                    event={
                        "type": "content_block_start",
                        "content_block": {"type": block_type},
                    },
                ),
                ResultMessage(
                    subtype="success",
                    duration_ms=0,
                    duration_api_ms=0,
                    is_error=False,
                    num_turns=1,
                    session_id="sdk-1",
                    stop_reason="end_turn",
                    usage={"output_tokens": 1},
                ),
            ),
        )
    )
    session = SdkSession(
        model="sonnet",
        system="system",
        directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
        client_factory=lambda options: client.capture_options(options),
    )

    await session.start()
    with pytest.raises(BackendFailure, match="protocol"):
        _ = [event async for event in session.stream_turn("first")]
    await session.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "block",
    (
        ToolUseBlock(id="tool-1", name="Bash", input={}),
        ServerToolUseBlock(id="tool-1", name="advisor", input={}),
        SdkToolResultBlock(tool_use_id="tool-1"),
        ServerToolResultBlock(tool_use_id="tool-1", content={}),
    ),
)
async def test_sdk_session_rejects_complete_tool_blocks_before_completed(
    tmp_path: Path,
    block: ToolUseBlock
    | ServerToolUseBlock
    | SdkToolResultBlock
    | ServerToolResultBlock,
) -> None:
    client = FakeSdkClient(
        responses=(
            (
                AssistantMessage(content=[block], model="sonnet"),
                ResultMessage(
                    subtype="success",
                    duration_ms=0,
                    duration_api_ms=0,
                    is_error=False,
                    num_turns=1,
                    session_id="sdk-1",
                    stop_reason="end_turn",
                    usage={"output_tokens": 1},
                ),
            ),
        )
    )
    session = SdkSession(
        model="sonnet",
        system="system",
        directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
        client_factory=lambda options: client.capture_options(options),
    )

    await session.start()
    with pytest.raises(BackendFailure, match="protocol"):
        _ = [event async for event in session.stream_turn("first")]
    await session.close()


@pytest.mark.anyio
async def test_sdk_session_does_not_restart_after_close(tmp_path: Path) -> None:
    client = FakeSdkClient(responses=())
    factory_calls = 0

    def client_factory(options: object) -> FakeSdkClient:
        nonlocal factory_calls
        factory_calls += 1
        return client.capture_options(options)  # type: ignore[arg-type]

    session = SdkSession(
        model="sonnet",
        system="system",
        directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
        client_factory=client_factory,
    )

    await session.start()
    await session.close()
    with pytest.raises(RuntimeError, match="closed"):
        await session.start()

    assert factory_calls == 1


def result_message(**overrides: object) -> ResultMessage:
    fields: dict[str, object] = {
        "subtype": "success",
        "duration_ms": 0,
        "duration_api_ms": 0,
        "is_error": False,
        "num_turns": 1,
        "session_id": "sdk-1",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 2, "output_tokens": 1},
    }
    fields.update(overrides)
    return ResultMessage(**fields)  # type: ignore[arg-type]


def text_event(
    *,
    session_id: object = "sdk-1",
    parent_tool_use_id: object = None,
) -> StreamEvent:
    return StreamEvent(
        uuid="event-1",
        session_id=session_id,  # type: ignore[arg-type]
        parent_tool_use_id=parent_tool_use_id,  # type: ignore[arg-type]
        event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "answer"},
        },
    )


async def collect_sdk_response(
    tmp_path: Path, messages: tuple[object, ...]
) -> list[object]:
    client = FakeSdkClient(responses=(messages,))
    session = SdkSession(
        model="sonnet",
        system="system",
        directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
        client_factory=lambda options: client.capture_options(options),
    )
    await session.start()
    try:
        return [event async for event in session.stream_turn("first")]
    finally:
        await session.close()


@pytest.mark.anyio
async def test_sdk_session_accepts_only_complete_ordered_raw_text_sequence(
    tmp_path: Path,
) -> None:
    messages = (*raw_text_events("answer", "sdk-1"), result_message())

    events = await collect_sdk_response(tmp_path, messages)

    assert events == [
        InputUsage(2),
        TextDelta("answer"),
        Completed("end_turn", {"input_tokens": 2, "output_tokens": 1}),
    ]


@pytest.mark.anyio
async def test_sdk_session_accepts_observed_complete_assistant_placement(
    tmp_path: Path,
) -> None:
    raw = raw_text_events("answer", "sdk-1")
    assistant = AssistantMessage([TextBlock("answer")], "sonnet", session_id="sdk-1")
    messages = (*raw[:3], assistant, *raw[3:], result_message())

    events = await collect_sdk_response(tmp_path, messages)

    assert events == [
        InputUsage(2),
        TextDelta("answer"),
        Completed("end_turn", {"input_tokens": 2, "output_tokens": 1}),
    ]


@pytest.mark.anyio
async def test_text_session_accepts_typed_text_split_across_blocks(
    tmp_path: Path,
) -> None:
    raw = raw_text_events("answer", "sdk-1")
    assistant = AssistantMessage(
        [TextBlock("ans"), TextBlock("wer")], "sonnet", session_id="sdk-1"
    )

    events = await collect_sdk_response(
        tmp_path, (*raw[:3], assistant, *raw[3:], result_message())
    )

    assert events == [
        InputUsage(2),
        TextDelta("answer"),
        Completed("end_turn", {"input_tokens": 2, "output_tokens": 1}),
    ]


@pytest.mark.anyio
async def test_text_session_rejects_a_second_raw_text_block(tmp_path: Path) -> None:
    raw = raw_text_events("one", "sdk-1")
    second = (
        StreamEvent(
            "event-second-start",
            "sdk-1",
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        StreamEvent(
            "event-second-delta",
            "sdk-1",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "two"},
            },
        ),
        StreamEvent(
            "event-second-stop",
            "sdk-1",
            {"type": "content_block_stop", "index": 1},
        ),
        AssistantMessage(
            [TextBlock("one"), TextBlock("two")],
            "sonnet",
            session_id="sdk-1",
        ),
    )

    with pytest.raises(BackendFailure, match="protocol"):
        await collect_sdk_response(
            tmp_path, (*raw[:4], *second, *raw[4:], result_message())
        )


@pytest.mark.anyio
async def test_sdk_session_rejects_text_delta_after_complete_assistant_message(
    tmp_path: Path,
) -> None:
    raw = raw_text_events("a", "sdk-1")
    assistant = AssistantMessage([TextBlock("a")], "sonnet", session_id="sdk-1")
    late_delta = StreamEvent(
        uuid="event-late-delta",
        session_id="sdk-1",
        event={
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "secret-b"},
        },
    )

    with pytest.raises(BackendFailure, match="protocol") as error:
        await collect_sdk_response(
            tmp_path,
            (*raw[:3], assistant, late_delta, *raw[3:], result_message()),
        )

    assert "secret-b" not in str(error.value)


@pytest.mark.anyio
async def test_sdk_session_rejects_unknown_raw_event_without_leaking_it(
    tmp_path: Path,
) -> None:
    unknown = StreamEvent(
        uuid="event-secret",
        session_id="sdk-1",
        event={"type": "future_secret_event", "payload": "secret-payload"},
    )

    with pytest.raises(BackendFailure, match="protocol") as error:
        await collect_sdk_response(tmp_path, (unknown, result_message()))

    assert "secret" not in str(error.value)


@pytest.mark.anyio
async def test_sdk_session_rejects_raw_text_delta_before_block_start(
    tmp_path: Path,
) -> None:
    with pytest.raises(BackendFailure, match="protocol"):
        await collect_sdk_response(tmp_path, (text_event(), result_message()))


@pytest.mark.anyio
@pytest.mark.parametrize(
    "content",
    [
        [ThinkingBlock("secret-thought", "secret-signature")],
        "secret-not-a-list",
        [object()],
    ],
)
async def test_sdk_session_requires_every_assistant_block_to_be_exact_text(
    tmp_path: Path, content: object
) -> None:
    assistant = AssistantMessage(content, "sonnet")  # type: ignore[arg-type]

    with pytest.raises(BackendFailure, match="protocol") as error:
        await collect_sdk_response(tmp_path, (assistant, result_message()))

    assert "secret" not in str(error.value)


@pytest.mark.anyio
@pytest.mark.parametrize("is_error", [None, 0, ""])
async def test_sdk_session_requires_exact_false_success_flag(
    tmp_path: Path, is_error: object
) -> None:
    with pytest.raises(BackendFailure, match="protocol") as error:
        await collect_sdk_response(
            tmp_path,
            (*raw_text_events("answer", "sdk-1"), result_message(is_error=is_error)),
        )
    assert repr(is_error) not in str(error.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "usage",
    [
        [],
        {"input_tokens": True, "output_tokens": 1},
        {"input_tokens": -1, "output_tokens": 1},
        {"input_tokens": 1, "output_tokens": "secret-usage"},
    ],
)
async def test_sdk_session_rejects_malformed_used_usage_counters(
    tmp_path: Path, usage: object
) -> None:
    with pytest.raises(BackendFailure, match="protocol") as error:
        await collect_sdk_response(
            tmp_path,
            (*raw_text_events("answer", "sdk-1"), result_message(usage=usage)),
        )
    assert "secret-usage" not in str(error.value)


@pytest.mark.anyio
@pytest.mark.parametrize("stop_reason", [None, "tool_use", 1])
async def test_sdk_session_rejects_non_text_stop_reasons(
    tmp_path: Path, stop_reason: object
) -> None:
    with pytest.raises(BackendFailure, match="protocol") as error:
        await collect_sdk_response(
            tmp_path,
            (
                *raw_text_events("answer", "sdk-1"),
                result_message(stop_reason=stop_reason),
            ),
        )
    assert "tool_use" not in str(error.value)


@pytest.mark.anyio
async def test_sdk_session_rejects_deferred_tool_result_state(tmp_path: Path) -> None:
    deferred = DeferredToolUse("tool-secret", "Bash", {"command": "secret"})
    with pytest.raises(BackendFailure, match="protocol") as error:
        await collect_sdk_response(
            tmp_path,
            (
                *raw_text_events("answer", "sdk-1"),
                result_message(deferred_tool_use=deferred),
            ),
        )
    assert "tool-secret" not in str(error.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "messages",
    [
        (text_event(parent_tool_use_id="tool-secret"), result_message()),
        (
            *raw_text_events("answer", "sdk-1"),
            result_message(session_id="sdk-other-secret"),
        ),
        (text_event(session_id=""), result_message()),
    ],
)
async def test_sdk_session_rejects_parent_or_mismatched_stream_identity(
    tmp_path: Path, messages: tuple[object, ...]
) -> None:
    with pytest.raises(BackendFailure, match="protocol") as error:
        await collect_sdk_response(tmp_path, messages)
    assert "secret" not in str(error.value)


@pytest.mark.anyio
async def test_sdk_session_rejects_parent_attributed_complete_message(
    tmp_path: Path,
) -> None:
    message = AssistantMessage(
        [TextBlock("answer")],
        "sonnet",
        parent_tool_use_id="tool-secret",
        session_id="sdk-1",
    )
    with pytest.raises(BackendFailure, match="protocol") as error:
        await collect_sdk_response(tmp_path, (message, result_message()))
    assert "tool-secret" not in str(error.value)


@pytest.mark.anyio
async def test_sdk_session_rejects_injected_result_origin(tmp_path: Path) -> None:
    with pytest.raises(BackendFailure, match="protocol"):
        await collect_sdk_response(
            tmp_path,
            (
                *raw_text_events("answer", "sdk-1"),
                result_message(origin={"kind": "task-notification"}),
            ),
        )


@pytest.mark.anyio
async def test_sdk_session_rejects_non_success_result_subtype(tmp_path: Path) -> None:
    with pytest.raises(BackendFailure, match="protocol"):
        await collect_sdk_response(
            tmp_path,
            (
                *raw_text_events("answer", "sdk-1"),
                result_message(subtype="secret-subtype"),
            ),
        )


@pytest.mark.anyio
async def test_sdk_session_normalizes_only_gateway_usage_counters(
    tmp_path: Path,
) -> None:
    events = await collect_sdk_response(
        tmp_path,
        (
            *raw_text_events("answer", "sdk-1"),
            result_message(
                usage={
                    "input_tokens": 2,
                    "output_tokens": 1,
                    "cache_read_input_tokens": 4,
                    "service_tier": "standard",
                }
            ),
        ),
    )
    assert events == [
        InputUsage(2),
        TextDelta("answer"),
        Completed("end_turn", {"input_tokens": 2, "output_tokens": 1}),
    ]


@pytest.mark.anyio
async def test_sdk_session_emits_message_start_input_usage_before_text(
    tmp_path: Path,
) -> None:
    raw = raw_text_events("answer", "sdk-1", input_tokens=7)
    events = await collect_sdk_response(
        tmp_path,
        (
            *raw,
            result_message(usage={"input_tokens": 7, "output_tokens": 1}),
        ),
    )
    assert events == [
        InputUsage(7),
        TextDelta("answer"),
        Completed("end_turn", {"input_tokens": 7, "output_tokens": 1}),
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "usage",
    [None, {}, {"input_tokens": True}, {"input_tokens": "secret-usage"}],
)
async def test_sdk_session_rejects_malformed_message_start_usage(
    tmp_path: Path, usage: object
) -> None:
    start = StreamEvent(
        uuid="event-start",
        session_id="sdk-1",
        event={"type": "message_start", "message": {"usage": usage}},
    )
    with pytest.raises(BackendFailure, match="protocol") as error:
        await collect_sdk_response(
            tmp_path, (start, *raw_text_events("answer", "sdk-1")[1:], result_message())
        )
    assert "secret-usage" not in str(error.value)


@pytest.mark.anyio
async def test_sdk_session_keeps_boundary_usage_when_aggregate_usage_differs(
    tmp_path: Path,
) -> None:
    start = StreamEvent(
        uuid="event-start",
        session_id="sdk-1",
        event={
            "type": "message_start",
            "message": {"usage": {"input_tokens": 7}},
        },
    )
    events = await collect_sdk_response(
        tmp_path,
        (
            start,
            *raw_text_events("answer", "sdk-1")[1:],
            result_message(usage={"input_tokens": 8, "output_tokens": 99}),
        ),
    )
    assert events[-1] == Completed("end_turn", {"input_tokens": 7, "output_tokens": 1})


@pytest.mark.anyio
async def test_sdk_session_requires_same_identity_across_turns(tmp_path: Path) -> None:
    client = FakeSdkClient(
        responses=(
            (*raw_text_events("answer", "sdk-1"), result_message()),
            (
                *raw_text_events("answer", "sdk-other"),
                result_message(session_id="sdk-other"),
            ),
        )
    )
    session = SdkSession(
        "sonnet",
        "system",
        directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
        client_factory=lambda options: client.capture_options(options),
    )
    await session.start()
    assert [event async for event in session.stream_turn("first")]
    with pytest.raises(BackendFailure, match="protocol"):
        _ = [event async for event in session.stream_turn("second")]
    await session.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "stop_reason",
    ("end_turn", "max_tokens", "model_context_window_exceeded", "refusal"),
)
async def test_sdk_session_accepts_legitimate_text_stop_reasons(
    tmp_path: Path, stop_reason: str
) -> None:
    events = await collect_sdk_response(
        tmp_path,
        (
            *raw_text_events("answer", "sdk-1", stop_reason=stop_reason),
            result_message(stop_reason=stop_reason),
        ),
    )
    assert events[-1] == Completed(stop_reason, {"input_tokens": 2, "output_tokens": 1})


def live_system(message_subtype: str, **changes: object) -> SystemMessage:
    data: dict[str, object] = {
        "type": "system",
        "subtype": message_subtype,
        "session_id": "sdk-1",
        "uuid": f"{message_subtype}-1",
    }
    if message_subtype == "init":
        data.update(
            model="claude-sonnet-5",
            tools=[],
            mcp_servers=[],
            skills=[],
            plugins=[],
            permissionMode="dontAsk",
        )
    else:
        data["status"] = "requesting"
    data.update(changes)
    return SystemMessage(message_subtype, data)  # type: ignore[arg-type]


def live_rate_limit(**changes: object) -> RateLimitEvent:
    fields: dict[str, object] = {
        "status": "allowed_warning",
        "resets_at": 1_789_052_400,
        "rate_limit_type": "seven_day",
        "utilization": 0.27,
        "raw": {"status": "allowed_warning"},
    }
    fields.update(changes)
    info = RateLimitInfo(**fields)  # type: ignore[arg-type]
    return RateLimitEvent(info, "rate-1", "sdk-1")


@pytest.mark.anyio
async def test_sdk_session_allows_live_system_and_rate_messages(tmp_path: Path) -> None:
    events = await collect_sdk_response(
        tmp_path,
        (
            live_system("init"),
            live_system("status"),
            *raw_text_events("answer", "sdk-1"),
            live_rate_limit(),
            result_message(),
        ),
    )
    assert events == [
        InputUsage(2),
        TextDelta("answer"),
        Completed("end_turn", {"input_tokens": 2, "output_tokens": 1}),
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "message",
    [
        TaskStartedMessage(
            "task_started", {}, "task-secret", "work", "task-1", "sdk-1"
        ),
        HookEventMessage("hook_started", {}, "PreToolUse", "sdk-1", "hook-1"),
        ConversationResetMessage("new-secret", "reset-1", "sdk-1"),
        MirrorErrorMessage("mirror_error", {}, error="mirror-secret"),
        UserMessage("user-secret"),
        SystemMessage("future-secret", {"type": "system"}),
    ],
)
async def test_sdk_session_rejects_non_text_sdk_message_variants(
    tmp_path: Path, message: object
) -> None:
    with pytest.raises(BackendFailure, match="protocol") as error:
        await collect_sdk_response(tmp_path, (message, result_message()))
    assert "secret" not in str(error.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "message",
    [
        live_system("init", session_id="sdk-other-secret"),
        live_system("status", status="idle-secret"),
        live_system("status", subtype="future-secret"),
        live_rate_limit(status="future-secret"),
        live_rate_limit(utilization=1.5),
        live_rate_limit(resets_at=-1),
        live_rate_limit(overage_resets_at=True),
        live_rate_limit(rate_limit_type="future-secret"),
        live_rate_limit(raw=[]),
    ],
)
async def test_sdk_session_rejects_malformed_benign_sdk_metadata(
    tmp_path: Path, message: object
) -> None:
    with pytest.raises(BackendFailure, match="protocol") as error:
        await collect_sdk_response(tmp_path, (message, result_message()))
    assert "secret" not in str(error.value)


def echo_definition(*, name: str = "echo") -> ToolDefinition:
    return ToolDefinition(
        name,
        "Repeat an integer.",
        {
            "type": "object",
            "properties": {"v": {"type": "integer"}},
            "required": ["v"],
            "additionalProperties": False,
        },
    )


def make_tool_session(
    tmp_path: Path,
    messages: tuple[object, ...],
    *,
    tools: tuple[ToolDefinition, ...] = (echo_definition(),),
    start_tool_callbacks: bool = True,
    wait_for_tool_callbacks_before_user: bool = True,
    user_message_barrier: tuple[asyncio.Event, asyncio.Event, asyncio.Event]
    | None = None,
    message_barriers: dict[int, tuple[asyncio.Event, asyncio.Event]] | None = None,
    before_message_actions: dict[int, Callable[[], None]] | None = None,
    system: str = "system",
) -> tuple[SdkSession, FakeSdkClient]:
    client = FakeSdkClient(
        responses=(messages,),
        start_tool_callbacks=start_tool_callbacks,
        wait_for_tool_callbacks_before_user=wait_for_tool_callbacks_before_user,
        user_message_barrier=user_message_barrier,
        message_barriers=message_barriers,
        before_message_actions=before_message_actions,
    )
    session = SdkSession(
        "sonnet",
        system,
        directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
        client_factory=lambda options: client.capture_options(options),
        tools=tools,
        dialect="anthropic",
    )
    return session, client


async def next_boundary(iterator: object) -> list[object]:
    events: list[object] = []
    while True:
        event = await anext(iterator)  # type: ignore[arg-type]
        events.append(event)
        if isinstance(event, Completed):
            return events


@pytest.mark.anyio
async def test_tool_session_exposes_only_generated_caller_tools(
    tmp_path: Path,
) -> None:
    caller_system = "caller\x00system\nverbatim"
    session, client = make_tool_session(
        tmp_path, sdk_response("done", "sdk-1"), system=caller_system
    )

    await session.start()
    try:
        assert client.options is not None
        assert set(client.options.mcp_servers) == {"caller_tools_v1"}
        assert client.options.allowed_tools == ["mcp__caller_tools_v1__echo"]
        assert client.options.tools == []
        assert client.options.skills == []
        assert client.options.setting_sources == []
        assert client.options.agents == {}
        assert client.options.plugins == []
        assert client.options.thinking == {"type": "disabled"}
        assert client.options.strict_mcp_config is True
        assert client.options.permission_mode == "dontAsk"
        assert client.options.max_buffer_size == 8 * 1024 * 1024
        assert client.options.system_prompt == caller_system
        assert client.options.cwd == tmp_path
        assert client.options.include_partial_messages is True
        assert client.options.settings is None
        assert client.options.env == {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
        assert client.options.extra_args == {
            "restricted": None,
            "disable-slash-commands": None,
            "no-session-persistence": None,
        }
    finally:
        await session.close()


@pytest.mark.anyio
async def test_text_session_keeps_empty_tool_configuration_and_large_buffer(
    tmp_path: Path,
) -> None:
    client = FakeSdkClient(responses=(sdk_response("done", "sdk-1"),))
    session = SdkSession(
        "sonnet",
        "system",
        directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
        client_factory=lambda options: client.capture_options(options),
    )
    await session.start()
    try:
        assert client.options is not None
        assert client.options.mcp_servers == {}
        assert client.options.allowed_tools == []
        assert client.options.max_buffer_size == 8 * 1024 * 1024
    finally:
        await session.close()


@pytest.mark.anyio
async def test_stream_generation_emits_one_native_tool_boundary_then_final_text(
    tmp_path: Path,
) -> None:
    messages = (
        *raw_tool_events(
            (("sdk-tool-1", "mcp__caller_tools_v1__echo", '{"v":1}'),),
            "sdk-1",
            input_tokens=3,
            output_tokens=2,
        ),
        live_system("status"),
        UserMessage(
            [
                SdkToolResultBlock(
                    "sdk-tool-1",
                    [{"type": "text", "text": "one"}],
                    None,
                )
            ],
            tool_use_result=[{"type": "text", "text": "one"}],  # type: ignore[arg-type]
        ),
        *raw_text_events("done", "sdk-1", input_tokens=7, output_tokens=4),
        result_message(usage={"input_tokens": 91, "output_tokens": 37}),
    )
    session, client = make_tool_session(tmp_path, messages)
    await session.start()
    generation = session.stream_generation("caller-final-text")
    try:
        boundary = await next_boundary(generation)
        assert boundary[0] == InputUsage(3)
        assert isinstance(boundary[1], ToolCall)
        call = boundary[1]
        assert call.name == "echo"
        assert dict(call.arguments) == {"v": 1}
        assert not call.id.startswith("sdk-tool")
        assert boundary[2] == Completed(
            "tool_use", {"input_tokens": 3, "output_tokens": 2}
        )

        await session.submit_tool_results((ToolResultBlock(call.id, ("one",), False),))
        final = await next_boundary(generation)
        assert final == [
            InputUsage(7),
            TextDelta("done"),
            Completed("end_turn", {"input_tokens": 7, "output_tokens": 4}),
        ]
        with pytest.raises(StopAsyncIteration):
            await anext(generation)
        assert client.prompts == ["caller-final-text"]
        assert len(client.tool_results) == 1
        assert [block.text for block in client.tool_results[0].content] == ["one"]
    finally:
        await session.close()


@pytest.mark.anyio
async def test_tool_error_echo_accepts_live_rate_limit_message_before_echo(
    tmp_path: Path,
) -> None:
    messages = (
        *raw_tool_events(
            (("sdk-tool-1", "mcp__caller_tools_v1__echo", '{"v":1}'),),
            "sdk-1",
        ),
        live_rate_limit(),
        UserMessage(
            [SdkToolResultBlock("sdk-tool-1", "expected", True)],
            tool_use_result="Error: expected",
        ),
        *raw_text_events("done", "sdk-1"),
        result_message(),
    )
    session, _ = make_tool_session(tmp_path, messages)
    await session.start()
    generation = session.stream_generation("go")
    try:
        boundary = await next_boundary(generation)
        call = next(event for event in boundary if isinstance(event, ToolCall))
        await asyncio.sleep(0)
        await session.submit_tool_results(
            (ToolResultBlock(call.id, ("expected",), True),)
        )
        assert (await next_boundary(generation))[-1] == Completed(
            "end_turn", {"input_tokens": 2, "output_tokens": 1}
        )
    finally:
        await session.close()


@pytest.mark.anyio
async def test_parallel_native_results_may_arrive_in_separate_user_messages(
    tmp_path: Path,
) -> None:
    messages = (
        *raw_tool_events(
            (
                ("sdk-a", "mcp__caller_tools_v1__echo", '{"v":1}'),
                ("sdk-b", "mcp__caller_tools_v1__echo", '{"v":1}'),
            ),
            "sdk-1",
        ),
        UserMessage([SdkToolResultBlock("sdk-b", "right", True)]),
        UserMessage(
            [SdkToolResultBlock("sdk-a", [{"type": "text", "text": "left"}], False)]
        ),
        *raw_text_events("done", "sdk-1", input_tokens=8, output_tokens=1),
        result_message(usage={"input_tokens": 101, "output_tokens": 44}),
    )
    session, _ = make_tool_session(tmp_path, messages)
    await session.start()
    generation = session.stream_generation("go")
    try:
        boundary = await next_boundary(generation)
        calls = [event for event in boundary if isinstance(event, ToolCall)]
        assert len(calls) == 2
        await session.submit_tool_results(
            (
                ToolResultBlock(calls[1].id, ("right",), True),
                ToolResultBlock(calls[0].id, ("left",), False),
            )
        )
        assert (await next_boundary(generation))[-1] == Completed(
            "end_turn", {"input_tokens": 8, "output_tokens": 1}
        )
    finally:
        await session.close()


@pytest.mark.anyio
async def test_parallel_native_result_echoes_must_match_exact_internal_ids(
    tmp_path: Path,
) -> None:
    messages = (
        *raw_tool_events(
            (
                ("sdk-a", "mcp__caller_tools_v1__echo", '{"v":1}'),
                ("sdk-b", "mcp__caller_tools_v1__echo", '{"v":1}'),
            ),
            "sdk-1",
        ),
        UserMessage(
            [
                SdkToolResultBlock("sdk-a", "right", False),
                SdkToolResultBlock("sdk-b", "left", False),
            ]
        ),
        *raw_text_events("done", "sdk-1"),
        result_message(),
    )
    session, _ = make_tool_session(tmp_path, messages)
    await session.start()
    generation = session.stream_generation("go")
    try:
        boundary = await next_boundary(generation)
        calls = [event for event in boundary if isinstance(event, ToolCall)]
        await session.submit_tool_results(
            (
                ToolResultBlock(calls[0].id, ("left",), False),
                ToolResultBlock(calls[1].id, ("right",), False),
            )
        )
        with pytest.raises(BackendFailure, match="protocol"):
            _ = [event async for event in generation]
    finally:
        await session.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "raw_result",
    [
        "one",
        [],
        [
            {"type": "text", "text": "one"},
            {"type": "text", "text": ""},
        ],
        [{"type": "text"}],
        [{"text": "one"}],
        [{"type": "image", "text": "one"}],
        [{"type": "text", "text": "one", "extra": "secret"}],
        [{"type": "text", "text": "changed"}],
    ],
)
async def test_success_result_echo_requires_exact_observed_tool_use_result_envelope(
    tmp_path: Path, raw_result: object
) -> None:
    messages = (
        *raw_tool_events(
            (("sdk-tool-1", "mcp__caller_tools_v1__echo", '{"v":1}'),),
            "sdk-1",
        ),
        UserMessage(
            [SdkToolResultBlock("sdk-tool-1", "one", False)],
            tool_use_result=raw_result,  # type: ignore[arg-type]
        ),
        *raw_text_events("done", "sdk-1"),
        result_message(),
    )
    session, _ = make_tool_session(tmp_path, messages)
    await session.start()
    generation = session.stream_generation("go")
    try:
        boundary = await next_boundary(generation)
        call = next(event for event in boundary if isinstance(event, ToolCall))
        await session.submit_tool_results((ToolResultBlock(call.id, ("one",), False),))
        with pytest.raises(BackendFailure, match="protocol") as error:
            _ = [event async for event in generation]
        assert "secret" not in str(error.value)
        assert "changed" not in str(error.value)
    finally:
        await session.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "echo",
    [
        UserMessage([SdkToolResultBlock("sdk-other", "one", False)]),
        UserMessage(
            [
                SdkToolResultBlock("sdk-tool-1", "one", False),
                SdkToolResultBlock("sdk-tool-1", "one", False),
            ]
        ),
        UserMessage([SdkToolResultBlock("sdk-tool-1", "changed", False)]),
        UserMessage([SdkToolResultBlock("sdk-tool-1", "one", True)]),
        UserMessage([SdkToolResultBlock("sdk-tool-1", None, False)]),
        UserMessage([SdkToolResultBlock("sdk-tool-1", [{"type": "image"}], False)]),
        UserMessage(
            [SdkToolResultBlock("sdk-tool-1", "one", False)],
            parent_tool_use_id="parent-secret",
        ),
        UserMessage(
            [SdkToolResultBlock("sdk-tool-1", "one", False)],
            origin={"kind": "peer"},
        ),
        UserMessage(
            [SdkToolResultBlock("sdk-tool-1", "one", False)],
            tool_use_result=[  # type: ignore[arg-type]
                {"type": "text", "text": "changed"}
            ],
        ),
        UserMessage(
            [SdkToolResultBlock("sdk-tool-1", "one", True)],
            tool_use_result="Error: changed",
        ),
    ],
)
async def test_sdk_session_rejects_malformed_or_mismatched_native_result_echo(
    tmp_path: Path, echo: UserMessage
) -> None:
    messages = (
        *raw_tool_events(
            (("sdk-tool-1", "mcp__caller_tools_v1__echo", '{"v":1}'),),
            "sdk-1",
        ),
        echo,
        *raw_text_events("done", "sdk-1"),
        result_message(),
    )
    session, _ = make_tool_session(tmp_path, messages)
    await session.start()
    generation = session.stream_generation("go")
    try:
        boundary = await next_boundary(generation)
        call = next(event for event in boundary if isinstance(event, ToolCall))
        await session.submit_tool_results((ToolResultBlock(call.id, ("one",), False),))
        with pytest.raises(BackendFailure, match="protocol") as error:
            _ = [event async for event in generation]
        assert "secret" not in str(error.value)
    finally:
        await session.close()


@pytest.mark.anyio
async def test_sdk_session_rejects_user_message_outside_result_echo_phase(
    tmp_path: Path,
) -> None:
    session, _ = make_tool_session(
        tmp_path,
        (UserMessage([SdkToolResultBlock("sdk-tool", "secret", False)]),),
    )
    await session.start()
    try:
        with pytest.raises(BackendFailure, match="protocol") as error:
            _ = [event async for event in session.stream_generation("go")]
        assert "secret" not in str(error.value)
    finally:
        await session.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "sdk_name",
    ["Bash", "echo", "mcp__other__echo", "mcp__caller_tools_v1__unknown"],
)
async def test_sdk_session_rejects_non_generated_or_unknown_raw_tool_names(
    tmp_path: Path, sdk_name: str
) -> None:
    messages = raw_tool_events((("sdk-tool-1", sdk_name, '{"v":1}'),), "sdk-1")
    session, _ = make_tool_session(tmp_path, messages)
    await session.start()
    try:
        with pytest.raises(BackendFailure, match="protocol"):
            _ = [event async for event in session.stream_generation("go")]
    finally:
        await session.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "partial_json",
    ["{bad-json", json.dumps({"v": "x" * (256 * 1024 + 1)})],
)
async def test_sdk_session_rejects_malformed_or_oversized_raw_arguments(
    tmp_path: Path, partial_json: str
) -> None:
    raw = list(
        raw_tool_events(
            (("sdk-tool-1", "mcp__caller_tools_v1__echo", '{"v":1}'),),
            "sdk-1",
        )
    )
    delta = raw[2]
    assert isinstance(delta, StreamEvent)
    delta.event["delta"]["partial_json"] = partial_json
    session, _ = make_tool_session(tmp_path, tuple(raw))
    await session.start()
    try:
        with pytest.raises(BackendFailure, match="protocol") as error:
            _ = [event async for event in session.stream_generation("go")]
        assert "bad-json" not in str(error.value)
    finally:
        await session.close()


@pytest.mark.anyio
async def test_sdk_session_rejects_raw_and_typed_tool_call_mismatch(
    tmp_path: Path,
) -> None:
    messages = list(
        raw_tool_events(
            (("sdk-tool-1", "mcp__caller_tools_v1__echo", '{"v":1}'),),
            "sdk-1",
        )
    )
    assistant = messages[4]
    assert isinstance(assistant, AssistantMessage)
    assistant.content[0] = ToolUseBlock(
        "sdk-tool-1", "mcp__caller_tools_v1__echo", {"v": 2}
    )
    session, _ = make_tool_session(tmp_path, tuple(messages))
    await session.start()
    try:
        with pytest.raises(BackendFailure, match="protocol"):
            _ = [event async for event in session.stream_generation("go")]
    finally:
        await session.close()


@pytest.mark.anyio
async def test_tool_enabled_system_init_requires_exact_generated_metadata(
    tmp_path: Path,
) -> None:
    expected = live_system(
        "init",
        tools=["mcp__caller_tools_v1__echo"],
        mcp_servers=[{"name": "caller_tools_v1", "status": "connected"}],
    )
    messages = (expected, *raw_text_events("done", "sdk-1"), result_message())
    session, _ = make_tool_session(tmp_path, messages)
    await session.start()
    try:
        assert (await next_boundary(session.stream_generation("go")))[-1] == Completed(
            "end_turn", {"input_tokens": 2, "output_tokens": 1}
        )
    finally:
        await session.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tools", "servers"),
    [
        (
            ["mcp__caller_tools_v1__echo", "Bash"],
            [{"name": "caller_tools_v1", "status": "connected"}],
        ),
        (
            ["mcp__caller_tools_v1__echo"] * 2,
            [{"name": "caller_tools_v1", "status": "connected"}],
        ),
        (
            ["mcp__caller_tools_v1__echo"],
            [
                {"name": "caller_tools_v1", "status": "connected"},
                {"name": "ambient", "status": "connected"},
            ],
        ),
        (
            ["mcp__caller_tools_v1__echo"],
            [{"name": "caller_tools_v1", "status": "failed"}],
        ),
        (
            ["mcp__other__echo"],
            [{"name": "caller_tools_v1", "status": "connected"}],
        ),
    ],
)
async def test_tool_enabled_system_init_rejects_every_metadata_expansion(
    tmp_path: Path, tools: list[str], servers: list[dict[str, str]]
) -> None:
    message = live_system("init", tools=tools, mcp_servers=servers)
    session, _ = make_tool_session(tmp_path, (message,))
    await session.start()
    try:
        with pytest.raises(BackendFailure, match="protocol"):
            _ = [event async for event in session.stream_generation("go")]
    finally:
        await session.close()


@pytest.mark.anyio
async def test_tool_boundary_preserves_text_before_publication_order(
    tmp_path: Path,
) -> None:
    messages = (
        *raw_tool_events(
            (
                ("sdk-a", "mcp__caller_tools_v1__echo", '{"v":1}'),
                ("sdk-b", "mcp__caller_tools_v1__echo", '{"v":2}'),
            ),
            "sdk-1",
            text="calling: ",
        ),
        UserMessage(
            [
                SdkToolResultBlock("sdk-a", "one", False),
                SdkToolResultBlock("sdk-b", "two", False),
            ]
        ),
        *raw_text_events("done", "sdk-1"),
        result_message(usage={"input_tokens": 30, "output_tokens": 20}),
    )
    session, _ = make_tool_session(tmp_path, messages)
    await session.start()
    generation = session.stream_generation("go")
    try:
        boundary = await next_boundary(generation)
        assert boundary[0:2] == [InputUsage(3), TextDelta("calling: ")]
        calls = [event for event in boundary if isinstance(event, ToolCall)]
        assert [dict(call.arguments) for call in calls] == [{"v": 1}, {"v": 2}]
        await session.submit_tool_results(
            (
                ToolResultBlock(calls[0].id, ("one",), False),
                ToolResultBlock(calls[1].id, ("two",), False),
            )
        )
        assert (await next_boundary(generation))[-1].stop_reason == "end_turn"  # type: ignore[union-attr]
    finally:
        await session.close()


@pytest.mark.anyio
async def test_stream_generation_supports_two_native_tool_rounds(
    tmp_path: Path,
) -> None:
    messages = (
        *raw_tool_events(
            (("sdk-a", "mcp__caller_tools_v1__echo", '{"v":1}'),), "sdk-1"
        ),
        UserMessage([SdkToolResultBlock("sdk-a", "first", False)]),
        *raw_tool_events(
            (("sdk-b", "mcp__caller_tools_v1__echo", '{"v":2}'),),
            "sdk-1",
            input_tokens=6,
            output_tokens=3,
        ),
        UserMessage([SdkToolResultBlock("sdk-b", "second", False)]),
        *raw_text_events("done", "sdk-1", input_tokens=9, output_tokens=4),
        result_message(usage={"input_tokens": 200, "output_tokens": 100}),
    )
    session, _ = make_tool_session(tmp_path, messages)
    await session.start()
    bridge = session._bridge
    assert bridge is not None
    generation = session.stream_generation("go")
    try:
        first = await next_boundary(generation)
        first_call = next(event for event in first if isinstance(event, ToolCall))
        first_arguments = first_call.arguments
        await session.submit_tool_results(
            (ToolResultBlock(first_call.id, ("first",), False),)
        )
        second = await next_boundary(generation)
        second_call = next(event for event in second if isinstance(event, ToolCall))
        assert second_call.id != first_call.id
        assert all(
            invocation.arguments is not first_arguments
            for invocation in bridge._epoch_invocations
        )
        assert all(
            pending.invocation.arguments is not first_arguments
            for pending in bridge._pending.values()
        )
        await session.submit_tool_results(
            (ToolResultBlock(second_call.id, ("second",), False),)
        )
        assert await next_boundary(generation) == [
            InputUsage(9),
            TextDelta("done"),
            Completed("end_turn", {"input_tokens": 9, "output_tokens": 4}),
        ]
        publication_queue = getattr(bridge, "_publications", None)
        assert publication_queue is None, (
            f"bridge retained {publication_queue.qsize()} completed invocations"
        )
        assert bridge._pending == {}
        assert bridge._pending_by_internal == {}
        assert bridge._epoch_invocations == []
        assert bridge._expected_by_internal == {}
        assert bridge._callbacks_seen == set()
    finally:
        await session.close()

    assert bridge._pending == {}
    assert bridge._pending_by_internal == {}
    assert bridge._epoch_invocations == []
    assert bridge._expected_by_internal == {}
    assert bridge._callbacks_seen == set()
    assert bridge._issued_ids == set()


@pytest.mark.anyio
async def test_sdk_session_rejects_missing_parallel_native_result_echo(
    tmp_path: Path,
) -> None:
    messages = (
        *raw_tool_events(
            (
                ("sdk-a", "mcp__caller_tools_v1__echo", '{"v":1}'),
                ("sdk-b", "mcp__caller_tools_v1__echo", '{"v":2}'),
            ),
            "sdk-1",
        ),
        UserMessage([SdkToolResultBlock("sdk-a", "one", False)]),
        *raw_text_events("done", "sdk-1"),
        result_message(),
    )
    session, _ = make_tool_session(tmp_path, messages)
    await session.start()
    generation = session.stream_generation("go")
    try:
        boundary = await next_boundary(generation)
        calls = [event for event in boundary if isinstance(event, ToolCall)]
        await session.submit_tool_results(
            (
                ToolResultBlock(calls[0].id, ("one",), False),
                ToolResultBlock(calls[1].id, ("two",), False),
            )
        )
        with pytest.raises(BackendFailure, match="protocol"):
            _ = [event async for event in generation]
    finally:
        await session.close()


@pytest.mark.anyio
async def test_sdk_session_rejects_raw_tool_block_followed_by_text(
    tmp_path: Path,
) -> None:
    messages = list(
        raw_tool_events((("sdk-a", "mcp__caller_tools_v1__echo", '{"v":1}'),), "sdk-1")
    )
    messages[4:4] = [
        StreamEvent(
            "late-text-start",
            "sdk-1",
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        StreamEvent(
            "late-text-delta",
            "sdk-1",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "secret"},
            },
        ),
        StreamEvent(
            "late-text-stop",
            "sdk-1",
            {"type": "content_block_stop", "index": 1},
        ),
    ]
    session, _ = make_tool_session(tmp_path, tuple(messages))
    await session.start()
    try:
        with pytest.raises(BackendFailure, match="protocol") as error:
            _ = [event async for event in session.stream_generation("go")]
        assert "secret" not in str(error.value)
    finally:
        await session.close()


@pytest.mark.anyio
async def test_sdk_session_rejects_parent_attributed_raw_tool_block(
    tmp_path: Path,
) -> None:
    messages = list(
        raw_tool_events((("sdk-a", "mcp__caller_tools_v1__echo", '{"v":1}'),), "sdk-1")
    )
    raw_start = messages[1]
    assert isinstance(raw_start, StreamEvent)
    raw_start.parent_tool_use_id = "parent-secret"
    session, _ = make_tool_session(tmp_path, tuple(messages))
    await session.start()
    try:
        with pytest.raises(BackendFailure, match="protocol") as error:
            _ = [event async for event in session.stream_generation("go")]
        assert "secret" not in str(error.value)
    finally:
        await session.close()


@pytest.mark.anyio
async def test_sdk_session_rejects_handler_publication_without_matching_raw_call(
    tmp_path: Path,
) -> None:
    raw = raw_text_events("answer", "sdk-1")
    injected = AssistantMessage(
        [ToolUseBlock("sdk-a", "mcp__caller_tools_v1__echo", {"v": 1})],
        "sonnet",
        session_id="sdk-1",
    )
    messages = (*raw[:3], injected, *raw[3:], result_message())
    session, _ = make_tool_session(tmp_path, messages)
    await session.start()
    try:
        with pytest.raises(BackendFailure, match="protocol"):
            _ = [event async for event in session.stream_generation("go")]
    finally:
        await session.close()


@pytest.mark.anyio
async def test_text_compatibility_wrapper_fails_closed_on_native_tool_boundary(
    tmp_path: Path,
) -> None:
    messages = raw_tool_events(
        (("sdk-a", "mcp__caller_tools_v1__echo", '{"v":1}'),), "sdk-1"
    )
    session, _ = make_tool_session(tmp_path, messages)
    await session.start()
    try:
        with pytest.raises(BackendFailure, match="protocol"):
            _ = [event async for event in session.stream_turn("go")]
    finally:
        await session.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("callback_name", "callback_arguments"),
    [("echo", {"v": 2}), ("missing", {"v": 1})],
)
async def test_sdk_session_rejects_callback_set_that_does_not_match_raw_calls(
    tmp_path: Path, callback_name: str, callback_arguments: dict[str, object]
) -> None:
    messages = raw_tool_events(
        (("sdk-a", "mcp__caller_tools_v1__echo", '{"v":1}'),), "sdk-1"
    )
    session, client = make_tool_session(tmp_path, messages, start_tool_callbacks=False)
    await session.start()
    client.start_tool_callback(callback_name, callback_arguments, internal_id="sdk-a")
    try:
        with pytest.raises(BackendFailure, match="protocol"):
            _ = [event async for event in session.stream_generation("go")]
    finally:
        await session.close()


@pytest.mark.anyio
async def test_callback_after_seal_cannot_join_the_next_tool_epoch(
    tmp_path: Path,
) -> None:
    messages = (
        *raw_tool_events(
            (("sdk-a", "mcp__caller_tools_v1__echo", '{"v":1}'),), "sdk-1"
        ),
        UserMessage([SdkToolResultBlock("sdk-a", "first", False)]),
        *raw_tool_events(
            (("sdk-b", "mcp__caller_tools_v1__echo", '{"v":2}'),), "sdk-1"
        ),
    )
    session, client = make_tool_session(tmp_path, messages)
    await session.start()
    generation = session.stream_generation("go")
    try:
        first = await next_boundary(generation)
        call = next(event for event in first if isinstance(event, ToolCall))
        client.start_tool_callback(
            "echo", {"v": 99}, internal_id="sdk-late", wait_for_echo=False
        )
        await session.submit_tool_results(
            (ToolResultBlock(call.id, ("first",), False),)
        )
        with pytest.raises(BackendFailure, match="protocol"):
            _ = [event async for event in generation]
    finally:
        await session.close()


def test_raw_tool_validator_rejects_arguments_outside_caller_schema() -> None:
    validator = RawSdkMessageValidator((echo_definition(),))
    messages = raw_tool_events(
        (("sdk-a", "mcp__caller_tools_v1__echo", '{"v":"secret"}'),), "sdk-1"
    )

    with pytest.raises(BackendFailure, match="protocol") as error:
        for message in messages:
            if isinstance(message, StreamEvent):
                validator.observe(message.event)
            else:
                validator.validate_assistant(message)

    assert "secret" not in str(error.value)


def test_raw_tool_validator_accepts_live_direct_caller_attribution() -> None:
    validator = RawSdkMessageValidator((echo_definition(),))
    messages = list(
        raw_tool_events((("sdk-a", "mcp__caller_tools_v1__echo", '{"v":1}'),), "sdk-1")
    )
    start = messages[1]
    assert isinstance(start, StreamEvent)
    start.event["content_block"]["caller"] = {"type": "direct"}

    for message in messages:
        if isinstance(message, StreamEvent):
            validator.observe(message.event)
        else:
            validator.validate_assistant(message)

    assert validator.complete is True
    assert len(validator.tool_calls) == 1


def test_raw_tool_validator_rejects_missing_caller_attribution() -> None:
    validator = RawSdkMessageValidator((echo_definition(),))
    messages = list(
        raw_tool_events((("sdk-a", "mcp__caller_tools_v1__echo", '{"v":1}'),), "sdk-1")
    )
    start = messages[1]
    assert isinstance(start, StreamEvent)
    del start.event["content_block"]["caller"]

    with pytest.raises(BackendFailure, match="protocol"):
        for message in messages:
            if isinstance(message, StreamEvent):
                validator.observe(message.event)
            else:
                validator.validate_assistant(message)


def test_raw_tool_validator_accepts_live_per_block_parallel_typed_messages() -> None:
    sdk_name = "mcp__caller_tools_v1__echo"
    validator = RawSdkMessageValidator((echo_definition(),))
    messages = list(
        raw_tool_events(
            (("sdk-a", sdk_name, '{"v":1}'), ("sdk-b", sdk_name, '{"v":1}')),
            "sdk-1",
        )
    )
    final_typed = next(
        message for message in messages if isinstance(message, AssistantMessage)
    )
    messages.remove(final_typed)
    for internal_id, block_index in reversed((("sdk-a", 0), ("sdk-b", 1))):
        stop = next(
            index
            for index, message in enumerate(messages)
            if isinstance(message, StreamEvent)
            and message.event.get("type") == "content_block_stop"
            and message.event.get("index") == block_index
        )
        messages.insert(
            stop,
            AssistantMessage([ToolUseBlock(internal_id, sdk_name, {"v": 1})], "sonnet"),
        )

    for message in messages:
        if isinstance(message, StreamEvent):
            validator.observe(message.event)
        else:
            validator.validate_assistant(message)

    assert validator.complete is True
    assert len(validator.tool_calls) == 2


@pytest.mark.parametrize(
    "caller",
    ["direct", {"type": "agent"}, {"type": "direct", "extra": "secret"}],
)
def test_raw_tool_validator_rejects_non_direct_caller_attribution(
    caller: object,
) -> None:
    validator = RawSdkMessageValidator((echo_definition(),))
    messages = list(
        raw_tool_events((("sdk-a", "mcp__caller_tools_v1__echo", '{"v":1}'),), "sdk-1")
    )
    start = messages[1]
    assert isinstance(start, StreamEvent)
    start.event["content_block"]["caller"] = caller

    with pytest.raises(BackendFailure, match="protocol") as error:
        for message in messages:
            if isinstance(message, StreamEvent):
                validator.observe(message.event)
            else:
                validator.validate_assistant(message)

    assert "secret" not in str(error.value)


@pytest.mark.anyio
async def test_deferred_handler_that_never_arrives_fails_closed_after_result(
    tmp_path: Path,
) -> None:
    messages = raw_tool_events(
        (("sdk-a", "mcp__caller_tools_v1__echo", '{"v":1}'),), "sdk-1"
    )
    session, _ = make_tool_session(tmp_path, messages, start_tool_callbacks=False)
    await session.start()
    generation = session.stream_generation("go")
    try:
        boundary = await next_boundary(generation)
        call = next(event for event in boundary if isinstance(event, ToolCall))
        await session.submit_tool_results((ToolResultBlock(call.id, ("one",), False),))
        with pytest.raises(BackendFailure, match="without result") as error:
            _ = [event async for event in generation]
        assert "sdk-a" not in str(error.value)
    finally:
        await session.close()


@pytest.mark.anyio
async def test_missing_deferred_callback_rejects_arriving_terminal_boundary(
    tmp_path: Path,
) -> None:
    messages = (
        *raw_tool_events(
            (
                ("sdk-a", "mcp__caller_tools_v1__echo", '{"v":1}'),
                ("sdk-b", "mcp__caller_tools_v1__echo", '{"v":2}'),
            ),
            "sdk-1",
        ),
        UserMessage(
            [
                SdkToolResultBlock("sdk-a", "first", False),
                SdkToolResultBlock("sdk-b", "second", False),
            ]
        ),
        *raw_text_events("must-not-commit", "sdk-1"),
        result_message(),
    )
    session, client = make_tool_session(
        tmp_path, messages, start_tool_callbacks=False
    )
    await session.start()
    client.start_tool_callback("echo", {"v": 1}, internal_id="sdk-a")
    generation = session.stream_generation("go")
    emitted: list[object] = []
    try:
        first = await next_boundary(generation)
        calls = [event for event in first if isinstance(event, ToolCall)]
        await session.submit_tool_results(
            (
                ToolResultBlock(calls[0].id, ("first",), False),
                ToolResultBlock(calls[1].id, ("second",), False),
            )
        )
        with pytest.raises(BackendFailure, match="protocol") as error:
            async for event in generation:
                emitted.append(event)
        assert "sdk-b" not in str(error.value)
        assert "must-not-commit" not in str(error.value)
        assert TextDelta("must-not-commit") not in emitted
        assert not any(
            isinstance(event, Completed) and event.stop_reason == "end_turn"
            for event in emitted
        )
    finally:
        await session.close()


@pytest.mark.anyio
async def test_serial_deferred_callback_delivers_stored_result_before_final_text(
    tmp_path: Path,
) -> None:
    messages = (
        *raw_tool_events(
            (
                ("sdk-a", "mcp__caller_tools_v1__echo", '{"v":1}'),
                ("sdk-b", "mcp__caller_tools_v1__echo", '{"v":2}'),
            ),
            "sdk-1",
        ),
        UserMessage(
            [
                SdkToolResultBlock("sdk-a", "first", False),
                SdkToolResultBlock("sdk-b", "second", False),
            ]
        ),
        *raw_text_events("done", "sdk-1"),
        result_message(),
    )
    session, client = make_tool_session(
        tmp_path, messages, start_tool_callbacks=False
    )
    await session.start()
    client.start_tool_callback("echo", {"v": 1}, internal_id="sdk-a")
    generation = session.stream_generation("go")
    try:
        first = await next_boundary(generation)
        calls = [event for event in first if isinstance(event, ToolCall)]
        await session.submit_tool_results(
            (
                ToolResultBlock(calls[1].id, ("second",), False),
                ToolResultBlock(calls[0].id, ("first",), False),
            )
        )
        client.start_tool_callback("echo", {"v": 2}, internal_id="sdk-b")
        assert await next_boundary(generation) == [
            InputUsage(2),
            TextDelta("done"),
            Completed("end_turn", {"input_tokens": 2, "output_tokens": 1}),
        ]
        assert len(client.tool_results) == 2
        assert [block.text for block in client.tool_results[1].content] == ["second"]
    finally:
        await session.close()


@pytest.mark.anyio
async def test_same_loop_incoming_first_stays_fatal_after_exact_callback_completes(
    tmp_path: Path,
) -> None:
    raw_tools = raw_tool_events(
        (
            ("sdk-a", "mcp__caller_tools_v1__echo", '{"v":1}'),
            ("sdk-b", "mcp__caller_tools_v1__echo", '{"v":2}'),
        ),
        "sdk-1",
    )
    release_callback = asyncio.Event()
    callback_completed = asyncio.Event()
    messages = (
        *raw_tools,
        UserMessage(
            [
                SdkToolResultBlock("sdk-a", "first", False),
                SdkToolResultBlock("sdk-b", "second", False),
            ]
        ),
        *raw_text_events("must-not-commit", "sdk-1"),
        result_message(),
    )
    next_item_index = len(raw_tools) + 1
    session, client = make_tool_session(
        tmp_path,
        messages,
        start_tool_callbacks=False,
        wait_for_tool_callbacks_before_user=False,
        before_message_actions={next_item_index: release_callback.set},
    )
    await session.start()
    client.start_tool_callback("echo", {"v": 1}, internal_id="sdk-a")
    client.start_tool_callback(
        "echo",
        {"v": 2},
        internal_id="sdk-b",
        entry_barrier=release_callback,
        completed=callback_completed,
    )
    generation = session.stream_generation("go")
    emitted: list[object] = []
    try:
        first = await next_boundary(generation)
        calls = [event for event in first if isinstance(event, ToolCall)]
        await session.submit_tool_results(
            (
                ToolResultBlock(calls[0].id, ("first",), False),
                ToolResultBlock(calls[1].id, ("second",), False),
            )
        )
        with pytest.raises(BackendFailure, match="protocol") as error:
            async for event in generation:
                emitted.append(event)
        assert release_callback.is_set()
        assert callback_completed.is_set()
        assert "sdk-b" not in str(error.value)
        assert "must-not-commit" not in str(error.value)
        assert TextDelta("must-not-commit") not in emitted
    finally:
        await session.close()


@pytest.mark.anyio
async def test_same_loop_callback_first_accepts_immediately_following_item(
    tmp_path: Path,
) -> None:
    raw_tools = raw_tool_events(
        (
            ("sdk-a", "mcp__caller_tools_v1__echo", '{"v":1}'),
            ("sdk-b", "mcp__caller_tools_v1__echo", '{"v":2}'),
        ),
        "sdk-1",
    )
    next_item_entered = asyncio.Event()
    release_next_item = asyncio.Event()
    release_callback = asyncio.Event()
    callback_completed = asyncio.Event()
    messages = (
        *raw_tools,
        UserMessage(
            [
                SdkToolResultBlock("sdk-a", "first", False),
                SdkToolResultBlock("sdk-b", "second", False),
            ]
        ),
        *raw_text_events("done", "sdk-1"),
        result_message(),
    )
    next_item_index = len(raw_tools) + 1
    session, client = make_tool_session(
        tmp_path,
        messages,
        start_tool_callbacks=False,
        wait_for_tool_callbacks_before_user=False,
        message_barriers={
            next_item_index: (next_item_entered, release_next_item)
        },
    )
    await session.start()
    client.start_tool_callback("echo", {"v": 1}, internal_id="sdk-a")
    client.start_tool_callback(
        "echo",
        {"v": 2},
        internal_id="sdk-b",
        entry_barrier=release_callback,
        completed=callback_completed,
    )
    generation = session.stream_generation("go")
    try:
        first = await next_boundary(generation)
        calls = [event for event in first if isinstance(event, ToolCall)]
        await session.submit_tool_results(
            (
                ToolResultBlock(calls[0].id, ("first",), False),
                ToolResultBlock(calls[1].id, ("second",), False),
            )
        )
        final_task = asyncio.create_task(next_boundary(generation))
        await next_item_entered.wait()
        release_callback.set()
        await callback_completed.wait()
        release_next_item.set()
        assert await final_task == [
            InputUsage(2),
            TextDelta("done"),
            Completed("end_turn", {"input_tokens": 2, "output_tokens": 1}),
        ]
    finally:
        await session.close()


@pytest.mark.anyio
async def test_user_message_while_awaiting_submit_fails_closed(
    tmp_path: Path,
) -> None:
    messages = (
        *raw_tool_events(
            (("sdk-a", "mcp__caller_tools_v1__echo", '{"v":1}'),), "sdk-1"
        ),
        UserMessage([SdkToolResultBlock("sdk-a", "secret", False)]),
    )
    session, _ = make_tool_session(
        tmp_path,
        messages,
        wait_for_tool_callbacks_before_user=False,
    )
    await session.start()
    try:
        with pytest.raises(BackendFailure, match="protocol") as error:
            _ = [event async for event in session.stream_generation("go")]
        assert "secret" not in str(error.value)
    finally:
        await session.close()


@pytest.mark.anyio
async def test_user_message_after_complete_result_echo_fails_closed(
    tmp_path: Path,
) -> None:
    messages = (
        *raw_tool_events(
            (("sdk-a", "mcp__caller_tools_v1__echo", '{"v":1}'),), "sdk-1"
        ),
        UserMessage([SdkToolResultBlock("sdk-a", "one", False)]),
        UserMessage([SdkToolResultBlock("sdk-a", "secret-extra", False)]),
    )
    session, _ = make_tool_session(tmp_path, messages)
    await session.start()
    generation = session.stream_generation("go")
    try:
        boundary = await next_boundary(generation)
        call = next(event for event in boundary if isinstance(event, ToolCall))
        await session.submit_tool_results((ToolResultBlock(call.id, ("one",), False),))
        with pytest.raises(BackendFailure, match="protocol") as error:
            _ = [event async for event in generation]
        assert "secret-extra" not in str(error.value)
    finally:
        await session.close()


@pytest.mark.anyio
async def test_prefetched_user_message_keeps_awaiting_submit_phase(
    tmp_path: Path,
) -> None:
    reached = asyncio.Event()
    release = asyncio.Event()
    delivered = asyncio.Event()
    messages = (
        *raw_tool_events(
            (("sdk-a", "mcp__caller_tools_v1__echo", '{"v":1}'),), "sdk-1"
        ),
        UserMessage([SdkToolResultBlock("sdk-a", "one", False)]),
        *raw_text_events("done", "sdk-1"),
        result_message(),
    )
    session, _ = make_tool_session(
        tmp_path,
        messages,
        wait_for_tool_callbacks_before_user=False,
        user_message_barrier=(reached, release, delivered),
    )
    await session.start()
    generation = session.stream_generation("go")
    try:
        boundary = await next_boundary(generation)
        call = next(event for event in boundary if isinstance(event, ToolCall))
        await reached.wait()
        release.set()
        await delivered.wait()
        await session.submit_tool_results((ToolResultBlock(call.id, ("one",), False),))

        with pytest.raises(BackendFailure, match="protocol") as error:
            _ = [event async for event in generation]
        assert "sdk-a" not in str(error.value)
        assert "one" not in str(error.value)
    finally:
        await session.close()


@pytest.mark.anyio
async def test_prefetched_user_message_received_after_submit_is_accepted(
    tmp_path: Path,
) -> None:
    reached = asyncio.Event()
    release = asyncio.Event()
    delivered = asyncio.Event()
    messages = (
        *raw_tool_events(
            (("sdk-a", "mcp__caller_tools_v1__echo", '{"v":1}'),), "sdk-1"
        ),
        UserMessage([SdkToolResultBlock("sdk-a", "one", False)]),
        *raw_text_events("done", "sdk-1"),
        result_message(),
    )
    session, _ = make_tool_session(
        tmp_path,
        messages,
        wait_for_tool_callbacks_before_user=False,
        user_message_barrier=(reached, release, delivered),
    )
    await session.start()
    generation = session.stream_generation("go")
    try:
        boundary = await next_boundary(generation)
        call = next(event for event in boundary if isinstance(event, ToolCall))
        await reached.wait()
        await session.submit_tool_results((ToolResultBlock(call.id, ("one",), False),))
        release.set()
        await delivered.wait()

        assert await next_boundary(generation) == [
            InputUsage(2),
            TextDelta("done"),
            Completed("end_turn", {"input_tokens": 2, "output_tokens": 1}),
        ]
    finally:
        await session.close()
