# Claude Agent SDK text gateway and historical probes

This package ships a private localhost text gateway and retains the earlier
trusted-local feasibility probes as historical comparator evidence. The
authoritative offline release gate, including the real Pi provider integration,
is:

```console
make release-offline
```

Live subscription checks remain separately opt-in.

## Current runnable text gateway

The current implementation is a private, single-user compatibility gateway for
fresh, linear text conversations. It uses the Claude Agent SDK and the Claude
login already available to the process. Run it from the same normal host login
context where `claude` is authenticated; a sandboxed process may not be able to
read the macOS Keychain item even though the CLI works in a terminal.

Install the locked dependencies and launch the default model on loopback:

```bash
uv sync --dev
uv run claude-proxy --model sonnet
```

The server listens at `http://127.0.0.1:8317`. It exposes
`POST /v1/chat/completions`, `POST /v1/messages`, `GET /v1/models`, and
`GET /health`. `--host` accepts loopback IP addresses only. Repeat `--model`
to expose more than one configured Agent SDK model alias. `--max-sessions`
sets the positive retained-session limit and defaults to 8. Both POST endpoints
require `Content-Type: application/json`; normal media-type parameters such as
`charset=utf-8` are accepted.

### Pi configuration

Add this provider to `~/.pi/agent/models.json`:

```json
{
  "providers": {
    "claude-subscription-local": {
      "baseUrl": "http://127.0.0.1:8317/v1",
      "api": "openai-completions",
      "apiKey": "local-placeholder",
      "compat": {
        "supportsDeveloperRole": false,
        "supportsReasoningEffort": false,
        "supportsStore": true,
        "supportsUsageInStreaming": true,
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

Start a new text-only Pi session with:

```bash
pi --provider claude-subscription-local --model sonnet --no-tools
```

The dummy API key is deliberately non-secret; the gateway ignores it and uses
the operator's local Claude login. Do not configure sampling parameters,
reasoning options, or tools. The Pi provider's normal `store: false` and
streaming-usage fields are accepted, as is the advisory `max_tokens` field
added by Pi's ordinary `streamSimple` path.

Pi 0.84.4 enables automatic context compaction by default, and `/compact`
performs the same lossy history replacement manually. Both produce a rewritten
transcript that this append-only gateway must reject. Add the following key to
the existing `~/.pi/agent/settings.json`, or to `.pi/settings.json` for only the
current project, and do not invoke `/compact` while using this provider:

```json
{
  "compaction": {
    "enabled": false
  }
}
```

Disabling auto-compaction does not make very long sessions unlimited; start a
new Pi session before the model context is exhausted.

Most clients can use transcript matching without a custom header. A client that
can set per-conversation headers may send a unique
`X-Claude-Proxy-Session: <id>` to distinguish independent conversations that
begin with identical text. Never configure one static value globally: that
would collapse all conversations into one lineage.

### Supported boundary

- Supported: text-only streaming and non-streaming calls, exact system/user
  strings, retries of completed requests, and append-only continuations that
  originated through this running gateway.
- Advisory only: `max_tokens` and `max_completion_tokens`; the Agent SDK does
  not provide exact output-token enforcement through this path.
- Unsupported: imported assistant histories, edits, branching, tools, exact
  sampling/stop controls, public or multi-user service, and recovery of live
  conversations after the gateway restarts.
- Capacity: at most 8 sessions are retained by default. Fresh admission at the
  limit evicts the least-recently-used idle session. In-flight and
  replay-reserved sessions are never evicted; if all retained sessions are busy,
  both dialects return HTTP 503 with `session_capacity`.
- Concurrency and replay: one turn at a time per conversation. An in-flight
  duplicate or continuation returns HTTP 409. Only the current completed
  transcript head replays from memory; an older head may return
  `session_mismatch` after a successful continuation.
- Terminal reasons: Anthropic preserves `end_turn`, `max_tokens`, `refusal`, and
  `model_context_window_exceeded`. OpenAI maps them to `stop`, `length`,
  `content_filter`, and `length`, respectively. Chat Completions has no distinct
  context-window reason, so context exhaustion deliberately uses its truncation
  signal.

The real Pi provider integration suite uses actual Uvicorn and localhost HTTP
but deterministic fake SDK sessions, so it never invokes a model:

```bash
.venv/bin/pytest -q --strict-markers --forbid-skips -W error tests/integration
```

It is included in `make release-offline`, which is the authoritative offline
release gate. `make check` remains the faster development gate.

The separately gated live test uses only a synthetic marker and never inspects
credentials:

```bash
CLAUDE_PROXY_LIVE=1 CLAUDE_PROXY_LIVE_MODEL=sonnet \
  .venv/bin/pytest -q --strict-markers -m live tests/live/test_gateway_text.py
```

## Superseded legacy Phase 0 feasibility record

Everything below this heading describes the earlier fail-closed probe design,
its Agent SDK 0.2.148 pin, and its negative Phase 0 verdict. It is retained for
audit history and is not the launch or verification contract for the current
Agent SDK 0.2.152 text gateway above. In particular,
`RUN_LIVE_CLAUDE_TESTS=1` is the legacy probe opt-in; current gateway live tests
use `CLAUDE_PROXY_LIVE=1` plus `CLAUDE_PROXY_LIVE_MODEL`.

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
