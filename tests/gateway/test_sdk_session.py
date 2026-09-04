from __future__ import annotations

from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    DeferredToolUse,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from claude_sdk_proxy.domain import BackendFailure, Completed, TextDelta
from claude_sdk_proxy.sdk_session import SdkSession
from tests.gateway.fakes import FakeSdkClient, FixedTemporaryDirectory, sdk_response


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

    assert first == [TextDelta("one"), Completed("end_turn", {"output_tokens": 1})]
    assert second == [TextDelta("two"), Completed("end_turn", {"output_tokens": 1})]
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
        ToolResultBlock(tool_use_id="tool-1"),
        ServerToolResultBlock(tool_use_id="tool-1", content={}),
    ),
)
async def test_sdk_session_rejects_complete_tool_blocks_before_completed(
    tmp_path: Path,
    block: ToolUseBlock
    | ServerToolUseBlock
    | ToolResultBlock
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
@pytest.mark.parametrize("is_error", [None, 0, ""])
async def test_sdk_session_requires_exact_false_success_flag(
    tmp_path: Path, is_error: object
) -> None:
    with pytest.raises(BackendFailure, match="protocol") as error:
        await collect_sdk_response(
            tmp_path, (text_event(), result_message(is_error=is_error))
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
            tmp_path, (text_event(), result_message(usage=usage))
        )
    assert "secret-usage" not in str(error.value)


@pytest.mark.anyio
@pytest.mark.parametrize("stop_reason", [None, "tool_use", 1])
async def test_sdk_session_rejects_non_text_stop_reasons(
    tmp_path: Path, stop_reason: object
) -> None:
    with pytest.raises(BackendFailure, match="protocol") as error:
        await collect_sdk_response(
            tmp_path, (text_event(), result_message(stop_reason=stop_reason))
        )
    assert "tool_use" not in str(error.value)


@pytest.mark.anyio
async def test_sdk_session_rejects_deferred_tool_result_state(tmp_path: Path) -> None:
    deferred = DeferredToolUse("tool-secret", "Bash", {"command": "secret"})
    with pytest.raises(BackendFailure, match="protocol") as error:
        await collect_sdk_response(
            tmp_path,
            (text_event(), result_message(deferred_tool_use=deferred)),
        )
    assert "tool-secret" not in str(error.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "messages",
    [
        (text_event(parent_tool_use_id="tool-secret"), result_message()),
        (text_event(), result_message(session_id="sdk-other-secret")),
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
                text_event(),
                result_message(origin={"kind": "task-notification"}),
            ),
        )


@pytest.mark.anyio
async def test_sdk_session_rejects_non_success_result_subtype(tmp_path: Path) -> None:
    with pytest.raises(BackendFailure, match="protocol"):
        await collect_sdk_response(
            tmp_path, (text_event(), result_message(subtype="secret-subtype"))
        )


@pytest.mark.anyio
async def test_sdk_session_normalizes_only_gateway_usage_counters(
    tmp_path: Path,
) -> None:
    events = await collect_sdk_response(
        tmp_path,
        (
            text_event(),
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
        TextDelta("answer"),
        Completed("end_turn", {"input_tokens": 2, "output_tokens": 1}),
    ]


@pytest.mark.anyio
async def test_sdk_session_emits_message_start_input_usage_before_text(
    tmp_path: Path,
) -> None:
    from claude_sdk_proxy.domain import InputUsage

    start = StreamEvent(
        uuid="event-start",
        session_id="sdk-1",
        event={
            "type": "message_start",
            "message": {"usage": {"input_tokens": 7, "output_tokens": 0}},
        },
    )
    events = await collect_sdk_response(
        tmp_path,
        (
            start,
            text_event(),
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
            tmp_path, (start, text_event(), result_message())
        )
    assert "secret-usage" not in str(error.value)


@pytest.mark.anyio
async def test_sdk_session_rejects_start_and_result_input_usage_mismatch(
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
    with pytest.raises(BackendFailure, match="protocol"):
        await collect_sdk_response(
            tmp_path,
            (start, text_event(), result_message(usage={"input_tokens": 8})),
        )


@pytest.mark.anyio
async def test_sdk_session_requires_same_identity_across_turns(tmp_path: Path) -> None:
    client = FakeSdkClient(
        responses=(
            (text_event(), result_message()),
            (
                text_event(session_id="sdk-other"),
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
