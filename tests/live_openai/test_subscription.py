from __future__ import annotations

import httpx
import pytest

from tests.fixtures.image_data import solid_png

pytestmark = pytest.mark.live
MODELS = ("gpt-6-astra", "gpt-5.6-sol")


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
    return response.json()


@pytest.mark.parametrize("model", MODELS)
def test_models_effort_and_accounting(openai_live_client: httpx.Client, model: str):
    result = _chat(
        openai_live_client,
        model,
        [{"role": "user", "content": "Reply with exactly OK."}],
        reasoning_effort="low",
    )
    assert result["model"]
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
    second_assistant = second["choices"][0]["message"]
    assert second_assistant.get("content")

    compacted = _chat(
        openai_live_client,
        model,
        [
            {
                "role": "user",
                "content": "Compacted summary: a tool inspected a solid red image.",
            },
            second_assistant,
            {"role": "user", "content": "Reply with the remembered color."},
        ],
    )
    assert compacted["choices"][0]["message"].get("content")


@pytest.mark.parametrize("model", MODELS)
def test_stream_can_be_cancelled_by_client(
    openai_live_client: httpx.Client, model: str
):
    with openai_live_client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": "Count slowly to 1000."}],
            "max_tokens": 2048,
            "stream": True,
        },
    ) as response:
        assert response.status_code == 200, response.read().decode()
        first_data = next(
            line for line in response.iter_lines() if line.startswith("data:")
        )
        assert first_data
    health = openai_live_client.get("/health")
    assert health.status_code == 200


def test_stock_pi_anthropic_smoke(openai_live_base_url: str, tmp_path):
    from tests.integration.pi_image_support import run_pi_image

    output = __import__("asyncio").run(
        run_pi_image(openai_live_base_url, tmp_path, provider_model="gpt-6-astra")
    )
    assert output.strip()
