from dataclasses import replace

import pytest

from claude_sdk_proxy.domain import (
    CanonicalMessage,
    RequestValidationError,
    TextBlock,
    TextRequest,
    ThinkingBlock,
    ToolCallBlock,
    ToolDefinition,
    ToolResultBlock,
)
from claude_sdk_proxy.openai_subscription import translation as tr
from claude_sdk_proxy.openai_subscription.replay import (
    ReplayCache,
    decode_reasoning,
    encode_reasoning,
)
from claude_sdk_proxy.thinking import ThinkingOptions


def request(**kwargs):
    return TextRequest(
        model="gpt-6-astra",
        system="",
        messages=(CanonicalMessage.user_text("hi"),),
        max_tokens=99,
        stream=False,
        **kwargs,
    )


def test_body_is_caller_only_and_subscription_streaming():
    body = tr.build_body(
        request(tools=(ToolDefinition("lookup", "mine", {"type": "object"}),)), "acct"
    )
    assert body == {
        "model": "gpt-6-astra",
        "instructions": "",
        "store": False,
        "stream": True,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        "include": ["reasoning.encrypted_content"],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "tools": [
            {
                "type": "function",
                "name": "lookup",
                "description": "mine",
                "parameters": {"type": "object"},
                "strict": None,
            }
        ],
    }


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-5.6-sol"])
@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_explicit_supported_efforts(model, effort):
    req = replace(
        request(), model=model, thinking=ThinkingOptions(mode="adaptive", effort=effort)
    )
    assert tr.build_body(req, "acct")["reasoning"] == {
        "effort": effort,
        "summary": "auto",
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"model": "gpt-5"},
        {"thinking": ThinkingOptions(mode="adaptive", effort="ultra")},
        {"thinking": ThinkingOptions(mode="enabled", budget_tokens=2048)},
        {"refusal_fallback": "auto"},
    ],
)
def test_unsupported_options_rejected(changes):
    with pytest.raises(RequestValidationError):
        tr.build_body(replace(request(), **changes), "acct")


def test_tool_ids_and_results_are_native():
    req = replace(
        request(),
        messages=(
            CanonicalMessage.user_text("hi"),
            CanonicalMessage(
                "assistant", (ToolCallBlock("call_A", "lookup", {"x": 1}),)
            ),
            CanonicalMessage("user", (ToolResultBlock("call_A", ("ok",), False),)),
        ),
    )
    assert tr.build_body(req, "acct")["input"][1:] == [
        {
            "type": "function_call",
            "call_id": "call_A",
            "name": "lookup",
            "arguments": '{"x":1}',
        },
        {"type": "function_call_output", "call_id": "call_A", "output": "ok"},
    ]


def test_reasoning_envelope_is_scoped_and_preserves_ciphertext():
    item = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "brief"}],
        "encrypted_content": "opaque-not-decoded",
    }
    signature = encode_reasoning(item, "acct", "gpt-6-astra", scope="0" * 64)
    assert signature.startswith("openai-subscription:v1:")
    assert decode_reasoning(signature, "acct", "gpt-6-astra", scope="0" * 64) == item
    assert (
        decode_reasoning(signature, "different", "gpt-6-astra", scope="0" * 64) is None
    )
    assert decode_reasoning(signature, "acct", "gpt-5.6-sol", scope="0" * 64) is None
    assert (
        decode_reasoning("claude-signature", "acct", "gpt-6-astra", scope="0" * 64)
        is None
    )
    with pytest.raises(RequestValidationError):
        decode_reasoning(
            "openai-subscription:v1:not-base64", "acct", "gpt-6-astra", scope="0" * 64
        )


def test_foreign_reasoning_degrades_and_matching_reasoning_replays():
    item = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [],
        "encrypted_content": "secret",
    }
    req = replace(
        request(),
        messages=(
            CanonicalMessage.user_text("hi"),
            CanonicalMessage(
                "assistant",
                (
                    ThinkingBlock("brief", "unbound-placeholder"),
                    TextBlock("answer"),
                ),
            ),
            CanonicalMessage.user_text("next"),
        ),
    )
    from claude_sdk_proxy.openai_subscription.replay import envelope_scope

    signature = encode_reasoning(
        item,
        "acct",
        "gpt-6-astra",
        scope=envelope_scope(req, "acct", req.messages[:-1]),
    )
    req = replace(
        req,
        messages=(
            req.messages[0],
            CanonicalMessage(
                "assistant", (ThinkingBlock("brief", signature), TextBlock("answer"))
            ),
            req.messages[-1],
        ),
    )
    assert tr.build_body(req, "acct")["input"][1] == item
    assert all(
        i.get("type") != "reasoning" for i in tr.build_body(req, "other")["input"]
    )


def test_replay_requires_exact_context_and_transcript_and_marks_ambiguity():
    cache = ReplayCache(max_entries=2, max_bytes=4096)
    req = request(dialect="openai")
    history = req.messages + (CanonicalMessage.assistant_text("answer"),)
    native = [
        {"role": "assistant", "id": "msg_1", "phase": "final_answer", "content": []}
    ]
    cache.put(req, "acct", history, native)
    next_req = replace(req, messages=history + (CanonicalMessage.user_text("next"),))
    assert cache.get(next_req, "acct") == native
    assert cache.get(next_req, "other") is None
    assert cache.get(replace(next_req, system="different"), "acct") is None
    assert (
        cache.get(
            replace(next_req, messages=(CanonicalMessage.user_text("summary"),)), "acct"
        )
        is None
    )
    cache.put(req, "acct", history, [{"id": "different"}])
    assert cache.get(next_req, "acct") is None
    cache.put(req, "acct", history, native)
    assert cache.get(next_req, "acct") is None


def test_replay_entry_and_aggregate_eviction_are_bounded():
    cache = ReplayCache(max_entries=1, max_bytes=512)
    req = request()
    first = req.messages + (CanonicalMessage.assistant_text("one"),)
    second = req.messages + (CanonicalMessage.assistant_text("two"),)
    cache.put(req, "acct", first, [{"id": "1"}])
    cache.put(req, "acct", second, [{"id": "2"}])
    assert (
        cache.get(
            replace(req, messages=first + (CanonicalMessage.user_text("x"),)), "acct"
        )
        is None
    )
    assert cache.get(
        replace(req, messages=second + (CanonicalMessage.user_text("x"),)), "acct"
    ) == [{"id": "2"}]
    cache.put(req, "acct", first, [{"data": "x" * 1024}])
    assert cache.bytes_used <= 512
    assert (
        cache.get(
            replace(req, messages=first + (CanonicalMessage.user_text("x"),)), "acct"
        )
        is None
    )
