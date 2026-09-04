from __future__ import annotations

from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, ToolUseBlock

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
