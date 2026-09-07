from __future__ import annotations

import json
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent
from claude_agent_sdk import TextBlock as SdkTextBlock
from claude_agent_sdk import ThinkingBlock as SdkThinkingBlock

from claude_sdk_proxy.app import create_app
from claude_sdk_proxy.sdk_session import SdkSession
from tests.gateway.asgi_client import lifespan_app, post_json
from tests.gateway.fakes import FakeSdkClient, FixedTemporaryDirectory


def frame(event: dict[str, Any]) -> StreamEvent:
    return StreamEvent(uuid="event", session_id="sdk-thinking", event=event)


def thinking_response() -> tuple[Any, ...]:
    # Handwritten wire frames, including real SDK per-block typed ordering.
    return (
        frame(
            {
                "type": "message_start",
                "message": {
                    "model": "claude-sonnet-5",
                    "usage": {"input_tokens": 5, "output_tokens": 2},
                },
            }
        ),
        frame(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            }
        ),
        frame(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "thinking_delta",
                    "thinking": "reasoning ",
                    "estimated_tokens": None,
                },
            }
        ),
        frame(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "summary"},
            }
        ),
        frame(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "opaque-signature"},
            }
        ),
        AssistantMessage(
            [SdkThinkingBlock("reasoning summary", "opaque-signature")],
            "claude-sonnet-5",
            session_id="sdk-thinking",
        ),
        frame({"type": "content_block_stop", "index": 0}),
        frame(
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": ""},
            }
        ),
        frame(
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "answer"},
            }
        ),
        AssistantMessage(
            [SdkTextBlock("answer")], "claude-sonnet-5", session_id="sdk-thinking"
        ),
        frame({"type": "content_block_stop", "index": 1}),
        frame(
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "stop_details": None,
                },
                "usage": {"output_tokens": 17},
                "context_management": {"applied_edits": []},
            }
        ),
        frame({"type": "message_stop"}),
        ResultMessage(
            subtype="success",
            duration_ms=0,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="sdk-thinking",
            stop_reason="end_turn",
            usage={"output_tokens": 17},
        ),
    )


@pytest.mark.anyio
@pytest.mark.parametrize("tools_enabled", [False, True])
async def test_raw_thinking_is_reasoning_not_answer_and_usage_is_sdk_count(
    tmp_path,
    tools_enabled,
):
    client = FakeSdkClient((thinking_response(),))
    app = create_app(
        models=("claude-sonnet-5",),
        session_factory=lambda model, system, **kw: SdkSession(
            model,
            system,
            **kw,
            directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
            client_factory=client.capture_options,
        ),
    )
    body = {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]}
    if tools_enabled:
        body["tools"] = [
            {
                "type": "function",
                "function": {"name": "echo", "parameters": {"type": "object"}},
            }
        ]
    async with lifespan_app(app):
        response = await post_json(app, "/v1/chat/completions", body)
    assert response.status == 200, response.json
    assert response.json["choices"][0]["message"]["content"] == "answer"
    assert (
        response.json["choices"][0]["message"]["reasoning_content"]
        == "reasoning summary"
    )
    assert response.json["usage"]["completion_tokens"] == 17


@pytest.mark.anyio
@pytest.mark.parametrize("dialect", ["anthropic", "openai"])
async def test_stream_preserves_thinking_fields_and_block_indices(tmp_path, dialect):
    client = FakeSdkClient((thinking_response(),))
    app = create_app(
        models=("claude-sonnet-5",),
        session_factory=lambda model, system, **kw: SdkSession(
            model,
            system,
            **kw,
            directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
            client_factory=client.capture_options,
        ),
    )
    body = {
        "model": "claude-sonnet-5",
        "max_tokens": 100,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }
    path = "/v1/messages" if dialect == "anthropic" else "/v1/chat/completions"
    async with lifespan_app(app):
        response = await post_json(app, path, body)
    assert response.status == 200
    records = [
        json.loads(line[6:])
        for line in response.body.decode().splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    if dialect == "openai":
        deltas = [r["choices"][0]["delta"] for r in records if r.get("choices")]
        assert [d["reasoning_content"] for d in deltas if "reasoning_content" in d] == [
            "reasoning ",
            "summary",
        ]
        assert [d["content"] for d in deltas if d.get("content")] == ["answer"]
        assert "opaque-signature" not in response.body.decode()
    else:
        assert [
            (r["index"], r["content_block"]["type"])
            for r in records
            if r.get("type") == "content_block_start"
        ] == [(0, "thinking"), (1, "text")]
        assert [
            (r["index"], r["delta"])
            for r in records
            if r.get("type") == "content_block_delta"
        ] == [
            (0, {"type": "thinking_delta", "thinking": "reasoning "}),
            (0, {"type": "thinking_delta", "thinking": "summary"}),
            (0, {"type": "signature_delta", "signature": "opaque-signature"}),
            (1, {"type": "text_delta", "text": "answer"}),
        ]


@pytest.mark.parametrize(
    "mutation",
    [
        "signature_type",
        "empty_signature",
        "wrong_delta",
        "bad_estimate",
        "unsigned",
        "typed_mismatch",
        "late_delta",
    ],
)
def test_malformed_thinking_is_rejected(mutation):
    from claude_sdk_proxy.domain import BackendFailure
    from claude_sdk_proxy.sdk_tool_protocol import RawSdkMessageValidator

    events = list(thinking_response()[:-1])
    if mutation == "signature_type":
        events[4].event["delta"]["signature"] = 8
    elif mutation == "empty_signature":
        events[4].event["delta"]["signature"] = ""
    elif mutation == "wrong_delta":
        events[2].event["delta"] = {"type": "text_delta", "text": "leak"}
    elif mutation == "bad_estimate":
        events[2].event["delta"]["estimated_tokens"] = True
    elif mutation == "unsigned":
        del events[4:6]
    elif mutation == "typed_mismatch":
        events[5] = AssistantMessage(
            [SdkThinkingBlock("wrong", "opaque-signature")], "claude-sonnet-5"
        )
    else:
        events.insert(6, events[2])
    validator = RawSdkMessageValidator(())
    with pytest.raises(BackendFailure):
        for event in events:
            if isinstance(event, StreamEvent):
                validator.observe(event.event)
            else:
                validator.validate_assistant(event)


@pytest.mark.parametrize("typed_mode", ["per_block", "batch"])
@pytest.mark.parametrize("kind", ["thinking", "redacted_thinking"])
def test_empty_display_and_redacted_blocks_keep_native_payload(kind, typed_mode):
    from claude_agent_sdk._internal.message_parser import parse_message

    from claude_sdk_proxy.domain import (
        RedactedThinkingBlock,
        ThinkingBlock,
        ThinkingCompleted,
    )
    from claude_sdk_proxy.sdk_tool_protocol import RawSdkMessageValidator

    block = (
        {"type": "thinking", "thinking": "", "signature": ""}
        if kind == "thinking"
        else {"type": kind, "data": "opaque-redacted"}
    )
    validator = RawSdkMessageValidator(())
    validator.observe(thinking_response()[0].event)
    validator.observe(
        {"type": "content_block_start", "index": 0, "content_block": block}
    )
    if kind == "thinking":
        validator.observe(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "sig"},
            }
        )
        block = {**block, "signature": "sig"}
    typed = parse_message(
        {
            "type": "assistant",
            "message": {"model": "claude-sonnet-5", "content": [block]},
        }
    )
    assert isinstance(typed, AssistantMessage)
    if kind == "redacted_thinking":
        assert (
            typed.content == []
        )  # Characterizes the installed SDK's lossy projection.
    if typed_mode == "per_block":
        validator.validate_assistant(typed)
    completed = validator.observe({"type": "content_block_stop", "index": 0})
    if typed_mode == "batch":
        validator.validate_assistant(typed)
    assert completed == ThinkingCompleted(
        0,
        ThinkingBlock("", "sig")
        if kind == "thinking"
        else RedactedThinkingBlock("opaque-redacted"),
    )


def test_missing_typed_thinking_does_not_pass_as_lossy_redacted_projection():
    from claude_sdk_proxy.domain import BackendFailure
    from claude_sdk_proxy.sdk_tool_protocol import RawSdkMessageValidator

    validator = RawSdkMessageValidator(())
    with pytest.raises(BackendFailure):
        for event in thinking_response()[:-1]:
            if isinstance(event, StreamEvent):
                validator.observe(event.event)


@pytest.mark.anyio
@pytest.mark.parametrize("tools_enabled", [False, True])
async def test_disconnect_during_thinking_invalidates_uncommitted_session(
    tmp_path, tools_enabled
):
    import asyncio

    from tests.gateway.asgi_client import _LIFESPAN_STATES, post_json_then_disconnect

    entered, release = asyncio.Event(), asyncio.Event()
    client = FakeSdkClient(
        (thinking_response(),), message_barriers={3: (entered, release)}
    )
    app = create_app(
        models=("claude-sonnet-5",),
        session_factory=lambda model, system, **kw: SdkSession(
            model,
            system,
            **kw,
            directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
            client_factory=client.capture_options,
        ),
    )
    body = {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]}
    if tools_enabled:
        body["tools"] = [
            {
                "type": "function",
                "function": {"name": "echo", "parameters": {"type": "object"}},
            }
        ]
    async with lifespan_app(app):
        await post_json_then_disconnect(
            app, "/v1/chat/completions", body, after=entered
        )
        await asyncio.wait_for(client.disconnected.wait(), 1)
        registry = _LIFESPAN_STATES[app]["registry"]
        assert not registry._implicit
    assert client.disconnect_count == 1


def interleaved_tool_response():
    from claude_agent_sdk import ToolUseBlock

    events = [thinking_response()[0]]
    for index in range(4):
        if index % 2 == 0:
            events += [
                frame(
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {
                            "type": "thinking",
                            "thinking": "",
                            "signature": "",
                        },
                    }
                ),
                frame(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {
                            "type": "thinking_delta",
                            "thinking": f"reason-{index}",
                        },
                    }
                ),
                frame(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {
                            "type": "signature_delta",
                            "signature": f"sig-{index}",
                        },
                    }
                ),
                AssistantMessage(
                    [SdkThinkingBlock(f"reason-{index}", f"sig-{index}")],
                    "claude-sonnet-5",
                ),
                frame({"type": "content_block_stop", "index": index}),
            ]
        else:
            events += [
                frame(
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {
                            "type": "tool_use",
                            "id": f"sdk-{index}",
                            "name": "mcp__caller_tools__echo",
                            "input": {},
                            "caller": {"type": "direct"},
                        },
                    }
                ),
                frame(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "input_json_delta", "partial_json": "{}"},
                    }
                ),
                AssistantMessage(
                    [ToolUseBlock(f"sdk-{index}", "mcp__caller_tools__echo", {})],
                    "claude-sonnet-5",
                ),
                frame({"type": "content_block_stop", "index": index}),
            ]
    events += [
        frame(
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": "tool_use",
                    "stop_sequence": None,
                    "stop_details": None,
                },
                "usage": {"output_tokens": 17},
                "context_management": {"applied_edits": []},
            }
        ),
        frame({"type": "message_stop"}),
    ]
    return tuple(events)


@pytest.mark.anyio
@pytest.mark.parametrize("stream", [False, True])
async def test_thinking_between_tools_keeps_wire_order_after_sealing(tmp_path, stream):
    client = FakeSdkClient((interleaved_tool_response(),))
    app = create_app(
        models=("claude-sonnet-5",),
        session_factory=lambda model, system, **kw: SdkSession(
            model,
            system,
            **kw,
            directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
            client_factory=client.capture_options,
        ),
    )
    body = {
        "model": "claude-sonnet-5",
        "max_tokens": 100,
        "stream": stream,
        "tools": [{"name": "echo", "input_schema": {"type": "object"}}],
        "messages": [{"role": "user", "content": "go"}],
    }
    async with lifespan_app(app):
        response = await post_json(app, "/v1/messages", body)
        assert response.status == 200
        if stream:
            records = [
                json.loads(line[6:])
                for line in response.body.decode().splitlines()
                if line.startswith("data: ")
            ]
            starts = [
                (r["index"], r["content_block"]["type"])
                for r in records
                if r["type"] == "content_block_start"
            ]
            assert starts == [
                (0, "thinking"),
                (1, "tool_use"),
                (2, "thinking"),
                (3, "tool_use"),
            ]
        else:
            assert [b["type"] for b in response.json["content"]] == [
                "thinking",
                "tool_use",
                "thinking",
                "tool_use",
            ]
        assert client.tool_handler_count == 2


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["empty", "redacted"])
@pytest.mark.parametrize("stream", [False, True])
async def test_native_empty_display_and_raw_redacted_survive_http(
    tmp_path, kind, stream
):
    original = thinking_response()
    if kind == "empty":
        expected = {"type": "thinking", "thinking": "", "signature": "opaque-signature"}
        events = (
            original[0],
            original[1],
            original[4],
            AssistantMessage(
                [SdkThinkingBlock("", "opaque-signature")], "claude-sonnet-5"
            ),
            *original[6:],
        )
    else:
        expected = {"type": "redacted_thinking", "data": "opaque-redacted"}
        events = (
            original[0],
            frame(
                {"type": "content_block_start", "index": 0, "content_block": expected}
            ),
            AssistantMessage([], "claude-sonnet-5"),
            *original[6:],
        )
    client = FakeSdkClient((events,))
    app = create_app(
        models=("claude-sonnet-5",),
        session_factory=lambda model, system, **kw: SdkSession(
            model,
            system,
            **kw,
            directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
            client_factory=client.capture_options,
        ),
    )
    async with lifespan_app(app):
        response = await post_json(
            app,
            "/v1/messages",
            {
                "model": "claude-sonnet-5",
                "max_tokens": 100,
                "stream": stream,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status == 200
    if not stream:
        assert response.json["content"] == [
            expected,
            {"type": "text", "text": "answer"},
        ]
    else:
        records = [
            json.loads(line[6:])
            for line in response.body.decode().splitlines()
            if line.startswith("data: ")
        ]
        starts = [r for r in records if r["type"] == "content_block_start"]
        assert [r["index"] for r in starts] == [0, 1]
        assert starts[0]["content_block"] == (
            expected if kind == "redacted" else {**expected, "signature": ""}
        )
        assert [r["index"] for r in records if r["type"] == "content_block_stop"] == [
            0,
            1,
        ]
        if kind == "empty":
            assert records[2]["delta"] == {
                "type": "signature_delta",
                "signature": "opaque-signature",
            }


def test_text_thinking_text_interleaving_preserves_signed_order_and_public_text():
    from claude_sdk_proxy.domain import (
        CanonicalMessage,
        TextBlock,
        TextDelta,
        ThinkingBlock,
        ThinkingCompleted,
    )
    from claude_sdk_proxy.sdk_tool_protocol import RawSdkMessageValidator
    from claude_sdk_proxy.session_identity import messages_equal
    from claude_sdk_proxy.tool_session_actor import _assistant_message

    validator = RawSdkMessageValidator(())
    normalized = []
    original = thinking_response()
    frames = [original[0]]
    for new_index, subset in enumerate((original[7:11], original[1:7], original[7:11])):
        for item in subset:
            frames.append(
                frame({**item.event, "index": new_index})
                if isinstance(item, StreamEvent)
                else item
            )
    frames += list(original[11:-1])
    for item in frames:
        if isinstance(item, StreamEvent):
            event = validator.observe(item.event)
            if event is not None:
                normalized.append(event)
        else:
            validator.validate_assistant(item)
    assistant = _assistant_message(tuple(normalized))
    assert assistant.blocks == (
        TextBlock("answer"),
        ThinkingBlock("reasoning summary", "opaque-signature"),
        TextBlock("answer"),
    )
    assert [e.index for e in normalized if isinstance(e, ThinkingCompleted)] == [1]
    assert [e.text for e in normalized if isinstance(e, TextDelta)] == [
        "answer",
        "answer",
    ]
    assert messages_equal(
        (assistant,),
        (CanonicalMessage.assistant_text("answeranswer"),),
        dialect="openai",
    )


def thinking_progress(**overrides):
    from claude_agent_sdk import SystemMessage

    return SystemMessage(
        "thinking_tokens",
        {
            "type": "system",
            "subtype": "thinking_tokens",
            "estimated_tokens": 50,
            "estimated_tokens_delta": 50,
            "session_id": "sdk-thinking",
            "uuid": "progress",
            **overrides,
        },
    )


@pytest.mark.anyio
@pytest.mark.parametrize("tools_enabled", [False, True])
async def test_live_thinking_token_progress_is_not_output_or_usage(
    tmp_path, tools_enabled
):
    raw = thinking_response()
    client = FakeSdkClient(((*raw[:2], thinking_progress(), *raw[2:]),))
    app = create_app(
        models=("claude-sonnet-5",),
        session_factory=lambda model, system, **kw: SdkSession(
            model,
            system,
            **kw,
            directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
            client_factory=client.capture_options,
        ),
    )
    body = {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]}
    if tools_enabled:
        body["tools"] = [
            {
                "type": "function",
                "function": {"name": "echo", "parameters": {"type": "object"}},
            }
        ]
    async with lifespan_app(app):
        response = await post_json(app, "/v1/chat/completions", body)
    assert response.status == 200, response.json
    assert response.json["choices"][0]["message"] == {
        "role": "assistant",
        "content": "answer",
        "reasoning_content": "reasoning summary",
    }
    assert response.json["usage"]["completion_tokens"] == 17


@pytest.mark.anyio
@pytest.mark.parametrize(
    "overrides,index",
    [
        ({"estimated_tokens": None}, 2),
        ({"estimated_tokens": True}, 2),
        ({"estimated_tokens": -1}, 2),
        ({"estimated_tokens_delta": -1}, 2),
        ({"estimated_tokens_delta": 51}, 2),
        ({"estimated_tokens_delta": 2**63}, 2),
        ({"session_id": "different"}, 2),
        ({"uuid": ""}, 2),
        ({"unexpected": "field"}, 2),
        ({}, 0),
        ({}, 8),
        ({}, 13),
    ],
)
async def test_thinking_progress_remains_strictly_scoped(tmp_path, overrides, index):
    from claude_sdk_proxy.domain import BackendFailure

    raw = thinking_response()
    client = FakeSdkClient(
        ((*raw[:index], thinking_progress(**overrides), *raw[index:]),)
    )
    session = SdkSession(
        "claude-sonnet-5",
        "",
        directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
        client_factory=client.capture_options,
    )
    await session.start()
    try:
        with pytest.raises(BackendFailure):
            _ = [event async for event in session.stream_generation("hi")]
    finally:
        await session.close()
