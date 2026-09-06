# Caller-Owned Tools for the Claude Subscription Gateway

Date: 2026-09-05

## Purpose

Add native caller-owned tool calling to the existing localhost OpenAI- and
Anthropic-compatible gateway. A normal agent harness supplies tool definitions,
receives structured tool calls, executes the tools itself, and returns structured
results. The proxy never executes the caller's operation and never emulates tools
with JSON instructions in the prompt.

This is an in-memory extension of the merged text gateway. It does not revive the
old native lifecycle, attestation, evidence-manifest, persistent journal, or crash
recovery architecture. The server remains an experimental, trusted-local,
single-user process backed by the operator's authenticated Claude Agent SDK.

## Approved product decisions

- Tool execution, approval, retries, and side effects belong to the calling
  harness.
- Both Anthropic Messages and OpenAI Chat Completions expose their normal tool
  shapes.
- A single assistant turn may contain multiple calls, including identical calls
  to the same tool. Results may arrive in any order.
- Tool results may contain text or JSON serialized as text. Anthropic results may
  carry `is_error`; OpenAI has no equivalent standard flag, so errors remain text
  at that dialect boundary.
- Multimodal tool results are rejected rather than flattened.
- A harness needs only its usual provider configuration. The existing optional
  `x-claude-proxy-session` header improves disambiguation but is not required.
- Caller tools are the only tools exposed to Claude. Claude Code built-ins,
  skills, plugins, subagents, ambient MCP servers, and filesystem settings remain
  disabled.
- Caller tool schemas travel through the SDK's native MCP tool mechanism. The
  proxy adds no natural-language tool instructions.

## Non-goals

- Proxy-owned or proxy-executed tools
- Claude Code built-in tools
- Prompt-encoded JSON tool emulation
- OpenAI Responses API support
- Images, files, resources, or other multimodal tool results
- Changing tools, model, system prompt, or API dialect within a conversation
- Imported conversations that did not originate through this running proxy
- Conversation branching or durable restart recovery
- Forced, required, named, or disabled-per-turn tool choice
- Guaranteeing serial execution when `parallel_tool_calls` is false

## Public API subset

### Anthropic Messages

Requests may add:

- `tools`: an array of objects containing `name`, `description`, and an
  object-valued JSON Schema in `input_schema`; an empty array is equivalent to
  omitting tools on a fresh text-only conversation;
- `tool_choice`: omitted or `type: "auto"` with
  `disable_parallel_tool_use` omitted or `false`;
- assistant `tool_use` content blocks in prior history;
- user `tool_result` content blocks containing a string or text blocks and an
  optional Boolean `is_error`.

Responses may contain text and `tool_use` blocks. Streaming uses the standard
`content_block_start`, `input_json_delta`, and `content_block_stop` events and
finishes the message with `stop_reason: "tool_use"`. The complete arguments may
be delivered as one JSON delta after generation rather than as token-sized
fragments.

### OpenAI Chat Completions

Requests may add:

- `tools`: an array of `type: "function"` definitions; an empty array is
  equivalent to omitting tools on a fresh text-only conversation;
- `tool_choice`: omitted or `"auto"`;
- `parallel_tool_calls`: omitted or `true`;
- assistant `tool_calls` history;
- one or more contiguous `role: "tool"` result messages with `tool_call_id` and
  string `content`.

Responses may contain text and `tool_calls`. Streaming uses `delta.tool_calls`
and ends with `finish_reason: "tool_calls"`. JSON tool results use the normal
OpenAI convention: the harness serializes the JSON value into the tool message's
string content.

### Rejected controls and shapes

The adapters return a stable HTTP 400 `unsupported_feature` error for supported
wire shapes that request behavior outside this subset:

- forced, required, named, or `none` tool choice;
- explicit `parallel_tool_calls: false`;
- non-function OpenAI tools;
- image, document, resource, or other non-text result blocks.

Malformed values and invalid completeness constraints return HTTP 400
`invalid_request`, including:

- non-object or otherwise invalid input schemas and unknown schema dialects;
- external or relative JSON Schema references (self-contained fragment
  references such as `#/$defs/item` and local anchors are pre-resolved; the
  proxy never resolves schemas over the network or filesystem);
- user text mixed into a pending tool-result continuation;
- Anthropic `is_error: true` results with no non-empty text (the backend rejects
  empty error content; empty successful results remain valid);
- partial result sets.

Tool names use Anthropic's portable constraint `[A-Za-z0-9_-]{1,64}`. Names
must be unique within one request. The parser caps a request at 128 tool
definitions, each UTF-8 description at 8 KiB,
each canonical JSON Schema at 64 KiB, and all schemas together at 512 KiB. Each
generated argument object is capped at 256 KiB of canonical JSON. Each tool result
is capped at 256 KiB of UTF-8 text and a complete result set at 1 MiB. Exceeding
a caller-controlled limit returns HTTP 400; an oversized model-generated argument
object is an SDK protocol failure.
Every schema and argument JSON value is limited to 64 nested containers. The
root mapping or array has depth 1, and each nested mapping or array adds 1;
depth 64 is accepted and depth 65 is rejected. Caller definitions and history
return HTTP 400, while model-generated over-depth arguments are a redacted SDK
protocol failure.

## Canonical model

The current string-only canonical message becomes a sequence of typed blocks:

```python
TextBlock(text: str)
ToolCallBlock(id: str, name: str, arguments: dict[str, object])
ToolResultBlock(tool_call_id: str, content: tuple[str, ...], is_error: bool)
CanonicalMessage(role: Literal["user", "assistant"], blocks: tuple[...])
```

OpenAI's contiguous `role: "tool"` messages normalize into one canonical user
tool-result turn. Anthropic already expresses all results as blocks in one user
turn. A pending continuation must contain all and only the pending public call
IDs, exactly once. Result order is irrelevant because correlation is by ID.

Tool definitions are canonicalized without changing their JSON Schema values.
Every request in a tool-enabled conversation must resend the same non-empty
definition set; omission, an empty array, or any change is a session mismatch.
The transcript fingerprint includes the dialect, model, system prompt, exact
tool definitions, all block types, public call IDs, arguments, results, and error
flags. This makes model, system, schema, result, or dialect changes explicit
session mismatches instead of silent mutations.

## Architecture

```text
Harness HTTP request
        |
        v
Anthropic/OpenAI adapter
        | canonical transcript
        v
SessionRegistry -------- transcript fingerprint
        |
        v
ToolSessionActor -------- sole SDK stream/state owner
        |
        v
in-process MCP ToolBridge
        | one future per invocation
        v
Harness executes tool and returns result
```

### `ToolBridge`

`tool_bridge.py` converts the immutable caller definitions into one in-process
SDK MCP server. It advertises only those tools and maps each public name to the
SDK's generated `mcp__<server>__<name>` name.

Every handler invocation is a distinct coroutine. On invocation it:

1. validates the arguments supplied by the SDK;
2. mints a dialect-appropriate, opaque public call ID;
3. records a `ToolInvocation` containing that ID, original name, and arguments
   in the current bridge epoch;
4. awaits the future associated with that public ID;
5. returns the harness-provided text blocks and error flag as an MCP result.

The public ID belongs to the handler invocation, not to a `(name, arguments)`
lookup. Two identical calls therefore have independent futures, and reverse-order
results cannot be confused. The internal-only exception is the SDK callback's
private `_meta["claudecode/toolUseId"]`: the bridge requires it to equal the
unique raw and typed assistant tool-use ID and requires the callback name and
canonical arguments to match that raw call. This establishes the exact
bijection needed when the SDK serializes callbacks for parallel blocks. The ID
is never exposed as a public call ID, accepted from a caller, or included in an
error or log, and caller results still correlate only by independently minted
public IDs. There is no name, argument, order, or guessing fallback.

After result echo validation, an epoch remains sealed until every raw call has
entered exactly one metadata-validated callback and its stored caller result has
returned through that callback. An arriving assistant or result boundary wins a
race against an incomplete callback barrier and fails the session without
committing that boundary. A later tool-generating boundary opens the next epoch;
terminal text does not reopen callback admission.
Completed epoch invocation/argument collections are cleared at that barrier;
the bridge does not keep a lifetime publication history.

### `ToolSessionActor`

The actor is the only consumer of both the SDK message stream and bridge
notifications. This prevents an HTTP request, handler, and cleanup task from
racing to consume or mutate the same session.

Its states are:

```text
READY -> GENERATING -> READY
                    -> WAITING_FOR_TOOLS -> GENERATING
any nonterminal state -> CLOSED
```

During generation the actor forwards validated text deltas. It observes native
SDK tool-use blocks but buffers public tool calls until the SDK's complete
`message_stop`. At that boundary the session seals every raw/typed SDK ID against
the callback's private metadata ID, public name, and canonical arguments, while
allowing an exact placeholder for an SDK-serialized deferred callback. Any
missing, duplicate, extra, or mismatched association is an SDK protocol failure
and closes the session; there is no fallback correlation.

After validation, the actor emits the bridge calls in stable invocation order,
commits the canonical assistant turn, caches the response for retry, ends the
public response at the tool boundary, and enters `WAITING_FOR_TOOLS`. The SDK
operation and MCP handler futures remain alive.

When the next HTTP request supplies a valid complete result set, the actor binds
that request to the already-running SDK operation, commits the canonical result
turn, and resolves each future by public ID. The SDK receives the MCP results and
continues generation. Another tool boundary repeats the same cycle; a normal
assistant completion returns the actor to `READY`.

### `SessionRegistry`

The existing implicit transcript matching remains the default. An exact replay
of a completed request returns the cached result and never invokes a tool again.
An optional `x-claude-proxy-session` value continues to provide explicit lineage
when a client can retain custom headers.

`WAITING_FOR_TOOLS` sessions are busy and cannot be evicted. They occupy one of
the existing `--max-sessions` slots. Idle completed sessions remain eligible for
least-recently-used eviction. A fresh request returns the existing HTTP 503
`session_capacity` error when every slot is generating or waiting.

## SDK configuration and prompt effects

A tool-enabled session preserves the text gateway's isolation options but
replaces the empty MCP configuration with its one generated server:

```python
ClaudeAgentOptions(
    system_prompt=caller_system_or_empty,
    tools=[],
    allowed_tools=[generated caller MCP names only],
    skills=[],
    setting_sources=[],
    mcp_servers={generated_server_name: in_process_server},
    strict_mcp_config=True,
    permission_mode="dontAsk",
    agents={},
    plugins=[],
    max_buffer_size=8 * 1024 * 1024,
    # existing empty cwd, disabled memory/slash commands/persistence
)
```

The 8 MiB internal line buffer is derived from the 1 MiB aggregate UTF-8 result
limit: JSON escaping can expand a one-byte control character to six wire bytes,
and the remainder covers the SDK message envelope. This does not increase any
public schema, argument, or result limit.

The proxy does not enable general MCP discovery or any Claude Code built-in.
Tool schemas are necessarily visible to Claude, and Anthropic automatically adds
its native tool-use system instructions when tools are present. Those schema and
provider-added tokens are unavoidable on the Agent SDK path; the proxy must not
add a second prose description or JSON-output instruction.

## HTTP lifecycle, retries, and cleanup

- Only one public mutation of a conversation may be in flight at a time.
- A duplicate of an in-flight request returns HTTP 409. A completed duplicate replays
  the cached canonical response with the same public tool IDs.
- Invalid or incomplete tool results return HTTP 400 without destroying the
  waiting session, allowing a corrected retry before timeout.
- Transcript, schema, model, system, dialect, stale-head, and explicit-session
  conflicts return HTTP 409 `session_mismatch` or `session_conflict`.
- Session-capacity exhaustion remains HTTP 503.
- SDK start/protocol/termination failures return HTTP 502 and close the session.
- Generation or tool-result-wait expiration returns HTTP 504 when a request is
  attached; otherwise it closes the parked session. A later continuation returns
  HTTP 409 because its live lineage no longer exists.

The existing turn timeout remains 300 seconds. A separate configurable
`--tool-result-timeout` also defaults to 300 seconds and starts after each tool
boundary is committed. A valid but malformed retry does not reset it. Each new
tool boundary starts a fresh wait deadline.

Disconnect before a response commits cancels the generation and closes the
session. Once a tool-boundary response has committed, closing that completed HTTP
connection does not cancel the parked SDK operation. If a continuation disconnects
after its results have been committed to the SDK, the actor continues consuming
the generation into the replay cache; the harness can retry the identical request
without executing tools again.

Shutdown or timeout cancels all pending handler futures, disconnects the SDK, and
cleans the temporary directory through the existing bounded teardown mechanism.
There is no recovery after process restart.

## Module changes

- `domain.py`: typed content blocks, canonical tools, calls, results, and events
- `tool_bridge.py`: in-process MCP registration, invocation IDs/futures, result
  delivery, and cancellation
- `sdk_session.py`: tool-aware SDK setup and validated native tool events
- `session_turn.py`: actor states and public-boundary commit behavior
- `sessions.py`: tool-aware fingerprints, matching, replay, and busy-session
  capacity
- `anthropic_api.py`: strict tool parsing and Anthropic response/SSE rendering
- `openai_api.py`: strict function-tool parsing and Chat Completions rendering
- `app.py`: route tool continuations through the existing lease/monitor flow
- `cli.py`: `--tool-result-timeout`
- `docs/feasibility/README.md`: supported tool subset, configuration, limits, and
  examples

No new database, subprocess supervisor, durable state layer, attestation format,
or alternate backend is introduced.

## Verification

### Unit tests

- tool-definition and JSON Schema validation;
- canonical block conversion and fingerprints;
- dialect-specific call IDs and rendering;
- malformed, duplicate, unknown, missing, and stale results;
- unsupported tool-choice and multimodal shapes;
- Anthropic error-result preservation and OpenAI's documented text-only error
  limitation.

### Actor and bridge tests

- one call and one result;
- multiple distinct parallel calls;
- two identical calls with different results returned in reverse order;
- mixed text and tool calls;
- repeated tool rounds;
- SDK/handler call-set mismatch;
- duplicate request replay without handler reinvocation;
- disconnect before and after result commit;
- tool-result timeout, shutdown cancellation, and capacity exhaustion.

### HTTP compatibility tests

Run complete streaming and non-streaming loops through both endpoints using the
official OpenAI and Anthropic clients. Existing text-only tests remain unchanged
and must all pass.

### Live subscription tests

Against the authenticated installed Agent SDK, prove:

1. a single caller tool round trip;
2. identical parallel calls with distinguishable reverse-order results;
3. mixed text and tool output;
4. explicit tool errors;
5. repeated tool rounds;
6. replay after a continuation disconnect;
7. timeout cleanup;
8. only the generated caller MCP tools are exposed;
9. a Pi-agent tool loop works with provider configuration only.

General tool support is complete only when the identical-parallel reverse-result
case passes through both public dialects. A failure is fixed or reported as a
hard unsupported boundary; the implementation must never correlate calls by
guessing from identical names or arguments.

## Acceptance criteria

- Both endpoints complete the live tool matrix while preserving their documented
  wire shapes.
- The harness, not the proxy, performs every external operation.
- Parallel and identical calls correlate only by unique public IDs.
- No built-in, ambient, skill, plugin, subagent, or connector tool becomes
  available.
- The proxy adds no tool instructions to the system or user prompt.
- Text-only behavior and the complete existing test suite remain green.
- The implementation remains an in-memory extension of the current gateway and
  contains none of the superseded recovery architecture.

## Superseded plan

This design replaces `docs/superpowers/plans/2026-08-29-phase-3-external-tools.md`
as the direction for caller-owned tools. That historical plan remains in git for
context but is not an implementation dependency.

## References

- [Anthropic Agent SDK custom tools](https://code.claude.com/docs/en/agent-sdk/custom-tools)
- [Anthropic tool definitions](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools)
- [Anthropic tool-use overview](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview)
- [Anthropic Messages API](https://platform.claude.com/docs/en/api/http/messages/create)
- [OpenAI Chat Completions API](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
