import asyncio
import base64
import json
from dataclasses import replace

import httpx
import pytest

from quaylet.domain import (
    CanonicalMessage,
    ImageBlock,
    RequestValidationError,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from quaylet.openai_subscription.backend import Backend, SubscriptionFailure
from quaylet.openai_subscription.replay import (
    ReplayCache,
    decode_reasoning,
    encode_reasoning,
    valid_reasoning,
)
from quaylet.openai_subscription.translation import build_body
from tests.fixtures.image_data import solid_png

from .test_backend import Auth, Bytes, collect, sse, terminal, text_item
from .test_translation import request


def test_replay_does_not_conflate_user_image_text_order():
    image = ImageBlock("image/png", solid_png())
    req = replace(
        request(),
        messages=(
            CanonicalMessage("user", (TextBlock("before"), image, TextBlock("after"))),
        ),
    )
    history = req.messages + (CanonicalMessage.assistant_text("answer"),)
    cache = ReplayCache()
    cache.put(req, "acct", history, [{"id": "opaque"}])
    changed = (
        CanonicalMessage("user", (image, TextBlock("beforeafter"))),
        history[-1],
        CanonicalMessage.user_text("next"),
    )
    assert cache.get(replace(req, messages=changed), "acct") is None


def test_images_and_image_tool_results_stay_in_caller_order():
    image = ImageBlock("image/png", solid_png())
    req = replace(
        request(),
        messages=(
            CanonicalMessage("user", (TextBlock("look"), image)),
            CanonicalMessage("assistant", (ToolCallBlock("call_A", "lookup", {}),)),
            CanonicalMessage(
                "user", (ToolResultBlock("call_A", ("before", image, "after"), False),)
            ),
        ),
    )
    items = build_body(req, "acct")["input"]
    assert items[0]["content"][1]["image_url"] == "data:image/png;base64," + image.data
    assert [p["type"] for p in items[-1]["output"]] == [
        "input_text",
        "input_image",
        "input_text",
    ]
    assert len(items) == 3


@pytest.mark.parametrize(
    "changes",
    [
        {"v": True},
        {"provider": "claude"},
        {"item": {"type": "function_call"}},
        {"extra": "bad"},
    ],
)
def test_envelope_shape_rejects_wrong_version_provider_item_and_extra_fields(changes):
    signature = encode_reasoning(
        {"type": "reasoning", "id": "rs_1", "summary": []},
        "acct",
        "gpt-6-astra",
        scope="0" * 64,
    )
    prefix = "openai-subscription:v1:"
    raw = json.loads(base64.urlsafe_b64decode(signature[len(prefix) :]))
    raw.update(changes)
    bad = prefix + base64.urlsafe_b64encode(json.dumps(raw).encode()).decode()
    with pytest.raises(RequestValidationError):
        decode_reasoning(bad, "acct", "gpt-6-astra", scope="0" * 64)


@pytest.mark.parametrize(
    "optional",
    [
        {"content": None},
        {"content": [{"type": "reasoning_text", "text": "private trace"}]},
        {"encrypted_content": None, "status": None},
    ],
    ids=["null-content", "reasoning-content", "nullable-fields"],
)
def test_reasoning_envelope_preserves_official_optional_fields(optional):
    item = {"type": "reasoning", "id": "rs_1", "summary": [], **optional}
    signature = encode_reasoning(item, "acct", "gpt-6-astra", scope="0" * 64)
    assert decode_reasoning(signature, "acct", "gpt-6-astra", scope="0" * 64) == item


@pytest.mark.parametrize(
    "content",
    [
        {},
        [None],
        [{"type": "summary_text", "text": "wrong kind"}],
        [{"type": "reasoning_text"}],
        [{"type": "reasoning_text", "text": 1}],
        [{"type": "reasoning_text", "text": "ok", "extra": True}],
    ],
)
def test_reasoning_item_rejects_malformed_content(content):
    item = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [],
        "content": content,
    }
    assert not valid_reasoning(item)


@pytest.mark.anyio
async def test_transient_sse_error_retries_once_before_content():
    calls = 0

    def handler(r):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            stream=Bytes(
                sse({"type": "error", "code": "server_error", "message": "PRIVATE"})
                if calls == 1
                else sse(terminal([text_item()]))
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        backend = Backend(Auth(), client=client)
        lease = await backend.open_turn(request())
        result = [e async for e in lease.stream()]
        assert result[-1].stop_reason == "end_turn"
        assert calls == 2
        await backend.close()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["arguments", "type", "delta-type"])
async def test_inconsistent_tool_completion_is_rejected_without_cache(change):
    item = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_A",
        "name": "lookup",
        "arguments": '{"x":1}',
    }
    final = (
        {**item, "arguments": '{"x":2}'}
        if change == "arguments"
        else {**text_item(), "id": "fc_1"}
    )
    events = [
        {"type": "response.output_item.added", "output_index": 0, "item": item},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        terminal([final]),
    ]
    if change == "delta-type":
        events.insert(
            1, {"type": "response.output_text.delta", "output_index": 0, "delta": "bad"}
        )
        events[-1] = terminal([item])
    with pytest.raises(SubscriptionFailure):
        await collect(sse(*events))


@pytest.mark.anyio
async def test_repeated_cancellation_does_not_abandon_response_cleanup():
    closing = asyncio.Event()
    release = asyncio.Event()

    class SlowClose(Bytes):
        async def aclose(self):
            closing.set()
            await release.wait()
            self.closed = True

    stream = SlowClose(b"", hang=True)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=stream))
    ) as client:
        backend = Backend(Auth(), client=client)
        lease = await backend.open_turn(request())

        async def consume():
            return [e async for e in lease.stream()]

        task = asyncio.create_task(consume())
        await stream.waiting.wait()
        task.cancel()
        await closing.wait()
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed
        await backend.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "authentication"),
        (403, "access_denied"),
        (429, "rate_limit"),
        (503, "upstream_error"),
    ],
)
async def test_http_failures_have_bounded_attempts_and_no_body_disclosure(
    status, expected
):
    calls = 0
    streams = []

    def handler(r):
        nonlocal calls
        calls += 1
        stream = Bytes(b"SECRET")
        streams.append(stream)
        return httpx.Response(status, stream=stream)

    auth = Auth()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        backend = Backend(auth, client=client)
        lease = await backend.open_turn(request())
        with pytest.raises(SubscriptionFailure) as error:
            _ = [e async for e in lease.stream()]
        assert error.value.category == expected
        assert "SECRET" not in str(error.value)
        assert calls == (1 if status == 403 else 2)
        assert all(s.closed for s in streams)
        await backend.close()


@pytest.mark.anyio
async def test_incomplete_terminal_never_populates_replay_cache():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                stream=Bytes(
                    sse(
                        {
                            "type": "response.incomplete",
                            "response": {
                                "status": "incomplete",
                                "output": [text_item()],
                                "incomplete_details": {"reason": "max_output_tokens"},
                            },
                        }
                    )
                ),
            )
        )
    ) as client:
        backend = Backend(Auth(), client=client)
        lease = await backend.open_turn(request())
        events = [e async for e in lease.stream()]
        assert events[-1].stop_reason == "max_tokens"
        assert backend.cache.bytes_used == 0
        await backend.close()


@pytest.mark.anyio
async def test_oversize_request_is_rejected_before_auth_or_http(monkeypatch):
    from quaylet.openai_subscription import translation

    monkeypatch.setattr(translation, "MAX_REQUEST_BYTES", 256, raising=False)

    class NoAuth(Auth):
        async def credentials(self):
            pytest.fail("Oversize input reached auth")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: pytest.fail("Oversize input reached HTTP")
        )
    ) as client:
        backend = Backend(NoAuth(), client=client)
        req = replace(request(), system="x" * 1024)
        with pytest.raises(RequestValidationError):
            await backend.open_turn(req)
        await backend.close()
