from __future__ import annotations

import asyncio

import pytest

from claude_sdk_proxy.app import create_app
from claude_sdk_proxy.domain import Completed, ImageBlock, TextDelta, ToolCall
from tests.gateway.fakes import FakeConversationSession
from tests.integration.pi_gateway_support import serve
from tests.integration.pi_image_support import run_pi_image


@pytest.mark.anyio
async def test_stock_pi_read_returns_image_to_original_tool_call(tmp_path):
    class ImageSession(FakeConversationSession):
        def __init__(self):
            super().__init__("unused")
            self.ready = asyncio.Event()
            self.results = ()

        async def stream_generation(self, prompt):
            yield ToolCall(
                "toolu_screenshot", "read", {"path": str(tmp_path / "color.png")}
            )
            yield Completed("tool_use", {"input_tokens": 1, "output_tokens": 1})
            await self.ready.wait()
            yield TextDelta("red")
            yield Completed("end_turn", {"input_tokens": 1, "output_tokens": 1})

        async def submit_tool_results(self, results):
            self.results = tuple(results)
            self.ready.set()

        async def wait_failure(self):
            await asyncio.Event().wait()

    session = ImageSession()
    app = create_app(
        models=("sonnet",), session_factory=lambda *args, **kwargs: session
    )
    async with serve(app) as url:
        output = await run_pi_image(url, tmp_path)
    assert output.strip() == "red"
    assert len(session.results) == 1
    assert session.results[0].tool_call_id == "toolu_screenshot"
    assert any(isinstance(part, ImageBlock) for part in session.results[0].content)
