from __future__ import annotations

import json
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, UserMessage
from claude_agent_sdk import ToolResultBlock as SdkToolResultBlock
from openai.types.completion_usage import CompletionUsage

import claude_sdk_proxy.app as app_module
from claude_sdk_proxy.anthropic_api import (
    encode_anthropic_start,
    render_anthropic_response,
)
from claude_sdk_proxy.domain import (
    BackendFailure,
    Completed,
    InputUsage,
    RequestValidationError,
    TextDelta,
    ToolDefinition,
)
from claude_sdk_proxy.openai_api import (
    encode_openai_event,
    encode_openai_start,
    parse_openai_request,
    render_openai_response,
)
from claude_sdk_proxy.sdk_tool_protocol import RawSdkMessageValidator
from tests.gateway.asgi_client import lifespan_app, post_json
from tests.gateway.fakes import FakeSdkClient, raw_text_events, raw_tool_events
from tests.gateway.test_sdk_session import collect_sdk_response, result_message
from tests.gateway.test_tool_http import (
    _assistant_sdk_message,
    _sdk_app,
    _sdk_result_messages,
    _sdk_tool_body,
    _sse_records,
    _tool_payloads,
)


def _anthropic_start_usage(chunks: tuple[bytes, ...]) -> object:
    payload = json.loads(chunks[0].split(b"data: ", 1)[1])
    return payload["message"]["usage"]


def _openai_payload(chunk: bytes) -> dict[str, object]:
    return json.loads(chunk.removeprefix(b"data: ").strip())


def test_explicit_openai_completion_timestamp_is_consistent_across_outputs() -> None:
    created = 1_799_000_123
    completed = Completed("end_turn", {"input_tokens": 2, "output_tokens": 1})

    assert _openai_payload(
        encode_openai_start("chatcmpl_test", "sonnet", created=created)[0]
    )["created"] == created
    chunks = encode_openai_event(
        "chatcmpl_test",
        "sonnet",
        completed,
        include_usage=True,
        created=created,
    )
    assert [_openai_payload(chunk)["created"] for chunk in chunks[:-1]] == [
        created,
        created,
    ]
    assert render_openai_response(
        "chatcmpl_test", "sonnet", "done", completed, created=created
    )["created"] == created

    # Existing callers that do not pass request context retain their old contract.
    assert _openai_payload(encode_openai_start("chatcmpl_test", "sonnet")[0])[
        "created"
    ] == 0


@pytest.mark.parametrize("user", ["advisory-user", None])
def test_openai_user_and_known_null_optionals_are_advisory(user: str | None) -> None:
    request = parse_openai_request(
        {
            "model": "sonnet",
            "messages": [{"role": "user", "content": "go"}],
            "user": user,
            "tools": None,
            "tool_choice": None,
            "parallel_tool_calls": None,
            "stream_options": None,
            "max_completion_tokens": None,
            "temperature": None,
        },
        frozenset({"sonnet"}),
    )

    assert request.model == "sonnet"
    assert request.max_tokens is None
    assert request.tools == ()
    assert request == parse_openai_request(
        {
            "model": "sonnet",
            "messages": [{"role": "user", "content": "go"}],
        },
        frozenset({"sonnet"}),
    )


@pytest.mark.parametrize(("field", "value"), [("user", 7), ("unknown", None)])
def test_openai_advisory_normalization_does_not_ignore_invalid_values(
    field: str, value: object
) -> None:
    with pytest.raises(RequestValidationError) as error:
        parse_openai_request(
            {
                "model": "sonnet",
                "messages": [{"role": "user", "content": "go"}],
                field: value,
            },
            frozenset({"sonnet"}),
        )

    assert error.value.field == field


@pytest.mark.anyio
@pytest.mark.parametrize("stream", [False, True])
async def test_asgi_captures_one_timestamp_for_the_entire_openai_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream: bool
) -> None:
    created = 1_799_000_456
    timestamp_calls = 0

    def timestamp() -> int:
        nonlocal timestamp_calls
        timestamp_calls += 1
        return created

    monkeypatch.setattr(app_module, "_completion_created", timestamp)
    client = FakeSdkClient(
        responses=(
            (
                *raw_text_events("done", "sdk-real"),
                ResultMessage(
                    subtype="success",
                    duration_ms=0,
                    duration_api_ms=0,
                    is_error=False,
                    num_turns=1,
                    session_id="sdk-real",
                    stop_reason="end_turn",
                    usage={"input_tokens": 2, "output_tokens": 1},
                ),
            ),
        )
    )
    app = _sdk_app(tmp_path, client)
    body: dict[str, object] = {
        "model": "sonnet",
        "messages": [{"role": "user", "content": "go"}],
        "stream": stream,
    }
    if stream:
        body["stream_options"] = {"include_usage": True}

    async with lifespan_app(app):
        response = await post_json(app, "/v1/chat/completions", body)

    assert response.status == 200
    assert timestamp_calls == 1
    if stream:
        chunk_payloads = [
            item
            for _, item in _sse_records(response.body)
            if isinstance(item, dict) and item.get("object") == "chat.completion.chunk"
        ]
        assert len(chunk_payloads) >= 3
        assert {item["created"] for item in chunk_payloads} == {created}
    else:
        assert response.json["created"] == created


def test_cache_usage_maps_without_fabricating_absent_optional_fields() -> None:
    boundary = InputUsage(
        5,
        cache_read_input_tokens=700,
        cache_creation_input_tokens=11,
    )
    usage = {
        "input_tokens": boundary.input_tokens,
        "output_tokens": 2,
        "cache_read_input_tokens": boundary.cache_read_input_tokens,
        "cache_creation_input_tokens": boundary.cache_creation_input_tokens,
    }
    completed = Completed("end_turn", usage)

    assert _anthropic_start_usage(
        encode_anthropic_start("msg_test", "sonnet", boundary)
    ) == {
        "input_tokens": 5,
        "output_tokens": 0,
        "cache_read_input_tokens": 700,
        "cache_creation_input_tokens": 11,
    }
    assert render_anthropic_response("msg_test", "sonnet", "ok", completed)[
        "usage"
    ] == {
        "input_tokens": 5,
        "output_tokens": 2,
        "cache_read_input_tokens": 700,
        "cache_creation_input_tokens": 11,
    }
    openai_usage = render_openai_response(
        "chatcmpl_test", "sonnet", "ok", completed
    )["usage"]
    assert openai_usage == {
        "prompt_tokens": 716,
        "completion_tokens": 2,
        "total_tokens": 718,
        "prompt_tokens_details": {
            "cached_tokens": 700,
            "cache_write_tokens": 11,
        },
    }
    typed_usage = CompletionUsage.model_validate(openai_usage)
    assert typed_usage.prompt_tokens_details is not None
    assert typed_usage.prompt_tokens_details.cached_tokens == 700
    assert typed_usage.prompt_tokens_details.cache_write_tokens == 11

    absent = Completed("end_turn", {"input_tokens": 5, "output_tokens": 2})
    assert "prompt_tokens_details" not in render_openai_response(
        "chatcmpl_test", "sonnet", "ok", absent
    )["usage"]
    assert render_anthropic_response("msg_test", "sonnet", "ok", absent)[
        "usage"
    ] == {"input_tokens": 5, "output_tokens": 2}


@pytest.mark.anyio
async def test_sdk_boundary_preserves_cache_snapshot_and_checks_repetitions(
    tmp_path: Path,
) -> None:
    cache_usage = {
        "input_tokens": 5,
        "cache_read_input_tokens": 700,
        "cache_creation_input_tokens": 11,
    }
    raw = list(
        raw_text_events("done", "sdk-1", input_tokens=5, output_tokens=2)
    )
    raw[0].event["message"]["usage"] = {**cache_usage, "output_tokens": 0}
    raw[3:3] = [
        AssistantMessage(
            [TextBlock("done")],
            "sonnet",
            usage={**cache_usage, "output_tokens": 2},
            session_id="sdk-1",
        )
    ]
    raw[-2].event["usage"] = {**cache_usage, "output_tokens": 2}

    events = await collect_sdk_response(
        tmp_path,
        (
            *raw,
            result_message(
                usage={
                    "input_tokens": 77,
                    "output_tokens": 44,
                    "cache_read_input_tokens": 900,
                    "cache_creation_input_tokens": 1500,
                }
            ),
        ),
    )

    assert events == [
        InputUsage(5, 700, 11),
        # The aggregate ResultMessage is validated but the raw boundary is published.
        # Text remains independently typed and is not replaced by usage reconciliation.
        TextDelta("done"),
        Completed(
            "end_turn",
            {
                "input_tokens": 5,
                "output_tokens": 2,
                "cache_read_input_tokens": 700,
                "cache_creation_input_tokens": 11,
            },
        ),
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("location", ["message_delta", "assistant", "result"])
async def test_sdk_rejects_malformed_or_mismatched_repeated_cache_usage(
    tmp_path: Path, location: str
) -> None:
    usage = {
        "input_tokens": 5,
        "output_tokens": 2,
        "cache_read_input_tokens": 700,
        "cache_creation_input_tokens": 11,
    }
    raw = list(raw_text_events("done", "sdk-1", input_tokens=5, output_tokens=2))
    raw[0].event["message"]["usage"].update(
        {
            "cache_read_input_tokens": 700,
            "cache_creation_input_tokens": 11,
        }
    )
    assistant = AssistantMessage(
        [TextBlock("done")], "sonnet", usage=dict(usage), session_id="sdk-1"
    )
    raw[3:3] = [assistant]
    raw[-2].event["usage"].update(usage)
    aggregate: dict[str, object] = dict(usage)
    if location == "message_delta":
        raw[-2].event["usage"]["cache_read_input_tokens"] = 701
    elif location == "assistant":
        assert assistant.usage is not None
        assistant.usage["cache_read_input_tokens"] = "secret-usage"
    else:
        aggregate["cache_read_input_tokens"] = "secret-usage"

    with pytest.raises(BackendFailure, match="protocol") as error:
        await collect_sdk_response(
            tmp_path,
            (*raw, result_message(usage=aggregate)),
        )

    assert "secret-usage" not in str(error.value)


def _attach_boundary_usage(
    raw: tuple[object, ...], usage: dict[str, int]
) -> tuple[object, ...]:
    for item in raw:
        if isinstance(item, AssistantMessage):
            item.usage = dict(usage)
        event = getattr(item, "event", None)
        if not isinstance(event, dict):
            continue
        if event.get("type") == "message_start":
            event["message"]["usage"].update(
                {
                    field: value
                    for field, value in usage.items()
                    if field != "output_tokens"
                }
            )
        elif event.get("type") == "message_delta":
            event["usage"].update(usage)
    return raw


def _public_usage(dialect: str, stream: bool, body: bytes, payload: object) -> object:
    if not stream:
        assert isinstance(payload, dict)
        return payload["usage"]
    records = _sse_records(body)
    if dialect == "openai":
        return next(
            item["usage"]
            for _, item in records
            if isinstance(item, dict) and item.get("choices") == []
        )
    start = next(item for event, item in records if event == "message_start")
    delta = next(item for event, item in records if event == "message_delta")
    return {**start["message"]["usage"], **delta["usage"]}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("dialect", "path"),
    [
        ("anthropic", "/v1/messages"),
        ("openai", "/v1/chat/completions"),
    ],
)
@pytest.mark.parametrize("stream", [False, True])
async def test_asgi_repeated_tools_preserve_each_raw_boundary_usage(
    tmp_path: Path, dialect: str, path: str, stream: bool
) -> None:
    tool_usage = {
        "input_tokens": 5,
        "output_tokens": 2,
        "cache_read_input_tokens": 700,
        "cache_creation_input_tokens": 11,
    }
    final_usage = {
        "input_tokens": 2,
        "output_tokens": 4,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 1235,
    }
    sdk_name = "mcp__caller_tools_v1__echo"
    tool_raw = _attach_boundary_usage(
        raw_tool_events(
            (
                ("sdk-a", sdk_name, '{"v":1}'),
                ("sdk-b", sdk_name, '{"v":1}'),
            ),
            "sdk-real",
            input_tokens=5,
            output_tokens=2,
        ),
        tool_usage,
    )
    final_raw = list(
        _attach_boundary_usage(
            raw_text_events(
                "done", "sdk-real", input_tokens=2, output_tokens=4
            ),
            final_usage,
        )
    )
    final_raw.insert(
        3,
        AssistantMessage(
            [TextBlock("done")],
            "sonnet",
            usage=dict(final_usage),
            session_id="sdk-real",
        ),
    )
    aggregate = {
        "input_tokens": 77,
        "output_tokens": 44,
        "cache_read_input_tokens": 900,
        "cache_creation_input_tokens": 1500,
    }
    result = ResultMessage(
        subtype="success",
        duration_ms=0,
        duration_api_ms=0,
        is_error=False,
        num_turns=2,
        session_id="sdk-real",
        stop_reason="end_turn",
        usage=aggregate,
    )
    client = FakeSdkClient(
        responses=(
            (
                *tool_raw,
                UserMessage(
                    [
                        SdkToolResultBlock("sdk-a", "left", False),
                        SdkToolResultBlock("sdk-b", "right", False),
                    ]
                ),
                *final_raw,
                result,
            ),
        )
    )
    app = _sdk_app(tmp_path, client)

    async with lifespan_app(app):
        first = await post_json(app, path, _sdk_tool_body(dialect, stream=stream))
        first_payload = {} if stream else first.json
        calls = _tool_payloads(dialect, first.body, first_payload)
        transcript: list[dict[str, object]] = [
            {"role": "user", "content": "go"},
            _assistant_sdk_message(dialect, calls),
            *_sdk_result_messages(
                dialect, ((calls[0][0], "left"), (calls[1][0], "right"))
            ),
        ]
        final = await post_json(
            app,
            path,
            _sdk_tool_body(dialect, transcript, stream=stream),
        )

    expected_tool = (
        {
            "input_tokens": 5,
            "output_tokens": 2,
            "cache_read_input_tokens": 700,
            "cache_creation_input_tokens": 11,
        }
        if dialect == "anthropic"
        else {
            "prompt_tokens": 716,
            "completion_tokens": 2,
            "total_tokens": 718,
            "prompt_tokens_details": {
                "cached_tokens": 700,
                "cache_write_tokens": 11,
            },
        }
    )
    expected_final = (
        {
            "input_tokens": 2,
            "output_tokens": 4,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 1235,
        }
        if dialect == "anthropic"
        else {
            "prompt_tokens": 1237,
            "completion_tokens": 4,
            "total_tokens": 1241,
            "prompt_tokens_details": {
                "cached_tokens": 0,
                "cache_write_tokens": 1235,
            },
        }
    )
    assert first.status == final.status == 200
    assert len(calls) == 2
    assert _public_usage(dialect, stream, first.body, first_payload) == expected_tool
    assert _public_usage(
        dialect, stream, final.body, {} if stream else final.json
    ) == expected_final


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("dialect", "path"),
    [
        ("anthropic", "/v1/messages"),
        ("openai", "/v1/chat/completions"),
    ],
)
@pytest.mark.parametrize("stream", [False, True])
async def test_asgi_malformed_cache_counter_fails_closed_without_value_leak(
    tmp_path: Path, dialect: str, path: str, stream: bool
) -> None:
    raw = list(raw_text_events("done", "sdk-real"))
    raw[0].event["message"]["usage"]["cache_read_input_tokens"] = "secret-usage"
    client = FakeSdkClient(
        responses=(
            (
                *raw,
                ResultMessage(
                    subtype="success",
                    duration_ms=0,
                    duration_api_ms=0,
                    is_error=False,
                    num_turns=1,
                    session_id="sdk-real",
                    stop_reason="end_turn",
                    usage={"input_tokens": 2, "output_tokens": 1},
                ),
            ),
        )
    )
    app = _sdk_app(tmp_path, client)

    async with lifespan_app(app):
        response = await post_json(app, path, _sdk_tool_body(dialect, stream=stream))

    assert response.status == 502
    assert b"secret-usage" not in response.body
    assert b"Backend request failed" in response.body


@pytest.mark.anyio
async def test_text_boundary_accepts_increasing_interim_output_snapshots(
    tmp_path: Path,
) -> None:
    raw = list(raw_text_events("done", "sdk-1", input_tokens=5, output_tokens=61))
    raw[0].event["message"]["usage"] = {
        "input_tokens": 5,
        "output_tokens": 33,
        "cache_read_input_tokens": 7,
        "cache_creation_input_tokens": 11,
    }
    raw[3:3] = [
        AssistantMessage(
            [TextBlock("done")],
            "sonnet",
            usage={
                "input_tokens": 5,
                "output_tokens": 33,
                "cache_read_input_tokens": 7,
                "cache_creation_input_tokens": 11,
            },
            session_id="sdk-1",
        )
    ]
    raw[-2].event["usage"] = {
        "input_tokens": 5,
        "output_tokens": 61,
        "cache_read_input_tokens": 7,
        "cache_creation_input_tokens": 11,
    }

    events = await collect_sdk_response(
        tmp_path,
        (*raw, result_message(usage={"input_tokens": 5, "output_tokens": 61})),
    )

    assert events[-1] == Completed(
        "end_turn",
        {
            "input_tokens": 5,
            "output_tokens": 61,
            "cache_read_input_tokens": 7,
            "cache_creation_input_tokens": 11,
        },
    )


@pytest.mark.anyio
async def test_text_boundary_rejects_output_snapshot_regression(
    tmp_path: Path,
) -> None:
    raw = list(raw_text_events("done", "sdk-1", input_tokens=5, output_tokens=32))
    raw[0].event["message"]["usage"]["output_tokens"] = 33
    raw[3:3] = [
        AssistantMessage(
            [TextBlock("done")],
            "sonnet",
            usage={"input_tokens": 5, "output_tokens": 33},
            session_id="sdk-1",
        )
    ]

    with pytest.raises(BackendFailure, match="protocol"):
        await collect_sdk_response(
            tmp_path,
            (*raw, result_message(usage={"input_tokens": 5, "output_tokens": 32})),
        )

def test_tool_boundary_accepts_increasing_interim_output_snapshots() -> None:
    sdk_name = "mcp__caller_tools_v1__echo"
    raw = list(raw_tool_events((("sdk-a", sdk_name, '{"v":1}'),), "sdk-1"))
    start = raw[0]
    assert not isinstance(start, AssistantMessage)
    start.event["message"]["usage"] = {
        "input_tokens": 5,
        "output_tokens": 33,
        "cache_read_input_tokens": 7,
        "cache_creation_input_tokens": 11,
    }
    assistant = next(item for item in raw if isinstance(item, AssistantMessage))
    assistant.usage = {
        "input_tokens": 5,
        "output_tokens": 33,
        "cache_read_input_tokens": 7,
        "cache_creation_input_tokens": 11,
    }
    delta = next(
        item
        for item in raw
        if not isinstance(item, AssistantMessage)
        and item.event.get("type") == "message_delta"
    )
    delta.event["usage"] = {
        "input_tokens": 5,
        "output_tokens": 61,
        "cache_read_input_tokens": 7,
        "cache_creation_input_tokens": 11,
    }
    validator = RawSdkMessageValidator(
        (
            ToolDefinition(
                "echo",
                "Echo a value.",
                {
                    "type": "object",
                    "properties": {"v": {"type": "integer"}},
                    "required": ["v"],
                    "additionalProperties": False,
                },
            ),
        )
    )

    for item in raw:
        if isinstance(item, AssistantMessage):
            validator.validate_assistant(item)
        else:
            validator.observe(item.event)

    assert validator.boundary_usage == {
        "input_tokens": 5,
        "output_tokens": 61,
        "cache_read_input_tokens": 7,
        "cache_creation_input_tokens": 11,
    }
