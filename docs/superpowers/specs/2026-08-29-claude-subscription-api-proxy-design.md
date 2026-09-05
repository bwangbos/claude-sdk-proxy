# Claude Subscription API Proxy — Design Specification

**Status:** Approved trusted-local architecture
**Date:** 2026-08-31
**Scope:** Personal, local-only proxy using Anthropic's official Claude Agent SDK

## 1. Decision summary

Build a small asynchronous localhost proxy with Anthropic Messages-shaped and OpenAI Chat Completions-shaped endpoints. The backend uses the official Claude Agent SDK transport and the user's separately established Claude login. It never offers a Claude login flow and never reads, exports, replays, or impersonates Claude OAuth credentials. This is a personal local integration, not an Anthropic-approved third-party product authentication design; section 2.1 makes that policy boundary explicit.

The proxy has three deliberately distinct operating modes:

1. **One-shot standard mode:** Unmodified clients can make a fresh, single-user-turn text request when their selected parameter profile permits the required wire fields they send. Non-empty tool definitions and prior assistant or tool history are rejected because a one-shot request has no retained session in which to complete the caller-owned tool protocol and the Agent SDK cannot seed an arbitrary native transcript into a new session.
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

### 2.3 Supported platform and durability boundary

V1 supports only Darwin/macOS 14 or newer with the runtime root on a local APFS volume. Network filesystems, FUSE providers, cloud-synchronized placeholders, external volumes lacking the probed semantics, Linux, and Windows are rejected at startup. This narrow matrix is intentional: lifecycle correctness depends on Darwin process identity and enumeration, `setsid`/PGID/SID behavior, same-filesystem atomic replacement, local advisory locks, mode/owner checks, and durable file plus parent-directory synchronization. Windows Job Objects and Linux `/proc`/pidfd implementations are future platform-specific designs, not inferred equivalents.

The native lifecycle helper obtains boot identity from `kern.boottime`, process start/UID/executable/PGID/SID data from Darwin process APIs, and enumerates the anchored group without treating a reusable number as a signaling capability. V1 has no Darwin equivalent of a Linux pidfd, so a numeric PID/PGID plus identity recheck can prove presence or absence but never authorizes a later signal. A process signal is permitted only from a retaining parent/reaper that has not reaped the direct child and keeps it unreaped across the final `waitpid(WNOHANG)` check and signal, or through a live actor's authenticated control channel asking it to terminate itself. Group STOP or KILL is permitted from the anchor's live retaining supervisor while that supervisor keeps the anchor unreaped through group-absence confirmation. When the supervisor is gone but the proxy controller remains, the live anchor remains the incarnation sentinel and may perform cooperative control/TERM that leaves itself running; stubborn or forking descendants produce `cleanup_unconfirmed`. If the anchor subsequently observes EOF on that same inherited controller endpoint, both possible reconcilers are gone. The anchor's continuing `PID = PGID = SID` relationship then provides an incarnation-bound self-cleanup capability: it records `UNCONFIRMED`, sends TERM to its own current group, and after a short monotonic deadline sends KILL to that same group. This is not authority derived from a numeric observation, never authorizes filesystem deletion, and prevents a crashed controller from leaving an immortal anchor. Without one of those incarnation-bound relationships, cleanup requires operator action rather than signaling a numeric identifier.

The durable protocol is scoped to process crashes while the same kernel and mounted local APFS filesystem remain available. It does not promise automatic recovery after kernel panic, reboot, power loss, volume rollback, or storage-controller failure; those cases may leave startup cleanup-only for operator resolution. The exact v1 commit primitives are: exclusive create with mode `0600`; complete bounded `write`/`O_APPEND`; `fcntl(F_FULLFSYNC)` returning success on the file before a record authorizes action; and `fsync` returning success on the open parent directory after create, `renameat`, or `unlinkat`. Nonauthoritative owner replacement writes and full-syncs a same-directory temporary file, calls `renameat`, then syncs the parent directory. Journal creation uses `openat(O_CREAT|O_EXCL|O_APPEND)`, `fcntl(F_PREALLOCATE)`, the complete `INTENT` append, full file sync, then parent-directory sync. Journal deletion calls `unlinkat` only after cleanup confirmation, then syncs the parent directory. Any short write, unsupported call, or nonzero required-sync result fails closed before dependent action. Phase 0 records the exact OS/build/filesystem tuple and runs process-kill probes for these sequences, parent-held zombie nonreuse, forbidden verify-to-signal races, supervisor-owned group signaling, anchor-only cooperative fallback, lock release, append ordering/interleaving/torn-record recovery, reserved-block behavior, session/group membership, and process-identity reuse. Startup reprobes syscall/filesystem support; a true OS-crash/reboot durability claim would require a separate sacrificial-volume fault program and approved design.

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
| Retired run-token digest | Not a credential and grants no authorization. During its short tombstone TTL, an exact digest can only receive the terminal `410` envelope for its own inference endpoint or exact run deletion path. |
| No credential | `/health` only, unless the server was started with `--no-auth`. |

The server mints and signs every derived run token; the launcher only requests, receives, conveys, and revokes it.

Active and retired run-token lookup is atomic under the run registry lock. An active token whose monotonic deadline has elapsed, or a token being revoked, is first removed from the authorizing map and replaced by a non-authorizing keyed digest record containing only its run ID, bound dialect, stable terminal code/cause, and tombstone deadline. The same transition closes any bound automatic session with that reason and transfers its resources to teardown before the retired record becomes observable. The global lock order is run registry then actor; actor code never acquires the run registry while holding its transition lock, and teardown runs after both are released. A request matching that retired digest never becomes an authenticated principal, never reaches normal endpoint authorization or session selection, and cannot read or mutate any state. It may receive only the stored `410 session_expired` or `410 session_closed` envelope on the bound dialect's inference endpoint or `DELETE /_proxy/runs/{its_exact_run_id}`; every other endpoint returns `401 invalid_local_api_key`. After the token-tombstone deadline, the digest record is erased and every use returns `401`.

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

An explicit-mode idempotency record is keyed by `(authenticated local principal, selected session ID, Idempotency-Key)`. Its fingerprint is HMAC-SHA-256 under a random process-local key and covers the HTTP method and normalized endpoint/dialect, supplied session head, canonical validated request body including immutable configuration and full submitted history, streaming mode, selected parameter policy, and every allowlisted framing-affecting header such as `anthropic-version`. It excludes authorization material, `Host`, `Origin`, connection headers, request IDs, and other transport-only metadata. Automatic mode has no caller idempotency header: within its run-owned session, the same canonical fingerprint is the retry identity described in section 5.3. One-shot mode has no retry record.

Request processing authenticates and selects the session/run first, enforces its deadline under the owning lock, then performs the scoped idempotency lookup before current-head or transcript-staleness rejection. A committed match replays its recorded status, replayable response headers, recorded result head, and body/SSE bytes exactly; the recorded result head may now be stale and never advances the caller across later unseen segments. Volatile connection metadata such as `Date` is not part of the replay record. Under the actor lock, replay admission acquires a reference-counted immutable snapshot of that record and reserves one bounded writer slot/queue before releasing the lock. That admission is the linearization point: expiry, deletion, shutdown, or run revocation occurring afterward prevents new snapshots but does not alter or cancel the admitted bytes. Its unified response-write lease starts at admission; a slow or disconnected writer is force-closed at the lease deadline and releases its reference/accounting exactly once. An in-flight match follows the deterministic rules in section 8.3; a key match with a different fingerprint returns `409 idempotency_conflict`. Only a record miss proceeds to current-head, transcript, and reservation validation. This ordering makes a historical exact retry replayable even though its original head is no longer current, without allowing a key or fingerprint to select a session globally.

Committed idempotency records are never evicted while their session remains live and nonterminal. Each session has configured maximum committed-segment count, per-segment cached bytes, and aggregate cached bytes. A server-wide replay-byte total counts every record continuously from reservation through the release of its last admitted snapshot, including after its session becomes terminal. Cached-byte accounting covers the serialized body/event stream, recorded status, every replayable header including the recorded result head, fingerprint/key metadata, and fixed record overhead; live reservations count against both entry and aggregate limits. Per-connection writer queues and snapshot references have separate global count/byte limits reserved at original, retry-waiter, or replay admission as applicable. On a new operation, the actor atomically reserves its idempotency entry/full per-segment bytes in session and server budgets, active-generation slot, original response-writer slot, and writer-queue allowance before it creates a head/idempotency operation record or enters `GENERATING`. Any failed reservation releases the entire bundle and leaves state/head/cache unchanged: idempotency capacity returns `409 idempotency_capacity`, while generation or writer capacity returns its documented `429`. Existing exact replays remain available. Only after the full bundle succeeds may the operation record and next head be created. On commit, unused reserved bytes are released. If a non-streaming or streaming operation exceeds its reserved per-segment allowance, the proxy cancels it, emits the dialect's terminal stream error when headers were already sent, and moves the session to `LOST` without committing its head or idempotency record. Entering `LOST` prevents new replay admission and drops the cache owner's references immediately; any already admitted immutable snapshot drains under its response-write lease before its final content reference and global bytes are released. Later lookups return only `409 session_lost` until the reason-only tombstone expires. The client must start a new session or run token after capacity rejection or loss.

Session and run `ttl_seconds` values define immutable wall-clock `expires_at` values for clients and corresponding monotonic deadlines computed at creation. They never refresh, pause, or extend during generation, idleness, retries, or tool waits; runtime decisions use only the monotonic deadline. A bound automatic session cannot outlive its run token. A separate monotonic pending-tool deadline may close the session earlier but never later than the session deadline.

Each accepted operation also has a configured monotonic deadline that begins when its head/idempotency reservation is created and the actor enters `GENERATING`, including lazy SDK-client construction. Timer callbacks are advisory wakeups, not the source of truth. Under the run-registry lock, active-token authorization checks run expiry before selection. Under the actor transition lock, every request checks session/run expiry before idempotency replay or reservation, and every completion checks both session/run and operation deadlines before committing a head or replay record. `now >= deadline` deterministically selects the applicable terminal transition even if a timer callback was delayed. When one lock acquisition observes multiple elapsed deadlines, precedence is run expiry, session expiry, pending-tool timeout, then operation timeout; a deletion or shutdown transition that acquired the lock earlier is already terminal and wins. The completion timestamp is the proxy's monotonic clock read under the actor lock after SDK `ResultMessage` validation and iterator completion; SDK event timestamps and earlier arrival times do not make a late commit on time.

If the operation deadline wins, the proxy cancels the SDK operation, commits neither the reserved head nor idempotency record, closes the session with reason `operation_timeout`, and transfers all owned resources to teardown. Before response headers this operation receives `504 operation_timeout`; after streaming headers it receives the dialect's terminal error event and connection close. There is no recoverable post-reservation timeout. Validation and capacity rejection occur before reservation and therefore do not use this outcome. A one-shot operation follows the same cancellation and teardown rule but has no reusable session tombstone.

Session IDs and heads are opaque, random, and contain no Claude credential or SDK session value.

#### Explicit session creation

`POST /_proxy/sessions` accepts a proxy control object rather than a Messages request:

```json
{
  "dialect": "anthropic",
  "model": "configured-model-name",
  "system": "",
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

The initial head represents an empty native conversation with fixed configuration; the SDK process may be created lazily on the first valid user turn. V1's `system` value is one plain string, including the empty string; arrays, structured blocks, cache-control metadata, and file/preset forms are rejected with `400 unsupported_parameter` because the pinned Python Agent SDK cannot preserve their public block boundaries. Every public request must repeat the same model, system prompt, tools, and thinking configuration so the proxy can validate rather than infer them. `DELETE` closes the actor and leaves a short-lived reason-only tombstone.

### 5.3 Automatic linear-session mode

`claude-proxy run -- <harness command>` requests a server-minted per-run proxy token and supplies the harness's ordinary base-URL and API-key environment variables. No harness source modification is required.

Against a separately running server, the launcher first calls `POST /_proxy/runs` with a dialect, parameter policy, and TTL. The server mints and returns a `run_id`, an opaque short-lived API-key value, and its expiry. When local authentication is enabled, this control request requires the master key and the child receives only the derived token. The first valid public request made with that token binds its model, system prompt, tool definitions, and thinking configuration; subsequent changes are rejected. `DELETE /_proxy/runs/{run_id}` revokes the token and closes its session.

For a separately running server with a generated master key, `claude-proxy run` requires `--api-key-file <path>` or `LOCAL_PROXY_API_KEY_FILE`; it validates file ownership, regular-file type, exact mode, and runtime path before reading. There is no global implicit key-file search. A launcher that starts its own server transfers the master key in memory and never exposes it to the harness. On orderly shutdown the server removes its key file after revocation and cleanup handoff. Runtime-directory removal occurs only after the crash-cleanup reconciliation in section 12 confirms that no owned supervisor/process/workdir remains; a stale key is never reused.

Each generated token owns at most one current live conversation:

- The first valid request creates the session.
- Later requests must extend the recorded public head exactly.
- An identical request under the same token is necessarily treated as a retry and returns the cached response; the proxy cannot distinguish it from a user's intention to start a second identical conversation.
- A stale, divergent, or second initial conversation returns `409 session_ambiguous` with instructions to start a new run token or use explicit mode.
- A new token is required when starting a separate conversation.

The run record has an irreversible lifecycle: `UNBOUND -> BOUND(session_id, terminal_cell) -> TERMINAL_BINDING(reason, status)`. It never returns to `UNBOUND`. The terminal cell is a small content-free atomic object referenced by both the run record and actor. Before an automatic actor publishes any `LOST` or `CLOSED` transition, it compare-and-swaps that cell from live to the same terminal reason/status; the session may later remove its own tombstone without erasing the run's terminal binding. An active token whose cell is terminal can still use its otherwise authorized model/capability endpoints until run expiry, but every inference request returns the stored `409 session_lost`, `410 session_expired`, or `410 session_closed` and can never bind a fresh actor. Run expiry or explicit revocation then performs the non-authorizing retired-digest transition in section 5.1. Token lookup reads the cell under the run-registry lock; actor request admission rechecks it under the actor lock, so the cell CAS is the linearization point for a terminal race without reversing the global lock order.

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

A new live automatic or explicit session may begin with:

- An optional caller system prompt represented as one plain string.
- One user turn containing supported content blocks.
- A fixed model and fixed initial tool definition set.

For a live session, the caller may resend the full public history as standard clients normally do. The proxy verifies that the submitted prefix exactly matches events previously exposed by the proxy, then sends only the genuinely new native input into the existing SDK session.

The recorded public transcript is validation state, not reconstructed model context. It is held in memory, scoped to a live session, and deleted on expiry or shutdown.

The following produce `409` or `422` rather than prompt serialization:

- Unknown prior assistant history.
- A request based on an expired or terminal session.
- A stale head.
- Branching from an earlier assistant message.
- Changing model, system prompt, tool definitions, thinking configuration, or dialect mid-session.
- Mixing a pending tool result with unrelated new user text.

### 6.2 Field support

The exact capability matrix is published by `GET /_proxy/capabilities` and versioned independently of model names.

| Public field | v1 behavior |
|---|---|
| `model` | A configured public alias maps to one exact validated backend model ID; immutable within a session and verified on every response before content is exposed. |
| `system` / system messages | One plain string at session creation; passed without proxy text and immutable thereafter. Anthropic system arrays/blocks/cache metadata and structured OpenAI system content are rejected. |
| `messages` | Supported under the transcript rules above. |
| `stream` | Supported for text; tool streaming remains gated. |
| `tools` | Gated for automatic and explicit live sessions. A non-empty tool set is rejected in one-shot mode before SDK mutation because tool results require a continuation request. |
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

- The one optional leading OpenAI `system` message must contain a plain string and becomes the canonical system prompt.
- User messages become canonical user blocks.
- Assistant text becomes canonical assistant text.
- Assistant `tool_calls` and `tool` messages map to canonical tool-use and tool-result events only when tool support is enabled.
- OpenAI chunk ordering and `[DONE]` framing are generated from canonical stream events.
- Unsupported OpenAI-only fields are rejected before any SDK state mutation.

The adapter does not invent usage, finish reasons, tool errors, or sampling semantics that the backend did not provide.

### 6.5 Protocol details

- Anthropic requests require `anthropic-version: 2023-06-01`; unsupported versions receive `400 unsupported_version`.
- Unknown request fields are rejected. Compatibility policy applies only to its documented allowlist and never turns on general extra-field ignoring.
- The OpenAI v1 subset accepts at most one leading `system` message with string content and rejects structured system content, `developer` messages, and later system messages rather than flattening blocks or merging roles with different semantics.
- Public response IDs (`msg_lp_*`, `chatcmpl_lp_*`) and OpenAI timestamps are proxy-generated correlation values. They do not expose or claim to be Anthropic IDs.
- Stop mappings are allowlisted from observed SDK events. `end_turn` maps to Anthropic `end_turn` and OpenAI `stop`; gated tool use maps to Anthropic `tool_use` and OpenAI `tool_calls`. Unknown reasons produce a protocol error rather than a guessed mapping.
- Exact SDK-supplied usage is returned when present. If a required usage value is unavailable, non-streaming requests fail with `502 sdk_protocol_error`; a stream already in progress emits a terminal error event and marks the session lost. Zero is never used as a placeholder.
- Official Anthropic and OpenAI client libraries are black-box compatibility fixtures for headers, error envelopes, SSE parsing, IDs, timestamps, usage, and finish reasons.

## 7. Backend isolation and prompt purity

Each live conversation owns one long-lived `ClaudeSDKClient` actor. The actor alone may call or drain that client; SDK clients are never shared concurrently across event loops, tasks, or conversations.

The effective options are equivalent to:

```python
ClaudeAgentOptions(
    model=exact_validated_backend_model_id,
    system_prompt=caller_system_string_or_empty,
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

Startup validation is not sufficient for a lazily created child. The pinned Python SDK fixes model, system-prompt string, and optional MCP definitions in construction options, so v1 does not claim those immutable values are absent from the trusted local CLI process during initialization. Instead, every new child connects behind a model-input gate: no user turn is released, and Phase 0 packet/control traces must prove that no caller system string, tool schema, or user content is submitted to an Anthropic model or other network peer before attestation. During that content-free initialization exchange, the proxy validates the child's exact executable/version, effective environment fingerprint, endpoint/provider selection, and positive non-secret authentication-source evidence against the feasibility manifest, then records the child-attestation state. If the SDK cannot expose this evidence before its first model/network submission of caller configuration, the subscription backend remains unavailable. The gate must also prove either that source selection is fixed for that connected child's lifetime or that the same evidence can be revalidated before every later turn without model mutation. In the latter case, every turn revalidates before input release; a changed, missing, or ambiguous signal closes the actor before mutation and returns `503 sdk_unavailable`. Stored login/profile contents are never opened by the proxy.

The feasibility manifest also maps every public model alias to one exact backend model ID for the pinned subscription account and runtime. Generic moving aliases and runtime fallback are not advertised. Child initialization must accept the exact ID and expose the resolved selection when the runtime provides it. For every response, the proxy buffers all content until the first authoritative SDK message-start or assistant envelope identifies that exact backend model; every later model-bearing event must agree. A missing, fallback, or mismatched model produces `502 sdk_protocol_error`, commits no success framing, and moves a reusable actor to `LOST`. Streaming therefore never releases content before model identity is validated. A tuple for which the pinned runtime cannot expose an authoritative pre-content model identity is omitted from capabilities. `/_proxy/capabilities` and `/v1/models` distinguish the public alias from its exact validated backend ID.

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
2. Validate request syntax, field support, immutable configuration, and framing inputs sufficiently to construct the canonical idempotency fingerprint; reject malformed or unsupported input before changing SDK state.
3. Perform the scoped idempotency lookup. Return a committed replay, apply the non-streaming-wait or streaming-retry rule to an in-flight match, or reject a conflicting key before current-head and transcript-staleness checks.
4. On a record miss, validate the current head and transcript prefix.
5. Convert only the genuinely new user turn into the SDK's native input form.
6. Atomically reserve the complete capacity bundle defined in section 5.2. On success only, create the operation/next-head record and enter `GENERATING`; on failure, release the bundle and return the capacity error with no state change.
7. Start or continue the actor-owned SDK query, then drain and translate SDK events continuously.
8. For an ordinary answer, continue draining through a successful SDK `ResultMessage` and iterator completion before atomically recording public events, activating the reserved head, finalizing the response cache, and releasing the buffered terminal-success framing to admitted writers.

An `AssistantMessage` or public `message_stop` does not by itself commit an ordinary turn: result status, usage, and trailing protocol events must validate first. Tool use is the explicit exception because the public API must return control while the internal query remains suspended; it commits only after the complete tool-call set has been finalized, all corresponding callbacks are known to be suspended, and the actor can enter `WAITING_FOR_TOOLS` atomically. Section 8.2 defines that transition.

### 8.2 Actor state machine

The logical session record has five explicit states while it exists. Removal after a tombstone TTL is record absence, not a sixth state:

```text
IDLE
  -> GENERATING
  -> LOST                 asynchronously detected unusable SDK transport/process

WAITING_FOR_TOOLS
  -> GENERATING
  -> LOST                 unusable transport/process or suspended-callback corruption

GENERATING
  -> IDLE                 ordinary commit, or proved-safe rollback to prior IDLE
  -> WAITING_FOR_TOOLS    tool-use commit, or proved-safe rollback to prior tool wait
  -> LOST                 unrepairable stream/SDK/state failure
  -> CLOSED               reasoned terminal result, timeout, expiry, deletion, or shutdown

IDLE
  -> CLOSED               expiry, deletion, or shutdown

WAITING_FOR_TOOLS
  -> CLOSED               tool timeout, deletion, expiry, or shutdown

LOST
  -> [record removed]     reason-only lost-tombstone TTL elapsed; later lookup is absent/404
```

Head and idempotency rules:

- A request at active head `hN` reserves `hN+1` and an operation record before model state changes.
- For non-streaming output, `hN+1` is returned only after it becomes active.
- For streaming output, `hN+1` is returned in response headers as a provisional value because headers precede completion. It becomes active only when the complete terminal-success bundle is validated and atomically committed with state/cache; the bundle is released to writers afterward, so socket delivery is not the commit point.
- If a stream fails after exposing a provisional head but before commit, the session becomes `LOST`; a later request using either head receives `409 session_lost`.
- A committed tool-use response activates `hN+1`, finalizes the first HTTP request's idempotency record, and enters `WAITING_FOR_TOOLS` only after the full tool-call set and suspended callbacks are established. The internal receive loop remains alive.
- A tool-result request is the only new operation accepted in `WAITING_FOR_TOOLS`. It must use `hN+1`, must carry a new idempotency key in explicit mode, and reserves `hN+2` before resolving callbacks.
- A continuation may commit another fully established tool-use boundary and return to `WAITING_FOR_TOOLS`, or commit a final answer and return to `IDLE` only after a successful internal SDK `ResultMessage` and iterator completion.
- While a session is live and nonterminal, an exact retry of any committed public segment returns that record's original result head, status, replayable headers, and body/event bytes without touching the SDK. Its result head may be stale relative to the session's current head.
- If any component of the atomic capacity bundle is unavailable, the request returns its `409`/`429` before creating the reserved head/operation record or mutating SDK/state; existing exact retries continue to work.

Entering `LOST` or `CLOSED` atomically transfers the SDK client and tracked process/workdir handles to a teardown owner, makes them unreachable to request handling, drops all actor-owned transcript, response, prompt, tool, and idempotency-cache references, and starts bounded teardown. No new content reference can be acquired after that transition. A writer promoted after commit (original non-streaming, admitted waiter, completed original stream tail, or historical replay) holds an immutable independently reference-counted response snapshot and may finish only under its unified response-write lease; completion or forced close releases content/global accounting exactly once. An original writer still attached to an uncommitted operation has no snapshot authorization: a terminal transition may make only the specified dialect error writable, starting the unified lease immediately beforehand if it has not started, then closes it and releases its reservation through the same exact-once CAS. Terminal cleanup therefore waits for no network client and makes no claim of immediate erasure while an already authorized committed response is still being written. An actor-owned watcher reports asynchronous child/transport death through the same serialized transition lock as requests, expiry, and deletion; if the native session is no longer usable, the first winning transition from `IDLE`, `GENERATING`, or `WAITING_FOR_TOOLS` is `LOST`, and the watcher transfers ownership exactly once. Suspended-callback corruption uses the same rule. For an automatic session, the terminal-cell CAS described in section 5.3 occurs before publishing this actor transition. The reason-only session tombstone retains only opaque ID, machine-readable terminal reason, and expiry. The cleanup registry retains the minimum SDK/process/workdir handles, identities, attempt state, and last error required to close and confirm absence; the child SDK/CLI may still hold model state in memory until termination succeeds, so cleanup failure is never described as complete erasure. A lost tombstone returns `409 session_lost`. A terminal reason of TTL expiry returns `410 session_expired`; context exhaustion, operation timeout, deletion, tool timeout, launcher exit, or shutdown returns `410 session_closed` on later lookups, with the stable cause retained in the envelope. Tombstone expiry removes the session record, is independent of cleanup retry and admitted-writer draining, and never discards unconfirmed cleanup authority. After tombstone expiry an explicit session ID is unknown and returns `404 session_not_found`; an expired/revoked run-token digest is retained only for the configured token-tombstone TTL and then becomes `401 invalid_local_api_key`. If any closing cause wins a race with generation, the actor transfers ownership and closes; it never returns to `IDLE`.

#### Post-reservation failure contract

The native-mutation boundary is the first point at which a new user turn may have been accepted by the SDK/CLI or any suspended tool callback has been resolved. The backend sets its local `mutation_possible` flag immediately before invoking either action; an exception or transport loss after that point is treated as possible mutation unless the pinned SDK supplies positive contrary evidence. A rollback to a prior live state is allowed only when `mutation_possible` is false, the prior native session is still usable (or is an empty session whose failed lazy client was completely torn down), and no callback was resolved. Unknown cases fail closed; they are never retried against guessed SDK state.

Every request outcome is classified by this table. “No commit” means the reserved head/idempotency record is discarded. Existing cache ownership is retained only for the proved-safe live rollback row; every `LOST` or `CLOSED` row blocks new response-snapshot admission and drops cache-owner references under the terminal transition above, without mutating an immutable snapshot admitted to any writer before the transition.

| Cause and point | State outcome | Current HTTP/SSE outcome | Head, replay, and later lookup |
|---|---|---|---|
| Authentication, validation, transcript, stale-head, or capacity rejection before reservation | State unchanged | Its documented `4xx`; no streaming headers | No reservation; prior commits remain replayable. |
| SDK lazy construction or proxy-internal failure after reservation, with `mutation_possible=false` and the safe-rollback proof above | Restore the exact prior `IDLE` or `WAITING_FOR_TOOLS` state | `503 sdk_unavailable` or `500 internal_error`; streaming headers have not yet been sent | Release reservation; prior commits remain replayable; retry may create a new operation. |
| SDK lazy construction, transport, or proxy failure with `mutation_possible=false`, but safe rollback cannot be proved because the prior client is unusable, newly allocated teardown is unconfirmed, or state certainty is otherwise lost | `LOST` | `503 sdk_unavailable` for backend/construction failure or `500 internal_error` for proxy failure; no SSE headers were sent, and the JSON error uses the unified write lease | No commit; cache owner drops replay; transfer all owned/failed resources to cleanup; later lookup is `409 session_lost`. |
| Context exhaustion reported as the classified terminal SDK result | `CLOSED`, reason `context_exhausted` | `422 context_exhausted` before headers, or terminal dialect error after headers | No commit; later lookup is `410 session_closed` with the cause. |
| Accepted-operation deadline | `CLOSED`, reason `operation_timeout` | `504 operation_timeout` before headers, or terminal dialect error after headers | No commit; later lookup is `410 session_closed` with the cause. |
| Absolute TTL, deletion, pending-tool timeout, launcher exit, or server shutdown wins the terminal race | `CLOSED` with that stable reason | `410 session_expired` for TTL; otherwise `410 session_closed`; an active stream receives the dialect terminal error, then closes | No commit for an unfinished operation; later lookup uses the same reason mapping. |
| SDK unsuccessful result other than a positively classified context exhaustion, protocol/translation/required-usage failure, callback corruption, response/cache overflow, or proxy failure with `mutation_possible=true` | `LOST` | Respectively `502 sdk_operation_failed`, `502 sdk_protocol_error`, `502 response_limit_exceeded`, or `500 internal_error` before headers; otherwise the corresponding terminal dialect error | No commit; cache owner drops replay; pre-admitted snapshots may drain; lookup is `409 session_lost` until tombstone removal. |
| SDK/CLI transport or process loss | Safe rollback only if the proof above holds; otherwise `LOST` | `503 sdk_unavailable` before headers when possible, or terminal dialect error after headers | Safe row retains prior replay; otherwise no commit, cache owner drops replay, pre-admitted snapshots may drain, then lookup is `409 session_lost`. |

The proxy has no separate public “cancel operation but keep session” action. Explicit `DELETE`, launcher exit, and shutdown are closing causes. An HTTP disconnect alone follows section 8.4 and does not select a row until the retained operation itself reaches an outcome.

Automatic mode applies the same transitions internally but does not require the harness to see head values.

### 8.3 Concurrency

- One operation may mutate a session at a time while `GENERATING`.
- A second request with a different idempotency key during `GENERATING` receives `409 session_busy` rather than waiting indefinitely.
- An exact in-flight non-streaming retry may wait on the same operation future only after atomically reserving one slot under both a per-operation waiter limit and a server-wide waiter/response-writer limit, plus its fixed bounded writer-queue byte allowance. The original request owns the first writer reservation. If admission capacity is unavailable, the retry receives `429 retry_waiter_capacity` with `Retry-After: 1` and the operation is unaffected. On commit, each waiter reservation is promoted without further allocation to a reference on the completed immutable response plus its already reserved writer queue, and the waiter receives the response from byte zero without invoking the SDK again. Disconnect or terminal error releases the reservation; if the operation terminates, all admitted waiters receive the same current-operation error before subsequent requests see the tombstone.
- An exact in-flight streaming retry never attaches to a live tail because v1 has no stream cursor. It receives `409 operation_in_progress` with `Retry-After: 1`, while the original operation continues and retains a complete byte-zero replay spool within its reservation. After commit, the same retry receives the exact cached stream from byte zero. If the spool exceeds its bound, the session becomes `LOST` as specified above.
- Every original operation reserves one server-wide response-writer slot and fixed queue allowance before SDK mutation; lack of capacity returns `429 response_writer_capacity`. A streaming original starts its monotonic response-write lease immediately before SSE headers. A successful non-streaming original starts it at commit when it acquires the committed response reference. Any original request that instead produces a post-reservation JSON error—including a streaming request failing before SSE headers—starts the same lease immediately before that error becomes writable. An admitted non-streaming retry waiter is bounded by the operation deadline while waiting and starts the lease when promoted at commit; if the operation errors instead, its lease starts when that shared error becomes writable. A committed historical replay starts its lease at snapshot admission. Completion, disconnect, lease expiry, and terminal-transition races use one exact-once writer-state CAS to close the socket and release queue bytes, waiter/writer slots, and any immutable response reference. A streaming writer whose lease expires before operation commit is detached and closed, but the bounded operation/spool may continue for later committed replay. No writer can retain capacity or content beyond its lease.
- HTTP connection admission is separately bounded at TCP accept and reserves one connection slot plus a fixed small control/error-envelope writer before reading any application bytes. Monotonic accept-to-complete-header, header-to-complete-body, keep-alive-idle, and total pre-operation deadlines run even for anonymous or incomplete requests. The parser enforces request-line bytes, URI bytes, header count, per-header and aggregate-header bytes, declared body bytes, incrementally read body bytes, and a fixed maximum requests per connection; unsupported transfer encodings and ambiguous length framing are rejected. A deadline, limit, parse error, partial-body disconnect, keep-alive expiry, or connection close releases the connection/request/control-writer state exactly once. Once an error becomes writable, the same bounded response-write lease applies. Thus slowloris or idle peers cannot retain the finite admission pool indefinitely, failure to reserve the larger model-response bundle can still return its documented bounded `4xx` without creating an operation, and exhaustion of the base connection pool rejects/closes at accept before request processing.
- `WAITING_FOR_TOOLS` is not considered busy for its one valid matching tool-result request.
- Separate sessions may run concurrently up to configured global limits.
- There is no implicit branch creation.

### 8.4 Disconnects and cancellation

The server distinguishes HTTP-client disconnect from explicit model cancellation:

- A disconnect does not automatically kill an operation. Non-streaming retries may wait on its shared future; streaming retries receive `409 operation_in_progress` until they can retrieve a committed byte-zero replay.
- Buffers are strictly bounded. Exceeding the bound cancels the SDK operation and moves the session to `LOST` without committing its reserved head or idempotency record.
- Operation timeout, absolute session/run expiry, explicit session deletion, process shutdown, or tool timeout cancels pending work and transfers the SDK client to teardown. Operation timeout uses the `504`/terminal-stream behavior defined above; subsequent lookups see the reasoned closed tombstone.
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
2. Repeat for streaming, non-streaming, disconnect, retry, cancellation, absolute TTL expiry, pending-tool timeout, SDK interrupt, and graceful shutdown.
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
- For ordinary answers, forward nonterminal text/content deltas without waiting for completion while simultaneously appending their exact serialized bytes to the operation's bounded replay spool. Buffer the entire success-bearing terminal bundle: Anthropic `message_delta` fields carrying stop reason/usage plus `message_stop`, and OpenAI final chunks carrying `finish_reason` or usage plus `[DONE]`. No stop reason, final usage, success marker, or equivalent semantic success is exposed until a successful SDK `ResultMessage`, iterator completion, and under-lock deadline check validate the turn. The actor then appends the ordered terminal bundle to the replay record, commits head/cache/state, and releases that immutable bundle to each admitted writer as one ordered enqueue action. A failure before commit emits only the dialect terminal error and never any buffered success frame.
- For tool use, likewise buffer every stop/finish/usage success frame until the complete public call set is known, all matching MCP callbacks are suspended, and the `WAITING_FOR_TOOLS` commit succeeds; then release the ordered terminal bundle.
- Do not synthesize token usage when the SDK has not supplied it.
- Mark unsupported or unavailable usage fields according to the compatibility dialect rather than returning invented zeroes.
- Validate all request fields before sending SSE headers.
- Complete lazy SDK construction and all conditions for a proved-safe rollback before sending SSE headers. Headers may be sent only after `mutation_possible` becomes true or a terminal pre-header result is already known.
- After headers are sent, terminal backend errors become dialect-appropriate error events followed by connection close.
- Bounded queues provide backpressure between the SDK actor and HTTP writer.
- The replay spool is distinct from the writer queue, starts at byte zero, includes all serialized SSE events and replayable response metadata, and consumes the operation's reserved per-segment/aggregate byte budget even when the original client disconnects.
- Time to first byte, first model event, first text token, and final event are measured separately in debug mode.

## 11. Errors

Proxy errors use stable machine-readable codes and the closest public dialect envelope:

| HTTP status | Example code | Meaning |
|---|---|---|
| `400` | `invalid_http_request` | Request line, framing, transfer encoding, or JSON transport shape is invalid or ambiguous. |
| `400` | `unsupported_parameter` | Field cannot be honored under the selected policy. |
| `400` | `invalid_ttl` | Requested TTL exceeds the configured positive bound or is otherwise invalid. |
| `408` | `request_timeout` | Header, body-read, or keep-alive deadline elapsed before operation admission. |
| `401` | `invalid_local_api_key` | Local proxy authentication failed. |
| `404` | `model_not_configured` | Model is not in the configured list. |
| `404` | `session_not_found` | Explicit session tombstone expired or the session ID never existed. |
| `409` | `session_busy` | A different operation is already active. |
| `409` | `stale_session_head` | Request does not extend the current head. |
| `409` | `session_ambiguous` | Automatic token cannot identify a unique valid continuation. |
| `409` | `session_lost` | Live SDK state cannot be proven consistent. |
| `409` | `operation_in_progress` | An exact streaming retry arrived before the original stream committed; retry after the advertised delay. |
| `409` | `idempotency_conflict` | Idempotency key was reused with different content. |
| `409` | `idempotency_capacity` | The live session lacks entry or byte capacity for another replayable segment; start a new session or run. |
| `410` | `session_expired` | Session or run TTL elapsed. |
| `413` | `request_too_large` | Declared or incrementally observed body bytes exceed the configured limit. |
| `422` | `history_unavailable` | Request contains history that cannot be seeded or validated. |
| `422` | `context_exhausted` | The non-compacting native session has reached its usable context limit. |
| `431` | `request_headers_too_large` | Request-line/header count or byte limits were exceeded. |
| `429` | `session_capacity` | Configured live-session/process limit was reached. |
| `429` | `lifecycle_capacity` | A run/session/tombstone/cleanup lifetime slot could not be reserved without eviction. |
| `429` | `retry_waiter_capacity` | Per-operation or server-wide retry waiter/writer capacity is full; retry later. |
| `429` | `response_writer_capacity` | Server-wide response writer/queue capacity is full before operation admission. |
| `500` | `internal_error` | Proxy failure; after possible native mutation the session is lost. |
| `502` | `sdk_operation_failed` | Agent SDK returned an unclassified unsuccessful terminal result. |
| `502` | `sdk_protocol_error` | Agent SDK produced an invalid or untranslatable event. |
| `502` | `response_limit_exceeded` | Generated output exceeded a configured queue, response, or replay-cache bound. |
| `503` | `sdk_unavailable` | Claude login, CLI, or SDK transport is unavailable. |
| `504` | `operation_timeout` | The accepted operation exceeded its deadline; the session was closed without committing the reserved head. |
| `410` | `session_closed` | Session closed; the response cause may be `context_exhausted`, `operation_timeout`, `tool_result_timeout`, deletion, launcher exit, or shutdown. |

Errors never trigger transcript flattening, hidden retries, or automatic session replacement.

## 12. Resource lifecycle

The proxy remains one server process, but the SDK may own one Claude subprocess per live conversation. Therefore:

- Set conservative limits for live sessions, active generations, pending tool calls, non-streaming retry waiters/writers, queued bytes, request size, response size, tool-result size, idempotency entries, per-segment cached bytes, aggregate cached bytes, and total replay bytes.
- Bound all authoritative control registries by fixed-size lifecycle slots: `max_run_lifecycle_records` counts active unbound/bound/terminal runs and their successor retired-token digests; `max_session_lifecycle_records` counts live sessions and reason-only session tombstones; `max_cleanup_ownership_records` counts reserved or allocated supervisor/anchor/journal ownership until confirmed cleanup. No authoritative terminal record is evicted early.
- `POST /_proxy/runs` atomically reserves one run lifecycle slot, one future automatic-session lifecycle slot, and one cleanup-ownership slot before returning a token. Explicit session creation atomically reserves its session and cleanup slots. An automatic run that never binds releases its unused session/cleanup reservations only when it becomes terminal; a bound session keeps its session slot through session-tombstone expiry. A run's slot is reused across active-to-retired transition and released only at token-tombstone expiry. A cleanup slot is released only after confirmed process/workdir absence and durable journal deletion, independently of public tombstones. One-shot admission reserves an ephemeral cleanup slot before allocation and releases it only after confirmation.
- Session TTL, run TTL, session-tombstone TTL, token-tombstone TTL, pending-tool timeout, operation timeout, and unified response-write timeout each have configured positive maxima. A client-supplied session/run TTL outside its allowed range receives `400 invalid_ttl`; startup rejects invalid server timeout bounds. If any required lifecycle/cleanup reservation is unavailable, creation or one-shot admission returns `429 lifecycle_capacity` before creating an ID, token, workdir, journal, or process. Churn therefore cannot bypass count bounds, and an unconfirmed cleanup consumes capacity rather than being forgotten.
- Expire every session and run at its immutable absolute deadline, including during generation; cancellation and teardown use the same ownership-transfer race rules as deletion and shutdown.
- Use a separately configurable pending-tool timeout, capped by the session's immutable absolute deadline.
- Reject new sessions at capacity instead of evicting an active session.
- Close SDK clients and temporary working directories deterministically.
- Bound operation, graceful-close, process-termination, and forced-kill intervals separately. Only resources newly allocated by failed construction, or resources atomically transferred from a session entering `LOST` or `CLOSED`, enter the teardown state machine. Pre-reservation rejection is teardown-free. A proved-safe rollback tears down only its failed newly allocated resources and retains the existing usable client and replay state. Normal close, post-mutation failure, operation timeout, expiry, explicit deletion, and server shutdown transfer all actor-owned resources through the common teardown path.
- The common mode-`0700` runtime root contains a stable reconciliation-lock file. Every startup takes it exclusively before scanning, claiming, signaling, or deleting anything. Each per-instance directory has a mode-`0600` owner record containing instance nonce, OS boot/session ID, proxy PID/start/UID/executable identity, and diagnostic takeover counter, plus a separate stable lifetime-lock file whose canonical file descriptor the proxy holds exclusively until exit. The owner record and lock-file entries are file- and parent-directory-synchronized before the instance creates keys, allocation journals, workdirs, or processes. The owner record proves live instance ownership but is never a destructive-action fence; no transition requires it to commit atomically with an allocation journal.
- Every lock domain in v1—root reconciliation, instance lifetime, per-allocation append admission, and per-allocation destructive action—uses Darwin BSD `flock(LOCK_EX | LOCK_NB)` on a dedicated regular file opened with `openat(O_RDONLY | O_NOFOLLOW | O_CLOEXEC)` after owner/mode/device/inode verification. Each process has one canonical, never-duplicated FD per domain and one in-process mutex shared by all async tasks/threads; code must take that mutex before attempting or releasing `flock`. Lock acquisition retries nonblocking under a monotonic deadline. Forked children close every inherited lock FD before any journal or process action and reopen only the domains they explicitly own; exec also closes them through `O_CLOEXEC`. Lock files are never renamed or unlinked while their protected instance/allocation exists, and no unrelated descriptor for a lock inode is opened or closed. Phase 0 tests same-process task contention, separate opens, accidental `dup`, unrelated-FD close, fork/exec inheritance, owner exit, and process-exit release against the pinned Darwin build. Any behavior differing from this model fails the platform gate.
- A reconciler never touches a directory whose lifetime lock is held by another process. If it can take an abandoned instance's lock, it retains that lock through reconciliation, verifies prior owner absence using boot ID plus PID-start/UID/executable identity, and durably replaces the owner record before examining allocations. A crash after that owner-record update is harmless: a later claimant repeats the abandoned-lifetime-lock check, and every allocation remains independently governed by its own journal. A valid unexpired cleanup generation or its one durably admitted batch may still own action after proxy death; the claimant only observes or waits while it can act. To retire an expired executor, the claimant uses the durable journal-CAS protocol below. After the admitted batch completes or confirmed executor absence releases its action lock, the claimant installs that journal's replacement cleanup generation and may perform cleanup. Every target/file syscall is authorized by the exact admitted-batch rule below. The root lock serializes startup claimants; per-allocation journal CAS serializes them with anchor watchdogs. Releasing the root lock does not release either the claimant's own or claimed abandoned-instance lifetime lock. A live wedged proxy therefore remains owned rather than being mistaken for abandoned, while crash-released locks permit one durable takeover.
- Define ownership as the SDK client, its per-session supervisor shim/reaper, a durable proxy-owned process-group anchor, every outside-group cleanup-helper invocation, and every process/workdir the pinned transport can positively track. The proxy supplies the Agent SDK the shim as its Claude executable. The shim relays the SDK transport but runs outside the CLI process group, remains the unreaping parent of the anchor, and never reaps it until all authorized anchor/group signaling is complete. The anchor is the dedicated group leader, never execs the official CLI, and remains alive as the group-incarnation sentinel until cleanup. Through the anchor, the shim starts the unmodified CLI as a gated group member. A live retaining supervisor may freeze, enumerate, TERM, and if necessary KILL the anchored group while keeping the anchor unreaped through group-absence confirmation. If that supervisor is gone after `RUNNING`, the anchor itself may become the sole `ACTIVE_READY` executor for cooperative process cleanup and issue only an allowlisted group TERM whose handler keeps it alive. Because the unmodified CLI has no authenticated no-fork/quiescence protocol, enumeration cannot prove atomic absence; the anchor must then enter `UNCONFIRMED` and remain alive with the journal, workdir, and cleanup slot. It may not issue group STOP/KILL, declare `DONE`, exit, promote a filesystem helper, or authorize file removal. A pre-`ARMED` journal that proves the CLI was never released may use its narrower fail-dead proof, but a `RUNNING` supervisorless allocation always stays unconfirmed. A CLI leader or descendant may exit without destroying the group-incarnation proof because the anchor remains. Phase 0 must prove the retaining-supervisor path, anchor-as-executor cooperative attempt, and mandatory unconfirmed fallback.
- Each allocation has exactly one authoritative, bounded, mode-`0600` append-only journal in its per-instance directory plus stable, never-renamed destructive-action and append-admission lock inodes. Before allocation, the server reserves the journal's configured hard-maximum blocks with Darwin `F_PREALLOCATE`. The logical EOF has a lower normal-record ceiling and a hard ceiling; their difference is a recovery tail sized for the statically worst-case retirement, handoff, cleanup, and terminal record count. Every physical byte written—including stale, short, torn, checksum-invalid, and superseded records—counts by EOF against the applicable region. `F_PREALLOCATE` is only the physical-space guarantee and is never treated as a logical cap.
- Every append takes the append-admission mutex and `flock` independently of the destructive-action lock, validates the current EOF and canonical parent, selects the record class, and performs exactly one bounded `O_APPEND` write before releasing both. It never holds append admission across full synchronization, waiting, target action, or network I/O. A normal record is rejected if its maximum write could cross the normal ceiling; only the fixed allowlist of retirement/recovery transitions may use the tail, and no write whose maximum could cross the hard ceiling is attempted. Lock acquisition and the preallocated local write have short monotonic deadlines; failure authorizes no action. A tail-capacity or hard-limit failure records an in-memory unhealthy reason when possible, permits no further destructive action or signaling, retains the journal/workdir/cleanup slot, and requires operator resolution. Each record carries magic, format version, allocation nonce, record length, logical sequence, cleanup epoch, previous canonical-record hash, transition payload, and checksum. A short, torn, or checksum-invalid record is never authoritative and the scanner resynchronizes at the next valid magic/length boundary.
- Canonical state is the deterministic chain obtained by scanning complete records in physical append order from `INTENT`: a transition is accepted only when legal and its previous hash names the current canonical record; the first valid child wins and later siblings are stale. An in-memory scan is not durable authority. Before any record can authorize a signal, process/file syscall, child gate, acknowledgement, or deletion, the relying actor performs a durable-head certification loop: under append admission snapshot the complete canonical head hash and physical EOF; release admission; require `F_FULLFSYNC` success on the journal FD; reacquire admission and rescan/fstat; accept the snapshot as certified only if both head hash and EOF are unchanged. If either changed, repeat from the new complete snapshot within a monotonic retry bound or fail closed. This also certifies records appended by another actor; no one relies on an unsynchronized visible tail. After certification, ordinary action still requires the current-head rule or the admitted-batch rule below. Journal records are never edited or compacted while an allocation exists, and admission reserves the fixed hard-maximum journal bytes as part of its cleanup slot. No correctness transition spans owner and allocation files or two allocation journals; a process crash leaves an independently replayable prefix for each allocation.
- Bootstrap is a two-phase durable protocol and allocates nothing before intent durability. First, the server chooses a random allocation nonce and deterministic intended workdir name under the verified runtime root, exclusively creates and preallocates the journal, appends and synchronizes its `INTENT` header, and synchronizes the directory entry. Only then may it create the mode-`0700` workdir. It appends the workdir path/device/inode as the next canonical record before spawning anything.
- Every bootstrap process is fail-dead until its identity is durable. It receives the allocation nonce, an inherited proxy-owned control pipe, a short monotonic acknowledgement deadline, and access only to the journal append-CAS helper. Before acknowledging readiness or creating a child, the newly spawned supervisor self-measures and commits its nonce-bound boot/PID-start/UID/executable/PGID/SID identity as the next canonical record. The server verifies that record and sends a durable-identity acknowledgement. Control-pipe EOF, identity-write failure, lost CAS eligibility, or acknowledgement timeout before that message causes unconditional bounded supervisor exit without spawning the anchor.
- The acknowledged supervisor starts the proxy-owned anchor behind a gate with the same direct proxy-control/fail-dead protocol. The anchor first calls `setsid()` and, before readiness, commits its nonce-bound `PID = PGID = SID`, boot ID, UID, and executable identity as the target group/session incarnation proof. The server verifies that it differs from the supervisor PGID/SID; until acknowledgement, EOF/failure/timeout makes the anchor exit without forking. Only then may the anchor fork a gated CLI bootstrap member. That member likewise commits boot/PID-start/PGID/SID/UID, proves membership in the anchor's target group/session, and records the expected official CLI executable path/device/inode/hash as canonical state `ARMED`; it exits without exec on EOF/failure/acknowledgement timeout. After the server acknowledges `ARMED`, exec is released. The supervisor, anchor, and server then verify actual CLI membership/identity, commit `RUNNING`, and only then permit SDK/model input. Parent-hash CAS orders these self-writes so a losing or stale writer cannot erase a state update.
- After durable acknowledgement, the anchor does not exit merely because the CLI or supervisor channel closes. A live retaining supervisor can complete the forced-cleanup path and reap it. Without that supervisor after `RUNNING`, the anchor may become the sole cooperative cleanup generation and remain alive in `UNCONFIRMED` while the proxy controller endpoint is live; it never delegates process signaling or filesystem cleanup. If that final controller endpoint reaches EOF, the anchor performs the bounded incarnation-bound self-cleanup described above and exits while retaining the `UNCONFIRMED` journal and workdir for later diagnosis. Before acknowledgement it always fails dead. Thus a process crash in any spawn-to-record window leaves either a durably identified process, a gated process required to terminate within the bootstrap grace, an explicit unconfirmed sentinel with a live controller, or retained unconfirmed artifacts after all controlling processes are lost; an `ARMED`/`RUNNING` journal retains the group incarnation even if the CLI leader exits.
- Lifecycle identity and cleanup authority are separate. Supervisor, anchor, helper, and restart-process identities are durably recorded independently of cleanup state, and exactly one cleanup generation exists at a time. `PREPARED` may name an already recorded eligible actor or a not-yet-spawned gated helper. No candidate touches the target or files before it becomes the exact `ACTIVE_READY` executor. `UNCONFIRMED` is a durable no-action state that retains all capacity and artifacts for observation or operator resolution.

| Canonical state | Legal next state | Preconditions and authority |
|---|---|---|
| no generation | `PREPARED(g, candidate, claim_deadline)` | Allocation owner or prior retirement authority; candidate identity/nonce recorded, or helper remains pre-spawn gated. |
| `PREPARED` | `ACTIVE_READY(g, executor, lease, completed_steps)` | Exact candidate self-recorded and acknowledged; prior executor absent; action lock observed available; transition is durable-head certified before gate release. |
| `PREPARED` | `RETIRING_IDLE(g, prior_candidate, authority, authority_epoch, deadline)` | Claim deadline expired; blocks candidate activation. |
| `ACTIVE_READY` | `BATCH_ACTIVE(g, batch_nonce, executor, descriptors, preconditions, completed_steps)` | Exact unexpired executor holds action lock; append admission linearizes the batch. |
| `BATCH_ACTIVE` | `ACTIVE_READY(g, executor, lease, completed_steps')` | Exact executor completed the recorded idempotent batch, durably certified `BATCH_DONE`, and then releases action lock. This cycle may repeat any bounded number of times within journal capacity. |
| `ACTIVE_READY` | `DONE(g, executor)` | Exact executor has no admitted batch and all required process absence, workdir removal, and terminal checks are already proved by completed batches. |
| `ACTIVE_READY` | `RETIRING_IDLE(g, prior_executor, authority, authority_epoch, deadline)` | Executor lease expired before any batch won admission; blocks new batches. |
| `BATCH_ACTIVE` | `RETIRING_BATCH(g, prior_executor, authority, authority_epoch, deadline, exact_batch)` | Lease expired after batch admission; preserves authority for exactly that batch and blocks all later batches. |
| `RETIRING_BATCH` | `RETIRING_IDLE(..., batch_outcome=completed|interrupted)` | Old executor certifies `BATCH_DONE`, or an incarnation-bound parent confirms it absent and the action lock available; all batch descriptors/outcome remain recorded. |
| `RETIRING_IDLE` or `RETIRING_BATCH` | same state with `authority_epoch+1` and replacement authority | Prior retirement-authority deadline expired; preserves generation, prior actor, exact batch/outcome, and all eligibility. The stale authority is fenced by canonical-head checks and gains no target authority. |
| `RETIRING_IDLE` | `PREPARED(g+1, candidate, claim_deadline)` | Prior executor proved absent, action lock available, interrupted batch retained for the next executor, and candidate eligible. |
| any non-`DONE` state | `UNCONFIRMED(reason, retained_state)` | Required signal capability, identity/absence proof, lock, journal capacity, or filesystem proof is unavailable. No automated destructive action follows. |
| `DONE` | journal deletion / slot release | Recorded executor is absent, no helper/anchor/process remains, workdir is absent, and deletion commit sequence succeeds. |

- Irreversible work is linearized by `BATCH_ACTIVE`. The sole `ACTIVE_READY` executor first acquires the destructive-action mutex and `flock`, then append admission; verifies its generation and lease; and appends the bounded ordered syscall descriptors and preconditions. It retains the action lock while performing durable-head certification. Retirement may append `RETIRING_BATCH` meanwhile, but that state explicitly preserves the admitted batch. The executor may execute only those descriptors and, before each syscall, certifies a head that is either its `BATCH_ACTIVE` or a preserving `RETIRING_BATCH`. It appends/certifies `BATCH_DONE` while still holding the action lock, producing `ACTIVE_READY` or `RETIRING_IDLE` as the table requires, then releases the lock. The lock is never held while sleeping or awaiting network/external I/O; TERM/wait/KILL and recursive cleanup therefore use multiple batches separated by unlocked bounded waits. Process exit releases the OS lock, and the durable descriptors make an interrupted batch reconcilable.
- Retirement authorizes only incarnation-bound termination and absence confirmation of the exact expired executor. It never grants target-group signaling, action-lock acquisition, batch reconciliation, or file deletion. If retirement loses append admission to a batch, it must create `RETIRING_BATCH` and wait for completion or safe owner death; no successor activates earlier. The authority may signal only as retaining parent/reaper under section 2.3; otherwise it requests authenticated self-termination and enters `UNCONFIRMED` if the actor remains. Replacement authority is the explicit same-state/next-authority-epoch transition in the table. Instance owner metadata grants no action authority.
- Every spawned helper calls `setsid()` before self-recording and must record `PID = PGID = SID`, boot ID, UID, executable identity, and candidate generation nonce. Its coordinator verifies that helper PGID/SID differ from the target anchor group/session and from the supervisor before promoting `PREPARED` to `ACTIVE_READY` or releasing the action gate. EOF, self-record failure, separation failure, or acknowledgement timeout before release makes the helper exit without cleanup actions. After `ACTIVE_READY`, coordinator death does not revoke it because the durable generation is authoritative. The helper records `DONE` after all batch cycles and exits but never deletes its own identity; a later owner confirms absence first.
- Proxy control-pipe EOF opens a short supervisor claim window. The already-running, durably identified supervisor may be named by `PREPARED` and must acknowledge promotion to `ACTIVE_READY` before that deadline; it then has one fixed cleanup lease. If the supervisor generation expires, the anchor may commit retirement and request supervisor self-termination over their authenticated control channel, but it is not the supervisor's parent and never signals its PID. A wedged or ambiguous supervisor moves to `UNCONFIRMED` and blocks a successor. If the supervisor is proved absent, the anchor may become the next `PREPARED -> ACTIVE_READY` executor and perform the supervisorless cooperative process batch described above; for a `RUNNING` journal it then enters `UNCONFIRMED`, remains the sentinel, and never hands filesystem cleanup to a helper.
- An allocation journal contains only allocation/server/generation nonces, record hashes/sequences/checksums, its cleanup epoch, supervisor, anchor, CLI-member, cleanup-executor and target process-group identities including boot/PGID/SID fields, expected/actual executable identities, leases/lifecycle state, and the session workdir identity. It contains no prompt, transcript, response, tool payload, run token, proxy key, Claude credential, or SDK transport bytes. Every journal create, append, and deletion uses the supported full-file plus containing-directory synchronization protocol where applicable; ordinary buffered writes are insufficient.
- Ordinary teardown performs graceful SDK close and a bounded supervisor request. With the retaining supervisor alive, that parent uses multiple admitted batches to freeze/enumerate the group, perform bounded TERM then forced group KILL if needed, confirm group absence while retaining the anchor unreaped, reap it, and remove the verified workdir. Without the supervisor after `RUNNING`, the anchor becomes the active cooperative executor, sends TERM while preserving itself as sentinel, then records `UNCONFIRMED`; it remains a sentinel while the proxy controller endpoint remains live. EOF on that final inherited endpoint triggers bounded TERM/KILL of the anchor's own current group and anchor exit, retaining the journal and workdir. No path individually signals an enumerated PID or externally signals an orphan PGID. An absent/reused identity may prove the recorded actor is gone for safe file reconciliation, but the replacement process is never signaled. Missing incarnation-bound control, a wedged nonchild, or any other failure retains live handles in the in-memory registry and durable journal, rejects new work through unhealthy status, and continues bounded observation/control-channel retries or requires explicit operator resolution. Actor-owned prompt/session references are dropped regardless of cleanup outcome; every admitted original, promoted-waiter, or replay writer follows the unified bounded lease rule. No claim is made about memory still owned by an unconfirmed child.
- Before accepting work, startup holds the root reconciliation lock, waits the bounded bootstrap fail-dead grace, and examines each instance lifetime lock. Held locks are live-owned and skipped without inspecting or mutating their journals. For an unlocked abandoned instance, startup claims and retains its lifetime lock, verifies prior owner absence, durably installs its diagnostic owner record, and replays each allocation journal independently without acting. A live recorded supervisor or executor must pass all identity/generation/lease checks; a live anchor's group-incarnation proof grants startup no signaling capability. Valid `ACTIVE_READY`, `BATCH_ACTIVE`, or `RETIRING_BATCH` work follows the table. For an expired nonchild actor, startup may commit retirement and request authenticated self-termination but never signals its PID/PGID; a still-live or ambiguous actor becomes `UNCONFIRMED`. Only after proved prior absence and action-lock availability may startup become a new `ACTIVE_READY` executor and reconcile recorded non-process work. It never signals an orphan target group; a `RUNNING` allocation not already cleaned by its retaining supervisor remains under its anchor or becomes unconfirmed. Workdir removal requires proved process cleanup plus matching path/device/inode/owner/mode/root containment. Partial states reconcile deterministic paths and gated processes without unsafe signals; any unrecorded live artifact blocks startup. Malformed state, unexpected descendants, parent-hash conflict, or failed cleanup likewise fails closed.
- Normal shutdown stops new work, terminalizes sessions, and keeps HTTP handling alive until every original, promoted-waiter, and replay writer completes or reaches its response-write deadline and is force-closed. Because the server is the retaining parent needed for forced cleanup, it does not voluntarily exit until every allocation reaches `DONE`, all cleanup actors are reaped, and no writer slot, queue, response reference, workdir, or journal remains. Failure stays in cleanup-only/drain mode for operator resolution; a foreground deadline changes health/reporting but does not abandon parenthood. Abrupt server failure may truncate a network response and yields only the explicitly limited anchor/restart recovery paths; a same-UID user forcibly killing recorded actors remains inside the trust boundary.
- On shutdown, stop accepting work, give active non-tool streams a short grace period, then cancel and close unfinished sessions with the `shutdown` terminal reason.
- Except for the generated local master-key runtime file explicitly defined in section 5.1, the proxy persists no subscription credentials, run tokens, transcripts, prompts, tool payloads, or responses to disk. The pinned Python SDK/CLI is launched with transcript/history persistence disabled. A versioned path policy classifies known credential paths as metadata/event-only and known noncredential state/workdirs as safe for content canaries. The whole relevant Claude root receives path/size/mtime event snapshots; only explicitly safe paths and newly created artifacts classified as noncredential are scanned for unique benign canaries. Unknown new paths fail the gate pending classification rather than being opened. The core probe uses the actual existing-login path without modifying credential files; synthetic-root evidence is supplemental.

## 13. Configuration and models

Configuration is environment- or small-file-based and contains:

- An exact supported Claude Agent SDK version and exact bundled/installed Claude CLI version.
- Listen host and port.
- Generated or explicitly configured local master API key, unless the explicit `--no-auth` mode is selected.
- Configured public model aliases and their exact, feasibility-validated backend model IDs; moving aliases and fallback mappings are forbidden.
- Default strict/compatibility parameter policy.
- Session, concurrency, journal-region, request-line/header/body, keep-alive/request-count, response, and monotonic read/write/operation/cleanup timeout limits.
- Debug logging toggle.
- Tool capability toggle, which cannot be enabled unless the SDK-version gate is recorded as passing.
- Backend kind and authentication source. V1 accepts only `backend_kind=agent_sdk_subscription` and `auth_source=existing_claude_login`.

Startup fails closed when the actual SDK or CLI version differs from the validated pair or when the OS/filesystem capability probes fail. Upgrades are intentional changes that rerun authentication/model attestation, prompt-purity, session, streaming, persistence, lifecycle, and tool gates before the supported tuple is updated.

`GET /v1/models` returns only configured public aliases whose exact backend IDs passed the current child-attestation and response-identity gates. It does not scrape undocumented endpoints, silently follow a moving alias, or claim capabilities absent from `/_proxy/capabilities`.

`/_proxy/capabilities`, startup diagnostics, and validation manifests expose `backend_kind`, `auth_source`, `semantic_class=prompt_isolated_agent_sdk`, the supported OS/filesystem tuple, and public-alias-to-exact-backend-model mappings. A future `platform_api_key` backend would use `semantic_class=raw_messages_api`, but it is not implemented by v1 and requires a separate approved design. Startup and each child gate fail when inherited overrides, stored non-secret mode indicators, or missing/unknown runtime evidence make authentication provenance or resolved model identity ambiguous.

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
- System-prompt fidelity tests proving that a bounded plain string, including empty, is passed exactly and that Anthropic block arrays/cache metadata plus structured or multiple OpenAI system messages are rejected before allocation or SDK mutation.
- Canonical event conversion and round trips.
- SSE golden traces and chunk ordering.
- Terminal-success framing gates for Anthropic stop/usage `message_delta` plus `message_stop` and OpenAI finish/usage chunks plus `[DONE]`, proving no success-bearing frame is emitted before result/iterator/deadline validation and commit.
- Unsupported-field policy.
- Session/head compare-and-swap behavior.
- Idempotent retry scope, canonical fingerprint inputs, historical-lookup ordering, original-result-head replay, bounded non-streaming shared-future waiter storms (admit/promote/disconnect/error/capacity), streaming `operation_in_progress` then byte-zero replay, and conflict detection across sessions, principals, endpoints, dialects, heads, framing headers, and streaming modes.
- Atomic capacity-bundle fault injection at every reservation component, proving no head/operation/state mutation on `409`/`429`, complete rollback of partial reservations, and bounded control-envelope delivery when model-writer capacity is full.
- Randomized actor-state-machine transitions, including simultaneous identical and divergent requests.
- Expiry versus request, deletion during generation/tool wait, bounded-buffer overflow, and subprocess death after possible SDK mutation.
- Disconnect injection after every SSE event boundary and provisional-head failure handling.
- Transcript canonicalization over Unicode, JSON argument ordering, empty fields, and content variants.
- Redaction and secret-canary tests.
- Loopback `Host`, browser `Origin`, CORS, content-type, and local-authentication tests.
- Slowloris and HTTP-parser tests for accept-to-header, body-read, keep-alive-idle, and total pre-operation deadlines; request-line/URI/header-count/header-byte/declared-body/incremental-body limits; ambiguous framing and transfer encodings; maximum requests per connection; and partial-read/disconnect/deadline races, proving exact-once release of every connection, request, and control-writer slot.
- Table-driven endpoint-by-credential-class authorization, including rejection of launcher-forged, cross-run, expired, and revoked derived tokens.
- Reason-aware terminal lookup before and after session/run tombstone expiry, including expiry during generation and tool wait.
- Deliberately delayed timer callbacks proving under-lock monotonic expiry before replay/reservation and operation-deadline checks before commit; completion event timestamps cannot override a late lock-time check.
- Expiry, deletion, shutdown, and run-revocation races at replay admission and every body/SSE byte boundary, proving a pre-transition snapshot drains exactly within its write deadline, a post-transition request gets only the tombstone, and global snapshot/queue bytes are released on completion, timeout, and disconnect.
- Slow/disconnected original streaming and non-streaming writers plus promoted retry waiters, with commit/terminal/shutdown races at every byte boundary, proving monotonic lease start points, exact-once slot/queue/reference release, bounded pre-commit waiting, and forced close of every writer class.
- Active-to-retired run-token races proving a retired digest grants no principal or general endpoint access, returns only its own stable `410` path, and becomes `401` after tombstone expiry.
- Automatic-run lifecycle tests for every actor `LOST`/`CLOSED` cause, proving the terminal-cell binding survives session-tombstone removal, cannot rebind, returns its stable `409`/`410` inference error until run expiry/revocation, and then follows retired-digest semantics.
- Registry churn tests at every run/session/cleanup limit and maximum TTL, proving atomic pre-creation reservation, no early terminal eviction, automatic future-slot accounting, independent cleanup-slot retention, deterministic `429`, and release only at the specified tombstone/cleanup boundary.
- Table-driven coverage of every post-reservation failure row before and after the native-mutation boundary, asserting current HTTP/SSE code, restored or terminal state, head commit, replay retention/erasure, tombstone cause, and later lookup.
- Cleanup ownership-transfer tests exercise every row in the normative automaton, including repeated `ACTIVE_READY -> BATCH_ACTIVE -> ACTIVE_READY`, `DONE`, both retirement variants, completed/interrupted batch outcomes, authority-epoch replacement preserving all fields, `UNCONFIRMED`, and final deletion. Race batch admission against retirement in both append orders and deschedule after the last check before each syscall. Assert retirement before admission blocks action; retirement after admission preserves exactly that batch until `BATCH_DONE`/lock release or death; no successor activates early; and only a later `ACTIVE_READY` owner reconciles interruption. Inject process death and torn writes at every append/full-sync/directory-sync boundary and between allocations. Race durable-head certification by appending before sync, between snapshot/sync, and between sync/recheck; no unsynchronized head may authorize action. Charge every physical byte, fill both journal regions, and prove exhaustion permits no action. Test the named BSD `flock` model for same-process tasks/separate FDs/dup/close/fork/exec/exit. Prove only a retaining parent can signal; inject PID/PGID reuse at every boundary. With a retaining supervisor, test multi-batch group STOP/TERM/wait/KILL while the anchor stays unreaped. Without it after `RUNNING`, assert the anchor is the sole `ACTIVE_READY` cooperative executor, never issues group STOP/KILL, always transitions to `UNCONFIRMED`, remains alive, and never promotes a helper or authorizes file removal. Cover parent/anchor/helper death, CLI leader exit, corrupt journals, unmatched artifacts, stubborn descendants, and stale-workdir identity.
- Platform-gate tests on the supported macOS/APFS tuple plus negative fixtures for unsupported Darwin versions, nonlocal/non-APFS mounts, failed `openat`/`write`/`F_PREALLOCATE`/`F_FULLFSYNC`/directory-`fsync`/`renameat`/`unlinkat`, failed lock inheritance/release, and unavailable process/group identity primitives. Process-kill fault injection covers every specified commit boundary. Tests and product language explicitly make no kernel-crash, reboot, power-loss, or volume-rollback recovery claim; such a claim requires the separate sacrificial-volume program.

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
- Per-child authentication provenance before first model submission, including traces that allow immutable system/MCP options to be serialized locally during SDK construction but prove no caller configuration or user content reaches a model/network peer before the initialization evidence gate. Cover source immutability or safe pre-turn revalidation and negative changes to non-secret profile/provider/endpoint selection between startup, child connection, and later turns. No negative case may release a model turn.
- Streaming and non-streaming equivalence.
- Session retry, stale head, new run token, expiry, and shutdown.
- The complete cleanup matrix: partial construction, protocol/request failure, bounded-buffer overflow, expiry, deletion, capacity rollback, tool timeout, launcher cancellation, ordinary shutdown, and abrupt proxy death, including supervisor handoff, durable-journal reconciliation, stubborn-child terminate/kill escalation, and confirmed reap.
- All tool capability-gate cases.
- Exact public-alias/backend-model selection, initialization identity where exposed, message-start/assistant identity in streaming and non-streaming modes, and injected missing/mismatched/fallback model events proving zero content or success framing is released before `502 sdk_protocol_error` and `LOST`; supported thinking/effort options are tested only for those exact model tuples.
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
  supervisor.py          # CLI shim, process-group ownership, cleanup journal
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
