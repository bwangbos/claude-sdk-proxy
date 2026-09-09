import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from quaylet.domain import (
    CanonicalMessage,
    Completed,
    ResponseIdentity,
    TextDelta,
    ToolCallBlock,
    ToolResultBlock,
)
from quaylet.openai_subscription.backend import Backend, SubscriptionFailure
from quaylet.openai_subscription.events import parse_sse
from quaylet.openai_subscription.replay import ReplayCache, encode_reasoning

from .test_backend import Auth, Bytes, collect, sse, terminal, text_item
from .test_translation import request


@pytest.mark.anyio
async def test_generator_aclose_releases_open_http_without_caching_partial_output():
    stream = Bytes(
        sse(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**text_item(), "content": []},
            },
            {"type": "response.output_text.delta", "output_index": 0, "delta": "hello"},
        ),
        hang=True,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=stream))
    ) as client:
        backend = Backend(Auth(), client=client)
        lease = await backend.open_turn(request())
        iterator = lease.stream()
        while not isinstance(await anext(iterator), TextDelta):
            pass
        await iterator.aclose()
        assert stream.closed
        assert backend.cache.bytes_used == 0
        await backend.close()


@pytest.mark.anyio
async def test_shutdown_cancels_active_read_and_rejects_new_turns():
    stream = Bytes(b"", hang=True)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=stream))
    ) as client:
        backend = Backend(Auth(), client=client)
        lease = await backend.open_turn(request())

        async def consume():
            return [e async for e in lease.stream()]

        task = asyncio.create_task(consume())
        await stream.waiting.wait()
        await backend.close()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed
        with pytest.raises(SubscriptionFailure):
            await backend.open_turn(request())


@pytest.mark.anyio
async def test_shared_prefix_turns_run_concurrently_and_ambiguous_metadata_is_miss():
    started = 0
    both = asyncio.Event()

    class Rendezvous(Bytes):
        async def __aiter__(self):
            nonlocal started
            started += 1
            if started == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 1)
            yield self.data

    calls = 0
    bodies = []

    def handler(r):
        nonlocal calls
        calls += 1
        bodies.append(json.loads(r.content))
        native = {**text_item(), "id": f"msg_{calls}"}
        return httpx.Response(200, stream=Rendezvous(sse(terminal([native]))))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        backend = Backend(Auth(), client=client)
        req = request(dialect="openai")

        async def turn():
            lease = await backend.open_turn(req)
            return [e async for e in lease.stream()]

        await asyncio.gather(turn(), turn())
        continuation = replace(
            req,
            messages=req.messages
            + (
                CanonicalMessage.assistant_text("hello"),
                CanonicalMessage.user_text("next"),
            ),
        )
        lease = await backend.open_turn(continuation)
        _ = [e async for e in lease.stream()]
        assert "id" not in bodies[-1]["input"][1]
        await backend.close()


@pytest.mark.anyio
async def test_tool_results_replay_multiple_rounds_without_parked_execution():
    call = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_A",
        "name": "lookup",
        "arguments": "{}",
    }
    call2 = {**call, "id": "fc_2", "call_id": "call_B"}
    bodies = []

    def handler(r):
        bodies.append(json.loads(r.content))
        item = [call, call2, text_item()][len(bodies) - 1]
        return httpx.Response(200, stream=Bytes(sse(terminal([item]))))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        backend = Backend(Auth(), client=client)
        req = request(dialect="openai")
        lease = await backend.open_turn(req)
        _ = [e async for e in lease.stream()]
        req = replace(
            req,
            messages=req.messages
            + (
                CanonicalMessage("assistant", (ToolCallBlock("call_A", "lookup", {}),)),
                CanonicalMessage(
                    "user", (ToolResultBlock("call_A", ("first",), False),)
                ),
            ),
        )
        lease = await backend.open_turn(req)
        _ = [e async for e in lease.stream()]
        req = replace(
            req,
            messages=req.messages
            + (
                CanonicalMessage("assistant", (ToolCallBlock("call_B", "lookup", {}),)),
                CanonicalMessage(
                    "user", (ToolResultBlock("call_B", ("second",), False),)
                ),
            ),
        )
        lease = await backend.open_turn(req)
        _ = [e async for e in lease.stream()]
        assert [i.get("call_id") for i in bodies[2]["input"] if "call_id" in i] == [
            "call_A",
            "call_A",
            "call_B",
            "call_B",
        ]
        assert bodies[2]["input"][1]["id"] == "fc_1"
        await backend.close()


@pytest.mark.anyio
async def test_sse_multiline_data_and_bare_cr_frames():
    async def chunks():
        for chunk in (
            b":x\r",
            b'\rdata: {"type":\rdata: "response.created"}\r\r',
            b"\n",
        ):
            yield chunk

    assert [e async for e in parse_sse(chunks())] == [{"type": "response.created"}]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "usage",
    [
        {
            "input_tokens": 1,
            "output_tokens": 1,
            "input_tokens_details": {"cached_tokens": 2},
        },
        {
            "input_tokens": 1,
            "output_tokens": 1,
            "output_tokens_details": {"reasoning_tokens": 2},
        },
        {"input_tokens": True, "output_tokens": 0},
        {},
    ],
)
async def test_invalid_usage_cannot_be_reported_as_valid_totals(usage):
    with pytest.raises(SubscriptionFailure):
        await collect(sse(terminal([], usage=usage)))


@pytest.mark.anyio
async def test_invalid_upstream_identity_is_not_header_or_diagnostic_evidence():
    events, _ = await collect(sse(terminal([text_item()], model="SECRET\nheader:bad")))
    assert all(not e.verified for e in events if isinstance(e, ResponseIdentity))
    assert isinstance(events[-1], Completed)


@pytest.mark.anyio
async def test_retried_attempt_model_is_not_final_identity_evidence():
    calls = 0

    def handler(r):
        nonlocal calls
        calls += 1
        data = (
            sse(
                {
                    "type": "response.created",
                    "response": {"model": "gpt-6-astra-snapshot"},
                },
                {"type": "error", "code": "server_error"},
            )
            if calls == 1
            else sse(terminal([text_item()]))
        )
        return httpx.Response(200, stream=Bytes(data))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        backend = Backend(Auth(), client=client)
        lease = await backend.open_turn(request())
        events = [e async for e in lease.stream()]
        assert [e for e in events if isinstance(e, ResponseIdentity)][
            -1
        ].verified is False
        assert "reported_model" not in lease.diagnostics
        await backend.close()


def test_aggregate_cache_limit_evicts_even_when_entry_count_has_room():
    cache = ReplayCache(max_entries=100, max_bytes=300)
    req = request()
    for i in range(20):
        history = req.messages + (CanonicalMessage.assistant_text(str(i)),)
        cache.put(req, "acct", history, [{"data": "x" * 100}])
        assert cache.bytes_used <= 300
    earliest = replace(
        req,
        messages=req.messages
        + (CanonicalMessage.assistant_text("0"), CanonicalMessage.user_text("next")),
    )
    assert cache.get(earliest, "acct") is None


def test_oversize_envelope_is_rejected_without_exposing_ciphertext():
    with pytest.raises(ValueError) as error:
        encode_reasoning(
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [],
                "encrypted_content": "SECRET" * 200000,
            },
            "acct",
            "gpt-6-astra",
            scope="0" * 64,
        )
    assert "SECRET" not in str(error.value)


@pytest.mark.anyio
async def test_refusal_delta_streams_distinctly_and_replays_visible_fallback():
    from quaylet import domain

    item = {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "content": [{"type": "refusal", "refusal": "Cannot help"}],
    }
    bodies = []

    def handler(r):
        bodies.append(json.loads(r.content))
        return httpx.Response(
            200,
            stream=Bytes(
                sse(
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {**item, "content": []},
                    },
                    {
                        "type": "response.refusal.delta",
                        "output_index": 0,
                        "delta": "Cannot ",
                    },
                    terminal([item]),
                )
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        backend = Backend(Auth(), client=client)
        req = request(dialect="openai")
        lease = await backend.open_turn(req)
        events = [e async for e in lease.stream()]
        assert [e.text for e in events if isinstance(e, domain.RefusalDelta)] == [
            "Cannot ",
            "help",
        ]
        assert not any(isinstance(e, TextDelta) for e in events)
        continuation = replace(
            req,
            messages=req.messages
            + (
                CanonicalMessage.assistant_text("Cannot help"),
                CanonicalMessage.user_text("next"),
            ),
        )
        lease = await backend.open_turn(continuation)
        _ = [e async for e in lease.stream()]
        assert bodies[1]["input"][1]["content"][0] == {
            "type": "refusal",
            "refusal": "Cannot help",
        }
        backend.cache.clear()
        lease = await backend.open_turn(continuation)
        _ = [e async for e in lease.stream()]
        assert bodies[2]["input"][1]["content"][0] == {
            "type": "output_text",
            "text": "Cannot help",
        }
        await backend.close()


@pytest.mark.anyio
async def test_terminal_omission_does_not_erase_already_received_ciphertext():
    from quaylet.domain import ThinkingCompleted
    from quaylet.openai_subscription.replay import decode_reasoning

    item = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [],
        "encrypted_content": "opaque",
    }
    events, _ = await collect(
        sse(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**item, "encrypted_content": ""},
            },
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            terminal([{k: v for k, v in item.items() if k != "encrypted_content"}]),
        )
    )
    completed = next(e for e in events if isinstance(e, ThinkingCompleted))
    from quaylet.openai_subscription.replay import envelope_scope

    scope = envelope_scope(
        request(),
        "acct",
        request().messages + (CanonicalMessage("assistant", (completed.block,)),),
    )
    assert (
        decode_reasoning(completed.block.signature, "acct", "gpt-6-astra", scope=scope)[
            "encrypted_content"
        ]
        == "opaque"
    )


@pytest.mark.anyio
async def test_changed_item_id_at_done_is_rejected_before_tool_emission():
    from quaylet.domain import ToolCall

    item = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_A",
        "name": "lookup",
        "arguments": "{}",
    }
    data = sse(
        {"type": "response.output_item.added", "output_index": 0, "item": item},
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {**item, "id": "fc_changed"},
        },
    )
    events = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Bytes(data)))
    ) as client:
        backend = Backend(Auth(), client=client)
        lease = await backend.open_turn(request())
        with pytest.raises(SubscriptionFailure):
            async for event in lease.stream():
                events.append(event)
        assert not any(isinstance(e, ToolCall) for e in events)
        await backend.close()


@pytest.mark.anyio
async def test_failure_diagnostics_record_only_safe_metadata(caplog):
    import logging

    caplog.set_level(logging.INFO, logger="quaylet.diagnostics")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                403,
                stream=Bytes(b"SECRET ciphertext"),
                headers={"x-request-id": "req_safe"},
            )
        )
    ) as client:
        backend = Backend(Auth(), client=client)
        lease = await backend.open_turn(request())
        with pytest.raises(SubscriptionFailure):
            _ = [e async for e in lease.stream()]
        diagnostics = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict)
            and r.msg.get("event") == "openai_subscription_turn"
        ]
        assert diagnostics[-1]["provider"] == "openai-subscription"
        assert diagnostics[-1]["requested_model"] == "gpt-6-astra"
        assert diagnostics[-1]["upstream_request_id"] == "req_safe"
        assert diagnostics[-1]["status"] == 403
        assert diagnostics[-1]["category"] == "access_denied"
        assert "SECRET" not in caplog.text and "ciphertext" not in caplog.text
        await backend.close()
