from __future__ import annotations

import copy
import json
import os

import pytest

from claude_sdk_proxy.app import create_app
from tests.gateway.asgi_client import lifespan_app, post_json
from tests.gateway.test_transcript_recovery import completed_result_tail
from tests.live.test_gateway_tools import (
    _append_results,
    _assistant_message,
    _calls,
    _path,
    _text,
)

pytestmark = [pytest.mark.live, pytest.mark.anyio]


@pytest.mark.parametrize("dialect", ["openai", "anthropic"])
@pytest.mark.parametrize("suspended", [False, True])
async def test_live_complete_tool_results_recover_without_reexecuting_tools(
    dialect, suspended
):
    assert os.environ.get("CLAUDE_PROXY_LIVE") == "1"
    model = os.environ["CLAUDE_PROXY_LIVE_MODEL"]
    body = completed_result_tail()
    body["model"] = model
    body["messages"][0]["content"] = (
        "Call echo once with value 'same'. After its result arrives, "
        "return that result verbatim without calling any more tools."
    )
    marker = "RECOVERED_RESULT_47c92"
    body["messages"][-1]["content"] = marker
    body["max_tokens"] = 512
    if dialect == "anthropic":
        function = body["tools"][0]["function"]
        body["tools"] = [
            {
                "name": function["name"],
                "description": function["description"],
                "input_schema": function["parameters"],
            }
        ]
        call = body["messages"][-2]["tool_calls"][0]
        call["id"] = "toolu_imported_one"
        body["messages"] = [
            body["messages"][0],
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["function"]["name"],
                        "input": json.loads(call["function"]["arguments"]),
                    }
                ],
            },
        ]
        _append_results(dialect, body["messages"], [(call["id"], marker, False)])
    app = create_app(models=(model,))
    async with lifespan_app(app):
        path = _path(dialect)
        if suspended:
            initial = {**body, "messages": body["messages"][:1]}
            boundary = await post_json(app, path, initial)
            assistant = _assistant_message(dialect, boundary)
            calls = _calls(dialect, assistant)
            assert len(calls) == 1
            body["messages"] = [body["messages"][0], assistant]
            _append_results(
                dialect, body["messages"], [(calls[0]["id"], marker, False)]
            )
            # Earlier history changed, but the pending calls and result batch match.
            body["messages"][0]["content"] += " This is a restored transcript."
        response = await post_json(app, path, body)
        assert response.status == 200, response.json
        message = _assistant_message(dialect, response)
        assert not _calls(dialect, message), message
        assert marker in _text(dialect, message)
        if suspended:
            assert (
                response.headers["x-claude-proxy-session"]
                == boundary.headers["x-claude-proxy-session"]
            )
        # The recovered session remains usable, including its returned header.
        continued = copy.deepcopy(body)
        continued["messages"] += [
            message,
            {"role": "user", "content": "Repeat the saved result, no tools."},
        ]
        second = await post_json(
            app,
            path,
            continued,
            headers={
                "x-claude-proxy-session": response.headers["x-claude-proxy-session"]
            },
        )
        assert second.status == 200, second.json
        assert marker in _text(dialect, _assistant_message(dialect, second))
