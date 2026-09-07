# Claude subscription proxy reassessment

Date: 2026-09-03

> **Historical design checkpoint:** This report motivated the initial SDK
> gateway. Later implementation added caller tools, full-transcript import and
> rebasing, Pi compaction, images, and model/thinking controls. Restrictions and
> unproven capabilities below describe this date, not the current product.
> Use the [root README](../../README.md) and [documentation index](../README.md)
> for current usage and verification; no custom Pi adapter is required.

## Bottom line

We should proceed, but with a narrower and more accurate product definition.

The Claude Agent SDK works with the operator's Claude Max login, streams
incrementally, maintains multi-turn sessions, and can expose one caller-defined
structured tool call before its async handler returns. That is enough to build
a useful local text endpoint and an experimental, gated tool path.

It cannot act exactly like a stateless vLLM/SGLang or commercial
OpenAI/Anthropic endpoint. The supported SDK rejects arbitrary assistant-history
input and lacks exact controls for output `max_tokens`, temperature, top-p,
top-k, and stop sequences.

The right target is a **private, stateful compatibility gateway for fresh,
linear harness sessions**, not a universal API clone.

## What changed from the previous verdict

| Earlier conclusion | Fresh evidence | Corrected conclusion |
| --- | --- | --- |
| Agent SDK auth failed | Host execution succeeds with `claude.ai`/Max; only the Codex sandbox cannot see the macOS Keychain credential | Authentication passes in the real deployment environment |
| Agent SDK streaming failed | Live request produced incremental deltas and an exact result | Streaming passes |
| Multi-turn failed | A persistent SDK client recovered an exact marker on turn two with the same session ID | Native proxy-managed linear sessions pass; arbitrary replay still fails |
| Structured tools failed | A single JSON Schema MCP tool emitted ID/name/arguments and `message_stop` while its handler remained blocked | One linear bridge is proven; generic, parallel, and duplicate-call handling remains unproven |

Anthropic documents macOS Keychain storage in its [authentication guide](https://code.claude.com/docs/en/authentication),
user-only streaming input in the [Agent SDK input guide](https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode),
native continuity in [work with sessions](https://code.claude.com/docs/en/agent-sdk/sessions),
and JSON Schema custom tools in [give Claude custom tools](https://code.claude.com/docs/en/agent-sdk/custom-tools).

## The workable architecture

```text
Pi / Aider / Continue
        |
        | OpenAI Chat Completions or Anthropic Messages
        v
localhost compatibility renderer
        |
        | canonical transcript-head fingerprint
        v
in-memory session registry
        |
        | one persistent ClaudeSDKClient per active conversation
        v
Claude Agent SDK -> authenticated Claude subscription
        |
        | experimental caller tool emitted; async MCP handler waits
        v
harness executes tool -> next HTTP request resolves handler -> Claude continues
```

For each fresh conversation, the proxy starts an isolated SDK session with a
caller system prompt, no Claude Code built-in tools, no project/user settings,
no skills, plugins, agents, or ambient MCP servers. Tool-enabled sessions must
not use `--safe-mode`, because current Anthropic documentation says it disables
MCP servers and hooks. Instead, use restricted mode plus `setting_sources=[]`,
`tools=[]`, `strict_mcp_config=True`, and an exact `allowed_tools` list containing
only the generated `mcp__<server>__<tool>` names. Caller tool schemas become
in-process MCP tools whose handlers are continuation points, not tool executors.

In the proven single-call case, when Claude calls a tool, the proxy streams the
structured call to the harness and closes that HTTP response only after the
SDK's complete `message_stop`. The SDK process remains alive with its handler
awaiting a future. The harness executes its own tool and sends the normal
tool-result message on its next API request; the proxy validates the ID and
resolves the future. The handler itself receives arguments but no tool-use ID,
so parallel or identical calls cannot be promised until correlation and cleanup
tests pass.

## How an ordinary harness finds the session

Standard Chat Completions and Messages requests resend the full conversation
but do not carry a portable server session ID. The proxy can offer a constrained
configuration-only mode by hashing the canonical transcript:

- after every response, record `transcript fingerprint -> live SDK session`;
- on the next request, require the supplied history to equal a known head plus
  one new user turn or one complete tool-result set;
- treat every duplicate existing transcript head as the same lineage: return
  `409` while it is in flight and replay its cached response after completion;
- reject edited/imported history, stale branching, and concurrent mutation.

This supports append-only behavior without flattening history into a prompt,
but only while each identical transcript head is treated as the same live
lineage. A hash cannot distinguish a retry from a second independent
conversation with identical history, so that second conversation is
unrepresentable in configuration-only mode. Robust concurrent use requires an
optional proxy session header (where a harness supports custom headers) or a
small provider adapter. The proxy cannot support an arbitrary conversation that
did not originate through it.

## Harness impact

Most configurable harnesses do not need source changes, but they do need one
provider configuration:

- Pi supports custom `baseUrl`, model, API key, and `openai-completions` or
  `anthropic-messages`; it labels Chat Completions its most compatible mode.
  See [Pi custom models](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/models.md).
- Aider accepts an OpenAI-compatible base URL. See [Aider configuration](https://aider.chat/docs/llms/openai-compat.html).
- Continue accepts `apiBase` and can be configured to use Chat Completions.
  See [Continue configuration](https://docs.continue.dev/customize/model-providers/top-level/openai).

Harnesses without a configurable base URL, or those that require Responses or
unsupported API features, still need an adapter or later endpoint work.

For Pi, the initial provider should use `openai-completions`, a dummy local API
key, `supportsDeveloperRole: false`, and no reasoning/sampling options until
their mappings are verified.

## Honest capability boundary

| Capability | MVP status |
| --- | --- |
| Existing Claude subscription authentication | Yes, when the proxy runs in the user's normal host context |
| Text streaming/non-streaming | Yes |
| Exact caller system string | Yes, except unavoidable managed policy |
| Fresh append-only multi-turn | Yes |
| Basic caller tools and text results | Experimental/gated; one linear call is proven, but correlation and cleanup are not production-ready |
| Arbitrary imported assistant/tool history | No |
| Conversation edits or branching | No initially |
| Exact `max_tokens` enforcement | No; accept as advisory because Anthropic Messages requires it |
| Temperature/top-p/top-k/stop sequences | No; reject explicit use |
| Exact internal tool name | No; SDK sees MCP-prefixed names, proxy maps them back externally |
| Restart recovery | No initially |
| Public or multi-user subscription service | No |
| Multiple identical concurrent conversation heads | No in configuration-only mode; use an explicit proxy session identifier |

OpenAI and Anthropic compatibility entails more than endpoint names. Their
protocols define role history, tool-call/result correlation, streamed arguments,
finish reasons, usage, and errors. See the [OpenAI Chat Completions reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create),
[Anthropic Messages reference](https://platform.claude.com/docs/en/api/http/messages/create),
and [Anthropic streaming reference](https://platform.claude.com/docs/en/build-with-claude/streaming).

## Why not `claude -p`

`claude -p` is the same underlying Claude Code engine with a less useful
programmatic interface. The Agent SDK already supplies typed events, streaming,
session control, cancellation, MCP handlers, and subscription authentication.
Maintaining two production backends adds complexity without closing the missing
arbitrary-history or generation-control gaps. Keep `claude -p` only as a
diagnostic comparator.

## Why not deferred-tool resume

The newer `PreToolUse` defer mechanism is promising for process-per-request
durability, but it still lacks a direct submit-external-result API. An open
issue also reports that resumed in-process MCP tools can be unavailable during
startup: [Agent SDK issue #370](https://github.com/anthropics/claude-agent-sdk-typescript/issues/370).
The demonstrated long-lived session plus blocked handler is simpler for a local
MVP.

## Policy and deployment

Anthropic's current help-center update says Agent SDK, `claude -p`, and
third-party Agent SDK usage still draw from subscription limits:
[use the Agent SDK with your Claude plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan).

Anthropic also says third-party developers cannot offer Claude.ai login or
subscription rate limits to their own users without approval. This project must
therefore remain localhost-only, private, and single-user, relying on the
operator's already-authenticated installation rather than offering login.

## Recommended next implementation slice

1. Correct the stale capability report.
2. Update the pinned Agent SDK from 0.2.148 to current 0.2.152 and rerun the
   bounded probes.
3. Remove `claude -p` from the production design.
4. Implement text-only in-memory sessions first: duplicates join one lineage
   (`409` in flight, replay after completion), with an optional explicit session
   identifier for independent identical conversations.
5. Keep tools behind an experimental flag; implement only the proven
   single-call continuation path.
6. Run a real Pi fixture: first text turn, tool call, tool result, final answer,
   retry, timeout, cancellation, and disconnect.
7. Prove or reject parallel and duplicate tool calls explicitly before calling
   tools supported.
8. Add Anthropic rendering over the same session engine.
9. Consider `/v1/responses`, persistence, and branching only after the core
   path works end to end.

The full evidence record is in
`.superpowers/research/2026-09-03-subscription-proxy-reassessment/report-source.md`.
