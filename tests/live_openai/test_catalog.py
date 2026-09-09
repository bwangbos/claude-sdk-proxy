"""Opt-in, bounded added-model checks without touching a running server."""

import asyncio

import pytest

from quaylet.app import create_app
from tests.fixtures.image_data import solid_png
from tests.gateway.asgi_client import lifespan_app, post_json

pytestmark = [pytest.mark.live, pytest.mark.anyio]


@pytest.mark.parametrize(
    "model",
    ["gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.3-codex-spark"],
)
async def test_added_model_tool_result_and_identity(openai_live_opt_in, model):
    app = create_app(models=(model,))
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_color",
                "description": "Read the stored color.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        }
    ]
    messages = [
        {
            "role": "user",
            "content": (
                "Call read_color once. After its result, reply only with the color."
            ),
        }
    ]
    async with lifespan_app(app):
        async with asyncio.timeout(120):
            first = await post_json(
                app,
                "/v1/chat/completions",
                {
                    "model": model,
                    "messages": messages,
                    "tools": tools,
                    "reasoning_effort": "low",
                },
            )
            assert first.status == 200
            assistant = first.json["choices"][0]["message"]
            calls = assistant.get("tool_calls", [])
            assert len(calls) == 1 and calls[0]["function"]["name"] == "read_color"
            content = (
                "red"
                if model.endswith("spark")
                else [
                    {"type": "text", "text": "Read the color in this image."},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + solid_png()},
                    },
                ]
            )
            second = await post_json(
                app,
                "/v1/chat/completions",
                {
                    "model": model,
                    "messages": [
                        *messages,
                        assistant,
                        {
                            "role": "tool",
                            "tool_call_id": calls[0]["id"],
                            "content": content,
                        },
                    ],
                    "tools": tools,
                    "reasoning_effort": "low",
                },
            )
            assert second.status == 200
            assert second.headers["x-quaylet-actual-model"] == model
            assert second.json["model"] == model
            assert "red" in second.json["choices"][0]["message"]["content"].lower()
            usage = second.json["usage"]
            assert (
                usage["total_tokens"]
                == usage["prompt_tokens"] + usage["completion_tokens"]
            )
