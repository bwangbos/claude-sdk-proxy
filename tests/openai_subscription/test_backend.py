import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from claude_sdk_proxy.domain import (
    CanonicalMessage,
    Completed,
    ResponseIdentity,
    TextDelta,
    ThinkingCompleted,
    ThinkingDelta,
    ToolCall,
)
from claude_sdk_proxy.openai_subscription.backend import Backend, SubscriptionFailure
from claude_sdk_proxy.openai_subscription.replay import decode_reasoning
from claude_sdk_proxy.openai_subscription.storage import Credentials

from .test_translation import request


class Auth:
    def __init__(self):
        self.refreshes = 0

    async def credentials(self):
        return Credentials("access", "refresh", 9999999999, "acct", "rev")

    async def refresh_once(self, rejected_access_token):
        assert rejected_access_token == "access"
        self.refreshes += 1
        return Credentials("new-access", "refresh", 9999999999, "acct", "rev")


class Bytes(httpx.AsyncByteStream):
    def __init__(self, data, *, fragmented=False, hang=False):
        self.data = data
        self.fragmented = fragmented
        self.hang = hang
        self.closed = False
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        if self.fragmented:
            for byte in self.data:
                yield bytes([byte])
        else:
            yield self.data
        if self.hang:
            self.waiting.set()
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


def sse(*events):
    return b": comment\r\n\r\n" + b"".join(
        b"data: " + json.dumps(e, ensure_ascii=False).encode() + b"\r\n\r\n"
        for e in events
    )


def terminal(output=(), **kwargs):
    return {
        "type": "response.completed",
        "response": {"status": "completed", "output": list(output), **kwargs},
    }


def text_item(text="hello"):
    return {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "phase": "final_answer",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


async def collect(data, *, req=None, fragmented=False):
    stream = Bytes(data, fragmented=fragmented)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=stream))
    )
    backend = Backend(Auth(), client=client)
    try:
        lease = await backend.open_turn(req or request())
        events = [e async for e in lease.stream()]
        return events, stream
    finally:
        await backend.close()
        await client.aclose()


@pytest.mark.anyio
async def test_fragmented_utf8_and_frame_preserve_streamed_text_and_truthful_usage():
    events, stream = await collect(
        sse(
            {
                "type": "response.created",
                "response": {"model": "gpt-6-astra-2026-09-01"},
            },
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**text_item(""), "content": []},
            },
            {
                "type": "response.output_text.delta",
                "output_index": 0,
                "content_index": 0,
                "delta": "hé😀",
            },
            terminal(
                [text_item("hé😀")],
                usage={
                    "input_tokens": 100,
                    "output_tokens": 40,
                    "input_tokens_details": {"cached_tokens": 70},
                    "output_tokens_details": {"reasoning_tokens": 30},
                },
            ),
        ),
        fragmented=True,
    )
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["hé😀"]
    identities = [e for e in events if isinstance(e, ResponseIdentity)]
    assert identities[0].verified is False
    assert identities[-1].actual_model == "gpt-6-astra-2026-09-01"
    assert identities[-1].verified is True
    end = events[-1]
    assert isinstance(end, Completed)
    assert dict(end.usage) == {
        "input_tokens": 30,
        "cache_read_input_tokens": 70,
        "output_tokens": 40,
        "reasoning_tokens": 30,
    }
    assert end.provider == "openai-subscription"
    assert stream.closed


@pytest.mark.anyio
async def test_late_reasoning_ciphertext_backfilled_after_immediate_summary_delta():
    partial = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "brief"}],
    }
    events, _ = await collect(
        sse(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**partial, "summary": []},
            },
            {
                "type": "response.reasoning_summary_text.delta",
                "output_index": 0,
                "summary_index": 0,
                "delta": "brief",
            },
            {"type": "response.output_item.done", "output_index": 0, "item": partial},
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {**text_item(), "content": []},
            },
            {
                "type": "response.output_text.delta",
                "output_index": 1,
                "content_index": 0,
                "delta": "hello",
            },
            terminal([{**partial, "encrypted_content": "CIPHERTEXT"}, text_item()]),
        )
    )
    delta = next(e for e in events if isinstance(e, ThinkingDelta))
    assert delta.text == "brief" and delta.index == 0
    assert events.index(delta) < next(
        i for i, e in enumerate(events) if isinstance(e, TextDelta)
    )
    completed = next(e for e in events if isinstance(e, ThinkingCompleted))
    assert (
        decode_reasoning(completed.block.signature, "acct", "gpt-6-astra")[
            "encrypted_content"
        ]
        == "CIPHERTEXT"
    )


@pytest.mark.anyio
async def test_fragmented_multiple_tool_arguments_preserve_distinct_native_ids():
    a = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_A",
        "name": "lookup",
        "arguments": '{"a":1}',
    }
    b = {
        "type": "function_call",
        "id": "fc_2",
        "call_id": "call_B",
        "name": "lookup",
        "arguments": "{}",
    }
    events, _ = await collect(
        sse(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**a, "arguments": ""},
            },
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "delta": '{"a":',
            },
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "delta": "1}",
            },
            {"type": "response.output_item.done", "output_index": 0, "item": a},
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {**b, "arguments": ""},
            },
            {"type": "response.output_item.done", "output_index": 1, "item": b},
            terminal([a, b]),
        )
    )
    assert [(e.id, dict(e.arguments)) for e in events if isinstance(e, ToolCall)] == [
        ("call_A", {"a": 1}),
        ("call_B", {}),
    ]
    assert events[-1].stop_reason == "tool_use"


@pytest.mark.anyio
async def test_missing_usage_is_not_zero_and_refusal_is_explicit():
    item = {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "content": [{"type": "refusal", "refusal": "Cannot help"}],
    }
    events, _ = await collect(sse(terminal([item])))
    assert events[-1].usage is None
    assert events[-1].refusal == "Cannot help"
    assert events[-1].stop_reason == "refusal"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "data,category",
    [
        (sse({"type": "error", "error": {"message": "SECRET"}}), "upstream_error"),
        (sse({"type": "response.created", "response": {}}), "missing_completion"),
        (b"data: nope\n\n", "invalid_response"),
        (b"data: " + b"x" * (1024 * 1024 + 1), "buffer_limit"),
    ],
    ids=["upstream-error", "missing-terminal", "bad-json", "large-frame"],
)
async def test_stream_failures_are_sanitized_and_close_http(data, category):
    with pytest.raises(SubscriptionFailure) as failure:
        await collect(data)
    assert failure.value.category == category
    assert "SECRET" not in str(failure.value)


@pytest.mark.anyio
async def test_auth_and_transient_retries_before_content_only_and_fixed_headers():
    captured = []
    streams = []

    def handler(r):
        captured.append(r)
        status = [401, 503, 200][len(captured) - 1]
        stream = Bytes(sse(terminal([text_item()])))
        streams.append(stream)
        return httpx.Response(status, stream=stream, headers={"x-request-id": "req_1"})

    auth = Auth()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        backend = Backend(auth, client=client)
        lease = await backend.open_turn(request())
        events = [e async for e in lease.stream()]
        assert events[-1].stop_reason == "end_turn"
        assert len(captured) == 3 and auth.refreshes == 1
        assert (
            str(captured[-1].url) == "https://chatgpt.com/backend-api/codex/responses"
        )
        assert captured[-1].headers["Authorization"] == "Bearer new-access"
        assert captured[-1].headers["chatgpt-account-id"] == "acct"
        assert captured[-1].headers["User-Agent"].startswith("claude-sdk-proxy")
        assert captured[-1].headers["originator"] == "claude-sdk-proxy"
        assert json.loads(captured[-1].content)["instructions"] == ""
        assert all(s.closed for s in streams)
        await backend.close()
        assert not client.is_closed


@pytest.mark.anyio
async def test_after_content_failure_is_not_restarted():
    calls = 0

    def handler(r):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            stream=Bytes(
                sse(
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {**text_item(), "content": []},
                    },
                    {
                        "type": "response.output_text.delta",
                        "output_index": 0,
                        "delta": "partial",
                    },
                    {
                        "type": "error",
                        "error": {"code": "server_error", "message": "SECRET"},
                    },
                )
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        backend = Backend(Auth(), client=client)
        lease = await backend.open_turn(request())
        with pytest.raises(SubscriptionFailure):
            _ = [e async for e in lease.stream()]
        assert calls == 1
        assert backend.cache.bytes_used == 0
        await backend.close()


@pytest.mark.anyio
async def test_abort_cancels_pending_read_and_backend_close_cleans_active_leases():
    streams = []

    def handler(r):
        stream = Bytes(sse({"type": "response.created", "response": {}}), hang=True)
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        backend = Backend(Auth(), client=client)
        lease = await backend.open_turn(request())

        async def run():
            return [e async for e in lease.stream()]

        task = asyncio.create_task(run())
        while not streams:
            await asyncio.sleep(0)
        await streams[0].waiting.wait()
        await lease.abort()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert streams[0].closed
        await backend.close()


@pytest.mark.anyio
async def test_total_timeout_closes_http():
    stream = Bytes(b"", hang=True)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=stream))
    ) as client:
        backend = Backend(Auth(), client=client, timeout=0.01)
        lease = await backend.open_turn(request())
        with pytest.raises(SubscriptionFailure) as error:
            _ = [e async for e in lease.stream()]
        assert error.value.category == "timeout"
        assert stream.closed
        await backend.close()


@pytest.mark.anyio
async def test_completed_replay_preserves_metadata_and_compaction_drops_it():
    bodies = []

    def handler(r):
        bodies.append(json.loads(r.content))
        return httpx.Response(200, stream=Bytes(sse(terminal([text_item()]))))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        backend = Backend(Auth(), client=client)
        req = request(dialect="openai")
        lease = await backend.open_turn(req)
        _ = [e async for e in lease.stream()]
        history = req.messages + (
            CanonicalMessage.assistant_text("hello"),
            CanonicalMessage.user_text("next"),
        )
        lease = await backend.open_turn(replace(req, messages=history))
        _ = [e async for e in lease.stream()]
        assert bodies[1]["input"][1]["id"] == "msg_1"
        assert bodies[1]["input"][1]["phase"] == "final_answer"
        lease = await backend.open_turn(
            replace(req, messages=(CanonicalMessage.user_text("compacted"),))
        )
        _ = [e async for e in lease.stream()]
        assert bodies[2]["input"] == [
            {"role": "user", "content": [{"type": "input_text", "text": "compacted"}]}
        ]
        await backend.close()
