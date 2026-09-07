# Claude SDK Proxy

An MIT-licensed, local HTTP compatibility gateway that lets OpenAI- and
Anthropic-compatible agent harnesses use the Claude login already available on
your machine.

The proxy exposes familiar API endpoints on loopback, translates requests into
Claude Agent SDK sessions, and returns standard streaming or non-streaming
responses. The calling harness remains responsible for executing its own tools.

> [!IMPORTANT]
> This is an experimental, single-user local tool. It is not an Anthropic
> product, not a drop-in implementation of every OpenAI or Anthropic feature,
> and not intended to be exposed as a public or multi-user service. Use it in a
> way that complies with the terms governing your Claude account and the Agent
> SDK.

## What works

- OpenAI Chat Completions at `POST /v1/chat/completions`
- Anthropic Messages at `POST /v1/messages`
- Streaming and non-streaming text responses
- Image inputs and image tool results (PNG, JPEG, GIF, and WebP)
- Caller-owned function tools, including parallel calls and repeated tool rounds
- Stock [Pi](https://github.com/earendil-works/pi-mono) tool use without a custom adapter
- Pi automatic compaction and `/compact`
- Generic full-transcript import and rebasing at completed turn boundaries
- In-memory continuation, retry replay, bounded session capacity, and teardown
- Multiple configured Claude model aliases
- Sonnet/Opus thinking controls and model/effort changes between completed turns
- Separate reasoning streams, native signed-thinking replay, and refusal recovery

The current compatibility suite targets Pi `0.85.1`, Claude Agent SDK `0.2.152`,
Python `3.14`, and macOS. Official OpenAI and Anthropic Python clients are also
covered by integration tests.

## How it works

```text
Pi or another agent harness
        │
        │ OpenAI Chat Completions or Anthropic Messages
        ▼
Claude SDK Proxy on 127.0.0.1
        │
        │ Claude Agent SDK session
        ▼
Claude through your existing local login
```

For tool use, the proxy exposes only the tool definitions supplied by the
caller. Claude returns structured tool calls, the harness executes them, and the
harness sends the structured results back. The proxy does not execute caller
operations and does not enable Claude Code built-ins, ambient MCP servers,
skills, plugins, subagents, or auto-memory.

The proxy adds no natural-language tool instructions to your system prompt or
user message. When tools are present, the Claude provider still supplies its
native tool-use instructions and schemas.

## Requirements

- macOS with a working local Claude login
- Python `>=3.14,<3.15`
- [`uv`](https://docs.astral.sh/uv/)
- Pi `0.85.1` if you are using the Pi integration

Confirm that Claude authentication works in a normal terminal before starting:

```bash
claude -p "Reply with OK"
```

Run the proxy from that same login context. A sandboxed process may be unable to
read the macOS Keychain even when `claude` works in your terminal.

## Quick start

Clone the repository, install the locked dependencies, and start the default
`sonnet` model:

```bash
git clone https://github.com/bwangbos/claude-sdk-proxy.git
cd claude-sdk-proxy
uv sync --dev
uv run claude-proxy --model sonnet
```

The server listens on `http://127.0.0.1:8317` by default. In another terminal:

```bash
curl http://127.0.0.1:8317/health
curl http://127.0.0.1:8317/v1/models
```

Expected health response:

```json
{"status":"ok"}
```

## Configure Pi

Add either or both providers below under `providers` in
`~/.pi/agent/models.json`. Merge them with providers already in that file rather
than replacing the file. The OpenAI transport is the simplest general-purpose
choice; use the Anthropic transport when tool results can contain images.

```json
{
  "providers": {
    "claude-subscription-local": {
      "name": "Claude subscription local",
      "baseUrl": "http://127.0.0.1:8317/v1",
      "api": "openai-completions",
      "apiKey": "local-placeholder",
      "compat": {
        "supportsDeveloperRole": false,
        "supportsReasoningEffort": true,
        "supportsStore": true,
        "supportsUsageInStreaming": true,
        "supportsStrictMode": false,
        "maxTokensField": "max_tokens"
      },
      "models": [
        {
          "id": "sonnet",
          "name": "Claude Sonnet subscription",
          "reasoning": true,
          "thinkingLevelMap": {
            "off": "none",
            "minimal": null,
            "low": "low",
            "medium": "medium",
            "high": "high",
            "xhigh": "xhigh",
            "max": "max"
          },
          "input": ["text", "image"],
          "contextWindow": 200000,
          "maxTokens": 16384,
          "cost": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0
          }
        },
        {
          "id": "opus",
          "name": "Claude Opus subscription",
          "reasoning": true,
          "thinkingLevelMap": {
            "off": "none",
            "minimal": null,
            "low": "low",
            "medium": "medium",
            "high": "high",
            "xhigh": "xhigh",
            "max": "max"
          },
          "input": ["text", "image"],
          "contextWindow": 200000,
          "maxTokens": 16384,
          "cost": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0
          }
        }
      ]
    },
    "claude-subscription-vision": {
      "name": "Claude subscription local vision",
      "baseUrl": "http://127.0.0.1:8317",
      "api": "anthropic-messages",
      "apiKey": "local-placeholder",
      "headers": {"anthropic-beta": ""},
      "compat": {
        "forceAdaptiveThinking": true,
        "supportsEagerToolInputStreaming": false,
        "supportsStrictTools": false,
        "supportsCacheControlOnTools": false
      },
      "models": [
        {
          "id": "sonnet",
          "name": "Claude Sonnet subscription",
          "reasoning": true,
          "thinkingLevelMap": {
            "off": "none",
            "minimal": null,
            "low": "low",
            "medium": "medium",
            "high": "high",
            "xhigh": "xhigh",
            "max": "max"
          },
          "input": ["text", "image"],
          "contextWindow": 200000,
          "maxTokens": 16384,
          "cost": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0
          }
        },
        {
          "id": "opus",
          "name": "Claude Opus subscription",
          "reasoning": true,
          "thinkingLevelMap": {
            "off": "none",
            "minimal": null,
            "low": "low",
            "medium": "medium",
            "high": "high",
            "xhigh": "xhigh",
            "max": "max"
          },
          "input": ["text", "image"],
          "contextWindow": 200000,
          "maxTokens": 16384,
          "cost": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0
          }
        }
      ]
    }
  }
}
```

The placeholder key is intentionally non-secret. The proxy ignores it and uses
the local Claude login of the process running the server.

Start the proxy with both advertised aliases, then start Pi with its ordinary
tools:

```bash
uv run claude-proxy --model sonnet --model opus
pi --provider claude-subscription-local --model sonnet
```

If the quick-start server is already running, stop it with `Ctrl+C` before
launching the two-model command; do not start a second server on the same port.
Confirm `/v1/models` lists both aliases. These custom Pi entries are explicit:
adding a server alias alone does not add it to Pi's model picker.

No Pi adapter or extension is required. Use `/model` to choose Sonnet or Opus
within the same provider, and `/thinking` or `Shift+Tab` to change thinking level.
Keep `supportsStrictMode: false` in the provider configuration; Pi otherwise
adds a tool-definition field outside the
proxy's supported subset.

Pi's reasoning selector exposes `off`, `low`, `medium`, `high`, `xhigh`, and
`max`. `off` maps to the OpenAI value `none`; Pi's unsupported `minimal` slot is
hidden by mapping it to `null`. If controls are omitted from an API request,
thinking remains disabled. An accepted effort does not guarantee a visible
thinking summary: the backend may return no summary for a simple prompt, and
OpenAI's default display mode may omit one.

Model and effort can change only after a completed assistant answer. Keep the
API dialect, system prompt, and tool definitions fixed, and never switch while
tool results are still pending. Pi needs no fixed `X-Claude-Proxy-Session`
header; the proxy recognizes its exact replay shape while retaining native
signed history. The deterministic integration suite verifies Sonnet/high to
Opus/low, another unchanged Opus turn, and a switch back to Sonnet/high through
both transports. See the
[verification record](docs/research/2026-09-06-model-thinking-verification.md)
for successful live bidirectional post-tool switches through both transports
and the independently reproduced upstream refusal limitation. Switching does
not guarantee that Claude will answer every subsequent request.

The example's `contextWindow` and `maxTokens` are client-side configuration,
not discovered account limits or enforced backend caps. Zero costs disable
Pi's per-token cost estimate; they do not mean the subscription is free or
unlimited. Alias targets and available effort levels can change upstream.

### Pi with images and screenshots

Restart the proxy and Pi after updating the configuration, then select the
Anthropic transport:

```bash
pi --provider claude-subscription-vision --model sonnet
```

Ask Pi to read an image file, or attach one. The beta header and compatibility
flags above disable optional features outside the proxy's supported subset.
Do not switch API dialects within one conversation.

Pi `0.85.1`'s OpenAI transport moves tool-returned images into an extra user
message, separating them from their tool call IDs. That layout is not supported
by this proxy; use the Anthropic provider above for Pi's image workflows.
Other OpenAI clients can still send image inputs directly.

## Use another client

Any harness that can target an OpenAI Chat Completions or Anthropic Messages
base URL can use the gateway if it stays within the supported subset.

### OpenAI Python client

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8317/v1",
    api_key="local-placeholder",
)

response = client.chat.completions.create(
    model="sonnet",
    messages=[{"role": "user", "content": "Reply with OK"}],
    reasoning_effort="high",
)
print(response.choices[0].message.content)
```

### Anthropic Python client

```python
from anthropic import Anthropic

client = Anthropic(
    base_url="http://127.0.0.1:8317",
    api_key="local-placeholder",
)

response = client.messages.create(
    model="sonnet",
    max_tokens=128,
    thinking={"type": "adaptive", "display": "summarized"},
    output_config={"effort": "high"},
    messages=[{"role": "user", "content": "Reply with OK"}],
)
print("".join(block.text for block in response.content if block.type == "text"))
```

The API key values satisfy client-library validation only. They are not used to
authenticate with Claude.

An empty native refusal is a terminal response, not answer text: Anthropic JSON
returns `content: []` with `stop_reason: "refusal"`, and Anthropic SSE emits no
content blocks; OpenAI returns empty answer content with
`finish_reason: "content_filter"`. Raw usage is retained.
Append that assistant response unchanged and then a new user message to continue;
an exact retry replays the terminal response without another generation.
Anthropic empty assistant arrays normalize to the same canonical singleton empty
text block as `content: ""` (or one empty text block). This narrow structural replay
allowance also supports OpenAI's empty assistant content: public transcripts cannot
authenticate why an assistant was empty. Empty user messages, multiple empty text
blocks, malformed thinking blocks, and incomplete tool-result boundaries remain
invalid. Native signed history is retained when continuing or switching models.

### Image inputs

Images are sent as native image blocks, not descriptions injected into prompt
text. For example, using the OpenAI client initialized above:

```python
import base64
from pathlib import Path

image = base64.b64encode(Path("screenshot.png").read_bytes()).decode("ascii")
response = client.chat.completions.create(
    model="sonnet",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "What is visible in this screenshot?"},
            {"type": "image_url", "image_url": {
                "url": f"data:image/png;base64,{image}"
            }},
        ],
    }],
)
print(response.choices[0].message.content)
```

Anthropic requests use its native
[`image` content block](https://platform.claude.com/docs/en/build-with-claude/vision):

```json
{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "BASE64_IMAGE_BYTES"}}
```

Text and image blocks retain their order. Successful Anthropic `tool_result`
content can contain the same text/image blocks. The OpenAI endpoint additionally
accepts text/`image_url` arrays in `role: "tool"` content as a **proxy extension**;
not every OpenAI client supports that shape. Tool images remain correlated by
call ID, including in replay and imported/rebased histories.

Limits apply to the **entire submitted transcript**, including tool results:
20 images, 3 MiB decoded per image, and 12 MiB decoded in total. Use canonical
base64 for PNG, JPEG, GIF, or WebP. The proxy checks encoding, size, and MIME
signatures; Claude still validates image decoding and dimensions. Remote URLs,
file URLs, non-`auto` OpenAI image detail, image error results, PDFs, audio, and
image generation are not supported. Return tool failures as text.

## API surface

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Minimal liveness response |
| `GET` | `/v1/models` | Configured model aliases |
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat and function tools |
| `POST` | `/v1/messages` | Anthropic-compatible messages and tools |

Both POST endpoints require `Content-Type: application/json`. Normal parameters
such as `charset=utf-8` are accepted.

### Model and thinking controls

`model` must exactly match one of the server's repeated `--model` values.
`GET /v1/models` lists that allowlist, not every model available to your Claude
account. There is no automatic model fallback.

The proxy disables the SDK's classifier-triggered model downgrade for every
session using `CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK=1`. This is distinct from the
SDK's `fallback_model` option for overload/unavailability. A supported native
refusal ends as `refusal` (Anthropic) or `content_filter` (OpenAI), not a retry on
another model. The recognized named categories include `cyber`, `bio`,
`frontier_llm`, `general_harms`, and `reasoning_extraction`.

If the runtime nevertheless emits a model-switch notice, the proxy stops that
generation without accepting the replacement answer. JSON responses report
HTTP 502 with `reason: "model_fallback_disabled"`; an already-started stream
reports an explicit fallback-disabled error. This is a policy enforcement
failure, not a completed refusal, and must not be scored as an answer from the
requested model. There is no mixed-model opt-in in this release. Response
`model` still names the requested alias; it is not an exact-version attestation.
See the [verification notes](docs/research/2026-09-07-refusal-model-fidelity.md)
for evidence and live-verification limitations.

| Setting | OpenAI Chat Completions | Anthropic Messages |
| --- | --- | --- |
| Thinking off | Omit `reasoning_effort`, use `null`, or use `"none"` | Omit `thinking`, use `null`, or use `{"type":"disabled"}`; omit effort |
| Adaptive effort | `reasoning_effort: "low"`, `"medium"`, `"high"`, `"xhigh"`, or `"max"` | `thinking: {"type":"adaptive"}` with `output_config: {"effort":"high"}` (same five effort values for current aliases) |
| Thinking display | No separate display request control | Add `display: "summarized"` or `"omitted"` inside active `thinking` |

Both current `sonnet` and `opus` aliases support the listed levels in the
[recorded live checks](docs/research/2026-09-06-model-thinking-verification.md).
Pinned older model IDs have different capability rules; the proxy rejects
unsupported model/control combinations with HTTP 400, without a silent
downgrade. The legacy Anthropic `thinking: {"type":"enabled","budget_tokens":N}`
form is accepted only for configured supported 4.5 IDs, with a positive integer
budget below `max_tokens`; it is not the mode for the current aliases. These
older-ID rules are parser-tested, not part of the Sonnet/Opus live matrix.

Anthropic returns native `thinking`/`redacted_thinking` blocks, including signature
deltas when streaming; preserve the assembled blocks unchanged on replay. OpenAI returns separate
`reasoning_content` in assistant messages and streaming deltas, a compatibility
extension rather than authenticated native thinking. The proxy retains native
signed history for recognized continuations; unsigned OpenAI reasoning strings
cannot reconstruct it after an unrelated import or restart. Reasoning summaries
are not appended to answer text, and an accepted effort need not produce a
visible summary.

### Compatibility boundary

Supported:

- Text messages, including OpenAI text-only content-block arrays
- Embedded images in user messages and successful tool results
- Anthropic system text-block arrays and advisory content-block cache hints
- Caller-provided function tools with self-contained JSON Schemas
- Mixed text and tool calls, parallel calls, and multiple tool rounds
- Streaming usage frames and non-streaming usage objects, including cache counters
- Standard OpenAI SDK assistant-message dumps with null optional metadata
- Completed-request replay and linear continuation
- Complete imported or rewritten transcripts at completed boundaries
- Disabled or adaptive thinking with `low`, `medium`, `high`, `xhigh`, or `max`
  effort on the configured Sonnet and Opus aliases
- Model or effort changes at completed assistant-answer boundaries

Intentionally unsupported:

- PDFs, arbitrary files, audio, video, remote image URLs, and image generation
- OpenAI Responses API
- Exact temperature, sampling, or stop-sequence controls
- Forced, named, required, or disabled-per-turn tool choice
- `parallel_tool_calls: false`
- Branching one explicit session ID into concurrent histories
- Public hosting, multiple users, or API-key authentication
- Durable server-side storage (clients must retain and resend complete transcripts)

`max_tokens` and `max_completion_tokens` are accepted as advisory values. The
Agent SDK path does not provide exact output-token enforcement.
Anthropic ephemeral `cache_control` hints on content blocks are accepted but
not forwarded; caching remains controlled by the SDK/backend.
Known optional OpenAI fields set to `null` are treated as absent; unknown fields
and non-null unsupported controls are still rejected. The OpenAI `user` field
is accepted as advisory metadata, not as a conversation identifier.

Usage is reported per public response, not as cumulative SDK-session totals.
Anthropic keeps `input_tokens`, `cache_read_input_tokens`, and
`cache_creation_input_tokens` separate. OpenAI `prompt_tokens` includes all three;
`prompt_tokens_details.cached_tokens` and `cache_write_tokens` break out cache
reads and writes. Cache detail fields are omitted if the backend did not supply
them. `total_tokens` is the full prompt count plus output. An Anthropic
`input_tokens` value of 2 can therefore be valid for a heavily cached turn.

## Tools

Tool definitions must be sent on every request in a tool-enabled conversation
and must remain unchanged along with the system prompt and API dialect. Model
and effort must remain unchanged until every pending tool result has completed;
they may change after the assistant's completed answer. The gateway publishes
each tool call with an opaque public ID and waits for the harness to return
exactly one result for every call.

Parallel results may be returned in any order. Correlation is always by the
public call ID—never by tool name, arguments, or position. A replacement
transcript must include a complete result batch for any pending tool calls;
partial batches and changed pending call definitions are rejected.

The detailed [gateway reference](docs/feasibility/README.md) contains complete
Anthropic and OpenAI tool request examples, schema limits, timeout behavior, and
error semantics.

## Sessions and compaction

The gateway keeps linear SDK sessions in memory. Most clients can rely on
transcript matching without custom headers. If a client supports a unique
per-conversation header, it may send:

```http
X-Claude-Proxy-Session: conversation-specific-id
```

The proxy returns this header on admitted responses, and clients may send the
returned value on subsequent requests, including conversations that started
without it. Do not configure one static value globally; that would collapse
unrelated conversations into one lineage. Stateless clients sharing identical
prefixes are inherently ambiguous: use distinct per-conversation headers for
parallel chats. OpenAI `user` does not disambiguate sessions.

When a harness compacts or otherwise rewrites a completed transcript, the proxy
imports the complete replacement snapshot into a fresh ephemeral SDK session.
This is how Pi automatic compaction and `/compact` work without a Pi-specific
adapter. The snapshot must be structurally complete and end with either a
text/image user turn or a complete tool-result batch. For a result-ending import,
the proxy seeds native history with both calls and results, then sends an empty
SDK continuation signal; it adds no natural-language instruction and does not
execute the historical calls again. A suspended conversation can be replaced
only when its exact pending calls and complete result IDs match the snapshot.
In-flight requests, changed system/tools/dialect, and stale identified heads still
fail closed; recovery is not an unconditional retry after any error.
Model/thinking changes use the same replacement mechanism after a completed
assistant turn, not while awaiting tool results.

Restarting the proxy clears all active sessions, suspended tool calls, imported
history, and replay entries. A client can recover by submitting its complete
transcript, including already-executed tool results; the proxy cannot reconstruct
history or missing results on its own.

## Server options

```text
uv run claude-proxy \
  [--host 127.0.0.1] \
  [--port 8317] \
  [--model MODEL]... \
  [--max-sessions 8] \
  [--tool-result-timeout 300] \
  [--log PATH] \
  [--log-json]
```

- `--host` accepts loopback IP addresses only.
- Repeat `--model` to expose multiple Agent SDK model aliases.
- Idle sessions are evicted least-recently-used when capacity is reached.
- In-flight and tool-waiting sessions are never evicted.
- If every retained session is busy, the API returns HTTP `503` with
  `session_capacity`.
- Generation and tool-result waits default to 300 seconds; backend teardown is
  bounded separately.

Example with two model aliases:

```bash
uv run claude-proxy --model sonnet --model opus --max-sessions 16
```

## Troubleshooting

### Connection refused

The provider entry does not start the gateway. Run the following in a separate
terminal and confirm `/health` responds:

```bash
uv run claude-proxy --model sonnet --model opus
```

### Pi returns `param: "tools"`

Confirm the Pi provider has `"supportsStrictMode": false`. Restart Pi after
changing `models.json`.

### Pi returns `param: "messages"`

Update to the current proxy version and restart the running proxy. Pi `0.85.1`
uses text-content arrays, which older proxy builds rejected.

### Claude works in a terminal but the proxy cannot authenticate

Start the proxy from the same normal macOS login context. Sandboxes and detached
services may not have access to the Keychain credential used by Claude.

### `session_mismatch` or `request_in_flight` (HTTP 409)

Keep the system prompt, tool definitions, and dialect stable for a conversation.
Model/thinking changes are allowed only between completed assistant turns;
keep them unchanged during tool-result continuations. Do not reuse one explicit
session header for unrelated chats, and do not continue a stale transcript head
after a newer turn has committed.
The error includes a stable `reason` (for example `system_changed`, `stale_head`,
`tool_ids_mismatch`, or `ambiguous_session`) and guidance about the session header.
Wait for active work to finish before retrying `request_in_flight`; use a unique
session header when the requests actually belong to different conversations.

Every HTTP response includes an `X-Request-ID`. For opt-in diagnostics:

```bash
uv run claude-proxy --model sonnet --log proxy.jsonl --log-json
```

`--log PATH` appends readable metadata by default; add `--log-json` for JSON lines.
`--log-json` alone writes to stderr. Logs contain request IDs, hashed session
references, selection/rebase/replay decisions, safe rejection reasons, and HTTP
status. Backend failures also record the stage, exception class, allowlisted
reason, and proxy code locations before exception redaction. A blocked fallback
records the original and proposed replacement model IDs. Logs do not include
prompts, tool arguments/results, credentials, raw session keys, upstream refusal
explanations, exception text, or traceback locals.
New log files are created with owner-only permissions. Logs are not rotated;
choose a suitable path and retention policy. Include the request ID and relevant
diagnostic lines when reporting a failure.

### Changes do not take effect

The server does not hot-reload. Stop it with `Ctrl+C` and start it again after
updating the checkout.

### Opus is missing or thinking controls are rejected

Check `/v1/models` for both aliases, restart the server with
`--model sonnet --model opus`, and use the current Pi configuration above.
Pi needs `reasoning: true`, the model-level `thinkingLevelMap`, and the matching
provider compatibility flags. Restart Pi after changing provider configuration.
Changing `models.json` alone does not update a running proxy's allowlist or code.

## Development

The authoritative deterministic release gate is:

```bash
make release-offline
```

It builds the native lifecycle helpers and runs unit, Darwin lifecycle, gateway,
official-client, and real-Pi integration tests with warnings, skips, and marker
mistakes treated as failures. It also runs Ruff and strict mypy checks. The
faster development gate is:

```bash
make check
```

Live subscription tests are separate and explicit because they invoke Claude:

```bash
CLAUDE_PROXY_LIVE=1 CLAUDE_PROXY_LIVE_MODEL=sonnet \
  .venv/bin/pytest -q --strict-markers --forbid-skips -W error \
  tests/live/test_gateway_text.py tests/live/test_gateway_tools.py
```

See the [documentation index](docs/README.md) for the current gateway reference,
verification records, and historical designs. Old feasibility verdicts and
implementation plans are audit records, not current feature restrictions.

## Security and privacy

- The server is constrained to loopback and assumes a trusted local caller.
- It does not validate incoming API keys.
- Prompts, tool schemas, tool arguments, and returned tool results are sent to
  Claude through the Agent SDK; this is not an offline inference server.
- Built-in Claude Code tools and ambient configuration are disabled.
- Do not put the gateway behind a public reverse proxy or expose its port to
  other machines.

## License

This project is open source under the [MIT License](LICENSE).
Dependencies, including the Claude Agent SDK, retain their own licenses.
Using Claude services remains subject to the terms governing your account;
this project's license does not replace those terms.
