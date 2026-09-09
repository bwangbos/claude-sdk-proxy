from __future__ import annotations

import httpx
import pytest

from quaylet.app import create_app
from tests.gateway.asgi_client import AsgiResponse
from tests.gateway.fakes import sdk_response
from tests.gateway.test_refusal_continuation import RefusalFactory, _assistant, _body
from tests.gateway.test_sdk_refusal import _assert_http_refusal, refusal_response
from tests.integration.pi_gateway_support import serve


@pytest.mark.anyio
@pytest.mark.parametrize("dialect", ["anthropic", "openai"])
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
@pytest.mark.parametrize("tools", [False, True], ids=["no-tools", "tools"])
async def test_empty_refusal_replay_and_continuation_over_real_http(
    tmp_path, dialect, stream, tools
):
    factory = RefusalFactory(
        tmp_path,
        [(refusal_response(tools_enabled=tools), sdk_response("continued", "sdk-1"))],
    )
    app = create_app(models=("sonnet",), session_factory=factory)
    path = "/v1/messages" if dialect == "anthropic" else "/v1/chat/completions"
    messages = [{"role": "user", "content": "refuse"}]
    async with serve(app) as base_url, httpx.AsyncClient(base_url=base_url) as client:
        body = _body(dialect, messages, stream, tools)
        refused = await client.post(path, json=body)
        response = AsgiResponse(
            refused.status_code, dict(refused.headers), refused.content
        )
        _assert_http_refusal(dialect, stream, response)
        replay = await client.post(path, json=body)
        _assert_http_refusal(
            dialect,
            stream,
            AsgiResponse(replay.status_code, dict(replay.headers), replay.content),
        )
        assert factory.clients[0].prompts == ["refuse"]
        messages += [
            _assistant(response, dialect, stream),
            {"role": "user", "content": "continue"},
        ]
        continued = await client.post(path, json=_body(dialect, messages, tools=tools))
        assert continued.status_code == 200, continued.text
        assert "continued" in continued.text
        assert (
            continued.headers["x-quaylet-session"]
            == refused.headers["x-quaylet-session"]
        )
        assert factory.clients[0].prompts == ["refuse", "continue"]
        assert len(factory.clients) == 1
    assert factory.clients[0].disconnect_count == 1
    assert factory.directories[0].cleanup_count == 1
