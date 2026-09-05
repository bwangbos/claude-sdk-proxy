from __future__ import annotations

import asyncio
import re
import secrets
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from claude_agent_sdk.types import McpSdkServerConfig
from jsonschema.exceptions import ValidationError  # type: ignore[import-untyped]
from jsonschema.validators import validator_for  # type: ignore[import-untyped]
from mcp.server import Server, ServerRequestContext
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

from claude_sdk_proxy.domain import (
    BackendFailure,
    Dialect,
    RequestValidationError,
    ToolCall,
    ToolDefinition,
    ToolResultBlock,
)
from claude_sdk_proxy.tool_contract import (
    JsonValue,
    canonical_json,
    freeze_json,
    plain_json,
    validate_tool_arguments,
    validate_tool_definitions,
    validate_tool_results,
)

_SERVER_NAME = "caller_tools_v1"
_MAX_RESULT_CHARS = 256 * 1024
_PUBLIC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_PROTOCOL_FAILURE = "SDK tool protocol failure"
_CANCELLED = "SDK tool bridge closed"


class BridgeProtocolFailure(RuntimeError):
    """Private diagnostic for a malformed or out-of-epoch SDK callback."""


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    public_id: str
    name: str
    arguments: Mapping[str, JsonValue]
    sdk_name: str


@dataclass(slots=True)
class _PendingInvocation:
    invocation: ToolInvocation
    future: asyncio.Future[ToolResultBlock]
    delivered: bool = False


type ExpectedCall = ToolCall | ToolInvocation | tuple[str, Mapping[str, object]]


class ToolBridge:
    """Expose caller tools to the SDK while leaving their execution to the caller."""

    def __init__(
        self,
        definitions: Iterable[ToolDefinition],
        dialect: Dialect,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if dialect not in {"anthropic", "openai"}:
            raise ValueError("tool bridge dialect is invalid")
        self._dialect = dialect
        self._definitions = validate_tool_definitions(definitions)
        self._definitions_by_name = {
            definition.name: definition for definition in self._definitions
        }
        self._validators: dict[str, Any] = {}
        for definition in self._definitions:
            schema = _plain_json_object(definition.input_schema)
            validator_class = validator_for(schema)
            self._validators[definition.name] = validator_class(schema)

        self._id_factory = id_factory or self._new_public_id
        self._issued_ids: set[str] = set()
        self._pending: dict[str, _PendingInvocation] = {}
        self._parked_failures: set[asyncio.Future[None]] = set()
        self._publications: asyncio.Queue[ToolInvocation | BridgeProtocolFailure] = (
            asyncio.Queue()
        )
        self._entry_changed = asyncio.Event()
        self._failure_changed = asyncio.Event()
        self._failure: BridgeProtocolFailure | None = None
        self._cancelled = False
        self._epoch_sealed = False
        self._epoch_verified = False
        self._epoch_resolved = False
        self._epoch_transition: asyncio.Future[None] | None = None
        self._epoch_invocations: list[ToolInvocation] = []

        wire_tools = [
            Tool(
                name=definition.name,
                description=definition.description,
                input_schema=_plain_json_object(definition.input_schema),
                _meta={"anthropic/maxResultSizeChars": _MAX_RESULT_CHARS},
            )
            for definition in self._definitions
        ]

        async def on_list_tools(
            context: ServerRequestContext[object],
            params: PaginatedRequestParams | None,
        ) -> ListToolsResult:
            del context, params
            return ListToolsResult(tools=wire_tools)

        async def on_call_tool(
            context: ServerRequestContext[object], params: CallToolRequestParams
        ) -> CallToolResult:
            del context
            return await self.invoke(params.name, params.arguments or {})

        server: Server[object] = Server(
            _SERVER_NAME,
            version="1.0.0",
            on_list_tools=on_list_tools,
            on_call_tool=on_call_tool,
        )
        self.mcp_server: McpSdkServerConfig = {
            "type": "sdk",
            "name": _SERVER_NAME,
            "instance": server,
        }
        self.server_config = self.mcp_server
        self.allowed_tools = tuple(
            self._sdk_name(definition.name) for definition in self._definitions
        )

    async def call_tool(self, name: str, arguments: object) -> CallToolResult:
        """Exercise the same bridge path as the registered low-level callback."""
        return await self.invoke(name, arguments)

    async def invoke(self, name: str, arguments: object) -> CallToolResult:
        """Publish one SDK handler entry and wait for its caller-owned result."""
        if self._cancelled:
            raise BackendFailure(_CANCELLED)
        if self._epoch_sealed:
            return await self._park_protocol_failure(
                BridgeProtocolFailure("tool callback entered a sealed epoch")
            )

        definition = self._definitions_by_name.get(name)
        if definition is None:
            return await self._park_protocol_failure(
                BridgeProtocolFailure("SDK invoked an unknown caller tool")
            )

        try:
            frozen_arguments = validate_tool_arguments(arguments)
            self._validators[name].validate(plain_json(frozen_arguments))
        except (RequestValidationError, ValidationError):
            return await self._park_protocol_failure(
                BridgeProtocolFailure("SDK supplied invalid caller-tool arguments")
            )

        try:
            public_id = self._id_factory()
        except Exception:
            return await self._park_protocol_failure(
                BridgeProtocolFailure("public tool ID generation failed")
            )
        if not self._valid_new_id(public_id):
            return await self._park_protocol_failure(
                BridgeProtocolFailure("public tool ID generation was invalid")
            )

        loop = asyncio.get_running_loop()
        future: asyncio.Future[ToolResultBlock] = loop.create_future()
        invocation = ToolInvocation(
            public_id=public_id,
            name=definition.name,
            arguments=frozen_arguments,
            sdk_name=self._sdk_name(definition.name),
        )
        pending = _PendingInvocation(invocation, future)
        self._issued_ids.add(public_id)
        self._pending[public_id] = pending
        self._epoch_invocations.append(invocation)
        self._publications.put_nowait(invocation)
        self._entry_changed.set()

        try:
            result = await future
            return CallToolResult(
                content=[
                    TextContent(type="text", text=text) for text in result.content
                ],
                is_error=result.is_error,
            )
        finally:
            if self._pending.get(public_id) is pending:
                del self._pending[public_id]
                self._finish_epoch_transition()

    async def next_invocation(self) -> ToolInvocation:
        """Return the next handler entry, redacting protocol failures."""
        publication = await self._publications.get()
        if isinstance(publication, BridgeProtocolFailure):
            raise BackendFailure(_PROTOCOL_FAILURE)
        return publication

    async def seal_epoch(
        self, expected_calls: Iterable[ExpectedCall]
    ) -> tuple[ToolInvocation, ...]:
        """Close the current callback epoch after its exact raw set has entered."""
        expected = tuple(_call_identity(call) for call in expected_calls)
        expected_count = len(expected)
        while True:
            self._entry_changed.clear()
            if self._cancelled:
                raise BackendFailure(_CANCELLED)
            if self._failure is not None:
                raise BackendFailure(_PROTOCOL_FAILURE)
            if self._epoch_sealed:
                self._signal_failure(
                    BridgeProtocolFailure("tool callback epoch was sealed twice")
                )
                raise BackendFailure(_PROTOCOL_FAILURE)
            actual_count = len(self._epoch_invocations)
            if actual_count > expected_count:
                self._signal_failure(
                    BridgeProtocolFailure("SDK published extra tool callbacks")
                )
                raise BackendFailure(_PROTOCOL_FAILURE)
            if actual_count == expected_count:
                actual = tuple(
                    (item.name, canonical_json(item.arguments))
                    for item in self._epoch_invocations
                )
                if Counter(actual) != Counter(expected):
                    self._signal_failure(
                        BridgeProtocolFailure("SDK tool callback set did not match")
                    )
                    raise BackendFailure(_PROTOCOL_FAILURE)
                self._epoch_sealed = True
                self._epoch_verified = True
                return tuple(self._epoch_invocations)
            await self._entry_changed.wait()

    async def begin_epoch(self) -> None:
        """Open admission for the next assistant message after handler quiescence."""
        if self._cancelled:
            raise BackendFailure(_CANCELLED)
        if self._failure is not None:
            raise BackendFailure(_PROTOCOL_FAILURE)
        if (
            not self._epoch_sealed
            or not self._epoch_verified
            or not self._epoch_resolved
            or any(not pending.delivered for pending in self._pending.values())
            or self._epoch_transition is not None
        ):
            self._signal_failure(
                BridgeProtocolFailure("tool callback epoch transition was premature")
            )
            raise BackendFailure(_PROTOCOL_FAILURE)

        loop = asyncio.get_running_loop()
        transition: asyncio.Future[None] = loop.create_future()
        self._epoch_transition = transition
        self._finish_epoch_transition()
        try:
            await asyncio.shield(transition)
        except asyncio.CancelledError:
            if self._epoch_transition is transition:
                self._epoch_transition = None
            raise

    def resolve(self, results: Iterable[ToolResultBlock]) -> None:
        """Atomically validate and deliver one complete caller result batch."""
        validated = validate_tool_results(results)
        result_ids = {result.tool_call_id for result in validated}
        pending_ids = {
            public_id
            for public_id, pending in self._pending.items()
            if not pending.delivered
        }
        if result_ids != pending_ids:
            raise RequestValidationError(
                "messages", "tool results must match pending calls"
            )

        by_id = {result.tool_call_id: result for result in validated}
        pending_batch = tuple(self._pending[public_id] for public_id in pending_ids)
        if any(pending.future.done() for pending in pending_batch):
            raise RequestValidationError(
                "messages", "tool results must match pending calls"
            )

        self._epoch_sealed = True
        self._epoch_resolved = True
        for pending in pending_batch:
            pending.delivered = True
            pending.future.set_result(by_id[pending.invocation.public_id])

    def cancel(self) -> None:
        """Fail all parked callbacks and permanently close admission."""
        if self._cancelled:
            return
        self._cancelled = True
        failure = BackendFailure(_CANCELLED)
        for pending in tuple(self._pending.values()):
            if not pending.future.done():
                pending.future.set_exception(failure)
        for parked in tuple(self._parked_failures):
            if not parked.done():
                parked.set_exception(BackendFailure(_CANCELLED))
        if self._epoch_transition is not None:
            if not self._epoch_transition.done():
                self._epoch_transition.set_exception(BackendFailure(_CANCELLED))
            self._epoch_transition = None
        self._publications.put_nowait(BridgeProtocolFailure("bridge was cancelled"))
        self._entry_changed.set()

    async def wait_failure(self) -> None:
        """Wait until an SDK callback violates the bridge's lifetime protocol."""
        await self._failure_changed.wait()
        raise BackendFailure(_PROTOCOL_FAILURE)

    async def _park_protocol_failure(
        self, failure: BridgeProtocolFailure
    ) -> CallToolResult:
        loop = asyncio.get_running_loop()
        parked: asyncio.Future[None] = loop.create_future()
        self._parked_failures.add(parked)
        self._signal_failure(failure)
        try:
            await parked
        finally:
            self._parked_failures.discard(parked)
        raise BackendFailure(_CANCELLED)

    def _signal_failure(self, failure: BridgeProtocolFailure) -> None:
        if self._failure is None:
            self._failure = failure
            self._publications.put_nowait(failure)
            self._failure_changed.set()
        self._entry_changed.set()

    def _finish_epoch_transition(self) -> None:
        transition = self._epoch_transition
        if transition is None or self._pending:
            return
        self._epoch_invocations = []
        self._epoch_sealed = False
        self._epoch_verified = False
        self._epoch_resolved = False
        self._epoch_transition = None
        transition.set_result(None)

    def _new_public_id(self) -> str:
        prefix = "toolu_" if self._dialect == "anthropic" else "call_"
        return prefix + secrets.token_urlsafe(18)

    def _valid_new_id(self, public_id: object) -> bool:
        if not isinstance(public_id, str) or _PUBLIC_ID.fullmatch(public_id) is None:
            return False
        prefix = "toolu_" if self._dialect == "anthropic" else "call_"
        return public_id.startswith(prefix) and public_id not in self._issued_ids

    @staticmethod
    def _sdk_name(public_name: str) -> str:
        return f"mcp__{_SERVER_NAME}__{public_name}"


def _plain_json_object(value: Mapping[str, object]) -> dict[str, Any]:
    plain = plain_json(cast(Mapping[str, JsonValue], value))
    if not isinstance(plain, dict):
        raise RequestValidationError("tools", "input schema must be an object")
    return plain


def _call_identity(call: ExpectedCall) -> tuple[str, str]:
    if isinstance(call, tuple):
        name, arguments = call
    else:
        name = call.name
        arguments = call.arguments
    frozen = freeze_json(arguments)
    if not isinstance(frozen, Mapping):
        raise BackendFailure(_PROTOCOL_FAILURE)
    return name, canonical_json(frozen)


__all__ = ["BridgeProtocolFailure", "ToolBridge", "ToolInvocation"]
