from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ToolResultBlock as SdkToolResultBlock
from claude_agent_sdk import UserMessage

from claude_sdk_proxy.app import create_app
from claude_sdk_proxy.domain import ThinkingBlock
from claude_sdk_proxy.sdk_session import SdkSession
from claude_sdk_proxy.thinking import ThinkingOptions
from tests.gateway.fakes import FakeSdkClient, FixedTemporaryDirectory, sdk_response
from tests.gateway.test_thinking_streams import (
    interleaved_tool_response,
    thinking_response,
)
from tests.integration.pi_gateway_support import run_pi_thinking, serve


class PiThinkingSessionFactory:
    def __init__(self, tmp_path: Path) -> None:
        first_response = (
            *interleaved_tool_response(),
            UserMessage(
                [
                    SdkToolResultBlock("sdk-1", "first", False),
                    SdkToolResultBlock("sdk-3", "second", False),
                ]
            ),
            *thinking_response(),
        )
        self.clients = [
            FakeSdkClient((first_response,)),
            FakeSdkClient(
                (
                    thinking_response(),
                    sdk_response("PI_STILL_OPUS_DONE", "sdk-thinking"),
                )
            ),
            FakeSdkClient((sdk_response("PI_SWITCH_BACK_DONE", "sdk-back"),)),
        ]
        self._clients: Iterator[FakeSdkClient] = iter(self.clients)
        self.tmp_path = tmp_path
        self.models: list[str] = []
        self.histories: list[tuple[Any, ...]] = []
        self.thinking: list[ThinkingOptions] = []

    def __call__(
        self,
        model: str,
        system: str,
        *,
        history: tuple[Any, ...] = (),
        thinking: ThinkingOptions = ThinkingOptions(),
        **kwargs: Any,
    ) -> SdkSession:
        client = next(self._clients)
        directory = self.tmp_path / f"session-{len(self.models)}"
        directory.mkdir()
        self.models.append(model)
        self.histories.append(history)
        self.thinking.append(thinking)
        return SdkSession(
            model,
            system,
            history=history,
            thinking=thinking,
            directory_factory=lambda: FixedTemporaryDirectory(directory),
            client_factory=client.capture_options,
            **kwargs,
        )


@pytest.mark.anyio
@pytest.mark.parametrize("dialect", ["openai", "anthropic"])
async def test_real_pi_preserves_thinking_through_tool_replay_and_switches_settings(
    tmp_path: Path, dialect: str
) -> None:
    """Catch Pi payload drift or a header-dependent replay/switch implementation."""
    factory = PiThinkingSessionFactory(tmp_path)
    app = create_app(models=("sonnet", "opus"), session_factory=factory)

    async with serve(app) as base_url:
        result = await run_pi_thinking(base_url, dialect)

    assert result["customSessionHeaders"] == []
    assert result["models"] == [
        {
            "id": "sonnet",
            "api": f"{dialect}-messages"
            if dialect == "anthropic"
            else "openai-completions",
            "reasoning": True,
            "input": ["text", "image"],
            "thinkingLevelMap": {
                "off": "none",
                "minimal": None,
                "low": "low",
                "medium": "medium",
                "high": "high",
                "xhigh": "xhigh",
                "max": "max",
            },
        },
        {
            "id": "opus",
            "api": f"{dialect}-messages"
            if dialect == "anthropic"
            else "openai-completions",
            "reasoning": True,
            "input": ["text", "image"],
            "thinkingLevelMap": {
                "off": "none",
                "minimal": None,
                "low": "low",
                "medium": "medium",
                "high": "high",
                "xhigh": "xhigh",
                "max": "max",
            },
        },
    ]
    assert [turn["finishReason"] for turn in result["turns"]] == [
        "toolUse",
        "stop",
        "stop",
        "stop",
        "stop",
    ]
    assert [block["type"] for block in result["turns"][1]["content"]] == [
        "thinking",
        "text",
    ]
    assert result["turns"][1]["content"][0]["thinking"] == "reasoning summary"
    assert result["turns"][1]["content"][1]["text"] == "answer"
    assert [block["type"] for block in result["turns"][2]["content"]] == [
        "thinking",
        "text",
    ]
    assert result["turns"][3]["content"] == [
        {"type": "text", "text": "PI_STILL_OPUS_DONE"}
    ]
    assert result["turns"][4]["content"] == [
        {"type": "text", "text": "PI_SWITCH_BACK_DONE"}
    ]
    assert result["toolResults"] == [
        {"name": "echo", "content": "first"},
        {"name": "echo", "content": "second"},
    ]

    assert factory.models == ["sonnet", "opus", "sonnet"]
    assert factory.thinking == [
        ThinkingOptions(mode="adaptive", effort="high", display="summarized")
        if dialect == "anthropic"
        else ThinkingOptions(mode="adaptive", effort="high"),
        ThinkingOptions(mode="adaptive", effort="low", display="summarized")
        if dialect == "anthropic"
        else ThinkingOptions(mode="adaptive", effort="low"),
        ThinkingOptions(mode="adaptive", effort="high", display="summarized")
        if dialect == "anthropic"
        else ThinkingOptions(mode="adaptive", effort="high"),
    ]
    assert any(
        isinstance(block, ThinkingBlock) and block.signature == "opaque-signature"
        for message in factory.histories[1]
        for block in message.blocks
    ), factory.histories
    assert [client.options.model for client in factory.clients] == [
        "sonnet",
        "opus",
        "sonnet",
    ]
    assert [client.options.effort for client in factory.clients] == [
        "high",
        "low",
        "high",
    ]
    assert [client.disconnect_count for client in factory.clients] == [1, 1, 1]

    payloads = result["requestPayloads"]
    assert [payload["model"] for payload in payloads] == [
        "sonnet",
        "sonnet",
        "opus",
        "opus",
        "sonnet",
    ]
    if dialect == "openai":
        assert [payload["reasoningEffort"] for payload in payloads] == [
            "high",
            "high",
            "low",
            "low",
            "high",
        ]
        replayed = payloads[1]["messages"][1]
        assert replayed["reasoning_content"] == "reason-0reason-2"
        assert "sig-0" not in str(replayed)
    else:
        assert [payload["thinking"] for payload in payloads] == [
            {"type": "adaptive", "display": "summarized"},
            {"type": "adaptive", "display": "summarized"},
            {"type": "adaptive", "display": "summarized"},
            {"type": "adaptive", "display": "summarized"},
            {"type": "adaptive", "display": "summarized"},
        ]
        assert [payload["effort"] for payload in payloads] == [
            "high",
            "high",
            "low",
            "low",
            "high",
        ]
        replayed = payloads[1]["messages"][1]
        assert replayed["content"][0] == {
            "type": "thinking",
            "thinking": "reason-0",
            "signature": "sig-0",
        }
