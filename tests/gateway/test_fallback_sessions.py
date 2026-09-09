from dataclasses import replace

import pytest

from quaylet.domain import (
    CanonicalMessage,
    Completed,
    ResponseIdentity,
    TextDelta,
    TextRequest,
    ToolCall,
    ToolResultBlock,
)
from quaylet.session_identity import request_fingerprint
from quaylet.sessions import SessionMismatch, SessionRegistry
from quaylet.thinking import ThinkingOptions
from tests.gateway.test_tool_sessions import (
    ToolSession,
    collect,
    continuation,
    echo_tool,
)


def initial(*, tools=False):
    return TextRequest(
        "opus-5",
        "",
        (CanonicalMessage.user_text("go"),),
        128,
        False,
        tools=(echo_tool(),) if tools else (),
        refusal_fallback="auto",
    )


class Factory:
    def __init__(self, *, tools=False, downgraded=True):
        self.options = []
        self.sessions = []
        self.tools = tools
        self.downgraded = downgraded

    def __call__(self, model, system, **kwargs):
        self.options.append(kwargs)
        switched = self.downgraded and len(self.options) == 1
        actual = "opus-4.8" if switched or kwargs["fallback_provenance"] else model
        identity = ResponseIdentity(
            model, actual, switched or kwargs["fallback_provenance"]
        )
        if self.tools and len(self.options) == 1:
            boundaries = (
                (
                    identity,
                    ToolCall("call-1", "echo", {"value": "v"}),
                    Completed("tool_use", None),
                ),
                (identity, TextDelta("done"), Completed("end_turn", None)),
            )
        else:
            boundaries = ((identity, TextDelta("done"), Completed("end_turn", None)),)
        session = ToolSession(boundaries)
        self.sessions.append(session)
        return session


def registry(factory, **kwargs):
    return SessionRegistry(factory, configured_models=("opus-5", "opus-4.8"), **kwargs)


def test_policy_is_part_of_retry_identity():
    request = initial()
    assert request_fingerprint(request) != request_fingerprint(
        replace(request, refusal_fallback="off")
    )


@pytest.mark.anyio
@pytest.mark.parametrize("downgraded", [False, True])
async def test_known_rebase_preserves_only_validated_active_model(downgraded):
    factory = Factory(downgraded=downgraded)
    reg = registry(factory)
    request = initial()
    try:
        events = await collect((await reg.open_turn(request, "known")).stream())
        assert events[0].fallback is downgraded
        changed = replace(
            request,
            messages=(
                CanonicalMessage.user_text("rewritten"),
                CanonicalMessage.assistant_text("done"),
                CanonicalMessage.user_text("next"),
            ),
        )
        await collect((await reg.open_turn(changed, "known")).stream())
        assert factory.options[1]["active_backend_model"] == (
            "claude-opus-4-8" if downgraded else "claude-opus-5"
        )
        assert factory.options[1]["fallback_provenance"] is downgraded
    finally:
        await reg.close()


@pytest.mark.anyio
@pytest.mark.parametrize("explicit", [None, "known"])
async def test_thinking_only_replacement_preserves_validated_downgrade(explicit):
    factory = Factory()
    reg = registry(factory)
    request = initial()
    try:
        await collect((await reg.open_turn(request, explicit)).stream())
        changed = replace(
            request,
            thinking=ThinkingOptions(mode="adaptive", effort="low"),
            messages=(
                *request.messages,
                CanonicalMessage.assistant_text("done"),
                CanonicalMessage.user_text("next"),
            ),
        )
        events = await collect((await reg.open_turn(changed, explicit)).stream())
        assert factory.options[1]["active_backend_model"] == "claude-opus-4-8"
        assert factory.options[1]["fallback_provenance"] is True
        assert factory.options[1]["refusal_fallback"] == "auto"
        assert factory.options[1]["thinking"] == ThinkingOptions(
            mode="adaptive", effort="low"
        )
        assert events[0] == ResponseIdentity("opus-5", "opus-4.8", True)
        assert (
            await collect((await reg.open_turn(changed, explicit)).stream()) == events
        )
        assert len(factory.sessions) == 2
    finally:
        await reg.close()


@pytest.mark.anyio
async def test_fresh_import_and_evicted_retry_never_infer_fallback_provenance():
    factory = Factory()
    reg = registry(factory, max_sessions=1)
    request = initial()
    try:
        await collect((await reg.open_turn(request, "old")).stream())
        imported = replace(
            request,
            messages=(
                *request.messages,
                CanonicalMessage.assistant_text("done"),
                CanonicalMessage.user_text("next"),
            ),
        )
        await collect((await reg.open_turn(imported, "fresh")).stream())
        await collect((await reg.open_turn(request, "old")).stream())
        for options in factory.options[1:]:
            assert options["active_backend_model"] == "claude-opus-5"
            assert options["fallback_provenance"] is False
    finally:
        await reg.close()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["policy", "model"])
async def test_completed_settings_change_resets_provenance(change):
    factory = Factory()
    reg = registry(factory)
    request = initial()
    try:
        await collect((await reg.open_turn(request, "known")).stream())
        next_request = replace(
            request,
            messages=(
                *request.messages,
                CanonicalMessage.assistant_text("done"),
                CanonicalMessage.user_text("next"),
            ),
        )
        next_request = replace(
            next_request,
            **(
                {"refusal_fallback": "off"}
                if change == "policy"
                else {"model": "opus-4.8"}
            ),
        )
        events = await collect((await reg.open_turn(next_request, "known")).stream())
        assert factory.options[1]["fallback_provenance"] is False
        assert factory.options[1]["active_backend_model"] == (
            "claude-opus-5" if change == "policy" else "claude-opus-4-8"
        )
        assert events[0].fallback is False
    finally:
        await reg.close()


@pytest.mark.anyio
@pytest.mark.parametrize("explicit", [None, "known"])
async def test_pending_rebase_retains_downgrade_and_complete_call_results(explicit):
    factory = Factory(tools=True)
    reg = registry(factory)
    request = initial(tools=True)
    try:
        events = await collect((await reg.open_turn(request, explicit)).stream())
        calls = tuple(e for e in events if isinstance(e, ToolCall))
        next_request = replace(
            continuation(request, calls, (ToolResultBlock("call-1", ("ok",), False),)),
            refusal_fallback="auto",
        )
        changed = replace(
            next_request,
            messages=(
                CanonicalMessage.user_text("rewritten"),
                *next_request.messages[1:],
            ),
        )
        await collect((await reg.open_turn(changed, explicit)).stream())
        assert factory.options[1]["active_backend_model"] == "claude-opus-4-8"
        assert factory.options[1]["fallback_provenance"] is True
        assert factory.options[1]["history"] == changed.messages
        assert factory.sessions[0].results == []
    finally:
        await reg.close()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["policy", "model"])
@pytest.mark.parametrize("explicit", [None, "known"])
async def test_mid_tool_policy_change_rejected_without_harming_continuation(
    change, explicit
):
    factory = Factory(tools=True)
    reg = registry(factory)
    request = initial(tools=True)
    try:
        events = await collect((await reg.open_turn(request, explicit)).stream())
        retry = await collect((await reg.open_turn(request, explicit)).stream())
        assert retry == events
        calls = tuple(e for e in events if isinstance(e, ToolCall))
        next_request = continuation(
            request, calls, (ToolResultBlock("call-1", ("ok",), False),)
        )
        changed = (
            next_request
            if change == "policy"
            else replace(next_request, refusal_fallback="auto", model="opus-4.8")
        )
        with pytest.raises(SessionMismatch, match="pending tools"):
            await reg.open_turn(changed, explicit)
        next_request = replace(next_request, refusal_fallback="auto")
        final = await collect((await reg.open_turn(next_request, explicit)).stream())
        assert final[0] == ResponseIdentity("opus-5", "opus-4.8", True)
        assert (
            await collect((await reg.open_turn(next_request, explicit)).stream())
            == final
        )
        if explicit:
            with pytest.raises(SessionMismatch, match="stale"):
                await reg.open_turn(request, explicit)
        assert len(factory.sessions) == 1
    finally:
        await reg.close()
