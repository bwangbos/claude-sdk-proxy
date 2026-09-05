from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ResultMessage, UserMessage
from claude_agent_sdk import ToolResultBlock as SdkToolResultBlock

from claude_sdk_proxy.app import create_app
from claude_sdk_proxy.domain import (
    Completed,
    ConversationEvent,
    Dialect,
    InputUsage,
    TextDelta,
    ToolCall,
    ToolDefinition,
    ToolResultBlock,
)
from claude_sdk_proxy.sdk_session import SdkSession
from tests.gateway.asgi_client import (
    _LIFESPAN_STATES,
    lifespan_app,
    post_json,
    post_json_then_disconnect,
)
from tests.gateway.fakes import (
    FakeSdkClient,
    FixedTemporaryDirectory,
    raw_text_events,
    raw_tool_events,
)


class ToolBoundarySession:
    def __init__(
        self, boundary: tuple[ConversationEvent, ...] | None = None
    ) -> None:
        self.prompts: list[str] = []
        self.close_count = 0
        self.closed = asyncio.Event()
        self._failure = asyncio.Event()
        self.boundary = boundary or (
            ToolCall("call_one", "echo", {"value": "same"}),
            Completed("tool_use", {"input_tokens": 7, "output_tokens": 3}),
        )

    async def start(self) -> None:
        pass

    async def stream_generation(
        self, prompt: str
    ) -> AsyncIterator[ConversationEvent]:
        self.prompts.append(prompt)
        for event in self.boundary:
            yield event

    async def submit_tool_results(
        self, results: Iterable[ToolResultBlock]
    ) -> None:
        raise AssertionError(f"unexpected results: {tuple(results)!r}")

    async def wait_failure(self) -> None:
        await self._failure.wait()

    async def close(self) -> None:
        self.close_count += 1
        self.closed.set()


class RepeatedRoundSession(ToolBoundarySession):
    def __init__(
        self, boundaries: tuple[tuple[ConversationEvent, ...], ...]
    ) -> None:
        super().__init__(())
        self.boundaries = boundaries
        self.results: list[tuple[ToolResultBlock, ...]] = []
        self.handler_count = 0
        self._resume = asyncio.Event()

    async def stream_generation(
        self, prompt: str
    ) -> AsyncIterator[ConversationEvent]:
        self.prompts.append(prompt)
        for index, boundary in enumerate(self.boundaries):
            if index:
                await self._resume.wait()
                self._resume.clear()
            for event in boundary:
                yield event

    async def submit_tool_results(
        self, results: Iterable[ToolResultBlock]
    ) -> None:
        self.handler_count += 1
        self.results.append(tuple(results))
        self._resume.set()


class BlockingToolSession(ToolBoundarySession):
    def __init__(self, *, after_delta: bool = False) -> None:
        super().__init__()
        self.after_delta = after_delta
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def start(self) -> None:
        if not self.after_delta:
            self.entered.set()
            await self.release.wait()

    async def stream_generation(
        self, prompt: str
    ) -> AsyncIterator[ConversationEvent]:
        self.prompts.append(prompt)
        if self.after_delta:
            yield InputUsage(13)
            yield TextDelta("partial")
            self.entered.set()
            await self.release.wait()
        yield ToolCall("toolu_one", "echo", {"value": "same"})
        yield Completed("tool_use", {"input_tokens": 13, "output_tokens": 6})


def tool_body(dialect: str, *, stream: bool = False) -> dict[str, object]:
    common: dict[str, object] = {
        "model": "sonnet",
        "messages": [{"role": "user", "content": "go"}],
        "max_tokens": 128,
        "stream": stream,
    }
    schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    if dialect == "anthropic":
        common["tools"] = [
            {"name": "echo", "description": "echo", "input_schema": schema}
        ]
    else:
        common["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "description": "echo",
                    "parameters": schema,
                },
            }
        ]
    return common


def _body_with_messages(
    dialect: str, messages: list[dict[str, object]], *, stream: bool
) -> dict[str, object]:
    body = tool_body(dialect, stream=stream)
    body["messages"] = messages
    if dialect == "openai" and stream:
        body["stream_options"] = {"include_usage": True}
    return body


def _assistant_call_message(
    dialect: str, calls: tuple[tuple[str, str], ...]
) -> dict[str, object]:
    if dialect == "anthropic":
        return {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": identifier,
                    "name": "echo",
                    "input": {"value": value},
                }
                for identifier, value in calls
            ],
        }
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": identifier,
                "type": "function",
                "function": {
                    "name": "echo",
                    "arguments": json.dumps(
                        {"value": value}, separators=(",", ":")
                    ),
                },
            }
            for identifier, value in calls
        ],
    }


def _result_messages(
    dialect: str, results: tuple[tuple[str, str], ...]
) -> list[dict[str, object]]:
    if dialect == "anthropic":
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": identifier,
                        "content": value,
                    }
                    for identifier, value in results
                ],
            }
        ]
    return [
        {"role": "tool", "tool_call_id": identifier, "content": value}
        for identifier, value in results
    ]


def _public_boundary(
    dialect: str, response_body: bytes, response_json: object, *, stream: bool
) -> tuple[tuple[str, ...], tuple[int, int], str | None]:
    if not stream:
        assert isinstance(response_json, dict)
        if dialect == "anthropic":
            calls = tuple(
                block["id"]
                for block in response_json["content"]
                if block["type"] == "tool_use"
            )
            usage = response_json["usage"]
            text = "".join(
                block["text"]
                for block in response_json["content"]
                if block["type"] == "text"
            )
            return calls, (usage["input_tokens"], usage["output_tokens"]), text
        choice = response_json["choices"][0]
        message = choice["message"]
        calls = tuple(call["id"] for call in message.get("tool_calls", []))
        usage = response_json["usage"]
        return calls, (
            usage["prompt_tokens"],
            usage["completion_tokens"],
        ), message["content"] or ""
    records = [
        json.loads(line.removeprefix(b"data: "))
        for line in response_body.splitlines()
        if line.startswith(b"data: ") and line != b"data: [DONE]"
    ]
    if dialect == "anthropic":
        calls = tuple(
            item["content_block"]["id"]
            for item in records
            if item["type"] == "content_block_start"
            and item["content_block"]["type"] == "tool_use"
        )
        start = records[0]["message"]["usage"]["input_tokens"]
        terminal = next(item for item in records if item["type"] == "message_delta")
        text = "".join(
            item["delta"]["text"]
            for item in records
            if item["type"] == "content_block_delta"
            and item["delta"]["type"] == "text_delta"
        )
        return calls, (start, terminal["usage"]["output_tokens"]), text
    calls = tuple(
        item["choices"][0]["delta"]["tool_calls"][0]["id"]
        for item in records
        if item["choices"] and "tool_calls" in item["choices"][0]["delta"]
    )
    usage = next(item["usage"] for item in records if not item["choices"])
    text = "".join(
        item["choices"][0]["delta"].get("content", "")
        for item in records
        if item["choices"]
    )
    return calls, (usage["prompt_tokens"], usage["completion_tokens"]), text


def _sdk_tool_body(
    dialect: str,
    messages: list[dict[str, object]] | None = None,
    *,
    stream: bool,
) -> dict[str, object]:
    body: dict[str, object] = {
        "model": "sonnet",
        "messages": messages or [{"role": "user", "content": "go"}],
        "max_tokens": 128,
        "stream": stream,
    }
    schema = {
        "type": "object",
        "properties": {"v": {"type": "integer"}},
        "required": ["v"],
        "additionalProperties": False,
    }
    if dialect == "anthropic":
        body["tools"] = [
            {"name": "echo", "description": "echo integer", "input_schema": schema}
        ]
    else:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "description": "echo integer",
                    "parameters": schema,
                },
            }
        ]
        if stream:
            body["stream_options"] = {"include_usage": True}
    return body


def _sdk_app(tmp_path: Path, client: FakeSdkClient):
    def factory(
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
    ) -> SdkSession:
        return SdkSession(
            model,
            system,
            directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
            client_factory=lambda options: client.capture_options(options),
            tools=tools,
            dialect=dialect,
        )

    return create_app(models=("sonnet",), session_factory=factory)


def _result_message(usage: dict[str, int]) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=0,
        duration_api_ms=0,
        is_error=False,
        num_turns=3,
        session_id="sdk-real",
        stop_reason="end_turn",
        usage=usage,
    )


def _tool_payloads(dialect: str, response_body: bytes, response_json: object):
    if response_json:
        assert isinstance(response_json, dict)
        if dialect == "anthropic":
            return tuple(
                (block["id"], block["input"])
                for block in response_json["content"]
                if block["type"] == "tool_use"
            )
        return tuple(
            (call["id"], json.loads(call["function"]["arguments"]))
            for call in response_json["choices"][0]["message"]["tool_calls"]
        )
    records = _sse_records(response_body)
    if dialect == "anthropic":
        starts = {
            item["index"]: item["content_block"]
            for _, item in records
            if isinstance(item, dict) and item.get("type") == "content_block_start"
        }
        deltas = {
            item["index"]: item["delta"]["partial_json"]
            for _, item in records
            if isinstance(item, dict)
            and item.get("type") == "content_block_delta"
            and item["delta"]["type"] == "input_json_delta"
        }
        return tuple(
            (block["id"], json.loads(deltas[index]))
            for index, block in sorted(starts.items())
            if block["type"] == "tool_use"
        )
    return tuple(
        (
            item["choices"][0]["delta"]["tool_calls"][0]["id"],
            json.loads(
                item["choices"][0]["delta"]["tool_calls"][0]["function"][
                    "arguments"
                ]
            ),
        )
        for _, item in records
        if isinstance(item, dict)
        and item.get("choices")
        and "tool_calls" in item["choices"][0]["delta"]
    )


def _assistant_sdk_message(
    dialect: str, calls: tuple[tuple[str, dict[str, int]], ...]
) -> dict[str, object]:
    if dialect == "anthropic":
        return {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": identifier,
                    "name": "echo",
                    "input": arguments,
                }
                for identifier, arguments in calls
            ],
        }
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": identifier,
                "type": "function",
                "function": {
                    "name": "echo",
                    "arguments": json.dumps(
                        arguments, sort_keys=True, separators=(",", ":")
                    ),
                },
            }
            for identifier, arguments in calls
        ],
    }


def _sdk_result_messages(
    dialect: str, results: tuple[tuple[str, str], ...]
) -> list[dict[str, object]]:
    if dialect == "anthropic":
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": identifier,
                        "content": value,
                    }
                    for identifier, value in results
                ],
            }
        ]
    return [
        {"role": "tool", "tool_call_id": identifier, "content": value}
        for identifier, value in results
    ]


def _sse_records(body: bytes) -> list[tuple[str | None, Any]]:
    records: list[tuple[str | None, Any]] = []
    event: str | None = None
    for line in body.splitlines():
        if line.startswith(b"event: "):
            event = line.removeprefix(b"event: ").decode()
        elif line.startswith(b"data: "):
            raw = line.removeprefix(b"data: ")
            records.append((event, "[DONE]" if raw == b"[DONE]" else json.loads(raw)))
            event = None
    return records


def _normalize_response_id(records: list[tuple[str | None, Any]]):
    copied = json.loads(json.dumps(records))
    for _, item in copied:
        if not isinstance(item, dict):
            continue
        if "id" in item:
            item["id"] = "<response>"
        message = item.get("message")
        if isinstance(message, dict) and "id" in message:
            message["id"] = "<response>"
    return copied


def _normalized_http_payload(response_json: object) -> object:
    copied = json.loads(json.dumps(response_json))
    if isinstance(copied, dict) and "id" in copied:
        copied["id"] = "<response>"
    return copied


def _assert_exact_tool_sse(
    dialect: str,
    body: bytes,
    calls: tuple[tuple[str, dict[str, int]], ...],
    usage: tuple[int, int],
) -> None:
    if dialect == "anthropic":
        expected: list[list[object]] = [
            [
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "<response>",
                        "type": "message",
                        "role": "assistant",
                        "model": "sonnet",
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": usage[0], "output_tokens": 0},
                    },
                },
            ]
        ]
        for index, (identifier, arguments) in enumerate(calls):
            expected.extend(
                [
                    [
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": index,
                            "content_block": {
                                "type": "tool_use",
                                "id": identifier,
                                "name": "echo",
                                "input": {},
                            },
                        },
                    ],
                    [
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": json.dumps(
                                    arguments, sort_keys=True, separators=(",", ":")
                                ),
                            },
                        },
                    ],
                    [
                        "content_block_stop",
                        {"type": "content_block_stop", "index": index},
                    ],
                ]
            )
        expected.extend(
            [
                [
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {
                            "stop_reason": "tool_use",
                            "stop_sequence": None,
                        },
                        "usage": {"output_tokens": usage[1]},
                    },
                ],
                ["message_stop", {"type": "message_stop"}],
            ]
        )
        assert _normalize_response_id(_sse_records(body)) == expected
        return
    first = {
        "id": "<response>",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "sonnet",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": ""},
                "logprobs": None,
                "finish_reason": None,
            }
        ],
    }
    expected = [[None, first]]
    for index, (identifier, arguments) in enumerate(calls):
        expected.append(
            [
                None,
                {
                    **first,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": index,
                                        "id": identifier,
                                        "type": "function",
                                        "function": {
                                            "name": "echo",
                                            "arguments": json.dumps(
                                                arguments,
                                                sort_keys=True,
                                                separators=(",", ":"),
                                            ),
                                        },
                                    }
                                ]
                            },
                            "logprobs": None,
                            "finish_reason": None,
                        }
                    ],
                },
            ]
        )
    expected.extend(
        [
            [
                None,
                {
                    **first,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "logprobs": None,
                            "finish_reason": "tool_calls",
                        }
                    ],
                },
            ],
            [
                None,
                {
                    "id": "<response>",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "sonnet",
                    "choices": [],
                    "usage": {
                        "prompt_tokens": usage[0],
                        "completion_tokens": usage[1],
                        "total_tokens": sum(usage),
                    },
                },
            ],
            [None, "[DONE]"],
        ]
    )
    assert _normalize_response_id(_sse_records(body)) == expected


def _assert_exact_final_sse(dialect: str, body: bytes) -> None:
    records = _normalize_response_id(_sse_records(body))
    if dialect == "anthropic":
        assert records == [
            [
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "<response>",
                        "type": "message",
                        "role": "assistant",
                        "model": "sonnet",
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 11, "output_tokens": 0},
                    },
                },
            ],
            [
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ],
            [
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "real-sdk-final"},
                },
            ],
            [
                "content_block_stop",
                {"type": "content_block_stop", "index": 0},
            ],
            [
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 4},
                },
            ],
            ["message_stop", {"type": "message_stop"}],
        ]
        return
    chunk = {
        "id": "<response>",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "sonnet",
    }
    assert records == [
        [
            None,
            {
                **chunk,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "logprobs": None,
                        "finish_reason": None,
                    }
                ],
            },
        ],
        [
            None,
            {
                **chunk,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "real-sdk-final"},
                        "logprobs": None,
                        "finish_reason": None,
                    }
                ],
            },
        ],
        [
            None,
            {
                **chunk,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "logprobs": None,
                        "finish_reason": "stop",
                    }
                ],
            },
        ],
        [
            None,
            {
                **chunk,
                "choices": [],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 4,
                    "total_tokens": 15,
                },
            },
        ],
        [None, "[DONE]"],
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("dialect", "path"),
    [
        ("anthropic", "/v1/messages"),
        ("openai", "/v1/chat/completions"),
    ],
)
async def test_nonstream_tool_boundary_preserves_public_call(
    dialect: str, path: str
) -> None:
    session = ToolBoundarySession()

    def factory(
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
    ) -> ToolBoundarySession:
        del model, system, tools, dialect
        return session

    app = create_app(models=("sonnet",), session_factory=factory)
    async with lifespan_app(app):
        response = await post_json(app, path, tool_body(dialect))

    assert response.status == 200
    if dialect == "anthropic":
        assert response.json["content"] == [
            {
                "type": "tool_use",
                "id": "call_one",
                "name": "echo",
                "input": {"value": "same"},
            }
        ]
        assert response.json["stop_reason"] == "tool_use"
        assert response.json["usage"] == {"input_tokens": 7, "output_tokens": 3}
    else:
        choice = response.json["choices"][0]
        assert choice["message"] == {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_one",
                    "type": "function",
                    "function": {
                        "name": "echo",
                        "arguments": '{"value":"same"}',
                    },
                }
            ],
        }
        assert choice["finish_reason"] == "tool_calls"
        assert response.json["usage"] == {
            "prompt_tokens": 7,
            "completion_tokens": 3,
            "total_tokens": 10,
        }


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("dialect", "path"),
    [
        ("anthropic", "/v1/messages"),
        ("openai", "/v1/chat/completions"),
    ],
)
async def test_stream_tool_boundary_uses_one_response_local_index_state(
    dialect: str, path: str
) -> None:
    session = ToolBoundarySession(
        (
            TextDelta("thinking"),
            ToolCall("call_left", "echo", {"value": "left"}),
            ToolCall("call_right", "echo", {"value": "right"}),
            Completed("tool_use", {"input_tokens": 11, "output_tokens": 5}),
        )
    )

    def factory(
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
    ) -> ToolBoundarySession:
        del model, system, tools, dialect
        return session

    app = create_app(models=("sonnet",), session_factory=factory)
    body = tool_body(dialect, stream=True)
    if dialect == "openai":
        body["stream_options"] = {"include_usage": True}
    async with lifespan_app(app):
        response = await post_json(app, path, body)

    assert response.status == 200
    if dialect == "anthropic":
        records = [
            json.loads(line.removeprefix(b"data: "))
            for line in response.body.splitlines()
            if line.startswith(b"data: ")
        ]
        starts = [
            item for item in records if item["type"] == "content_block_start"
        ]
        assert starts == [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "call_left",
                    "name": "echo",
                    "input": {},
                },
            },
            {
                "type": "content_block_start",
                "index": 2,
                "content_block": {
                    "type": "tool_use",
                    "id": "call_right",
                    "name": "echo",
                    "input": {},
                },
            },
        ]
        delta = next(item for item in records if item["type"] == "message_delta")
        assert delta["delta"]["stop_reason"] == "tool_use"
        assert delta["usage"] == {"output_tokens": 5}
    else:
        records = [
            json.loads(line.removeprefix(b"data: "))
            for line in response.body.splitlines()
            if line.startswith(b"data: ") and line != b"data: [DONE]"
        ]
        calls = [
            item["choices"][0]["delta"]["tool_calls"][0]
            for item in records
            if item["choices"]
            and "tool_calls" in item["choices"][0]["delta"]
        ]
        assert calls == [
            {
                "index": 0,
                "id": "call_left",
                "type": "function",
                "function": {"name": "echo", "arguments": '{"value":"left"}'},
            },
            {
                "index": 1,
                "id": "call_right",
                "type": "function",
                "function": {"name": "echo", "arguments": '{"value":"right"}'},
            },
        ]
        terminal = next(
            item
            for item in records
            if item["choices"] and item["choices"][0]["finish_reason"] is not None
        )
        assert terminal["choices"][0]["finish_reason"] == "tool_calls"
        usage = next(item for item in records if not item["choices"])["usage"]
        assert usage == {
            "prompt_tokens": 11,
            "completion_tokens": 5,
            "total_tokens": 16,
        }


@pytest.mark.anyio
async def test_parked_tool_boundary_expires_through_http_app_configuration() -> None:
    session = ToolBoundarySession()

    def factory(
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
    ) -> ToolBoundarySession:
        del model, system, tools, dialect
        return session

    app = create_app(
        models=("sonnet",),
        session_factory=factory,
        tool_result_timeout_seconds=0.001,
    )
    async with lifespan_app(app):
        response = await post_json(app, "/v1/messages", tool_body("anthropic"))
        await asyncio.wait_for(session.closed.wait(), timeout=0.5)

    assert response.status == 200
    assert session.close_count == 1


@pytest.mark.anyio
async def test_expired_tool_result_submission_is_http_504() -> None:
    session = RepeatedRoundSession(
        (
            (
                ToolCall("toolu_one", "echo", {"value": "same"}),
                Completed("tool_use", {"input_tokens": 3, "output_tokens": 2}),
            ),
            (
                TextDelta("unused"),
                Completed("end_turn", {"input_tokens": 4, "output_tokens": 1}),
            ),
        )
    )

    def factory(
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
    ) -> RepeatedRoundSession:
        del model, system, tools, dialect
        return session

    app = create_app(models=("sonnet",), session_factory=factory)
    headers = {"X-Claude-Proxy-Session": "expired-results"}
    messages = [
        {"role": "user", "content": "go"},
        _assistant_call_message("anthropic", (("toolu_one", "same"),)),
        *_result_messages("anthropic", (("toolu_one", "late"),)),
    ]
    async with lifespan_app(app):
        boundary = await post_json(
            app, "/v1/messages", tool_body("anthropic"), headers
        )
        registry = _LIFESPAN_STATES[app]["registry"]
        actor = registry._explicit["expired-results"]
        actor._wait_deadline = asyncio.get_running_loop().time() - 1
        expired = await post_json(
            app,
            "/v1/messages",
            _body_with_messages("anthropic", messages, stream=False),
            headers,
        )

    assert boundary.status == 200
    assert expired.status == 504
    assert expired.json["error"]["type"] == "backend_timeout"
    assert session.handler_count == 0
    assert session.close_count == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("dialect", "path"),
    [
        ("anthropic", "/v1/messages"),
        ("openai", "/v1/chat/completions"),
    ],
)
@pytest.mark.parametrize("stream", [False, True])
async def test_http_repeated_tool_rounds_reverse_parallel_results_and_replay(
    dialect: str, path: str, stream: bool
) -> None:
    prefix = "toolu" if dialect == "anthropic" else "call"
    first_calls = ((f"{prefix}_left", "same"), (f"{prefix}_right", "same"))
    second_calls = ((f"{prefix}_again", "again"),)
    session = RepeatedRoundSession(
        (
            (
                InputUsage(5),
                ToolCall(first_calls[0][0], "echo", {"value": "same"}),
                ToolCall(first_calls[1][0], "echo", {"value": "same"}),
                Completed("tool_use", {"input_tokens": 5, "output_tokens": 2}),
            ),
            (
                InputUsage(7),
                ToolCall(second_calls[0][0], "echo", {"value": "again"}),
                Completed("tool_use", {"input_tokens": 7, "output_tokens": 3}),
            ),
            (
                InputUsage(11),
                TextDelta("left-result/right-result/final"),
                Completed("end_turn", {"input_tokens": 11, "output_tokens": 4}),
            ),
        )
    )

    def factory(
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
    ) -> RepeatedRoundSession:
        del model, system, tools, dialect
        return session

    app = create_app(models=("sonnet",), session_factory=factory)
    headers = {"X-Claude-Proxy-Session": f"rounds-{dialect}-{stream}"}
    messages: list[dict[str, object]] = [{"role": "user", "content": "go"}]
    async with lifespan_app(app):
        first = await post_json(
            app,
            path,
            _body_with_messages(dialect, messages, stream=stream),
            headers,
        )
        messages.append(_assistant_call_message(dialect, first_calls))
        messages.extend(
            _result_messages(
                dialect,
                (
                    (first_calls[1][0], "right-result"),
                    (first_calls[0][0], "left-result"),
                ),
            )
        )
        second = await post_json(
            app,
            path,
            _body_with_messages(dialect, messages, stream=stream),
            headers,
        )
        messages.append(_assistant_call_message(dialect, second_calls))
        messages.extend(
            _result_messages(dialect, ((second_calls[0][0], "round-two"),))
        )
        final_body = _body_with_messages(dialect, messages, stream=stream)
        final = await post_json(app, path, final_body, headers)
        handler_count = session.handler_count
        replay = await post_json(app, path, final_body, headers)

    assert first.status == second.status == final.status == replay.status == 200
    first_json = {} if stream else first.json
    second_json = {} if stream else second.json
    final_json = {} if stream else final.json
    replay_json = {} if stream else replay.json
    assert _public_boundary(dialect, first.body, first_json, stream=stream) == (
        tuple(item[0] for item in first_calls),
        (5, 2),
        "",
    )
    assert _public_boundary(dialect, second.body, second_json, stream=stream) == (
        tuple(item[0] for item in second_calls),
        (7, 3),
        "",
    )
    final_public = _public_boundary(
        dialect, final.body, final_json, stream=stream
    )
    assert final_public == (
        (),
        (11, 4),
        "left-result/right-result/final",
    )
    assert _public_boundary(
        dialect, replay.body, replay_json, stream=stream
    ) == final_public
    assert session.results == [
        (
            ToolResultBlock(first_calls[0][0], ("left-result",), False),
            ToolResultBlock(first_calls[1][0], ("right-result",), False),
        ),
        (ToolResultBlock(second_calls[0][0], ("round-two",), False),),
    ]
    assert handler_count == session.handler_count == 2
    assert session.prompts == ["go"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("dialect", "path"),
    [
        ("anthropic", "/v1/messages"),
        ("openai", "/v1/chat/completions"),
    ],
)
@pytest.mark.parametrize("stream", [False, True])
async def test_real_sdk_http_boundaries_usage_bridge_replay_and_wire(
    tmp_path: Path, dialect: str, path: str, stream: bool
) -> None:
    sdk_name = "mcp__caller_tools_v1__echo"
    messages = (
        *raw_tool_events(
            (("sdk-a", sdk_name, '{"v":1}'), ("sdk-b", sdk_name, '{"v":1}')),
            "sdk-real",
            input_tokens=5,
            output_tokens=2,
        ),
        UserMessage(
            [
                SdkToolResultBlock("sdk-b", "right-result", False),
                SdkToolResultBlock("sdk-a", "left-result", False),
            ]
        ),
        *raw_tool_events(
            (("sdk-c", sdk_name, '{"v":2}'),),
            "sdk-real",
            input_tokens=7,
            output_tokens=3,
        ),
        UserMessage([SdkToolResultBlock("sdk-c", "round-two", False)]),
        *raw_text_events(
            "real-sdk-final", "sdk-real", input_tokens=11, output_tokens=4
        ),
        _result_message({"input_tokens": 101, "output_tokens": 47}),
    )
    client = FakeSdkClient(responses=(messages,))
    app = _sdk_app(tmp_path, client)
    initial_body = _sdk_tool_body(dialect, stream=stream)
    async with lifespan_app(app):
        first = await post_json(app, path, initial_body)
        first_json = {} if stream else first.json
        first_calls = _tool_payloads(dialect, first.body, first_json)
        assert first_calls[0][0] != first_calls[1][0]
        assert first_calls[0][1] == first_calls[1][1] == {"v": 1}
        assert client.tool_handler_count == 2

        replay = await post_json(app, path, initial_body)
        assert client.tool_handler_count == 2
        if stream:
            assert _normalize_response_id(_sse_records(replay.body)) == (
                _normalize_response_id(_sse_records(first.body))
            )
        else:
            assert _normalized_http_payload(replay.json) == (
                _normalized_http_payload(first.json)
            )

        transcript: list[dict[str, object]] = [
            {"role": "user", "content": "go"},
            _assistant_sdk_message(dialect, first_calls),
            *_sdk_result_messages(
                dialect,
                (
                    (first_calls[1][0], "right-result"),
                    (first_calls[0][0], "left-result"),
                ),
            ),
        ]
        second = await post_json(
            app, path, _sdk_tool_body(dialect, transcript, stream=stream)
        )
        second_json = {} if stream else second.json
        second_calls = _tool_payloads(dialect, second.body, second_json)
        transcript.extend(
            [
                _assistant_sdk_message(dialect, second_calls),
                *_sdk_result_messages(
                    dialect, ((second_calls[0][0], "round-two"),)
                ),
            ]
        )
        final = await post_json(
            app, path, _sdk_tool_body(dialect, transcript, stream=stream)
        )
        final_json = {} if stream else final.json

    assert first.status == replay.status == second.status == final.status == 200
    assert _public_boundary(dialect, first.body, first_json, stream=stream) == (
        tuple(item[0] for item in first_calls),
        (5, 2),
        "",
    )
    assert _public_boundary(dialect, second.body, second_json, stream=stream) == (
        tuple(item[0] for item in second_calls),
        (7, 3),
        "",
    )
    assert _public_boundary(dialect, final.body, final_json, stream=stream) == (
        (),
        (11, 4),
        "real-sdk-final",
    )
    if stream:
        _assert_exact_tool_sse(dialect, first.body, first_calls, (5, 2))
        _assert_exact_tool_sse(dialect, second.body, second_calls, (7, 3))
        _assert_exact_final_sse(dialect, final.body)
    assert client.tool_handler_count == 3
    assert client.prompts == ["go"]
    assert len(client.tool_results) == 3


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("dialect", "path", "header_chunk"),
    [
        ("anthropic", "/v1/messages", b"event: message_start\n"),
        ("openai", "/v1/chat/completions", b'"role":"assistant"'),
    ],
)
async def test_real_sdk_disconnect_after_usage_before_tool_commit_aborts(
    tmp_path: Path, dialect: str, path: str, header_chunk: bytes
) -> None:
    raw_blocked = asyncio.Event()
    raw_release = asyncio.Event()
    sdk_name = "mcp__caller_tools_v1__echo"
    client = FakeSdkClient(
        responses=(
            raw_tool_events(
                (("sdk-private-call", sdk_name, '{"v":1}'),),
                "sdk-private-session",
                input_tokens=5,
                output_tokens=2,
            ),
        ),
        message_barriers={1: (raw_blocked, raw_release)},
    )
    app = _sdk_app(tmp_path, client)
    async with lifespan_app(app):
        response = await post_json(
            app,
            path,
            _sdk_tool_body(dialect, stream=True),
            disconnect_after_body_contains=header_chunk,
        )
        await asyncio.wait_for(client.disconnected.wait(), timeout=0.5)
        registry = _LIFESPAN_STATES[app]["registry"]
        assert not registry._implicit

    assert response.status == 200
    assert raw_blocked.is_set()
    assert client.tool_handler_count == 0
    assert client.disconnect_count == 1
    assert b"sdk-private-call" not in response.body
    assert b"sdk-private-session" not in response.body


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("dialect", "path", "terminal_chunk"),
    [
        ("anthropic", "/v1/messages", b"event: message_stop\n"),
        ("openai", "/v1/chat/completions", b"data: [DONE]\n\n"),
    ],
)
async def test_real_sdk_disconnect_after_complete_tool_wire_keeps_actor(
    tmp_path: Path, dialect: str, path: str, terminal_chunk: bytes
) -> None:
    sdk_name = "mcp__caller_tools_v1__echo"
    messages = (
        *raw_tool_events(
            (("sdk-private-call", sdk_name, '{"v":1}'),),
            "sdk-private-session",
            input_tokens=5,
            output_tokens=2,
        ),
        UserMessage([SdkToolResultBlock("sdk-private-call", "result", False)]),
        *raw_text_events(
            "continued", "sdk-private-session", input_tokens=8, output_tokens=3
        ),
        ResultMessage(
            subtype="success",
            duration_ms=0,
            duration_api_ms=0,
            is_error=False,
            num_turns=2,
            session_id="sdk-private-session",
            stop_reason="end_turn",
            usage={"input_tokens": 88, "output_tokens": 33},
        ),
    )
    client = FakeSdkClient(responses=(messages,))
    app = _sdk_app(tmp_path, client)
    async with lifespan_app(app):
        boundary = await post_json(
            app,
            path,
            _sdk_tool_body(dialect, stream=True),
            disconnect_after_body_contains=terminal_chunk,
        )
        calls = _tool_payloads(dialect, boundary.body, {})
        assert len(calls) == 1
        assert client.tool_handler_count == 1
        assert client.disconnect_count == 0
        transcript = [
            {"role": "user", "content": "go"},
            _assistant_sdk_message(dialect, calls),
            *_sdk_result_messages(dialect, ((calls[0][0], "result"),)),
        ]
        continued = await post_json(
            app, path, _sdk_tool_body(dialect, transcript, stream=False)
        )

    assert boundary.status == continued.status == 200
    assert _public_boundary(dialect, continued.body, continued.json, stream=False) == (
        (),
        (8, 3),
        "continued",
    )
    assert client.tool_handler_count == 1
    assert client.disconnect_count == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("dialect", "path"),
    [
        ("anthropic", "/v1/messages"),
        ("openai", "/v1/chat/completions"),
    ],
)
@pytest.mark.parametrize("stream", [False, True])
async def test_real_sdk_oversized_raw_tool_arguments_are_redacted_http_errors(
    tmp_path: Path, dialect: str, path: str, stream: bool
) -> None:
    secret = "oversized-model-secret-" + "z" * (300 * 1024)
    encoded = json.dumps({"v": secret}, separators=(",", ":"))
    client = FakeSdkClient(
        responses=(
            raw_tool_events(
                (
                    (
                        "sdk-internal-oversized",
                        "mcp__caller_tools_v1__echo",
                        encoded,
                    ),
                ),
                "sdk-session-secret",
                input_tokens=5,
                output_tokens=2,
            ),
        )
    )
    app = _sdk_app(tmp_path, client)
    body = _sdk_tool_body(dialect, stream=stream)
    if dialect == "anthropic":
        body["tools"][0]["input_schema"]["properties"]["v"] = {  # type: ignore[index]
            "type": "string"
        }
    else:
        body["tools"][0]["function"]["parameters"]["properties"]["v"] = {  # type: ignore[index]
            "type": "string"
        }
    async with lifespan_app(app):
        response = await post_json(app, path, body)

    if stream:
        assert response.status == 200
        assert b"backend_error" in response.body
    else:
        assert response.status == 502
        assert response.json["error"][
            "type" if dialect == "anthropic" else "code"
        ] == "backend_error"
    assert b"oversized-model-secret" not in response.body
    assert b"sdk-internal-oversized" not in response.body
    assert b"sdk-session-secret" not in response.body
    assert client.tool_handler_count == 0
    assert client.disconnect_count == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("dialect", "path"),
    [
        ("anthropic", "/v1/messages"),
        ("openai", "/v1/chat/completions"),
    ],
)
@pytest.mark.parametrize("stream", [False, True])
async def test_http_single_tool_round_completes_in_each_mode(
    dialect: str, path: str, stream: bool
) -> None:
    identifier = "toolu_one" if dialect == "anthropic" else "call_one"
    session = RepeatedRoundSession(
        (
            (
                InputUsage(2),
                ToolCall(identifier, "echo", {"value": "same"}),
                Completed("tool_use", {"input_tokens": 2, "output_tokens": 1}),
            ),
            (
                InputUsage(6),
                TextDelta("one-round-final"),
                Completed("end_turn", {"input_tokens": 6, "output_tokens": 2}),
            ),
        )
    )

    def factory(
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
    ) -> RepeatedRoundSession:
        del model, system, tools, dialect
        return session

    app = create_app(models=("sonnet",), session_factory=factory)
    messages = [
        {"role": "user", "content": "go"},
        _assistant_call_message(dialect, ((identifier, "same"),)),
        *_result_messages(dialect, ((identifier, "one-result"),)),
    ]
    async with lifespan_app(app):
        boundary = await post_json(
            app,
            path,
            _body_with_messages(
                dialect, [{"role": "user", "content": "go"}], stream=stream
            ),
        )
        final = await post_json(
            app,
            path,
            _body_with_messages(dialect, messages, stream=stream),
        )

    assert boundary.status == final.status == 200
    boundary_json = {} if stream else boundary.json
    final_json = {} if stream else final.json
    assert _public_boundary(
        dialect, boundary.body, boundary_json, stream=stream
    ) == ((identifier,), (2, 1), "")
    assert _public_boundary(dialect, final.body, final_json, stream=stream) == (
        (),
        (6, 2),
        "one-round-final",
    )
    assert session.handler_count == 1
    assert session.prompts == ["go"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "bad_results",
    [
        (("toolu_left", "left"),),
        (("toolu_left", "left"), ("toolu_left", "duplicate")),
        (("toolu_left", "left"), ("toolu_unknown", "unknown")),
    ],
)
async def test_invalid_caller_results_are_400_and_corrected_retry_still_works(
    bad_results: tuple[tuple[str, str], ...]
) -> None:
    calls = (("toolu_left", "same"), ("toolu_right", "same"))
    session = RepeatedRoundSession(
        (
            (
                ToolCall(calls[0][0], "echo", {"value": "same"}),
                ToolCall(calls[1][0], "echo", {"value": "same"}),
                Completed("tool_use", {"input_tokens": 3, "output_tokens": 2}),
            ),
            (
                TextDelta("corrected"),
                Completed("end_turn", {"input_tokens": 4, "output_tokens": 1}),
            ),
        )
    )

    def factory(
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
    ) -> RepeatedRoundSession:
        del model, system, tools, dialect
        return session

    app = create_app(models=("sonnet",), session_factory=factory)
    headers = {"X-Claude-Proxy-Session": "invalid-results"}
    messages: list[dict[str, object]] = [{"role": "user", "content": "go"}]
    async with lifespan_app(app):
        first = await post_json(
            app,
            "/v1/messages",
            _body_with_messages("anthropic", messages, stream=False),
            headers,
        )
        messages.append(_assistant_call_message("anthropic", calls))
        invalid_messages = [*messages, *_result_messages("anthropic", bad_results)]
        invalid = await post_json(
            app,
            "/v1/messages",
            _body_with_messages("anthropic", invalid_messages, stream=False),
            headers,
        )
        valid_messages = [
            *messages,
            *_result_messages(
                "anthropic",
                (("toolu_right", "right"), ("toolu_left", "left")),
            ),
        ]
        corrected = await post_json(
            app,
            "/v1/messages",
            _body_with_messages("anthropic", valid_messages, stream=False),
            headers,
        )

    assert first.status == corrected.status == 200
    assert invalid.status == 400
    assert invalid.json == {
        "type": "error",
        "error": {"type": "invalid_request", "message": "Invalid request"},
    }
    assert corrected.json["content"] == [{"type": "text", "text": "corrected"}]
    assert session.handler_count == 1


@pytest.mark.anyio
async def test_http_tool_session_freezes_schema_and_dialect_as_409() -> None:
    session = ToolBoundarySession()

    def factory(
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
    ) -> ToolBoundarySession:
        del model, system, tools, dialect
        return session

    app = create_app(models=("sonnet",), session_factory=factory)
    headers = {"X-Claude-Proxy-Session": "frozen-config"}
    async with lifespan_app(app):
        first = await post_json(app, "/v1/messages", tool_body("anthropic"), headers)
        changed_schema = tool_body("anthropic")
        changed_schema["tools"][0]["input_schema"]["properties"]["extra"] = {  # type: ignore[index]
            "type": "boolean"
        }
        schema = await post_json(
            app, "/v1/messages", changed_schema, headers
        )
        dialect = await post_json(
            app, "/v1/chat/completions", tool_body("openai"), headers
        )

    assert first.status == 200
    for response in (schema, dialect):
        assert response.status == 409
        assert b"frozen-config" not in response.body
    assert schema.json["error"]["type"] == "session_mismatch"
    assert dialect.json["error"]["code"] == "session_mismatch"


@pytest.mark.anyio
async def test_duplicate_http_tool_request_is_409_while_generation_is_active() -> None:
    session = BlockingToolSession()

    def factory(
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
    ) -> BlockingToolSession:
        del model, system, tools, dialect
        return session

    app = create_app(models=("sonnet",), session_factory=factory)
    headers = {"X-Claude-Proxy-Session": "busy-tools"}
    async with lifespan_app(app):
        active_task = asyncio.create_task(
            post_json(app, "/v1/messages", tool_body("anthropic"), headers)
        )
        await session.entered.wait()
        duplicate = await post_json(
            app, "/v1/messages", tool_body("anthropic"), headers
        )
        session.release.set()
        active = await active_task

    assert active.status == 200
    assert duplicate.status == 409
    assert duplicate.json["error"]["type"] == "request_in_flight"


@pytest.mark.anyio
async def test_waiting_http_tool_session_makes_all_busy_capacity_503() -> None:
    session = ToolBoundarySession()

    def factory(
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
    ) -> ToolBoundarySession:
        del model, system, tools, dialect
        return session

    app = create_app(models=("sonnet",), session_factory=factory, max_sessions=1)
    async with lifespan_app(app):
        waiting = await post_json(
            app,
            "/v1/messages",
            tool_body("anthropic"),
            {"X-Claude-Proxy-Session": "waiting"},
        )
        other = await post_json(
            app,
            "/v1/messages",
            tool_body("anthropic"),
            {"X-Claude-Proxy-Session": "other"},
        )

    assert waiting.status == 200
    assert other.status == 503
    assert other.json["error"]["type"] == "session_capacity"


@pytest.mark.anyio
@pytest.mark.parametrize("stream", [False, True])
async def test_tool_generation_timeout_before_first_event_is_http_504(
    stream: bool,
) -> None:
    session = BlockingToolSession()

    def factory(
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
    ) -> BlockingToolSession:
        del model, system, tools, dialect
        return session

    app = create_app(
        models=("sonnet",),
        session_factory=factory,
        turn_timeout_seconds=0.001,
    )
    async with lifespan_app(app):
        response = await post_json(
            app, "/v1/messages", tool_body("anthropic", stream=stream)
        )

    assert response.status == 504
    assert response.json["error"]["type"] == "backend_timeout"
    assert session.close_count == 1


@pytest.mark.anyio
async def test_tool_generation_timeout_after_sse_start_is_redacted_504_event() -> None:
    session = BlockingToolSession(after_delta=True)

    def factory(
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
    ) -> BlockingToolSession:
        del model, system, tools, dialect
        return session

    app = create_app(
        models=("sonnet",),
        session_factory=factory,
        turn_timeout_seconds=0.001,
    )
    async with lifespan_app(app):
        response = await post_json(
            app, "/v1/messages", tool_body("anthropic", stream=True)
        )

    assert response.status == 200
    assert b'"type":"backend_timeout"' in response.body
    assert b"toolu_one" not in response.body
    assert session.close_count == 1


@pytest.mark.anyio
async def test_disconnect_after_committed_tool_boundary_keeps_actor() -> None:
    calls = (("toolu_one", "same"),)
    session = RepeatedRoundSession(
        (
            (
                ToolCall("toolu_one", "echo", {"value": "same"}),
                Completed("tool_use", {"input_tokens": 3, "output_tokens": 2}),
            ),
            (
                TextDelta("survived"),
                Completed("end_turn", {"input_tokens": 4, "output_tokens": 1}),
            ),
        )
    )

    def factory(
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
    ) -> RepeatedRoundSession:
        del model, system, tools, dialect
        return session

    app = create_app(models=("sonnet",), session_factory=factory)
    initial = tool_body("anthropic", stream=True)
    messages = [
        {"role": "user", "content": "go"},
        _assistant_call_message("anthropic", calls),
        *_result_messages("anthropic", (("toolu_one", "result"),)),
    ]
    async with lifespan_app(app):
        disconnected = await post_json(
            app,
            "/v1/messages",
            initial,
            disconnect_after_start=True,
            block_body_after_start=True,
        )
        assert session.close_count == 0
        continued = await post_json(
            app,
            "/v1/messages",
            _body_with_messages("anthropic", messages, stream=False),
        )

    assert disconnected.status == 200
    assert continued.status == 200
    assert continued.json["content"] == [{"type": "text", "text": "survived"}]
    assert session.handler_count == 1


@pytest.mark.anyio
async def test_disconnect_after_result_commit_finishes_for_identical_replay() -> None:
    session = RepeatedRoundSession(
        (
            (
                ToolCall("toolu_one", "echo", {"value": "same"}),
                Completed("tool_use", {"input_tokens": 3, "output_tokens": 2}),
            ),
            (
                TextDelta("background-final"),
                Completed("end_turn", {"input_tokens": 8, "output_tokens": 2}),
            ),
        )
    )
    generation_entered = asyncio.Event()
    generation_release = asyncio.Event()
    original_stream = session.stream_generation

    async def blocked_stream(prompt: str) -> AsyncIterator[ConversationEvent]:
        index = 0
        async for event in original_stream(prompt):
            if index == 2:
                generation_entered.set()
                await generation_release.wait()
            index += 1
            yield event

    session.stream_generation = blocked_stream  # type: ignore[method-assign]

    def factory(
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
    ) -> RepeatedRoundSession:
        del model, system, tools, dialect
        return session

    app = create_app(models=("sonnet",), session_factory=factory)
    messages = [
        {"role": "user", "content": "go"},
        _assistant_call_message("anthropic", (("toolu_one", "same"),)),
        *_result_messages("anthropic", (("toolu_one", "result"),)),
    ]
    result_body = _body_with_messages("anthropic", messages, stream=False)
    async with lifespan_app(app):
        first = await post_json(app, "/v1/messages", tool_body("anthropic"))
        disconnected = await post_json_then_disconnect(
            app, "/v1/messages", result_body, after=generation_entered
        )
        generation_release.set()
        registry = _LIFESPAN_STATES[app]["registry"]
        actor = next(iter(registry._implicit.values()))
        await actor._runner
        handler_count = session.handler_count
        replay = await post_json(app, "/v1/messages", result_body)

    assert first.status == replay.status == 200
    assert disconnected is None
    assert replay.json["content"] == [
        {"type": "text", "text": "background-final"}
    ]
    assert replay.json["usage"] == {"input_tokens": 8, "output_tokens": 2}
    assert handler_count == session.handler_count == 1
    assert session.prompts == ["go"]


@pytest.mark.anyio
async def test_shutdown_closes_waiting_tool_actor_once() -> None:
    session = ToolBoundarySession()

    def factory(
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
    ) -> ToolBoundarySession:
        del model, system, tools, dialect
        return session

    app = create_app(models=("sonnet",), session_factory=factory)
    async with lifespan_app(app):
        response = await post_json(app, "/v1/messages", tool_body("anthropic"))
        assert response.status == 200
        assert session.close_count == 0

    assert session.close_count == 1


@pytest.mark.anyio
async def test_oversized_caller_result_is_redacted() -> None:
    caller_session = RepeatedRoundSession(
        (
            (
                ToolCall("toolu_one", "echo", {"value": "same"}),
                Completed("tool_use", {"input_tokens": 3, "output_tokens": 2}),
            ),
            (
                TextDelta("unused"),
                Completed("end_turn", {"input_tokens": 4, "output_tokens": 1}),
            ),
        )
    )
    def factory(
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
    ) -> ToolBoundarySession:
        del model, system, tools, dialect
        return caller_session

    app = create_app(models=("sonnet",), session_factory=factory)
    oversized = "caller-result-secret-" + "y" * (256 * 1024)
    messages = [
        {"role": "user", "content": "go"},
        _assistant_call_message("anthropic", (("toolu_one", "same"),)),
        *_result_messages("anthropic", (("toolu_one", oversized),)),
    ]
    async with lifespan_app(app):
        boundary = await post_json(
            app,
            "/v1/messages",
            tool_body("anthropic"),
            {"X-Claude-Proxy-Session": "caller-secret-session"},
        )
        caller = await post_json(
            app,
            "/v1/messages",
            _body_with_messages("anthropic", messages, stream=False),
            {"X-Claude-Proxy-Session": "caller-secret-session"},
        )

    assert boundary.status == 200
    assert caller.status == 400
    assert caller.json["error"]["type"] == "invalid_request"
    assert b"caller-result-secret" not in caller.body
    assert b"caller-secret-session" not in caller.body
