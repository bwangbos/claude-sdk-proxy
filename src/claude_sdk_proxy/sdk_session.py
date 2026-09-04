from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Never, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    StreamEvent,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from claude_sdk_proxy.domain import (
    BackendFailure,
    Completed,
    ConversationEvent,
    InputUsage,
    TextDelta,
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

_TOOL_BLOCK_TYPES = {
    "tool_use", "server_tool_use", "tool_result", "server_tool_result"
}
_TOOL_DELTA_TYPES = _TOOL_BLOCK_TYPES | {"input_json_delta"}
_TOOL_BLOCK_CLASSES = (
    ToolUseBlock,
    ServerToolUseBlock,
    ToolResultBlock,
    ServerToolResultBlock,
)
_PROTOCOL_ERROR = "Agent SDK protocol failure"
_STOP_REASONS = {"end_turn", "max_tokens"}


class SdkSession:
    def __init__(
        self, model: str, system: str,
        directory_factory: DirectoryFactory | None = None,
        client_factory: ClientFactory = ClaudeSDKClient,
    ) -> None:
        self._model = model
        self._system = system
        self._directory_factory = directory_factory or self._new_directory
        self._client_factory = client_factory
        self._directory: SessionDirectoryProtocol | None = None
        self._client: SdkClientProtocol | None = None
        self._closed = False
        self._sdk_session_id: str | None = None

    @staticmethod
    def _new_directory() -> TemporaryDirectory[str]:
        return TemporaryDirectory(prefix="claude-proxy-")

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("SDK session is closed")
        if self._client is not None:
            return
        directory = self._directory_factory()
        self._directory = directory
        options = ClaudeAgentOptions(
            model=self._model,
            system_prompt=self._system,
            tools=[], allowed_tools=[], skills=[], setting_sources=[],
            mcp_servers={},
            strict_mcp_config=True,
            permission_mode="dontAsk",
            agents={}, plugins=[],
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
        self._closed = True
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
        start_input: int | None = None
        saw_text = False
        try:
            await client.query(prompt)
            async for message in client.receive_response():
                if completed is not None or failure is not None:
                    if failure is not None:
                        failure = "Agent SDK message after result"
                        continue
                    raise BackendFailure("Agent SDK message after result")
                if isinstance(message, StreamEvent):
                    self._observe_session_id(message.session_id)
                    if message.parent_tool_use_id is not None:
                        self._fail_protocol()
                    event = message.event
                    if not isinstance(event, Mapping):
                        self._fail_protocol()
                    event_type = event.get("type")
                    if not isinstance(event_type, str):
                        self._fail_protocol()
                    self._reject_tool_event(event)
                    if event_type == "message_start":
                        if start_input is not None or saw_text:
                            self._fail_protocol()
                        start_input = self._message_start_input(event)
                        yield InputUsage(start_input)
                        continue
                    if event_type == "content_block_start":
                        block = event.get("content_block")
                        if (
                            not isinstance(block, Mapping)
                            or block.get("type") != "text"
                        ):
                            self._fail_protocol()
                    elif event_type == "content_block_delta":
                        delta = event.get("delta")
                        if (
                            not isinstance(delta, Mapping)
                            or delta.get("type") != "text_delta"
                        ):
                            self._fail_protocol()
                        text = delta.get("text")
                        if not isinstance(text, str):
                            self._fail_protocol()
                        saw_text = True
                        yield TextDelta(text)
                elif isinstance(message, AssistantMessage):
                    self._validate_assistant(message)
                elif isinstance(message, UserMessage):
                    self._validate_user(message)
                elif isinstance(message, ResultMessage):
                    self._observe_session_id(message.session_id)
                    if type(message.is_error) is not bool:
                        self._fail_protocol()
                    if message.is_error is True:
                        failure = "Agent SDK query failed"
                    else:
                        completed = self._normalize_result(message, start_input)
        except BackendFailure:
            raise
        except Exception:
            raise BackendFailure("Agent SDK query failed") from None
        if failure is not None:
            raise BackendFailure(failure)
        if completed is None:
            raise BackendFailure("Agent SDK stream ended without result")
        yield completed

    def _observe_session_id(self, value: object) -> None:
        if not isinstance(value, str) or not value:
            self._fail_protocol()
        if self._sdk_session_id is None:
            self._sdk_session_id = value
        elif value != self._sdk_session_id:
            self._fail_protocol()

    def _normalize_result(
        self, message: ResultMessage, start_input: int | None
    ) -> Completed:
        if (
            message.subtype != "success"
            or message.stop_reason not in _STOP_REASONS
            or message.deferred_tool_use is not None
            or message.permission_denials not in (None, [])
            or message.errors not in (None, [])
            or message.api_error_status is not None
            or message.terminal_reason not in (None, "completed")
        ):
            self._fail_protocol()
        origin = message.origin
        if origin is not None and not self._human_origin(origin):
            self._fail_protocol()
        usage = self._normalize_usage(message.usage, ("input_tokens", "output_tokens"))
        if start_input is not None and usage is not None:
            final_input = usage.get("input_tokens")
            if final_input is not None and final_input != start_input:
                self._fail_protocol()
        return Completed(message.stop_reason, usage)
    @classmethod
    def _message_start_input(cls, event: Mapping[str, Any]) -> int:
        message = event.get("message")
        if not isinstance(message, Mapping):
            cls._fail_protocol()
        usage = cls._normalize_usage(message.get("usage"), ("input_tokens",))
        if usage is None or "input_tokens" not in usage:
            cls._fail_protocol()
        return usage["input_tokens"]
    @classmethod
    def _normalize_usage(
        cls, usage: object, fields: tuple[str, ...]
    ) -> dict[str, int] | None:
        if usage is None:
            return None
        if not isinstance(usage, Mapping):
            cls._fail_protocol()
        normalized: dict[str, int] = {}
        for field in fields:
            if field not in usage:
                continue
            value = usage[field]
            if type(value) is not int or value < 0:
                cls._fail_protocol()
            normalized[field] = value
        return normalized
    def _validate_assistant(self, message: AssistantMessage) -> None:
        if message.parent_tool_use_id is not None or message.error is not None:
            self._fail_protocol()
        if message.session_id is not None:
            self._observe_session_id(message.session_id)
        if self._has_tool_block(message.content):
            self._fail_protocol()

    @classmethod
    def _validate_user(cls, message: UserMessage) -> None:
        if (
            message.parent_tool_use_id is not None
            or message.tool_use_result is not None
            or cls._has_tool_block(message.content)
        ):
            cls._fail_protocol()
        origin = message.origin
        if origin is not None and not cls._human_origin(origin):
            cls._fail_protocol()

    @classmethod
    def _reject_tool_event(cls, event: Mapping[str, Any]) -> None:
        if event.get("type") in _TOOL_DELTA_TYPES:
            cls._fail_protocol()
        for field in ("content_block", "delta"):
            value = event.get(field)
            if isinstance(value, Mapping) and value.get("type") in _TOOL_DELTA_TYPES:
                cls._fail_protocol()

    @staticmethod
    def _has_tool_block(content: object) -> bool:
        return isinstance(content, list) and any(
            isinstance(block, _TOOL_BLOCK_CLASSES) for block in content
        )

    @staticmethod
    def _human_origin(origin: object) -> bool:
        return isinstance(origin, Mapping) and origin.get("kind") == "human"
    @staticmethod
    def _fail_protocol() -> Never:
        raise BackendFailure(_PROTOCOL_ERROR)
