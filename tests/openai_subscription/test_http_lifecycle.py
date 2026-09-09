import asyncio
import json

import httpx
import pytest

from quaylet.domain import BackendFailure
from quaylet.openai_subscription.backend import Backend
from tests.gateway.asgi_client import lifespan_app, post_json, post_json_then_disconnect
from tests.gateway.fakes import FakeConversationSession

from .test_backend import Auth, Bytes, sse, terminal, text_item
from .test_http import PATHS, body, create_app, frames


@pytest.mark.anyio
async def test_claude_precontent_error_retains_verified_identity_headers():
    from quaylet.app import create_app as real_create_app

    class Failure(FakeConversationSession):
        async def stream_generation(self, prompt):
            yield self.identity
            raise BackendFailure("synthetic")

    app = real_create_app(
        models=("sonnet-5",), session_factory=lambda *a, **k: Failure("unused")
    )
    async with lifespan_app(app):
        result = await post_json(
            app, "/v1/chat/completions", body(True, model="sonnet-5")
        )
    assert result.status == 502
    assert result.headers["x-quaylet-actual-model"] == "sonnet-5"


@pytest.mark.anyio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_http_errors_are_categorized_and_body_is_secret(path, status):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(status, text="SECRET upstream error")
        )
    ) as client:
        app = create_app(
            models=("gpt-6-astra",), subscription_backend=Backend(Auth(), client=client)
        )
        async with lifespan_app(app):
            result = await post_json(
                app, path, body(True), {"x-request-id": "req_safe"}
            )
        assert result.status == {401: 401, 403: 403, 429: 429, 500: 502}[status]
        assert b"SECRET" not in result.body
        assert result.headers["x-request-id"].startswith("req_")


@pytest.mark.anyio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("stream", [False, True])
async def test_disconnect_closes_subscription_http_and_does_not_cache(path, stream):
    upstream = Bytes(
        sse(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**text_item(), "content": []},
            },
            {"type": "response.output_text.delta", "output_index": 0, "delta": "hello"},
        ),
        hang=True,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=upstream))
    ) as client:
        backend = Backend(Auth(), client=client)
        app = create_app(models=("gpt-6-astra",), subscription_backend=backend)
        async with lifespan_app(app):
            if stream:
                await asyncio.wait_for(
                    post_json(
                        app, path, body(True), disconnect_after_body_contains=b"hello"
                    ),
                    2,
                )
            else:
                await asyncio.wait_for(
                    post_json_then_disconnect(
                        app, path, body(), after=upstream.waiting
                    ),
                    2,
                )
            assert upstream.closed
            assert backend.cache.bytes_used == 0


@pytest.mark.anyio
@pytest.mark.parametrize("path", PATHS)
async def test_shared_prefix_http_turns_do_not_serialize(path):
    count = 0
    both = asyncio.Event()

    class Rendezvous(Bytes):
        async def __aiter__(self):
            nonlocal count
            count += 1
            if count == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 1)
            yield self.data

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, stream=Rendezvous(sse(terminal([text_item()])))
            )
        )
    ) as client:
        app = create_app(
            models=("gpt-6-astra",), subscription_backend=Backend(Auth(), client=client)
        )
        async with lifespan_app(app):
            results = await asyncio.gather(
                *(post_json(app, path, body()) for _ in range(2))
            )
        assert [r.status for r in results] == [200, 200]


@pytest.mark.anyio
@pytest.mark.parametrize("path", PATHS)
async def test_precontent_retry_drains_stale_identity_before_headers(path):
    attempts = []

    def handler(r):
        attempts.append(r)
        data = (
            sse(
                {"type": "response.created", "response": {"model": "stale-model"}},
                {"type": "error", "error": {"code": "server_error"}},
            )
            if len(attempts) == 1
            else sse(terminal([text_item()]))
        )
        return httpx.Response(200, stream=Bytes(data))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        app = create_app(
            models=("gpt-6-astra",), subscription_backend=Backend(Auth(), client=client)
        )
        async with lifespan_app(app):
            result = await post_json(app, path, body(True))
        assert result.status == 200
        assert "x-quaylet-actual-model" not in result.headers
        assert b"stale-model" not in result.body
        assert b"hello" in result.body


@pytest.mark.anyio
@pytest.mark.parametrize("path", PATHS)
async def test_fragmented_parallel_tool_arguments_render_complete_calls(path):
    output = [
        {
            "type": "function_call",
            "id": f"fc_{i}",
            "call_id": f"call_{i}",
            "name": "lookup",
            "arguments": '{"city":"Montréal"}',
        }
        for i in range(2)
    ]
    events = []
    for i, call in enumerate(output):
        events.append(
            {
                "type": "response.output_item.added",
                "output_index": i,
                "item": {**call, "arguments": ""},
            }
        )
    for part in ['{"city":', '"Montréal"}']:
        for i in range(2):
            events.append(
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": i,
                    "delta": part,
                }
            )
    for i, call in enumerate(output):
        events.append(
            {"type": "response.output_item.done", "output_index": i, "item": call}
        )
    events.append(terminal(output))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, stream=Bytes(sse(*events), fragmented=True))
        )
    ) as client:
        app = create_app(
            models=("gpt-6-astra",), subscription_backend=Backend(Auth(), client=client)
        )
        async with lifespan_app(app):
            result = await post_json(app, path, body(True))
        fs = frames(result)
        assert not any("error" in f for f in fs)
        if path.endswith("completions"):
            calls = [
                c for f in fs for c in f["choices"][0]["delta"].get("tool_calls", [])
            ]
            assert [c["index"] for c in calls] == [0, 1]
            assert [json.loads(c["function"]["arguments"]) for c in calls] == [
                {"city": "Montréal"}
            ] * 2
        else:
            calls = [f["delta"] for f in fs if f["type"] == "content_block_delta"]
            assert [json.loads(c["partial_json"]) for c in calls] == [
                {"city": "Montréal"}
            ] * 2
