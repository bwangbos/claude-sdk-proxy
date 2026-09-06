from dataclasses import replace
from pathlib import Path

import pytest

from claude_sdk_proxy.anthropic_api import parse_anthropic_request
from claude_sdk_proxy.domain import (
    CanonicalMessage,
    RequestValidationError,
    TextRequest,
)
from claude_sdk_proxy.openai_api import parse_openai_request
from claude_sdk_proxy.sdk_session import SdkSession
from claude_sdk_proxy.session_identity import request_fingerprint
from claude_sdk_proxy.sessions import SessionConflict, SessionRegistry
from claude_sdk_proxy.thinking import ThinkingOptions
from tests.gateway.fakes import (
    FakeSdkClient,
    FakeSessionFactory,
    FixedTemporaryDirectory,
)


def test_openai_high_reaches_normalized_request() -> None:
    request = parse_openai_request(
        {
            "model": "claude-sonnet-4-6",
            "messages": [{"role": "user", "content": "hello"}],
            "reasoning_effort": "high",
        },
        frozenset({"claude-sonnet-4-6"}),
    )

    assert request.thinking.mode == "adaptive"
    assert request.thinking.effort == "high"


@pytest.mark.parametrize("value", [pytest.param(None, id="null")])
def test_openai_null_reasoning_control_is_omitted(value: object) -> None:
    request = parse_openai_request(
        {
            "model": "custom-model",
            "messages": [{"role": "user", "content": "hello"}],
            "reasoning_effort": value,
        },
        frozenset({"custom-model"}),
    )

    assert request.thinking.mode == "disabled"
    assert request.thinking.effort is None


def test_openai_omitted_reasoning_control_disables_thinking() -> None:
    request = parse_openai_request(
        {
            "model": "custom-model",
            "messages": [{"role": "user", "content": "hello"}],
        },
        frozenset({"custom-model"}),
    )

    assert request.thinking.mode == "disabled"
    assert request.thinking.effort is None


def test_openai_none_disables_thinking() -> None:
    request = parse_openai_request(
        {
            "model": "custom-model",
            "messages": [{"role": "user", "content": "hello"}],
            "reasoning_effort": "none",
        },
        frozenset({"custom-model"}),
    )

    assert request.thinking.mode == "disabled"


@pytest.mark.parametrize("value", ["minimal", "extreme", 3, True])
def test_openai_invalid_reasoning_effort_is_rejected(value: object) -> None:
    with pytest.raises(RequestValidationError) as raised:
        parse_openai_request(
            {
                "model": "claude-sonnet-4-6",
                "messages": [{"role": "user", "content": "hello"}],
                "reasoning_effort": value,
            },
            frozenset({"claude-sonnet-4-6"}),
        )

    assert raised.value.field == "reasoning_effort"


@pytest.mark.parametrize(
    ("model", "effort"),
    [
        ("claude-sonnet-4-6", "xhigh"),
        ("claude-haiku-4-5-20251001", "high"),
        ("custom-model", "low"),
    ],
)
def test_openai_unsupported_model_effort_pair_is_rejected(
    model: str, effort: str
) -> None:
    with pytest.raises(RequestValidationError) as raised:
        parse_openai_request(
            {
                "model": model,
                "messages": [{"role": "user", "content": "hello"}],
                "reasoning_effort": effort,
            },
            frozenset({model}),
        )

    assert raised.value.field == "reasoning_effort"
    assert "not supported" in raised.value.reason


@pytest.mark.parametrize("model", ["sonnet", "opus"])
def test_current_sdk_aliases_accept_observed_effort_levels(model: str) -> None:
    request = parse_openai_request(
        {
            "model": model,
            "messages": [{"role": "user", "content": "hello"}],
            "reasoning_effort": "xhigh",
        },
        frozenset({model}),
    )

    assert request.thinking.mode == "adaptive"
    assert request.thinking.effort == "xhigh"


def _anthropic_body(model: str = "claude-sonnet-4-6") -> dict[str, object]:
    return {
        "model": model,
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": "hello"}],
    }


@pytest.mark.parametrize("field", ["thinking", "output_config"])
def test_anthropic_null_optional_control_is_omitted(field: str) -> None:
    body = _anthropic_body("custom-model")
    body[field] = None

    request = parse_anthropic_request(body, frozenset({"custom-model"}))

    assert request.thinking.mode == "disabled"
    assert request.thinking.effort is None


def test_anthropic_adaptive_effort_and_display_are_normalized() -> None:
    body = _anthropic_body()
    body["thinking"] = {"type": "adaptive", "display": "summarized"}
    body["output_config"] = {"effort": "high"}

    request = parse_anthropic_request(body, frozenset({"claude-sonnet-4-6"}))

    assert request.thinking.mode == "adaptive"
    assert request.thinking.effort == "high"
    assert request.thinking.display == "summarized"
    assert request.thinking.budget_tokens is None


def test_anthropic_legacy_enabled_budget_is_normalized() -> None:
    body = _anthropic_body("claude-sonnet-4-5-20250929")
    body["thinking"] = {
        "type": "enabled",
        "budget_tokens": 4096,
        "display": "omitted",
    }

    request = parse_anthropic_request(
        body, frozenset({"claude-sonnet-4-5-20250929"})
    )

    assert request.thinking.mode == "enabled"
    assert request.thinking.budget_tokens == 4096
    assert request.thinking.display == "omitted"


@pytest.mark.parametrize("budget", [True, False, 0, -1, 1.5, "4096"])
def test_anthropic_invalid_enabled_budget_is_rejected(budget: object) -> None:
    body = _anthropic_body("claude-sonnet-4-5-20250929")
    body["thinking"] = {"type": "enabled", "budget_tokens": budget}

    with pytest.raises(RequestValidationError) as raised:
        parse_anthropic_request(
            body, frozenset({"claude-sonnet-4-5-20250929"})
        )

    assert raised.value.field == "thinking"
    assert "budget_tokens" in raised.value.reason


def test_anthropic_thinking_budget_must_be_below_max_tokens() -> None:
    body = _anthropic_body("claude-sonnet-4-5-20250929")
    body["max_tokens"] = 4096
    body["thinking"] = {"type": "enabled", "budget_tokens": 4096}

    with pytest.raises(RequestValidationError) as raised:
        parse_anthropic_request(
            body, frozenset({"claude-sonnet-4-5-20250929"})
        )

    assert raised.value.field == "thinking"
    assert "less than max_tokens" in raised.value.reason


def test_anthropic_disabled_plus_effort_is_rejected() -> None:
    body = _anthropic_body()
    body["thinking"] = {"type": "disabled"}
    body["output_config"] = {"effort": "high"}

    with pytest.raises(RequestValidationError) as raised:
        parse_anthropic_request(body, frozenset({"claude-sonnet-4-6"}))

    assert raised.value.field == "output_config"
    assert "disabled" in raised.value.reason


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("thinking", {"type": "adaptive", "unexpected": True}),
        ("output_config", {"effort": "high", "unexpected": True}),
    ],
)
def test_anthropic_unknown_nested_control_field_is_rejected(
    field: str, value: object
) -> None:
    body = _anthropic_body()
    body[field] = value

    with pytest.raises(RequestValidationError) as raised:
        parse_anthropic_request(body, frozenset({"claude-sonnet-4-6"}))

    assert raised.value.field == field
    assert "unknown" in raised.value.reason


@pytest.mark.parametrize("field", ["thinking", "output_config"])
@pytest.mark.parametrize("value", ["adaptive", [], 1, True])
def test_anthropic_non_object_nested_control_is_rejected(
    field: str, value: object
) -> None:
    body = _anthropic_body()
    body[field] = value

    with pytest.raises(RequestValidationError) as raised:
        parse_anthropic_request(body, frozenset({"claude-sonnet-4-6"}))

    assert raised.value.field == field


@pytest.mark.parametrize("display", ["full", "", 1, True])
def test_anthropic_invalid_thinking_display_is_rejected(display: object) -> None:
    body = _anthropic_body()
    body["thinking"] = {"type": "adaptive", "display": display}

    with pytest.raises(RequestValidationError) as raised:
        parse_anthropic_request(body, frozenset({"claude-sonnet-4-6"}))

    assert raised.value.field == "thinking"
    assert "display" in raised.value.reason


def test_openai_and_anthropic_effort_normalize_equivalently() -> None:
    openai = parse_openai_request(
        {
            "model": "claude-sonnet-4-6",
            "messages": [{"role": "user", "content": "hello"}],
            "reasoning_effort": "high",
        },
        frozenset({"claude-sonnet-4-6"}),
    )
    body = _anthropic_body()
    body["thinking"] = {"type": "adaptive"}
    body["output_config"] = {"effort": "high"}
    anthropic = parse_anthropic_request(
        body, frozenset({"claude-sonnet-4-6"})
    )

    assert openai.thinking == anthropic.thinking


@pytest.mark.anyio
async def test_sdk_session_receives_adaptive_thinking_options(tmp_path: Path) -> None:
    client = FakeSdkClient(responses=())
    session = SdkSession(
        model="claude-sonnet-4-6",
        system="system",
        directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
        client_factory=lambda options: client.capture_options(options),
        thinking=ThinkingOptions(
            mode="adaptive", effort="high", display="summarized"
        ),
    )

    await session.start()
    await session.close()

    assert client.options is not None
    assert client.options.thinking == {
        "type": "adaptive",
        "display": "summarized",
    }
    assert client.options.effort == "high"


@pytest.mark.anyio
async def test_sdk_session_receives_enabled_thinking_budget(tmp_path: Path) -> None:
    client = FakeSdkClient(responses=())
    session = SdkSession(
        model="claude-sonnet-4-5-20250929",
        system="system",
        directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
        client_factory=lambda options: client.capture_options(options),
        thinking=ThinkingOptions(
            mode="enabled", budget_tokens=4096, display="omitted"
        ),
    )

    await session.start()
    await session.close()

    assert client.options is not None
    assert client.options.thinking == {
        "type": "enabled",
        "budget_tokens": 4096,
        "display": "omitted",
    }
    assert client.options.effort is None


@pytest.mark.anyio
async def test_sdk_session_default_explicitly_disables_thinking(tmp_path: Path) -> None:
    client = FakeSdkClient(responses=())
    session = SdkSession(
        model="custom-model",
        system="system",
        directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
        client_factory=lambda options: client.capture_options(options),
    )

    await session.start()
    await session.close()

    assert client.options is not None
    assert client.options.thinking == {"type": "disabled"}
    assert client.options.effort is None


def test_request_fingerprint_distinguishes_thinking_effort() -> None:
    base = TextRequest(
        model="claude-sonnet-4-6",
        system="",
        messages=(CanonicalMessage.user_text("hello"),),
        max_tokens=1024,
        stream=False,
        thinking=ThinkingOptions(mode="adaptive", effort="low"),
    )

    assert request_fingerprint(base) != request_fingerprint(
        replace(base, thinking=ThinkingOptions(mode="adaptive", effort="high"))
    )


@pytest.mark.anyio
async def test_registry_passes_normalized_thinking_to_session_factory() -> None:
    factory = FakeSessionFactory(outputs=("answer",))
    registry = SessionRegistry(factory)
    thinking = ThinkingOptions(mode="adaptive", effort="high")
    request = TextRequest(
        model="claude-sonnet-4-6",
        system="",
        messages=(CanonicalMessage.user_text("hello"),),
        max_tokens=1024,
        stream=False,
        thinking=thinking,
    )

    await registry.open_turn(request, explicit_id="thinking-session")

    assert factory.thinking_options == [thinking]
    await registry.close()


@pytest.mark.anyio
async def test_explicit_session_rejects_thinking_change_during_active_turn() -> None:
    factory = FakeSessionFactory(outputs=("answer",))
    registry = SessionRegistry(factory)
    request = TextRequest(
        model="claude-sonnet-4-6",
        system="",
        messages=(CanonicalMessage.user_text("hello"),),
        max_tokens=1024,
        stream=False,
        thinking=ThinkingOptions(mode="adaptive", effort="low"),
    )
    await registry.open_turn(request, explicit_id="thinking-session")

    with pytest.raises(SessionConflict, match="busy"):
        await registry.open_turn(
            replace(
                request,
                thinking=ThinkingOptions(mode="adaptive", effort="high"),
            ),
            explicit_id="thinking-session",
        )

    await registry.close()
