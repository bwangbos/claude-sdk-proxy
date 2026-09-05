from __future__ import annotations

from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Never, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    UserMessage,
)
from claude_agent_sdk import (
    ToolResultBlock as SdkToolResultBlock,
)

from claude_sdk_proxy.domain import (
    BackendFailure,
    Completed,
    ConversationEvent,
    Dialect,
    ToolCall,
    ToolDefinition,
    ToolResultBlock,
)
from claude_sdk_proxy.sdk_metadata import (
    validate_rate_limit_event,
    validate_system_message,
)
from claude_sdk_proxy.sdk_text_protocol import fail_protocol, normalize_usage
from claude_sdk_proxy.sdk_tool_protocol import RawSdkMessageValidator
from claude_sdk_proxy.tool_bridge import ToolBridge
from claude_sdk_proxy.tool_contract import (
    validate_tool_definitions,
    validate_tool_results,
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

_STOP_REASONS = {
    "end_turn", "max_tokens", "model_context_window_exceeded", "refusal"
}


class SdkSession:
    def __init__(
        self,
        model: str,
        system: str,
        directory_factory: DirectoryFactory | None = None,
        client_factory: ClientFactory = ClaudeSDKClient,
        *,
        tools: Iterable[ToolDefinition] = (),
        dialect: Dialect = "anthropic",
    ) -> None:
        self._model = model
        self._system = system
        self._directory_factory = directory_factory or self._new_directory
        self._client_factory = client_factory
        self._directory: SessionDirectoryProtocol | None = None
        self._client: SdkClientProtocol | None = None
        self._closed = False
        self._sdk_session_id: str | None = None
        self._tools = validate_tool_definitions(tools)
        self._bridge = ToolBridge(self._tools, dialect) if self._tools else None
        self._expected_sdk_tools = (
            self._bridge.allowed_tools if self._bridge is not None else ()
        )
        self._expected_mcp_servers = (
            ("caller_tools_v1",) if self._bridge is not None else ()
        )
        self._awaiting_submit = False
        self._awaiting_echo = False
        self._epoch_needs_begin = False
        self._expected_internal_ids: set[str] = set()
        self._expected_echo_values: Counter[tuple[str, bool]] = Counter()
        self._echo_ids: set[str] = set()
        self._echo_values: Counter[tuple[str, bool]] = Counter()

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
            tools=[],
            allowed_tools=list(self._expected_sdk_tools),
            skills=[],
            setting_sources=[],
            mcp_servers=(
                {"caller_tools_v1": self._bridge.mcp_server}
                if self._bridge is not None
                else {}
            ),
            strict_mcp_config=True,
            permission_mode="dontAsk",
            agents={},
            plugins=[],
            cwd=Path(directory.__enter__()),
            include_partial_messages=True,
            stderr=_discard_stderr,
            max_buffer_size=8 * 1024 * 1024,
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
        if self._bridge is not None:
            self._bridge.cancel()
        try:
            if client is not None:
                await client.disconnect()
        except Exception:
            raise BackendFailure("Agent SDK query failed") from None
        finally:
            if directory is not None:
                directory.cleanup()

    async def stream_turn(self, prompt: str) -> AsyncIterator[ConversationEvent]:
        async for event in self.stream_generation(prompt):
            if isinstance(event, ToolCall) or (
                isinstance(event, Completed) and event.stop_reason == "tool_use"
            ):
                self._fail_protocol()
            yield event

    async def stream_generation(self, prompt: str) -> AsyncIterator[ConversationEvent]:
        client = self._client
        if client is None:
            raise BackendFailure("Agent SDK query failed")
        completed: Completed | None = None
        failure: str | None = None
        raw: RawSdkMessageValidator | None = None
        terminal_boundary: Completed | None = None
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
                    if event_type == "message_start":
                        if terminal_boundary is not None or self._awaiting_submit:
                            self._fail_protocol()
                        if self._awaiting_echo:
                            self._finish_echo()
                            self._epoch_needs_begin = True
                        raw = RawSdkMessageValidator(self._tools)
                    if raw is None:
                        self._fail_protocol()
                    if event_type == "content_block_start":
                        block = event.get("content_block")
                        if (
                            isinstance(block, Mapping)
                            and block.get("type") == "tool_use"
                            and self._epoch_needs_begin
                        ):
                            if self._bridge is None:
                                self._fail_protocol()
                            await self._bridge.begin_epoch()
                            self._epoch_needs_begin = False
                    normalized = raw.observe(event)
                    if normalized is not None:
                        yield normalized
                    if event_type == "message_stop":
                        if not raw.complete:
                            self._fail_protocol()
                        boundary = Completed(raw.stop_reason, raw.boundary_usage)
                        if raw.has_tools:
                            async for public_event in self._tool_boundary(raw):
                                yield public_event
                        else:
                            terminal_boundary = boundary
                elif isinstance(message, AssistantMessage):
                    if raw is None or self._awaiting_echo or self._awaiting_submit:
                        self._fail_protocol()
                    self._validate_assistant(message, raw)
                elif isinstance(message, ResultMessage):
                    if (
                        raw is None
                        or terminal_boundary is None
                        or self._awaiting_submit
                        or self._awaiting_echo
                    ):
                        self._fail_protocol()
                    self._observe_session_id(message.session_id)
                    if type(message.is_error) is not bool:
                        self._fail_protocol()
                    if message.is_error is True:
                        failure = "Agent SDK query failed"
                    else:
                        self._validate_result(message, terminal_boundary.stop_reason)
                        completed = terminal_boundary
                elif type(message) is SystemMessage:
                    if self._awaiting_echo or self._awaiting_submit:
                        self._fail_protocol()
                    self._observe_session_id(
                        validate_system_message(
                            message,
                            self._expected_sdk_tools,
                            self._expected_mcp_servers,
                        )
                    )
                elif isinstance(message, RateLimitEvent):
                    if self._awaiting_echo or self._awaiting_submit:
                        self._fail_protocol()
                    self._observe_session_id(validate_rate_limit_event(message))
                elif type(message) is UserMessage:
                    self._observe_result_echo(message)
                else:
                    self._fail_protocol()
        except BackendFailure:
            raise
        except Exception:
            raise BackendFailure("Agent SDK query failed") from None
        if failure is not None:
            raise BackendFailure(failure)
        if completed is None:
            raise BackendFailure("Agent SDK stream ended without result")
        yield completed

    async def submit_tool_results(self, results: Iterable[ToolResultBlock]) -> None:
        if self._bridge is None or not self._awaiting_submit or self._awaiting_echo:
            self._fail_protocol()
        normalized = validate_tool_results(results)
        self._bridge.resolve(normalized)
        self._expected_echo_values = Counter(
            ("".join(result.content), result.is_error) for result in normalized
        )
        self._echo_ids = set()
        self._echo_values = Counter()
        self._awaiting_submit = False
        self._awaiting_echo = True

    def _observe_session_id(self, value: object) -> None:
        if not isinstance(value, str) or not value:
            self._fail_protocol()
        if self._sdk_session_id is None:
            self._sdk_session_id = value
        elif value != self._sdk_session_id:
            self._fail_protocol()

    def _validate_result(
        self, message: ResultMessage, boundary_stop_reason: object
    ) -> None:
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
        usage = normalize_usage(message.usage, ("input_tokens", "output_tokens"))
        del usage
        if message.stop_reason != boundary_stop_reason:
            self._fail_protocol()

    def _validate_assistant(
        self, message: AssistantMessage, raw: RawSdkMessageValidator
    ) -> None:
        if message.session_id is not None:
            self._observe_session_id(message.session_id)
        raw.validate_assistant(message)

    async def _tool_boundary(
        self, raw: RawSdkMessageValidator
    ) -> AsyncIterator[ConversationEvent]:
        bridge = self._bridge
        if bridge is None or raw.stop_reason != "tool_use":
            self._fail_protocol()
        calls = raw.tool_calls
        invocations = await bridge.seal_epoch(
            (call.public_name, call.arguments) for call in calls
        )
        self._expected_internal_ids = {call.internal_id for call in calls}
        if len(self._expected_internal_ids) != len(calls):
            self._fail_protocol()
        self._awaiting_submit = True
        for invocation in invocations:
            yield ToolCall(invocation.public_id, invocation.name, invocation.arguments)
        yield Completed("tool_use", raw.boundary_usage)

    def _observe_result_echo(self, message: UserMessage) -> None:
        if (
            not self._awaiting_echo
            or message.parent_tool_use_id is not None
            or message.origin is not None
            or message.tool_use_result is not None
            or not isinstance(message.content, list)
            or not message.content
        ):
            self._fail_protocol()
        for block in message.content:
            if type(block) is not SdkToolResultBlock:
                self._fail_protocol()
            tool_use_id = block.tool_use_id
            if (
                not isinstance(tool_use_id, str)
                or tool_use_id not in self._expected_internal_ids
                or tool_use_id in self._echo_ids
                or type(block.is_error) is not bool
            ):
                self._fail_protocol()
            text = self._normalize_echo_content(block.content)
            self._echo_ids.add(tool_use_id)
            self._echo_values[(text, block.is_error)] += 1

    def _finish_echo(self) -> None:
        if (
            self._echo_ids != self._expected_internal_ids
            or self._echo_values != self._expected_echo_values
        ):
            self._fail_protocol()
        self._awaiting_echo = False
        self._expected_internal_ids = set()
        self._expected_echo_values = Counter()
        self._echo_ids = set()
        self._echo_values = Counter()

    @staticmethod
    def _normalize_echo_content(value: object) -> str:
        if isinstance(value, str):
            return value
        if not isinstance(value, list):
            fail_protocol()
        text: list[str] = []
        for block in value:
            if (
                not isinstance(block, Mapping)
                or set(block) != {"type", "text"}
                or block.get("type") != "text"
                or not isinstance(block.get("text"), str)
            ):
                fail_protocol()
            text.append(block["text"])
        return "".join(text)

    @staticmethod
    def _human_origin(origin: object) -> bool:
        return isinstance(origin, Mapping) and origin.get("kind") == "human"

    @staticmethod
    def _fail_protocol() -> Never:
        fail_protocol()
