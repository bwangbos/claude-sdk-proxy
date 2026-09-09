from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

from quaylet.app import create_app
from quaylet.domain import (
    BackendFailure,
    CanonicalMessage,
    Completed,
    InputUsage,
    ResponseIdentity,
    ToolDefinition,
)
from quaylet.sdk_session import SdkSession
from tests.gateway.asgi_client import (
    _LIFESPAN_STATES,
    lifespan_app,
    post_json,
)
from tests.gateway.fakes import (
    FakeSdkClient,
    FixedTemporaryDirectory,
    raw_text_events,
    sdk_response,
)

RAW_USAGE = {
    "input_tokens": 24,
    "cache_creation_input_tokens": 43,
    "cache_read_input_tokens": 948,
    "output_tokens": 0,
}
SYNTHETIC_DIAGNOSTIC = "private synthetic refusal diagnostic"


def _init(*, session_id: str = "sdk-1", tools_enabled: bool = False) -> SystemMessage:
    return SystemMessage(
        "init",
        {
            "type": "system",
            "subtype": "init",
            "session_id": session_id,
            "uuid": "init-1",
            "model": "claude-opus-5",
            "tools": ["mcp__caller_tools__echo"] if tools_enabled else [],
            "mcp_servers": (
                [{"name": "caller_tools", "status": "connected"}]
                if tools_enabled
                else []
            ),
            "skills": [],
            "plugins": [],
            "permissionMode": "dontAsk",
        },
    )


def _raw_start(*, session_id: str = "sdk-1") -> StreamEvent:
    return StreamEvent(
        "raw-start",
        session_id,
        {
            "type": "message_start",
            "message": {
                "type": "message",
                "role": "assistant",
                "id": "msg-refusal",
                "model": "claude-opus-5",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "stop_details": None,
                "usage": {**RAW_USAGE, "output_tokens": 0},
            },
        },
    )


def _notice(*, session_id: str = "sdk-1", **changes: object) -> SystemMessage:
    data: dict[str, object] = {
        "type": "system",
        "subtype": "model_refusal_no_fallback",
        "session_id": session_id,
        "uuid": "notice-1",
        "request_id": "request-1",
        "refused_user_message_uuid": "user-1",
        "original_model": "claude-opus-5",
        "content": "refusal",
        "api_refusal_category": "reasoning_extraction",
        "api_refusal_explanation": "private upstream explanation",
    }
    data.update(changes)
    return SystemMessage("model_refusal_no_fallback", data)  # type: ignore[arg-type]


def _diagnostic(**changes: object) -> AssistantMessage:
    fields: dict[str, object] = {
        "content": [TextBlock(SYNTHETIC_DIAGNOSTIC)],
        "model": "<synthetic>",
        "error": "invalid_request",
        "usage": {
            "input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 0,
        },
        "stop_reason": "refusal",
        "session_id": "sdk-1",
        "message_id": "synthetic-message-1",
        "uuid": "synthetic-1",
    }
    fields.update(changes)
    return AssistantMessage(**fields)  # type: ignore[arg-type]


def _raw_delta(
    *,
    session_id: str = "sdk-1",
    stop_reason: object = "refusal",
    details: object = None,
    usage: object = None,
) -> StreamEvent:
    stop_details = (
        {
            "type": "refusal",
            "category": "reasoning_extraction",
            "explanation": "private raw refusal explanation",
            "fallback_has_prefill_claim": False,
        }
        if details is None
        else details
    )
    return StreamEvent(
        "raw-delta",
        session_id,
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": stop_reason,
                "stop_sequence": None,
                "stop_details": stop_details,
            },
            "usage": RAW_USAGE if usage is None else usage,
            "context_management": {"applied_edits": []},
        },
    )


def _raw_stop(*, session_id: str = "sdk-1") -> StreamEvent:
    return StreamEvent("raw-stop", session_id, {"type": "message_stop"})


def _result(**changes: object) -> ResultMessage:
    fields: dict[str, object] = {
        "subtype": "success",
        "duration_ms": 0,
        "duration_api_ms": 0,
        "is_error": True,
        "num_turns": 1,
        "session_id": "sdk-1",
        "stop_reason": "refusal",
        "usage": RAW_USAGE,
        "api_error_status": None,
        "terminal_reason": "api_error",
    }
    fields.update(changes)
    return ResultMessage(**fields)  # type: ignore[arg-type]


def refusal_response(*, tools_enabled: bool = False) -> tuple[object, ...]:
    return (
        _init(tools_enabled=tools_enabled),
        _raw_start(),
        _notice(),
        _diagnostic(),
        _raw_delta(),
        _raw_stop(),
        _result(),
    )


def _echo_definition() -> ToolDefinition:
    return ToolDefinition(
        "echo",
        "Echo a string.",
        {"type": "object", "properties": {"value": {"type": "string"}}},
    )


async def _collect_sdk_refusal(
    tmp_path: Path,
    messages: tuple[object, ...],
    *,
    tools: tuple[ToolDefinition, ...] = (),
) -> list[object]:
    client = FakeSdkClient((messages,))
    session = SdkSession(
        "opus",
        "",
        tools=tools,
        directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
        client_factory=client.capture_options,
    )
    await session.start()
    try:
        return [event async for event in session.stream_generation("prompt")]
    finally:
        await session.close()


@pytest.mark.anyio
async def test_sdk_session_disables_automatic_refusal_downgrade(tmp_path: Path) -> None:
    client = FakeSdkClient(())
    session = SdkSession(
        "opus",
        "",
        directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
        client_factory=client.capture_options,
    )
    await session.start()
    try:
        assert client.options is not None
        assert client.options.env["CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK"] == "1"
    finally:
        await session.close()


@pytest.mark.anyio
@pytest.mark.parametrize("category", ["cyber", "bio", "frontier_llm", "general_harms"])
@pytest.mark.parametrize("prefill_field", [False, True])
async def test_sdk_session_returns_classifier_refusal_without_downgrade(
    tmp_path: Path,
    category: str,
    prefill_field: bool,
) -> None:
    messages = list(refusal_response())
    messages[2] = _notice(api_refusal_category=category)
    messages[4] = _raw_delta(
        details={
            "type": "refusal",
            "category": category,
            "explanation": "private classifier explanation",
            **({"fallback_has_prefill_claim": False} if prefill_field else {}),
        }
    )
    assert await _collect_sdk_refusal(tmp_path, tuple(messages)) == [
        ResponseIdentity("opus-5", "opus-5", False),
        InputUsage(24, 948, 43),
        Completed("refusal", RAW_USAGE),
    ]


@pytest.mark.anyio
async def test_unexpected_sdk_fallback_is_explicit_and_never_publishes_replacement(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from quaylet.http_errors import error_detail
    from quaylet.tool_session_actor import _redact_backend

    caplog.set_level(logging.INFO, logger="quaylet.diagnostics")
    data = {
        **_notice(api_refusal_category="cyber").data,
        "subtype": "model_refusal_fallback",
        "trigger": "refusal",
        "direction": "retry",
        "scope": "session",
        "fallback_model": "claude-opus-4-8",
        "retracted_message_uuids": [],
    }
    messages = (
        _init(),
        _raw_start(),
        SystemMessage("model_refusal_fallback", data),
        *sdk_response("must not publish downgraded answer", "sdk-1"),
    )
    with pytest.raises(BackendFailure) as caught:
        await _collect_sdk_refusal(tmp_path, messages)
    detail = error_detail(_redact_backend(caught.value))
    assert detail.reason == "model_fallback_disabled"
    assert "fallback" in detail.message.lower()
    records = [r.msg for r in caplog.records if isinstance(r.msg, dict)]
    assert any(
        r.get("event") == "model_fallback_blocked"
        and r.get("fallback_model") == "claude-opus-4-8"
        for r in records
    )
    assert "private" not in str(records)


@pytest.mark.anyio
async def test_protocol_failure_logs_location_without_sdk_payload(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="quaylet.diagnostics")
    with pytest.raises(BackendFailure):
        await _collect_sdk_refusal(tmp_path, (_notice(content="SECRET_PAYLOAD"),))
    records = [r.msg for r in caplog.records if isinstance(r.msg, dict)]
    failures = [r for r in records if r.get("event") == "backend_failure"]
    assert failures
    assert failures[0]["reason"] == "sdk_protocol_failure"
    assert "sdk_session.py" in failures[0]["location"]
    assert "SECRET_PAYLOAD" not in str(records)


@pytest.mark.anyio
@pytest.mark.parametrize("dialect", ["openai", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
async def test_http_fallback_failure_is_explicit_and_correlated(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    dialect: str,
    stream: bool,
) -> None:
    caplog.set_level(logging.INFO, logger="quaylet.diagnostics")
    notice = SystemMessage(
        "model_refusal_fallback",
        {
            **_notice(api_refusal_category="cyber").data,
            "subtype": "model_refusal_fallback",
            "trigger": "refusal",
            "direction": "retry",
            "scope": "session",
            "fallback_model": "claude-opus-4-8",
            "retracted_message_uuids": [],
        },
    )
    client = FakeSdkClient(((_init(), _raw_start(), notice),))

    def factory(*args: Any, **kwargs: Any) -> SdkSession:
        return SdkSession(
            *args,
            **kwargs,
            directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
            client_factory=client.capture_options,
        )

    app = create_app(models=("opus",), session_factory=factory)
    endpoint = "/v1/messages" if dialect == "anthropic" else "/v1/chat/completions"
    async with lifespan_app(app):
        response = await post_json(
            app,
            endpoint,
            _opus_http_body(dialect, [{"role": "user", "content": "hello"}], stream),
        )
    assert b"Automatic model fallback is disabled" in response.body
    if not stream:
        assert response.status == 502
        assert response.json["error"]["reason"] == "model_fallback_disabled"
    assert client.disconnected
    records = [r.msg for r in caplog.records if isinstance(r.msg, dict)]
    assert any(
        r.get("event") == "model_fallback_blocked"
        and r.get("request_id") == response.headers["x-request-id"]
        for r in records
    )
    assert "private" not in str(records)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tools", [(), (_echo_definition(),)], ids=["no-tools", "tools"]
)
async def test_sdk_session_normalizes_only_complete_native_refusal(
    tmp_path: Path, tools: tuple[ToolDefinition, ...]
) -> None:
    events = await _collect_sdk_refusal(
        tmp_path, refusal_response(tools_enabled=bool(tools)), tools=tools
    )

    assert events == [
        ResponseIdentity("opus-5", "opus-5", False),
        InputUsage(24, 948, 43),
        Completed("refusal", RAW_USAGE),
    ]
    assert SYNTHETIC_DIAGNOSTIC not in repr(events)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tools", [(), (_echo_definition(),)], ids=["no-tools", "tools"]
)
async def test_sdk_session_rejects_refusal_replacing_partial_raw_message(
    tmp_path: Path, tools: tuple[ToolDefinition, ...]
) -> None:
    partial = raw_text_events("partial before refusal", "sdk-1")[:3]
    valid_refusal = refusal_response(tools_enabled=bool(tools))
    messages = (valid_refusal[0], *partial, *valid_refusal[1:])

    with pytest.raises(BackendFailure):
        await _collect_sdk_refusal(tmp_path, messages, tools=tools)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        "missing-notice",
        "duplicate-notice",
        "notice-before-start",
        "diagnostic-before-notice",
        "missing-diagnostic",
        "duplicate-diagnostic",
        "missing-raw-stop",
        "missing-result",
        "wrong-notice-session",
        "missing-diagnostic-session",
        "wrong-diagnostic-session",
        "wrong-raw-session",
        "wrong-result-session",
        "wrong-raw-reason",
        "wrong-result-reason",
    ],
)
async def test_sdk_session_rejects_uncorrelated_refusal_transaction(
    tmp_path: Path, case: str
) -> None:
    messages = list(refusal_response())
    if case == "missing-notice":
        del messages[2]
    elif case == "duplicate-notice":
        messages.insert(3, _notice())
    elif case == "notice-before-start":
        messages[1], messages[2] = messages[2], messages[1]
    elif case == "diagnostic-before-notice":
        messages[2], messages[3] = messages[3], messages[2]
    elif case == "missing-diagnostic":
        del messages[3]
    elif case == "duplicate-diagnostic":
        messages.insert(4, _diagnostic())
    elif case == "missing-raw-stop":
        del messages[5]
    elif case == "missing-result":
        del messages[6]
    elif case == "wrong-notice-session":
        messages[2] = _notice(session_id="sdk-other")
    elif case == "missing-diagnostic-session":
        messages[3] = _diagnostic(session_id=None)
    elif case == "wrong-diagnostic-session":
        messages[3] = _diagnostic(session_id="sdk-other")
    elif case == "wrong-raw-session":
        messages[4] = _raw_delta(session_id="sdk-other")
    elif case == "wrong-result-session":
        messages[6] = _result(session_id="sdk-other")
    elif case == "wrong-raw-reason":
        messages[4] = _raw_delta(stop_reason="end_turn")
    else:
        messages[6] = _result(stop_reason="end_turn")

    with pytest.raises(BackendFailure):
        await _collect_sdk_refusal(tmp_path, tuple(messages))


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("target", "changes"),
    [
        ("notice", {"api_refusal_category": "future-category"}),
        ("notice", {"api_refusal_category": 1}),
        ("notice", {"api_refusal_explanation": None}),
        ("notice", {"uuid": ""}),
        ("notice", {"unexpected": "private"}),
        ("diagnostic", {"model": "opus"}),
        ("diagnostic", {"error": "server_error"}),
        ("diagnostic", {"stop_reason": "end_turn"}),
        ("diagnostic", {"content": []}),
        ("diagnostic", {"content": [ToolUseBlock("tool-1", "Bash", {})]}),
        ("diagnostic", {"usage": {"input_tokens": 1, "output_tokens": 0}}),
        ("raw-details", {"type": "future"}),
        ("raw-details", {"category": "frontier_llm"}),
        ("raw-details", {"explanation": None}),
        ("raw-details", {"fallback_has_prefill_claim": True}),
        ("raw-details", {"unexpected": "private"}),
        ("raw-usage", {"output_tokens": True}),
        ("raw-usage", {"output_tokens": 1}),
        ("raw-usage", {**RAW_USAGE, "input_tokens": 25}),
        ("result", {"subtype": "error"}),
        ("result", {"is_error": False}),
        ("result", {"terminal_reason": "completed"}),
        ("result", {"api_error_status": 400}),
        ("result", {"errors": ["private"]}),
        ("result", {"permission_denials": ["private"]}),
        ("result", {"usage": {"output_tokens": "private"}}),
    ],
)
async def test_sdk_session_rejects_malformed_native_refusal(
    tmp_path: Path, target: str, changes: dict[str, object]
) -> None:
    messages = list(refusal_response())
    if target == "notice":
        messages[2] = _notice(**changes)
    elif target == "diagnostic":
        messages[3] = _diagnostic(**changes)
    elif target == "raw-details":
        details = {
            "type": "refusal",
            "category": "reasoning_extraction",
            "explanation": "private raw refusal explanation",
            "fallback_has_prefill_claim": False,
            **changes,
        }
        messages[4] = _raw_delta(details=details)
    elif target == "raw-usage":
        usage = {**RAW_USAGE, **changes}
        messages[4] = _raw_delta(usage=usage)
    else:
        messages[6] = _result(**changes)

    with pytest.raises(BackendFailure) as caught:
        await _collect_sdk_refusal(tmp_path, tuple(messages))
    assert "private" not in str(caught.value)


@pytest.mark.anyio
@pytest.mark.parametrize("block_type", ["text", "tool_use"])
async def test_sdk_session_rejects_raw_content_in_native_refusal(
    tmp_path: Path, block_type: str
) -> None:
    content_block: dict[str, object]
    if block_type == "text":
        content_block = {"type": "text", "text": ""}
    else:
        content_block = {
            "type": "tool_use",
            "id": "tool-private",
            "name": "mcp__caller_tools__echo",
            "input": {},
            "caller": {"type": "direct"},
        }
    content = StreamEvent(
        "content-private",
        "sdk-1",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": content_block,
        },
    )
    tools = (_echo_definition(),) if block_type == "tool_use" else ()
    valid = refusal_response(tools_enabled=bool(tools))
    assert await _collect_sdk_refusal(tmp_path, valid, tools=tools) == [
        ResponseIdentity("opus-5", "opus-5", False),
        InputUsage(24, 948, 43),
        Completed("refusal", RAW_USAGE),
    ]
    messages = list(valid)
    messages.insert(3, content)

    with pytest.raises(BackendFailure) as caught:
        await _collect_sdk_refusal(
            tmp_path,
            tuple(messages),
            tools=tools,
        )
    assert "private" not in str(caught.value)


@pytest.mark.anyio
async def test_ordinary_sdk_error_result_remains_a_failure(tmp_path: Path) -> None:
    messages = (
        *raw_text_events("answer", "sdk-1", model="claude-opus-5"),
        _result(stop_reason="end_turn", terminal_reason="api_error"),
    )

    with pytest.raises(BackendFailure, match="query failed"):
        await _collect_sdk_refusal(tmp_path, messages)


@pytest.mark.anyio
@pytest.mark.parametrize("missing_result", [False, True])
async def test_terminal_sdk_failure_is_logged_before_redaction(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    missing_result: bool,
) -> None:
    caplog.set_level(logging.INFO, logger="quaylet.diagnostics")
    messages = tuple(raw_text_events("answer", "sdk-1", model="claude-opus-5"))
    if not missing_result:
        messages += (_result(stop_reason="end_turn", terminal_reason="api_error"),)
    with pytest.raises(BackendFailure):
        await _collect_sdk_refusal(tmp_path, messages)
    reason = "sdk_missing_result" if missing_result else "sdk_query_failed"
    assert any(
        isinstance(r.msg, dict)
        and r.msg.get("stage") == "sdk_generation"
        and r.msg.get("reason") == reason
        for r in caplog.records
    )


@pytest.mark.anyio
async def test_refusal_accepts_valid_rate_limit_metadata_before_result(
    tmp_path: Path,
) -> None:
    from tests.gateway.test_sdk_session import live_rate_limit

    messages = list(refusal_response())
    messages.insert(-1, live_rate_limit())
    assert await _collect_sdk_refusal(tmp_path, tuple(messages)) == [
        ResponseIdentity("opus-5", "opus-5", False),
        InputUsage(24, 948, 43),
        Completed("refusal", RAW_USAGE),
    ]


class _HttpRefusalFactory:
    def __init__(self, tmp_path: Path, category: str = "reasoning_extraction") -> None:
        refusal = list(refusal_response())
        refusal[2] = _notice(api_refusal_category=category)
        refusal[4] = _raw_delta(
            details={
                "type": "refusal",
                "category": category,
                "explanation": "private classifier explanation",
                "fallback_has_prefill_claim": False,
            }
        )
        self._clients: Iterator[FakeSdkClient] = iter(
            (
                FakeSdkClient(
                    (
                        sdk_response("prior answer", "sdk-1", model="claude-opus-5"),
                        tuple(refusal),
                    )
                ),
                FakeSdkClient(
                    (sdk_response("recovered", "sdk-2", model="claude-opus-5"),)
                ),
            )
        )
        self.clients: list[FakeSdkClient] = []
        self.histories: list[tuple[CanonicalMessage, ...]] = []
        self._tmp_path = tmp_path

    def __call__(
        self,
        model: str,
        system: str,
        *,
        history: tuple[CanonicalMessage, ...] = (),
        **kwargs: Any,
    ) -> SdkSession:
        client = next(self._clients)
        path = self._tmp_path / f"session-{len(self.clients)}"
        path.mkdir()
        self.clients.append(client)
        self.histories.append(history)
        return SdkSession(
            model,
            system,
            history=history,
            directory_factory=lambda: FixedTemporaryDirectory(path),
            client_factory=client.capture_options,
            **kwargs,
        )


def _http_body(dialect: str, messages: list[dict[str, object]], stream: bool) -> dict:
    body = _opus_http_body(dialect, messages, stream)
    body["model"] = "sonnet"
    return body


def _opus_http_body(
    dialect: str, messages: list[dict[str, object]], stream: bool
) -> dict:
    body: dict[str, object] = {
        "model": "opus",
        "messages": messages,
        "stream": stream,
    }
    if dialect == "anthropic":
        body["max_tokens"] = 128
    elif stream:
        body["stream_options"] = {"include_usage": True}
    return body


def _assert_http_refusal(dialect: str, stream: bool, response: Any) -> None:
    assert response.status == 200
    assert SYNTHETIC_DIAGNOSTIC.encode() not in response.body
    if stream:
        expected = (
            b'"stop_reason":"refusal"'
            if dialect == "anthropic"
            else b'"finish_reason":"content_filter"'
        )
        assert expected in response.body
        assert (
            b'"output_tokens":0' in response.body
            or b'"completion_tokens":0' in response.body
        )
        return
    payload = response.json
    if dialect == "anthropic":
        assert payload["content"] == []
        assert payload["stop_reason"] == "refusal"
        assert payload["usage"] == {
            "input_tokens": 24,
            "cache_creation_input_tokens": 43,
            "cache_read_input_tokens": 948,
            "output_tokens": 0,
        }
    else:
        assert payload["choices"][0]["message"]["content"] == ""
        assert payload["choices"][0]["finish_reason"] == "content_filter"
        assert payload["usage"] == {
            "prompt_tokens": 1015,
            "completion_tokens": 0,
            "total_tokens": 1015,
            "prompt_tokens_details": {
                "cached_tokens": 948,
                "cache_write_tokens": 43,
            },
        }


@pytest.mark.anyio
@pytest.mark.parametrize("dialect", ["openai", "anthropic"])
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
@pytest.mark.parametrize("category", ["reasoning_extraction", "cyber"])
async def test_http_refusal_replays_and_can_recover_earlier_history(
    tmp_path: Path,
    dialect: str,
    stream: bool,
    category: str,
) -> None:
    factory = _HttpRefusalFactory(tmp_path, category)
    app = create_app(models=("opus",), session_factory=factory)
    path = "/v1/messages" if dialect == "anthropic" else "/v1/chat/completions"
    first_messages = [{"role": "user", "content": "first"}]
    refusal_messages = [
        *first_messages,
        {"role": "assistant", "content": "prior answer"},
        {"role": "user", "content": "refuse"},
    ]
    recovery_messages = [
        *first_messages,
        {"role": "assistant", "content": "prior answer"},
        {"role": "user", "content": "recover"},
    ]

    async with lifespan_app(app):
        first = await post_json(
            app, path, _opus_http_body(dialect, first_messages, False)
        )
        session_id = first.headers["x-quaylet-session"]
        headers = {"x-quaylet-session": session_id}
        refused = await post_json(
            app, path, _opus_http_body(dialect, refusal_messages, stream), headers
        )
        replay = await post_json(
            app, path, _opus_http_body(dialect, refusal_messages, stream), headers
        )
        _assert_http_refusal(dialect, stream, refused)
        _assert_http_refusal(dialect, stream, replay)
        entry = _LIFESPAN_STATES[app]["registry"]._implicit[session_id]
        assert entry.transcript[-1] == CanonicalMessage.assistant_text("")
        assert SYNTHETIC_DIAGNOSTIC not in repr(entry.transcript)
        recovered = await post_json(
            app, path, _opus_http_body(dialect, recovery_messages, False), headers
        )

    assert recovered.status == 200
    assert "recovered" in recovered.body.decode()
    assert factory.histories == [
        (),
        (
            CanonicalMessage.user_text("first"),
            CanonicalMessage.assistant_text("prior answer"),
        ),
    ]
    assert [client.prompts for client in factory.clients] == [
        ["first", "refuse"],
        ["recover"],
    ]
    assert [client.disconnect_count for client in factory.clients] == [1, 1]
