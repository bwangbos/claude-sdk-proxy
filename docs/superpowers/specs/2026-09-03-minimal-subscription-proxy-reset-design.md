# Minimal Claude Subscription Proxy — Reset Design

Date: 2026-09-03

## Purpose

Build a small local HTTP compatibility proxy that lets configurable Anthropic-
and OpenAI-compatible clients send requests through an already authenticated
Claude installation. The proxy uses only Anthropic's official Agent SDK or the
installed `claude -p` command. It never extracts OAuth credentials, recreates
private Claude Code requests, spoofs client metadata, or forwards subscription
tokens as API keys.

This design replaces the crash-consistent lifecycle architecture on the MVP
path. That work remains preserved on `feat/phase1-anthropic-core`; it is not a
dependency of this proxy.

## Product boundary

The proxy is an experimental, personal, trusted-local tool. It binds to
`127.0.0.1` by default and is not designed for public or multi-user service.
Documentation must not claim that a subscription-backed compatibility proxy is
equivalent to, or a supported replacement for, Anthropic's commercial API.

The MVP exposes:

- `POST /v1/messages`
- `POST /v1/chat/completions`
- `GET /v1/models`
- `GET /health`

`/v1/messages` is the canonical request and event model. The OpenAI endpoint is
a translation layer over it.

## Simplicity constraints

The MVP is Python-only and uses one server process. Each request owns its
backend operation and temporary resources. If the server process dies, in-flight
requests fail; restart recovery is intentionally absent.

The MVP must not contain:

- native C code;
- persistent journals, ledgers, databases, or conversation storage;
- durable restart recovery or cleanup successors;
- custom flock domains, inode provenance protocols, or signal-mask state
  machines;
- prompt rewriting, hidden system prompts, context compression, model routing,
  account rotation, analytics, or third-party telemetry;
- vLLM/SGLang forwarding;
- proxy-executed filesystem, shell, web, skill, subagent, or MCP tools.

Target at most 1,500 non-test Python lines for the initial implementation and
at most 300 lines per module. Exceeding either budget requires explicit design
review rather than silent expansion.

## Delivery sequence

### 1. Capability spike

Before building all endpoints, implement the smallest production-shaped
vertical slice necessary to answer five questions for both backends:

1. Can an authenticated request complete without reading credentials directly?
2. Can output stream incrementally with acceptable time to first event?
3. Can ambient Claude Code behavior be suppressed structurally?
4. Can caller-provided tool schemas produce structured tool calls while Claude
   built-in tools remain disabled?
5. Can system, user, and assistant turns be preserved without flattening them
   into an instruction-bearing string?

The spike uses one canonical request, captures the exact SDK options or CLI
arguments/stdin constructed by the proxy, and records subprocess count and
latency. It does not build generalized recovery, compatibility, or session
machinery.

If a backend cannot preserve a requested field, it reports the field as
unsupported. If it cannot provide structured caller-tool calls without enabling
Claude built-ins or prompt-based emulation, tool calling is marked unsupported
for that backend. The Agent SDK's in-process MCP/tool execution mechanism must
not be repurposed to execute harness-owned tools inside the proxy. No
JSON-in-prompt approximation is permitted.

The rest of the MVP proceeds when at least one backend passes authentication,
streaming, prompt-purity construction, and multi-turn role preservation.
Agent-harness compatibility is claimed only if at least one backend also passes
structured caller-tool calling.

### 2. Thin HTTP MVP

After the capability result is documented, build the four endpoints, SSE
streaming, OpenAI translation, optional local authentication, cancellation,
timeouts, bounded concurrency, debug inspection, and examples.

### 3. Repository cleanup

After the vertical slice and HTTP tests pass, remove the old Phase 0 native and
attestation machinery from the shipped reset branch. Git history and the frozen
hardening branch preserve it. The package and default test commands must contain
only the proxy and its directly relevant tests.

## Architecture

```text
Anthropic/OpenAI client
        |
        | HTTP + optional local key
        v
aiohttp application
        |
        | CanonicalRequest / CanonicalEvent
        v
Backend protocol
   |                 |
   v                 v
claude -p        Agent SDK
adapter          adapter
```

Use `aiohttp` for the async HTTP server and streaming response transport. Keep
request validation explicit and small; do not add an application framework,
ORM, task queue, or dependency-injection container.

The expected module boundaries are:

- `config.py`: environment/CLI configuration and model allowlist;
- `domain.py`: canonical request, content, tool, usage, event, and error types;
- `backends/base.py`: the small async streaming backend protocol;
- `backends/claude_p.py`: `claude -p` process construction and event parsing;
- `backends/agent_sdk.py`: official Agent SDK option construction and events;
- `anthropic_api.py`: Anthropic parsing and response/SSE rendering;
- `openai_api.py`: OpenAI subset translation and rendering;
- `app.py`: routing, authentication, admission, cancellation, and errors;
- `cli.py`: server entry point;
- `debug.py`: opt-in structured inspection and redaction.

These are boundaries, not a requirement to create empty abstraction files. If
two adjacent responsibilities remain clearer together under the line budget,
keep them together.

## Backend contract

Both adapters implement one interface conceptually equivalent to:

```python
class Backend(Protocol):
    async def stream(self, request: CanonicalRequest) -> AsyncIterator[CanonicalEvent]: ...
```

The backend is selected once at startup with `PROXY_BACKEND=claude-p|agent-sdk`.
There is no per-request routing or failover. Both adapters consume the same
canonical request and disclose their supported capability set.

The server may start a backend child process per request. Optimization to a
persistent process is allowed only after measurement demonstrates a material
benefit and a separate design proves there is no context leakage between
requests.

### Prompt purity

For the Agent SDK, construct options equivalent to:

```python
ClaudeAgentOptions(
    model=request.model,
    system_prompt=request.system or "",
    tools=[],
    allowed_tools=[],
    skills=[],
    setting_sources=[],
    mcp_servers={},
    strict_mcp_config=True,
    cwd=request_temp_directory,
)
```

Use only arguments supported by the installed SDK version. An unavailable
field is handled explicitly, never approximated by injecting instructions.

For `claude -p`, use only documented CLI flags. The capability spike must prove
which ambient settings and built-in behavior can actually be disabled. If the
CLI cannot meet the prompt-purity boundary, retain it as an explicitly less-pure
optional backend or exclude it from the MVP default; do not compensate by
spoofing private protocol details.

Each request runs in a fresh empty temporary working directory. Cleanup is
best-effort with ordinary `TemporaryDirectory` semantics. No durable cleanup
state is created.

### Credentials and environment

Authentication remains owned by the Agent SDK or installed Claude CLI. The
proxy never locates, parses, logs, copies, or exports Claude credentials.

Pass only the environment needed for normal authenticated SDK/CLI operation and
document the names passed. Values are never logged. The capability spike, not
an attestation framework, determines whether a stricter allowlist is practical.

## Canonical API subset

The canonical request supports:

- `model`;
- `system`;
- `messages` with text content initially;
- `max_tokens`;
- `temperature`, `top_p`, and `top_k` when faithfully supported;
- `stop_sequences`;
- `stream`;
- caller-provided `tools` and `tool_choice` only where the selected backend
  passed the structured tool capability;
- supported thinking configuration;
- opaque request metadata only when the backend accepts it without behavioral
  emulation.

Unsupported non-default parameters return HTTP 400 with a stable error type and
the field name. They are never silently ignored.

The OpenAI endpoint supports only:

- `model`;
- system, user, and assistant messages;
- `temperature` and `top_p`;
- `max_tokens` or `max_completion_tokens`;
- `stop`;
- `stream`;
- tools only when supported by the selected backend.

OpenAI-only features such as logprobs, response formats, parallel tool choices,
audio, images, assistants, batches, files, embeddings, and fine-tuning are out
of scope.

## Streaming

Adapters yield normalized events as soon as the backend produces them. The HTTP
layer renders those events directly as Anthropic or OpenAI SSE without buffering
the complete answer.

The event model needs only:

- response start/model;
- text delta;
- structured tool-call start/delta/stop when supported;
- usage;
- response stop;
- typed backend error.

On client disconnect, cancel the backend operation and terminate its child
process with a short bounded grace period, then force-kill if necessary. This is
request cleanup, not durable recovery.

## Configuration

Defaults:

```text
PROXY_HOST=127.0.0.1
PROXY_PORT=8317
PROXY_BACKEND=<selected backend>
PROXY_MODELS=<explicit comma-separated allowlist>
PROXY_REQUEST_TIMEOUT_SECONDS=300
PROXY_MAX_CONCURRENCY=4
PROXY_DEBUG=0
LOCAL_PROXY_API_KEY=<unset>
```

The capability spike selects the documented default. Prefer `agent-sdk` when it
passes because it is the intended official programmatic integration; otherwise
default only to a backend that passes the required boundary. The operator can
still explicitly select either backend whose limitations are documented.

An unset local key permits requests only because the default bind address is
loopback. Binding to a non-loopback address requires an explicit local key and a
separate `--allow-non-loopback` acknowledgement.

`GET /v1/models` returns only the configured allowlist. It never scrapes an
undocumented model endpoint.

## Error behavior

- `400`: malformed or unsupported request/parameter;
- `401`: missing or invalid optional local key;
- `404`: model not in the configured allowlist;
- `429`: concurrency capacity unavailable;
- `502`: backend start, protocol, or unexpected termination failure;
- `504`: request timeout.

The MVP performs no automatic model/backend retry. Retrying a generation can
duplicate cost or tool actions and belongs to the calling harness.

Errors use the selected API's conventional envelope and never include backend
stderr, environment values, credentials, or authorization headers.

## Debug inspection

With `PROXY_DEBUG=1`, log structured representations of:

- incoming request after authorization headers are removed;
- canonical request;
- backend selection and capability set;
- exact SDK option names or CLI argument names;
- exact prompt/message structure sent to the backend;
- normalized response event types;
- queue, startup, first-event, generation, and total latency.

Authorization values, API keys, environment values, credentials, and raw SDK/
CLI stderr remain redacted. Debug logging may include prompt content because the
local operator explicitly opted in; documentation must state this clearly.

## Testing and acceptance

### Deterministic tests

- parse and validate the supported Anthropic subset;
- translate the supported OpenAI subset into the same canonical request;
- render non-streaming and SSE responses for both protocols;
- prove unsupported fields fail explicitly;
- capture fake Agent SDK calls and assert only caller system/messages plus the
  minimum explicit options are constructed;
- capture a fake `claude` executable's argv, stdin, cwd, and environment names;
- prove built-in tools/settings/skills/MCP are disabled in constructed inputs;
- prove debug logs redact secrets;
- prove disconnect, timeout, and capacity cancellation terminate the fake child;
- prove no request data persists after completion;
- run a representative configurable-base-URL harness against the local fake
  backend.

Tests assert hand-derived request/event fixtures rather than using production
translation helpers to calculate expected values.

### Opt-in live tests

Live tests never run in the default suite. With explicit operator opt-in and an
already authenticated installation, test separately for each backend:

- one text response;
- streaming with more than one incremental event;
- caller system prompt preservation;
- no ambient project instruction by using an empty temporary cwd;
- structured caller-tool call and tool-result continuation, if claimed;
- cancellation;
- latency and subprocess count.

Live prompt-purity results are evidence, not proof of Anthropic's private
server-side prompt composition. The deterministic construction tests are the
inspectable contract the proxy controls.

## Definition of done

The MVP is done when:

1. A curl request works through at least one already authenticated backend.
2. Anthropic and OpenAI text streaming pass deterministic end-to-end tests.
3. The constructed backend request contains only caller content and explicitly
   configured minimum metadata within the adapter's observable boundary.
4. Unsupported parameters fail explicitly.
5. A configurable-base-URL agent harness can make a text request without code
   modification.
6. Tool-capable harness support is claimed only after a structured tool-call
   round trip passes; otherwise the limitation is prominent.
7. Default tests, lint, and type checking pass without credentials or network.
8. The shipped implementation remains within the simplicity budget and imports
   none of the frozen native lifecycle subsystem.

## Deferred work

- persistent sessions or backend-process pooling;
- multimodal input;
- broader OpenAI compatibility;
- retry/failover;
- public-network deployment;
- multi-user or multi-account support;
- durable crash recovery;
- any harness-specific provider adapter that cannot use a configurable
  Anthropic/OpenAI base URL.
