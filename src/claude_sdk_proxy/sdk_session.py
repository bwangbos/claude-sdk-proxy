from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    ToolUseBlock,
)

from claude_sdk_proxy.domain import (
    BackendFailure,
    Completed,
    ConversationEvent,
    TextDelta,
    UnsupportedFeature,
)


def _discard_stderr(_: str) -> None:
    pass


class SdkClientProtocol(Protocol):
    async def connect(self) -> None: ...
    async def query(self, prompt: str) -> None: ...
    def receive_response(self) -> AsyncIterator[Any]: ...
    async def disconnect(self) -> None: ...


class SessionDirectoryProtocol(Protocol):
    def __enter__(self) -> str: ...
    def cleanup(self) -> None: ...


type ClientFactory = Callable[[ClaudeAgentOptions], SdkClientProtocol]
type DirectoryFactory = Callable[[], SessionDirectoryProtocol]


class SdkSession:
    def __init__(
        self,
        model: str,
        system: str,
        directory_factory: DirectoryFactory | None = None,
        client_factory: ClientFactory = ClaudeSDKClient,
    ) -> None:
        self._model = model
        self._system = system
        self._directory_factory = directory_factory or self._new_directory
        self._client_factory = client_factory
        self._directory: SessionDirectoryProtocol | None = None
        self._client: SdkClientProtocol | None = None

    @staticmethod
    def _new_directory() -> TemporaryDirectory[str]:
        return TemporaryDirectory(prefix="claude-proxy-")

    async def start(self) -> None:
        if self._client is not None:
            return
        directory = self._directory_factory()
        self._directory = directory
        options = ClaudeAgentOptions(
            model=self._model,
            system_prompt=self._system,
            tools=[],
            allowed_tools=[],
            skills=[],
            setting_sources=[],
            mcp_servers={},
            strict_mcp_config=True,
            permission_mode="dontAsk",
            agents={},
            plugins=[],
            cwd=Path(directory.__enter__()),
            include_partial_messages=True,
            stderr=_discard_stderr,
            max_buffer_size=64 * 1024,
            env={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
            extra_args={
                "restricted": None,
                "disable-slash-commands": None,
                "no-session-persistence": None,
            },
        )
        client = self._client_factory(options)
        self._client = client
        try:
            await client.connect()
        except Exception:
            await self.close()
            raise BackendFailure("Agent SDK query failed") from None

    async def close(self) -> None:
        client, directory = self._client, self._directory
        self._client = None
        self._directory = None
        try:
            if client is not None:
                await client.disconnect()
        except Exception:
            raise BackendFailure("Agent SDK query failed") from None
        finally:
            if directory is not None:
                directory.cleanup()

    async def stream_turn(self, prompt: str) -> AsyncIterator[ConversationEvent]:
        client = self._client
        if client is None:
            raise BackendFailure("Agent SDK query failed")
        completed: Completed | None = None
        failure: str | None = None
        try:
            await client.query(prompt)
            async for message in client.receive_response():
                if completed is not None or failure is not None:
                    if failure is not None:
                        failure = "Agent SDK message after result"
                        continue
                    raise BackendFailure("Agent SDK message after result")
                if isinstance(message, StreamEvent):
                    event = message.event
                    if event.get("type") != "content_block_delta":
                        continue
                    delta = event.get("delta")
                    if not isinstance(delta, dict):
                        raise BackendFailure("invalid Agent SDK delta")
                    if delta.get("type") == "text_delta":
                        text = delta.get("text")
                        if not isinstance(text, str):
                            raise BackendFailure("invalid Agent SDK text delta")
                        yield TextDelta(text)
                    elif delta.get("type") in {"input_json_delta", "tool_use"}:
                        raise UnsupportedFeature("tools", "Claude built-in tool event")
                elif isinstance(message, AssistantMessage) and any(
                    isinstance(block, ToolUseBlock) for block in message.content
                ):
                    raise UnsupportedFeature("tools", "Claude built-in tool event")
                elif isinstance(message, ResultMessage):
                    if message.is_error:
                        failure = "Agent SDK query failed"
                    else:
                        completed = Completed(message.stop_reason, message.usage)
        except (BackendFailure, UnsupportedFeature):
            raise
        except Exception:
            raise BackendFailure("Agent SDK query failed") from None
        if failure is not None:
            raise BackendFailure(failure)
        if completed is None:
            raise BackendFailure("Agent SDK stream ended without result")
        yield completed
