from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from typing import Any, Literal, cast

import pytest

from claude_sdk_proxy.app import create_app
from tests.gateway.asgi_client import AsgiResponse, lifespan_app, post_json
from tests.integration.pi_gateway_support import (
    TOOL_FIXTURE,
    communicate_or_reap,
    pi_agent_module,
    pi_ai_module,
    pi_coding_agent_module,
    serve,
)

pytestmark = [pytest.mark.live, pytest.mark.anyio]

type Dialect = Literal["anthropic", "openai"]


@pytest.fixture
def live_model() -> str:
    if os.environ.get("CLAUDE_PROXY_LIVE") != "1":
        pytest.fail("live tool tests require CLAUDE_PROXY_LIVE=1")
    model = os.environ.get("CLAUDE_PROXY_LIVE_MODEL", "")
    if not model.strip():
        pytest.fail("live tool tests require a non-empty CLAUDE_PROXY_LIVE_MODEL")
    return model


def _schema(*, field: str, values: list[str]) -> dict[str, object]:
    return {
        "type": "object",
        "properties": {field: {"type": "string", "enum": values}},
        "required": [field],
        "additionalProperties": False,
    }


def _tool(
    dialect: Dialect,
    *,
    name: str,
    description: str,
    schema: Mapping[str, object],
) -> dict[str, object]:
    if dialect == "anthropic":
        return {
            "name": name,
            "description": description,
            "input_schema": schema,
        }
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": schema,
        },
    }


def _request(
    model: str,
    dialect: Dialect,
    messages: list[dict[str, Any]],
    tools: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "model": model,
        "messages": messages,
        "max_tokens": 512,
        "tools": tools,
    }


def _path(dialect: Dialect) -> str:
    return "/v1/messages" if dialect == "anthropic" else "/v1/chat/completions"


def _assistant_message(dialect: Dialect, response: AsgiResponse) -> dict[str, Any]:
    assert response.status == 200, response.body.decode(errors="replace")
    if dialect == "anthropic":
        return {"role": "assistant", "content": response.json["content"]}
    message = response.json["choices"][0]["message"]
    return {
        key: value
        for key in ("role", "content", "tool_calls")
        if (value := message.get(key)) is not None
    }


def _calls(dialect: Dialect, message: Mapping[str, Any]) -> list[dict[str, Any]]:
    if dialect == "anthropic":
        return [block for block in message["content"] if block["type"] == "tool_use"]
    return cast(list[dict[str, Any]], message.get("tool_calls", []))


def _call_id(call: Mapping[str, Any]) -> str:
    return cast(str, call["id"])


def _call_arguments(dialect: Dialect, call: Mapping[str, Any]) -> dict[str, Any]:
    if dialect == "anthropic":
        return cast(dict[str, Any], call["input"])
    return cast(dict[str, Any], json.loads(call["function"]["arguments"]))


def _text(dialect: Dialect, message: Mapping[str, Any]) -> str:
    if dialect == "anthropic":
        return "".join(
            block["text"] for block in message["content"] if block["type"] == "text"
        )
    return cast(str | None, message.get("content")) or ""


def _append_results(
    dialect: Dialect,
    messages: list[dict[str, Any]],
    results: list[tuple[str, str, bool]],
) -> None:
    if dialect == "anthropic":
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": identifier,
                        "content": content,
                        **({"is_error": True} if is_error else {}),
                    }
                    for identifier, content, is_error in results
                ],
            }
        )
        return
    assert all(not is_error for _, _, is_error in results)
    messages.extend(
        {
            "role": "tool",
            "tool_call_id": identifier,
            "content": content,
        }
        for identifier, content, _ in results
    )


async def _send(
    app: Any,
    model: str,
    dialect: Dialect,
    messages: list[dict[str, Any]],
    tools: list[dict[str, object]],
    session: str,
    **kwargs: Any,
) -> AsgiResponse:
    return await post_json(
        app,
        _path(dialect),
        _request(model, dialect, messages, tools),
        {"X-Claude-Proxy-Session": session},
        **kwargs,
    )


@pytest.mark.parametrize("dialect", ["anthropic", "openai"])
async def test_live_single_caller_tool_round(live_model: str, dialect: Dialect) -> None:
    marker = f"LIVE_ONE_RESULT_{dialect.upper()}"
    tools = [
        _tool(
            dialect,
            name="one_round_probe",
            description="Call exactly once when asked for the one-round probe.",
            schema=_schema(field="request", values=["one"]),
        )
    ]
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                "Integration test: call one_round_probe exactly once with request "
                "set to 'one'. Wait for its result, then include that result in your "
                "final answer."
            ),
        }
    ]
    app = create_app(models=(live_model,))
    async with lifespan_app(app):
        boundary = await _send(
            app, live_model, dialect, messages, tools, f"live-one-{dialect}"
        )
        assistant = _assistant_message(dialect, boundary)
        calls = _calls(dialect, assistant)
        assert len(calls) == 1
        assert _call_arguments(dialect, calls[0]) == {"request": "one"}
        messages.append(assistant)
        _append_results(dialect, messages, [(_call_id(calls[0]), marker, False)])
        final = await _send(
            app, live_model, dialect, messages, tools, f"live-one-{dialect}"
        )

    assert marker in _text(dialect, _assistant_message(dialect, final))


@pytest.mark.parametrize("dialect", ["anthropic", "openai"])
async def test_live_identical_parallel_calls_correlate_reverse_results(
    live_model: str, dialect: Dialect
) -> None:
    left_marker = f"LIVE_PARALLEL_LEFT_{dialect.upper()}"
    right_marker = f"LIVE_PARALLEL_RIGHT_{dialect.upper()}"
    tools = [
        _tool(
            dialect,
            name="parallel_identity_probe",
            description=(
                "For the parallel identity test, invoke this tool exactly twice in "
                "one assistant turn with identical arguments."
            ),
            schema=_schema(field="request", values=["identical"]),
        )
    ]
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                "This is a tool protocol test. In your next assistant turn call "
                "parallel_identity_probe exactly TWO times in parallel. Both calls "
                'must use exactly {"request":"identical"}. Do not call it '
                "sequentially and do not answer finally until both results arrive. "
                "Afterwards include both returned marker strings in the final answer."
            ),
        }
    ]
    app = create_app(models=(live_model,))
    session = f"live-parallel-{dialect}"
    async with lifespan_app(app):
        boundary = await _send(app, live_model, dialect, messages, tools, session)
        assistant = _assistant_message(dialect, boundary)
        calls = _calls(dialect, assistant)
        assert len(calls) == 2
        identifiers = [_call_id(call) for call in calls]
        assert len(set(identifiers)) == 2
        assert [_call_arguments(dialect, call) for call in calls] == [
            {"request": "identical"},
            {"request": "identical"},
        ]
        messages.append(assistant)
        _append_results(
            dialect,
            messages,
            [
                (identifiers[1], right_marker, False),
                (identifiers[0], left_marker, False),
            ],
        )
        final = await _send(app, live_model, dialect, messages, tools, session)

    final_text = _text(dialect, _assistant_message(dialect, final))
    assert left_marker in final_text
    assert right_marker in final_text


async def test_live_mixed_text_and_tool_call(live_model: str) -> None:
    dialect: Dialect = "anthropic"
    marker = "LIVE_MIXED_RESULT"
    tools = [
        _tool(
            dialect,
            name="mixed_content_probe",
            description="Invoke once after emitting a short text preface.",
            schema=_schema(field="request", values=["mixed"]),
        )
    ]
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                "In one assistant message, first emit a non-empty text preface and "
                "then call mixed_content_probe exactly once with request 'mixed'. "
                "After its result arrives, include the result in your final answer."
            ),
        }
    ]
    app = create_app(models=(live_model,))
    async with lifespan_app(app):
        boundary = await _send(app, live_model, dialect, messages, tools, "live-mixed")
        assistant = _assistant_message(dialect, boundary)
        calls = _calls(dialect, assistant)
        assert _text(dialect, assistant).strip()
        assert len(calls) == 1
        messages.append(assistant)
        _append_results(dialect, messages, [(_call_id(calls[0]), marker, False)])
        final = await _send(app, live_model, dialect, messages, tools, "live-mixed")

    assert marker in _text(dialect, _assistant_message(dialect, final))


async def test_live_anthropic_nonempty_error_result(live_model: str) -> None:
    dialect: Dialect = "anthropic"
    tools = [
        _tool(
            dialect,
            name="expected_failure_probe",
            description="Invoke exactly once; this test expects the tool to fail.",
            schema=_schema(field="request", values=["fail"]),
        )
    ]
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                "Call expected_failure_probe exactly once with request 'fail'. Its "
                "error is expected. After receiving it, stop using tools and answer."
            ),
        }
    ]
    app = create_app(models=(live_model,))
    async with lifespan_app(app):
        boundary = await _send(app, live_model, dialect, messages, tools, "live-error")
        assistant = _assistant_message(dialect, boundary)
        calls = _calls(dialect, assistant)
        assert len(calls) == 1
        messages.append(assistant)
        _append_results(
            dialect,
            messages,
            [(_call_id(calls[0]), "LIVE_EXPECTED_NONEMPTY_ERROR", True)],
        )
        final = await _send(app, live_model, dialect, messages, tools, "live-error")

    assert _text(dialect, _assistant_message(dialect, final)).strip()


async def test_live_anthropic_empty_error_is_rejected_then_corrected(
    live_model: str,
) -> None:
    dialect: Dialect = "anthropic"
    marker = "LIVE_CORRECTED_ERROR"
    tools = [
        _tool(
            dialect,
            name="correctable_failure_probe",
            description="Invoke exactly once; this test supplies a corrected error.",
            schema=_schema(field="request", values=["correct"]),
        )
    ]
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                "Call correctable_failure_probe exactly once with request 'correct'. "
                "After its expected error arrives, stop using tools and answer."
            ),
        }
    ]
    app = create_app(models=(live_model,))
    async with lifespan_app(app):
        boundary = await _send(
            app, live_model, dialect, messages, tools, "live-corrected-error"
        )
        assistant = _assistant_message(dialect, boundary)
        calls = _calls(dialect, assistant)
        assert len(calls) == 1
        identifier = _call_id(calls[0])
        messages.append(assistant)
        invalid_messages = [*messages]
        _append_results(dialect, invalid_messages, [(identifier, "", True)])
        invalid = await _send(
            app,
            live_model,
            dialect,
            invalid_messages,
            tools,
            "live-corrected-error",
        )
        assert invalid.status == 400
        valid_messages = [*messages]
        _append_results(dialect, valid_messages, [(identifier, marker, True)])
        final = await _send(
            app,
            live_model,
            dialect,
            valid_messages,
            tools,
            "live-corrected-error",
        )

    assert _text(dialect, _assistant_message(dialect, final)).strip()


async def test_live_repeated_tool_rounds(live_model: str) -> None:
    dialect: Dialect = "openai"
    markers = ("LIVE_ROUND_ONE_RESULT", "LIVE_ROUND_TWO_RESULT")
    tools = [
        _tool(
            dialect,
            name="ordered_round_probe",
            description=(
                "Call once with step 'first'. Only after that result arrives, call "
                "once with step 'second'. Never issue both calls together."
            ),
            schema=_schema(field="step", values=["first", "second"]),
        )
    ]
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                "Run exactly two ordered tool rounds. First call ordered_round_probe "
                "once with step 'first'. Wait for its result. Then call it once with "
                "step 'second'. Wait again, then include both results in your answer."
            ),
        }
    ]
    app = create_app(models=(live_model,))
    async with lifespan_app(app):
        for index, step in enumerate(("first", "second")):
            boundary = await _send(
                app, live_model, dialect, messages, tools, "live-rounds"
            )
            assistant = _assistant_message(dialect, boundary)
            calls = _calls(dialect, assistant)
            assert len(calls) == 1
            assert _call_arguments(dialect, calls[0]) == {"step": step}
            messages.append(assistant)
            _append_results(
                dialect,
                messages,
                [(_call_id(calls[0]), markers[index], False)],
            )
        final = await _send(app, live_model, dialect, messages, tools, "live-rounds")

    final_text = _text(dialect, _assistant_message(dialect, final))
    assert all(marker in final_text for marker in markers)


async def test_live_continuation_disconnect_replays_committed_answer(
    live_model: str,
) -> None:
    dialect: Dialect = "anthropic"
    marker = "LIVE_DISCONNECT_COMMITTED_RESULT"
    tools = [
        _tool(
            dialect,
            name="disconnect_probe",
            description="Invoke exactly once, then repeat its result in the answer.",
            schema=_schema(field="request", values=["disconnect"]),
        )
    ]
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                "Call disconnect_probe exactly once with request 'disconnect'. After "
                "its result arrives, include that exact result marker in your answer."
            ),
        }
    ]
    app = create_app(models=(live_model,))
    async with lifespan_app(app):
        boundary = await _send(
            app, live_model, dialect, messages, tools, "live-disconnect"
        )
        assistant = _assistant_message(dialect, boundary)
        calls = _calls(dialect, assistant)
        assert len(calls) == 1
        messages.append(assistant)
        _append_results(dialect, messages, [(_call_id(calls[0]), marker, False)])
        disconnected = await _send(
            app,
            live_model,
            dialect,
            messages,
            tools,
            "live-disconnect",
            disconnect_after_body_contains=marker.encode(),
        )
        replay = await _send(
            app, live_model, dialect, messages, tools, "live-disconnect"
        )

    disconnected_text = _text(dialect, _assistant_message(dialect, disconnected))
    replay_text = _text(dialect, _assistant_message(dialect, replay))
    assert marker in disconnected_text
    assert replay_text == disconnected_text


async def test_live_tool_result_wait_timeout_cleans_up(live_model: str) -> None:
    dialect: Dialect = "anthropic"
    tools = [
        _tool(
            dialect,
            name="timeout_probe",
            description="Invoke exactly once for the timeout test.",
            schema=_schema(field="request", values=["timeout"]),
        )
    ]
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": "Call timeout_probe exactly once with request 'timeout'.",
        }
    ]
    app = create_app(
        models=(live_model,),
        tool_result_timeout_seconds=0.05,
    )
    async with lifespan_app(app):
        boundary = await _send(
            app, live_model, dialect, messages, tools, "live-timeout"
        )
        assistant = _assistant_message(dialect, boundary)
        calls = _calls(dialect, assistant)
        assert len(calls) == 1
        messages.append(assistant)
        _append_results(
            dialect,
            messages,
            [(_call_id(calls[0]), "LIVE_LATE_RESULT", False)],
        )
        await asyncio.sleep(0.2)
        late = await _send(app, live_model, dialect, messages, tools, "live-timeout")
        if late.status == 504:
            late = await _send(
                app, live_model, dialect, messages, tools, "live-timeout"
            )

    assert late.status == 409
    assert late.json["error"]["type"] == "session_mismatch"


async def test_live_exposes_only_generated_caller_tool(live_model: str) -> None:
    dialect: Dialect = "anthropic"
    tools = [
        _tool(
            dialect,
            name="generated_isolation_probe",
            description=(
                "The only permitted action for this isolation test. Invoke it once."
            ),
            schema=_schema(field="token", values=["only-caller-tool"]),
        )
    ]
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                "Invoke the available generated_isolation_probe exactly once with "
                "token 'only-caller-tool'. Do not attempt any other action."
            ),
        }
    ]
    app = create_app(models=(live_model,))
    async with lifespan_app(app):
        boundary = await _send(
            app, live_model, dialect, messages, tools, "live-isolation"
        )

    assistant = _assistant_message(dialect, boundary)
    calls = _calls(dialect, assistant)
    assert len(calls) == 1
    assert calls[0]["name"] == "generated_isolation_probe"
    assert "mcp__" not in calls[0]["name"]
    assert _call_arguments(dialect, calls[0]) == {"token": "only-caller-tool"}


async def test_live_stock_pi_agent_completes_tool_loop_with_provider_config_only(
    live_model: str,
) -> None:
    if live_model != "sonnet":
        pytest.fail("the stock Pi live fixture requires CLAUDE_PROXY_LIVE_MODEL=sonnet")
    env = {
        **os.environ,
        "PI_AI_MODULE": str(pi_ai_module()),
        "PI_AGENT_MODULE": str(pi_agent_module()),
        "PI_CODING_AGENT_MODULE": str(pi_coding_agent_module()),
        "PI_TOOL_PROMPT": (
            "Protocol test: call echo exactly twice in two rounds. First call it "
            'with exactly {"value":"first"}. After that result, call it '
            'with exactly {"value":"second"}. After the second result, '
            "finish with a non-empty answer and do not call any more tools."
        ),
    }
    app = create_app(models=(live_model,))
    async with serve(app) as base_url:
        env["PROXY_BASE_URL"] = f"{base_url}/v1"
        process = await asyncio.create_subprocess_exec(
            "node",
            str(TOOL_FIXTURE),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await communicate_or_reap(process, timeout_seconds=120)
    if process.returncode != 0:
        pytest.fail(
            f"Pi live tool fixture failed ({process.returncode}): "
            f"{stderr.decode(errors='replace')}"
        )
    result = json.loads(stdout)

    assert result["provider"] == {
        "api": "openai-completions",
        "baseUrl": f"{base_url}/v1",
    }
    assert result["packageVersions"] == {
        "pi-coding-agent": "0.84.4",
        "pi-agent-core": "0.84.4",
        "pi-ai": "0.84.4",
    }
    assert result["customSessionHeaders"] == []
    assert result["executions"] == [{"value": "first"}, {"value": "second"}]
    assert result["executionCountAfterReplay"] == 2
    assert result["finalText"].strip()
    assert result["replayText"] == result["finalText"]
