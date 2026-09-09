# Opt-in direct ChatGPT backend

This backend is experimental and disabled unless `gpt-6-astra` or
`gpt-5.6-sol` is explicitly included in the proxy's `--model` arguments. The
default remains the Claude-backed `sonnet-5` gateway. This project is not an
OpenAI product, and documenting the integration is not an endorsement or a
statement that the fixed, unofficial subscription endpoint is authorized for
your account. Check the terms and limits applicable to your account before use.

The implementation sends requests only to the fixed SSE endpoint
`https://chatgpt.com/backend-api/codex/responses`. It does not expose a native
`/v1/responses` proxy route and never falls back to OpenAI API-key billing, a
different endpoint, another model, or the Claude backend. ChatGPT plan limits
apply. If your account permits continuing with credits after included usage is
exhausted, that may consume those credits; the proxy does not determine or
enforce the account's subscription or credit policy.

## Login and startup

Quaylet stores its own OAuth credentials in `~/.config/quaylet/`, with a private
directory and owner-only credential file. It does not read Pi or Codex credentials.
The earlier `~/.config/claude-sdk-proxy/` location is no longer consulted.
If upgrading, stop any process using the old credential store before moving
that directory to the new name; never overwrite an existing destination or
copy refresh tokens into two active stores. Alternatively, log in explicitly
at the new location. The proxy does not perform automatic migration.

Create a proxy-owned login explicitly; serving never opens a browser:

```bash
uv run quaylet login openai
uv run quaylet auth-status openai
```

Status output contains no access or refresh token. Logout is likewise explicit:

```bash
uv run quaylet logout openai
```

After login, expose one or both exact model IDs:

```bash
uv run quaylet --model gpt-6-astra --model gpt-5.6-sol
```

They use the existing Chat Completions and Anthropic Messages frontend paths.
Do not use Claude aliases for this backend. There are no OpenAI aliases, and
Claude's default model and optional refusal fallback are unchanged. Confirm the
configured provider split with `GET /v1/models`.

Supported explicit reasoning efforts are `low`, `medium`, `high`, `xhigh`, and
`max`; omitting the control retains the provider default. Omission does not
claim to disable internal reasoning. `none`, disabled/budget thinking forms,
and `ultra` are rejected. `max_tokens` and `max_completion_tokens` are accepted
for frontend compatibility but are not enforced upstream. The proxy does not
truncate a tool argument or invent a stop reason to simulate that limit.

## Data, tools, images, and replay

The caller remains responsible for executing tools. Function definitions and
complete result batches are forwarded; the proxy does not run tools. Embedded
PNG, JPEG, GIF, and WebP inputs and successful image tool results use the root
README's count and decoded-size limits. Remote image URLs are never fetched.

Continuation is stateless upstream. The proxy submits the visible complete
transcript and, where available, opaque response metadata needed to preserve
message IDs, function-item IDs, phases, and encrypted reasoning content. Replay
is memory-bounded, account/model/system/tool scoped, and requires an exact
visible prefix. Restart, eviction, edited history, or compaction can lose native
reasoning fidelity and degrade to visible-history translation. An Anthropic
`redacted_thinking` block whose data begins
`openai-subscription:assistant:v1:` is portable OpenAI replay metadata; it is
not evidence that Anthropic or OpenAI produced redacted reasoning. Preserve it
unchanged if the client supports it. Pi may display it as
`[Reasoning redacted]`.

Late upstream model identity can update later stream chunks and non-streaming
JSON, but cannot retroactively change HTTP headers or an Anthropic
`message_start` already sent. Unknown usage stays unknown: JSON/Chat usage is
`null`; Anthropic streaming uses null input/output counters until terminal
usage arrives. Output totals already include reasoning tokens. Cached input is
reported separately and is not added twice.

## Diagnostics and live verification

Use the existing redacted diagnostics controls, for example:

```bash
uv run quaylet --model gpt-6-astra --log proxy.jsonl --log-json
```

Logs include safe categories and request IDs, never credential values, prompts,
tool arguments/results, upstream error bodies, or raw provider identifiers.

The deterministic suite uses mock transport and proves translation, replay,
cancellation, accounting shapes, client compatibility, and credential/header
isolation. It does not prove current endpoint access or account authorization.
The bounded 2026-09-08 execution and its explicit limitations are recorded in
the [live verification follow-up](verification/2026-09-08-openai-live.md);
results from that account and date do not establish future or general access.
After completing the new proxy login, start a separate test-owned proxy on an
alternate loopback port with both exact models in one terminal and run the
bounded live battery in another:

```bash
# Terminal 1
uv run quaylet --port 8318 --model gpt-6-astra --model gpt-5.6-sol

# Terminal 2
OPENAI_SUBSCRIPTION_LIVE=1 \
  OPENAI_SUBSCRIPTION_TEST_BASE_URL=http://127.0.0.1:8318 \
  make live-openai
```

Use another unused loopback port if 8318 is occupied, and set both the server's
`--port` and `OPENAI_SUBSCRIPTION_TEST_BASE_URL` consistently. The battery has
two-minute HTTP deadlines, checks requested routing plus observed upstream model
identity, both models, effort, usage, tool/image continuation, compacted-history
answers, client disconnect, and a stock Pi smoke using only a temporary
`PI_CODING_AGENT_DIR`. After explicit opt-in, a test-owned ephemeral loopback app
uses the proxy-owned login and real fixed upstream endpoint; an instrumented
`httpx.AsyncHTTPTransport` observes that disconnect closes the actual local
upstream connection, releases the active lease, and creates no replay entry.
The probe requests deliberately long output and disconnects after the first
visible content only if the observer has not seen a completed, incomplete,
failed, or error terminal SSE event; a naturally completed response fails the
cancellation gate.
Provider-side compute or billing cessation after connection close is not
observable from this proxy and is not claimed. The battery does not edit
`~/.pi`, activate a service, or make performance claims.
