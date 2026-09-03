import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    ToolUseBlock,
)

from claude_sdk_proxy.agent_sdk_backend import AgentSdkBackend
from claude_sdk_proxy.domain import (
    BackendEvent,
    BackendFailure,
    CanonicalMessage,
    CanonicalRequest,
    Completed,
    TextDelta,
    ToolDefinition,
    UnsupportedFeature,
)


def test_build_options_disables_ambient_agent_behavior(tmp_path: Path) -> None:
    request = CanonicalRequest(
        model="claude-test",
        system="caller-system",
        messages=(CanonicalMessage("user", "caller-message"),),
    )

    options = AgentSdkBackend().build_options(request, tmp_path)

    assert options.model == "claude-test"
    assert options.system_prompt == "caller-system"
    assert options.tools == []
    assert options.allowed_tools == []
    assert options.skills == []
    assert options.setting_sources == []
    assert options.mcp_servers == {}
    assert options.strict_mcp_config is True
    assert options.permission_mode == "dontAsk"
    assert options.agents == {}
    assert options.plugins == []
    assert options.cwd == tmp_path
    assert options.include_partial_messages is True
    assert options.extra_args == {
        "safe-mode": None,
        "restricted": None,
        "disable-slash-commands": None,
        "no-session-persistence": None,
        "system-prompt-snapshot": "off",
    }


def test_sdk_rejects_assistant_history_instead_of_flattening() -> None:
    request = CanonicalRequest(
        model="claude-test",
        system="",
        messages=(
            CanonicalMessage("user", "first"),
            CanonicalMessage("assistant", "prior answer"),
            CanonicalMessage("user", "next"),
        ),
    )

    with pytest.raises(UnsupportedFeature, match="messages"):
        AgentSdkBackend().prompt_for(request)


def test_sdk_rejects_caller_tools_instead_of_using_mcp() -> None:
    request = CanonicalRequest(
        model="claude-test",
        system="",
        messages=(CanonicalMessage("user", "use a tool"),),
        tools=(ToolDefinition("weather", "Get weather", {"type": "object"}),),
    )

    with pytest.raises(UnsupportedFeature, match="tools"):
        AgentSdkBackend().prompt_for(request)


def test_structural_report_does_not_claim_unverified_capabilities() -> None:
    report = AgentSdkBackend().structural_report()

    assert report.backend == "agent-sdk"
    assert report.authentication == "untested"
    assert report.streaming == "untested"
    assert report.prompt_construction == "pass"
    assert report.multi_turn == "fail"
    assert report.structured_tools == "fail"
    assert report.single_turn_text_viable is False
    assert report.evidence == (
        "ClaudeAgentOptions disables built-ins and ambient sources",
        "query accepts string or user-message iterable; "
        "assistant replay is not claimed",
        "caller tools would require proxy-executed MCP and are rejected",
    )


def test_stream_maps_text_deltas_and_result_from_injected_query() -> None:
    request = CanonicalRequest(
        model="claude-test",
        system="caller-system",
        messages=(CanonicalMessage("user", "caller-message"),),
    )
    observed: dict[str, Any] = {}

    async def fake_query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        observed["prompt"] = prompt
        observed["options"] = options
        yield StreamEvent(
            uuid="event-1",
            session_id="session-1",
            event={
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "hel"},
            },
        )
        yield StreamEvent(
            uuid="event-2",
            session_id="session-1",
            event={
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "lo"},
            },
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=0,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="session-1",
            stop_reason="end_turn",
            usage={"output_tokens": 1},
        )

    async def collect() -> list[BackendEvent]:
        return [event async for event in AgentSdkBackend(fake_query).stream(request)]

    assert asyncio.run(collect()) == [
        TextDelta("hel"),
        TextDelta("lo"),
        Completed("end_turn", {"output_tokens": 1}),
    ]
    assert observed["prompt"] == "caller-message"
    assert observed["options"].system_prompt == "caller-system"


def test_stream_rejects_post_result_message_without_exposing_completed() -> None:
    request = CanonicalRequest(
        model="claude-test",
        system="",
        messages=(CanonicalMessage("user", "caller-message"),),
    )
    received: list[BackendEvent] = []

    async def fake_query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        del prompt, options
        yield ResultMessage(
            subtype="success",
            duration_ms=0,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="session-1",
            stop_reason="end_turn",
            usage={"output_tokens": 1},
        )
        yield StreamEvent(
            uuid="event-1",
            session_id="session-1",
            event={
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "late"},
            },
        )

    async def consume() -> None:
        async for event in AgentSdkBackend(fake_query).stream(request):
            received.append(event)

    with pytest.raises(BackendFailure, match="after result"):
        asyncio.run(consume())

    assert received == []


def test_stream_rejects_duplicate_result_without_exposing_completed() -> None:
    request = CanonicalRequest(
        model="claude-test",
        system="",
        messages=(CanonicalMessage("user", "caller-message"),),
    )
    received: list[BackendEvent] = []

    async def fake_query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        del prompt, options
        for session_id in ("session-1", "session-2"):
            yield ResultMessage(
                subtype="success",
                duration_ms=0,
                duration_api_ms=0,
                is_error=False,
                num_turns=1,
                session_id=session_id,
                stop_reason="end_turn",
                usage={"output_tokens": 1},
            )

    async def consume() -> None:
        async for event in AgentSdkBackend(fake_query).stream(request):
            received.append(event)

    with pytest.raises(BackendFailure, match="after result"):
        asyncio.run(consume())

    assert received == []


def test_stream_cleans_temporary_directory_before_exposing_completed() -> None:
    request = CanonicalRequest(
        model="claude-test",
        system="",
        messages=(CanonicalMessage("user", "caller-message"),),
    )
    temporary_directory: Path | None = None

    async def fake_query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        nonlocal temporary_directory
        del prompt
        temporary_directory = Path(options.cwd)
        yield ResultMessage(
            subtype="success",
            duration_ms=0,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="session-1",
            stop_reason="end_turn",
            usage={"output_tokens": 1},
        )

    async def consume() -> list[BackendEvent]:
        received: list[BackendEvent] = []
        async for event in AgentSdkBackend(fake_query).stream(request):
            assert temporary_directory is not None
            assert not temporary_directory.exists()
            received.append(event)
        return received

    assert asyncio.run(consume()) == [Completed("end_turn", {"output_tokens": 1})]


def test_stream_rejects_agent_tool_use_from_injected_query() -> None:
    request = CanonicalRequest(
        model="claude-test",
        system="",
        messages=(CanonicalMessage("user", "caller-message"),),
    )

    async def fake_query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        del prompt, options
        yield AssistantMessage(
            content=[ToolUseBlock(id="tool-1", name="Bash", input={})],
            model="claude-test",
        )

    async def collect() -> None:
        async for _ in AgentSdkBackend(fake_query).stream(request):
            pass

    with pytest.raises(UnsupportedFeature, match="tools") as error:
        asyncio.run(collect())

    assert error.value.field == "tools"
