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

Create a proxy-owned login explicitly; serving never opens a browser:

```bash
uv run claude-proxy login openai
uv run claude-proxy auth-status openai
```

Status output contains no access or refresh token. Logout is likewise explicit:

```bash
uv run claude-proxy logout openai
```

After login, expose one or both exact model IDs:

```bash
uv run claude-proxy --model gpt-6-astra --model gpt-5.6-sol
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
uv run claude-proxy --model gpt-6-astra --log proxy.jsonl --log-json
```

Logs include safe categories and request IDs, never credential values, prompts,
tool arguments/results, upstream error bodies, or raw provider identifiers.

The deterministic suite uses mock transport and proves translation, replay,
cancellation, accounting shapes, client compatibility, and credential/header
isolation. It does not prove current endpoint access or account authorization.
After completing the new proxy login, start the proxy with both exact models in
one terminal and run the bounded live battery in another:

```bash
OPENAI_SUBSCRIPTION_LIVE=1 make live-openai
```

To test another loopback port, also set
`OPENAI_SUBSCRIPTION_TEST_BASE_URL=http://127.0.0.1:PORT`. The battery has
two-minute HTTP deadlines, covers both models, effort, usage, tool/image
continuation, compacted history, cancellation, and a stock Pi smoke using only
a temporary `PI_CODING_AGENT_DIR`. It does not edit `~/.pi`, activate a service,
or make performance claims.
