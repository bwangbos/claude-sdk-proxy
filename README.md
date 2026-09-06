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

Add the following provider under `providers` in `~/.pi/agent/models.json`. Merge
it with any providers already in that file rather than replacing them.

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
        "supportsReasoningEffort": false,
        "supportsStore": true,
        "supportsUsageInStreaming": true,
        "supportsStrictMode": false,
        "maxTokensField": "max_tokens"
      },
      "models": [
        {
          "id": "sonnet",
          "name": "Claude subscription (local)",
          "reasoning": false,
          "input": ["text"],
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

Start Pi with its ordinary tools:

```bash
pi --provider claude-subscription-local --model sonnet
```

No Pi adapter or extension is required. Keep `supportsStrictMode: false` in the
provider configuration; Pi otherwise adds a tool-definition field outside the
proxy's supported subset.

### Pi with images and screenshots

For image attachments and tools that return screenshots, add this separate
provider under `providers`. It uses Pi's stock Anthropic transport, which keeps
images attached to the tool result that produced them. No extension is required.
Keep your existing text provider if you want to continue using it.

```json
"claude-subscription-vision": {
  "baseUrl": "http://127.0.0.1:8317",
  "api": "anthropic-messages",
  "apiKey": "local-placeholder",
  "headers": {"anthropic-beta": ""},
  "compat": {
    "supportsEagerToolInputStreaming": false,
    "supportsStrictTools": false,
    "supportsCacheControlOnTools": false
  },
  "models": [
    {
      "id": "sonnet",
      "name": "Claude subscription (local vision)",
      "reasoning": false,
      "input": ["text", "image"],
      "contextWindow": 200000,
      "maxTokens": 16384,
      "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
    }
  ]
}
```

Restart the proxy and Pi after updating, then run:

```bash
pi --provider claude-subscription-vision --model sonnet
```

Ask Pi to read an image file, or attach one. The beta header and compatibility
flags above disable optional features outside the proxy's supported subset.
Do not switch API dialects in the middle of an active tool round.

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
    messages=[{"role": "user", "content": "Reply with OK"}],
)
print(response.content[0].text)
```

The API key values satisfy client-library validation only. They are not used to
authenticate with Claude.

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

### Compatibility boundary

Supported:

- Text messages, including OpenAI text-only content-block arrays
- Embedded images in user messages and successful tool results
- Anthropic system text-block arrays and advisory content-block cache hints
- Caller-provided function tools with self-contained JSON Schemas
- Mixed text and tool calls, parallel calls, and multiple tool rounds
- Streaming usage frames and normal non-streaming usage objects
- Completed-request replay and linear continuation
- Complete imported or rewritten transcripts at completed boundaries

Intentionally unsupported:

- PDFs, arbitrary files, audio, video, remote image URLs, and image generation
- OpenAI Responses API
- Exact temperature, sampling, stop-sequence, or reasoning controls
- Forced, named, required, or disabled-per-turn tool choice
- `parallel_tool_calls: false`
- Branching one explicit session ID into concurrent histories
- Public hosting, multiple users, or API-key authentication
- Durable conversation recovery after the proxy process restarts

`max_tokens` and `max_completion_tokens` are accepted as advisory values. The
Agent SDK path does not provide exact output-token enforcement.
Anthropic ephemeral `cache_control` hints on content blocks are accepted but
not forwarded; caching remains controlled by the SDK/backend.

## Tools

Tool definitions must be sent on every request in a tool-enabled conversation
and must remain unchanged along with the model, system prompt, and API dialect.
The gateway publishes each tool call with an opaque public ID and waits for the
harness to return exactly one result for every call.

Parallel results may be returned in any order. Correlation is always by the
public call ID—never by tool name, arguments, or position. A pending tool round
must be completed before compaction or transcript rebasing.

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

Do not configure one static value globally; that would collapse unrelated
conversations into one lineage.

When a harness compacts or otherwise rewrites a completed transcript, the proxy
imports the complete replacement snapshot into a fresh ephemeral SDK session.
This is how Pi automatic compaction and `/compact` work without a Pi-specific
adapter. The snapshot must be structurally complete, end with a text/image user turn,
and contain no unresolved tool boundary.

Restarting the proxy clears all active sessions, suspended tool calls, imported
history, and replay entries.

## Server options

```text
uv run claude-proxy \
  [--host 127.0.0.1] \
  [--port 8317] \
  [--model MODEL]... \
  [--max-sessions 8] \
  [--tool-result-timeout 300]
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
uv run claude-proxy --model sonnet
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

### `session_mismatch` or `session_conflict`

Keep the model, system prompt, tool definitions, and dialect stable for a
conversation. Do not reuse one explicit session header for unrelated chats, and
do not continue a stale transcript head after a newer turn has committed.

### Changes do not take effect

The server does not hot-reload. Stop it with `Ctrl+C` and start it again after
updating the checkout.

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

The historical feasibility evidence and superseded probe design remain under
[`docs/feasibility`](docs/feasibility/README.md) for auditability. The runnable
gateway and this README are the current usage path.

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
