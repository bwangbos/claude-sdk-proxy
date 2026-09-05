from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
    UserMessage,
)
from mcp.server import Server
from mcp.types import CallToolRequestParams, CallToolResult

from claude_sdk_proxy.domain import (
    Completed,
    ConversationEvent,
    Dialect,
    TextDelta,
    ToolDefinition,
)


class FixedTemporaryDirectory:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.cleanup_count = 0

    def __enter__(self) -> str:
        return str(self.path)

    def __exit__(self, *args: object) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        self.cleanup_count += 1


class FakeSdkClient:
    def __init__(
        self,
        responses: tuple[tuple[Any, ...], ...],
        *,
        start_tool_callbacks: bool = True,
        wait_for_tool_callbacks_before_user: bool = True,
        user_message_barrier: tuple[
            asyncio.Event, asyncio.Event, asyncio.Event
        ]
        | None = None,
        message_barriers: Mapping[int, tuple[asyncio.Event, asyncio.Event]]
        | None = None,
        before_message_actions: Mapping[int, Callable[[], None]] | None = None,
    ) -> None:
        self._responses = iter(responses)
        self._response: tuple[Any, ...] = ()
        self.connect_count = 0
        self.disconnect_count = 0
        self.disconnected = asyncio.Event()
        self.tool_handler_count = 0
        self.prompts: list[str] = []
        self.options: ClaudeAgentOptions | None = None
        self.tool_results: list[CallToolResult] = []
        self._tool_tasks: list[asyncio.Task[CallToolResult]] = []
        self._parked_tool_tasks: list[asyncio.Task[CallToolResult]] = []
        self._start_callbacks = start_tool_callbacks
        self._wait_before_user = wait_for_tool_callbacks_before_user
        self._user_message_barrier = user_message_barrier
        self._message_barriers = dict(message_barriers or {})
        self._before_message_actions = dict(before_message_actions or {})

    def capture_options(self, options: ClaudeAgentOptions) -> FakeSdkClient:
        self.options = options
        return self

    async def connect(self) -> None:
        self.connect_count += 1

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)
        self._response = next(self._responses)

    async def receive_response(self) -> AsyncIterator[Any]:
        for index, message in enumerate(self._response):
            barrier = self._message_barriers.get(index)
            if barrier is not None:
                entered, release = barrier
                entered.set()
                await release.wait()
            action = self._before_message_actions.get(index)
            if action is not None:
                action()
            if self._start_callbacks and isinstance(message, AssistantMessage):
                self._start_tool_callbacks(message)
            if (
                self._wait_before_user
                and isinstance(message, UserMessage)
                and self._tool_tasks
            ):
                self.tool_results.extend(await asyncio.gather(*self._tool_tasks))
                self._tool_tasks.clear()
            if isinstance(message, UserMessage) and self._user_message_barrier:
                reached, release, delivered = self._user_message_barrier
                reached.set()
                await release.wait()
                # No await separates this signal from returning the item, so a
                # waiter resumes only after the pending anext() has completed.
                delivered.set()
            yield message

    async def disconnect(self) -> None:
        self.disconnect_count += 1
        self.disconnected.set()
        tasks = [*self._tool_tasks, *self._parked_tool_tasks]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tool_tasks.clear()
        self._parked_tool_tasks.clear()

    def _start_tool_callbacks(self, message: AssistantMessage) -> None:
        for block in message.content:
            if type(block) is not ToolUseBlock:
                continue
            prefix = "mcp__caller_tools_v1__"
            if not block.name.startswith(prefix):
                continue
            self.start_tool_callback(
                block.name.removeprefix(prefix),
                block.input,
                internal_id=block.id,
            )

    def start_tool_callback(
        self,
        name: str,
        arguments: object,
        *,
        internal_id: str,
        wait_for_echo: bool = True,
        entry_barrier: asyncio.Event | None = None,
        completed: asyncio.Event | None = None,
    ) -> None:
        assert self.options is not None
        assert isinstance(self.options.mcp_servers, dict)
        config = self.options.mcp_servers["caller_tools_v1"]
        server = cast(Server[Any], config["instance"])
        entry = server.get_request_handler("tools/call")
        assert entry is not None
        params = CallToolRequestParams(
            name=name,
            arguments=cast(dict[str, object], arguments),
            meta={"claudecode/toolUseId": internal_id},
        )
        async def invoke_handler() -> CallToolResult:
            if entry_barrier is not None:
                await entry_barrier.wait()
            self.tool_handler_count += 1
            try:
                return await cast(Any, entry.handler)(None, params)
            finally:
                if completed is not None:
                    completed.set()

        task = asyncio.create_task(invoke_handler())
        target = self._tool_tasks if wait_for_echo else self._parked_tool_tasks
        target.append(task)


def raw_text_events(
    text: str,
    session_id: str,
    *,
    input_tokens: int = 2,
    output_tokens: int = 1,
    stop_reason: str = "end_turn",
) -> tuple[StreamEvent, ...]:
    return (
        StreamEvent(
            uuid="event-start",
            session_id=session_id,
            event={
                "type": "message_start",
                "message": {
                    "usage": {"input_tokens": input_tokens, "output_tokens": 0}
                },
            },
        ),
        StreamEvent(
            uuid="event-block-start",
            session_id=session_id,
            event={
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        StreamEvent(
            uuid="event-delta",
            session_id=session_id,
            event={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        StreamEvent(
            uuid="event-block-stop",
            session_id=session_id,
            event={"type": "content_block_stop", "index": 0},
        ),
        StreamEvent(
            uuid="event-message-delta",
            session_id=session_id,
            event={
                "type": "message_delta",
                "delta": {
                    "stop_reason": stop_reason,
                    "stop_sequence": None,
                    "stop_details": None,
                },
                "usage": {"output_tokens": output_tokens},
                "context_management": {"applied_edits": []},
            },
        ),
        StreamEvent(
            uuid="event-message-stop",
            session_id=session_id,
            event={"type": "message_stop"},
        ),
    )


def raw_tool_events(
    calls: tuple[tuple[str, str, str], ...],
    session_id: str,
    *,
    text: str | None = None,
    input_tokens: int = 3,
    output_tokens: int = 2,
) -> tuple[StreamEvent | AssistantMessage, ...]:
    """Build one complete raw/typed native tool-use assistant boundary.

    Each call is ``(internal_id, generated_sdk_name, partial_json)``.
    """
    events: list[StreamEvent | AssistantMessage] = [
        StreamEvent(
            uuid="event-tool-start",
            session_id=session_id,
            event={
                "type": "message_start",
                "message": {
                    "usage": {"input_tokens": input_tokens, "output_tokens": 0}
                },
            },
        )
    ]
    complete: list[TextBlock | ToolUseBlock] = []
    import json

    offset = 0
    if text is not None:
        events.extend(
            [
                StreamEvent(
                    uuid="event-tool-text-start",
                    session_id=session_id,
                    event={
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                ),
                StreamEvent(
                    uuid="event-tool-text-delta",
                    session_id=session_id,
                    event={
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": text},
                    },
                ),
                StreamEvent(
                    uuid="event-tool-text-stop",
                    session_id=session_id,
                    event={"type": "content_block_stop", "index": 0},
                ),
            ]
        )
        complete.append(TextBlock(text))
        offset = 1

    for index, (internal_id, sdk_name, partial_json) in enumerate(calls):
        block_index = index + offset
        events.extend(
            [
                StreamEvent(
                    uuid=f"event-tool-{index}-start",
                    session_id=session_id,
                    event={
                        "type": "content_block_start",
                        "index": block_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": internal_id,
                            "name": sdk_name,
                            "input": {},
                            "caller": {"type": "direct"},
                        },
                    },
                ),
                StreamEvent(
                    uuid=f"event-tool-{index}-delta",
                    session_id=session_id,
                    event={
                        "type": "content_block_delta",
                        "index": block_index,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": partial_json,
                        },
                    },
                ),
                StreamEvent(
                    uuid=f"event-tool-{index}-stop",
                    session_id=session_id,
                    event={"type": "content_block_stop", "index": block_index},
                ),
            ]
        )
        complete.append(ToolUseBlock(internal_id, sdk_name, json.loads(partial_json)))
    events.extend(
        [
            AssistantMessage(complete, "sonnet", session_id=session_id),
            StreamEvent(
                uuid="event-tool-delta",
                session_id=session_id,
                event={
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": "tool_use",
                        "stop_sequence": None,
                        "stop_details": None,
                    },
                    "usage": {"output_tokens": output_tokens},
                    "context_management": {"applied_edits": []},
                },
            ),
            StreamEvent(
                uuid="event-tool-stop",
                session_id=session_id,
                event={"type": "message_stop"},
            ),
        ]
    )
    return tuple(events)


def sdk_response(
    text: str, session_id: str
) -> tuple[StreamEvent | AssistantMessage | ResultMessage, ...]:
    raw = raw_text_events(text, session_id)
    return (
        *raw[:3],
        AssistantMessage([TextBlock(text)], "sonnet", session_id=session_id),
        *raw[3:],
        ResultMessage(
            subtype="success",
            duration_ms=0,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id=session_id,
            stop_reason="end_turn",
            usage={"output_tokens": 1},
        ),
    )


class FakeConversationSession:
    def __init__(self, text: str) -> None:
        self._text = text
        self.start_count = 0
        self.close_count = 0
        self.prompts: list[str] = []

    async def start(self) -> None:
        self.start_count += 1

    async def stream_generation(
        self, prompt: str
    ) -> AsyncIterator[ConversationEvent]:
        self.prompts.append(prompt)
        yield TextDelta(self._text)
        yield Completed("end_turn", {"output_tokens": 1})

    async def close(self) -> None:
        if self.close_count == 0:
            self.close_count = 1


class FakeSessionFactory:
    def __init__(self, outputs: tuple[str, ...]) -> None:
        self._outputs = iter(outputs)
        self.sessions: list[FakeConversationSession] = []

    @property
    def created(self) -> int:
        return len(self.sessions)

    def __call__(
        self,
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
    ) -> FakeConversationSession:
        del model, system, tools, dialect
        session = FakeConversationSession(next(self._outputs))
        self.sessions.append(session)
        return session
