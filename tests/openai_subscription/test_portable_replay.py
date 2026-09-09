import json
from dataclasses import replace

import httpx
import pytest

from claude_sdk_proxy.domain import (
    CanonicalMessage,
    RedactedThinkingBlock,
    TextBlock,
    TextDelta,
    ThinkingCompleted,
    ToolCall,
    ToolCallBlock,
    ToolResultBlock,
)
from claude_sdk_proxy.openai_subscription.backend import Backend

from .test_backend import Auth, Bytes, sse, terminal, text_item
from .test_translation import request


def visible_message(events):
    # JSON frontend aggregates visible content before terminal thinking blocks.
    blocks = []
    for event in events:
        if isinstance(event, TextDelta):
            blocks.append(TextBlock(event.text))
        elif isinstance(event, ToolCall):
            blocks.append(ToolCallBlock(event.id, event.name, event.arguments))
    blocks.extend(e.block for e in events if isinstance(e, ThinkingCompleted))
    return CanonicalMessage("assistant", tuple(blocks))


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["text-only", "reasoning-text", "reasoning-tool"])
async def test_portable_carrier_preserves_native_assistant_on_fresh_backend(kind):
    reasoning = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "brief"}],
        "encrypted_content": "opaque",
    }
    call = {
        "type": "function_call",
        "id": "fc_native",
        "call_id": "call_A",
        "name": "lookup",
        "arguments": "{}",
    }
    output = (
        [text_item()]
        if kind == "text-only"
        else [reasoning, call if kind == "reasoning-tool" else text_item()]
    )
    captured = []

    def handler(r):
        captured.append(json.loads(r.content))
        return httpx.Response(200, stream=Bytes(sse(terminal(output))))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = Backend(Auth(), client=client)
        req = request()
        lease = await first.open_turn(req)
        events = [e async for e in lease.stream()]
        assert first.cache.bytes_used == 0
        await first.close()
        assistant = visible_message(events)
        carriers = [b for b in assistant.blocks if isinstance(b, RedactedThinkingBlock)]
        assert len(carriers) == 1
        assert carriers[0].data.startswith("openai-subscription:assistant:v1:")
        final_user = (
            CanonicalMessage("user", (ToolResultBlock("call_A", ("ok",), False),))
            if kind == "reasoning-tool"
            else CanonicalMessage.user_text("next")
        )
        continued = replace(req, messages=req.messages + (assistant, final_user))
        fresh = Backend(Auth(), client=client)
        lease = await fresh.open_turn(continued)
        _ = [e async for e in lease.stream()]
        assert captured[1]["input"][1:-1] == output
        await fresh.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change", ["removed", "malformed", "context", "dropped-reasoning"]
)
async def test_anthropic_cache_cannot_bypass_absent_or_incompatible_metadata(change):
    reasoning = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "brief"}],
        "encrypted_content": "opaque",
    }
    output = [reasoning, text_item()]
    captured = []

    def handler(r):
        captured.append(json.loads(r.content))
        return httpx.Response(200, stream=Bytes(sse(terminal(output))))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        backend = Backend(Auth(), client=client)
        req = request()
        lease = await backend.open_turn(req)
        events = [e async for e in lease.stream()]
        assistant = visible_message(events)
        carriers = [b for b in assistant.blocks if isinstance(b, RedactedThinkingBlock)]
        assert len(carriers) == 1
        if change == "removed":
            assistant = CanonicalMessage.assistant_text("hello")
        elif change == "malformed":
            assistant = CanonicalMessage(
                "assistant",
                (
                    TextBlock("hello"),
                    RedactedThinkingBlock(
                        "openai-subscription:assistant:v1:not-base64"
                    ),
                ),
            )
        elif change == "dropped-reasoning":
            assistant = CanonicalMessage("assistant", (TextBlock("hello"), carriers[0]))
        continued = replace(
            req, messages=req.messages + (assistant, CanonicalMessage.user_text("next"))
        )
        if change == "context":
            continued = replace(continued, system="changed")
        lease = await backend.open_turn(continued)
        _ = [e async for e in lease.stream()]
        assert not any(
            "encrypted_content" in i or "id" in i for i in captured[1]["input"]
        )
        assert any(
            i.get("content") == [{"type": "output_text", "text": "hello"}]
            for i in captured[1]["input"]
        )
        await backend.close()


@pytest.mark.anyio
async def test_chat_does_not_emit_portable_metadata_carrier():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, stream=Bytes(sse(terminal([text_item()]))))
        )
    ) as client:
        backend = Backend(Auth(), client=client)
        lease = await backend.open_turn(request(dialect="openai"))
        events = [e async for e in lease.stream()]
        assert not any(
            isinstance(e, ThinkingCompleted)
            and isinstance(e.block, RedactedThinkingBlock)
            for e in events
        )
        assert backend.cache.bytes_used > 0
        await backend.close()


@pytest.mark.anyio
async def test_carrier_is_single_and_bounded_with_multiple_reasoning_items(monkeypatch):
    from claude_sdk_proxy.openai_subscription import replay

    reasoning = [
        {
            "type": "reasoning",
            "id": f"rs_{i}",
            "summary": [],
            "encrypted_content": f"opaque-{i}",
        }
        for i in range(4)
    ]
    data = sse(terminal(reasoning + [text_item()]))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Bytes(data)))
    ) as client:
        backend = Backend(Auth(), client=client)
        lease = await backend.open_turn(request())
        events = [e async for e in lease.stream()]
        carriers = [
            e.block
            for e in events
            if isinstance(e, ThinkingCompleted)
            and isinstance(e.block, RedactedThinkingBlock)
        ]
        assert len(carriers) == 1
        assert len(carriers[0].data) <= replay.MAX_SIGNATURE_BYTES
        await backend.close()
    monkeypatch.setattr(replay, "MAX_SIGNATURE_BYTES", 256)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, stream=Bytes(sse(terminal([text_item("x" * 1000)])))
            )
        )
    ) as client:
        backend = Backend(Auth(), client=client)
        lease = await backend.open_turn(request())
        events = [e async for e in lease.stream()]
        assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "x" * 1000
        assert not any(
            isinstance(e, ThinkingCompleted)
            and isinstance(e.block, RedactedThinkingBlock)
            for e in events
        )
        await backend.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change", ["native-output", "missing-binding", "duplicate-carrier"]
)
async def test_carrier_cannot_override_visible_history_or_ambiguity(change):
    import base64

    from claude_sdk_proxy.openai_subscription.replay import ASSISTANT_PREFIX
    from claude_sdk_proxy.openai_subscription.translation import build_body

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, stream=Bytes(sse(terminal([text_item()]))))
        )
    ) as client:
        backend = Backend(Auth(), client=client)
        req = request()
        lease = await backend.open_turn(req)
        events = [e async for e in lease.stream()]
        assistant = visible_message(events)
        carrier = next(
            b for b in assistant.blocks if isinstance(b, RedactedThinkingBlock)
        )
        raw = json.loads(
            base64.urlsafe_b64decode(carrier.data[len(ASSISTANT_PREFIX) :])
        )
        if change == "native-output":
            raw["output"][0]["content"][0]["text"] = "injected answer"
        elif change == "missing-binding":
            raw.pop("scope")
        modified = RedactedThinkingBlock(
            ASSISTANT_PREFIX
            + base64.urlsafe_b64encode(json.dumps(raw).encode()).decode()
        )
        blocks = (TextBlock("hello"), modified) + (
            (modified,) if change == "duplicate-carrier" else ()
        )
        continued = replace(
            req,
            messages=req.messages
            + (
                CanonicalMessage("assistant", blocks),
                CanonicalMessage.user_text("next"),
            ),
        )
        assert build_body(continued, "acct")["input"][1] == {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "hello"}],
        }
        await backend.close()
