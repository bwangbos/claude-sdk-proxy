from __future__ import annotations

import asyncio
import json
import re
import secrets
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
    ImageContent,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

from claude_sdk_proxy.domain import (
    BackendFailure,
    Dialect,
    ImageBlock,
    RequestValidationError,
    ToolCall,
    ToolDefinition,
    ToolResultBlock,
)
from claude_sdk_proxy.images import ResultIdentity, result_identity
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
    internal_id: str
    future: asyncio.Future[ToolResultBlock]
    callback_entered: bool
    delivered: bool = False


type ExpectedCall = ToolCall | tuple[str, str, Mapping[str, object]]


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
        self._pending_by_internal: dict[str, _PendingInvocation] = {}
        self._parked_failures: set[asyncio.Future[None]] = set()
        self._entry_changed = asyncio.Event()
        self._failure_changed = asyncio.Event()
        self._failure: BridgeProtocolFailure | None = None
        self._cancelled = False
        self._epoch_sealed = False
        self._epoch_verified = False
        self._epoch_resolved = False
        self._epoch_invocations: list[ToolInvocation] = []
        self._expected_by_internal: dict[str, tuple[str, str]] = {}
        self._callbacks_seen: set[str] = set()

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
            meta = params.meta
            internal_id = (
                meta.get("claudecode/toolUseId") if isinstance(meta, Mapping) else None
            )
            return await self.invoke(
                params.name, params.arguments or {}, internal_id=internal_id
            )

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

    async def call_tool(
        self, name: str, arguments: object, *, internal_id: object
    ) -> CallToolResult:
        """Exercise the same bridge path as the registered low-level callback."""
        return await self.invoke(name, arguments, internal_id=internal_id)

    async def invoke(
        self, name: str, arguments: object, *, internal_id: object
    ) -> CallToolResult:
        """Publish one SDK handler entry and wait for its caller-owned result."""
        if self._cancelled:
            raise BackendFailure(_CANCELLED)
        if not isinstance(internal_id, str) or not internal_id:
            return await self._park_protocol_failure(
                BridgeProtocolFailure("SDK tool callback metadata was missing")
            )

        definition = self._definitions_by_name.get(name)
        if definition is None:
            return await self._park_protocol_failure(
                BridgeProtocolFailure("SDK invoked an unknown caller tool")
            )

        try:
            frozen_arguments = validate_tool_arguments(arguments)
            self._validators[name].validate(plain_json(frozen_arguments))
        except RequestValidationError, ValidationError:
            return await self._park_protocol_failure(
                BridgeProtocolFailure("SDK supplied invalid caller-tool arguments")
            )

        identity = (name, canonical_json(frozen_arguments))
        if self._epoch_sealed:
            pending = self._pending_by_internal.get(internal_id)
            if (
                pending is None
                or pending.callback_entered
                or self._expected_by_internal.get(internal_id) != identity
            ):
                return await self._park_protocol_failure(
                    BridgeProtocolFailure(
                        "SDK callback did not match the sealed raw tool call"
                    )
                )
            pending.callback_entered = True
            self._callbacks_seen.add(internal_id)
            return await self._await_result(pending)

        if internal_id in self._callbacks_seen:
            return await self._park_protocol_failure(
                BridgeProtocolFailure("SDK repeated a tool callback metadata ID")
            )

        try:
            pending = self._new_pending(
                internal_id, name, frozen_arguments, callback_entered=True
            )
        except BridgeProtocolFailure as failure:
            return await self._park_protocol_failure(failure)
        self._callbacks_seen.add(internal_id)
        self._epoch_invocations.append(pending.invocation)
        self._entry_changed.set()
        return await self._await_result(pending)

    async def seal_epoch(
        self, expected_calls: Iterable[ExpectedCall]
    ) -> tuple[ToolInvocation, ...]:
        """Close the current callback epoch after its exact raw set has entered."""
        expected = tuple(_call_identity(call) for call in expected_calls)
        # Admit callbacks already scheduled by the typed assistant message before
        # freezing the raw-call bijection. Later serial callbacks use placeholders.
        await asyncio.sleep(0)
        if self._cancelled:
            raise BackendFailure(_CANCELLED)
        if self._failure is not None:
            raise BackendFailure(_PROTOCOL_FAILURE)
        if self._epoch_sealed:
            self._signal_failure(
                BridgeProtocolFailure("tool callback epoch was sealed twice")
            )
            raise BackendFailure(_PROTOCOL_FAILURE)

        expected_ids = [internal_id for internal_id, _, _ in expected]
        if any(not internal_id for internal_id in expected_ids) or len(
            set(expected_ids)
        ) != len(expected_ids):
            self._signal_failure(
                BridgeProtocolFailure("raw tool call metadata IDs were invalid")
            )
            raise BackendFailure(_PROTOCOL_FAILURE)
        self._expected_by_internal = {
            internal_id: (name, arguments) for internal_id, name, arguments in expected
        }
        if not self._callbacks_seen.issubset(self._expected_by_internal):
            self._signal_failure(
                BridgeProtocolFailure("SDK callback metadata ID was unknown")
            )
            raise BackendFailure(_PROTOCOL_FAILURE)

        for internal_id, name, arguments_json in expected:
            pending = self._pending_by_internal.get(internal_id)
            if pending is None:
                definition = self._definitions_by_name.get(name)
                if definition is None:
                    self._signal_failure(
                        BridgeProtocolFailure("raw tool call name was unknown")
                    )
                    raise BackendFailure(_PROTOCOL_FAILURE)
                arguments = freeze_json(json.loads(arguments_json))
                assert isinstance(arguments, Mapping)
                try:
                    pending = self._new_pending(
                        internal_id,
                        definition.name,
                        arguments,
                        callback_entered=False,
                    )
                except BridgeProtocolFailure as failure:
                    self._signal_failure(failure)
                    raise BackendFailure(_PROTOCOL_FAILURE) from None
                self._epoch_invocations.append(pending.invocation)
            elif (
                pending.invocation.name,
                canonical_json(pending.invocation.arguments),
            ) != (name, arguments_json):
                self._signal_failure(
                    BridgeProtocolFailure("SDK tool callback did not match raw call")
                )
                raise BackendFailure(_PROTOCOL_FAILURE)

        self._epoch_sealed = True
        self._epoch_verified = True
        # Callback arrival order need not match native content-block order.
        # Publish the validated bijection in raw order so reasoning can remain
        # interleaved with the corresponding public tool calls.
        return tuple(
            self._pending_by_internal[internal_id].invocation
            for internal_id in expected_ids
        )

    async def wait_epoch_complete(self) -> None:
        """Wait until every sealed raw call has returned through its callback."""
        if self._cancelled:
            raise BackendFailure(_CANCELLED)
        if self._failure is not None:
            raise BackendFailure(_PROTOCOL_FAILURE)
        if (
            not self._epoch_sealed
            or not self._epoch_verified
            or not self._epoch_resolved
            or any(not pending.delivered for pending in self._pending.values())
        ):
            self._signal_failure(
                BridgeProtocolFailure("tool callback completion wait was premature")
            )
            raise BackendFailure(_PROTOCOL_FAILURE)

        while self._pending:
            self._entry_changed.clear()
            if self._pending:
                await self._entry_changed.wait()
            if self._cancelled:
                raise BackendFailure(_CANCELLED)
            if self._failure is not None:
                raise BackendFailure(_PROTOCOL_FAILURE)

        if self._callbacks_seen != set(self._expected_by_internal):
            self._signal_failure(
                BridgeProtocolFailure("sealed raw tool callback was not delivered")
            )
            raise BackendFailure(_PROTOCOL_FAILURE)
        self._epoch_invocations.clear()
        self._expected_by_internal.clear()
        self._callbacks_seen.clear()

    async def begin_epoch(self) -> None:
        """Open admission only when a subsequent tool boundary begins."""
        await self.wait_epoch_complete()
        self._epoch_invocations = []
        self._epoch_sealed = False
        self._epoch_verified = False
        self._epoch_resolved = False
        self._expected_by_internal = {}
        self._callbacks_seen = set()

    def resolve(
        self, results: Iterable[ToolResultBlock]
    ) -> dict[str, tuple[ResultIdentity, bool]]:
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
        expected_echoes: dict[str, tuple[ResultIdentity, bool]] = {}
        for pending in pending_batch:
            pending.delivered = True
            result = by_id[pending.invocation.public_id]
            pending.future.set_result(result)
            expected_echoes[pending.internal_id] = (
                result_identity(result.content),
                result.is_error,
            )
        return expected_echoes

    def cancel(self) -> None:
        """Fail all parked callbacks and permanently close admission."""
        if self._cancelled:
            return
        self._cancelled = True
        failure = BackendFailure(_CANCELLED)
        for pending in tuple(self._pending.values()):
            if not pending.future.done():
                if pending.callback_entered:
                    pending.future.set_exception(failure)
                else:
                    pending.future.cancel()
        for parked in tuple(self._parked_failures):
            if not parked.done():
                parked.set_exception(BackendFailure(_CANCELLED))
        self._pending.clear()
        self._pending_by_internal.clear()
        self._parked_failures.clear()
        self._epoch_invocations.clear()
        self._expected_by_internal.clear()
        self._callbacks_seen.clear()
        self._issued_ids.clear()
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
            self._failure_changed.set()
        self._entry_changed.set()

    def _new_pending(
        self,
        internal_id: str,
        name: str,
        arguments: Mapping[str, JsonValue],
        *,
        callback_entered: bool,
    ) -> _PendingInvocation:
        try:
            public_id = self._id_factory()
        except Exception:
            raise BridgeProtocolFailure("public tool ID generation failed") from None
        if not self._valid_new_id(public_id):
            raise BridgeProtocolFailure("public tool ID generation was invalid")
        invocation = ToolInvocation(
            public_id=public_id,
            name=name,
            arguments=arguments,
            sdk_name=self._sdk_name(name),
        )
        pending = _PendingInvocation(
            invocation,
            internal_id,
            asyncio.get_running_loop().create_future(),
            callback_entered,
        )
        self._issued_ids.add(public_id)
        self._pending[public_id] = pending
        self._pending_by_internal[internal_id] = pending
        return pending

    async def _await_result(self, pending: _PendingInvocation) -> CallToolResult:
        try:
            result = await pending.future
            return CallToolResult(
                content=[
                    ImageContent(
                        type="image", mime_type=text.media_type, data=text.data
                    )
                    if isinstance(text, ImageBlock)
                    else TextContent(type="text", text=text)
                    for text in result.content
                ],
                is_error=result.is_error,
            )
        finally:
            public_id = pending.invocation.public_id
            if self._pending.get(public_id) is pending:
                del self._pending[public_id]
                self._pending_by_internal.pop(pending.internal_id, None)
                self._entry_changed.set()

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


def _call_identity(call: ExpectedCall) -> tuple[str, str, str]:
    if isinstance(call, tuple):
        internal_id, name, arguments = call
    else:
        internal_id = call.id
        name = call.name
        arguments = call.arguments
    frozen = freeze_json(arguments)
    if not isinstance(frozen, Mapping):
        raise BackendFailure(_PROTOCOL_FAILURE)
    return internal_id, name, canonical_json(frozen)


__all__ = ["BridgeProtocolFailure", "ToolBridge", "ToolInvocation"]
