from __future__ import annotations

import asyncio
import copy
from dataclasses import replace

import pytest

from quaylet.app import create_app
from quaylet.domain import (
    BackendFailure,
    CanonicalMessage,
    ResponseIdentity,
    ToolResultBlock,
)
from quaylet.sessions import SessionCapacity, SessionConflict, SessionRegistry
from tests.gateway.asgi_client import lifespan_app, post_json
from tests.gateway.test_tool_http import tool_body
from tests.gateway.test_tool_sessions import (
    ToolFactory,
    ToolSession,
    call_boundary,
    collect,
    continuation,
    echo_tool,
    final_boundary,
    first_request,
)


@pytest.mark.anyio
@pytest.mark.parametrize("same_key", [False, True])
@pytest.mark.parametrize("cancel_cleanup", [False, True])
async def test_expired_rebase_respects_new_owner_and_capacity(same_key, cancel_cleanup):
    entered, release = asyncio.Event(), asyncio.Event()
    cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event()
    old = ToolSession((call_boundary("call_one"),))
    candidate = ToolSession(
        (final_boundary("recovered"),), start_entered=entered, start_release=release
    )
    newer = ToolSession((final_boundary("newer"),), generation_release=asyncio.Event())

    class BlockingCleanupRegistry(SessionRegistry):
        async def _remove_tool(self, actor):
            if cancel_cleanup and actor.backend is candidate:
                cleanup_entered.set()
                await cleanup_release.wait()
            await super()._remove_tool(actor)

    registry = BlockingCleanupRegistry(
        ToolFactory((old, candidate, newer)), max_sessions=1
    )
    first = first_request(tools=(echo_tool(),))
    try:
        lease = await registry.open_turn(first, None)
        sid = lease.response_headers["X-Quaylet-Session"]
        await collect(lease.stream())
        correct = continuation(
            first,
            (call_boundary("call_one")[0],),
            (ToolResultBlock("call_one", ("saved",), False),),
        )
        rewritten = replace(
            correct,
            messages=(
                CanonicalMessage.user_text("rewritten"),
                *correct.messages[1:],
            ),
        )
        task = asyncio.create_task(registry.open_turn(rewritten, sid))
        await asyncio.wait_for(entered.wait(), 2)
        old.fail_bridge()
        await asyncio.wait_for(old.closed.wait(), 2)
        newer_lease = await registry.open_turn(first, sid if same_key else "different")
        release.set()
        if cancel_cleanup:
            await asyncio.wait_for(cleanup_entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            cleanup_release.set()
        else:
            with pytest.raises(SessionConflict if same_key else SessionCapacity):
                await task
        await asyncio.wait_for(candidate.closed.wait(), 2)
        assert not newer.closed.is_set()
        assert newer_lease.response_headers["X-Quaylet-Session"] == (
            sid if same_key else "different"
        )
    finally:
        release.set()
        cleanup_release.set()
        await registry.close()


@pytest.mark.anyio
@pytest.mark.parametrize("bridge_failure", [False, True])
async def test_recovery_survives_old_session_expiring_during_candidate_start(
    bridge_failure,
):
    entered, release = asyncio.Event(), asyncio.Event()
    old = ToolSession((call_boundary("call_one"),))
    candidate = ToolSession(
        (final_boundary("recovered"),), start_entered=entered, start_release=release
    )
    registry = SessionRegistry(
        ToolFactory((old, candidate)), tool_result_timeout_seconds=0.1
    )
    first = first_request(tools=(echo_tool(),))
    try:
        lease = await registry.open_turn(first, "stable")
        await collect(lease.stream())
        correct = continuation(
            first,
            (call_boundary("call_one")[0],),
            (ToolResultBlock("call_one", ("saved",), False),),
        )
        rewritten = replace(
            correct,
            messages=(
                CanonicalMessage.user_text("rewritten"),
                *correct.messages[1:],
            ),
        )
        task = asyncio.create_task(registry.open_turn(rewritten, "stable"))
        await asyncio.wait_for(entered.wait(), 2)
        if bridge_failure:
            old.fail_bridge()
        await asyncio.wait_for(old.closed.wait(), 2)
        release.set()
        recovered = await task
        assert await collect(recovered.stream()) == list(final_boundary("recovered"))
        assert old.results == []
        assert old.close_count == 1
    finally:
        release.set()
        await registry.close()


@pytest.mark.anyio
@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_pending_rebase_preserves_original_and_blocks_competing_work(
    cancel,
):
    entered, release = asyncio.Event(), asyncio.Event()

    class FailingStart(ToolSession):
        async def start(self):
            await super().start()
            raise BackendFailure("synthetic startup failure")

    old = ToolSession((call_boundary("call_one"), final_boundary("original")))
    candidate = FailingStart((), start_entered=entered, start_release=release)
    factory = ToolFactory((old, candidate))
    registry = SessionRegistry(factory)
    first = first_request(tools=(echo_tool(),))
    try:
        first_lease = await registry.open_turn(first, "stable")
        await collect(first_lease.stream())
        correct = continuation(
            first,
            (call_boundary("call_one")[0],),
            (ToolResultBlock("call_one", ("saved",), False),),
        )
        rewritten = replace(
            correct,
            messages=(
                CanonicalMessage.user_text("rewritten"),
                *correct.messages[1:],
            ),
        )
        task = asyncio.create_task(registry.open_turn(rewritten, "stable"))
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(SessionConflict):
            await registry.open_turn(correct, "stable")
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            with pytest.raises(BackendFailure):
                await task
        assert old.results == []
        assert not old.closed.is_set()
        good = await registry.open_turn(correct, "stable")
        events = await collect(good.stream())
        assert events == list(final_boundary("original"))
        await asyncio.wait_for(candidate.closed.wait(), 2)
    finally:
        release.set()
        await registry.close()


def completed_result_tail():
    body = tool_body("openai")
    body["messages"] += [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_one",
                    "type": "function",
                    "function": {"name": "echo", "arguments": '{"value":"same"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_one", "content": "saved result"},
    ]
    return body


@pytest.mark.anyio
@pytest.mark.parametrize("explicit", [False, True])
async def test_complete_tool_results_can_import_without_a_live_session(explicit):
    backend = http_session((final_boundary("recovered"),))
    factory = ToolFactory((backend,))
    app = create_app(models=("sonnet",), session_factory=factory)
    headers = {"x-quaylet-session": "recovered"} if explicit else {}
    async with lifespan_app(app):
        result = await post_json(
            app, "/v1/chat/completions", completed_result_tail(), headers=headers
        )
        assert result.status == 200, result.json
        assert result.json["choices"][0]["message"]["content"] == "recovered"
        assert backend.results == []  # No callback or caller-tool execution on import.
        assert len(factory.histories[0]) == 3
        assert factory.histories[0][-2].blocks[0].id == "call_one"
        assert factory.histories[0][-1].blocks[0].tool_call_id == "call_one"


@pytest.mark.anyio
@pytest.mark.parametrize("explicit", [False, True])
async def test_complete_rewritten_result_tail_replaces_suspended_session(explicit):
    old = http_session((call_boundary("call_one"), final_boundary("original")))
    replacement = http_session((final_boundary("recovered"),))
    factory = ToolFactory((old, replacement))
    app = create_app(models=("sonnet",), session_factory=factory)
    headers = {"x-quaylet-session": "recovered"} if explicit else {}
    async with lifespan_app(app):
        first = await post_json(
            app, "/v1/chat/completions", tool_body("openai"), headers=headers
        )
        body = completed_result_tail()
        body["messages"][-2] = first.json["choices"][0]["message"]
        body["messages"][0]["content"] = "rewritten history"
        result = await post_json(app, "/v1/chat/completions", body, headers=headers)
        assert result.status == 200, result.json
        assert result.json["choices"][0]["message"]["content"] == "recovered"
        assert (
            result.headers["x-quaylet-session"]
            == first.headers["x-quaylet-session"]
        )
        await old.closed.wait()
        assert old.results == []


@pytest.mark.anyio
async def test_wrong_pending_ids_do_not_replace_an_identified_session():
    old = http_session((call_boundary("call_one"), final_boundary("original")))
    factory = ToolFactory((old,))
    app = create_app(models=("sonnet",), session_factory=factory)
    headers = {"x-quaylet-session": "stable"}
    async with lifespan_app(app):
        first = await post_json(
            app, "/v1/chat/completions", tool_body("openai"), headers=headers
        )
        body = completed_result_tail()
        body["messages"][-2] = first.json["choices"][0]["message"]
        wrong = copy.deepcopy(body)
        wrong["messages"][-2]["tool_calls"][0]["id"] = "call_other"
        wrong["messages"][-1]["tool_call_id"] = "call_other"
        rejected = await post_json(app, "/v1/chat/completions", wrong, headers=headers)
        assert rejected.status == 409
        good = await post_json(app, "/v1/chat/completions", body, headers=headers)
        assert good.status == 200
        assert len(factory.sessions) == 1


def http_session(boundaries):
    return ToolSession(
        tuple(
            (ResponseIdentity("sonnet-5", "sonnet-5", False), *boundary)
            for boundary in boundaries
        )
    )
