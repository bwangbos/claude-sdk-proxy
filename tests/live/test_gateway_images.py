from __future__ import annotations

import os

import pytest

from claude_sdk_proxy.app import create_app
from tests.fixtures.image_data import solid_png
from tests.gateway.asgi_client import lifespan_app, post_json

pytestmark = [pytest.mark.live, pytest.mark.anyio]


async def test_live_stock_pi_image_tool(tmp_path):
    from tests.integration.pi_gateway_support import serve
    from tests.integration.pi_image_support import run_pi_image

    assert os.environ.get("CLAUDE_PROXY_LIVE") == "1"
    assert os.environ["CLAUDE_PROXY_LIVE_MODEL"] == "sonnet"
    async with serve(create_app(models=("sonnet",))) as url:
        output = await run_pi_image(url, tmp_path)
    assert output.strip().lower().strip(".") == "red", output


@pytest.mark.parametrize("dialect", ["anthropic", "openai"])
@pytest.mark.parametrize("tool", [False, True])
async def test_live_image_understanding(dialect: str, tool: bool) -> None:
    assert os.environ.get("CLAUDE_PROXY_LIVE") == "1"
    model = os.environ["CLAUDE_PROXY_LIVE_MODEL"]
    image = (
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": solid_png(),
            },
        }
        if dialect == "anthropic"
        else {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + solid_png()},
        }
    )
    path = "/v1/messages" if dialect == "anthropic" else "/v1/chat/completions"
    prompt = "Name the solid color of the image. Reply with one lowercase color word."
    messages = [
        {
            "role": "user",
            "content": (
                "Call screenshot once, then name the solid color in its image. "
                "Reply with one lowercase color word."
            )
            if tool
            else [{"type": "text", "text": prompt}, image],
        }
    ]
    body = {"model": model, "messages": messages, "max_tokens": 128}
    if tool:
        definition = {
            "name": "screenshot",
            "description": "Return a screenshot",
            "input_schema": {"type": "object", "properties": {}},
        }
        body["tools"] = (
            [definition]
            if dialect == "anthropic"
            else [
                {
                    "type": "function",
                    "function": {
                        "name": "screenshot",
                        "description": "Return a screenshot",
                        "parameters": definition["input_schema"],
                    },
                }
            ]
        )
    app = create_app(models=(model,))
    async with lifespan_app(app):
        response = await post_json(app, path, body)
        assert response.status == 200, response.json
        if tool:
            assistant = (
                {"role": "assistant", "content": response.json["content"]}
                if dialect == "anthropic"
                else response.json["choices"][0]["message"]
            )
            messages.append(assistant)
            if dialect == "anthropic":
                call = next(
                    block
                    for block in assistant["content"]
                    if block["type"] == "tool_use"
                )
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": call["id"],
                                "content": [image],
                            }
                        ],
                    }
                )
            else:
                call = assistant["tool_calls"][0]
                messages.append(
                    {"role": "tool", "tool_call_id": call["id"], "content": [image]}
                )
            response = await post_json(app, path, body)
            assert response.status == 200, response.json
        text = (
            "".join(
                block["text"]
                for block in response.json["content"]
                if block["type"] == "text"
            )
            if dialect == "anthropic"
            else response.json["choices"][0]["message"]["content"]
        )
        assert text.strip().lower().strip(".") == "red", text
        # Import edited history into another ephemeral SDK session. Change the
        # image so repeating the earlier assistant's answer cannot pass this test.
        if dialect == "anthropic":
            image["source"]["data"] = solid_png(0, 0, 255)
        else:
            image["image_url"]["url"] = (
                "data:image/png;base64," + solid_png(0, 0, 255)
            )
        assistant = (
            {"role": "assistant", "content": response.json["content"]}
            if dialect == "anthropic"
            else response.json["choices"][0]["message"]
        )
        messages.append(assistant)
        messages.append(
            {
                "role": "user",
                "content": "The earlier answer may be wrong. Inspect the image again. "
                "Reply with its solid color, one lowercase word.",
            }
        )
        rebased = await post_json(
            app, path, body, headers={"x-claude-proxy-session": "image-import"}
        )
        assert rebased.status == 200, rebased.json
        text = (
            "".join(
                block["text"]
                for block in rebased.json["content"]
                if block["type"] == "text"
            )
            if dialect == "anthropic"
            else rebased.json["choices"][0]["message"]["content"]
        )
        assert text.strip().lower().strip(".") == "blue", text
