from __future__ import annotations

import json
from pathlib import Path

import pytest
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ToolResultBlock,
    UserMessage,
)


@pytest.mark.anyio
async def test_real_sdk_transport_accepts_escape_expanded_one_mib_result() -> None:
    cli_path = Path(__file__).parents[1] / "fixtures" / "fake_sdk_cli.py"
    wire = json.dumps("\x00" * (1024 * 1024))
    assert len(wire.encode()) > 6 * 1024 * 1024
    client = ClaudeSDKClient(
        ClaudeAgentOptions(
            cli_path=cli_path,
            max_buffer_size=8 * 1024 * 1024,
            setting_sources=[],
        )
    )

    await client.connect()
    try:
        await client.query("go")
        messages = [message async for message in client.receive_response()]
    finally:
        await client.disconnect()

    user = next(message for message in messages if isinstance(message, UserMessage))
    assert isinstance(user.content, list)
    result = user.content[0]
    assert isinstance(result, ToolResultBlock)
    assert result.content == "\x00" * (1024 * 1024)
