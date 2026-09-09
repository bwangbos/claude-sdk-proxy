# Quaylet Claude gateway reference and historical probes

This reference's SDK session, fallback, and parked-tool rules are specific to
Claude-backed models. Quaylet also has an opt-in direct ChatGPT backend; use its
[provider guide](../openai-subscription.md) for Astra/Sol authentication,
reasoning, stateless tool continuation, accounting, and replay behavior.

This package ships a single-user localhost text/image-and-caller-tool gateway
and retains the earlier trusted-local feasibility probes as historical comparator
evidence. The authoritative offline release gate, including the real Pi provider integration,
is:

```console
make release-offline
```

Live subscription checks remain separately opt-in.

For installation and the canonical Pi configuration, start with the
[root README](../../README.md). This page supplies detailed tool/session behavior;
the [documentation index](../README.md) separates current references from archives.

## Current runnable gateway

The current implementation is a local, single-user compatibility gateway for
fresh, linear text/image and caller-owned tool conversations. It uses the Claude Agent
SDK and the Claude login already available to the process. Run it from the same
normal host login context where `claude` is authenticated; a sandboxed process
may not be able to read the macOS Keychain item even though the CLI works in a
terminal.

Install the locked dependencies and launch the default model on loopback:

```bash
uv sync --dev
uv run quaylet --model sonnet-5
```

The server listens at `http://127.0.0.1:8317`. It exposes
`POST /v1/chat/completions`, `POST /v1/messages`, `GET /v1/models`, and
`GET /health`. `--host` accepts loopback IP addresses only. Repeat `--model`
to expose more than one pinned model. The canonical choices are `sonnet-5`,
`opus-5`, and `opus-4.8`; legacy `sonnet`/`opus` inputs normalize to version 5.
`/v1/models` lists only configured canonical names, not the account's full model
catalog. `--max-sessions` sets the positive retained-session limit and defaults
to 8.
`--tool-result-timeout` sets the positive finite number of seconds a suspended
tool operation can wait for caller results and defaults to `300.0`. Both POST
endpoints require `Content-Type: application/json`; normal media-type parameters
such as `charset=utf-8` are accepted.

### Caller-owned tool examples

The gateway publishes calls and remains suspended; the HTTP caller executes the
tool and continues with the exact public call ID. For Anthropic Messages, start a
turn with:

```bash
curl http://127.0.0.1:8317/v1/messages \
  -H 'Content-Type: application/json' \
  -H 'X-Quaylet-Session: example-anthropic' \
  -d '{
    "model":"sonnet-5",
    "max_tokens":256,
    "messages":[{"role":"user","content":"Use lookup once for Boston."}],
    "tools":[{"name":"lookup","description":"Look up a city","input_schema":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"],"additionalProperties":false}}]
  }'
```

Append the returned assistant `content` unchanged, execute every `tool_use`, then
append one user message containing exactly one `tool_result` per returned ID:

```json
{
  "model": "sonnet-5",
  "max_tokens": 256,
  "messages": [
    {"role": "user", "content": "Use lookup once for Boston."},
    {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_RETURNED_ID", "name": "lookup", "input": {"city": "Boston"}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_RETURNED_ID", "content": "sunny"}]}
  ],
  "tools": [{"name": "lookup", "description": "Look up a city", "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"], "additionalProperties": false}}]
}
```

For OpenAI Chat Completions, the equivalent first request is:

```bash
curl http://127.0.0.1:8317/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'X-Quaylet-Session: example-openai' \
  -d '{
    "model":"sonnet-5",
    "messages":[{"role":"user","content":"Use lookup once for Boston."}],
    "tools":[{"type":"function","function":{"name":"lookup","description":"Look up a city","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"],"additionalProperties":false}}}]
  }'
```

Append its assistant message unchanged and then a contiguous tool result for
each returned `tool_call_id`:

```json
{
  "model": "sonnet-5",
  "messages": [
    {"role": "user", "content": "Use lookup once for Boston."},
    {"role": "assistant", "content": null, "tool_calls": [{"id": "call_RETURNED_ID", "type": "function", "function": {"name": "lookup", "arguments": "{\"city\":\"Boston\"}"}}]},
    {"role": "tool", "tool_call_id": "call_RETURNED_ID", "content": "sunny"}
  ],
  "tools": [{"type": "function", "function": {"name": "lookup", "description": "Look up a city", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"], "additionalProperties": false}}}]
}
```

Send each tool-result continuation to the same endpoint and with the same model,
thinking configuration, refusal-fallback policy, system string, dialect, and
canonical tool definitions as the request that produced the calls. Model/thinking may change after the
assistant finishes that turn; system/tools/dialect remain fixed. Parallel
results may be returned in any order, but correlation is strictly by the public
ID. Never infer correlation from tool name, arguments, or position.

After auto fallback, keep the request's model unchanged for tool-result
continuations: `model: "opus-5"` remains the requested route even when the
response's actual model is `opus-4.8`. The known live session keeps using the
validated replacement model.

The SDK's private MCP callback metadata carries its native tool-use ID. The
gateway validates that ID against the raw and typed assistant blocks and uses it
only to establish an exact internal bijection, including when the SDK dispatches
parallel tool blocks through serial callbacks. It is never exposed as the public
call ID or in errors. Public IDs are minted independently, and caller results
still correlate only by those opaque public IDs; there is no name, argument, or
position fallback.

After the SDK echoes submitted results, the gateway accepts no later assistant
or final boundary until every raw call has entered exactly one validated
callback and the stored result has returned through it. The epoch stays sealed
through terminal text and reopens only if a later boundary generates tools. A
missing deferred callback therefore loses the session rather than allowing a
terminal answer to commit.

### Images

Both endpoints accept inline PNG, JPEG, GIF, and WebP user inputs. Anthropic
uses native base64 `image` blocks; OpenAI uses `image_url` blocks with base64
data URLs. Successful tool results can include images: native Anthropic
`tool_result.content` blocks, or the proxy's OpenAI extension accepting a
text/`image_url` array in a tool message. Image bytes and order are preserved
through SDK transport, replay identity, and imported history. Return error
results as text; image error results are unsupported.

The entire submitted history is limited to 20 images, 3 MiB decoded per image,
and 12 MiB decoded overall. Encoding, size, and MIME signatures are checked
locally; the backend validates image decoding and dimensions. No image URL
fetching, PDF/audio/video input, image generation, or non-`auto` OpenAI image
detail controls are provided.

For Pi image attachments and screenshot tools, use the
[Anthropic vision provider configuration](../../README.md#pi-with-images-and-screenshots).
Pi's OpenAI provider detaches image tool results into an additional user turn;
the gateway does not guess their tool IDs. Anthropic system text-block arrays
and ephemeral content-block cache hints are accepted for stock Pi compatibility.
Hints are advisory and do not control backend caching.

### Pi configuration, thinking, and compaction

Merge the [canonical Pi provider entries](../../README.md#configure-pi) into
`~/.pi/agent/models.json`; they cover the three pinned models in both API dialects,
verified thinking-level maps, and image inputs on all three models. Opus 4.8
image inputs and tool results have also been live-verified. This reference deliberately
links to one maintained configuration rather than duplicating it.

Start the three-model proxy in one terminal (stop any existing server on that port
first), then start Pi in another terminal with its ordinary tool set enabled:

```bash
uv run quaylet --model sonnet-5 --model opus-5 --model opus-4.8
pi --provider claude-subscription-local --model sonnet-5
```

The dummy API key is deliberately non-secret; the gateway ignores it and uses
the operator's local Claude login. Pi's ordinary native tool definitions and
loop are supported. Use `/model` within the same provider and `/thinking` between
completed assistant turns; do not configure unsupported sampling options. The Pi
provider's normal `store: false` and streaming-usage fields are accepted, as is
the advisory `max_tokens` field added by Pi's ordinary `streamSimple` path.

Pi 0.85.1's automatic context compaction and `/compact` both work through the
normal provider configuration. When Pi replaces old turns with its summary
message and retained recent turns, the gateway imports that complete rewritten
snapshot into a fresh ephemeral SDK session and continues from it. No Pi adapter
or compaction-specific prompt handling exists in the gateway.

The rewritten snapshot must still be a structurally complete supported
transcript and end with a text/image user message or complete tool-result batch.
A suspended session can be replaced only when its exact pending calls and result
IDs match; unresolved calls and partial batches are rejected. Result-ending
imports seed calls and results together and use an empty SDK continuation signal,
without rerunning historical caller operations. See the root README for recovery
limits, request IDs, and opt-in diagnostic logging.

Most clients can use transcript matching without a custom header. A client that
can set per-conversation headers may send a unique
`X-Quaylet-Session: <id>` to distinguish independent conversations that
begin with identical text. Never configure one static value globally: that
would collapse all conversations into one lineage.

### Model identity and optional refusal fallback

Strict fidelity is the default (`--refusal-fallback off`). To opt in to the
native Opus 5 → Opus 4.8 classifier fallback, configure both models and use
`--refusal-fallback auto`, or override one request with
`X-Quaylet-Refusal-Fallback: auto`. The header accepts exactly `off` or
`auto`, once; invalid/repeated values or an unavailable target reject the request
before generation. This is not overload retry or an arbitrary model router.

Auto mode buffers one response until its answer/refusal/tool boundary, including
when `stream: true`; headers and SSE frames arrive only afterward. It retains
at most 64 MiB of normalized payload per response, not a total process-memory
bound. Strict mode streams incrementally after validated model identity.

Successful JSON/SSE uses the canonical actual model. Responses include
`X-Quaylet-Requested-Model` and `X-Quaylet-Actual-Model`, plus
`X-Quaylet-Fallback: true` after a validated downgrade. Exact retained
replays keep their original metadata. Known-session rebasing or thinking-only
changes preserve the active model when the requested model/policy are unchanged;
completed model/policy changes clear that provenance. Fresh imports after
eviction/restart start the requested model. No durable replay is added.

Malformed identity, buffer overflow, or original-leg tool retraction fails closed
with no buffered output. The proxy closes/drains the session rather than
fabricating tool results or rolling back individual callbacks. See the
[model and fallback reference](../../README.md#model-and-thinking-controls) and
[fallback troubleshooting](../../README.md#fallback-fails-or-the-first-streaming-event-is-delayed).

### Transactional session replacement

With a per-conversation header, a divergent completed transcript atomically
replaces that ID's idle SDK session only after the replacement starts
successfully. If the replacement cannot start, or admission is cancelled before
the swap, the old session remains usable.
Without a header, a rewritten transcript starts a new implicit lineage and the
old idle lineage remains eligible for normal least-recently-used eviction.

### Supported boundary

- Supported: text, inline images, and caller-owned function tools in streaming and non-streaming
  calls, including mixed text/calls, parallel calls, repeated tool rounds,
  reverse-order result submission by public ID, exact system/user strings,
  retries of completed requests, append-only continuations that originated
  through this running gateway, and complete imported or rewritten transcripts
  at completed boundaries. Imported completed tool calls/results retain
  their native structured roles and are not flattened into prompt text.
  Successful tool results may be empty; Anthropic non-empty results may set
  `is_error: true` for text-only errors. OpenAI results have no supported structured
  error flag.
- Anthropic controls: omit `tool_choice`, or use `{"type":"auto"}` with
  `disable_parallel_tool_use` omitted or `false`. `any`, `tool`, `none`, named
  choice, and `disable_parallel_tool_use: true` are rejected.
- OpenAI controls: function tools only; omit `tool_choice`, or use `"auto"`;
  omit `parallel_tool_calls`, or set it to `true`. Required/none/named choices,
  `parallel_tool_calls: false`, legacy functions, and non-function tools are
  rejected.
- Advisory only: `max_tokens` and `max_completion_tokens`; the Agent SDK does
  not provide exact output-token enforcement through this path.
- Thinking: normalized OpenAI `reasoning_effort` and Anthropic `thinking` plus
  `output_config.effort`; omitted controls disable thinking. See the
  [model-dependent control reference](../../README.md#model-and-thinking-controls)
  for levels, legacy budget mode, display, and native versus public replay.
  Model/thinking changes are supported at completed assistant turns while
  fixed configuration and pending-tool identity remain guarded.
- Unsupported: rebasing a busy session or an unresolved tool boundary,
  concurrent branches/forks under one explicit session ID, mixed text and
  tool-result blocks in one user turn, non-text/image tool results, exact sampling/stop
  controls, and public or multi-user service. A restart loses every live SDK
  session, suspended tool call, retained transcript, and replay entry. Clients
  can recover by resending a structurally complete transcript, including all
  already-executed tool results; the gateway cannot reconstruct missing history.
- Limits: at most 128 tool definitions; each name is 1–64 ASCII letters,
  digits, `_`, or `-`; each UTF-8 description is at most 8 KiB; each canonical
  JSON Schema is at most 64 KiB and all schemas together at most 512 KiB; each
  canonical argument object is at most 256 KiB. Each joined UTF-8 result text is at
  most 256 KiB and one result batch is at most 1 MiB. JSON Schemas must be
  self-contained; only resolvable fragment references are accepted and no
  network or filesystem retrieval occurs. Schema and argument JSON is limited
  to 64 nested mappings/arrays, counting the root container as depth 1; depth
  64 is accepted and depth 65 is rejected.
- Capacity: at most 8 sessions are retained by default. Fresh admission at the
  limit evicts the least-recently-used idle session. In-flight and
  replay-reserved sessions are never evicted; if all retained sessions are busy,
  both dialects return HTTP 503 with `session_capacity`. Replacing an explicit
  session ID does not consume an additional retained-session slot.
- Timeouts: generation waits up to 300 seconds. A published tool boundary waits
  up to `--tool-result-timeout` seconds (default `300.0`); expiry closes and
  removes that session. Backend teardown is bounded to 5 seconds.
- Concurrency and replay: one turn at a time per conversation. An in-flight
  duplicate or continuation returns HTTP 409. Only the current completed
  transcript head replays from memory; an older head may return
  `session_mismatch` after a successful continuation.
- Terminal reasons: Anthropic preserves `end_turn`, `max_tokens`, `refusal`, and
  `model_context_window_exceeded`. OpenAI maps them to `stop`, `length`,
  `content_filter`, and `length`, respectively. Chat Completions has no distinct
  context-window reason, so context exhaustion deliberately uses its truncation
  signal.
- Empty native refusals are retained terminal responses with authoritative
  usage, not backend errors or synthetic answers. Anthropic JSON/SSE yields
  empty assistant content (`[]`); OpenAI yields empty answer text. An exact retry
  replays without generation, and the returned assistant may be replayed unchanged
  before a new user turn, including when tools are configured. Empty-assistant
  input is a narrow structural allowance, not proof of a native refusal; empty
  users and malformed or incomplete boundaries remain invalid.

For request IDs, safe session rejection reasons, and `--log`/`--log-json`, see
[troubleshooting and diagnostics](../../README.md#troubleshooting).

For a tool session, the proxy creates one in-process MCP server named
`caller_tools`, exposes only the tools from the request, and lets the Agent
SDK/provider inject its native schema and standard tool-use instructions. The
proxy does not append tool prose to the caller's system string or final user
text. Built-ins, ambient settings, MCP servers, skills, plugins, subagents,
auto-memory, slash commands, and durable session persistence remain disabled.
Imported history is materialized only into the Agent SDK's ephemeral session
store for the lifetime of that in-process conversation.

The real Pi provider integration suite uses actual Uvicorn and localhost HTTP
but deterministic fake SDK sessions, so it never invokes a model:

```bash
.venv/bin/pytest -q --strict-markers --forbid-skips -W error tests/integration
```

It is included in `make release-offline`, which is the authoritative offline
release gate. `make check` remains the faster development gate.

The separately gated live release matrix uses synthetic markers, the production
app, the installed Agent SDK, and a stock Pi agent. It never inspects
credentials. Run it from the normal authenticated host context:

```bash
QUAYLET_LIVE=1 QUAYLET_LIVE_MODEL=sonnet-5 \
  .venv/bin/pytest -q --strict-markers --forbid-skips -W error \
  tests/live/test_gateway_text.py tests/live/test_gateway_tools.py \
  tests/live/test_gateway_images.py
```

The exact deterministic release commands are:

```bash
.venv/bin/pytest -q tests/gateway tests/integration
make check
make release-offline
```

They select no subscription model. The directory-based `integration` target
automatically includes the official-client and real-Pi tool compatibility
tests, both of which use deterministic fake SDK sessions.

## Superseded legacy Phase 0 feasibility record

Everything below this heading describes the earlier fail-closed probe design,
its Agent SDK 0.2.148 pin, and its negative Phase 0 verdict. It is retained for
audit history and is not the launch or verification contract for the current
Agent SDK 0.2.152 gateway above. In particular,
`RUN_LIVE_CLAUDE_TESTS=1` is the legacy probe opt-in; current gateway live tests
use `QUAYLET_LIVE=1` plus `QUAYLET_LIVE_MODEL`. The archived commands below
retain their historical names and apply only to their original revision.

Legacy live subscription checks are opt-in and require
`RUN_LIVE_CLAUDE_TESTS=1`. They must stop if the legacy policy evidence is
absent, ambiguous, or negative.

## Release policy

Every release-evidence pytest command must use `--strict-markers --forbid-skips
-W error`. The `--forbid-skips` hook records collection and every test-phase
skip, including expected failures represented as skips, and changes the session
to failed. Optional developer-only checks may deliberately omit that flag, but
they are never release evidence.

## Runtime and policy inputs

The supported runtime tuple is Claude Agent SDK `0.2.148`, Claude CLI
`2.1.251`, macOS 14 or newer (Darwin 23 or newer), and a local `apfs` mount.
The probe resolves and hashes one regular executable before accepting its
version.

Before any subscription-backed check, obtain both official primary pages:

- <https://code.claude.com/docs/en/agent-sdk/overview>
- <https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan>

The final redacted manifest records each URL, retrieval UTC timestamp, page
SHA-256, and a narrow `personal_local_use_allowed` interpretation. It never
stores page bodies, credentials, prompts, or response content. An absent,
ambiguous, or negative interpretation disables all live subscription probes.

### Task 1 policy recheck

The required primary pages were retrieved on 2026-08-31. Their bodies were
hashed transiently and were not stored in this repository.

| Source | Retrieved (UTC) | SHA-256 |
| --- | --- | --- |
| Agent SDK overview | 2026-08-31T23:56:46Z | `2830a3e2b3623aa731e55bede28cf7ba652c524195b3082fce3f56f4aabc75e5` |
| Claude plan notice | 2026-08-31T23:56:46Z | `19ee9ebf0bbed7f2b6ec9562e730303269f97c232b3db04506a5c6f7c6d379ca` |

`personal_local_use_allowed` is **false** for this feasibility effort. The
current pages do not provide an unambiguous authorization for a local proxy to
offer existing Claude-login subscription access; the overview specifically
limits unapproved third-party offerings. This fail-closed result disables live
subscription probes unless current primary policy evidence later supplies an
affirmative, applicable authorization.

## Final Phase 0 verdict

The committed [`validated-environment.json`](validated-environment.json) is a
schema-valid, content-free record of the current environment. It is an honest
negative feasibility result, not passing release evidence:

- the installed CLI is `2.1.252`, while the supported tuple pins `2.1.251`;
- the personal-subscription policy verdict is non-affirmative;
- the public SDK/CLI surface does not prove per-child existing-login
  provenance, a pre-input network boundary, or lifetime/per-turn provenance;
- prompt isolation, attribution absence, compaction suppression, path-safe
  live persistence, exact backend identity, native continuity, streaming, and
  exact usage semantics were not run and remain false;
- no Task 9 live SDK-tool record exists, so optional tools remain disabled.

The deterministic Darwin/APFS, synchronization, lock, bounded-journal,
lifecycle, retaining-supervisor, environment-construction, and structured
string-input facts remain recorded as true. `load_manifest()` accepts this
record, while `require_core_gates()` deterministically rejects its explicit
false gate set. Phase 1 is therefore blocked.

The manifest contains only field names, enums, booleans, bounded integers,
paths and hashes for public executables/packages, policy source metadata,
typed usage shapes/digests, and the exact configured alias target. It contains
no prompt, response, observed usage value, session identifier, credential
path/value, environment value, credential, or transport byte.

## Manifest generation

The only generation command is an explicit live operation:

```console
RUN_LIVE_CLAUDE_TESTS=1 uv run claude-proxy-probe all \
  --ack-personal-local-use-policy \
  --output docs/feasibility/validated-environment.json
```

Caller acknowledgment is only an invocation guard and never counts as policy
proof. The command fails before creating or overwriting output while live
opt-in is absent or any policy/runtime/attestation prerequisite is false. The
current tuple therefore must not run this command to mint evidence. A future
affirmative collector must return one run-bound, exact 27-gate Tasks 1–9
candidate. The command validates that candidate through the manifest, usage,
and SDK schemas; authorizes the exact existing-output identity; writes through
a same-directory mode-`0600` temporary file, `F_FULLFSYNC`,
descriptor-relative rename, and parent-directory `fsync`; then reloads and
rechecks exact bytes, schemas, core gates, and mode before returning success.
The default production collector remains unavailable until the existing probe
surfaces can provide that complete candidate; it never rebuilds one from a
prior manifest.

Git records a regular non-executable blob only as mode `100644`, so a fresh
checkout cannot preserve the working file's owner-only `0600` permission.
Committed-artifact checks therefore verify canonical bytes, schema, and
content exclusions rather than checkout permissions. Every generated output
is independently created and verified as `0600` by the atomic writer.

Synthetic all-true manifests in unit tests exercise canonical digest, resolver,
and fail-closed collection-to-output orchestration mechanics. They are never
committed evidence and cannot unblock Phase 1.
