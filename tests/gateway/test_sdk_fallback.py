from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, StreamEvent, SystemMessage

from claude_sdk_proxy.domain import (
    BackendFailure,
    Completed,
    ResponseIdentity,
    TextDelta,
)
from claude_sdk_proxy.sdk_session import SdkSession
from tests.gateway.fakes import (
    FakeSdkClient,
    FixedTemporaryDirectory,
    raw_text_events,
    raw_tool_events,
    sdk_response,
)
from tests.gateway.test_sdk_refusal import _notice, _raw_delta, _raw_start, _raw_stop
from tests.gateway.test_sdk_session import echo_definition, next_boundary


def pinned_thinking(model: str) -> tuple[object, ...]:
    from tests.gateway.test_thinking_streams import thinking_response

    events = []
    for event in thinking_response():
        event = replace(event, session_id="sdk-1")
        if isinstance(event, StreamEvent) and event.event["type"] == "message_start":
            event.event["message"]["model"] = model
        if isinstance(event, AssistantMessage):
            event = replace(event, model=model)
        events.append(event)
    return tuple(events)


@pytest.mark.anyio
async def test_synthetic_closed_thinking_discard_keeps_replacement_signed_history(
    tmp_path: Path,
) -> None:
    from claude_sdk_proxy.domain import ThinkingBlock, ThinkingCompleted, ThinkingDelta

    original = pinned_thinking("claude-opus-5")[:7]
    events = await collect(
        tmp_path,
        (
            *original,
            fallback_notice(),
            _raw_delta(usage={"input_tokens": 5, "output_tokens": 2}),
            _raw_stop(),
            *pinned_thinking("claude-opus-4-8"),
        ),
    )
    assert events[0] == ResponseIdentity("opus-5", "opus-4.8", True)
    assert [e for e in events if isinstance(e, ThinkingDelta)] == [
        ThinkingDelta(0, "reasoning summary")
    ]
    assert [e for e in events if isinstance(e, ThinkingCompleted)] == [
        ThinkingCompleted(0, ThinkingBlock("reasoning summary", "opaque-signature"))
    ]
    assert events[-1] == Completed("end_turn", {"input_tokens": 5, "output_tokens": 17})


@pytest.mark.anyio
async def test_auto_no_fallback_refusal_is_committed_only_after_result(
    tmp_path: Path,
) -> None:
    from claude_sdk_proxy.domain import InputUsage
    from tests.gateway.test_sdk_refusal import RAW_USAGE, refusal_response

    assert await collect(tmp_path, refusal_response()) == [
        ResponseIdentity("opus-5", "opus-5", False),
        InputUsage(24, 948, 43),
        Completed("refusal", RAW_USAGE),
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        "second-hop",
        "missing-stop",
        "missing-replacement",
        "early-replacement",
        "unconfigured",
    ],
)
async def test_invalid_native_transition_never_releases_identity(
    tmp_path: Path, case: str
) -> None:
    messages = [
        _raw_start(),
        fallback_notice(),
        _raw_delta(),
        _raw_stop(),
        *pinned_response(),
    ]
    if case == "second-hop":
        messages.insert(5, fallback_notice(original_model="claude-opus-4-8"))
    elif case == "missing-stop":
        del messages[3]
    elif case == "missing-replacement":
        messages = messages[:4]
    elif case == "early-replacement":
        del messages[2:4]
    client = FakeSdkClient((tuple(messages),))
    session = SdkSession(
        "opus-5",
        "",
        refusal_fallback="auto",
        allowed_backend_models=("claude-opus-5",)
        if case == "unconfigured"
        else ("claude-opus-5", "claude-opus-4-8"),
        directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
        client_factory=client.capture_options,
    )
    await session.start()
    with pytest.raises(BackendFailure):
        await anext(session.stream_generation("go"))
    assert client.disconnected.is_set()


@pytest.mark.parametrize(
    "payload", [{"signature": "éé"}, {"data": "éé"}, {"partial_json": "éé"}]
)
def test_native_payload_limit_precedes_validator_retention(payload: dict) -> None:
    from claude_sdk_proxy.sdk_fallback import FallbackBuffer

    buffer = FallbackBuffer(limit_bytes=3)
    with pytest.raises(BackendFailure, match="fallback_buffer_limit"):
        buffer.admit_raw({"delta": payload})


def test_tool_suffix_retains_coalesced_thinking_not_per_delta_events() -> None:
    from claude_sdk_proxy.domain import ThinkingDelta, ToolDefinition
    from claude_sdk_proxy.sdk_tool_protocol import RawSdkMessageValidator
    from tests.gateway.test_thinking_streams import interleaved_tool_response

    raw = RawSdkMessageValidator((ToolDefinition("echo", "", {"type": "object"}),))
    # Existing fixture has thinking interleaved between raw tool calls.
    for event in interleaved_tool_response():
        if isinstance(event, StreamEvent):
            if event.event.get("delta", {}).get("thinking") == "reason-2":
                for char in "reason-2":
                    raw.observe(
                        {
                            **event.event,
                            "delta": {"type": "thinking_delta", "thinking": char},
                        }
                    )
            else:
                raw.observe(event.event)
        elif isinstance(event, AssistantMessage):
            raw.validate_assistant(event)
    assert [e for e in raw.tool_suffix if isinstance(e, ThinkingDelta)] == [
        ThinkingDelta(2, "reason-2")
    ]


@pytest.mark.anyio
async def test_synthetic_closed_original_text_is_discarded(tmp_path: Path) -> None:
    original = raw_text_events("discard me", "sdk-1", model="claude-opus-5")[:4]
    events = await collect(
        tmp_path,
        (
            *original,
            fallback_notice(),
            _raw_delta(usage={"input_tokens": 2, "output_tokens": 0}),
            _raw_stop(),
            *pinned_response(),
        ),
    )
    assert [e for e in events if isinstance(e, TextDelta)] == [TextDelta("accepted")]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "changes",
    [
        {"retracted_message_uuids": ["unknown"]},
        {"retracted_message_uuids": ["already-committed-tool"]},
        {"retracted_message_uuids": {}},
        {"unexpected_retraction": []},
        {"fallback_model": "claude-sonnet-5"},
        {"original_model": "claude-opus-4-8"},
        {"session_id": "other"},
        {"api_refusal_category": "unknown"},
        {"api_refusal_category": []},
        {"content": []},
        {"trigger": "overload"},
    ],
)
async def test_malformed_or_unsupported_notice_publishes_nothing(
    tmp_path: Path, changes: dict
) -> None:
    client = FakeSdkClient(
        (
            (
                _raw_start(),
                fallback_notice(**changes),
                _raw_delta(),
                _raw_stop(),
                *pinned_response(),
            ),
        )
    )
    session = SdkSession(
        "opus-5",
        "",
        refusal_fallback="auto",
        allowed_backend_models=("claude-opus-5", "claude-opus-4-8"),
        directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
        client_factory=client.capture_options,
    )
    await session.start()
    with pytest.raises(BackendFailure):
        await anext(session.stream_generation("go"))
    assert client.disconnected.is_set()


@pytest.mark.anyio
@pytest.mark.parametrize("policy", ["off", "auto"])
async def test_identity_is_live_only_in_strict_mode(
    tmp_path: Path, policy: str
) -> None:
    reached, release = asyncio.Event(), asyncio.Event()
    client = FakeSdkClient(
        (pinned_response("claude-opus-5"),), message_barriers={3: (reached, release)}
    )
    session = SdkSession(
        "opus-5",
        "",
        refusal_fallback=policy,
        directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
        client_factory=client.capture_options,
    )
    await session.start()
    stream = session.stream_generation("go")
    first = asyncio.create_task(anext(stream))
    try:
        if policy == "off":
            assert await first == ResponseIdentity("opus-5", "opus-5", False)
            assert not reached.is_set()
        else:
            await reached.wait()
            assert not first.done()
            release.set()
            assert await first == ResponseIdentity("opus-5", "opus-5", False)
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await stream.aclose()
        await session.close()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["timeout", "disconnect", "overflow"])
async def test_auto_failure_discards_buffer_and_closes_client(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    from claude_sdk_proxy.sdk_fallback import FallbackBuffer

    reached, release = asyncio.Event(), asyncio.Event()
    client = FakeSdkClient(
        (pinned_response("claude-opus-5"),),
        message_barriers={} if failure == "overflow" else {3: (reached, release)},
    )
    if failure == "overflow":
        monkeypatch.setattr(
            "claude_sdk_proxy.sdk_session.FallbackBuffer",
            lambda: FallbackBuffer(limit_bytes=3),
        )
    session = SdkSession(
        "opus-5",
        "",
        refusal_fallback="auto",
        directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
        client_factory=client.capture_options,
    )
    await session.start()
    stream = session.stream_generation("go")
    try:
        if failure == "overflow":
            with pytest.raises(BackendFailure, match="fallback_buffer_limit"):
                await anext(stream)
        elif failure == "timeout":
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.02):
                    await anext(stream)
        else:
            first = asyncio.create_task(anext(stream))
            await reached.wait()
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        assert client.disconnected.is_set()
    finally:
        await stream.aclose()
        await session.close()


@pytest.mark.anyio
@pytest.mark.parametrize("activity", ["raw-start", "registered", "parked"])
async def test_synthetic_original_tool_activity_rejects_and_drains_whole_bridge(
    tmp_path: Path, activity: str
) -> None:
    reached, release = asyncio.Event(), asyncio.Event()
    raw = raw_tool_events(
        (("original-tool", "mcp__caller_tools__echo", '{"v":1}'),),
        "sdk-1",
        model="claude-opus-5",
    )
    messages = (
        (*raw[:2], fallback_notice())
        if activity == "raw-start"
        else (_raw_start(), fallback_notice())
    )
    client = FakeSdkClient(
        (messages,), message_barriers={len(messages) - 1: (reached, release)}
    )
    session = SdkSession(
        "opus-5",
        "",
        refusal_fallback="auto",
        tools=(echo_definition(),),
        allowed_backend_models=("claude-opus-5", "claude-opus-4-8"),
        directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
        client_factory=client.capture_options,
    )
    await session.start()
    first = asyncio.create_task(anext(session.stream_generation("go")))
    await reached.wait()
    bridge = session._bridge
    assert bridge is not None
    if activity == "registered":
        await bridge.seal_epoch((("original-tool", "echo", {"v": 1}),))
    elif activity == "parked":
        client.start_tool_callback("echo", {"v": 1}, internal_id="original-tool")
        await asyncio.sleep(0)
    release.set()
    with pytest.raises(BackendFailure, match="fallback_tool_rollback_unsupported"):
        await first
    assert client.disconnected.is_set()
    assert not bridge.has_epoch_activity
    assert not client._tool_tasks
    assert client.tool_results == []
    with pytest.raises(BackendFailure):
        await bridge.begin_epoch()


@pytest.mark.anyio
async def test_replacement_tool_boundary_has_one_identity_and_new_call(
    tmp_path: Path,
) -> None:
    replacement = raw_tool_events(
        (("replacement-tool", "mcp__caller_tools__echo", '{"v":1}'),),
        "sdk-1",
        model="claude-opus-4-8",
    )
    client = FakeSdkClient(
        ((_raw_start(), fallback_notice(), _raw_delta(), _raw_stop(), *replacement),)
    )
    session = SdkSession(
        "opus-5",
        "",
        refusal_fallback="auto",
        tools=(echo_definition(),),
        allowed_backend_models=("claude-opus-5", "claude-opus-4-8"),
        directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
        client_factory=client.capture_options,
    )
    await session.start()
    stream = session.stream_generation("go")
    try:
        events = await next_boundary(stream)
        assert events[0] == ResponseIdentity("opus-5", "opus-4.8", True)
        assert len(events) == 4
        assert events[-1] == Completed(
            "tool_use", {"input_tokens": 3, "output_tokens": 2}
        )
        assert client.tool_results == []
        assert session._expected_internal_ids == {"replacement-tool"}
    finally:
        await stream.aclose()
        await session.close()


def test_discard_removes_original_leg() -> None:
    from claude_sdk_proxy.sdk_fallback import FallbackBuffer

    buffer = FallbackBuffer(limit_bytes=32)
    buffer.append(TextDelta("discard me"))
    buffer.discard()
    buffer.append(TextDelta("accepted"))
    assert buffer.release() == (TextDelta("accepted"),)


def test_buffer_counts_utf8_before_retaining() -> None:
    from claude_sdk_proxy.sdk_fallback import FallbackBuffer

    buffer = FallbackBuffer(limit_bytes=3)
    with pytest.raises(BackendFailure, match="fallback_buffer_limit"):
        buffer.append(TextDelta("éé"))
    assert buffer.release() == ()


def test_buffer_coalesces_and_resets_at_response_boundary() -> None:
    from claude_sdk_proxy.sdk_fallback import FallbackBuffer

    buffer = FallbackBuffer(limit_bytes=4)
    for _ in range(4):
        buffer.append(TextDelta("a"))
    assert buffer.release() == (TextDelta("aaaa"),)
    buffer.append(TextDelta("bbbb"))
    assert buffer.release() == (TextDelta("bbbb"),)


def fallback_notice(**changes: object) -> SystemMessage:
    return SystemMessage(
        "model_refusal_fallback",
        {
            **_notice().data,
            "subtype": "model_refusal_fallback",
            "trigger": "refusal",
            "direction": "retry",
            "scope": "session",
            "fallback_model": "claude-opus-4-8",
            "retracted_message_uuids": [],
            **changes,
        },
    )


def pinned_response(model: str = "claude-opus-4-8") -> tuple[object, ...]:
    events = list(sdk_response("accepted", "sdk-1"))
    start = events[0]
    assert isinstance(start, StreamEvent)
    events[0] = replace(
        start,
        event={
            "type": "message_start",
            "message": {
                **start.event["message"],
                "model": model,
                "id": "replacement",
            },
        },
    )
    events[3] = replace(events[3], model=model)
    return tuple(events)


async def collect(
    tmp_path: Path, messages: tuple[object, ...], policy: str = "auto"
) -> list[object]:
    client = FakeSdkClient((messages,))
    session = SdkSession(
        "opus-5",
        "",
        refusal_fallback=policy,
        allowed_backend_models=("claude-opus-5", "claude-opus-4-8"),
        directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
        client_factory=client.capture_options,
    )
    await session.start()
    try:
        return [event async for event in session.stream_generation("prompt")]
    finally:
        await session.close()


@pytest.mark.anyio
async def test_observed_switch_discards_usage_without_synthetic_diagnostic(
    tmp_path: Path,
) -> None:
    events = await collect(
        tmp_path,
        (
            _raw_start(),
            fallback_notice(),
            _raw_delta(),
            _raw_stop(),
            *pinned_response(),
        ),
    )
    assert events[0] == ResponseIdentity("opus-5", "opus-4.8", True)
    assert [e for e in events if isinstance(e, ResponseIdentity)] == [events[0]]
    assert [e for e in events if isinstance(e, TextDelta)] == [TextDelta("accepted")]
    assert events[-1] == Completed("end_turn", {"input_tokens": 2, "output_tokens": 1})


@pytest.mark.anyio
@pytest.mark.parametrize("model", [None, "opus-5", "claude-opus-4-8"])
async def test_strict_requires_exact_early_identity(
    tmp_path: Path, model: object
) -> None:
    start = _raw_start()
    start.event["message"]["model"] = model
    with pytest.raises(BackendFailure, match="backend_model_mismatch"):
        await collect(tmp_path, (start,), "off")


@pytest.mark.anyio
async def test_strict_typed_conflict_fails_after_live_identity(tmp_path: Path) -> None:
    messages = list(pinned_response("claude-opus-5"))
    assert isinstance(messages[3], AssistantMessage)
    messages[3] = replace(messages[3], model="claude-opus-4-8")
    with pytest.raises(BackendFailure, match="backend_model_mismatch"):
        await collect(tmp_path, tuple(messages), "off")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "messages",
    [(), pinned_response("claude-opus-5")[1:], (pinned_response("claude-opus-5")[3],)],
)
async def test_missing_raw_start_is_an_identity_failure(
    tmp_path: Path, messages: tuple
) -> None:
    with pytest.raises(BackendFailure, match="backend_model_mismatch"):
        await collect(tmp_path, messages, "off")
