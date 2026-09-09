from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from anthropic import AsyncAnthropic, BadRequestError
from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, UserMessage
from claude_agent_sdk import ToolResultBlock as SdkToolResultBlock
from openai import AsyncOpenAI

from quaylet.app import create_app
from quaylet.domain import Dialect, ToolDefinition
from quaylet.sdk_session import SdkSession
from tests.gateway.fakes import (
    FakeSdkClient,
    FixedTemporaryDirectory,
    raw_text_events,
    raw_tool_events,
)
from tests.gateway.test_sdk_fallback import fallback_notice
from tests.gateway.test_sdk_refusal import _raw_delta, _raw_stop
from tests.integration.pi_gateway_support import serve

_SCHEMA = {
    "type": "object",
    "properties": {"v": {"type": "integer"}},
    "required": ["v"],
    "additionalProperties": False,
}


def _result_message() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=0,
        duration_api_ms=0,
        is_error=False,
        num_turns=3,
        session_id="sdk-official",
        stop_reason="end_turn",
        usage={"input_tokens": 101, "output_tokens": 47},
    )


def _tool_app(tmp_path: Path, client: FakeSdkClient, *, fallback=False):
    def factory(
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
        **kwargs,
    ) -> SdkSession:
        return SdkSession(
            model,
            system,
            directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
            client_factory=lambda options: client.capture_options(options),
            tools=tools,
            dialect=dialect,
            **kwargs,
        )

    return create_app(
        models=("opus-5", "opus-4.8") if fallback else ("sonnet",),
        session_factory=factory,
        refusal_fallback="auto" if fallback else "off",
    )


def _sdk_messages(rounds: int) -> tuple[Any, ...]:
    sdk_name = "mcp__caller_tools__echo"
    messages: list[Any] = [
        *raw_tool_events(
            (
                ("sdk-left", sdk_name, '{"v":1}'),
                ("sdk-right", sdk_name, '{"v":1}'),
            ),
            "sdk-official",
            input_tokens=5,
            output_tokens=2,
        ),
        UserMessage(
            [
                SdkToolResultBlock("sdk-right", "right", False),
                SdkToolResultBlock("sdk-left", "left", False),
            ]
        ),
    ]
    if rounds == 2:
        messages.extend(
            [
                *raw_tool_events(
                    (("sdk-again", sdk_name, '{"v":2}'),),
                    "sdk-official",
                    input_tokens=7,
                    output_tokens=3,
                ),
                UserMessage([SdkToolResultBlock("sdk-again", "round-two", False)]),
            ]
        )
    messages.extend(
        [
            *raw_text_events(
                "left/right/final",
                "sdk-official",
                input_tokens=11,
                output_tokens=4,
            ),
            _result_message(),
        ]
    )
    return tuple(messages)


async def _anthropic_create(
    client: AsyncAnthropic,
    messages: list[dict[str, Any]],
    *,
    stream: bool,
    model: str = "sonnet",
):
    request = {
        "model": model,
        "messages": messages,
        "max_tokens": 128,
        "tools": [
            {
                "name": "echo",
                "description": "echo an integer",
                "input_schema": _SCHEMA,
            }
        ],
    }
    if not stream:
        result = await client.messages.create(**request)
        assert result.model == ("opus-4.8" if model == "opus-5" else "sonnet-5")
        return result
    async with client.messages.stream(**request) as response:
        async for _ in response:
            pass
        result = await response.get_final_message()
        assert result.model == ("opus-4.8" if model == "opus-5" else "sonnet-5")
        return result


def _anthropic_history(message: Any) -> dict[str, Any]:
    dumped = message.model_dump(exclude_none=True)
    content = []
    for block in dumped["content"]:
        fields = (
            ("type", "id", "name", "input")
            if block["type"] == "tool_use"
            else ("type", "text")
        )
        content.append({field: block[field] for field in fields})
    return {"role": dumped["role"], "content": content}


async def _openai_create(
    client: AsyncOpenAI,
    messages: list[dict[str, Any]],
    *,
    stream: bool,
    model: str = "sonnet",
) -> dict[str, Any]:
    request = {
        "model": model,
        "messages": messages,
        "max_tokens": 128,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "description": "echo an integer",
                    "parameters": _SCHEMA,
                },
            }
        ],
    }
    if stream:
        request["stream_options"] = {"include_usage": True}
    response = await client.chat.completions.create(**request, stream=stream)
    if not stream:
        assert response.model == ("opus-4.8" if model == "opus-5" else "sonnet-5")
        dumped = response.model_dump()
        message = dumped["choices"][0]["message"]
        return {
            "message": message,
            "finish_reason": dumped["choices"][0]["finish_reason"],
            "usage": response.usage.model_dump(exclude_none=True),
        }

    content: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    usage: dict[str, int] | None = None
    finish_reason: str | None = None
    async for chunk in response:
        assert chunk.model == ("opus-4.8" if model == "opus-5" else "sonnet-5")
        dumped = chunk.model_dump(exclude_none=True)
        if dumped.get("usage") is not None:
            usage = dumped["usage"]
        for choice in dumped["choices"]:
            finish_reason = choice.get("finish_reason") or finish_reason
            delta = choice["delta"]
            if delta.get("content"):
                content.append(delta["content"])
            for raw_call in delta.get("tool_calls", []):
                target = calls.setdefault(
                    raw_call["index"],
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                target["id"] += raw_call.get("id", "")
                function = raw_call.get("function", {})
                target["function"]["name"] += function.get("name", "")
                target["function"]["arguments"] += function.get("arguments", "")
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content) or None,
    }
    if calls:
        message["tool_calls"] = [calls[index] for index in sorted(calls)]
    assert usage is not None
    return {
        "message": message,
        "finish_reason": finish_reason,
        "usage": usage,
    }


@pytest.mark.anyio
@pytest.mark.parametrize("dialect", ["anthropic", "openai"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("rounds", [1, 2])
@pytest.mark.parametrize("fallback", [False, True])
async def test_official_clients_complete_tool_rounds_over_real_http(
    tmp_path: Path, dialect: str, stream: bool, rounds: int, fallback: bool
) -> None:
    native = list(_sdk_messages(rounds))
    if fallback:
        for index, event in enumerate(native):
            if (
                isinstance(event, StreamEvent)
                and event.event["type"] == "message_start"
            ):
                event.event["message"]["model"] = "claude-opus-4-8"
            if isinstance(event, AssistantMessage):
                native[index] = replace(event, model="claude-opus-4-8")
        native[:0] = [
            *raw_text_events(
                "discarded original output", "sdk-official", model="claude-opus-5"
            )[:4],
            fallback_notice(session_id="sdk-official"),
            _raw_delta(
                session_id="sdk-official", usage={"input_tokens": 2, "output_tokens": 0}
            ),
            replace(_raw_stop(), session_id="sdk-official"),
        ]
    sdk_client = FakeSdkClient(responses=(tuple(native),))
    app = _tool_app(tmp_path, sdk_client, fallback=fallback)
    model = "opus-5" if fallback else "sonnet"

    async def verify_headers(response):
        assert response.headers["x-quaylet-requested-model"] == (
            "opus-5" if fallback else "sonnet-5"
        )
        assert response.headers["x-quaylet-actual-model"] == (
            "opus-4.8" if fallback else "sonnet-5"
        )
        assert response.headers.get("x-quaylet-fallback") == (
            "true" if fallback else None
        )

    async with serve(app) as base_url:
        if dialect == "anthropic":
            async with AsyncAnthropic(
                base_url=base_url,
                http_client=httpx.AsyncClient(
                    event_hooks={"response": [verify_headers]}
                ),
                api_key="local-placeholder",
                max_retries=0,
                timeout=5,
            ) as client:
                messages: list[dict[str, Any]] = [{"role": "user", "content": "go"}]
                first = await _anthropic_create(
                    client, messages, stream=stream, model=model
                )
                replay = await _anthropic_create(
                    client, messages, stream=stream, model=model
                )
                assert replay.content == first.content
                calls = [block for block in first.content if block.type == "tool_use"]
                assert len(calls) == 2
                assert calls[0].id != calls[1].id
                assert calls[0].input == calls[1].input == {"v": 1}
                assert (first.usage.input_tokens, first.usage.output_tokens) == (
                    5,
                    2,
                )
                messages.extend(
                    [
                        _anthropic_history(first),
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": calls[1].id,
                                    "content": "right",
                                },
                                {
                                    "type": "tool_result",
                                    "tool_use_id": calls[0].id,
                                    "content": "left",
                                },
                            ],
                        },
                    ]
                )
                if rounds == 2:
                    second = await _anthropic_create(
                        client, messages, stream=stream, model=model
                    )
                    second_call = next(
                        block for block in second.content if block.type == "tool_use"
                    )
                    assert second_call.input == {"v": 2}
                    assert (
                        second.usage.input_tokens,
                        second.usage.output_tokens,
                    ) == (7, 3)
                    messages.extend(
                        [
                            _anthropic_history(second),
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": second_call.id,
                                        "content": "round-two",
                                    }
                                ],
                            },
                        ]
                    )
                final = await _anthropic_create(
                    client, messages, stream=stream, model=model
                )
                replay = await _anthropic_create(
                    client, messages, stream=stream, model=model
                )
                assert replay.content == final.content
                assert final.stop_reason == "end_turn"
                assert [block.text for block in final.content] == ["left/right/final"]
                assert (
                    final.usage.input_tokens,
                    final.usage.output_tokens,
                ) == (11, 4)
        else:
            async with AsyncOpenAI(
                base_url=f"{base_url}/v1",
                http_client=httpx.AsyncClient(
                    event_hooks={"response": [verify_headers]}
                ),
                api_key="local-placeholder",
                max_retries=0,
                timeout=5,
            ) as client:
                messages = [{"role": "user", "content": "go"}]
                first_openai = await _openai_create(
                    client, messages, stream=stream, model=model
                )
                assert (
                    await _openai_create(client, messages, stream=stream, model=model)
                    == first_openai
                )
                calls = first_openai["message"]["tool_calls"]
                assert len(calls) == 2
                assert calls[0]["id"] != calls[1]["id"]
                assert [
                    json.loads(call["function"]["arguments"]) for call in calls
                ] == [{"v": 1}, {"v": 1}]
                assert first_openai["finish_reason"] == "tool_calls"
                assert first_openai["usage"] == {
                    "prompt_tokens": 5,
                    "completion_tokens": 2,
                    "total_tokens": 7,
                }
                messages.extend(
                    [
                        first_openai["message"],
                        {
                            "role": "tool",
                            "tool_call_id": calls[1]["id"],
                            "content": "right",
                        },
                        {
                            "role": "tool",
                            "tool_call_id": calls[0]["id"],
                            "content": "left",
                        },
                    ]
                )
                if rounds == 2:
                    second_openai = await _openai_create(
                        client, messages, stream=stream, model=model
                    )
                    second_call = second_openai["message"]["tool_calls"][0]
                    assert json.loads(second_call["function"]["arguments"]) == {"v": 2}
                    assert second_openai["usage"] == {
                        "prompt_tokens": 7,
                        "completion_tokens": 3,
                        "total_tokens": 10,
                    }
                    messages.extend(
                        [
                            second_openai["message"],
                            {
                                "role": "tool",
                                "tool_call_id": second_call["id"],
                                "content": "round-two",
                            },
                        ]
                    )
                final_openai = await _openai_create(
                    client, messages, stream=stream, model=model
                )
                assert (
                    await _openai_create(client, messages, stream=stream, model=model)
                    == final_openai
                )
                assert final_openai["finish_reason"] == "stop"
                assert final_openai["message"]["content"] == "left/right/final"
                assert final_openai["usage"] == {
                    "prompt_tokens": 11,
                    "completion_tokens": 4,
                    "total_tokens": 15,
                }

    assert sdk_client.tool_handler_count == 2 + (rounds == 2)
    assert sdk_client.prompts == ["go"]
    assert len(sdk_client.tool_results) == 2 + (rounds == 2)
    assert [result.content[0].text for result in sdk_client.tool_results] == [
        "left",
        "right",
        *(["round-two"] if rounds == 2 else []),
    ]


@pytest.mark.anyio
async def test_anthropic_client_preserves_nonempty_explicit_tool_error(
    tmp_path: Path,
) -> None:
    sdk_name = "mcp__caller_tools__echo"
    messages = (
        *raw_tool_events(
            (("sdk-error", sdk_name, '{"v":1}'),),
            "sdk-official",
            input_tokens=3,
            output_tokens=2,
        ),
        UserMessage([SdkToolResultBlock("sdk-error", "failed clearly", True)]),
        *raw_text_events(
            "error handled", "sdk-official", input_tokens=6, output_tokens=2
        ),
        _result_message(),
    )
    sdk_client = FakeSdkClient(responses=(messages,))
    async with serve(_tool_app(tmp_path, sdk_client)) as base_url:
        async with AsyncAnthropic(
            base_url=base_url,
            api_key="local-placeholder",
            max_retries=0,
            timeout=5,
        ) as client:
            history: list[dict[str, Any]] = [{"role": "user", "content": "go"}]
            first = await _anthropic_create(client, history, stream=False)
            call = next(block for block in first.content if block.type == "tool_use")
            history.extend(
                [
                    _anthropic_history(first),
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": call.id,
                                "content": "failed clearly",
                                "is_error": True,
                            }
                        ],
                    },
                ]
            )
            final = await _anthropic_create(client, history, stream=False)

    assert [block.text for block in final.content] == ["error handled"]
    assert sdk_client.tool_results[0].is_error is True


@pytest.mark.anyio
async def test_anthropic_client_rejects_empty_error_before_sdk_resume(
    tmp_path: Path,
) -> None:
    sdk_name = "mcp__caller_tools__echo"
    messages = (
        *raw_tool_events(
            (("sdk-error", sdk_name, '{"v":1}'),),
            "sdk-official",
            input_tokens=3,
            output_tokens=2,
        ),
        UserMessage([SdkToolResultBlock("sdk-error", "corrected", True)]),
        *raw_text_events(
            "corrected result accepted",
            "sdk-official",
            input_tokens=6,
            output_tokens=2,
        ),
        _result_message(),
    )
    sdk_client = FakeSdkClient(responses=(messages,))
    async with serve(_tool_app(tmp_path, sdk_client)) as base_url:
        async with AsyncAnthropic(
            base_url=base_url,
            api_key="local-placeholder",
            max_retries=0,
            timeout=5,
        ) as client:
            history: list[dict[str, Any]] = [{"role": "user", "content": "go"}]
            first = await _anthropic_create(client, history, stream=False)
            call = next(block for block in first.content if block.type == "tool_use")
            history.append(_anthropic_history(first))
            with pytest.raises(BadRequestError) as rejected:
                await _anthropic_create(
                    client,
                    [
                        *history,
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": call.id,
                                    "content": "",
                                    "is_error": True,
                                }
                            ],
                        },
                    ],
                    stream=False,
                )
            assert rejected.value.status_code == 400
            assert sdk_client.tool_handler_count == 1
            assert sdk_client.tool_results == []
            history.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": call.id,
                            "content": "corrected",
                            "is_error": True,
                        }
                    ],
                }
            )
            final = await _anthropic_create(client, history, stream=False)

    assert [block.text for block in final.content] == ["corrected result accepted"]
    assert sdk_client.tool_handler_count == 1
    assert sdk_client.tool_results[0].is_error is True
