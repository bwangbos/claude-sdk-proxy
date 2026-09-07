from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from claude_agent_sdk import project_key_for_directory

from claude_sdk_proxy.domain import (
    CanonicalMessage,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    ToolResultPrompt,
)
from claude_sdk_proxy.sdk_history import seed_history
from claude_sdk_proxy.sdk_session import SdkSession
from tests.gateway.fakes import FakeSdkClient, FixedTemporaryDirectory, sdk_response


@pytest.mark.anyio
async def test_imported_results_are_seeded_before_empty_continuation(tmp_path):
    results = (
        ToolResultBlock("call_a", ("one",), False),
        ToolResultBlock("call_b", ("two",), True),
    )
    history = (
        CanonicalMessage.user_text("use both"),
        CanonicalMessage(
            "assistant",
            (
                ToolCallBlock("call_a", "lookup", {"key": "a"}),
                ToolCallBlock("call_b", "lookup", {"key": "b"}),
            ),
        ),
        CanonicalMessage("user", results),
    )
    client = FakeSdkClient(responses=(sdk_response("recovered", session_id="sdk-1"),))
    session = SdkSession(
        "sonnet",
        "",
        history=history,
        directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
        client_factory=lambda options: client.capture_options(options),
    )
    try:
        await session.start()
        prompt = ToolResultPrompt(results)
        _ = [event async for event in session.stream_turn(prompt)]
        assert client.prompts == [""]
        options = client.options
        entries = await options.session_store.load(
            {
                "project_key": project_key_for_directory(tmp_path),
                "session_id": options.resume,
            }
        )
        ids = [entry["message"]["content"][0]["id"] for entry in entries[1:3]]
        assert [entry["message"]["content"][0] for entry in entries[3:]] == [
            {
                "type": "tool_result",
                "tool_use_id": ids[0],
                "content": [{"type": "text", "text": "one"}],
            },
            {
                "type": "tool_result",
                "tool_use_id": ids[1],
                "content": [{"type": "text", "text": "two"}],
                "is_error": True,
            },
        ]
        assert client.tool_handler_count == 0
    finally:
        await session.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "sdk_tool_names", [{"lookup": "mcp__caller_tools__lookup"}, {}]
)
async def test_seed_history_preserves_roles_text_and_completed_tools(
    tmp_path: Path,
    sdk_tool_names: dict[str, str],
) -> None:
    history = (
        CanonicalMessage.user_text("use lookup"),
        CanonicalMessage(
            "assistant",
            (
                TextBlock("checking"),
                ToolCallBlock("call_public", "lookup", {"key": "color"}),
            ),
        ),
        CanonicalMessage("user", (ToolResultBlock("call_public", ("blue",), False),)),
        CanonicalMessage.assistant_text("the result was blue"),
    )

    seeded = await seed_history(
        history,
        cwd=tmp_path,
        model="sonnet",
        sdk_tool_names=sdk_tool_names,
    )
    key = {
        "project_key": seeded.project_key,
        "session_id": seeded.session_id,
    }
    entries = await seeded.store.load(key)

    assert entries is not None
    assert [entry["type"] for entry in entries] == [
        "user",
        "assistant",
        "assistant",
        "user",
        "assistant",
    ]
    assert all(entry["sessionId"] == seeded.session_id for entry in entries)
    assert uuid.UUID(seeded.session_id).version == 4
    assert entries[0]["message"] == {"role": "user", "content": "use lookup"}
    assert entries[0]["promptSource"] == "sdk"
    assert entries[1]["message"]["content"] == [{"type": "text", "text": "checking"}]
    assistant_tool = entries[2]["message"]["content"][0]
    assert entries[1]["message"]["id"] == entries[2]["message"]["id"]
    assert entries[1]["requestId"] == entries[2]["requestId"]
    assert [entries[1]["apiBlockIndex"], entries[2]["apiBlockIndex"]] == [0, 1]
    assert assistant_tool["name"] == "mcp__caller_tools__lookup"
    assert assistant_tool["input"] == {"key": "color"}
    assert assistant_tool["caller"] == {"type": "direct"}
    internal_id = assistant_tool["id"]
    assert internal_id != "call_public"
    assert entries[3]["message"]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": internal_id,
            "content": [{"type": "text", "text": "blue"}],
        }
    ]
    assert entries[3]["toolUseResult"] == [{"type": "text", "text": "blue"}]
    assert entries[3]["sourceToolAssistantUUID"] == entries[2]["uuid"]
    assert entries[3]["promptId"] == entries[0]["promptId"]
    assert entries[4]["message"]["content"] == [
        {"type": "text", "text": "the result was blue"}
    ]
    assert entries[0]["parentUuid"] is None
    assert [entry["parentUuid"] for entry in entries[1:]] == [
        entry["uuid"] for entry in entries[:-1]
    ]


@pytest.mark.anyio
async def test_seed_history_splits_parallel_results_into_native_entries(
    tmp_path: Path,
) -> None:
    history = (
        CanonicalMessage.user_text("use both"),
        CanonicalMessage(
            "assistant",
            (
                ToolCallBlock("call_a", "lookup", {"key": "a"}),
                ToolCallBlock("call_b", "lookup", {"key": "b"}),
            ),
        ),
        CanonicalMessage(
            "user",
            (
                ToolResultBlock("call_a", ("one",), False),
                ToolResultBlock("call_b", ("two",), False),
            ),
        ),
    )

    seeded = await seed_history(
        history,
        cwd=tmp_path,
        model="sonnet",
        sdk_tool_names={"lookup": "mcp__caller_tools__lookup"},
    )
    entries = await seeded.store.load(
        {"project_key": seeded.project_key, "session_id": seeded.session_id}
    )

    assert entries is not None
    assert [entry["type"] for entry in entries] == [
        "user",
        "assistant",
        "assistant",
        "user",
        "user",
    ]
    assert [entry["toolUseResult"] for entry in entries[3:]] == [
        [{"type": "text", "text": "one"}],
        [{"type": "text", "text": "two"}],
    ]
    assert [entry["sourceToolAssistantUUID"] for entry in entries[3:]] == [
        entries[1]["uuid"],
        entries[2]["uuid"],
    ]
    assert entries[4]["parentUuid"] == entries[3]["uuid"]
