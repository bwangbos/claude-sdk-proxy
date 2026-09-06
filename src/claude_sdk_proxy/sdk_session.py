from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal, Never, Protocol

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
from claude_agent_sdk.types import ThinkingConfig

from claude_sdk_proxy.domain import (
    BackendFailure,
    CanonicalMessage,
    Completed,
    ConversationEvent,
    Dialect,
    ImagePrompt,
    Prompt,
    ToolCall,
    ToolDefinition,
    ToolResultBlock,
    ToolResultPrompt,
)
from claude_sdk_proxy.images import (
    ResultIdentity,
    anthropic_image,
    render_content,
    result_identity,
)
from claude_sdk_proxy.sdk_history import seed_history
from claude_sdk_proxy.sdk_metadata import (
    validate_rate_limit_event,
    validate_system_message,
)
from claude_sdk_proxy.sdk_text_protocol import (
    USAGE_FIELDS,
    fail_protocol,
    normalize_usage,
)
from claude_sdk_proxy.sdk_tool_protocol import RawSdkMessageValidator, RawToolCall
from claude_sdk_proxy.thinking import ThinkingOptions
from claude_sdk_proxy.tool_bridge import ToolBridge, ToolInvocation
from claude_sdk_proxy.tool_contract import (
    validate_tool_definitions,
    validate_tool_results,
)


def _discard_stderr(_: str) -> None:
    pass


class SdkClientProtocol(Protocol):
    async def connect(self) -> None: ...
    async def query(self, prompt: str | AsyncIterable[dict[str, Any]]) -> None: ...
    def receive_response(self) -> AsyncIterator[Any]: ...
    async def disconnect(self) -> None: ...


class SessionDirectoryProtocol(Protocol):
    def __enter__(self) -> str: ...
    def cleanup(self) -> None: ...


type ClientFactory = Callable[[ClaudeAgentOptions], SdkClientProtocol]
type DirectoryFactory = Callable[[], SessionDirectoryProtocol]
type _ProtocolPhase = Literal["generation", "awaiting_submit", "awaiting_echo"]


@dataclass(frozen=True, slots=True)
class _ReceivedSdkMessage:
    message: Any
    phase: _ProtocolPhase
    tool_epoch: int


_STOP_REASONS = {"end_turn", "max_tokens", "model_context_window_exceeded", "refusal"}


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
        history: Iterable[CanonicalMessage] = (),
        thinking: ThinkingOptions = ThinkingOptions(),
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
        self._history = tuple(history)
        self._thinking = thinking
        self._seeded_result_prompt = bool(
            self._history
            and self._history[-1].role == "user"
            and self._history[-1].blocks
            and all(
                isinstance(block, ToolResultBlock) for block in self._history[-1].blocks
            )
        )
        self._bridge = ToolBridge(self._tools, dialect) if self._tools else None
        self._expected_sdk_tools = (
            self._bridge.allowed_tools if self._bridge is not None else ()
        )
        self._expected_mcp_servers = (
            ("caller_tools_v1",) if self._bridge is not None else ()
        )
        self._awaiting_submit = False
        self._awaiting_echo = False
        self._tool_epoch = 0
        self._epoch_needs_begin = False
        self._epoch_needs_completion = False
        self._expected_internal_ids: set[str] = set()
        self._expected_echo_values: dict[str, tuple[ResultIdentity, bool]] = {}
        self._echo_ids: set[str] = set()
        self._echo_values: dict[str, tuple[ResultIdentity, bool]] = {}

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
        try:
            options = await self._client_options(directory)
            client = self._client_factory(options)
            self._client = client
            await client.connect()
        except BaseException as error:
            try:
                await self.close()
            except BaseException:
                pass
            if isinstance(error, asyncio.CancelledError):
                raise
            raise BackendFailure("Agent SDK query failed") from None

    async def _client_options(
        self, directory: SessionDirectoryProtocol
    ) -> ClaudeAgentOptions:
        cwd = Path(directory.__enter__())
        sdk_tool_names = dict(
            zip(
                (definition.name for definition in self._tools),
                self._expected_sdk_tools,
                strict=True,
            )
        )
        seeded = (
            await seed_history(
                self._history,
                cwd=cwd,
                model=self._model,
                sdk_tool_names=sdk_tool_names,
            )
            if self._history
            else None
        )
        thinking: ThinkingConfig
        if self._thinking.mode == "disabled":
            thinking = {"type": "disabled"}
        elif self._thinking.mode == "adaptive":
            thinking = {"type": "adaptive"}
            if self._thinking.display is not None:
                thinking["display"] = self._thinking.display
        else:
            budget = self._thinking.budget_tokens
            if budget is None:
                raise ValueError("enabled thinking requires budget_tokens")
            thinking = {"type": "enabled", "budget_tokens": budget}
            if self._thinking.display is not None:
                thinking["display"] = self._thinking.display
        return ClaudeAgentOptions(
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
            cwd=cwd,
            include_partial_messages=True,
            thinking=thinking,
            effort=self._thinking.effort,
            stderr=_discard_stderr,
            # Tool-result envelopes can contain the image data twice.
            max_buffer_size=40 * 1024 * 1024,
            env={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
            extra_args={
                "restricted": None,
                "disable-slash-commands": None,
                **({} if seeded is not None else {"no-session-persistence": None}),
            },
            session_store=seeded.store if seeded is not None else None,
            resume=seeded.session_id if seeded is not None else None,
        )

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

    async def stream_turn(self, prompt: Prompt) -> AsyncIterator[ConversationEvent]:
        async for event in self.stream_generation(prompt):
            if isinstance(event, ToolCall) or (
                isinstance(event, Completed) and event.stop_reason == "tool_use"
            ):
                self._fail_protocol()
            yield event

    async def stream_generation(
        self, prompt: Prompt
    ) -> AsyncIterator[ConversationEvent]:
        client = self._client
        if client is None:
            raise BackendFailure("Agent SDK query failed")
        completed: Completed | None = None
        failure: str | None = None
        raw: RawSdkMessageValidator | None = None
        terminal_boundary: Completed | None = None
        prefetched: asyncio.Future[_ReceivedSdkMessage] | None = None
        try:
            if isinstance(prompt, ToolResultPrompt):
                if not self._seeded_result_prompt or self._history[
                    -1
                ] != CanonicalMessage("user", prompt.results):
                    self._fail_protocol()
                self._seeded_result_prompt = False
                # Seed calls AND results atomically. Resuming with a dangling
                # tool_use causes the native loader to discard it. An empty
                # query continues the completed native history without adding
                # a natural-language instruction or rerunning caller tools.
                await client.query("")
            elif isinstance(prompt, ImagePrompt):

                async def structured_prompt() -> AsyncIterator[dict[str, Any]]:
                    yield {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": render_content(prompt.blocks),
                        },
                        "parent_tool_use_id": None,
                    }

                await client.query(structured_prompt())
            else:
                await client.query(prompt)
            response = client.receive_response()
            while True:
                try:
                    if self._epoch_needs_completion:
                        received = await self._receive_after_epoch_completion(
                            response, prefetched
                        )
                        prefetched = None
                    elif prefetched is None:
                        received = await self._receive_message(response)
                    else:
                        received = await prefetched
                        prefetched = None
                except StopAsyncIteration:
                    break
                message = received.message
                position = self._protocol_position()
                if (received.phase, received.tool_epoch) != position and not (
                    (
                        type(message) is RateLimitEvent
                        or (
                            type(message) is SystemMessage
                            and message.subtype == "status"
                        )
                    )
                    and position == ("awaiting_echo", self._tool_epoch)
                    and received.phase in {"generation", "awaiting_submit"}
                    and received.tool_epoch in {self._tool_epoch - 1, self._tool_epoch}
                ):
                    self._fail_protocol()
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
                        raw = RawSdkMessageValidator(
                            self._tools,
                            allow_seeded_history=bool(self._history),
                        )
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
                            public_events, prefetched = await self._tool_boundary(
                                raw, response
                            )
                            for public_event in public_events:
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
                    if self._awaiting_submit or (
                        self._awaiting_echo and message.subtype != "status"
                    ):
                        self._fail_protocol()
                    if message.subtype == "thinking_tokens" and (
                        raw is None or not raw.thinking_active
                    ):
                        self._fail_protocol()
                    self._observe_session_id(
                        validate_system_message(
                            message,
                            self._expected_sdk_tools,
                            self._expected_mcp_servers,
                        )
                    )
                elif isinstance(message, RateLimitEvent):
                    if self._awaiting_submit:
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
        finally:
            if prefetched is not None:
                if not prefetched.done():
                    prefetched.cancel()
                await asyncio.gather(prefetched, return_exceptions=True)
        if failure is not None:
            raise BackendFailure(failure)
        if completed is None:
            raise BackendFailure("Agent SDK stream ended without result")
        yield completed

    async def submit_tool_results(self, results: Iterable[ToolResultBlock]) -> None:
        if self._bridge is None or not self._awaiting_submit or self._awaiting_echo:
            self._fail_protocol()
        normalized = validate_tool_results(results)
        self._expected_echo_values = self._bridge.resolve(normalized)
        if set(self._expected_echo_values) != self._expected_internal_ids:
            self._fail_protocol()
        self._echo_ids = set()
        self._echo_values = {}
        self._awaiting_submit = False
        self._awaiting_echo = True

    async def wait_failure(self) -> None:
        bridge = self._bridge
        if bridge is None:
            await asyncio.Event().wait()
            return
        await bridge.wait_failure()

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
        usage = normalize_usage(message.usage, USAGE_FIELDS)
        del usage
        if message.stop_reason != boundary_stop_reason:
            self._fail_protocol()

    def _validate_assistant(
        self,
        message: AssistantMessage,
        raw: RawSdkMessageValidator,
    ) -> None:
        if message.session_id is not None:
            self._observe_session_id(message.session_id)
        raw.validate_assistant(message)

    async def _tool_boundary(
        self,
        raw: RawSdkMessageValidator,
        response: AsyncIterator[Any],
    ) -> tuple[tuple[ConversationEvent, ...], asyncio.Future[_ReceivedSdkMessage]]:
        bridge = self._bridge
        if bridge is None or raw.stop_reason != "tool_use":
            self._fail_protocol()
        calls = raw.tool_calls
        invocations, prefetched = await self._seal_tool_epoch(
            bridge,
            tuple(
                (call.internal_id, call.public_name, call.arguments) for call in calls
            ),
            response,
        )
        self._expected_internal_ids = {call.internal_id for call in calls}
        if len(self._expected_internal_ids) != len(calls):
            self._fail_protocol()
        self._tool_epoch += 1
        self._awaiting_submit = True
        public_calls = {
            call.internal_id: ToolCall(
                invocation.public_id, invocation.name, invocation.arguments
            )
            for call, invocation in zip(calls, invocations, strict=True)
        }
        events: tuple[ConversationEvent, ...] = (
            *(
                public_calls[event.internal_id]
                if isinstance(event, RawToolCall)
                else event
                for event in raw.tool_suffix
            ),
            Completed("tool_use", raw.boundary_usage),
        )
        return events, prefetched

    async def _seal_tool_epoch(
        self,
        bridge: ToolBridge,
        expected_calls: tuple[tuple[str, str, Mapping[str, object]], ...],
        response: AsyncIterator[Any],
    ) -> tuple[tuple[ToolInvocation, ...], asyncio.Future[_ReceivedSdkMessage]]:
        invocations = await bridge.seal_epoch(expected_calls)
        incoming = asyncio.create_task(self._receive_message(response))
        return invocations, incoming

    async def _receive_message(
        self, response: AsyncIterator[Any]
    ) -> _ReceivedSdkMessage:
        message = await anext(response)
        phase, tool_epoch = self._protocol_position()
        return _ReceivedSdkMessage(message, phase, tool_epoch)

    async def _receive_after_epoch_completion(
        self,
        response: AsyncIterator[Any],
        prefetched: asyncio.Future[_ReceivedSdkMessage] | None,
    ) -> _ReceivedSdkMessage:
        bridge = self._bridge
        if bridge is None:
            self._fail_protocol()
        incoming_was_done = prefetched is not None and prefetched.done()
        completion = asyncio.create_task(bridge.wait_epoch_complete())
        incoming = prefetched or asyncio.create_task(self._receive_message(response))
        loop = asyncio.get_running_loop()
        winner: asyncio.Future[Literal["callback", "incoming"]] = loop.create_future()

        def record_winner(label: Literal["callback", "incoming"]) -> None:
            if not winner.done():
                winner.set_result(label)

        if incoming_was_done:
            record_winner("incoming")
        else:
            incoming.add_done_callback(lambda _: record_winner("incoming"))
        completion.add_done_callback(lambda _: record_winner("callback"))
        try:
            if await winner == "callback":
                await completion
                self._epoch_needs_completion = False
                return await incoming

            await asyncio.gather(incoming, return_exceptions=True)
            self._fail_protocol()
        finally:
            tasks = (winner, completion, incoming)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _protocol_position(self) -> tuple[_ProtocolPhase, int]:
        if self._awaiting_submit:
            return "awaiting_submit", self._tool_epoch
        if self._awaiting_echo:
            return "awaiting_echo", self._tool_epoch
        return "generation", self._tool_epoch

    def _observe_result_echo(self, message: UserMessage) -> None:
        if (
            not self._awaiting_echo
            or message.parent_tool_use_id is not None
            or message.origin is not None
            or not isinstance(message.content, list)
            or not message.content
        ):
            self._fail_protocol()
        raw_result = message.tool_use_result
        if raw_result is not None:
            if (
                len(message.content) != 1
                or type(message.content[0]) is not SdkToolResultBlock
            ):
                self._fail_protocol()
            typed_result = message.content[0]
            typed_text = self._normalize_echo_content(typed_result.content)
            if typed_result.is_error is True:
                if (
                    not isinstance(raw_result, str)
                    or raw_result != f"Error: {typed_text}"
                ):
                    self._fail_protocol()
            elif (
                not isinstance(raw_result, list)
                or self._normalize_echo_content(raw_result) != typed_text
            ):
                self._fail_protocol()
        for block in message.content:
            if type(block) is not SdkToolResultBlock:
                self._fail_protocol()
            tool_use_id = block.tool_use_id
            is_error = block.is_error
            if (
                not isinstance(tool_use_id, str)
                or tool_use_id not in self._expected_internal_ids
                or tool_use_id in self._echo_ids
                or is_error is not None
                and type(is_error) is not bool
            ):
                self._fail_protocol()
            text = self._normalize_echo_content(block.content)
            self._echo_ids.add(tool_use_id)
            self._echo_values[tool_use_id] = (text, is_error is True)
        if self._echo_ids == self._expected_internal_ids:
            self._finish_echo()

    def _finish_echo(self) -> None:
        if (
            self._echo_ids != self._expected_internal_ids
            or self._echo_values != self._expected_echo_values
        ):
            self._fail_protocol()
        self._awaiting_echo = False
        self._epoch_needs_completion = True
        self._epoch_needs_begin = True
        self._expected_internal_ids = set()
        self._expected_echo_values = {}
        self._echo_ids = set()
        self._echo_values = {}

    @staticmethod
    def _normalize_echo_content(value: object) -> ResultIdentity:
        if isinstance(value, str):
            return value
        if not isinstance(value, list):
            fail_protocol()
        if any(
            isinstance(block, Mapping) and block.get("type") == "image"
            for block in value
        ):
            parts = []
            for block in value:
                if not isinstance(block, Mapping):
                    fail_protocol()
                if block.get("type") == "image":
                    try:
                        parts.append(anthropic_image(block))
                    except ValueError:
                        fail_protocol()
                elif (
                    set(block) == {"type", "text"}
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                ):
                    parts.append(block["text"])
                else:
                    fail_protocol()
            return result_identity(parts)
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
