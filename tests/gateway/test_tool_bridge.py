from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from types import MappingProxyType
from typing import Any, cast

import pytest
from mcp.server import Server
from mcp.types import CallToolRequestParams, CallToolResult, ListToolsResult

from claude_sdk_proxy.domain import (
    BackendFailure,
    RequestValidationError,
    ToolCall,
    ToolDefinition,
    ToolResultBlock,
)
from claude_sdk_proxy.tool_bridge import ToolBridge


def echo_definition(
    schema: Mapping[str, object] | None = None, *, name: str = "echo"
) -> ToolDefinition:
    return ToolDefinition(
        name,
        "Repeat the provided value.",
        {"type": "object", "properties": {"v": {"type": "integer"}}}
        if schema is None
        else schema,
    )


def id_sequence(*values: str) -> Callable[[], str]:
    iterator = iter(values)
    return lambda: next(iterator)


def request_handler(bridge: ToolBridge, method: str) -> Callable[..., Any]:
    server = cast(Server[Any], bridge.server_config["instance"])
    entry = server.get_request_handler(method)
    assert entry is not None
    return entry.handler


async def list_tools(bridge: ToolBridge) -> ListToolsResult:
    handler = request_handler(bridge, "tools/list")
    return cast(ListToolsResult, await handler(None, None))


async def call_tool(
    bridge: ToolBridge, name: str, arguments: dict[str, object] | None
) -> CallToolResult:
    handler = request_handler(bridge, "tools/call")
    params = CallToolRequestParams(name=name, arguments=arguments)
    return cast(CallToolResult, await handler(None, params))


async def cancel_and_collect(
    bridge: ToolBridge, tasks: Iterable[asyncio.Task[CallToolResult]]
) -> None:
    bridge.cancel()
    for task in tasks:
        with pytest.raises(BackendFailure):
            await task


@pytest.mark.anyio
async def test_low_level_server_advertises_exact_caller_schemas_and_metadata() -> None:
    definitions = (
        echo_definition({"type": "object"}, name="object_only"),
        echo_definition(
            {
                "$defs": {"value": {"type": "integer"}},
                "allOf": [{"$ref": "#/$defs/value"}],
            },
            name="referenced",
        ),
        echo_definition(
            {"type": "object", "properties": {"text": {"type": "string"}}},
            name="ordinary",
        ),
    )
    bridge = ToolBridge(definitions, dialect="anthropic")

    result = await list_tools(bridge)

    assert [tool.name for tool in result.tools] == [
        "object_only",
        "ordinary",
        "referenced",
    ]
    assert {tool.name: tool.input_schema for tool in result.tools} == {
        definition.name: dict_from_frozen(definition.input_schema)
        for definition in definitions
    }
    assert all(
        tool.meta == {"anthropic/maxResultSizeChars": 262_144}
        for tool in result.tools
    )


def dict_from_frozen(value: Mapping[str, object]) -> dict[str, object]:
    def thaw(item: object) -> object:
        if isinstance(item, Mapping):
            return {key: thaw(child) for key, child in item.items()}
        if isinstance(item, tuple):
            return [thaw(child) for child in item]
        return item

    return cast(dict[str, object], thaw(value))


def test_bridge_exposes_one_isolated_server_and_generated_allowlist() -> None:
    bridge = ToolBridge(
        (echo_definition(name="zeta"), echo_definition(name="alpha")),
        dialect="openai",
    )

    assert bridge.server_config["type"] == "sdk"
    assert bridge.server_config["name"] == "caller_tools_v1"
    assert isinstance(bridge.server_config["instance"], Server)
    assert bridge.allowed_tools == (
        "mcp__caller_tools_v1__alpha",
        "mcp__caller_tools_v1__zeta",
    )


@pytest.mark.anyio
async def test_one_handler_parks_until_its_result_is_resolved() -> None:
    bridge = ToolBridge(
        (echo_definition(),), dialect="anthropic", id_factory=id_sequence("toolu_a")
    )
    task = asyncio.create_task(call_tool(bridge, "echo", {"v": 7}))

    invocation = await bridge.next_invocation()
    assert invocation.public_id == "toolu_a"
    assert invocation.name == "echo"
    assert invocation.arguments == {"v": 7}
    assert isinstance(invocation.arguments, MappingProxyType)
    assert invocation.sdk_name == "mcp__caller_tools_v1__echo"
    assert not task.done()

    bridge.resolve((ToolResultBlock("toolu_a", ("seven",), False),))
    result = await task
    assert [block.text for block in result.content] == ["seven"]
    assert result.is_error is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("content", "is_error"),
    [((), False), (("failed",), True)],
)
async def test_result_content_and_error_flag_map_exactly_to_mcp(
    content: tuple[str, ...], is_error: bool
) -> None:
    bridge = ToolBridge(
        (echo_definition(),), dialect="openai", id_factory=id_sequence("call_a")
    )
    task = asyncio.create_task(bridge.call_tool("echo", {"v": 1}))
    invocation = await bridge.next_invocation()

    bridge.resolve((ToolResultBlock(invocation.public_id, content, is_error),))

    result = await task
    assert [block.text for block in result.content] == list(content)
    assert result.is_error is is_error


@pytest.mark.anyio
async def test_empty_error_is_rejected_without_releasing_handler() -> None:
    bridge = ToolBridge(
        (echo_definition(),), dialect="openai", id_factory=id_sequence("call_a")
    )
    task = asyncio.create_task(bridge.call_tool("echo", {"v": 1}))
    invocation = await bridge.next_invocation()

    with pytest.raises(RequestValidationError):
        bridge.resolve((ToolResultBlock(invocation.public_id, (), True),))
    assert not task.done()

    bridge.resolve((ToolResultBlock(invocation.public_id, ("failed",), True),))
    assert (await task).is_error is True


@pytest.mark.anyio
async def test_near_limit_unicode_result_is_delivered_intact() -> None:
    bridge = ToolBridge(
        (echo_definition(),), dialect="anthropic", id_factory=id_sequence("toolu_a")
    )
    task = asyncio.create_task(bridge.call_tool("echo", {"v": 1}))
    invocation = await bridge.next_invocation()
    text = "☃" * 87_381

    bridge.resolve((ToolResultBlock(invocation.public_id, (text,), False),))

    result = await task
    assert [block.text for block in result.content] == [text]


@pytest.mark.anyio
async def test_distinct_parallel_handlers_publish_and_resolve_independently() -> None:
    bridge = ToolBridge(
        (echo_definition(),),
        dialect="anthropic",
        id_factory=id_sequence("toolu_a", "toolu_b"),
    )
    first_task = asyncio.create_task(bridge.call_tool("echo", {"v": 1}))
    first = await bridge.next_invocation()
    second_task = asyncio.create_task(bridge.call_tool("echo", {"v": 2}))
    second = await bridge.next_invocation()

    bridge.resolve(
        (
            ToolResultBlock(first.public_id, ("one",), False),
            ToolResultBlock(second.public_id, ("two",), False),
        )
    )

    assert [block.text for block in (await first_task).content] == ["one"]
    assert [block.text for block in (await second_task).content] == ["two"]


@pytest.mark.anyio
async def test_identical_handlers_resolve_by_public_id_in_reverse() -> None:
    bridge = ToolBridge(
        (echo_definition(),),
        dialect="openai",
        id_factory=id_sequence("call_a", "call_b"),
    )
    first_task = asyncio.create_task(bridge.call_tool("echo", {"v": 1}))
    first = await bridge.next_invocation()
    second_task = asyncio.create_task(bridge.call_tool("echo", {"v": 1}))
    second = await bridge.next_invocation()

    bridge.resolve(
        (
            ToolResultBlock(second.public_id, ("second",), False),
            ToolResultBlock(first.public_id, ("first",), False),
        )
    )

    first_result, second_result = await first_task, await second_task
    assert [block.text for block in first_result.content] == ["first"]
    assert [block.text for block in second_result.content] == ["second"]
    assert first_result.is_error is False
    assert second_result.is_error is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    "bad_results",
    [
        (ToolResultBlock("call_unknown", ("x",), False),),
        (
            ToolResultBlock("call_a", ("x",), False),
            ToolResultBlock("call_a", ("y",), False),
        ),
        (ToolResultBlock("call_a", ("x",), False),),
    ],
)
async def test_bad_result_batch_has_no_partial_effect(
    bad_results: tuple[ToolResultBlock, ...],
) -> None:
    bridge = ToolBridge(
        (echo_definition(),),
        dialect="openai",
        id_factory=id_sequence("call_a", "call_b"),
    )
    first_task = asyncio.create_task(bridge.call_tool("echo", {"v": 1}))
    first = await bridge.next_invocation()
    second_task = asyncio.create_task(bridge.call_tool("echo", {"v": 2}))
    second = await bridge.next_invocation()

    with pytest.raises(RequestValidationError):
        bridge.resolve(bad_results)
    assert not first_task.done()
    assert not second_task.done()

    bridge.resolve(
        (
            ToolResultBlock(first.public_id, ("one",), False),
            ToolResultBlock(second.public_id, ("two",), False),
        )
    )
    assert [block.text for block in (await first_task).content] == ["one"]
    assert [block.text for block in (await second_task).content] == ["two"]


@pytest.mark.anyio
async def test_cancel_fails_pending_handlers_and_prevents_new_publication() -> None:
    bridge = ToolBridge(
        (echo_definition(),), dialect="openai", id_factory=id_sequence("call_a")
    )
    task = asyncio.create_task(bridge.call_tool("echo", {"v": 1}))
    await bridge.next_invocation()

    await cancel_and_collect(bridge, (task,))
    with pytest.raises(BackendFailure):
        await bridge.call_tool("echo", {"v": 2})


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [("missing", {"v": 1}), ("echo", {"v": "wrong"}), ("echo", [])],
)
async def test_invalid_callback_is_fatal_and_never_becomes_an_invocation(
    name: str, arguments: object
) -> None:
    bridge = ToolBridge((echo_definition(),), dialect="openai")
    task = asyncio.create_task(bridge.call_tool(name, arguments))

    with pytest.raises(BackendFailure, match="SDK tool protocol failure"):
        await bridge.next_invocation()
    with pytest.raises(BackendFailure, match="SDK tool protocol failure"):
        await bridge.wait_failure()
    assert not task.done()

    await cancel_and_collect(bridge, (task,))


@pytest.mark.anyio
async def test_oversized_generated_arguments_are_fatal_before_id_minting() -> None:
    id_factory_called = False

    def id_factory() -> str:
        nonlocal id_factory_called
        id_factory_called = True
        return "call_a"

    bridge = ToolBridge(
        (
            echo_definition(
                {
                    "type": "object",
                    "properties": {"v": {"type": "string"}},
                    "required": ["v"],
                }
            ),
        ),
        dialect="openai",
        id_factory=id_factory,
    )
    task = asyncio.create_task(bridge.call_tool("echo", {"v": "x" * 262_144}))

    with pytest.raises(BackendFailure):
        await bridge.next_invocation()
    assert id_factory_called is False
    await cancel_and_collect(bridge, (task,))


@pytest.mark.anyio
async def test_seal_epoch_returns_exact_entries_in_handler_order() -> None:
    bridge = ToolBridge(
        (echo_definition(),),
        dialect="openai",
        id_factory=id_sequence("call_a", "call_b"),
    )
    first_task = asyncio.create_task(bridge.call_tool("echo", {"v": 1}))
    first = await bridge.next_invocation()
    second_task = asyncio.create_task(bridge.call_tool("echo", {"v": 2}))
    second = await bridge.next_invocation()

    sealed = await bridge.seal_epoch(
        (ToolCall("raw_2", "echo", {"v": 2}), ToolCall("raw_1", "echo", {"v": 1}))
    )

    assert sealed == (first, second)
    bridge.resolve(
        (
            ToolResultBlock(first.public_id, ("one",), False),
            ToolResultBlock(second.public_id, ("two",), False),
        )
    )
    await first_task
    await second_task


@pytest.mark.anyio
async def test_seal_epoch_rejects_an_already_present_extra_callback() -> None:
    bridge = ToolBridge(
        (echo_definition(),),
        dialect="openai",
        id_factory=id_sequence("call_a", "call_b"),
    )
    first_task = asyncio.create_task(bridge.call_tool("echo", {"v": 1}))
    await bridge.next_invocation()
    second_task = asyncio.create_task(bridge.call_tool("echo", {"v": 2}))
    await bridge.next_invocation()

    with pytest.raises(BackendFailure, match="SDK tool protocol failure"):
        await bridge.seal_epoch((ToolCall("raw", "echo", {"v": 1}),))
    with pytest.raises(BackendFailure):
        await bridge.wait_failure()

    await cancel_and_collect(bridge, (first_task, second_task))


@pytest.mark.anyio
async def test_callback_entering_after_expected_epoch_is_sealed_is_fatal() -> None:
    bridge = ToolBridge((echo_definition(),), dialect="openai")
    release_late_callback = asyncio.Event()

    async def late_callback() -> CallToolResult:
        await release_late_callback.wait()
        return await bridge.call_tool("echo", {"v": 1})

    task = asyncio.create_task(late_callback())
    assert await bridge.seal_epoch(()) == ()

    release_late_callback.set()
    with pytest.raises(BackendFailure, match="SDK tool protocol failure"):
        await bridge.wait_failure()
    assert not task.done()

    await cancel_and_collect(bridge, (task,))
