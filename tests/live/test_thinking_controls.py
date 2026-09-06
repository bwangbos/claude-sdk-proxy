from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, SystemMessage

from claude_sdk_proxy.app import create_app
from claude_sdk_proxy.sdk_session import SdkSession
from tests.integration.pi_gateway_support import run_pi_thinking, serve

pytestmark = [pytest.mark.live, pytest.mark.anyio]


class RecordingSdkClient:
    def __init__(
        self,
        client: ClaudeSDKClient,
        resolved_models: list[str],
        notices: list[dict[str, object]],
    ) -> None:
        self._client = client
        self._resolved_models = resolved_models
        self._notices = notices

    async def connect(self) -> None:
        await self._client.connect()

    async def query(self, prompt: Any) -> None:
        await self._client.query(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        async for message in self._client.receive_response():
            if isinstance(message, SystemMessage) and message.subtype == "init":
                model = message.data.get("model")
                assert isinstance(model, str) and model
                self._resolved_models.append(model)
            elif isinstance(message, SystemMessage) and message.subtype not in {
                "status",
                "thinking_tokens",
            }:
                self._notices.append(
                    {
                        "subtype": message.subtype,
                        "category": message.data.get("api_refusal_category"),
                        "explanation": message.data.get("api_refusal_explanation"),
                    }
                )
            yield message

    async def disconnect(self) -> None:
        await self._client.disconnect()


class LiveSdkFactory:
    def __init__(self) -> None:
        self.options: list[ClaudeAgentOptions] = []
        self.resolved_models: list[str] = []
        self.notices: list[dict[str, object]] = []

    def __call__(self, model: str, system: str, **kwargs: Any) -> SdkSession:
        def client_factory(options: ClaudeAgentOptions) -> RecordingSdkClient:
            self.options.append(options)
            return RecordingSdkClient(
                ClaudeSDKClient(options), self.resolved_models, self.notices
            )

        return SdkSession(model, system, client_factory=client_factory, **kwargs)


def _require_live() -> None:
    if os.environ.get("CLAUDE_PROXY_LIVE") != "1":
        pytest.fail("live thinking tests require CLAUDE_PROXY_LIVE=1")


ROWS = [
    pytest.param("sonnet", "none", "openai", id="sonnet-none-openai"),
    pytest.param("sonnet", "low", "anthropic", id="sonnet-low-anthropic"),
    pytest.param("sonnet", "medium", "openai", id="sonnet-medium-openai"),
    pytest.param("sonnet", "high", "anthropic", id="sonnet-high-anthropic"),
    pytest.param("sonnet", "xhigh", "openai", id="sonnet-xhigh-openai"),
    pytest.param("sonnet", "max", "anthropic", id="sonnet-max-anthropic"),
    pytest.param("opus", "none", "anthropic", id="opus-none-anthropic"),
    pytest.param("opus", "low", "openai", id="opus-low-openai"),
    pytest.param("opus", "medium", "anthropic", id="opus-medium-anthropic"),
    pytest.param("opus", "high", "openai", id="opus-high-openai"),
    pytest.param("opus", "xhigh", "anthropic", id="opus-xhigh-anthropic"),
    pytest.param("opus", "max", "openai", id="opus-max-openai"),
]


@pytest.mark.parametrize(("model", "level", "dialect"), ROWS)
async def test_live_pi_advertised_model_level_matrix(
    model: str, level: str, dialect: str
) -> None:
    """Every advertised tuple must reach the real SDK without downgrading."""
    _require_live()
    factory = LiveSdkFactory()
    app = create_app(models=(model,), session_factory=factory)
    pi_level = "off" if level == "none" else level

    async with serve(app) as base_url:
        result = await run_pi_thinking(
            base_url,
            dialect,
            scenario="probe",
            first_model=model,
            first_reasoning=pi_level,
            second_model=model,
            timeout_seconds=90,
        )

    assert result["customSessionHeaders"] == []
    assert len(factory.options) == 1
    assert factory.resolved_models == [
        "claude-sonnet-5" if model == "sonnet" else "claude-opus-5"
    ]
    options = factory.options[0]
    assert options.model == model
    expected_thinking: dict[str, str] = (
        {"type": "disabled"} if level == "none" else {"type": "adaptive"}
    )
    if dialect == "anthropic" and level != "none":
        expected_thinking["display"] = "summarized"
    assert options.thinking == expected_thinking
    assert options.effort == (None if level == "none" else level)

    payload = result["requestPayloads"][0]
    if dialect == "openai":
        assert payload["reasoningEffort"] == level
    elif level == "none":
        assert payload["thinking"] == {"type": "disabled"}
        assert "effort" not in payload
    else:
        assert payload["thinking"] == {
            "type": "adaptive",
            "display": "summarized",
        }
        assert payload["effort"] == level

    turn = result["turns"][0]
    assert turn["model"] == model
    assert turn["finishReason"] == "stop"
    text = "".join(
        block["text"] for block in turn["content"] if block["type"] == "text"
    )
    assert text == "PI_PROBE_OK"
    if any(block["type"] == "thinking" for block in turn["content"]):
        assert any(event["type"] == "thinking_delta" for event in turn["events"])


async def test_live_pi_anthropic_tool_replay_and_bidirectional_switch() -> None:
    """Exercise real Pi replay and the persistent cross-model wire projection."""
    _require_live()
    factory = LiveSdkFactory()
    app = create_app(
        models=("sonnet", "opus"),
        session_factory=factory,
        turn_timeout_seconds=120,
        tool_result_timeout_seconds=120,
    )

    async with serve(app) as base_url:
        result = await run_pi_thinking(
            base_url,
            "anthropic",
            scenario="flow",
            first_model="sonnet",
            first_reasoning="high",
            second_model="opus",
            second_reasoning="low",
            timeout_seconds=240,
        )

    assert result["customSessionHeaders"] == []
    assert [options.model for options in factory.options] == [
        "sonnet",
        "opus",
        "sonnet",
    ]
    assert factory.resolved_models == [
        "claude-sonnet-5",
        "claude-opus-5",
        "claude-sonnet-5",
    ]
    assert [options.effort for options in factory.options] == [
        "high",
        "low",
        "high",
    ]
    assert [turn["finishReason"] for turn in result["turns"]] == [
        "toolUse",
        "stop",
        "stop",
        "stop",
        "stop",
    ]
    calls = [
        block for block in result["turns"][0]["content"]
        if block["type"] == "toolCall"
    ]
    assert [(call["name"], call["arguments"]) for call in calls] == [
        ("echo", {"value": "first"}),
        ("echo", {"value": "second"}),
    ]
    assert result["toolResults"] == [
        {"name": "echo", "content": "first"},
        {"name": "echo", "content": "second"},
    ]
    texts = [
        "".join(
            block["text"]
            for block in turn["content"]
            if block["type"] == "text"
        )
        for turn in result["turns"]
    ]
    assert texts[1:] == [
        "4",
        "7",
        "11",
        "15",
    ]
    assert factory.notices == []


async def test_live_pi_anthropic_bidirectional_switch_roundtrip() -> None:
    """Isolate both settings switches from the unchanged Opus refusal probe."""
    _require_live()
    factory = LiveSdkFactory()
    app = create_app(
        models=("sonnet", "opus"),
        session_factory=factory,
        turn_timeout_seconds=120,
        tool_result_timeout_seconds=120,
    )

    async with serve(app) as base_url:
        result = await run_pi_thinking(
            base_url,
            "anthropic",
            scenario="roundtrip",
            first_model="sonnet",
            first_reasoning="high",
            second_model="opus",
            second_reasoning="low",
            timeout_seconds=240,
        )

    assert result["customSessionHeaders"] == []
    assert [options.model for options in factory.options] == [
        "sonnet",
        "opus",
        "sonnet",
    ]
    assert factory.resolved_models == [
        "claude-sonnet-5",
        "claude-opus-5",
        "claude-sonnet-5",
    ]
    assert [options.effort for options in factory.options] == [
        "high",
        "low",
        "high",
    ]
    assert [turn["finishReason"] for turn in result["turns"]] == [
        "toolUse",
        "stop",
        "stop",
        "stop",
    ]
    assert result["toolResults"] == [
        {"name": "echo", "content": "first"},
        {"name": "echo", "content": "second"},
    ]
    texts = [
        "".join(
            block["text"]
            for block in turn["content"]
            if block["type"] == "text"
        )
        for turn in result["turns"]
    ]
    assert texts[1:] == ["4", "7", "15"]
    assert factory.notices == []
