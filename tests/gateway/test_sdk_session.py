from __future__ import annotations

from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    StreamEvent,
    ToolResultBlock,
    ToolUseBlock,
)

from claude_sdk_proxy.domain import Completed, TextDelta, UnsupportedFeature
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
    with pytest.raises(UnsupportedFeature, match="tools"):
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
    with pytest.raises(UnsupportedFeature, match="tools"):
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
    with pytest.raises(UnsupportedFeature, match="tools"):
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
