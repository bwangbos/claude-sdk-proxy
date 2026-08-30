# Phase 3 Caller-Owned Tool Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add caller-defined external tools whose schemas are fixed per session, whose execution remains entirely in the harness, and whose results resume the same suspended Agent SDK query without enabling Claude Code built-in tools.

**Architecture:** Caller tools become one session-local in-process MCP server. One actor-owned receive loop correlates finalized public tool-use blocks with suspended MCP callbacks, commits an intermediate `WAITING_FOR_TOOLS` head, and resolves callbacks only after a later standard HTTP request supplies the complete matching result set. Anthropic and OpenAI adapters translate only at the boundary.

**Tech Stack:** Existing Phase 2 stack and the exact public in-process MCP APIs proven by Phase 0.

**Spec:** `docs/superpowers/specs/2026-08-29-claude-subscription-api-proxy-design.md`

## Global Constraints

- Do not enable tools for a resolved model unless `tools_gate_passed(model)` is the conjunction of every required Phase 0 `tool_gates_by_model[model]` value for the pinned SDK/CLI pair. Missing model/key evidence is false; no partial gate subset enables production tools.
- Do not use deferred-tool APIs, session restart/resume, SDK-private JSON-RPC IDs, argument/order guesses, or prompt serialization.
- Keep `tools=[]`; expose only the caller's immutable in-process MCP server and exact allowed MCP names.
- Tool schema, model, system prompt, thinking settings, result dialect, and error semantics are immutable for a session.
- Support text tool results plus explicit error status only. Reject empty, image, document, resource, search-result, and mixed results.
- A tool-use head commits only after the full call set and every corresponding suspended callback are established.
- A tool-result request must contain all and only the pending IDs, no unrelated user text, and a new idempotency key in explicit mode.
- Tool timeout, cancellation, deletion, or shutdown closes the session and cancels every pending callback.

## File Map

- `src/claude_sdk_proxy/domain.py`: tool schemas, calls, results, and canonical events.
- `src/claude_sdk_proxy/tool_bridge.py`: in-process MCP construction and public-ID callback correlation.
- `src/claude_sdk_proxy/backend.py`: tool-aware SDK event normalization and callback resolution protocol.
- `src/claude_sdk_proxy/session.py`: `WAITING_FOR_TOOLS` transitions, timeouts, and tool-result idempotency.
- `src/claude_sdk_proxy/control.py`: explicit-session tool acceptance and immutable session creation.
- `src/claude_sdk_proxy/app.py`: automatic first-bind bridge creation/rollback and route integration.
- `src/claude_sdk_proxy/config.py`: tool limits/timeouts and gate-aware feature configuration.
- `src/claude_sdk_proxy/anthropic.py`: Anthropic tools/tool-use/tool-result wire mapping.
- `src/claude_sdk_proxy/openai_adapter.py`: OpenAI functions/tool_calls/tool messages mapping.
- `tests/unit/test_tool_domain.py`: schema and transcript invariants.
- `tests/unit/test_tool_bridge.py`: deterministic correlation/cancellation tests.
- `tests/unit/test_tool_session.py`: actor state-machine tests.
- `tests/integration/test_tool_session_creation.py`: explicit and automatic bridge lifecycle tests.
- `tests/integration/test_anthropic_tools.py`: Anthropic HTTP/SSE tool loop.
- `tests/integration/test_openai_tools.py`: OpenAI HTTP/SSE tool loop.
- `tests/live/test_proxy_tool_loop.py`: end-to-end subscription tool loop using the real server.

---

### Task 1: Add Canonical Tool Types and Transcript Validation

**Files:**
- Modify: `src/claude_sdk_proxy/domain.py`
- Create: `tests/unit/test_tool_domain.py`

**Interfaces:**
- Consumes: existing canonical content/events.
- Produces: `ToolDefinition`, `ToolUseBlock`, `ToolResultBlock`, `PendingToolSet`, and `validate_tool_result_turn()`.

- [ ] **Step 1: Write failing tool invariant tests**

```python
# tests/unit/test_tool_domain.py
import pytest

from claude_sdk_proxy.domain import (
    ProxyError,
    ToolResultBlock,
    ToolUseBlock,
    validate_tool_result_turn,
)


def test_result_turn_must_match_all_pending_ids_once() -> None:
    pending = (
        ToolUseBlock(id="toolu_1", name="echo", input={"x": 1}),
        ToolUseBlock(id="toolu_2", name="echo", input={"x": 1}),
    )
    results = (
        ToolResultBlock(tool_use_id="toolu_2", text="second", is_error=False),
        ToolResultBlock(tool_use_id="toolu_1", text="first", is_error=False),
    )
    assert [item.tool_use_id for item in validate_tool_result_turn(pending, results)] == [
        "toolu_1", "toolu_2"
    ]


def test_partial_results_are_rejected_without_mutation() -> None:
    pending = (ToolUseBlock(id="toolu_1", name="echo", input={}),)
    with pytest.raises(ProxyError, match="complete pending tool set"):
        validate_tool_result_turn(pending, ())
```

- [ ] **Step 2: Implement frozen tool types and validation**

```python
# Add to src/claude_sdk_proxy/domain.py
class ToolDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    description: str
    input_schema: dict[str, Any]


class ToolUseBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any]


class ToolResultBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    text: str
    is_error: bool = False
```

`validate_tool_result_turn()` rejects duplicate IDs, unknown IDs, missing IDs, non-text results, and additional user content; it returns results ordered by pending call order without changing their text.

- [ ] **Step 3: Include tools in immutable configuration hashes**

Sort definitions only by their public list position, not by name. Canonical JSON encoding sorts object keys within each JSON Schema but preserves tool order. Add tests proving property-order differences normalize while tool-order/schema changes fail session validation.

- [ ] **Step 4: Run tests and commit**

Run: `uv run pytest tests/unit/test_tool_domain.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/domain.py tests/unit/test_tool_domain.py
git commit -m "feat: add canonical external tool types"
```

---

### Task 2: Productionize the Correlated In-Process MCP Bridge

**Files:**
- Create: `src/claude_sdk_proxy/tool_bridge.py`
- Modify: `src/claude_sdk_proxy/isolation.py`
- Modify: `src/claude_sdk_proxy/backend.py`
- Modify: `src/claude_sdk_proxy/session.py`
- Modify: `src/claude_sdk_proxy/control.py`
- Modify: `src/claude_sdk_proxy/app.py`
- Modify: `src/claude_sdk_proxy/config.py`
- Create: `src/claude_sdk_proxy/capabilities.py`
- Create: `tests/unit/test_tool_bridge.py`
- Create: `tests/integration/test_tool_session_creation.py`

**Interfaces:**
- Consumes: immutable `ToolDefinition` tuple, backend factory, session/control paths, and Phase 0's complete tool gate plus proven public ID-correlation mechanism.
- Produces: `ToolBridge`, `PendingCallback`, `ToolBridgeFactory`, tool-aware backend/session construction, `ToolBridge.all_suspended()`, `.resolve_all()`, `.cancel()`, and accurate capabilities.

- [ ] **Step 1: Write deterministic parallel-correlation tests**

```python
# tests/unit/test_tool_bridge.py
import pytest

from claude_sdk_proxy.domain import ToolResultBlock, ToolUseBlock
from claude_sdk_proxy.tool_bridge import ToolBridge


@pytest.mark.anyio
async def test_reverse_results_resolve_matching_callbacks() -> None:
    bridge = ToolBridge.for_test()
    first = bridge.suspend_for_test(public_id="toolu_1", name="echo", arguments={"x": 1})
    second = bridge.suspend_for_test(public_id="toolu_2", name="echo", arguments={"x": 1})
    await bridge.resolve_all((
        ToolResultBlock(tool_use_id="toolu_2", text="second"),
        ToolResultBlock(tool_use_id="toolu_1", text="first"),
    ))
    assert (await first)["content"][0]["text"] == "first"
    assert (await second)["content"][0]["text"] == "second"
```

- [ ] **Step 2: Implement callback records and atomic resolution**

```python
# Core declarations in src/claude_sdk_proxy/tool_bridge.py
from dataclasses import dataclass
from typing import Any
import anyio


@dataclass(slots=True)
class PendingCallback:
    public_id: str
    tool_name: str
    arguments: dict[str, Any]
    result: anyio.Event
    value: dict[str, Any] | None = None


def mcp_text_result(text: str, is_error: bool) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}
```

Build the MCP server with the exact public mechanism recorded by Phase 0. Correlation must use that recorded public hook/permission path; add an assertion that every handler suspension has one distinct public tool-use ID before returning `all_suspended=True`. `resolve_all()` validates the complete ID set first, assigns every value, then signals callbacks so partial resolution is impossible.

- [ ] **Step 3: Add cancellation tests**

Assert timeout/deletion cancellation wakes every handler with a controlled bridge exception, drops arguments/results from memory, and makes later resolution return `410 session_closed` without touching SDK state.

- [ ] **Step 4: Wire the bridge into isolated options**

Extend `build_agent_options()` with an optional `ToolBridge`. Keep `tools=[]`, set `mcp_servers` to exactly the bridge server, set `CLAUDE_AGENT_SDK_MCP_NO_PREFIX=1`, and set `allowed_tools` to exactly the caller tool names exposed by that server. Tests must assert public names round-trip unchanged and no built-in name is advertised.

- [ ] **Step 5: Integrate bridge creation into every session path**

Extend `SessionConfig` and its canonical hash with immutable `tools`. `POST /_proxy/sessions` accepts validated tool definitions and creates the `ToolBridge` before constructing the backend; if bridge or backend construction fails, cancel the bridge and roll back the registry entry atomically. Automatic mode accepts tools only on the unbound run token's first valid request, creates/binds the bridge and backend once, and applies the same rollback; exact simultaneous first-request retries share the construction reservation. One-shot tool requests are rejected because results require a reusable head. Later requests must repeat an exactly equivalent tool list where the dialect wire format requires tools, or omit it where the documented protocol permits; any semantic change returns `409 session_config_mismatch`.

Refactor the production backend factory interface to receive the complete `SessionConfig` plus optional bridge and to return one owner pair. Add integration tests for explicit creation, automatic first bind, exact retry during creation, divergent simultaneous bind, bridge-construction failure, backend-construction failure, deletion, and shutdown. Assert no registry/backend/bridge leak on every failure path.

- [ ] **Step 6: Gate and advertise the feature accurately**

At startup, load every named Phase 0 `tool_gates_by_model` boolean. `/_proxy/capabilities` advertises Anthropic/OpenAI caller-owned tools, streaming argument deltas, parallel correlation, timeout, and maximum schema/result sizes only for models whose complete conjunction is true. Before constructing a bridge, both explicit-session creation and automatic first bind resolve the configured model and reject missing/false evidence with `400 unsupported_tools`. Unit tests flip each boolean false in isolation, request an unrecorded model, and omit a model key; each case must hide the entire tool capability for that model and reject tool-bearing requests before bridge creation while leaving independently validated models unchanged.

- [ ] **Step 7: Run tests and commit**

Run: `uv run pytest tests/unit/test_tool_bridge.py tests/unit/test_isolation.py tests/integration/test_tool_session_creation.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/tool_bridge.py src/claude_sdk_proxy/isolation.py src/claude_sdk_proxy/backend.py src/claude_sdk_proxy/session.py src/claude_sdk_proxy/control.py src/claude_sdk_proxy/app.py src/claude_sdk_proxy/config.py src/claude_sdk_proxy/capabilities.py tests/unit/test_tool_bridge.py tests/unit/test_isolation.py tests/integration/test_tool_session_creation.py
git commit -m "feat: add correlated in-process MCP bridge"
```

---

### Task 3: Extend the Session Actor with Tool-Wait States

**Files:**
- Modify: `src/claude_sdk_proxy/backend.py`
- Modify: `src/claude_sdk_proxy/session.py`
- Create: `tests/unit/test_tool_session.py`

**Interfaces:**
- Consumes: tool-aware canonical events and `ToolBridge`.
- Produces: `ActorState.WAITING_FOR_TOOLS`, `SessionActor.submit_tool_results()`, timeout tombstones, and repeat-tool-cycle support.

- [ ] **Step 1: Write the intermediate-commit test**

Create a fake backend that emits two complete tool calls and exposes a fake suspended bridge. Assert the actor activates the reserved head and enters `WAITING_FOR_TOOLS` only after both callbacks are suspended, while the backend iterator remains active.

- [ ] **Step 2: Write incomplete, mixed, and concurrent-result tests**

Assert partial IDs, duplicate IDs, unknown IDs, tool results plus user text, stale head, repeated idempotency key with changed results, and a second simultaneous result request all fail before callback resolution.

- [ ] **Step 3: Implement the tool state transitions**

Add `WAITING_FOR_TOOLS`. `execute()` recognizes a finalized tool boundary, commits its public events/cache/head, and returns without cancelling the actor-owned receive task. `submit_tool_results()` validates the request and head, reserves the next head, changes to `GENERATING`, resolves all callbacks atomically, and resumes consuming the existing iterator. It either commits another complete tool set back to `WAITING_FOR_TOOLS` or waits for successful `ResultMessage`/iterator completion and returns to `IDLE`.

- [ ] **Step 4: Implement timeout and closure races**

Start a cancel scope when entering `WAITING_FOR_TOOLS`. Timeout, deletion, expiry, or shutdown performs `WAITING_FOR_TOOLS -> CLOSED`, cancels the bridge and receive loop, closes the backend, drops transcript/cache, and stores a reason-only `session_closed` tombstone. If the tool timeout fires with no HTTP request outstanding, record machine-readable cause `tool_result_timeout`; the next request returns `410 session_closed` with that cause. Add deterministic clock tests for timeout racing a result request; exactly one path wins the short actor reservation lock. Also cover deletion/shutdown while `GENERATING`, later `LOST -> CLOSED` cleanup, and prove no cancellation path returns to `IDLE`.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/unit/test_tool_session.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/backend.py src/claude_sdk_proxy/session.py tests/unit/test_tool_session.py
git commit -m "feat: add external tool wait state machine"
```

---

### Task 4: Add Anthropic Tool Wire Semantics

**Files:**
- Modify: `src/claude_sdk_proxy/anthropic.py`
- Create: `tests/integration/test_anthropic_tools.py`

**Interfaces:**
- Consumes: canonical tool definitions/calls/results and tool-aware actor.
- Produces: Anthropic `tools`, `tool_use`, `tool_result`, `stop_reason=tool_use`, and streaming tool deltas.

- [ ] **Step 1: Write a complete non-streaming tool-loop test**

First request supplies two tools and a user prompt. Fake backend returns two calls. Assert response contains two native `tool_use` blocks and `stop_reason="tool_use"`. Second request repeats that assistant response and supplies both results; assert the same session/head continues and returns final text without calling `send_user_turn()` for the results.

- [ ] **Step 2: Write schema/result rejection tests**

Reject per-turn tool changes, `tool_choice`, parallel-tool control, missing or extra results, content mixed with tool results, non-text results, mismatched assistant call blocks, oversized schema/arguments/results, and tool results on a session not waiting for tools.

- [ ] **Step 3: Implement Anthropic parsing/rendering**

Map public schemas to immutable `ToolDefinition`. Preserve public tool-use IDs from SDK events. Parse tool-result turns separately from user turns. Render text error results with `is_error`; never turn results into user prompt text. In SSE, buffer the terminal `message_delta`/`message_stop` until the complete call set and callbacks are suspended.

- [ ] **Step 4: Run tests and commit**

Run: `uv run pytest tests/integration/test_anthropic_tools.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/anthropic.py tests/integration/test_anthropic_tools.py
git commit -m "feat: expose caller-owned Anthropic tools"
```

---

### Task 5: Add OpenAI Tool Wire Semantics

**Files:**
- Modify: `src/claude_sdk_proxy/openai_adapter.py`
- Create: `tests/integration/test_openai_tools.py`

**Interfaces:**
- Consumes: canonical tool support from Tasks 1–3.
- Produces: OpenAI `tools`, assistant `tool_calls`, `tool` messages, and `finish_reason="tool_calls"`.

- [ ] **Step 1: Write OpenAI tool-loop and argument tests**

Assert function definitions map to canonical schemas, SDK argument objects serialize as compact JSON strings without semantic changes, two assistant calls preserve IDs/order, reversed tool messages resolve by ID, and final continuation returns ordinary assistant text.

- [ ] **Step 2: Write unsupported-semantics tests**

Reject legacy `functions`/`function_call`, tool choice, parallel controls, non-function tool types, tool messages with unknown IDs, assistant text mixed with pending calls when Anthropic semantics cannot preserve it, and tool error conventions that have no exact mapping.

- [ ] **Step 3: Implement OpenAI mapping**

Translate function tools to `ToolDefinition`, public SDK tool calls to `tool_calls`, and OpenAI tool messages to `ToolResultBlock(is_error=False)`. OpenAI has no standard tool-error flag in this subset; document that callers encode tool failures as text. Map canonical tool stop to `finish_reason="tool_calls"`; buffer final SSE success until callbacks are suspended.

- [ ] **Step 4: Run tests and commit**

Run: `uv run pytest tests/integration/test_openai_tools.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/openai_adapter.py tests/integration/test_openai_tools.py
git commit -m "feat: expose caller-owned OpenAI tools"
```

---

### Task 6: Run the Production Tool Lifecycle Gate

**Files:**
- Create: `tests/live/test_proxy_tool_loop.py`
- Modify: `docs/protocol.md`
- Modify: `docs/harnesses.md`

**Interfaces:**
- Consumes: complete proxy server and existing Claude login.
- Produces: end-to-end evidence for public HTTP pauses, retries, disconnects, repeated cycles, parallel calls, and cleanup.

- [ ] **Step 1: Add the live HTTP lifecycle matrix**

Parametrize Anthropic/OpenAI, streaming/non-streaming, 1/60/600-second pauses, normal/reversed results, one/two tool cycles, disconnect before/after terminal tool event, retry before/after result submission, timeout, deletion, and shutdown. Require exact IDs and no built-in tool/init exposure.

- [ ] **Step 2: Run short live cases**

Run: `RUN_LIVE_CLAUDE_TESTS=1 CLAUDE_PROXY_TEST_MODEL=sonnet uv run pytest tests/live/test_proxy_tool_loop.py -v -k 'not 600' -s`

Expected: PASS with no skipped selected cases.

- [ ] **Step 3: Run the long pause cases**

Run: `RUN_LIVE_CLAUDE_TESTS=1 CLAUDE_PROXY_TEST_MODEL=sonnet uv run pytest tests/live/test_proxy_tool_loop.py -v -k '600' -s`

Expected: PASS without a new SDK query or receive iterator during the wait.

- [ ] **Step 4: Update protocol documentation**

Document immutable tool sets, supported schema/result subset, exact intermediate heads, all-results-at-once requirement, timeout tombstones, retry behavior, OpenAI error limitation, and the capability-manifest gate.

- [ ] **Step 5: Run all verification and commit**

Run: `uv run pytest -v`

Expected: all unit/integration tests PASS.

Run: `uv run ruff check .`

Expected: no findings.

Run: `uv run mypy src/claude_sdk_proxy`

Expected: no errors.

```bash
git add tests/live/test_proxy_tool_loop.py docs/protocol.md docs/harnesses.md
git commit -m "test: verify end-to-end external tool lifecycle"
```
