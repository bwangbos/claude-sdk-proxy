from __future__ import annotations

import json
import logging

import pytest

from claude_sdk_proxy import cli
from claude_sdk_proxy.app import create_app
from tests.gateway.asgi_client import lifespan_app, post_json
from tests.gateway.fakes import FakeSessionFactory


@pytest.mark.anyio
@pytest.mark.parametrize("watcher", [False, True])
async def test_tool_continuation_backend_logs_use_current_request_id(caplog, watcher):
    import asyncio

    from claude_sdk_proxy.diagnostics import record
    from claude_sdk_proxy.domain import ModelFallbackDisabled, ResponseIdentity
    from tests.gateway.test_tool_http import RepeatedRoundSession, tool_body

    caplog.set_level(logging.INFO, logger="claude_sdk_proxy.diagnostics")

    class FailingContinuation(RepeatedRoundSession):
        async def stream_generation(self, prompt):
            yield ResponseIdentity("sonnet-5", "sonnet-5", False)
            for event in self.boundary:
                yield event
            await self._resume.wait()
            if watcher:
                await asyncio.Event().wait()
            record("model_fallback_blocked", fallback_model="claude-opus-4-8")
            raise ModelFallbackDisabled()

        async def wait_failure(self):
            if not watcher:
                await asyncio.Event().wait()
            await self._resume.wait()
            raise ModelFallbackDisabled()

    backend = FailingContinuation(())
    app = create_app(models=("sonnet",), session_factory=lambda *a, **k: backend)
    body = tool_body("openai")
    async with lifespan_app(app):
        first = await post_json(app, "/v1/chat/completions", body)
        assistant = first.json["choices"][0]["message"]
        second = await post_json(
            app,
            "/v1/chat/completions",
            {
                **body,
                "messages": [
                    *body["messages"],
                    assistant,
                    {"role": "tool", "tool_call_id": "call_one", "content": "ok"},
                ],
            },
        )
    assert first.status == 200
    assert second.status == 502
    assert first.headers["x-request-id"] != second.headers["x-request-id"]
    records = [
        r.msg
        for r in caplog.records
        if isinstance(r.msg, dict)
        and r.msg.get("event") in {"model_fallback_blocked", "backend_failure"}
    ]
    assert records
    assert all(r["request_id"] == second.headers["x-request-id"] for r in records)


@pytest.mark.anyio
@pytest.mark.parametrize("stream", [False, True])
async def test_request_ids_cover_validation_errors_and_success(stream):
    app = create_app(models=("sonnet",), session_factory=FakeSessionFactory(("hello",)))
    async with lifespan_app(app):
        bad = await post_json(app, "/v1/chat/completions", {})
        good = await post_json(
            app,
            "/v1/chat/completions",
            {
                "model": "sonnet",
                "stream": stream,
                "messages": [{"role": "user", "content": "hello"}],
            },
            headers={"x-request-id": "untrusted-id"},
        )
    assert bad.status == 400
    assert good.status == 200
    assert bad.headers.get("x-request-id", "").startswith("req_")
    assert good.headers.get("x-request-id", "").startswith("req_")
    assert bad.headers["x-request-id"] != good.headers["x-request-id"]
    assert good.headers["x-request-id"] != "untrusted-id"


@pytest.mark.anyio
async def test_session_errors_explain_reason_and_log_only_safe_metadata(caplog):
    caplog.set_level(logging.INFO, logger="claude_sdk_proxy.diagnostics")
    app = create_app(models=("sonnet",), session_factory=FakeSessionFactory(("hello",)))
    body = {
        "model": "sonnet",
        "messages": [{"role": "user", "content": "SECRET_PROMPT"}],
    }
    headers = {"x-claude-proxy-session": "SECRET_SESSION"}
    async with lifespan_app(app):
        first = await post_json(app, "/v1/chat/completions", body, headers=headers)
        changed = {
            **body,
            "messages": [
                {"role": "system", "content": "SECRET_SYSTEM"},
                *body["messages"],
            ],
        }
        error = await post_json(app, "/v1/chat/completions", changed, headers=headers)
    assert first.status == 200
    assert error.status == 409
    assert error.json["error"].get("reason") == "system_changed"
    assert "system" in error.json["error"]["message"].lower()
    assert "X-Claude-Proxy-Session" in error.json["error"]["message"]
    records = [r.msg for r in caplog.records if isinstance(r.msg, dict)]
    assert any(r.get("event") == "session_selected" for r in records)
    assert any(
        r.get("reason") == "system_changed"
        and r.get("request_id") == error.headers["x-request-id"]
        for r in records
    )
    assert "SECRET_" not in str(records)


def test_cli_json_log_file_has_request_diagnostics(monkeypatch, tmp_path):
    from claude_sdk_proxy.http_errors import error_detail
    from claude_sdk_proxy.sessions import SessionMismatch

    path = tmp_path / "proxy.jsonl"

    def run(*args, **kwargs):
        error_detail(SessionMismatch("session system does not match"))

    monkeypatch.setattr(cli.uvicorn, "run", run)
    assert cli.main(["--log", str(path), "--log-json"]) == 0
    event = json.loads(path.read_text())
    assert event["event"] == "request_rejected"
    assert event["reason"] == "system_changed"
    assert path.stat().st_mode & 0o777 == 0o600
