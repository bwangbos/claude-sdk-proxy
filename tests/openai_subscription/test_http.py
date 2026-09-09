import json

import httpx
import pytest

from claude_sdk_proxy.app import create_app as _create_app
from claude_sdk_proxy.openai_subscription.backend import Backend
from tests.gateway.asgi_client import lifespan_app, post_json, request
from tests.gateway.fakes import FakeSessionFactory

from .test_backend import Auth, Bytes, sse, terminal, text_item

PATHS = ["/v1/messages", "/v1/chat/completions"]


def create_app(**kwargs):
    return _create_app(session_factory=FakeSessionFactory(("fake Claude",)), **kwargs)


def body(stream=False, **extra):
    return {
        "model": "gpt-6-astra",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
        "stream": stream,
        **extra,
    }


def frames(response):
    return [
        json.loads(line[6:])
        for line in response.body.decode().splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("stream", [False, True])
async def test_subscription_route_uses_fixed_auth_and_truthful_identity(path, stream):
    captured = []

    def handler(r):
        captured.append(r)
        return httpx.Response(
            200,
            stream=Bytes(
                sse(
                    {
                        "type": "response.created",
                        "response": {"model": "gpt-6-astra-snapshot"},
                    },
                    terminal([text_item()]),
                )
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        backend = Backend(Auth(), client=client)
        app = create_app(
            models=("sonnet", "gpt-6-astra", "gpt-5.6-sol"),
            subscription_backend=backend,
            refusal_fallback="auto",
        )
        async with lifespan_app(app):
            models = await request(app, "GET", "/v1/models")
            assert [(m["id"], m["owned_by"]) for m in models.json["data"]] == [
                ("sonnet-5", "anthropic"),
                ("gpt-6-astra", "openai"),
                ("gpt-5.6-sol", "openai"),
            ]
            result = await post_json(
                app,
                path,
                body(stream),
                headers={
                    "authorization": "Bearer caller-secret",
                    "chatgpt-account-id": "evil",
                    "x-claude-proxy-session": "not a valid Claude session",
                },
            )
        assert result.status == 200
        assert result.headers["x-claude-proxy-actual-model"] == "gpt-6-astra-snapshot"
        assert b"hello" in result.body
        assert captured[0].headers["authorization"] == "Bearer access"
        assert captured[0].headers["chatgpt-account-id"] == "acct"
        payload = json.loads(captured[0].content)
        assert payload["instructions"] == ""
        assert payload["store"] is False and payload["stream"] is True
        assert not client.is_closed


@pytest.mark.anyio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("stream", [False, True])
async def test_missing_usage_and_unobserved_model_are_not_invented(path, stream):
    data = terminal([text_item()])
    data["response"].pop("usage", None)
    data["response"].pop("model", None)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, stream=Bytes(sse(data)))
        )
    ) as client:
        app = create_app(
            models=("gpt-6-astra",), subscription_backend=Backend(Auth(), client=client)
        )
        async with lifespan_app(app):
            extra = (
                {"stream_options": {"include_usage": True}}
                if path.endswith("completions")
                else {}
            )
            result = await post_json(app, path, body(stream, **extra))
        assert result.status == 200
        assert "x-claude-proxy-actual-model" not in result.headers
        if not stream:
            assert result.json["usage"] is None
        else:
            usages = [f.get("usage") for f in frames(result) if "usage" in f]
            if path.endswith("completions"):
                assert usages[-1] is None
            else:
                assert usages[-1] == {"input_tokens": None, "output_tokens": None}
                from anthropic.lib.streaming._messages import accumulate_event

                snapshot = None
                for frame in frames(result):
                    snapshot = accumulate_event(event=frame, current_snapshot=snapshot)
                assert snapshot.usage.input_tokens is None
                assert snapshot.usage.output_tokens is None


@pytest.mark.anyio
@pytest.mark.parametrize("path", PATHS)
async def test_explicit_fallback_rejected_without_upstream(path):
    captured = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: captured.append(r))
    ) as client:
        app = create_app(
            models=("gpt-6-astra",), subscription_backend=Backend(Auth(), client=client)
        )
        async with lifespan_app(app):
            result = await post_json(
                app, path, body(), {"x-claude-proxy-refusal-fallback": "auto"}
            )
        assert result.status == 400
        assert not captured


@pytest.mark.anyio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("stream", [False, True])
async def test_reasoning_interleaving_refusal_accounting_and_late_model(path, stream):
    reasoning = [
        {
            "type": "reasoning",
            "id": f"rs_{i}",
            "summary": [{"type": "summary_text", "text": f"summary{i}"}],
            "encrypted_content": f"cipher{i}",
        }
        for i in range(2)
    ]
    refusal = {
        "type": "message",
        "id": "msg_ref",
        "role": "assistant",
        "content": [{"type": "refusal", "refusal": "Cannot help"}],
    }
    events = []
    for i, item in enumerate(reasoning):
        events.extend(
            [
                {
                    "type": "response.output_item.added",
                    "output_index": i,
                    "item": {**item, "summary": [], "encrypted_content": ""},
                },
                {
                    "type": "response.reasoning_summary_text.delta",
                    "output_index": i,
                    "summary_index": 0,
                    "delta": f"summary{i}",
                },
            ]
        )
    events.extend(
        [
            {
                "type": "response.output_item.added",
                "output_index": 2,
                "item": {**refusal, "content": []},
            },
            {
                "type": "response.refusal.delta",
                "output_index": 2,
                "content_index": 0,
                "delta": "Cannot help",
            },
            terminal(
                reasoning + [refusal],
                model="gpt-6-astra-late",
                usage={
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "input_tokens_details": {"cached_tokens": 30},
                    "output_tokens_details": {"reasoning_tokens": 12},
                },
            ),
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, stream=Bytes(sse(*events), fragmented=True))
        )
    ) as client:
        app = create_app(
            models=("gpt-6-astra",), subscription_backend=Backend(Auth(), client=client)
        )
        async with lifespan_app(app):
            extra = (
                {"stream_options": {"include_usage": True}}
                if path.endswith("completions")
                else {}
            )
            result = await post_json(app, path, body(stream, **extra))
        assert result.status == 200
        if stream:
            assert "x-claude-proxy-actual-model" not in result.headers
            fs = frames(result)
            assert not any("error" in f for f in fs)
            if path.endswith("completions"):
                assert (
                    "".join(
                        f["choices"][0]["delta"].get("refusal", "")
                        for f in fs
                        if f["choices"]
                    )
                    == "Cannot help"
                )
                assert fs[-1]["model"] == "gpt-6-astra-late"
                assert fs[-1]["usage"] == {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "prompt_tokens_details": {"cached_tokens": 30},
                    "completion_tokens_details": {"reasoning_tokens": 12},
                }
            else:
                from anthropic.lib.streaming._messages import accumulate_event

                snapshot = None
                for frame in fs:
                    snapshot = accumulate_event(event=frame, current_snapshot=snapshot)
                assert snapshot.usage.input_tokens == 70
                assert [
                    b.thinking for b in snapshot.content if b.type == "thinking"
                ] == ["summary0", "summary1"]
                starts = [f for f in fs if f["type"] == "content_block_start"]
                assert [f["index"] for f in starts] == list(range(len(starts)))
                thinking = [
                    f for f in starts if f["content_block"]["type"] == "thinking"
                ]
                assert len(thinking) == 2
                for i, start in enumerate(thinking):
                    ds = [
                        f["delta"]
                        for f in fs
                        if f["type"] == "content_block_delta"
                        and f["index"] == start["index"]
                    ]
                    assert "".join(d.get("thinking", "") for d in ds) == f"summary{i}"
                    assert sum("signature" in d for d in ds) == 1
                stops = [f["index"] for f in fs if f["type"] == "content_block_stop"]
                assert sorted(stops) == list(range(len(starts)))
                assert fs[-2]["usage"] == {
                    "input_tokens": 70,
                    "cache_read_input_tokens": 30,
                    "output_tokens": 20,
                }
        else:
            assert result.headers["x-claude-proxy-actual-model"] == "gpt-6-astra-late"
            if path.endswith("completions"):
                assert result.json["choices"][0]["message"]["refusal"] == "Cannot help"
            else:
                assert [
                    b["text"] for b in result.json["content"] if b["type"] == "text"
                ] == ["Cannot help"]


@pytest.mark.anyio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize(
    "effort", ["low", "medium", "high", "xhigh", "max", "none", "ultra"]
)
async def test_provider_effort_validation_and_payload(path, effort):
    captured = []

    def handler(r):
        captured.append(json.loads(r.content))
        return httpx.Response(200, stream=Bytes(sse(terminal([text_item()]))))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        app = create_app(
            models=("gpt-6-astra",), subscription_backend=Backend(Auth(), client=client)
        )
        async with lifespan_app(app):
            extra = (
                {"reasoning_effort": effort}
                if path.endswith("completions")
                else {
                    "thinking": {"type": "adaptive"},
                    "output_config": {"effort": effort},
                }
            )
            result = await post_json(app, path, body(**extra))
        assert result.status == (400 if effort in {"none", "ultra"} else 200)
        if result.status == 200:
            assert captured[0]["reasoning"]["effort"] == effort
        else:
            assert not captured


@pytest.mark.anyio
@pytest.mark.parametrize("path", PATHS)
async def test_http_refusal_replay_survives_restart(path):
    captured = []
    output = [
        {
            "type": "message",
            "id": "msg_ref",
            "role": "assistant",
            "content": [{"type": "refusal", "refusal": "No thanks"}],
        }
    ]

    def handler(r):
        captured.append(json.loads(r.content))
        return httpx.Response(200, stream=Bytes(sse(terminal(output))))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for index in range(2):
            app = create_app(
                models=("gpt-6-astra",),
                subscription_backend=Backend(Auth(), client=client),
            )
            async with lifespan_app(app):
                if index == 0:
                    result = await post_json(app, path, body())
                    assert result.status == 200
                    assistant = (
                        result.json["choices"][0]["message"]
                        if path.endswith("completions")
                        else {"role": "assistant", "content": result.json["content"]}
                    )
                else:
                    result = await post_json(
                        app,
                        path,
                        body(
                            messages=body()["messages"]
                            + [assistant, {"role": "user", "content": "next"}]
                        ),
                    )
                    assert result.status == 200
        assert any("No thanks" in json.dumps(i) for i in captured[1]["input"])


@pytest.mark.anyio
@pytest.mark.parametrize("path", PATHS)
async def test_native_call_ids_and_parallel_results_roundtrip(path):
    captured = []
    output = [
        {
            "type": "function_call",
            "id": f"fc_{i}",
            "call_id": f"native.{i}-id",
            "name": "lookup",
            "arguments": '{"x":1}',
        }
        for i in range(2)
    ]

    def handler(r):
        captured.append(json.loads(r.content))
        return httpx.Response(200, stream=Bytes(sse(terminal(output))))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        app = create_app(
            models=("gpt-6-astra",), subscription_backend=Backend(Auth(), client=client)
        )
        async with lifespan_app(app):
            first = await post_json(app, path, body())
            assert first.status == 200
            if path.endswith("completions"):
                assistant = first.json["choices"][0]["message"]
                final = [
                    {"role": "tool", "tool_call_id": f"native.{i}-id", "content": "ok"}
                    for i in range(2)
                ]
            else:
                assistant = {"role": "assistant", "content": first.json["content"]}
                final = [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": f"native.{i}-id",
                                "content": "ok",
                            }
                            for i in range(2)
                        ],
                    }
                ]
            result = await post_json(
                app, path, body(messages=body()["messages"] + [assistant] + final)
            )
            assert result.status == 200
        assert [
            i["call_id"]
            for i in captured[1]["input"]
            if i.get("type") == "function_call_output"
        ] == ["native.0-id", "native.1-id"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "opaque",
    [
        {
            "type": "thinking",
            "thinking": "visible",
            "signature": "openai-subscription:v1:opaque",
        },
        {
            "type": "redacted_thinking",
            "data": "openai-subscription:assistant:v1:opaque",
        },
    ],
)
async def test_openai_metadata_is_rejected_before_claude_session(opaque):
    app = create_app(models=("sonnet",))
    async with lifespan_app(app):
        result = await post_json(
            app,
            "/v1/messages",
            body(
                model="sonnet",
                messages=[
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": [opaque]},
                    {"role": "user", "content": "next"},
                ],
            ),
        )
    assert result.status == 400
