from __future__ import annotations

import asyncio

import pytest

from claude_sdk_proxy.app import create_app
from tests.integration.pi_gateway_support import (
    SequenceSessionFactory,
    communicate_or_reap,
    eventually_closed,
    recover_failed_session,
    run_pi,
    serve,
)


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
