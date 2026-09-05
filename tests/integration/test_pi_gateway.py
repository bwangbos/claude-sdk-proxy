from __future__ import annotations

import asyncio
import socket

import pytest

from claude_sdk_proxy.app import create_app
from tests.integration.pi_gateway_support import (
    PiToolSession,
    SequenceSessionFactory,
    communicate_or_reap,
    eventually_closed,
    recover_failed_session,
    run_pi,
    run_pi_tool,
    serve,
)


@pytest.mark.anyio
async def test_server_socket_closes_when_bind_fails(monkeypatch) -> None:
    class FailingSocket:
        closed = False

        def setsockopt(self, *args: object) -> None:
            pass

        def bind(self, address: object) -> None:
            raise OSError("bind denied")

        def close(self) -> None:
            self.closed = True

    sock = FailingSocket()
    monkeypatch.setattr(socket, "socket", lambda *args: sock)

    with pytest.raises(OSError, match="bind denied"):
        async with serve(object()):
            pass

    assert sock.closed


@pytest.mark.anyio
async def test_node_child_is_reaped_when_fixture_times_out() -> None:
    process = await asyncio.create_subprocess_exec(
        "node", "-e", "setInterval(() => {}, 1000)"
    )
    try:
        with pytest.raises(TimeoutError):
            await communicate_or_reap(process, timeout_seconds=0.01)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode is not None


@pytest.mark.anyio
async def test_node_child_is_reaped_when_fixture_is_cancelled() -> None:
    process = await asyncio.create_subprocess_exec(
        "node", "-e", "setInterval(() => {}, 1000)"
    )
    communication = asyncio.create_task(
        communicate_or_reap(process, timeout_seconds=5)
    )
    await asyncio.sleep(0.02)
    try:
        communication.cancel()
        with pytest.raises(asyncio.CancelledError):
            await communication
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode is not None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("scenario", "outputs", "stall"),
    [
        ("linear", ("first answer", "second answer"), False),
        ("retry", ("replayed answer",), False),
        ("abort", ("partial",), True),
        ("timeout", ("partial",), True),
    ],
)
async def test_real_pi_openai_provider_against_gateway(
    scenario: str, outputs: tuple[str, ...], stall: bool
) -> None:
    specifications = [(outputs, stall)]
    if scenario in {"abort", "timeout"}:
        specifications.append((("recovered",), False))
    factory = SequenceSessionFactory(tuple(specifications))
    timeout = 0.1 if scenario == "timeout" else 5.0
    app = create_app(
        models=("sonnet",),
        session_factory=factory,
        turn_timeout_seconds=timeout,
    )

    async with serve(app) as base_url:
        result = await run_pi(base_url, scenario)
        if scenario in {"abort", "timeout"}:
            await eventually_closed(factory.sessions[0])
            recovery = await recover_failed_session(base_url, scenario)

    expected_payload = {
        "maxTokens": 4096,
        "store": False,
        "includeUsage": True,
        "hasTemperature": False,
        "hasReasoningEffort": False,
    }
    assert result["scenario"] == scenario
    assert result["requestPayloads"] == [expected_payload] * (
        2 if scenario in {"linear", "retry"} else 1
    )
    if scenario == "linear":
        assert result["turns"] == [
            {
                "text": "first answer",
                "finishReason": "stop",
                "error": None,
                "usage": {"input": 2, "output": 1, "totalTokens": 3},
            },
            {
                "text": "second answer",
                "finishReason": "stop",
                "error": None,
                "usage": {"input": 2, "output": 1, "totalTokens": 3},
            },
        ]
        assert factory.sessions[0].prompts == ["first", "second"]
        assert factory.created == 1
    elif scenario == "retry":
        assert result["turns"] == [
            {
                "text": "replayed answer",
                "finishReason": "stop",
                "error": None,
                "usage": {"input": 2, "output": 1, "totalTokens": 3},
            },
            {
                "text": "replayed answer",
                "finishReason": "stop",
                "error": None,
                "usage": {"input": 2, "output": 1, "totalTokens": 3},
            },
        ]
        assert factory.sessions[0].prompts == ["retry"]
        assert factory.created == 1
    else:
        assert result["turns"][0]["text"] == "partial"
        assert result["turns"][0]["finishReason"] == (
            "aborted" if scenario == "abort" else "error"
        )
        if scenario == "timeout":
            assert result["turns"][0]["error"]
        assert recovery["status"] == 200
        assert recovery["body"]["choices"][0]["message"]["content"] == "recovered"
        assert factory.created == 2
        assert factory.sessions[0].close_count == 1
        assert factory.sessions[1].prompts == [scenario]


@pytest.mark.anyio
async def test_real_pi_agent_executes_repeated_tools_with_provider_config_only(
) -> None:
    session = PiToolSession()

    def factory(*args, **kwargs):
        del args, kwargs
        return session

    app = create_app(models=("sonnet",), session_factory=factory)
    async with serve(app) as base_url:
        result = await run_pi_tool(base_url)

    assert result["provider"] == {
        "api": "openai-completions",
        "baseUrl": f"{base_url}/v1",
    }
    assert result["customSessionHeaders"] == []
    assert result["requestCount"] == 4
    assert result["requests"][2] == result["requests"][3]
    assert [
        {
            "maxTokens": request["maxTokens"],
            "store": request["store"],
            "includeUsage": request["includeUsage"],
        }
        for request in result["requests"]
    ] == [
        {"maxTokens": 4096, "store": False, "includeUsage": True}
    ] * 4
    assert [request["messageRoles"] for request in result["requests"]] == [
        ["user"],
        ["user", "assistant", "tool"],
        ["user", "assistant", "tool", "assistant", "tool"],
        ["user", "assistant", "tool", "assistant", "tool"],
    ]
    assert result["requests"][0]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Echo a value",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    assert result["nativeCalls"] == [
        {"name": "echo", "arguments": {"value": "first"}},
        {"name": "echo", "arguments": {"value": "second"}},
    ]
    assert result["nativeResults"] == [
        {"name": "echo", "content": "echo:first"},
        {"name": "echo", "content": "echo:second"},
    ]
    assert result["executions"] == [
        {"value": "first"},
        {"value": "second"},
    ]
    assert result["finalText"] == "pi tool loop complete"
    assert result["replayText"] == "pi tool loop complete"
    assert result["executionCountAfterReplay"] == 2
    assert session.handler_count == 2
    assert session.results == [
        (("call_first", "echo:first"),),
        (("call_second", "echo:second"),),
    ]
    assert session.prompts == ["run the echo tool twice"]
