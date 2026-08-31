# Claude Subscription API Proxy — Design Specification

**Status:** Trusted-local pivot amendment for review
**Date:** 2026-08-31
**Scope:** Personal, local-only proxy using Anthropic's official Claude Agent SDK

## 1. Decision summary

Build a small asynchronous localhost proxy with Anthropic Messages-shaped and OpenAI Chat Completions-shaped endpoints. The backend uses the official Claude Agent SDK transport and the user's separately established Claude login. It never offers a Claude login flow and never reads, exports, replays, or impersonates Claude OAuth credentials. This is a personal local integration, not an Anthropic-approved third-party product authentication design; section 2.1 makes that policy boundary explicit.

The proxy has three deliberately distinct operating modes:

1. **One-shot standard mode:** Unmodified clients can make a fresh, single-user-turn request when their selected parameter profile permits the required wire fields they send. No prior assistant or tool history is accepted because the Agent SDK cannot seed an arbitrary native transcript into a new session.
2. **Automatic linear-session mode:** A generated per-run local token identifies one live, linear conversation. Ordinary harnesses need only change their base URL and API key, or be launched through `claude-proxy run -- <command>`. The request body remains standard Anthropic/OpenAI wire shape.
3. **Explicit session-extension mode:** Clients that multiplex conversations use opaque session, head, and idempotency values in HTTP headers. This is the authoritative mode for safe concurrency and multiple conversations.

The proxy is not described as a drop-in implementation of either upstream API. It is an **Anthropic/OpenAI wire-shape subset backed by a stateful Claude Agent SDK session**. It must reject or explicitly disclose semantics that the SDK cannot honor.

Caller-provided external tools are a required design target, but remain behind an experimental capability gate until an executable spike proves that a suspended in-process MCP callback can be correlated and resumed safely across HTTP requests. Claude Code's built-in tools remain disabled in every mode.

## 2. Why this design is necessary

`claude -p` is not a lower-level inference transport. It is the Claude Code agent runtime, and the Agent SDK launches that same CLI transport. Directly invoking `claude -p` would provide a less typed and less documented boundary without restoring raw Messages API controls.

The current Agent SDK has no documented way to start a fresh session with an arbitrary sequence of native user, assistant, tool-use, and tool-result messages. Flattening that history into a transcript-shaped user prompt would violate prompt purity and alter model semantics. Consequently, lossless multi-turn behavior requires retaining the original live SDK session.

Standard Messages and Chat Completions requests do not carry a durable conversation identifier. A transcript hash alone cannot safely distinguish identical conversations, retries, stale heads, or concurrent branches. Exact continuity therefore requires either a unique per-run namespace or an explicit session extension.

These constraints mean the following three properties cannot all be offered simultaneously:

1. Claude subscription authentication through the official Agent SDK.
2. Completely stateless, unmodified Messages/Chat Completions semantics.
3. Faithful multi-turn history and tool behavior for the inference controls the backend actually supports.

This design preserves properties 1 and 3 for linear live sessions and provides a constrained form of property 2 for one-shot requests.

### 2.1 Subscription policy gate

Anthropic's current documentation is not a blanket authorization to ship subscription-backed products:

- The Agent SDK overview says unapproved third-party developers must not offer claude.ai login or subscription rate limits in their products and should use API-key authentication.
- Anthropic's June 2026 help-center update says the planned billing transition was paused and that Agent SDK, `claude -p`, and third-party-app usage still draw from the individual user's subscription limits for now.

This proxy therefore has a narrow policy scope:

- It is a personal, loopback-only tool operated by the same individual whose existing Claude login the official SDK discovers.
- It does not present a login screen, accept credentials, copy tokens, pool limits, serve other users, or promise that subscription-backed use will remain available.
- It must not be published or operated as a product for other users without current Anthropic approval or a switch to Claude Platform API-key authentication.
- Startup and documentation identify subscription behavior as policy-dependent and link the current Anthropic notice.

Phase 0 rechecks this policy against current primary documentation. A policy change that disallows the personal workflow stops the subscription backend rather than triggering an authentication workaround.

### 2.2 Trust model and sandbox decision

The release path is a **trusted-local integration**, not a security boundary against other processes running as the same operating-system user. The user, the proxy process, the official Agent SDK/Claude CLI subprocess, and other same-UID local processes are inside the v1 trust boundary. Loopback binding excludes remote network interfaces; it does not authenticate an operating-system user. The local bearer token authenticates HTTP callers. Neither control claims to defeat a malicious process that can inspect, signal, debug, or replace files owned by the same user.

"Prompt isolation" in this specification means minimizing observable ambient Claude Code configuration, tools, persistence, and proxy-added text through documented SDK controls and live canaries. It does not mean cryptographic isolation, a sealed worker, a native trust root, or hostile-local-process containment. Native launcher hardening, macOS Seatbelt/launchd containment, signed evidence ledgers, immutable Python-runtime closure proofs, and credential handoff attestation are explicitly outside the release critical path. That research may continue on an experimental branch, but it cannot block the HTTP proxy and cannot upgrade any production capability claim without a separate approved design.

The trusted-local release still applies practical defense in depth: loopback-only binding, a generated or explicitly configured local bearer key by default, an exact environment allowlist, an empty temporary working directory, disabled ambient Claude Code features, redacted diagnostics, bounded subprocess cleanup, and fail-closed version/capability checks. These controls reduce accidental leakage and semantic drift; they are not advertised as a sandbox.

## 3. Goals

In priority order:

1. Use only the official Agent SDK transport and its existing-login behavior, subject to the policy gate in section 2.1.
2. Inject no proxy prompt and suppress as much ambient Claude Code behavior as the public SDK permits, without treating same-UID process isolation as a release gate.
3. Preserve native message and tool semantics without serializing prior assistant history into text.
4. Work automatically with typical single-conversation CLI harnesses through base-URL and API-key configuration alone.
5. Offer an explicit, harness-neutral session extension for safe multiplexing and concurrency.
6. Stream text and tool events with low avoidable latency.
7. Keep the code small, inspectable, and replaceable by a future raw Messages API backend.
8. Fail clearly when fidelity is impossible.

## 4. Non-goals

- Acting as a multi-user service or routing other users' subscription credentials.
- Reading, copying, refreshing, or sending Claude OAuth credentials directly.
- Reconstructing private Claude Code requests or spoofing Claude Code metadata.
- Arbitrary cold-start history replay.
- Durable conversation storage or recovery across proxy restarts.
- Conversation branching from an earlier head in v1.
- Load balancing, account rotation, model routing, dashboards, analytics, or third-party telemetry.
- Reproducing unsupported inference controls through approximate prompt tricks.
- Enabling Claude Code filesystem, shell, web, ambient or user-configured MCP, skill, memory, or subagent behavior. The single proxy-owned in-process MCP bridge in section 9 is the only gated exception.
- Claiming byte-for-byte equivalence with Anthropic or OpenAI APIs.

## 5. Public HTTP surface

Required endpoints:

```text
POST /v1/messages
POST /v1/chat/completions
GET  /v1/models
GET  /health
```

Small proxy-specific endpoints:

```text
POST /_proxy/sessions
DELETE /_proxy/sessions/{session_id}
POST /_proxy/runs
DELETE /_proxy/runs/{run_id}
GET  /_proxy/capabilities
```

`/_proxy/*` is loopback-only and intentionally outside both compatibility dialects. `/v1/messages` is the canonical public dialect. `/v1/chat/completions` translates to and from the same internal representation.

### 5.1 Binding and authentication

- Default bind address: `127.0.0.1`.
- Port and loopback host may be overridden explicitly.
- Non-loopback binding is refused in v1. A later version may allow it only behind authenticated TLS termination and an explicit unsafe opt-in.
- Authentication is enabled by default. `LOCAL_PROXY_API_KEY` supplies the master bearer key; when absent, the server generates one and atomically stores it in a mode-`0600` file below a mode-`0700`, current-UID-owned per-process runtime directory. The server prints only that file's path. `--no-auth` is an explicit unsafe opt-in.
- `x-api-key` and `Authorization: Bearer` are accepted as local proxy credentials; they are never forwarded to the SDK.
- Logs always redact authorization, API keys, SDK credentials, cookies, and credential-looking environment values.
- The server accepts only expected loopback `Host` values, requires `Content-Type: application/json` for mutations, emits no permissive CORS headers, and rejects browser `Origin` headers unless explicitly allowlisted.
- With authentication disabled, any local user or process able to connect is authorized to consume the user's subscription through the proxy. Startup prints that warning.
- `/health` is the only anonymous endpoint and returns no model, backend, session, or capability detail. Every model, capability, inference, session, and run endpoint requires the bearer token unless `--no-auth` is explicitly selected.

Authorization is credential-class-specific:

| Credential | Allowed endpoints |
|---|---|
| Master bearer key | All public and `/_proxy/*` endpoints. Required to create explicit sessions or runs. |
| Derived run token | Public inference endpoints, `/v1/models`, `/_proxy/capabilities`, and deletion of its own run only. It cannot create sessions, create another run, inspect another run, or use explicit-session headers. |
| No credential | `/health` only, unless the server was started with `--no-auth`. |

The server mints and signs every derived run token; the launcher only requests, receives, conveys, and revokes it.

### 5.2 Harness-neutral session extension

Explicit mode uses these request headers:

```http
X-Claude-Proxy-Session: ses_<opaque>
X-Claude-Proxy-Head: head_<opaque>
Idempotency-Key: req_<caller-generated-or-wrapper-generated>
```

Successful responses return:

```http
X-Claude-Proxy-Session: ses_<opaque>
X-Claude-Proxy-Head: head_<next-opaque>
```

The head value is a compare-and-swap token, not a transcript offset. The server accepts exactly one current head for mutation. Reusing the same idempotency key with the same request returns the cached prior response. Reusing it with different content returns `409 idempotency_conflict`.

Committed idempotency records are never evicted while their session is live. Each session has a configured maximum committed-segment count; when the next operation would exceed it, the proxy returns `409 idempotency_capacity` before reserving a head or touching the SDK. The client must start a new session or run token. This finite session limit keeps the response cache bounded while preserving exact replay for every committed segment. Tombstone expiry deletes all remaining idempotency material.

Session IDs and heads are opaque, random, and contain no Claude credential or SDK session value.

#### Explicit session creation

`POST /_proxy/sessions` accepts a proxy control object rather than a Messages request:

```json
{
  "dialect": "anthropic",
  "model": "configured-model-name",
  "system": [],
  "tools": [],
  "thinking": null,
  "parameter_policy": "messages_compat",
  "ttl_seconds": 1800
}
```

It validates all immutable configuration before allocating an actor and returns:

```json
{
  "id": "ses_<opaque>",
  "head": "head_<initial-opaque>",
  "dialect": "anthropic",
  "parameter_policy": "messages_compat",
  "expires_at": "2026-08-29T12:00:00Z"
}
```

The initial head represents an empty native conversation with fixed configuration; the SDK process may be created lazily on the first valid user turn. Every public request must repeat the same model, system prompt, tools, and thinking configuration so the proxy can validate rather than infer them. `DELETE` closes the actor and leaves a short-lived reason-only tombstone.

### 5.3 Automatic linear-session mode

`claude-proxy run -- <harness command>` requests a server-minted per-run proxy token and supplies the harness's ordinary base-URL and API-key environment variables. No harness source modification is required.

Against a separately running server, the launcher first calls `POST /_proxy/runs` with a dialect, parameter policy, and TTL. The server mints and returns a `run_id`, an opaque short-lived API-key value, and its expiry. When local authentication is enabled, this control request requires the master key and the child receives only the derived token. The first valid public request made with that token binds its model, system prompt, tool definitions, and thinking configuration; subsequent changes are rejected. `DELETE /_proxy/runs/{run_id}` revokes the token and closes its session.

For a separately running server with a generated master key, `claude-proxy run` requires `--api-key-file <path>` or `LOCAL_PROXY_API_KEY_FILE`; it validates file ownership, regular-file type, exact mode, and runtime path before reading. There is no global implicit key-file search. A launcher that starts its own server transfers the master key in memory and never exposes it to the harness. On orderly shutdown the server removes its key file and runtime directory. On startup, it removes only current-UID-owned stale per-process directories whose recorded server PID is confirmed absent; a stale key is never reused.

Each generated token owns at most one current live conversation:

- The first valid request creates the session.
- Later requests must extend the recorded public head exactly.
- An identical request under the same token is necessarily treated as a retry and returns the cached response; the proxy cannot distinguish it from a user's intention to start a second identical conversation.
- A stale, divergent, or second initial conversation returns `409 session_ambiguous` with instructions to start a new run token or use explicit mode.
- A new token is required when starting a separate conversation.

The zero-source-change claim is limited to harnesses that honor an injected base URL and API key and run exactly one logical conversation per generated token. A process that multiplexes conversations needs explicit mode or an existing request hook that supplies its headers.

When local authentication is disabled, the server mints a random API-key value used only as a routing namespace. When authentication is enabled, the server mints and signs a short-lived derived token so the launcher can convey authentication without exposing the configured master key to the child harness. All generation, signing, run binding, expiry, digest storage, and revocation state is server-side. Derived run tokens and signing state remain in memory only. The generated master-key runtime file described above is the sole permitted proxy credential write; it is never confused with a Claude subscription credential.

Requests with neither explicit session headers nor a distinct routing token use one-shot mode.

Selection precedence is deterministic:

1. A recognized derived run token selects its one automatic session and bound profile.
2. Otherwise, explicit session headers select a session owned by the authenticated local principal.
3. Otherwise, the request is one-shot and uses the server's configured direct-request policy.

Dialect, parameter policy, and TTL are immutable once a session or run token is created. Expired or revoked control objects are never revived by transcript matching.

### 5.4 No transcript-based session selection

The proxy records a canonical public transcript and fingerprints it, but fingerprints are used only to validate an already-selected session. They never choose a session globally.

This prevents identical transcripts from aliasing across callers. It also makes retries, stale requests, and concurrent branches explicit rather than heuristic.

## 6. Compatibility contract

### 6.1 Transcript rules

A new session may begin with:

- An optional caller system prompt.
- One user turn containing supported content blocks.
- A fixed model and fixed initial tool definition set.

For a live session, the caller may resend the full public history as standard clients normally do. The proxy verifies that the submitted prefix exactly matches events previously exposed by the proxy, then sends only the genuinely new native input into the existing SDK session.

The recorded public transcript is validation state, not reconstructed model context. It is held in memory, scoped to a live session, and deleted on expiry or shutdown.

The following produce `409` or `422` rather than prompt serialization:

- Unknown prior assistant history.
- A request based on an expired or evicted session.
- A stale head.
- Branching from an earlier assistant message.
- Changing model, system prompt, tool definitions, thinking configuration, or dialect mid-session.
- Mixing a pending tool result with unrelated new user text.

### 6.2 Field support

The exact capability matrix is published by `GET /_proxy/capabilities` and versioned independently of model names.

| Public field | v1 behavior |
|---|---|
| `model` | Supported from configured allowlist; immutable within a session. |
| `system` / system messages | Supported at session creation; passed without proxy text; immutable thereafter. |
| `messages` | Supported under the transcript rules above. |
| `stream` | Supported for text; tool streaming remains gated. |
| `thinking` / effort | Anthropic dialect only, and only for exact model/configuration tuples whose full block, signature, streaming, and replay semantics pass the Phase 0 capability gate. OpenAI `reasoning_effort` is rejected in v1. Immutable within a session. |
| `metadata` | Retained for local correlation only; never injected into the model prompt. |
| `max_tokens`, `max_completion_tokens` | Not enforceable by the SDK. Strict policy rejects them; compatibility policy accepts and explicitly reports them as ignored. |
| `temperature`, `top_p`, `top_k` | Unsupported. Strict policy rejects; compatibility policy may accept-and-report-as-ignored only when explicitly configured. |
| `stop`, `stop_sequences` | Unsupported and rejected by default. |
| `tool_choice`, parallel-tool controls | Unsupported until exact SDK behavior is demonstrated. |
| `response_format`, seed, logprobs, penalties, `n` | Unsupported and rejected. |

There is no silent ignore path. Compatibility policy is explicit configuration associated with a local token or server profile. Every response affected by it includes:

```http
X-Claude-Proxy-Ignored-Parameters: max_tokens,temperature
```

Non-streaming responses also include a proxy warning object only where the public dialect permits extra fields without breaking clients. Debug logs record the same warning after redaction.

Strict policy remains the default for direct server use. Because `max_tokens` is mandatory in the Anthropic Messages request schema but unenforceable by this backend, `claude-proxy run` and any advertised Anthropic-client preset must explicitly select the documented Messages compatibility profile. That profile accepts `max_tokens`, reports it as ignored, and leaves all other unsupported controls strict unless separately listed. The proxy consequently does not claim that a stock Anthropic client works against the default strict profile merely by changing its base URL.

### 6.3 Content support

Initial implementation order:

1. Text user and assistant content.
2. Base64 image inputs after conformance tests.
3. Document/PDF inputs after conformance tests.
4. Tool-result content beyond text only after MCP conversion tests.

URLs, audio, citations, search-result blocks, and unverified content variants are rejected rather than coerced. Content support must be identical in streaming and non-streaming modes unless the capabilities document says otherwise.

Anthropic thinking support is tuple-gated by model, thinking mode/budget, and effort. A passing tuple must preserve thinking and redacted-thinking block types, text or opaque payload bytes, signatures, block order, `thinking_delta`/`signature_delta` ordering, stop behavior, and SDK usage in both streaming and non-streaming traces. The canonical public transcript stores those exposed blocks and signatures exactly so a resent history can be compared byte-for-byte without reinjecting it. Unknown block/delta shapes fail the operation and disable that tuple. Thinking-enabled sessions are Anthropic-only; the OpenAI adapter neither hides nor approximates thinking content.

### 6.4 OpenAI translation

The OpenAI adapter is intentionally thin:

- System messages become the canonical system prompt.
- User messages become canonical user blocks.
- Assistant text becomes canonical assistant text.
- Assistant `tool_calls` and `tool` messages map to canonical tool-use and tool-result events only when tool support is enabled.
- OpenAI chunk ordering and `[DONE]` framing are generated from canonical stream events.
- Unsupported OpenAI-only fields are rejected before any SDK state mutation.

The adapter does not invent usage, finish reasons, tool errors, or sampling semantics that the backend did not provide.

### 6.5 Protocol details

- Anthropic requests require `anthropic-version: 2023-06-01`; unsupported versions receive `400 unsupported_version`.
- Unknown request fields are rejected. Compatibility policy applies only to its documented allowlist and never turns on general extra-field ignoring.
- The OpenAI v1 subset accepts at most one leading `system` message and rejects `developer` messages and later system messages rather than merging roles with different semantics.
- Public response IDs (`msg_lp_*`, `chatcmpl_lp_*`) and OpenAI timestamps are proxy-generated correlation values. They do not expose or claim to be Anthropic IDs.
- Stop mappings are allowlisted from observed SDK events. `end_turn` maps to Anthropic `end_turn` and OpenAI `stop`; gated tool use maps to Anthropic `tool_use` and OpenAI `tool_calls`. Unknown reasons produce a protocol error rather than a guessed mapping.
- Exact SDK-supplied usage is returned when present. If a required usage value is unavailable, non-streaming requests fail with `502 sdk_protocol_error`; a stream already in progress emits a terminal error event and marks the session lost. Zero is never used as a placeholder.
- Official Anthropic and OpenAI client libraries are black-box compatibility fixtures for headers, error envelopes, SSE parsing, IDs, timestamps, usage, and finish reasons.

## 7. Backend isolation and prompt purity

Each live conversation owns one long-lived `ClaudeSDKClient` actor. The actor alone may call or drain that client; SDK clients are never shared concurrently across event loops, tasks, or conversations.

The effective options are equivalent to:

```python
ClaudeAgentOptions(
    model=selected_model,
    system_prompt=caller_system_or_empty,
    tools=[],
    skills=[],
    setting_sources=[],
    mcp_servers=caller_tool_bridge_or_empty,
    strict_mcp_config=True,
    agents={},
)
```

Additional isolation:

The controls below are behavioral and operational isolation inside the trusted-local model defined in section 2.2. They must not be described as hostile-process containment or credential sealing.

The Agent SDK/CLI child environment is constructed deny-by-default. The only inherited names are `HOME`, `USER`, `LOGNAME`, `TMPDIR`, `TMP`, `TEMP`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TZ`, `SSL_CERT_FILE`, `SSL_CERT_DIR`, and an optional existing-login location in `CLAUDE_CONFIG_DIR`. `PATH` is constructed from the verified Claude CLI directory plus fixed system directories rather than inherited. `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, and their lowercase equivalents are passed only when the user enables the documented network-proxy option. Proxy-owned isolation variables listed below are set to fixed values rather than inherited. `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, provider credential overrides, proxy bearer/master keys, and every unrecognized `ANTHROPIC_*`, `CLAUDE_*`, and `LOCAL_PROXY_*` name are stripped or cause a startup error when their presence makes authentication provenance ambiguous. Additional pass-through names require explicit configuration and appear, by name only, in diagnostics.

- Use an empty, proxy-owned temporary working directory rather than the repository or user's home directory.
- Set `CLAUDE_CODE_SKIP_PROMPT_HISTORY=1` so Python SDK sessions do not write prompt history or transcripts under `~/.claude/projects/`.
- Set `CLAUDE_CODE_ATTRIBUTION_HEADER=0` so the CLI does not prepend its client-version and prompt-fingerprint attribution block to the system prompt.
- Set `DISABLE_COMPACT=1`. Automatic SDK summaries are hidden prompt rewriting and are therefore forbidden; a session that exhausts its context fails explicitly instead of compacting.
- Set `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` and `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`.
- Set `CLAUDE_CODE_DISABLE_BUNDLED_SKILLS=1`, `CLAUDE_CODE_DISABLE_POLICY_SKILLS=1`, and `CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS=1` where supported by the pinned CLI.
- Set `ENABLE_CLAUDEAI_MCP_SERVERS=false`, `CLAUDE_CODE_DISABLE_WORKFLOWS=1`, and `CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL=1`.
- Set `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` and `CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1` to request the pinned runtime's documented suppression of telemetry/error reporting, feature fetches, update traffic, marketplace activity, and title-generation model calls not required for inference. Phase 0 records observable behavior rather than asserting suppression beyond the public contract.
- Do not load plugins, connectors, slash commands, skills, hooks, or subagents.
- If caller tools are enabled, expose only one in-process MCP server created from that session's immutable tool definitions.
- Set `tools=[]` even when custom MCP tools exist so Claude Code built-ins are absent.
- Allow only the exact generated MCP tool names.
- Do not use Claude Code's system-prompt preset.
- Never prepend, append, summarize, or annotate the caller's system prompt or messages.
- Do not use `--bare`, because it also disables the subscription credential path.

The default Claude configuration directory, or an explicitly selected existing `CLAUDE_CONFIG_DIR`, must remain available to the official CLI for its existing login, so isolation cannot rely on replacing it with an empty directory. A clean synthetic config-root experiment may remain diagnostic research, but inability to authenticate there is never a release gate. The environment controls, `setting_sources=[]`, temporary working directory, startup inspection, and live canaries must produce no observable ambient capability use or prohibited prompt/tool/response persistence. Unrelated authentication and runtime bookkeeping is classified separately by path and metadata without reading credential contents. If the pinned SDK/CLI ignores `CLAUDE_CODE_SKIP_PROMPT_HISTORY` and persists prohibited content, prompt-purity validation fails and the project stops.

For the pinned SDK/CLI pair, Phase 0 must identify a public, non-secret initialization or connection evidence shape that positively distinguishes the user's existing Claude subscription login from API-key, API-key-helper, cloud-provider, custom-endpoint, and unknown modes. The accepted source enum and evidence shape are versioned in the feasibility manifest. Absence, ambiguity, or a negative mode fails Phase 0 and startup; credential contents are never read. Negative tests cover environment overrides and controlled non-secret configuration mode changes. If the official runtime exposes no reliable positive provenance signal, the subscription backend remains unavailable rather than asserting `existing_claude_login`.

The proxy can minimize ambient Claude Code behavior but cannot prove that the Agent SDK runtime is byte-for-byte equivalent to the raw Messages API. Managed policy and server-side behavior outside the SDK's controls may still exist. The product language and diagnostics must say **prompt-isolated Agent SDK**, not **raw Anthropic prompt**.

Prompt-purity tests inspect both constructed SDK options and observable initialization events. Any unexpected system text, ambient configuration, built-in tool, connector, skill, memory, or subagent fails the test.

## 8. Session actor and request lifecycle

Each session actor owns:

- One `ClaudeSDKClient` and its subprocess/transport.
- Immutable session configuration.
- Current opaque head.
- Canonical public transcript and its validation fingerprints.
- At most one active model operation.
- Pending external tool callbacks.
- A bounded idempotency-response cache.
- Activity timestamps and cancellation state.

### 8.1 Ordinary turn

1. Authenticate and select the session by explicit ID or per-run token.
2. Validate the current head, idempotency key, request shape, immutable configuration, and transcript prefix.
3. Reject unsupported fields before changing SDK state.
4. Convert only the new user turn into the SDK's native input form.
5. Reserve an operation and its next head, then start or continue the actor-owned SDK query.
6. Drain SDK events continuously and translate them to the selected public dialect.
7. For an ordinary answer, continue draining through a successful SDK `ResultMessage` and iterator completion before atomically recording public events, activating the reserved head, and finalizing the response cache.

An `AssistantMessage` or public `message_stop` does not by itself commit an ordinary turn: result status, usage, and trailing protocol events must validate first. Tool use is the explicit exception because the public API must return control while the internal query remains suspended; it commits only after the complete tool-call set has been finalized, all corresponding callbacks are known to be suspended, and the actor can enter `WAITING_FOR_TOOLS` atomically. Section 8.2 defines that transition.

### 8.2 Actor state machine

The actor has five explicit states:

```text
IDLE
  -> GENERATING
  -> IDLE                 ordinary final response
  -> WAITING_FOR_TOOLS    committed tool-use response

WAITING_FOR_TOOLS
  -> GENERATING           validated tool-result request resolves callbacks

GENERATING
  -> LOST                 unrepairable stream/SDK/state failure
  -> CLOSED               explicit deletion or shutdown cancellation

IDLE
  -> CLOSED               expiry, deletion, or shutdown

WAITING_FOR_TOOLS
  -> CLOSED               tool timeout, deletion, expiry, or shutdown

LOST
  -> CLOSED               reason-only lost-tombstone TTL elapsed; cleanup state is independent
```

Head and idempotency rules:

- A request at active head `hN` reserves `hN+1` and an operation record before model state changes.
- For non-streaming output, `hN+1` is returned only after it becomes active.
- For streaming output, `hN+1` is returned in response headers as a provisional value because headers precede the final event. It becomes active only at a valid terminal public message event.
- If a stream fails after exposing a provisional head but before commit, the session becomes `LOST`; a later request using either head receives `409 session_lost`.
- A committed tool-use response activates `hN+1`, finalizes the first HTTP request's idempotency record, and enters `WAITING_FOR_TOOLS` only after the full tool-call set and suspended callbacks are established. The internal receive loop remains alive.
- A tool-result request is the only new operation accepted in `WAITING_FOR_TOOLS`. It must use `hN+1`, must carry a new idempotency key in explicit mode, and reserves `hN+2` before resolving callbacks.
- A continuation may commit another fully established tool-use boundary and return to `WAITING_FOR_TOOLS`, or commit a final answer and return to `IDLE` only after a successful internal SDK `ResultMessage` and iterator completion.
- An exact retry of any committed public segment returns that segment and its active head without touching the SDK.
- If the configured committed-segment limit is reached, every new operation fails with `409 idempotency_capacity` before SDK mutation; existing exact retries continue to work.

Entering `LOST` or `CLOSED` atomically transfers the SDK client and tracked process/workdir handles to a teardown owner, makes them unreachable to request handling, erases proxy-held transcript, response, prompt, tool, and idempotency content, and starts bounded teardown. The reason-only session tombstone retains only opaque ID, machine-readable terminal reason, and expiry. The cleanup registry retains the minimum SDK/process/workdir handles, identities, attempt state, and last error required to close and confirm absence; the child SDK/CLI may still hold model state in memory until termination succeeds, so cleanup failure is never described as complete erasure. A lost tombstone returns `409 session_lost`. A terminal reason of TTL expiry returns `410 session_expired`; deletion, tool timeout, launcher exit, or shutdown returns `410 session_closed`. Tombstone expiry is independent of cleanup retry and never discards unconfirmed cleanup authority. After tombstone expiry an explicit session ID is unknown and returns `404 session_not_found`; an expired/revoked run-token digest is retained only for the configured token-tombstone TTL and then becomes `401 invalid_local_api_key`. If deletion or shutdown wins a race with generation, the actor transfers ownership and closes; it never returns to `IDLE`.

Automatic mode applies the same transitions internally but does not require the harness to see head values.

### 8.3 Concurrency

- One operation may mutate a session at a time while `GENERATING`.
- A second request with a different idempotency key during `GENERATING` receives `409 session_busy` rather than waiting indefinitely.
- Exact retries attach to the existing buffered result where safe or receive the cached completed result.
- `WAITING_FOR_TOOLS` is not considered busy for its one valid matching tool-result request.
- Separate sessions may run concurrently up to configured global limits.
- There is no implicit branch creation.

### 8.4 Disconnects and cancellation

The server distinguishes HTTP-client disconnect from explicit model cancellation:

- A disconnect does not automatically kill an operation if an idempotent retry can safely reattach or retrieve its buffered result.
- Buffers are strictly bounded. Exceeding the bound cancels the SDK operation and marks the session unusable.
- Explicit session deletion, idle expiry, process shutdown, or tool timeout cancels pending work and closes the SDK client.
- A session whose SDK state may have advanced without a complete recorded public event is poisoned and returns `409 session_lost`; it is never guessed back into sync.
- In automatic mode, termination or exit of the launched harness triggers immediate run-token revocation, server-side operation cancellation, and the same bounded SDK/CLI teardown. An ordinary HTTP disconnect alone retains the retry behavior above.

## 9. Caller-owned tool bridge

### 9.1 Required semantics

The harness remains responsible for tool policy, approval, execution, retries, and results. The proxy never executes caller tools.

At session creation, caller tool schemas are registered as in-process SDK MCP tools while Claude Code built-ins remain disabled. Tool definitions cannot be added, removed, or changed during the session.

Intended flow:

1. Claude emits native assistant tool-use blocks.
2. The actor exposes those blocks as Anthropic `tool_use` or OpenAI `tool_calls` and completes that public HTTP response with the corresponding stop reason.
3. The in-process MCP handlers remain suspended; the SDK query and receive loop remain alive inside the actor.
4. The harness executes tools normally.
5. Its next standard request repeats the public assistant tool call and supplies matching tool results.
6. The proxy validates the transcript and IDs, resolves the corresponding suspended MCP handlers, and streams the continuation from the same internal SDK query as the second HTTP response.
7. Tool results are not submitted as a new SDK user prompt; they become MCP handler return values.

### 9.2 Capability gate

This flow is plausible in the current SDK but is not a documented cross-HTTP lifecycle. Tool capability remains disabled by default until all of these tests pass against the supported SDK version:

1. Suspend one handler for 1, 60, and 600 seconds; finish the first HTTP response; resolve it from a second request; receive the continuation; then complete a later ordinary user turn.
2. Repeat for streaming, non-streaming, disconnect, retry, cancellation, idle timeout, SDK interrupt, and graceful shutdown.
3. Trigger two parallel identical tool calls, return their results in reverse order, and prove exact public tool-use ID to callback correlation.
4. Verify that tool definitions truly remain fixed and that attempted mutations are rejected.
5. Verify event ordering, usage, stop reasons, and tool argument deltas for both public dialects.

Failure of test 3 is a release blocker for general tool support. Permission and hook events may expose a tool-use ID, but the in-process MCP handler does not receive that public ID directly; the implementation must prove the cross-event correlation rather than assume it. Because the SDK does not guarantee serial tool calls, the proxy must not advertise a one-at-a-time workaround unless it can enforce that invariant.

### 9.3 Initial tool-result subset

The first supported result shape, if the gate passes, is text plus an error flag where the dialect supports it. Empty, image, document, resource, search-result, and mixed-content results remain unsupported until exact MCP conversion behavior is tested.

Tool-call IDs, tool names, arguments, and result sizes have strict bounds. Unknown, duplicate, partial, or mismatched results fail without resolving any callback.

## 10. Streaming behavior

The actor consumes SDK partial events as they arrive and translates them without waiting for the complete answer.

Anthropic streams emit the supported subset of message start, content-block start/delta/stop, message delta, and message stop events. OpenAI streams emit chat-completion chunks and a final `[DONE]` marker.

Rules:

- Preserve text and tool-argument delta ordering.
- For ordinary answers, buffer only the terminal success event (`message_stop` or final `[DONE]`), not text deltas, until a successful SDK `ResultMessage` and iterator completion validate the turn.
- For tool use, do not emit the terminal tool-use event until the complete public call set is known and all matching MCP callbacks are suspended.
- Do not synthesize token usage when the SDK has not supplied it.
- Mark unsupported or unavailable usage fields according to the compatibility dialect rather than returning invented zeroes.
- Validate all request fields before sending SSE headers.
- After headers are sent, terminal backend errors become dialect-appropriate error events followed by connection close.
- Bounded queues provide backpressure between the SDK actor and HTTP writer.
- Time to first byte, first model event, first text token, and final event are measured separately in debug mode.

## 11. Errors

Proxy errors use stable machine-readable codes and the closest public dialect envelope:

| HTTP status | Example code | Meaning |
|---|---|---|
| `400` | `unsupported_parameter` | Field cannot be honored under the selected policy. |
| `401` | `invalid_local_api_key` | Local proxy authentication failed. |
| `404` | `model_not_configured` | Model is not in the configured list. |
| `404` | `session_not_found` | Explicit session tombstone expired or the session ID never existed. |
| `409` | `session_busy` | A different operation is already active. |
| `409` | `stale_session_head` | Request does not extend the current head. |
| `409` | `session_ambiguous` | Automatic token cannot identify a unique valid continuation. |
| `409` | `session_lost` | Live SDK state cannot be proven consistent. |
| `409` | `idempotency_conflict` | Idempotency key was reused with different content. |
| `409` | `idempotency_capacity` | The live session retained its maximum committed segments; start a new session or run. |
| `410` | `session_expired` | Session or run TTL elapsed. |
| `422` | `history_unavailable` | Request contains history that cannot be seeded or validated. |
| `422` | `context_exhausted` | The non-compacting native session has reached its usable context limit. |
| `429` | `session_capacity` | Configured live-session/process limit was reached. |
| `502` | `sdk_protocol_error` | Agent SDK produced an invalid or untranslatable event. |
| `503` | `sdk_unavailable` | Claude login, CLI, or SDK transport is unavailable. |
| `410` | `session_closed` | Session closed; the response cause may be `tool_result_timeout`, deletion, or shutdown. |

Errors never trigger transcript flattening, hidden retries, or automatic session replacement.

## 12. Resource lifecycle

The proxy remains one server process, but the SDK may own one Claude subprocess per live conversation. Therefore:

- Set conservative limits for live sessions, active generations, pending tool calls, queued bytes, request size, tool-result size, and idempotency cache entries.
- Expire idle completed sessions after a configurable TTL.
- Use a separate, longer but bounded TTL for pending tool results.
- Reject new sessions at capacity instead of evicting an active session.
- Close SDK clients and temporary working directories deterministically.
- Bound graceful close, process termination, and forced-kill intervals separately. Construction rollback, normal close, request failure, expiry, explicit deletion, capacity rejection, and server shutdown must all run the same teardown state machine.
- Define ownership as the SDK client plus every process the pinned transport can positively track for that session. Teardown performs graceful SDK close, bounded terminate, bounded forced kill, reap confirmation, and workdir-removal retries under one foreground deadline. Success means confirmed absence of every tracked process and removal of the workdir. Failure records `cleanup_unconfirmed`, retains the teardown handles and identities in the cleanup registry, rejects new work through unhealthy status, and continues bounded periodic retries or requires explicit operator resolution. Proxy-held prompt/session copies are erased regardless of cleanup outcome; no claim is made about memory still owned by an unconfirmed child.
- On shutdown, stop accepting work, give active non-tool streams a short grace period, then cancel and mark unfinished sessions lost.
- Except for the generated local master-key runtime file explicitly defined in section 5.1, the proxy persists no subscription credentials, run tokens, transcripts, prompts, tool payloads, or responses to disk. The pinned Python SDK/CLI is launched with transcript/history persistence disabled. A versioned path policy classifies known credential paths as metadata/event-only and known noncredential state/workdirs as safe for content canaries. The whole relevant Claude root receives path/size/mtime event snapshots; only explicitly safe paths and newly created artifacts classified as noncredential are scanned for unique benign canaries. Unknown new paths fail the gate pending classification rather than being opened. The core probe uses the actual existing-login path without modifying credential files; synthetic-root evidence is supplemental.

## 13. Configuration and models

Configuration is environment- or small-file-based and contains:

- An exact supported Claude Agent SDK version and exact bundled/installed Claude CLI version.
- Listen host and port.
- Generated or explicitly configured local master API key, unless the explicit `--no-auth` mode is selected.
- Configured public model names and their SDK model mappings.
- Default strict/compatibility parameter policy.
- Session, concurrency, size, and timeout limits.
- Debug logging toggle.
- Tool capability toggle, which cannot be enabled unless the SDK-version gate is recorded as passing.
- Backend kind and authentication source. V1 accepts only `backend_kind=agent_sdk_subscription` and `auth_source=existing_claude_login`.

Startup fails closed when the actual SDK or CLI version differs from the validated pair. Upgrades are intentional changes that rerun prompt-purity, session, streaming, persistence, and tool gates before the supported pair is updated.

`GET /v1/models` returns only the configured public model list. It does not scrape undocumented endpoints or claim capabilities absent from `/_proxy/capabilities`.

`/_proxy/capabilities`, startup diagnostics, and validation manifests expose `backend_kind`, `auth_source`, and `semantic_class=prompt_isolated_agent_sdk`. A future `platform_api_key` backend would use `semantic_class=raw_messages_api`, but it is not implemented by v1 and requires a separate approved design. Startup fails when inherited overrides, stored non-secret mode indicators, or missing/unknown runtime evidence make the selected backend's authentication provenance ambiguous.

## 14. Diagnostics and observability

`PROXY_DEBUG=1` records structured local logs for:

- Request ID, session ID hash, and head transition.
- Incoming dialect and redacted request shape.
- Canonical field presence, block types, sizes, and keyed fingerprints; no caller content.
- SDK option field presence and exact proxy-owned isolation values; caller prompt fields are fingerprinted.
- New SDK input block types, sizes, and keyed fingerprints, never content or reconstructed prior history.
- SDK event types and public translated event types.
- Ignored-parameter warnings.
- Queue, subprocess, and latency measurements.
- Session creation, expiry, poisoning, and closure.

By default, message bodies and tool results are not logged. A separate explicit `PROXY_DEBUG_CONTENT=1` enables exact canonical request and SDK input content logging with a startup warning. OAuth material, authorization headers, API keys, cookies, and credential environment variables are redacted regardless of content logging.

The proxy adds no telemetry sink or outbound diagnostics. Network behavior of the official SDK and Anthropic service remains governed by the pinned runtime and its documented controls.

## 15. Validation strategy

### 15.1 Pure unit and protocol tests

- Anthropic and OpenAI request validation.
- Canonical event conversion and round trips.
- SSE golden traces and chunk ordering.
- Unsupported-field policy.
- Session/head compare-and-swap behavior.
- Idempotent retries and conflict detection.
- Randomized actor-state-machine transitions, including simultaneous identical and divergent requests.
- Expiry versus request, deletion during generation/tool wait, bounded-buffer overflow, and subprocess death after possible SDK mutation.
- Disconnect injection after every SSE event boundary and provisional-head failure handling.
- Transcript canonicalization over Unicode, JSON argument ordering, empty fields, and content variants.
- Redaction and secret-canary tests.
- Loopback `Host`, browser `Origin`, CORS, content-type, and local-authentication tests.
- Table-driven endpoint-by-credential-class authorization, including rejection of launcher-forged, cross-run, expired, and revoked derived tokens.
- Reason-aware terminal lookup before and after session/run tombstone expiry, including expiry during generation and tool wait.
- Cleanup ownership-transfer tests proving request handlers cannot regain the SDK client while unconfirmed resources remain tracked separately.

### 15.2 Prompt-purity tests

Construct a minimal request and assert that SDK configuration and submitted input contain only:

- The caller's system prompt or explicit empty prompt.
- The new caller content blocks.
- Minimum SDK-required transport metadata.
- Caller tool definitions only when the gated feature is active.

Canary the isolated environment with `CLAUDE.md`, user/project settings, skills, agents, MCP connectors, plugins, auto-memory, hooks, and built-in tool names. Any observed loading or exposure fails the test.

The real existing-login configuration root is never modified to plant a canary. User-level isolation is evidenced through the pinned runtime's public effective-configuration/init surface plus the versioned metadata/content path policy above. Project-level canaries live only in the proxy-owned temporary workdir. A synthetic config-root probe may plant user-level canaries, but is supplemental because it may not authenticate.

Inspect the effective system prompt for the Claude Code attribution block and its client-version/prompt-fingerprint fields. The test must prove that `CLAUDE_CODE_ATTRIBUTION_HEADER=0` removes it for the pinned subscription connection; a connection type that retains it fails prompt purity.

Force the context near the compaction threshold and assert that the SDK fails without emitting a `compact_boundary` or injecting a summary. Apply the versioned path policy before and after a session and assert that no prompt, transcript, debug log, title, memory, tool payload, or leaked temporary artifact was written to a content-safe path; metadata-only credential paths are never opened.

### 15.3 Live subscription integration tests

Opt-in tests requiring an existing official Claude login cover:

- One-shot text, automatic linear session, and explicit session mode.
- Streaming and non-streaming equivalence.
- Session retry, stale head, new run token, expiry, and shutdown.
- The complete cleanup matrix: partial construction, protocol/request failure, bounded-buffer overflow, expiry, deletion, capacity rollback, tool timeout, launcher cancellation, and shutdown, including stubborn-child terminate/kill escalation and confirmed reap.
- All tool capability-gate cases.
- Model selection and supported thinking/effort options.
- Debug traces showing no credential-content reads or credential mutation by proxy code; metadata-only path observations remain permitted.
- Exact pinned SDK/CLI startup checks and a negative test for version mismatch.

### 15.4 Comparative behavior and benchmarks

Where the user separately has legitimate API access, compare identical supported prompts across:

1. Raw Anthropic Messages API.
2. This prompt-isolated Agent SDK proxy.
3. Normal Claude Code.

Record behavior differences without treating nondeterministic text equality as a correctness assertion. Measure:

- Process startup time.
- Time to first SDK event and first text token.
- Total response time.
- Number of subprocesses.
- Per-session and total memory.
- Streaming queue overhead.

The proxy should be observably closer to raw Messages behavior than normal Claude Code in prompt contents and exposed tools, while acknowledging that the transport remains Claude Code/Agent SDK. Compatibility claims are published per mode and parameter profile. At least one representative full-history agentic harness must pass black-box one-shot, linear-session, streaming, retry, and cancellation tests before the corresponding preset is advertised. The API key supplied to a harness in automatic mode is a derived per-run routing token, never the stable master key. Tool compatibility remains unadvertised until the Phase 3 gate passes.

## 16. Minimal internal structure

Keep dependencies and modules small:

```text
proxy/
  server.py              # routes, authentication, SSE
  models.py              # public and canonical types
  anthropic_adapter.py   # Anthropic validation/events
  openai_adapter.py      # OpenAI-to-canonical translation
  sessions.py            # tokens, heads, actors, TTLs
  agent_backend.py       # isolated SDK client boundary
  tool_bridge.py         # gated in-process MCP relay
  config.py
tests/
```

The HTTP layer depends on a narrow backend protocol such as `start_session`, `send_user_turn`, `resolve_tools`, `events`, and `close`. No adapter imports Claude Agent SDK types directly. Replacing the backend with the raw Messages API must not require changing route or dialect modules.

Use a small async HTTP framework and the official Agent SDK. Avoid a database, task queue, cache server, frontend, plugin framework, or general-purpose proxy abstraction.

## 17. Delivery phases and gates

### Phase 0: feasibility spikes

- Recheck Anthropic's current subscription/third-party-login policy and preserve the personal-local-only scope; stop if that workflow is no longer permitted.
- Pin and record one exact Python Agent SDK and Claude CLI pair.
- Prove prompt isolation against the installed SDK/CLI version.
- Prove compaction is disabled and no transcript/prompt state is written to disk.
- Prove the effective child environment is allowlisted and authentication provenance is `existing_claude_login`; a synthetic clean config root is optional research, not a core gate.
- Prove long-lived linear sessions across completed responses.
- Execute the complete external-tool capability gate.
- Capture real streaming event traces needed by both dialect adapters.
- Record observable failures honestly, but do not require a native sandbox, sealed runtime, signed evidence ledger, or hostile same-UID containment before Phase 1.

A failed policy, prompt-isolation, persistence, or core-session spike stops the project. A failed tool spike removes tool support from v1 but does not block text-only sessions.

### Phase 1: text-only proxy

- Health, models, capabilities.
- Anthropic one-shot, automatic session, and explicit session endpoints.
- Streaming and non-streaming.
- Strict unsupported-field behavior and explicit compatibility profile.
- Session lifecycle, authentication, diagnostics, and limits.

### Phase 2: OpenAI adapter and launcher

- Chat Completions translation.
- Per-run token launcher with common environment-variable presets.
- Protocol conformance and failure-mode documentation.

### Phase 3: gated external tools

- Enable tools only if every required correlation/lifecycle test passes.

### Phase 4: gated multimodal content and comparative diagnostics

- Add images, documents, and richer tool results one content type at a time after conformance tests.

## 18. Rejected alternatives

### Direct `claude -p` per request

Rejected because it is the same Claude Code runtime with a less typed boundary, does not restore inference controls, and would still require history serialization for arbitrary stateless requests.

### Fresh Agent SDK query per multi-turn request

Rejected because the public SDK cannot seed arbitrary assistant/tool history. Transcript-shaped prompt reconstruction is explicitly forbidden.

### Global transcript-fingerprint routing

Rejected because identical histories, retries, stale prefixes, and branches are ambiguous. Fingerprints validate a selected session only.

### Proxy-owned tool execution

Rejected because the caller harness must retain tool policy and execution ownership, and because enabling Claude Code built-in tools violates prompt purity.

### Silent sampling emulation

Rejected because prompt instructions, local truncation, or guessed stop reasons cannot faithfully implement temperature, top-p, top-k, stop sequences, or max tokens.

### `--bare`

Rejected because it also disables the intended subscription credential path.

## 19. Known residual limitations

- The proxy consumes subscription limits and depends on the user's separately established Claude login and current Anthropic policy; it is not presented as an approved third-party login integration.
- It is suitable only for the individual user's local workflows, not credential routing for other users.
- A live conversation consumes an SDK client/subprocess and in-memory state.
- Restart, eviction, or SDK desynchronization loses the session.
- An unmodified client can safely use only one active conversation per generated run token.
- Multiplexed clients need the generic session extension or an equivalent existing request hook.
- Unsupported inference parameters prevent exact vLLM/SGLang equivalence.
- Disabling compaction preserves prompt purity but makes sufficiently long sessions terminate instead of automatically reclaiming context.
- Prompt isolation is bounded by public Agent SDK controls and testable observations, not a guarantee about Anthropic's server internals.
- A malicious or compromised same-UID local process is outside the v1 threat model; the proxy does not provide a credential sandbox against it.
- The separate sealed-worker feasibility branch is research-only and is not a prerequisite for, or part of, the trusted-local release artifact.
- Tool support may be excluded if callback correlation cannot be proven.

## 20. Research references

- [Claude Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview)
- [Current Agent SDK use with Claude plans notice](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)
- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)
- [Claude authentication](https://code.claude.com/docs/en/authentication)
- [Agent SDK sessions and persistence](https://code.claude.com/docs/en/agent-sdk/sessions)
- [Claude Code environment variables](https://code.claude.com/docs/en/env-vars)
- [Agent SDK agent loop and automatic compaction](https://code.claude.com/docs/en/agent-sdk/agent-loop)
- [Agent SDK system prompts](https://code.claude.com/docs/en/agent-sdk/modifying-system-prompts)
- [Agent SDK custom tools](https://code.claude.com/docs/en/agent-sdk/custom-tools)
- [Agent SDK streaming output](https://code.claude.com/docs/en/agent-sdk/streaming-output)
- [Agent SDK Python option definitions](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/types.py)
- [Agent SDK Python CLI transport](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/transport/subprocess_cli.py)
- [Agent SDK Python MCP bridge](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/sdk_mcp_bridge.py)
- [Anthropic tool-result handling](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)
- [Anthropic Messages API request schema](https://platform.claude.com/docs/en/api/messages)
- [Anthropic stop reasons](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons)
- [Agent SDK arbitrary-history feature request](https://github.com/anthropics/claude-agent-sdk-python/issues/848)
