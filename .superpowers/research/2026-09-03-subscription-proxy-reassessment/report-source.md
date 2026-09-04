# Research source: Claude subscription compatibility proxy reassessment

Date: 2026-09-03

## Decision question

How close can a private, local HTTP service get to an OpenAI- and
Anthropic-compatible inference endpoint while using the operator's existing
Claude subscription only through Anthropic's supported Claude Agent SDK or
`claude -p` surfaces?

The intended consumers are configurable agent harnesses such as Pi, Aider, and
Continue. Direct token extraction, private Claude Code wire emulation, client
identity spoofing, prompt-flattened history, and proxying local vLLM/SGLang
servers are excluded.

## Executive finding

The previous binary verdict was wrong in both directions:

1. Agent SDK authentication and incremental streaming work with this machine's
   Claude Max login when run in the normal host environment. The earlier
   authentication failure was caused by Codex sandbox isolation from macOS
   Keychain state, not by an Agent SDK or subscription prohibition.
2. A single caller-defined structured tool is experimentally bridgeable. A full JSON Schema tool
   registered as an in-process SDK MCP tool emits a structured tool-use event,
   including ID/name/arguments and a complete `message_stop`, while its async
   handler remains blocked. A local proxy can therefore return that call to the
   harness, keep the SDK session alive, and resolve the handler from the
   harness's next request. The handler receives arguments but no tool-use ID,
   so parallel and duplicate-call correlation remains a release-blocking gap.
3. Exact, general OpenAI/Anthropic emulation is still impossible through the
   supported SDK/CLI boundary. Arbitrary caller-supplied assistant history is
   rejected, and the Agent SDK does not expose direct equivalents for output
   `max_tokens`, temperature, top-p/top-k, stop sequences, or a native
   submit-external-tool-result operation.

The viable product is therefore an honest **stateful compatibility gateway for
fresh, linear harness sessions**, not a stateless drop-in clone of vLLM or the
commercial APIs.

## Evidence standard

- Official current Anthropic/OpenAI documentation and SDK source are primary.
- The installed runtime was inspected directly.
- Three bounded live probes used synthetic marker text, no real user content,
  no credential inspection, no filesystem tools, and no retained model output.
- GitHub issues are used only to qualify documented behavior and identify open
  implementation risks.

## Environment

- `claude-agent-sdk==0.2.148`, released 2026-08-28
- bundled Claude Code CLI `2.1.251`
- standalone Claude Code CLI `2.1.258`
- latest Python SDK observed on PyPI: `0.2.152`, bundling CLI `2.1.259`; the
  intervening releases are CLI-only bumps
- host authentication: `claude.ai`, Claude Max
- sandbox authentication: unavailable because the sandbox cannot see the
  normal macOS Keychain credential

Sources:

- [Python SDK release history](https://pypi.org/project/claude-agent-sdk/0.2.152/)
- [Official Python SDK changelog](https://github.com/anthropics/claude-agent-sdk-python/blob/main/CHANGELOG.md)
- [Claude Code authentication and credential storage](https://code.claude.com/docs/en/authentication)

## Claim and gap matrix

| Capability | Finding | Evidence | Confidence |
| --- | --- | --- | --- |
| Subscription auth | Pass on host; fail in Codex sandbox is an environment artifact | Host `claude auth status` reported `claude.ai`/Max; live SDK request passed. macOS credentials are stored in Keychain. | High |
| Subscription eligibility | Current subscription limits cover Agent SDK, `claude -p`, and third-party Agent SDK usage | Anthropic's 2026-06-15 update paused the proposed billing migration. | High, policy can change |
| Incremental text | Pass | Live SDK request produced two text deltas and exact terminal text; official SDK exposes raw parsed stream events. | High |
| Prompt replacement | Pass for caller text, subject to managed policy | SDK source passes `--system-prompt`; current docs say a custom string replaces the default. Filesystem sources, skills, tools, and MCP can be explicitly suppressed. | High |
| Total host isolation | Not universally provable | Managed policy can still load; several CLI controls are required. | High |
| Native linear multi-turn | Pass | Live `ClaudeSDKClient` probe returned exact markers across two requests with the same session ID. | High |
| Arbitrary message replay | Fail | Official streaming input type is user-only. A live assistant-input probe ended in `ResultError`, `terminal_reason=model_error`, with no text. | High |
| Resume SDK-created history | Pass | Public resume/fork/truncate and SessionStore APIs preserve valid Claude Code transcripts. | High |
| Import foreign history | Unsupported | `SessionStoreEntry` is opaque internal JSONL; import API only imports existing Claude sessions. | High |
| Basic caller tool schema | Experimental via MCP adaptation | Full JSON Schema accepted by a single-call live SDK tool probe. | High for schema registration only |
| Structured tool call emission | Pass | Live probe observed raw tool-use stream, ID, prefixed name, exact arguments, and `message_stop`. | High |
| External tool result continuation | Feasible through a blocked async handler | Handler remained pending until the probe released it after `message_stop`; SDK then completed successfully. | High for single-call linear flow |
| Native detached tool-result API | Absent | No public `submit_tool_result(id, result)` equivalent exists. | High |
| Parallel/duplicate tool calls | Unproven | Handler callback lacks tool-use ID; correlation needs hook/event bookkeeping and focused tests. | Medium |
| Exact tool names | Not exact internally | MCP names are always `mcp__<server>__<tool>`; proxy can map them back externally. | High |
| Output `max_tokens` | Not controllable exactly | SDK exposes task-wide token budget, not Messages/Chat output token cap. | High |
| Temperature/top-p/top-k/stops | Unsupported | Absent from current complete SDK option surface and CLI help. | High |
| Streaming wire fidelity | Translatable, not byte-native | SDK exposes parsed raw API events, not original HTTP headers/SSE bytes. | High |
| Automatic harness use | Configuration-only for many harnesses, not universal | Pi, Aider, and Continue expose custom base URL/provider controls. Transcript-only lookup cannot distinguish identical concurrent lineages. | High |

## Controlled live results

### Authentication and text streaming

The existing capability probe was rerun outside the Codex sandbox:

```text
backend: agent-sdk
authentication: pass
streaming: pass
prompt construction: pass
text deltas: 2
time to first delta: 1,077 ms
total: 2,245 ms
exact requested output: true
```

This supersedes the authentication/streaming row in
`docs/capabilities/2026-09-03-minimal-backend-capabilities.md`.

### Arbitrary assistant input

An SDK streaming input supplied one synthetic assistant message followed by a
user request. With custom empty system prompt, no built-ins, no settings,
skills, plugins, agents, or ambient MCP, the result was:

```json
{
  "accepted_without_exception": false,
  "exception_class": "ResultError",
  "result_is_error": true,
  "result_subtype": "success",
  "terminal_reason": "model_error",
  "text_delta_count": 0
}
```

The result confirms the documented user-only input type. It does not justify
hand-authoring Claude Code's explicitly internal transcript JSONL.

### Native multi-turn

A persistent `ClaudeSDKClient` received two user requests. The first stored a
synthetic marker; the second returned it exactly. Both result messages carried
the same session ID. No session persistence was used.

### Caller-defined tool boundary

The probe registered a single in-process MCP tool with a full JSON Schema and
an async handler blocked on an event. Before that event was released, the SDK
emitted:

- a raw tool-use stream with complete arguments;
- a nonempty tool-use ID;
- the internal name `mcp__compat_probe__probe_echo`;
- an `AssistantMessage` tool block; and
- the raw `message_stop` event.

After the synthetic external result released the handler, the SDK emitted its
tool-result user message and completed successfully. This proves one linear
external-tool continuation without prompt emulation or proxy execution of the
real tool. It does not prove a generic tool loop: the handler is args-only and
does not receive the tool-use ID needed to correlate parallel or identical
calls safely.

## What the Agent SDK actually is

Anthropic distinguishes the Client SDK from the Agent SDK: the Client SDK gives
direct Messages access and asks the caller to implement the tool loop; the
Agent SDK runs the Claude Code agent loop itself. See the [Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview)
and [agent-loop documentation](https://code.claude.com/docs/en/agent-sdk/agent-loop).

That distinction explains the mismatch. The Agent SDK can maintain its own
conversation and execute registered MCP handlers, but it is not a general
constructor for arbitrary API message arrays.

Current public session APIs materially improve the situation. SDK-created
sessions contain user messages, assistant responses, tool calls, and tool
results and can be resumed, forked, truncated, or mirrored to a SessionStore.
See [work with sessions](https://code.claude.com/docs/en/agent-sdk/sessions) and
[persist sessions to external storage](https://code.claude.com/docs/en/agent-sdk/session-storage).
They solve continuity for proxy-created sessions; they do not create a
supported foreign-transcript import path.

## Tool bridge analysis

Official custom tools are async in-process MCP handlers. Claude invokes the
handler and the handler returns content to the agent loop. See [custom tools](https://code.claude.com/docs/en/agent-sdk/custom-tools).

For this proxy, the handler need not execute the harness tool. It can act as a
continuation point:

1. Register each public tool schema as an SDK MCP tool.
2. Start a persistent SDK query.
3. When Claude calls a tool, the handler records the invocation and awaits an
   in-memory future.
4. Stream the structured SDK tool call to the HTTP client, mapping the MCP name
   back to the caller's original name.
5. Finish the HTTP response at the complete SDK `message_stop`, while the SDK
   query and handler remain alive.
6. On the harness's next HTTP request, validate the supplied tool-result IDs,
   resolve the matching futures, and stream the SDK's continuation.

This is adaptation, not prompt injection, token extraction, or execution of
the harness tool by the proxy.

Open risks requiring implementation-time tests:

- multiple parallel calls, especially identical calls to the same tool;
- exact correlation between hook/event IDs and handler invocations;
- cancellation while a handler is waiting;
- client disconnect and timeout cleanup;
- immutable tool schemas across one live session;
- SDK compaction and long-session behavior.

The newer `PreToolUse` `defer` feature is not the recommended MVP mechanism.
It can stop a process and preserve a pending call, but there is still no direct
caller-result submission method, and an open SDK issue reports that resumed
in-process MCP tools can become unavailable during startup. A long-lived local
session with blocked handlers is simpler and was directly demonstrated. See
[the open in-process defer/resume issue](https://github.com/anthropics/claude-agent-sdk-typescript/issues/370).

## Prompt and ambient-behavior boundary

The supported isolation configuration should use:

- caller string as `system_prompt` (or empty string);
- `tools=[]` to remove Claude Code built-ins;
- `setting_sources=[]`;
- `skills=[]`, `agents={}`, `plugins=[]`;
- only proxy-created MCP tools;
- `strict_mcp_config=True`;
- exact `allowed_tools=["mcp__<server>__<tool>", ...]` permission allowlisting;
- a fresh empty working directory per session;
- `restricted`, `disable-slash-commands`, and `no-session-persistence` while
  sessions remain in memory;
- `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`;
- `ENABLE_TOOL_SEARCH=false` for a small harness tool set so Claude sees the
  registered schemas directly rather than an internal discovery step.

Anthropic documents custom system-prompt replacement in [modifying system prompts](https://code.claude.com/docs/en/agent-sdk/modifying-system-prompts).
It documents the distinct CLI flags in the [CLI reference](https://code.claude.com/docs/en/cli-usage)
and MCP permission configuration in the [Agent SDK MCP guide](https://code.claude.com/docs/en/agent-sdk/mcp).
Do not use `safe-mode` for tool-enabled sessions: it disables MCP servers and
hooks, which the proposed bridge requires.

This removes normal Claude Code harness behavior. It cannot override
administrator-managed policy, and no supported surface proves the private
server-side prompt is byte-identical to the commercial Messages API.

## Harness compatibility

Configuration, not source modification, is enough for many harnesses:

- Pi supports custom `baseUrl`, API key, model metadata, and
  `openai-completions`, `openai-responses`, or `anthropic-messages`. It calls
  Chat Completions the most compatible mode. See [Pi custom models](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/models.md).
- Aider accepts an OpenAI-compatible base URL. See [Aider OpenAI-compatible APIs](https://aider.chat/docs/llms/openai-compat.html).
- Continue accepts `apiBase`; Chat Completions can be forced where Responses is
  the default. See [Continue OpenAI configuration](https://docs.continue.dev/customize/model-providers/top-level/openai).

This is not universal. A harness needs an overridable endpoint and must stay
inside the implemented protocol subset. Endpoint names alone are insufficient:
agent harnesses depend on role history, streamed tool arguments, finish reasons,
tool-call/result IDs, usage framing, errors, retries, and cancellation. The
[OpenAI Chat Completions reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create),
[Anthropic Messages reference](https://platform.claude.com/docs/en/api/http/messages/create),
and [Anthropic streaming reference](https://platform.claude.com/docs/en/build-with-claude/streaming)
define those obligations.

## Recommended product contract

### Build

Build a private localhost-only **Claude Subscription Harness Gateway** with the
Agent SDK as its sole production backend.

Implement OpenAI Chat Completions first, then the Anthropic Messages rendering
over the same session engine. Retain `/v1/models` and `/health`. Add the OpenAI
Responses API only after Chat Completions and tools pass real-harness fixtures.

### Session admission

- A new session accepts a system prompt plus a user turn.
- A continuation is accepted only when the client-supplied full transcript
  exactly matches a known proxy session head and appends one valid user turn or
  one complete set of pending tool results.
- Map heads to sessions by a canonical transcript fingerprint only in a
  constrained configuration-only mode. Treat every duplicate existing head as
  the same lineage: return `409` while it is in flight and replay the cached
  response after completion. A fingerprint cannot represent a second
  independent conversation with identical history.
- Offer an optional explicit proxy session header or provider adapter for
  robust concurrency and independent identical conversations.
- Cache completed responses so duplicates of a completed head replay safely.
- Reject imported assistant history, edited history, branch-from-old-head, tool
  schema changes, or concurrent mutation of one lineage with stable errors.
- Keep processes and state in memory for the MVP. Server restart ends sessions.

### Supported request subset

- exact text system/user/assistant content generated through this proxy;
- streaming and non-streaming text;
- experimental, feature-gated single-call tools with stable JSON Schema and
  text tool results; do not advertise general tool support until correlation,
  cancellation, timeout, disconnect, parallel, and duplicate-call tests pass;
- tool choice absent/automatic;
- one configured Claude model allowlist;
- thinking/effort only where the SDK has a direct option.

Accept the protocol-required `max_tokens` field for compatibility, but document
that the backend cannot enforce it as an output cap. Reject explicit sampling
and stop controls that cannot be honored. Never silently pretend those controls
worked.

### Do not build

- a second `claude -p` production backend;
- arbitrary transcript synthesis using internal JSONL;
- OAuth/token extraction or private Claude wire emulation;
- prompt serialization of assistant/tool history;
- durable journals, databases, crash recovery, account rotation, or model
  forwarding in the MVP.

`claude -p` uses the same underlying Claude Code engine but exposes less useful
typed session/tool control. Keep it only as a diagnostic comparison.

## Compatibility promise

The honest promise is:

> Works with configurable OpenAI Chat Completions and Anthropic Messages
> clients for fresh, linear, proxy-managed text sessions. Experimental
> single-call tools are gated. Configuration-only mode supports one unambiguous
> lineage per identical transcript head; robust concurrency requires an
> explicit proxy session identifier. Does not implement arbitrary stateless
> history replay or all generation controls.

It must not be described as an exact vLLM, SGLang, OpenAI, or Anthropic server.

## Policy boundary

Anthropic's current help article says Agent SDK, `claude -p`, and third-party
Agent SDK usage still draw from subscription limits after the planned billing
change was paused: [subscription usage update](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan).

The Agent SDK overview separately says third-party developers may not offer
Claude.ai login or rate limits to their own users without prior approval. This
project avoids that boundary by remaining private, local, single-user software
that relies on the operator's already-authenticated Claude installation. It
must not be exposed as a shared or public subscription-backed service.

## Proposed next proof sequence

1. Replace the stale capability verdict with the corrected matrix.
2. Update the pinned SDK from 0.2.148 to current 0.2.152 and rerun the bounded
   probes; releases 0.2.149-0.2.152 only advance the bundled CLI.
3. Build text-only in-memory sessions first: duplicates join one lineage (`409`
   in flight, replay after completion), with an optional explicit session
   identifier for independent identical conversations.
4. Put the single-call external tool bridge behind an experimental flag.
5. Prove one Pi session end-to-end: text turn, tool call, tool result, final
   answer, retry, cancellation, timeout, and disconnect.
6. Prove parallel and duplicate tool-call correlation or explicitly reject it
   before advertising general tool support.
7. Add Anthropic rendering and run the equivalent fixture.
8. Only then widen content types, session branching, persistence, or Responses.

## Overall assessment

- Exact generic API clone: **not achievable** through supported Agent SDK/CLI
  surfaces.
- Practical fresh-session text gateway: **achievable and supported by direct
  evidence**. General external-tool compatibility remains experimental.
- Recommended direction: **proceed, but reset the contract and implementation
  around a small stateful Agent SDK bridge**.
