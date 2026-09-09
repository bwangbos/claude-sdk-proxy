import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from claude_sdk_proxy.domain import (
    CanonicalMessage,
    Completed,
    RedactedThinkingBlock,
    ResponseIdentity,
    TextBlock,
    TextDelta,
    ThinkingCompleted,
    ThinkingDelta,
    ToolCall,
    ToolCallBlock,
    ToolResultBlock,
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
    from claude_sdk_proxy.domain import TextBlock
    from claude_sdk_proxy.openai_subscription.replay import envelope_scope

    scope = envelope_scope(
        request(),
        "acct",
        request().messages
        + (CanonicalMessage("assistant", (completed.block, TextBlock("hello"))),),
    )
    assert (
        decode_reasoning(completed.block.signature, "acct", "gpt-6-astra", scope=scope)[
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
@pytest.mark.parametrize("dialect", ["anthropic", "openai"])
async def test_sparse_terminal_replays_completed_done_items_in_both_dialects(dialect):
    reasoning = {
        "type": "reasoning",
        "id": "rs_native",
        "summary": [{"type": "summary_text", "text": "brief"}],
        "content": [{"type": "reasoning_text", "text": "private trace"}],
        "encrypted_content": "opaque",
        "status": "completed",
    }
    message = {
        **text_item("hello"),
        "id": "msg_native",
        "phase": "final_answer",
        "status": "completed",
    }
    call = {
        "type": "function_call",
        "id": "fc_native",
        "call_id": "call_native",
        "name": "lookup",
        "arguments": '{"a":1}',
        "status": "completed",
    }
    output = [reasoning, message, call]
    usage = {
        "input_tokens": 11,
        "output_tokens": 7,
        "input_tokens_details": {"cached_tokens": 3},
        "output_tokens_details": {"reasoning_tokens": 2},
    }
    first = sse(
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**reasoning, "summary": []},
        },
        {
            "type": "response.reasoning_summary_text.delta",
            "output_index": 0,
            "delta": "brief",
        },
        {"type": "response.output_item.done", "output_index": 0, "item": reasoning},
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {**message, "content": []},
        },
        {"type": "response.output_text.delta", "output_index": 1, "delta": "hello"},
        {"type": "response.output_item.done", "output_index": 1, "item": message},
        {
            "type": "response.output_item.added",
            "output_index": 2,
            "item": {**call, "arguments": ""},
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 2,
            "delta": '{"a":1}',
        },
        {"type": "response.output_item.done", "output_index": 2, "item": call},
        terminal([], usage=usage),
    )
    captured = []
    responses = iter([first, sse(terminal([text_item("next")]))])

    def handler(r):
        captured.append(json.loads(r.content))
        return httpx.Response(200, stream=Bytes(next(responses)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        backend = Backend(Auth(), client=client)
        req = request(dialect=dialect)
        lease = await backend.open_turn(req)
        events = [event async for event in lease.stream()]
        assert [e.text for e in events if isinstance(e, TextDelta)] == ["hello"]
        assert [e.text for e in events if isinstance(e, ThinkingDelta)] == ["brief"]
        calls = [
            (e.id, e.name, dict(e.arguments))
            for e in events
            if isinstance(e, ToolCall)
        ]
        assert calls == [("call_native", "lookup", {"a": 1})]
        assert dict(events[-1].usage) == {
            "input_tokens": 8,
            "cache_read_input_tokens": 3,
            "output_tokens": 7,
            "reasoning_tokens": 2,
        }

        completed = next(
            e.block
            for e in events
            if isinstance(e, ThinkingCompleted)
            and not isinstance(e.block, RedactedThinkingBlock)
        )
        blocks = (
            completed,
            TextBlock("hello"),
            ToolCallBlock("call_native", "lookup", {"a": 1}),
        )
        carrier = next(
            (
                e.block
                for e in events
                if isinstance(e, ThinkingCompleted)
                and isinstance(e.block, RedactedThinkingBlock)
            ),
            None,
        )
        assistant = CanonicalMessage(
            "assistant", blocks + ((carrier,) if carrier is not None else ())
        )
        continued = replace(
            req,
            messages=req.messages
            + (
                assistant,
                CanonicalMessage(
                    "user", (ToolResultBlock("call_native", ("ok",), False),)
                ),
            ),
        )
        lease = await backend.open_turn(continued)
        _ = [event async for event in lease.stream()]
        assert captured[1]["input"][1:4] == output
        await backend.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "events",
    [
        [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**text_item(""), "content": []},
            }
        ],
        [
            {
                "type": "response.output_item.done",
                "output_index": 1,
                "item": text_item(),
            }
        ],
        [
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": text_item(),
            },
            {
                "type": "response.output_item.done",
                "output_index": 2,
                "item": {**text_item(), "id": "msg_2"},
            },
        ],
    ],
    ids=["added-only", "missing-zero-boundary", "noncontiguous-boundary"],
)
async def test_sparse_terminal_rejects_unfinished_or_noncontiguous_items(events):
    with pytest.raises(SubscriptionFailure) as failure:
        await collect(sse(*events, terminal([])))
    assert failure.value.category == "invalid_response"


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


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change",
    ["prefix", "system", "tools", "compaction", "answer", "summary", "missing-binding"],
)
async def test_emitted_envelope_cannot_restore_changed_context(change):
    import base64

    from claude_sdk_proxy.domain import TextBlock, ThinkingBlock, ToolDefinition
    from claude_sdk_proxy.openai_subscription.replay import PREFIX
    from claude_sdk_proxy.openai_subscription.translation import build_body

    req = replace(
        request(),
        messages=(
            CanonicalMessage.user_text("old context"),
            CanonicalMessage.assistant_text("old answer"),
            CanonicalMessage.user_text("hi"),
        ),
    )
    reasoning = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "brief"}],
        "encrypted_content": "old opaque state",
    }
    events, _ = await collect(sse(terminal([reasoning, text_item()])), req=req)
    block = next(e.block for e in events if isinstance(e, ThinkingCompleted))
    assistant = CanonicalMessage("assistant", (block, TextBlock("hello")))
    continued = replace(
        req, messages=req.messages + (assistant, CanonicalMessage.user_text("next"))
    )
    assert any(
        i.get("encrypted_content") == "old opaque state"
        for i in build_body(continued, "acct")["input"]
    )
    if change == "prefix":
        continued = replace(
            continued,
            messages=(CanonicalMessage.user_text("rewritten"),)
            + continued.messages[1:],
        )
    elif change == "system":
        continued = replace(continued, system="new system")
    elif change == "tools":
        continued = replace(
            continued, tools=(ToolDefinition("new_tool", "new", {"type": "object"}),)
        )
    elif change == "compaction":
        continued = replace(continued, messages=continued.messages[2:])
    elif change in {"answer", "summary"}:
        edited = CanonicalMessage(
            "assistant",
            (
                ThinkingBlock(
                    "changed" if change == "summary" else block.thinking,
                    block.signature,
                ),
                TextBlock("changed" if change == "answer" else "hello"),
            ),
        )
        continued = replace(
            continued,
            messages=continued.messages[:-2] + (edited, continued.messages[-1]),
        )
    else:
        envelope = json.loads(base64.urlsafe_b64decode(block.signature[len(PREFIX) :]))
        envelope.pop("scope", None)
        signature = (
            PREFIX + base64.urlsafe_b64encode(json.dumps(envelope).encode()).decode()
        )
        edited = CanonicalMessage(
            "assistant", (ThinkingBlock(block.thinking, signature), TextBlock("hello"))
        )
        continued = replace(
            continued,
            messages=continued.messages[:-2] + (edited, continued.messages[-1]),
        )
    assert not any(
        i.get("type") == "reasoning" for i in build_body(continued, "acct")["input"]
    )


@pytest.mark.anyio
async def test_added_ciphertext_survives_done_and_terminal_omission():
    from claude_sdk_proxy.domain import TextBlock
    from claude_sdk_proxy.openai_subscription.translation import build_body

    item = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [],
        "encrypted_content": "opaque",
    }
    omitted = {k: v for k, v in item.items() if k != "encrypted_content"}
    events, _ = await collect(
        sse(
            {"type": "response.output_item.added", "output_index": 0, "item": item},
            {"type": "response.output_item.done", "output_index": 0, "item": omitted},
            terminal([omitted]),
        )
    )
    block = next(e.block for e in events if isinstance(e, ThinkingCompleted))
    req = request()
    continued = replace(
        req,
        messages=req.messages
        + (
            CanonicalMessage("assistant", (block, TextBlock(""))),
            CanonicalMessage.user_text("next"),
        ),
    )
    assert build_body(continued, "acct")["input"][1]["encrypted_content"] == "opaque"


@pytest.mark.anyio
async def test_terminal_frame_with_final_bare_cr_is_completed():
    data = b"data: " + json.dumps(terminal([text_item()])).encode() + b"\r\r"
    events, stream = await collect(data, fragmented=True)
    assert isinstance(events[-1], Completed)
    assert events[-1].stop_reason == "end_turn"
    assert stream.closed
