from collections.abc import AsyncIterator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ProcessError,
    ResultMessage,
    StreamEvent,
    ToolUseBlock,
    query,
)

from claude_sdk_proxy.domain import (
    BackendEvent,
    BackendFailure,
    CanonicalRequest,
    CapabilityReport,
    Completed,
    TextDelta,
    UnsupportedFeature,
)


def _discard_stderr(_: str) -> None:
    pass


class QueryFn(Protocol):
    def __call__(
        self, *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[Any]: ...


class AgentSdkBackend:
    def __init__(self, query_fn: QueryFn = query) -> None:
        self._query = query_fn
    def build_options(self, request: CanonicalRequest, cwd: Path) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            model=request.model,
            system_prompt=request.system,
            tools=[],
            allowed_tools=[],
            skills=[],
            setting_sources=[],
            mcp_servers={},
            strict_mcp_config=True,
            permission_mode="dontAsk",
            agents={},
            plugins=[],
            cwd=cwd,
            include_partial_messages=True,
            stderr=_discard_stderr,
            max_buffer_size=64 * 1024,
            extra_args={
                "safe-mode": None,
                "restricted": None,
                "disable-slash-commands": None,
                "no-session-persistence": None,
            },
        )
    def prompt_for(self, request: CanonicalRequest) -> str:
        if request.tools:
            raise UnsupportedFeature(
                "tools", "Agent SDK tools are built-in or MCP-executed"
            )
        if len(request.messages) != 1 or request.messages[0].role != "user":
            raise UnsupportedFeature(
                "messages", "public query input cannot replay assistant history"
            )
        return request.messages[0].content
    def structural_report(self) -> CapabilityReport:
        return CapabilityReport(
            backend="agent-sdk",
            authentication="untested",
            streaming="untested",
            prompt_construction="pass",
            multi_turn="fail",
            structured_tools="fail",
            evidence=(
                "Historical one-shot comparator; not the persistent "
                "claude-proxy gateway",
                "ClaudeAgentOptions disables built-ins and ambient sources",
                "query accepts string or user-message iterable; "
                "assistant replay is not claimed",
                "caller tools would require proxy-executed MCP and are rejected",
            ),
        )
    async def stream(self, request: CanonicalRequest) -> AsyncIterator[BackendEvent]:
        prompt = self.prompt_for(request)
        failure: str | None = None
        completed: Completed | None = None
        with TemporaryDirectory(prefix="claude-proxy-") as directory:
            options = self.build_options(request, Path(directory))
            try:
                async for message in self._query(prompt=prompt, options=options):
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
                            raise UnsupportedFeature(
                                "tools", "Claude built-in tool event"
                            )
                    elif isinstance(message, AssistantMessage) and any(
                        isinstance(block, ToolUseBlock) for block in message.content
                    ):
                        raise UnsupportedFeature("tools", "Claude built-in tool event")
                    elif isinstance(message, ResultMessage):
                        if message.is_error:
                            failure = "Agent SDK query failed"
                        else:
                            completed = Completed(message.stop_reason, message.usage)
            except ProcessError:
                raise BackendFailure(failure or "Agent SDK query failed") from None
            if failure is not None:
                raise BackendFailure(failure)
            if completed is None:
                raise BackendFailure("Agent SDK stream ended without result")
        yield completed
