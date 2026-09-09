from __future__ import annotations

import asyncio
import json
import re

import httpx
import pytest

from tests.fixtures.image_data import solid_png

pytestmark = pytest.mark.live
MODELS = ("gpt-6-astra", "gpt-5.6-sol")


def assert_response_identity(response: httpx.Response, requested: str) -> None:
    assert response.headers.get("x-claude-proxy-requested-model") == requested
    actual = response.headers.get("x-claude-proxy-actual-model")
    assert actual
    assert response.json()["model"] == actual


def assert_expected_color(answer: str, expected: str) -> None:
    words = set(re.findall(r"[a-z]+", answer.lower()))
    assert expected in words
    assert not ({"red", "green", "blue"} - {expected}).intersection(words)


def _chat(client: httpx.Client, model: str, messages: list[dict], **extra) -> dict:
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": model,
            "messages": messages,
            "max_tokens": 256,
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    assert_response_identity(response, model)
    return response.json()


@pytest.mark.parametrize("model", MODELS)
def test_models_effort_and_accounting(openai_live_client: httpx.Client, model: str):
    result = _chat(
        openai_live_client,
        model,
        [{"role": "user", "content": "Reply with exactly OK."}],
        reasoning_effort="low",
    )
    assert result["choices"][0]["message"].get("content")
    usage = result["usage"]
    assert usage is not None
    assert usage["prompt_tokens"] >= 0
    assert usage["completion_tokens"] >= 0
    assert usage["total_tokens"] == (
        usage["prompt_tokens"] + usage["completion_tokens"]
    )


@pytest.mark.parametrize("model", MODELS)
def test_tool_round_image_and_compacted_continuation(
    openai_live_client: httpx.Client, model: str
):
    tools = [
        {
            "type": "function",
            "function": {
                "name": "inspect_color",
                "description": "Return the supplied image's solid color.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        }
    ]
    first_messages = [
        {
            "role": "user",
            "content": "Call inspect_color once, then wait for its result.",
        }
    ]
    first = _chat(openai_live_client, model, first_messages, tools=tools)
    assistant = first["choices"][0]["message"]
    calls = assistant.get("tool_calls") or []
    assert len(calls) == 1
    result_image = "data:image/png;base64," + solid_png()
    second_messages = first_messages + [
        assistant,
        {
            "role": "tool",
            "tool_call_id": calls[0]["id"],
            "content": [
                {"type": "text", "text": "red"},
                {"type": "image_url", "image_url": {"url": result_image}},
            ],
        },
    ]
    second = _chat(openai_live_client, model, second_messages, tools=tools)
    assert second["choices"][0]["message"].get("content")

    compacted = _chat(
        openai_live_client,
        model,
        [
            {
                "role": "user",
                "content": "Compacted summary: a tool inspected a solid red image.",
            },
            {"role": "assistant", "content": "red"},
            {
                "role": "user",
                "content": (
                    "Reply exactly COLOR|TOOL, using the remembered color and "
                    "the tool name only if it remains in the compacted history; "
                    "otherwise use unknown for TOOL."
                ),
            },
        ],
    )
    compacted_answer = compacted["choices"][0]["message"].get("content") or ""
    assert_expected_color(compacted_answer, "red")
    assert compacted_answer.strip().lower() == "red|unknown"


@pytest.mark.anyio
async def test_real_upstream_disconnect_closes_connection_and_releases_turn(
    openai_live_opt_in: None,
):
    del openai_live_opt_in
    from claude_sdk_proxy.openai_subscription.backend import Backend
    from tests.integration.pi_gateway_support import serve
    from tests.live_openai.transport_observer import ObservedAsyncTransport
    from tests.openai_subscription.test_http import body, create_app

    transport = ObservedAsyncTransport(
        httpx.AsyncHTTPTransport(), observed_host="chatgpt.com"
    )
    upstream_client = httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(120.0, connect=15.0),
        follow_redirects=False,
    )
    backend = Backend(client=upstream_client, timeout=120.0)
    try:
        app = create_app(models=("gpt-6-astra",), subscription_backend=backend)
        async with serve(app) as base_url:
            async with httpx.AsyncClient(base_url=base_url, timeout=120.0) as client:
                async with client.stream(
                    "POST",
                    "/v1/chat/completions",
                    json=body(
                        stream=True,
                        max_tokens=8192,
                        messages=[
                            {
                                "role": "user",
                                "content": (
                                    "Write the integers 1 through 10000, one per "
                                    "line, with no omissions or commentary."
                                ),
                            }
                        ],
                    ),
                ) as response:
                    assert response.status_code == 200
                    async for line in response.aiter_lines():
                        if not line.startswith("data: {"):
                            continue
                        frame = json.loads(line[6:])
                        choices = frame.get("choices") or []
                        if choices and choices[0].get("delta", {}).get("content"):
                            assert transport.active == 1
                            assert not transport.terminal_seen
                            break
                    else:
                        pytest.fail("upstream completed before visible content")
            async with asyncio.timeout(5):
                while not transport.closed.is_set() or backend._leases:
                    await asyncio.sleep(0.01)
            assert backend.cache.bytes_used == 0
            assert transport.opened.is_set()
            assert transport.active == 0
    finally:
        await backend.close()
        await upstream_client.aclose()


def test_stock_pi_anthropic_smoke(openai_live_base_url: str, tmp_path):
    from tests.integration.pi_image_support import run_pi_image

    output = __import__("asyncio").run(
        run_pi_image(openai_live_base_url, tmp_path, provider_model="gpt-6-astra")
    )
    assert output.strip()
