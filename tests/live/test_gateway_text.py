from __future__ import annotations

import os

import pytest

from quaylet.app import create_app
from tests.gateway.asgi_client import lifespan_app, post_json
from tests.integration.pi_gateway_support import run_pi, serve


@pytest.mark.live
@pytest.mark.anyio
async def test_live_gateway_preserves_linear_context() -> None:
    if os.environ.get("QUAYLET_LIVE") != "1":
        pytest.fail("live test requires QUAYLET_LIVE=1")
    live_model = os.environ.get("QUAYLET_LIVE_MODEL", "")
    if not live_model:
        pytest.fail("live test requires QUAYLET_LIVE_MODEL")

    app = create_app(models=(live_model,))
    marker = "AMBER-731"
    first_body = {
        "model": live_model,
        "messages": [
            {
                "role": "user",
                "content": f"Remember {marker}. Reply with exactly READY.",
            }
        ],
        "max_tokens": 64,
    }
    async with lifespan_app(app):
        first = await post_json(app, "/v1/chat/completions", first_body)
        assert first.status == 200
        first_text = first.json["choices"][0]["message"]["content"]
        second = await post_json(
            app,
            "/v1/chat/completions",
            {
                "model": live_model,
                "messages": [
                    {
                        "role": "user",
                        "content": first_body["messages"][0]["content"],
                    },
                    {"role": "assistant", "content": first_text},
                    {
                        "role": "user",
                        "content": "Return only the marker, with no other text.",
                    },
                ],
                "max_tokens": 64,
            },
        )

    assert first_text == "READY"
    assert second.status == 200
    assert second.json["choices"][0]["message"]["content"] == marker


@pytest.mark.live
@pytest.mark.anyio
async def test_live_gateway_rebases_from_imported_history() -> None:
    if os.environ.get("QUAYLET_LIVE") != "1":
        pytest.fail("live test requires QUAYLET_LIVE=1")
    live_model = os.environ.get("QUAYLET_LIVE_MODEL", "")
    if not live_model:
        pytest.fail("live test requires QUAYLET_LIVE_MODEL")

    marker = "REBASING-CYAN-842"
    app = create_app(models=(live_model,))
    async with lifespan_app(app):
        response = await post_json(
            app,
            "/v1/chat/completions",
            {
                "model": live_model,
                "messages": [
                    {
                        "role": "user",
                        "content": "The earlier marker question was answered.",
                    },
                    {
                        "role": "assistant",
                        "content": f"The exact marker was {marker}.",
                    },
                    {
                        "role": "user",
                        "content": "Return only the exact earlier marker.",
                    },
                ],
                "max_tokens": 64,
            },
        )

    assert response.status == 200, response.body.decode(errors="replace")
    assert response.json["choices"][0]["message"]["content"] == marker


@pytest.mark.live
@pytest.mark.anyio
async def test_live_stock_pi_provider_rebases_compacted_transcript() -> None:
    if os.environ.get("QUAYLET_LIVE") != "1":
        pytest.fail("live test requires QUAYLET_LIVE=1")
    live_model = os.environ.get("QUAYLET_LIVE_MODEL", "")
    if live_model != "sonnet":
        pytest.fail("the stock Pi live fixture requires QUAYLET_LIVE_MODEL=sonnet")

    app = create_app(models=(live_model,))
    async with serve(app) as base_url:
        result = await run_pi(base_url, "rebase")

    assert result["scenario"] == "rebase"
    assert result["turns"][1]["text"] == "PI_COMPACTION_CYAN_913"
