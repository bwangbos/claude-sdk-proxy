import json
from dataclasses import replace

import httpx
import pytest

from quaylet.openai_subscription.backend import Backend
from tests.fixtures.image_data import solid_png
from tests.gateway.asgi_client import lifespan_app, post_json

from .test_backend import Auth, Bytes, sse, terminal, text_item
from .test_http import PATHS, body, create_app


def assistant_response(result, path):
    return (
        result.json["choices"][0]["message"]
        if path.endswith("completions")
        else {"role": "assistant", "content": result.json["content"]}
    )


@pytest.mark.anyio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize(
    "change", ["none", "restart", "evict", "model", "account", "compaction", "system"]
)
async def test_http_complete_history_replay_is_scoped_and_lossless(path, change):
    captured = []

    class Account(Auth):
        account = "acct"

        async def credentials(self):
            return replace(await super().credentials(), account_id=self.account)

    auth = Account()

    def handler(r):
        captured.append(json.loads(r.content))
        return httpx.Response(200, stream=Bytes(sse(terminal([text_item()]))))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        backend = Backend(auth, client=client, max_sessions=1)
        app = create_app(
            models=("gpt-6-astra", "gpt-5.6-sol"), subscription_backend=backend
        )
        async with lifespan_app(app):
            first = await post_json(app, path, body())
            assistant = assistant_response(first, path)
            messages = body()["messages"] + [
                assistant,
                {"role": "user", "content": "next"},
            ]
            if change == "evict":
                await post_json(
                    app, path, body(messages=[{"role": "user", "content": "different"}])
                )
            if change == "account":
                auth.account = "other"
            if change == "compaction":
                messages[0] = {"role": "user", "content": "compacted"}
            extra = {"model": "gpt-5.6-sol"} if change == "model" else {}
            if change == "system":
                if path.endswith("completions"):
                    messages.insert(0, {"role": "system", "content": "changed"})
                else:
                    extra["system"] = "changed"
            if change != "restart":
                result = await post_json(app, path, body(messages=messages, **extra))
                assert result.status == 200
        if change == "restart":
            fresh = create_app(
                models=("gpt-6-astra",),
                subscription_backend=Backend(auth, client=client),
            )
            async with lifespan_app(fresh):
                result = await post_json(fresh, path, body(messages=messages))
                assert result.status == 200
        native = [i for i in captured[-1]["input"] if i.get("role") == "assistant"]
        preserved = change == "none" or (
            path == "/v1/messages" and change in {"restart", "evict"}
        )
        assert (native == [text_item()]) == preserved
        assert native[0]["content"][0]["text"] == "hello"


@pytest.mark.anyio
@pytest.mark.parametrize("path", PATHS)
async def test_images_system_tools_and_repeated_tool_rounds(path):
    captured = []
    call = {
        "type": "function_call",
        "id": "fc_native",
        "call_id": "call_native",
        "name": "lookup",
        "arguments": "{}",
    }

    def handler(r):
        captured.append(json.loads(r.content))
        return httpx.Response(200, stream=Bytes(sse(terminal([call]))))

    image = (
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + solid_png()},
        }
        if path.endswith("completions")
        else {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": solid_png(),
            },
        }
    )
    schema = {"type": "object", "properties": {}}
    if path.endswith("completions"):
        extra = {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "description": "exact",
                        "parameters": schema,
                    },
                }
            ],
            "reasoning_effort": "high",
        }
        messages = [{"role": "system", "content": "Caller system\n EXACT"}]
    else:
        extra = {
            "system": "Caller system\n EXACT",
            "tools": [
                {"name": "lookup", "description": "exact", "input_schema": schema}
            ],
            "thinking": {"type": "adaptive", "display": "omitted"},
        }
        messages = []
    messages.append(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "before"},
                image,
                {"type": "text", "text": "after"},
            ],
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        app = create_app(
            models=("gpt-6-astra",), subscription_backend=Backend(Auth(), client=client)
        )
        async with lifespan_app(app):
            for _ in range(3):
                result = await post_json(app, path, body(messages=messages, **extra))
                assert result.status == 200
                messages.append(assistant_response(result, path))
                if path.endswith("completions"):
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": "call_native",
                            "content": [{"type": "text", "text": "result"}, image],
                        }
                    )
                else:
                    messages.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "call_native",
                                    "content": [
                                        {"type": "text", "text": "result"},
                                        image,
                                    ],
                                }
                            ],
                        }
                    )
        assert captured[0]["instructions"] == "Caller system\n EXACT"
        assert [i["type"] for i in captured[0]["input"][0]["content"]] == [
            "input_text",
            "input_image",
            "input_text",
        ]
        assert captured[0]["tools"][0]["parameters"] == schema
        assert captured[0]["tools"][0]["description"] == "exact"
        results = [
            i for i in captured[-1]["input"] if i.get("type") == "function_call_output"
        ]
        assert len(results) == 2
        assert [p["type"] for p in results[0]["output"]] == [
            "input_text",
            "input_image",
        ]
        if path == "/v1/messages":
            assert "summary" not in captured[0].get("reasoning", {})


@pytest.mark.anyio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize(
    "invalid",
    ["disabled", "budget", "remote-image", "incomplete-results", "unknown-model"],
)
async def test_malformed_provider_boundaries_fail_before_http(path, invalid):
    captured = []

    def handler(r):
        captured.append(r)
        return httpx.Response(500)

    value = body()
    if invalid == "disabled":
        value.update(
            {"reasoning_effort": "none"}
            if path.endswith("completions")
            else {"thinking": {"type": "disabled"}}
        )
    elif invalid == "budget":
        value.update(
            {"reasoning_effort": 1000}
            if path.endswith("completions")
            else {"thinking": {"type": "enabled", "budget_tokens": 10}}
        )
    elif invalid == "remote-image":
        image = (
            {"type": "image_url", "image_url": {"url": "http://127.0.0.1/secret"}}
            if path.endswith("completions")
            else {
                "type": "image",
                "source": {"type": "url", "url": "http://127.0.0.1/secret"},
            }
        )
        value["messages"] = [{"role": "user", "content": [image]}]
    elif invalid == "unknown-model":
        value["model"] = "gpt-6-astra-snapshot"
    else:
        if path.endswith("completions"):
            value["messages"] += [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_x",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "user", "content": "missing result"},
            ]
        else:
            value["messages"] += [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_x",
                            "name": "lookup",
                            "input": {},
                        }
                    ],
                },
                {"role": "user", "content": "missing result"},
            ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        app = create_app(
            models=("gpt-6-astra",), subscription_backend=Backend(Auth(), client=client)
        )
        async with lifespan_app(app):
            result = await post_json(app, path, value)
        assert result.status == (404 if invalid == "unknown-model" else 400)
        assert not captured
