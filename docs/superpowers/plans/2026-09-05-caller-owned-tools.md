# Caller-Owned Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add native caller-owned tool loops to both compatibility endpoints so an ordinary agent harness can define tools, receive uniquely identified calls, execute them, and return results while the authenticated Claude Agent SDK operation remains parked in memory.

**Architecture:** Both dialect adapters normalize tool definitions and transcript blocks into one immutable model. A per-conversation `ToolSessionActor` owns the sole long-running SDK receive iterator; an in-process MCP `ToolBridge` records one independently identified invocation per handler coroutine, seals each raw boundary against exact private callback metadata, and accepts caller results by opaque public ID. The existing registry still performs append-only transcript matching, single-flight admission, exact replay, and bounded teardown, extended with a non-evictable waiting-for-results state.

**Tech Stack:** Python 3.14, `claude-agent-sdk==0.2.152`, Starlette 1.x, Uvicorn 0.x, Anthropic and OpenAI Python clients in development tests, pytest, pytest-anyio, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-09-05-caller-owned-tools-design.md`

## Global Constraints

- Preserve every existing text-only behavior and test. A fresh request with omitted tools or `tools: []` must continue to use the empty-MCP text path.
- Do not replace the proven text-only `Conversation`/`TurnLease` execution path with the background actor. Add `ToolSessionActor` only for conversations whose frozen definition tuple is non-empty; the registry may share indexing/fingerprint/capacity helpers across both variants.
- The caller owns execution, approval, retry, and side effects. Production code must never dispatch an external operation on behalf of a tool call.
- Expose only the generated in-process MCP server and generated caller-tool allowlist. Keep built-ins, ambient MCP, settings, skills, agents, plugins, auto-memory, slash commands, and session persistence disabled.
- Do not add prose, JSON-output instructions, or synthetic tool results to caller system/user text. The only additional model-visible data is the SDK-native MCP schema and Anthropic's provider-owned tool instructions.
- Freeze dialect, model, system prompt, and the non-empty canonical tool-definition tuple for the lifetime of a tool-enabled conversation.
- Correlate caller results only by an opaque public ID minted per invocation. The narrow internal exception is the callback's private `_meta["claudecode/toolUseId"]`, which must form a bijection with raw/typed SDK IDs and must match the exact name and canonical arguments. Never expose or accept that SDK ID publicly, and never fall back to name, arguments, order, guessing, or a `(name, arguments)` lookup.
- Buffer tool calls until a complete validated SDK `message_stop`. Seal the raw IDs against already-entered callbacks and create placeholders for SDK-serialized deferred callbacks. After result echo, require every placeholder callback to enter and return its stored result before accepting a later SDK item; terminal text does not reopen the sealed epoch.
- Accept all and only the pending result IDs, exactly once and in any order. Invalid or partial result submissions return HTTP 400 without consuming the wait or resetting its deadline.
- Keep waiting sessions non-evictable and within `--max-sessions`. Normal completed sessions remain LRU-evictable.
- Commit and cache each public boundary before it can be observed as complete. A duplicate completed request replays the same text, calls, IDs, stop reason, and usage without re-entering the SDK or handler.
- Use the existing 300-second generation timeout and add a separate 300-second default tool-result timeout that resets only after a newly committed tool boundary.
- Before a boundary commits, cancellation/disconnect invalidates the session. After a tool boundary commits, disconnect leaves the SDK parked. After continuation results commit, background generation finishes into replay even if that HTTP client disconnects.
- Keep tool limits exactly as specified: 128 definitions; 8 KiB per description; 64 KiB per canonical schema and 512 KiB aggregate; 64 nested JSON mapping/array containers for schemas and arguments, counting the root as depth 1; 256 KiB per generated argument object; 256 KiB per result; 1 MiB aggregate results; 8 MiB SDK buffer. The buffer accounts for worst-case sixfold JSON escaping plus the SDK envelope; it does not enlarge a public limit.
- Keep production modules auditable. If `app.py`, either dialect adapter, `sessions.py`, or `session_turn.py` would exceed 300 lines, extract narrowly named parser, renderer, or actor helpers instead of creating a monolith.
- Every task follows red-green-refactor, runs the focused checks shown, and commits only its own coherent change. Do not weaken an existing fail-closed test to make tool traffic pass.

---

### Task 1: Define the canonical tool contract and bounded normalization

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/claude_sdk_proxy/domain.py`
- Create: `src/claude_sdk_proxy/tool_contract.py`
- Create: `tests/gateway/test_tool_contract.py`
- Modify: `tests/reset/test_domain.py`

**Interfaces:**
- Produces immutable `TextBlock`, `ToolCallBlock`, `ToolResultBlock`, `ToolDefinition`, and `CanonicalMessage` values and extends the existing `TextRequest` instead of introducing a parallel request type.
- `TextRequest` retains its existing fields and adds `dialect: Literal["anthropic", "openai"]` plus `tools: tuple[ToolDefinition, ...]`, both with text-compatible defaults until the adapters set them explicitly.
- `TextRequest.next_input` is either the final user text string or the complete tuple of `ToolResultBlock`; mixed user text/results is invalid. Definition tuples normalize by tool name and result-only turns normalize by public call ID, making definition/result order semantically irrelevant.
- Produces `canonical_json(value) -> str`, `validate_tool_definitions(...)`, `validate_tool_arguments(...)`, and `validate_tool_results(...)` with the exact spec size and 64-container depth limits.
- Adds `ToolCall` to `ConversationEvent`; `Completed("tool_use", usage)` is the public tool boundary.

- [ ] **Step 1: Write failing canonical-model tests**

Cover immutable block tuples, text-only backward construction helpers, alternating canonical turns, a final result-only user turn, mixed text/results, duplicate result IDs, malformed JSON Schemas, and values that are not recursively valid JSON. Accept nested self-contained `$ref`/`$dynamicRef`/`$recursiveRef` fragments with `$defs`/`allOf`, JSON Pointers, local anchors, and nested resource IDs; pre-resolve every schema-position reference with retrieval disabled. Reject external/relative and missing references without attempting network or filesystem resolution. Prove schema-unaware lookalikes such as `{"const":{"$ref":"literal"}}` and a property literally named `$ref` are not treated as reference keywords. Mutate the caller's original nested schema/argument dictionaries after construction and assert the canonical values do not change; assert direct mutation at every nested stored level fails. Keep caller-controlled parse/size failures as `RequestValidationError`; model-generated argument failures are mapped to `BackendFailure` by the SDK protocol layer in Task 5.

```python
def test_gateway_request_accepts_complete_result_turn_in_any_order() -> None:
    request = TextRequest(
        dialect="openai",
        model="sonnet",
        system="",
        messages=(
            CanonicalMessage.user_text("use both"),
            CanonicalMessage(
                "assistant",
                (
                    ToolCallBlock("call_a", "echo", {"value": "same"}),
                    ToolCallBlock("call_b", "echo", {"value": "same"}),
                ),
            ),
            CanonicalMessage(
                "user",
                (
                    ToolResultBlock("call_b", ("second",), False),
                    ToolResultBlock("call_a", ("first",), False),
                ),
            ),
        ),
        tools=(echo_definition(),),
        max_tokens=1024,
        stream=True,
    )

    assert {item.tool_call_id: item.content for item in request.next_input} == {
        "call_a": ("first",),
        "call_b": ("second",),
    }


def test_tool_definition_limits_are_measured_after_canonical_json() -> None:
    oversized = ToolDefinition("echo", "", {"type": "object", "x": "x" * 65536})
    with pytest.raises(RequestValidationError) as error:
        validate_tool_definitions((oversized,))
    assert error.value.field == "tools"
```

- [ ] **Step 2: Run the focused tests and confirm RED**

```bash
.venv/bin/pytest -q tests/gateway/test_tool_contract.py tests/reset/test_domain.py
```

Expected: collection fails because the new block types, extended `TextRequest`, and validation functions do not exist.

- [ ] **Step 3: Pin the directly imported JSON Schema validator**

Add `jsonschema==4.26.0` and `referencing==0.37.0` as direct runtime dependencies because gateway request validation imports both, then regenerate the lock without upgrading unrelated packages:

```bash
UV_CACHE_DIR=/private/tmp/claude-sdk-proxy-uv-cache uv lock
```

- [ ] **Step 4: Implement immutable blocks and request invariants**

Use these public shapes; copy incoming dictionaries before storing them and expose read-only mappings so a request fingerprint cannot mutate after admission.

```python
@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str


@dataclass(frozen=True, slots=True)
class ToolCallBlock:
    id: str
    name: str
    arguments: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    tool_call_id: str
    content: tuple[str, ...]
    is_error: bool


@dataclass(frozen=True, slots=True, init=False)
class CanonicalMessage:
    role: Role
    blocks: tuple[TextBlock | ToolCallBlock | ToolResultBlock, ...]

    def __init__(self, role: Role, content: str | tuple[CanonicalBlock, ...]) -> None:
        normalized = (TextBlock(content),) if isinstance(content, str) else content
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "blocks", normalized)

    @property
    def content(self) -> str:
        return self.require_text()


@dataclass(frozen=True, slots=True)
class TextRequest:
    model: str
    system: str
    messages: tuple[CanonicalMessage, ...]
    max_tokens: int | None
    stream: bool
    include_usage: bool = False
    dialect: Dialect = "anthropic"
    tools: tuple[ToolDefinition, ...] = ()
```

The compatibility constructor and `content` property above keep existing text-only call sites valid while storing only typed blocks. Add `CanonicalMessage.user_text(...)`, `assistant_text(...)`, and `require_text()` helpers. The new request fields come after every existing positional field, so this first commit does not silently reinterpret existing constructors.

- [ ] **Step 5: Implement bounded canonical JSON and tool validation**

In `tool_contract.py`, implement `freeze_json` that recursively copies mappings into `MappingProxyType` and arrays into tuples, plus `plain_json` that recursively thaws those values into dictionaries/lists. Both operations, `canonical_json`, and schema validation enforce one 64-container nesting limit before recursive library exhaustion; map defensive `RecursionError` only at those public boundaries. Make `canonical_json` call `plain_json` before encoding with `ensure_ascii=False`, sorted keys, compact separators, and `allow_nan=False`; a shallow `dict(...)` conversion is insufficient. Select the validator dialect, call `check_schema`, and create a `referencing.Resource` with Draft 2020-12 as the default only when `$schema` is absent. Put it in a `referencing.Registry` whose retrieval callback always fails, crawl registered subresources/anchors, and recursively walk `Resource.subresources()` with `resolver.in_subresource(...)`. Only at those schema locations, resolve `$ref`, `$dynamicRef`, and `$recursiveRef`; reject any non-fragment URI or failed lookup as caller-controlled HTTP 400. This schema-aware walk must ignore identical keys inside `const`, `enum`, defaults, examples, and property names. Reject non-string keys, NaN/infinity, duplicate names, non-object schemas/arguments, invalid names, non-Boolean error flags, invalid IDs, and every size/count/depth violation with `RequestValidationError` pointing to `tools` or `messages`. Empty successful result strings and empty successful text-block arrays are valid zero-byte results; reject `is_error=True` when the concatenated result text is empty because the backend rejects that shape. Return definitions sorted by name and result-only blocks sorted by public ID after validation; do not reorder assistant calls, whose stable order comes from handler invocation.

```python
def canonical_json(value: JsonValue) -> str:
    try:
        return json.dumps(
            plain_json(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise RequestValidationError("tools", "must contain JSON values") from None
```

- [ ] **Step 6: Run focused tests and static checks; confirm GREEN**

```bash
.venv/bin/pytest -q tests/gateway/test_tool_contract.py tests/reset tests/gateway/test_sessions.py
.venv/bin/ruff check src/claude_sdk_proxy/domain.py src/claude_sdk_proxy/tool_contract.py tests/gateway/test_tool_contract.py
.venv/bin/mypy src/claude_sdk_proxy/domain.py src/claude_sdk_proxy/tool_contract.py
```

- [ ] **Step 7: Commit Task 1**

```bash
git add pyproject.toml uv.lock src/claude_sdk_proxy/domain.py src/claude_sdk_proxy/tool_contract.py tests/gateway/test_tool_contract.py tests/reset/test_domain.py
git commit -m "feat: define canonical caller tool contract"
```

---

### Task 2: Parse and render the Anthropic Messages tool subset

**Files:**
- Modify: `src/claude_sdk_proxy/anthropic_api.py`
- Create: `src/claude_sdk_proxy/anthropic_tools.py`
- Modify: `tests/gateway/test_anthropic_api.py`

**Interfaces:**
- `parse_anthropic_request(...) -> TextRequest` accepts native definitions, auto choice, prior `tool_use`, and result-only `tool_result` turns.
- `render_anthropic_response(...)` renders ordered text and call blocks.
- `encode_anthropic_start(...)` preserves the existing eager text-block start for `tools=()`; tool-enabled responses use a render-state flag to start blocks lazily as each `TextDelta` run or complete `ToolCall` arrives.
- Anthropic public IDs use `toolu_` plus an opaque URL-safe suffix.
- Until Task 7 switches `app.py` to block-aware assembly, response functions accept their existing text-only arguments as a compatibility form and normalize them to one `TextBlock` internally.

- [ ] **Step 1: Write failing Anthropic parser tests**

Add table-driven cases for valid auto choice, empty fresh tools, multiple prior calls/results, result text as a string or text-block array, empty-string and empty text-block-array successful results, non-empty `is_error`, empty error rejection, duplicate names/IDs, incomplete results, mixed user text/results, named/none/disabled/serial choices, malformed schema, and multimodal result blocks.

```python
def test_anthropic_parser_normalizes_parallel_calls_and_reverse_results() -> None:
    request = parse_anthropic_request(
        {
            "model": "sonnet",
            "max_tokens": 1024,
            "tools": [anthropic_echo_tool()],
            "tool_choice": {"type": "auto", "disable_parallel_tool_use": False},
            "messages": [
                {"role": "user", "content": "twice"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "toolu_a", "name": "echo", "input": {"v": 1}},
                        {"type": "tool_use", "id": "toolu_b", "name": "echo", "input": {"v": 1}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_b", "content": "B"},
                        {"type": "tool_result", "tool_use_id": "toolu_a", "content": [{"type": "text", "text": "A"}]},
                    ],
                },
            ],
        },
        frozenset({"sonnet"}),
    )

    assert {item.tool_call_id: item.content for item in request.next_input} == {
        "toolu_a": ("A",),
        "toolu_b": ("B",),
    }
```

- [ ] **Step 2: Write failing Anthropic renderer tests**

Assert exact nonstream JSON and exact SSE order for text-only, call-only, and text-plus-two-call responses. Require one complete `input_json_delta` per call and `stop_reason: "tool_use"`.

```python
def test_anthropic_stream_renders_complete_tool_argument_delta() -> None:
    chunks = encode_anthropic_event(
        "msg_1",
        "sonnet",
        ToolCall("toolu_1", "echo", {"snowman": "☃"}),
        block_index=1,
    )
    assert [event_name(chunk) for chunk in chunks] == [
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
    ]
    assert payload(chunks[1])["delta"]["partial_json"] == '{"snowman":"☃"}'
```

- [ ] **Step 3: Run the focused tests and confirm RED**

```bash
.venv/bin/pytest -q tests/gateway/test_anthropic_api.py
```

Expected: the parser still rejects `tools`, and the renderer cannot encode `ToolCall`.

- [ ] **Step 4: Implement strict Anthropic normalization**

Move tool-specific parsing into `anthropic_tools.py`. Preserve block order and exact text. Permit text blocks in ordinary user/assistant turns, but require a pending continuation's final user turn to contain results only. Validate `tool_choice` structurally and raise `UnsupportedFeature("tool_choice", ...)` for semantically unsupported choices.

- [ ] **Step 5: Implement block-aware Anthropic rendering**

Make the renderer consume an ordered tuple of response blocks rather than a concatenated string. Maintain a small stream render state that preserves the current eager index-zero text block for text-only requests, but for tool-enabled requests opens a text block only when the first text delta arrives, closes it before a call, assigns monotonically increasing content indexes, and closes any open text block before `message_delta`/`message_stop`.

- [ ] **Step 6: Run focused tests and static checks; confirm GREEN**

```bash
.venv/bin/pytest -q tests/gateway/test_anthropic_api.py tests/gateway/test_tool_contract.py
.venv/bin/ruff check src/claude_sdk_proxy/anthropic_api.py src/claude_sdk_proxy/anthropic_tools.py tests/gateway/test_anthropic_api.py
.venv/bin/mypy src/claude_sdk_proxy/anthropic_api.py src/claude_sdk_proxy/anthropic_tools.py
```

- [ ] **Step 7: Commit Task 2**

```bash
git add src/claude_sdk_proxy/anthropic_api.py src/claude_sdk_proxy/anthropic_tools.py tests/gateway/test_anthropic_api.py
git commit -m "feat: add Anthropic tool wire protocol"
```

---

### Task 3: Parse and render the OpenAI Chat Completions tool subset

**Files:**
- Modify: `src/claude_sdk_proxy/openai_api.py`
- Create: `src/claude_sdk_proxy/openai_tools.py`
- Modify: `tests/gateway/test_openai_api.py`

**Interfaces:**
- `parse_openai_request(...) -> TextRequest` accepts function tools, auto choice, assistant `tool_calls`, and contiguous `role: "tool"` results.
- The parser coalesces contiguous OpenAI result messages into one canonical user result turn without changing public call IDs or result text.
- Nonstream responses use assistant `message.tool_calls`; streaming uses `delta.tool_calls` and `finish_reason: "tool_calls"`.
- OpenAI public IDs use `call_` plus an opaque URL-safe suffix.
- Until Task 7 switches `app.py` to block-aware assembly, response functions retain the existing text-only argument form and normalize it to one `TextBlock` internally.

- [ ] **Step 1: Write failing OpenAI parser tests**

Cover valid function definitions, empty fresh tools, omitted/auto choice, omitted/true parallel flag, nullable assistant content beside calls, contiguous reverse-order result messages, JSON-as-string results, duplicate/missing IDs, interleaved user content, non-function tools, forced/required/none/named choices, and `parallel_tool_calls: false`.

```python
def test_openai_parser_coalesces_reverse_order_tool_messages() -> None:
    request = parse_openai_request(
        {
            "model": "sonnet",
            "tools": [openai_echo_tool()],
            "parallel_tool_calls": True,
            "messages": [
                {"role": "user", "content": "twice"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        function_call("call_a", "echo", '{"v":1}'),
                        function_call("call_b", "echo", '{"v":1}'),
                    ],
                },
                {"role": "tool", "tool_call_id": "call_b", "content": "B"},
                {"role": "tool", "tool_call_id": "call_a", "content": "A"},
            ],
        },
        frozenset({"sonnet"}),
    )

    assert len(request.messages) == 3
    assert {item.tool_call_id: item.content for item in request.next_input} == {
        "call_a": ("A",),
        "call_b": ("B",),
    }
```

- [ ] **Step 2: Write failing OpenAI renderer tests**

Assert complete arguments are compact JSON strings, each streamed call has a stable index and ID, text remains ordered around calls, nonstream assistant content is `None` when absent, empty OpenAI result strings parse successfully, and finish reasons remain unchanged for text-only responses.

```python
def test_openai_stream_renders_two_identical_calls_with_distinct_ids() -> None:
    state = OpenAIStreamState()
    first = payload(encode_openai_event("chatcmpl_1", "sonnet", ToolCall("call_a", "echo", {"v": 1}), False, state)[0])
    second = payload(encode_openai_event("chatcmpl_1", "sonnet", ToolCall("call_b", "echo", {"v": 1}), False, state)[0])
    assert first["choices"][0]["delta"]["tool_calls"][0]["id"] == "call_a"
    assert second["choices"][0]["delta"]["tool_calls"][0]["id"] == "call_b"
    assert first["choices"][0]["delta"]["tool_calls"][0]["index"] == 0
    assert second["choices"][0]["delta"]["tool_calls"][0]["index"] == 1
```

- [ ] **Step 3: Run the focused tests and confirm RED**

```bash
.venv/bin/pytest -q tests/gateway/test_openai_api.py
```

Expected: the parser still rejects tool fields and the renderer has no `ToolCall` mapping.

- [ ] **Step 4: Implement strict OpenAI normalization**

Move tool-specific parsing into `openai_tools.py`. Parse `function.arguments` with `json.loads`, require an object, and re-encode canonically for fingerprints/rendering. Treat OpenAI's lack of an error flag as `ToolResultBlock(..., is_error=False)`; do not infer errors from result strings.

- [ ] **Step 5: Implement block-aware OpenAI rendering**

Emit each tool call in one `delta.tool_calls` chunk with a zero-based index. Make the terminal chunk use `tool_calls` whenever the canonical completion reason is `tool_use`; preserve the current usage chunk and `[DONE]` behavior.

- [ ] **Step 6: Run focused tests and static checks; confirm GREEN**

```bash
.venv/bin/pytest -q tests/gateway/test_openai_api.py tests/gateway/test_anthropic_api.py tests/gateway/test_tool_contract.py
.venv/bin/ruff check src/claude_sdk_proxy/openai_api.py src/claude_sdk_proxy/openai_tools.py tests/gateway/test_openai_api.py
.venv/bin/mypy src/claude_sdk_proxy/openai_api.py src/claude_sdk_proxy/openai_tools.py
```

- [ ] **Step 7: Commit Task 3**

```bash
git add src/claude_sdk_proxy/openai_api.py src/claude_sdk_proxy/openai_tools.py tests/gateway/test_openai_api.py
git commit -m "feat: add OpenAI tool wire protocol"
```

---

### Task 4: Build the in-process MCP bridge with per-invocation futures

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `src/claude_sdk_proxy/tool_bridge.py`
- Create: `tests/gateway/test_tool_bridge.py`

**Interfaces:**
- `ToolBridge(definitions, dialect, id_factory=None)` owns one generated `McpSdkServerConfig` and the generated allowed-tool tuple.
- `ToolBridge.seal_epoch(expected_calls) -> tuple[ToolInvocation, ...]` returns the current epoch in handler-entry order, with raw-order placeholders appended for callbacks deferred by the SDK. The epoch list is current-only and is cleared after the exact callback-completion barrier; there is no publication queue or lifetime cursor.
- `ToolBridge.resolve(results)` resolves all and only currently pending public IDs by ID, regardless of result order.
- `ToolBridge.cancel()` fails every pending handler, prevents new admission, and clears bridge-owned epoch collections.
- `ToolBridge.wait_failure()` is a lifetime fatal signal watched by the actor in generating and waiting states, so an invalid or late callback closes the session even after a public boundary has committed.
- `ToolInvocation` carries `public_id`, original public `name`, immutable arguments, and the generated SDK name only for internal validation.
- Each assistant message has a bridge epoch. `seal_epoch(expected_calls)` validates the exact private-ID/name/canonical-argument bijection for callbacks already entered, creates pending placeholders for deferred callbacks, rejects extras, and closes ordinary admission before returning the immutable invocation tuple. A sealed callback is accepted only for its exact unused placeholder. `wait_epoch_complete()` requires every callback to return its stored result before any later SDK item is accepted.

- [ ] **Step 1: Write failing bridge tests without an SDK subprocess**

Invoke the low-level MCP call callback directly. Prove advertised schema equality for `{ "type": "object" }`, schemas using `$ref`/`allOf`, and ordinary `properties`; assert each advertised tool has `_meta={"anthropic/maxResultSizeChars": 262144}`. Also prove one call/result, an empty successful result, non-empty error mapping, empty error rejection before bridge resolution, a near-256-KiB Unicode result delivered intact, distinct parallel calls, two identical parallel calls resolved in reverse order, unknown/duplicate/partial result rejection without consuming pending calls, cancellation, generated allowlist isolation, an extra callback already present at seal, and a callback released only after the expected set is sealed.

```python
@pytest.mark.anyio
async def test_identical_handlers_resolve_by_public_id_in_reverse() -> None:
    bridge = ToolBridge((echo_definition(),), dialect="openai", id_factory=id_sequence("call_a", "call_b"))
    first_task = asyncio.create_task(bridge.call_tool("echo", {"v": 1}, internal_id="sdk-a"))
    second_task = asyncio.create_task(bridge.call_tool("echo", {"v": 1}, internal_id="sdk-b"))
    first, second = await bridge.seal_epoch(
        (ToolCall("sdk-a", "echo", {"v": 1}), ToolCall("sdk-b", "echo", {"v": 1}))
    )

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
    await bridge.wait_epoch_complete()
```

- [ ] **Step 2: Run the focused tests and confirm RED**

```bash
.venv/bin/pytest -q tests/gateway/test_tool_bridge.py
```

Expected: `claude_sdk_proxy.tool_bridge` does not exist.

- [ ] **Step 3: Pin the directly imported MCP API**

Add `mcp==2.1.1` as a direct runtime dependency because production code now imports its low-level `Server`, `Tool`, `ListToolsResult`, `CallToolResult`, and `TextContent` types. Regenerate the lock without upgrading unrelated packages:

```bash
UV_CACHE_DIR=/private/tmp/claude-sdk-proxy-uv-cache uv lock
```

- [ ] **Step 4: Register exactly one low-level in-process MCP server**

Do not use `claude_agent_sdk.tool` or `create_sdk_mcp_server`: SDK 0.2.152's `_build_input_schema` rewrites valid object schemas unless both `type` and `properties` are present, and its helper validates before the proxy handler. Construct `mcp.types.Tool` values with an exact plain-dictionary copy of each caller `inputSchema`, and construct `mcp.server.Server` with `on_list_tools` and `on_call_tool` callbacks. Put that server in a typed `McpSdkServerConfig` dictionary with `type="sdk"` and `name="caller_tools_v1"`. Use the fixed private server key `caller_tools_v1`; its SDK allowlist entries are `mcp__caller_tools_v1__<public-name>`.

```python
wire_tools = [
    Tool(
        name=definition.name,
        description=definition.description,
        inputSchema=plain_json_object(definition.input_schema),
        _meta={"anthropic/maxResultSizeChars": 256 * 1024},
    )
    for definition in definitions
]

async def on_list_tools(context: object, params: object) -> ListToolsResult:
    del context, params
    return ListToolsResult(tools=wire_tools)

async def on_call_tool(context: object, params: CallToolRequestParams) -> CallToolResult:
    del context
    return await bridge.invoke(params.name, params.arguments or {})

config: McpSdkServerConfig = {
    "type": "sdk",
    "name": "caller_tools_v1",
    "instance": Server(
        "caller_tools_v1",
        version="1.0.0",
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    ),
}
```

- [ ] **Step 5: Implement per-coroutine IDs and atomic result delivery**

Before minting a public call, validate the generated arguments against the exact frozen caller schema with the already-pinned `jsonschema` validator and apply the 256 KiB canonical-argument and 64-container limits. A schema/shape/size/depth failure signals `BridgeProtocolFailure` and parks the callback for actor cancellation; it never becomes a caller-visible call or model-visible MCP error. For a valid call, require the exact private `_meta["claudecode/toolUseId"]`, enter the current epoch, and register the future atomically before awaiting it. At raw `message_stop`, seal the epoch against the exact private-ID/name/canonical-argument bijection, retaining already-entered handler order and appending placeholders for SDK-serialized callbacks. Reject extras and mismatches without a name/argument/order fallback. Validate a complete caller result batch before resolving any future so a bad batch has no partial effect. Return accepted caller results as `CallToolResult` containing only `TextContent` blocks and the exact Anthropic `isError` value. After echo validation, require all placeholder callbacks to enter and return, then clear current epoch invocation/argument collections.

- [ ] **Step 6: Run focused tests and static checks; confirm GREEN**

```bash
.venv/bin/pytest -q tests/gateway/test_tool_bridge.py tests/gateway/test_tool_contract.py
.venv/bin/ruff check src/claude_sdk_proxy/tool_bridge.py tests/gateway/test_tool_bridge.py
.venv/bin/mypy src/claude_sdk_proxy/tool_bridge.py
```

- [ ] **Step 7: Commit Task 4**

```bash
git add pyproject.toml uv.lock src/claude_sdk_proxy/tool_bridge.py tests/gateway/test_tool_bridge.py
git commit -m "feat: add in-process caller tool bridge"
```

---

### Task 5: Make the Agent SDK adapter validate native tool boundaries

**Files:**
- Modify: `src/claude_sdk_proxy/sdk_text_protocol.py`
- Create: `src/claude_sdk_proxy/sdk_tool_protocol.py`
- Modify: `src/claude_sdk_proxy/sdk_metadata.py`
- Modify: `src/claude_sdk_proxy/sdk_session.py`
- Create: `tests/fixtures/fake_sdk_cli.py`
- Modify: `tests/gateway/fakes.py`
- Modify: `tests/gateway/test_sdk_session.py`
- Create: `tests/gateway/test_sdk_transport.py`

**Interfaces:**
- `SdkSession(model, system, ..., *, tools=(), dialect="anthropic")` creates a `ToolBridge` only for non-empty tools; the new values are keyword-only so current constructor calls retain their meaning.
- `SdkSession.stream_generation(prompt)` is one receive iterator that may yield several public boundaries: `InputUsage`, `TextDelta`, `ToolCall`, `Completed("tool_use", usage)`, then later output, ending in one non-tool `Completed`.
- `SdkSession.submit_tool_results(results)` resolves only the bridge's current wait.
- `SdkSession.pending_tool_deadline` is not tracked here; lifecycle ownership remains in the actor.
- `SdkSession.stream_turn(prompt)` remains as a text-only compatibility wrapper for the current registry until Task 6 adopts `stream_generation`; it fails closed if a tool boundary somehow reaches a session created without tools.
- Every raw assistant message owns a fresh raw validator and boundary-local usage snapshot. The final SDK `ResultMessage.usage` is independently normalized aggregate query usage and is never required to equal the last raw message's usage.
- One or more consecutive post-submit SDK `UserMessage` values are accepted only in the exact awaiting-result-echo phase and only when their aggregate contains the complete native tool-result set for the preceding internal tool IDs; user messages remain fatal everywhere else.

- [ ] **Step 1: Extend fake SDK events and write failing configuration tests**

Add raw `content_block_start`/`input_json_delta`/`content_block_stop`, complete `AssistantMessage([ToolUseBlock(...)])`, `message_delta(stop_reason="tool_use")`, post-submit `UserMessage([SdkToolResultBlock(...)])`, and a final `ResultMessage`. Add one- and two-tool-round fixtures whose final aggregate `ResultMessage.usage` deliberately differs from every raw boundary, plus both one-combined-message and one-message-per-result parallel echoes. Assert text sessions retain empty MCP configuration, tool sessions expose one server/allowlist, `max_buffer_size == 8 * 1024 * 1024`, and every isolation option remains present. Also assert `system_prompt` equals the caller string byte-for-byte and `client.query()` receives only the caller's final text, proving the proxy added no tool prose.

```python
@pytest.mark.anyio
async def test_tool_session_exposes_only_generated_caller_tools(tmp_path: Path) -> None:
    session, client = tool_session(tmp_path, (echo_definition(),))
    await session.start()
    assert set(client.options.mcp_servers) == {"caller_tools_v1"}
    assert client.options.allowed_tools == ["mcp__caller_tools_v1__echo"]
    assert client.options.tools == []
    assert client.options.skills == []
    assert client.options.agents == {}
    assert client.options.plugins == []
```

- [ ] **Step 2: Write failing protocol tests**

Cover a single call, text plus calls, two identical calls, exact private-ID/name/argument mismatch, malformed/oversized/over-depth arguments, incomplete raw JSON, tool stop without handler entry, a callback released after the expected bridge epoch seals, handler entry without a raw SDK call, built-in/non-MCP tool names, parent-attributed blocks, a raw tool block followed by a text block, and a normal final result after one and two tool rounds. Cover deferred serial callbacks, both exact callback-completion versus incoming-SDK-item orderings, and bounded bridge state after repeated rounds. For native result echoes, cover single/parallel result-only `UserMessage` values, exact internal ID sets, content/error equality with bridge-delivered values, duplicate/missing/unknown IDs, strings versus text-block lists, unexpected blocks, origin/parent attribution, and user messages in every wrong phase. Preserve all existing raw text protocol failures.

- [ ] **Step 3: Write a failing real SDK transport buffer test**

Create an executable `tests/fixtures/fake_sdk_cli.py` that answers the SDK initialize control request, consumes one query input, and writes one valid tool-result `UserMessage` NDJSON line containing a 1 MiB aggregate of escape-heavy control text whose serialized line exceeds 6 MiB. In `test_sdk_transport.py`, instantiate the real `ClaudeSDKClient` with that `cli_path` and `max_buffer_size=8 * 1024 * 1024`, then assert the fully decoded result text arrives intact. This test must traverse SDK 0.2.152's subprocess transport/line framer rather than feed an already parsed `UserMessage` fake.

- [ ] **Step 4: Run the focused tests and confirm RED**

```bash
.venv/bin/pytest -q tests/gateway/test_sdk_session.py tests/gateway/test_sdk_transport.py tests/gateway/test_tool_bridge.py
```

Expected: tool events remain protocol failures and SDK options have no generated MCP server.

- [ ] **Step 5: Implement raw tool-use validation**

In `sdk_tool_protocol.py`, track raw block indexes and assemble JSON deltas, then compare them to exact complete `ToolUseBlock` values from `AssistantMessage`. Permit text blocks before the first tool block, but require tool-use blocks to be a suffix: after the first tool block starts, any later text block/delta is a protocol failure rather than a silently reordered public transcript. Accept only names with the exact generated `mcp__caller_tools_v1__` prefix and map them back through the frozen definition table before comparison; reject every built-in, unknown server, or unknown public name. At complete `message_stop`, seal the bridge using each raw/typed internal ID and require the callback's private `_meta["claudecode/toolUseId"]`, public name, and canonical arguments to match exactly. Keep opaque public IDs independent and never expose the SDK ID. Already-extra, duplicate, mismatched, or late callbacks are fatal; deferred callbacks may enter only their exact sealed placeholder, with no fallback correlation.

- [ ] **Step 6: Validate configuration-aware SDK metadata**

Change `validate_system_message` to receive the expected generated tool and MCP-server names. Text sessions still require exact empty `tools`, `mcp_servers`, `skills`, and `plugins`; tool sessions require exactly their derived generated tool list and `caller_tools_v1` server with no duplicate, unknown, built-in, or extra entry. Add raw init fixtures for exact generated-only acceptance and each rejected expansion, and retain redacted `BackendFailure` behavior.

- [ ] **Step 7: Integrate the bridge and multi-boundary receive iterator**

Configure the generated MCP server only when definitions are non-empty. Create/reset a raw validator at every `message_start`; on each validated tool boundary, yield its boundary-local `InputUsage`, calls, and `Completed("tool_use", boundary_usage)`, but keep iterating the same `receive_response()` after `submit_tool_results()` releases handlers. In the exact post-submit phase, accumulate one or more consecutive tool-result-only SDK `UserMessage` values until the complete native internal-ID set and exact delivered text/error values have been validated. Then race the bridge's exact callback-completion barrier against the next SDK item: callback completion must win, including when both tasks are done before the waiter resumes. A later tool-use start may open the next epoch; terminal text leaves it sealed. Require the final `ResultMessage` only at terminal completion; its status and stop reason must agree with the last raw boundary, while its independently valid aggregate usage may differ and is not emitted as that public boundary's usage.

- [ ] **Step 8: Run focused tests and static checks; confirm GREEN**

```bash
.venv/bin/pytest -q tests/gateway/test_sdk_session.py tests/gateway/test_sdk_transport.py tests/gateway/test_tool_bridge.py
.venv/bin/ruff check src/claude_sdk_proxy/sdk_session.py src/claude_sdk_proxy/sdk_text_protocol.py src/claude_sdk_proxy/sdk_tool_protocol.py src/claude_sdk_proxy/sdk_metadata.py tests/fixtures/fake_sdk_cli.py tests/gateway/fakes.py tests/gateway/test_sdk_session.py tests/gateway/test_sdk_transport.py
.venv/bin/mypy src/claude_sdk_proxy/sdk_session.py src/claude_sdk_proxy/sdk_text_protocol.py src/claude_sdk_proxy/sdk_tool_protocol.py src/claude_sdk_proxy/sdk_metadata.py
```

- [ ] **Step 9: Commit Task 5**

```bash
git add src/claude_sdk_proxy/sdk_session.py src/claude_sdk_proxy/sdk_text_protocol.py src/claude_sdk_proxy/sdk_tool_protocol.py src/claude_sdk_proxy/sdk_metadata.py tests/fixtures/fake_sdk_cli.py tests/gateway/fakes.py tests/gateway/test_sdk_session.py tests/gateway/test_sdk_transport.py
git commit -m "feat: validate native SDK tool boundaries"
```

---

### Task 6: Add the tool session actor and tool-aware transcript registry

**Files:**
- Modify: `src/claude_sdk_proxy/domain.py`
- Modify: `src/claude_sdk_proxy/session_turn.py`
- Modify: `src/claude_sdk_proxy/sessions.py`
- Create: `tests/gateway/test_tool_sessions.py`
- Modify: `tests/gateway/test_sessions.py`
- Modify: `tests/gateway/fakes.py`

**Interfaces:**
- `ToolSessionActor` owns the backend iterator and states `READY`, `GENERATING`, `WAITING_FOR_TOOLS`, and `CLOSED`.
- Existing `TurnLease.stream()` remains unchanged for empty-tool text sessions. A tool-specific lease exposes the same HTTP-facing one-shot iterator/abort/header surface so `app.py` can consume either without branching on implementation details.
- Registry fingerprints use canonical JSON over dialect, model, system, definitions, and every block field; rendering-only fields remain excluded.
- `SessionRegistry(..., tool_result_timeout_seconds=300.0)` schedules one wait deadline per committed tool boundary.
- A valid result continuation commits its canonical result turn before resolving bridge futures and attaches a response sink before SDK generation resumes.
- This task changes `SdkSessionProtocol` to `stream_generation(prompt)` plus `submit_tool_results(results)`. Replace the callable type alias with a callable protocol whose required positional arguments remain `(model, system)` and whose keyword-only arguments are `tools=()` and `dialect="anthropic"`; all text fakes and factory call sites are migrated in the same commit without reinterpreting existing positional arguments.
- Lock order is deliberately non-nested: never await or acquire an actor lock while holding the registry lock, and never acquire the registry lock while holding an actor lock. Registry admission reserves/releases an in-flight fingerprint under its lock, then calls the actor after releasing it; actor-driven removal is scheduled only after actor state has been finalized and its lock released.
- The actor races backend progress and tool-result commands against `ToolBridge.wait_failure()` for its entire lifetime; a late callback therefore closes the session rather than waiting unnoticed for another SDK boundary.

- [ ] **Step 1: Write failing fingerprint and admission tests**

Prove dialect/tool/schema/call/result/error changes do not alias; omitted tools are equivalent to empty tools only for fresh text requests; every tool continuation must resend the same non-empty definitions; imported calls/results are rejected; partial/unknown/duplicate/stale result sets are rejected without losing the waiting session; waiting sessions are non-evictable; all-busy capacity is 503.

```python
@pytest.mark.anyio
async def test_invalid_result_batch_leaves_actor_waiting_for_corrected_retry() -> None:
    registry, request, pending = await registry_waiting_on_two_calls()
    partial = continuation_with_results(request, (result(pending[0].id, "one"),))
    with pytest.raises(RequestValidationError):
        await registry.open_turn(partial, explicit_id=None)

    corrected = continuation_with_results(
        request,
        (result(pending[1].id, "two"), result(pending[0].id, "one")),
    )
    lease = await registry.open_turn(corrected, explicit_id=None)
    assert await collect(lease.stream()) == terminal_text_events("done")
```

- [ ] **Step 2: Write failing actor lifecycle tests**

Cover one call, distinct parallel calls, identical reverse-result calls, mixed text/calls, repeated rounds, completed replay without reinvocation, an actor-lifetime bridge failure after a sealed public boundary, in-flight duplicate 409, disconnect before first commit, disconnect after tool commit, continuation disconnect after result commit followed by replay, generation timeout, tool-result timeout, shutdown cancellation, and teardown failure redaction. Use barriers—not timing sleeps—to force result-versus-deadline, close-versus-tool-boundary, and shutdown-versus-result races in both orders; assert one linear outcome, no deadlock, no double commit, and one backend close.

```python
@pytest.mark.anyio
async def test_identical_parallel_calls_keep_reverse_results_distinct() -> None:
    registry, first = actor_registry_for_identical_calls()
    boundary = await collect((await registry.open_turn(first, None)).stream())
    calls = [event for event in boundary if isinstance(event, ToolCall)]
    assert len({call.id for call in calls}) == 2

    continuation = with_results(
        first,
        (
            ToolResultBlock(calls[1].id, ("right",), False),
            ToolResultBlock(calls[0].id, ("left",), False),
        ),
    )
    assert await collect((await registry.open_turn(continuation, None)).stream()) == terminal_text_events("left/right")
```

- [ ] **Step 3: Run the focused tests and confirm RED**

```bash
.venv/bin/pytest -q tests/gateway/test_tool_sessions.py tests/gateway/test_sessions.py
```

Expected: the registry has no tool-aware actor, fingerprints omit block/tool data, and the backend generator cannot survive a public boundary.

- [ ] **Step 4: Implement deterministic fingerprints and matching**

Serialize a tagged canonical structure rather than dataclass `repr`. Sort definitions by public name and every result-only block tuple by public call ID before hashing/comparison, while retaining assistant call invocation order:

```python
identity = {
    "dialect": request.dialect,
    "model": request.model,
    "system": request.system,
    "tools": [canonical_tool(tool) for tool in request.tools],
    "messages": [canonical_message(message) for message in request.messages],
}
fingerprint = sha256(canonical_json(identity).encode()).hexdigest()
```

Match continuations against the actor's committed transcript head. Before leasing a result continuation, validate that its final turn contains all and only `actor.pending_call_ids` and that every preceding block equals the committed head.

- [ ] **Step 5: Implement the sole-owner background actor**

Leave the existing empty-tool `Conversation`/`TurnLease` implementation intact. For a non-empty frozen definition tuple, use one background task per SDK operation and one response queue/future per attached public request. The task drains backend events until a `Completed` boundary. On `tool_use`, atomically commit assistant blocks/replay, set the pending IDs/deadline token, detach the completed response, and await a continuation signal. On terminal completion, commit/replay and return to `READY`. All actor state transitions serialize under one actor lock; HTTP cleanup never directly consumes the backend iterator. The registry lock is never held across an actor call, and actor callbacks release the actor lock before requesting registry removal. Start a fresh generation deadline for the initial prompt and after each committed valid result batch; never let tool-wait time count against the next generation window.

- [ ] **Step 6: Implement result commit, retry, timeout, and close semantics**

Validate the whole batch first. Under actor ownership, compare `loop.time()` with the stored monotonic deadline immediately before committing: expiry wins at or past the deadline even if a timer task was merely delayed, while a committed result invalidates the old deadline token so a queued timer cannot close the resumed generation. Attach the continuation response sink, commit the result turn, invalidate the wait token, then call `submit_tool_results`. If that request disconnects after commit, detach only the sender and let the actor buffer through its next boundary for replay. A tool wait callback carries the deadline token, rechecks it under actor ownership, finalizes `CLOSED`, releases the actor lock, and only then asks the registry to remove/schedule teardown. Make `close()` idempotently invalidate every timer token, cancel the backend task and bridge futures, and disconnect/clean the SDK through existing bounded teardown without holding either registry or actor lock across an await.

- [ ] **Step 7: Run focused tests and static checks; confirm GREEN**

```bash
.venv/bin/pytest -q tests/gateway/test_tool_sessions.py tests/gateway/test_sessions.py
.venv/bin/ruff check src/claude_sdk_proxy/domain.py src/claude_sdk_proxy/session_turn.py src/claude_sdk_proxy/sessions.py tests/gateway/fakes.py tests/gateway/test_tool_sessions.py tests/gateway/test_sessions.py
.venv/bin/mypy src/claude_sdk_proxy/domain.py src/claude_sdk_proxy/session_turn.py src/claude_sdk_proxy/sessions.py
```

- [ ] **Step 8: Commit Task 6**

```bash
git add src/claude_sdk_proxy/domain.py src/claude_sdk_proxy/session_turn.py src/claude_sdk_proxy/sessions.py tests/gateway/fakes.py tests/gateway/test_sessions.py tests/gateway/test_tool_sessions.py
git commit -m "feat: keep SDK tool sessions alive across requests"
```

---

### Task 7: Integrate tool boundaries with HTTP streaming, replay, and errors

**Files:**
- Modify: `src/claude_sdk_proxy/app.py`
- Modify: `src/claude_sdk_proxy/asgi_stream.py`
- Modify: `src/claude_sdk_proxy/http_errors.py`
- Modify: `tests/gateway/test_app.py`
- Create: `tests/gateway/test_tool_http.py`

**Interfaces:**
- Both POST routes feed `TextRequest` into the same registry/actor path.
- Nonstream collection preserves ordered `TextBlock | ToolCallBlock` rather than joining text.
- Streaming renderers receive per-response block indexes/call indexes through a dialect render state.
- Tool-result validation is HTTP 400; transcript/configuration mismatches are HTTP 409; generation/tool wait timeout is HTTP 504; SDK failure remains redacted HTTP/SSE 502.

- [ ] **Step 1: Write failing end-to-end ASGI tests for both dialects**

For each dialect and stream mode, complete a single tool round and repeated rounds. Add the release-critical case: two identical calls, distinct IDs, reverse-order result submission, and distinct final values. Assert exact replay IDs and that the fake bridge handler count does not increase. Give each raw assistant boundary distinct usage and give the SDK `ResultMessage` a different aggregate total; assert every HTTP response reports only its own boundary-local usage.

```python
@pytest.mark.anyio
@pytest.mark.parametrize("dialect", ["anthropic", "openai"])
@pytest.mark.parametrize("stream", [False, True])
async def test_http_identical_parallel_calls_round_trip_in_reverse(dialect: str, stream: bool) -> None:
    async with tool_gateway_client() as client:
        boundary = await send_first_tool_request(client, dialect, stream)
        calls = extract_calls(boundary, dialect)
        assert calls[0].arguments == calls[1].arguments
        assert calls[0].id != calls[1].id
        final = await send_reverse_results(client, dialect, stream, calls)
    assert extract_text(final, dialect) == "first=left second=right"
```

- [ ] **Step 2: Write failing lifecycle/error HTTP tests**

Cover partial/duplicate/unknown results (400 then corrected retry), tool/schema/dialect mismatch (409), duplicate in-flight request (409), capacity with waiting sessions (503), pre-header and midstream generation timeout (504/error event), parked tool timeout cleanup, disconnect after tool commit, disconnect after result commit with identical replay, shutdown, and oversized caller/model payload redaction.

- [ ] **Step 3: Run the focused tests and confirm RED**

```bash
.venv/bin/pytest -q tests/gateway/test_tool_http.py tests/gateway/test_app.py
```

Expected: the app concatenates only text, stream encoders have no per-response tool state, and disconnect cleanup always invalidates the backend.

- [ ] **Step 4: Make nonstream and stream response assembly block-aware**

Replace `text: list[str]` with ordered response blocks. Coalesce adjacent text deltas only for nonstream JSON. For SSE, instantiate one render state per HTTP response and feed each canonical event through it; terminal encoding must close any open block and map `Completed("tool_use")` to the dialect's tool finish reason.

- [ ] **Step 5: Route actor commit state through disconnect cleanup**

Expose an explicit lease property/method that tells cleanup whether the current public boundary or continuation result turn is committed. Abort only uncommitted work. A committed tool boundary releases the HTTP sender but not the actor; a committed continuation detaches the sender while the actor continues into replay.

- [ ] **Step 6: Complete stable error mapping**

Use `RequestValidationError` for malformed schemas/references/messages and invalid or partial result sets. Use `UnsupportedFeature` only for well-formed but unsupported controls and shapes such as forced tool choice, disabled parallel calls, non-function OpenAI tools, and non-text result blocks. Use `SessionMismatch` for transcript/configuration/dialect changes, `SessionConflict` for busy duplicates, `SessionCapacity` for all-busy admission, `SessionTimeout` for generation or attached result-wait timeout, and `BackendFailure` for SDK protocol faults. Never include SDK exception text, tool arguments, tool results, or session IDs in public errors.

- [ ] **Step 7: Run focused and full gateway checks; confirm GREEN**

```bash
.venv/bin/pytest -q tests/gateway/test_tool_http.py tests/gateway/test_app.py
.venv/bin/pytest -q tests/gateway
.venv/bin/ruff check src/claude_sdk_proxy/app.py src/claude_sdk_proxy/asgi_stream.py src/claude_sdk_proxy/http_errors.py tests/gateway/test_tool_http.py
.venv/bin/mypy src/claude_sdk_proxy/app.py src/claude_sdk_proxy/asgi_stream.py src/claude_sdk_proxy/http_errors.py
```

- [ ] **Step 8: Commit Task 7**

```bash
git add src/claude_sdk_proxy/app.py src/claude_sdk_proxy/asgi_stream.py src/claude_sdk_proxy/http_errors.py tests/gateway/test_app.py tests/gateway/test_tool_http.py
git commit -m "feat: serve caller tool loops over both APIs"
```

---

### Task 8: Prove compatibility with official clients and Pi

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/integration/test_official_tool_clients.py`
- Create: `tests/fixtures/pi_tool_client.mjs`
- Modify: `tests/integration/pi_gateway_support.py`
- Modify: `tests/integration/test_pi_gateway.py`

**Interfaces:**
- Development dependencies include bounded compatible versions of `anthropic` and `openai` used only for black-box compatibility tests.
- Official-client tests use actual localhost Uvicorn and deterministic fake SDK/bridge behavior.
- Pi uses its ordinary `openai-completions` provider configuration; the JavaScript fixture defines and executes tools without gateway-specific source changes or headers.

- [ ] **Step 1: Add failing official-client round-trip tests**

Start the real ASGI server fixture and use `AsyncAnthropic(base_url=..., api_key="local-placeholder")` and `AsyncOpenAI(base_url=..., api_key="local-placeholder")`. Exercise streaming and nonstream single/repeated rounds, non-empty explicit Anthropic errors, Anthropic empty-error rejection as HTTP 400 before SDK resume, identical parallel calls returned in reverse, and boundary-local usage that deliberately differs from aggregate SDK result usage.

```python
@pytest.mark.anyio
async def test_openai_client_round_trips_identical_parallel_calls() -> None:
    async with serve(tool_app()) as base_url:
        client = AsyncOpenAI(base_url=f"{base_url}/v1", api_key="local-placeholder")
        first = await client.chat.completions.create(**openai_first_request())
        calls = first.choices[0].message.tool_calls
        final = await client.chat.completions.create(
            **openai_result_request(calls, reverse=True)
        )
    assert calls[0].id != calls[1].id
    assert final.choices[0].message.content == "left/right"
```

- [ ] **Step 2: Run the official-client tests and confirm RED**

```bash
.venv/bin/pytest -q tests/integration/test_official_tool_clients.py
```

Expected: official client packages or the compatibility fixtures are absent.

- [ ] **Step 3: Add locked client dependencies and implement fixtures**

Add the clients to `[dependency-groups].dev`, run `uv lock`, and build request histories only through client model dumps/public inputs. Do not bypass serialization with raw `httpx` in these tests.

```bash
UV_CACHE_DIR=/private/tmp/claude-sdk-proxy-uv-cache uv lock
uv sync --dev
```

- [ ] **Step 4: Add a real Pi tool-loop fixture and failing test**

Mirror the existing `pi_text_client.mjs` provider setup, but give Pi one local echo tool and deterministically drive two successive calls to that same tool across ordinary agent-loop iterations. Record outbound request shapes and tool executions. Assert provider configuration only, native tool calls/results, no custom session header, repeated-loop completion, exactly two executions, and no additional execution on completed HTTP replay. Pi 0.84.4 collects parallel executions with `Promise.all` but serializes result messages in original call order, so reverse-order submission is deliberately proved by the raw HTTP and official-client tests instead of being falsely required from Pi.

- [ ] **Step 5: Run focused integration tests and confirm GREEN**

```bash
.venv/bin/pytest -q --strict-markers --forbid-skips -W error tests/integration/test_official_tool_clients.py tests/integration/test_pi_gateway.py
```

- [ ] **Step 6: Commit Task 8**

```bash
git add pyproject.toml uv.lock tests/integration/test_official_tool_clients.py tests/fixtures/pi_tool_client.mjs tests/integration/pi_gateway_support.py tests/integration/test_pi_gateway.py
git commit -m "test: prove standard harness tool compatibility"
```

---

### Task 9: Add configuration, operator documentation, and live subscription proof

**Files:**
- Modify: `src/claude_sdk_proxy/cli.py`
- Modify: `tests/gateway/test_cli.py`
- Create: `tests/live/test_gateway_tools.py`
- Modify: `docs/feasibility/README.md`

**Interfaces:**
- `--tool-result-timeout SECONDS` accepts a positive finite float and defaults to `300.0`.
- `create_app(..., tool_result_timeout_seconds=300.0)` receives the CLI value.
- `CLAUDE_PROXY_LIVE=1` and `CLAUDE_PROXY_LIVE_MODEL=<configured alias>` gate current live gateway tests; legacy `RUN_LIVE_CLAUDE_TESTS` remains unrelated.
- `make release-offline` includes deterministic gateway plus official-client/Pi tool tests but never invokes a subscription model.

- [ ] **Step 1: Write failing CLI tests**

Assert the default and explicit timeout are threaded into `create_app`; reject zero, negative, NaN, and infinity.

```python
def test_cli_threads_tool_result_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "create_app", lambda **kwargs: captured.update(kwargs) or object())
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: None)
    assert cli.main(["--tool-result-timeout", "17.5"]) == 0
    assert captured["tool_result_timeout_seconds"] == 17.5
```

- [ ] **Step 2: Run the CLI tests and confirm RED**

```bash
.venv/bin/pytest -q tests/gateway/test_cli.py
```

Expected: the option is unknown and `create_app` has no tool-result timeout argument.

- [ ] **Step 3: Implement the CLI/configuration path**

Add a finite-positive float parser, thread the value through `create_app` to `SessionRegistry`, and retain loopback-only host validation.

- [ ] **Step 4: Add the opt-in live tool matrix**

Use the production app and installed authenticated SDK. Add separate tests for: one round; identical parallel reverse results through Anthropic and OpenAI; mixed text/calls; a non-empty Anthropic `is_error`; empty Anthropic error rejection before SDK resume followed by a corrected retry; repeated rounds; continuation disconnect/replay; wait timeout cleanup; generated-tool-only isolation; and a Pi tool loop using only ordinary provider configuration. Mark every test `live` and fail clearly when the two explicit live environment variables or the already-required Pi installation are absent.

Because model behavior is nondeterministic, tool schemas and prompts should make the requested call count unambiguous, but assertions must inspect protocol invariants rather than exact prose. The identical-parallel tests must require exactly two distinct IDs and verify each returned marker appears in the model's final answer.

- [ ] **Step 5: Update operator documentation and offline release gate**

Document both endpoint examples, supported/rejected controls, result limits, restart loss, capacity/timeouts, injected native schema/provider instructions, Pi configuration without `--no-tools`, optional session header guidance, and the exact live command. Change the README's current supported boundary from “tools unsupported” to the tested caller-owned subset. Confirm the existing directory-based `integration` target automatically includes the new deterministic tool integration tests; do not edit the Makefile unless that verification disproves the assumption.

- [ ] **Step 6: Run deterministic release verification**

```bash
.venv/bin/pytest -q tests/gateway tests/integration
make check
make release-offline
```

Expected: all deterministic tests pass; no live subscription test is selected.

- [ ] **Step 7: Run the authenticated live release matrix**

Run from the normal authenticated host context, outside a credential-isolating sandbox:

```bash
CLAUDE_PROXY_LIVE=1 CLAUDE_PROXY_LIVE_MODEL=sonnet .venv/bin/pytest -q --strict-markers --forbid-skips -W error tests/live/test_gateway_text.py tests/live/test_gateway_tools.py
```

Expected: all text and tool scenarios pass. In particular, both dialect variants of identical parallel calls expose two distinct public IDs and correlate reverse-order results correctly. If the model/SDK cannot satisfy this invariant after prompt/schema stabilization, stop and report it as a hard unsupported boundary; do not add name/argument-based guessing.

- [ ] **Step 8: Run final static and repository checks**

```bash
.venv/bin/ruff check src tests
.venv/bin/mypy
git diff --check
git status --short
```

- [ ] **Step 9: Commit Task 9**

```bash
git add src/claude_sdk_proxy/cli.py tests/gateway/test_cli.py tests/live/test_gateway_tools.py docs/feasibility/README.md
git commit -m "docs: ship caller-owned tool gateway"
```

---

## Final review checklist

- [ ] Confirm every acceptance criterion in the design spec maps to at least one named deterministic or live test above.
- [ ] Confirm the two dialects share canonical session/bridge logic and differ only at strict parse/render boundaries.
- [ ] Confirm the release-critical identical-parallel reverse-result case passes for Anthropic and OpenAI without order/name/argument correlation.
- [ ] Confirm a completed retry reuses the original public IDs and does not invoke a handler or caller tool twice.
- [ ] Confirm invalid result batches leave the original waiting session usable until its original deadline.
- [ ] Confirm no production path enables built-ins, ambient settings/MCP, skills, plugins, agents, auto-memory, slash commands, persistence, or proxy-side tool execution.
- [ ] Confirm text-only gateway, official-client, and Pi regression suites pass unchanged except for intentional new tool assertions.
- [ ] Confirm live tests are opt-in and deterministic release targets never consume subscription inference.
- [ ] Confirm no superseded journal, supervisor, attestation, or crash-recovery subsystem is imported by the new gateway path.
- [ ] Confirm public errors and logs do not expose prompts, arguments, results, credentials, account metadata, SDK exception contents, or session identifiers.
