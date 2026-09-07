# Pinned models and buffered native refusal fallback

## Goal and approved scope

Expose `sonnet-5`, `opus-5`, and `opus-4.8`, with version-pinned SDK routing.
Provide explicit opt-in native refusal fallback for unmodified OpenAI-compatible
and Anthropic-compatible harnesses. Strict mode remains the default and streams
normally. The user approved buffering responses in automatic-fallback mode.

This is not a general model router or a custom retry engine. Do not add Fable,
Haiku, older model families, overload retry chains, prompt rewriting, or local
model forwarding. Do not execute user-provided benchmark tools during verification.

## Model identities

| Public model | SDK model |
| --- | --- |
| `sonnet-5` | `claude-sonnet-5` |
| `opus-5` | `claude-opus-5` |
| `opus-4.8` | `claude-opus-4-8` |

Retain `sonnet` and `opus` as deprecated input/CLI compatibility aliases pinned
to the corresponding version above, rather than forwarding moving SDK aliases.
Preserve currently supported explicit SDK IDs. Normalize equivalent IDs before
session comparison and capability validation. `/v1/models` advertises canonical
public IDs in configured order without duplicate compatibility entries.

Keep repeated `--model` as the allowlist mechanism. Default to `sonnet-5` when
no model is configured, preserving the current single-model default behavior.
Pinning is routing, not merely a display label. Do not permit ambient model
alias overrides to change the chosen backend version.

Verify Opus 4.8 tool, image, and thinking capabilities through the installed
runtime before advertising them in Pi. Do not infer every model's thinking
limits from Opus 5. Update the existing Pi proxy providers, preserving unrelated
providers, credentials, and extensions, only when implementing the model config.

## Public fallback controls

- Server: `--refusal-fallback off|auto`, default `off`.
- Request: `X-Claude-Proxy-Refusal-Fallback: off|auto`, overriding the server
  default. Absent means the server default; invalid or repeated values are 400.
- This header is transport metadata, not injected prompt content.
- Automatic mode permits the observed native `claude-opus-5` to
  `claude-opus-4-8` refusal transition only. The target must also be in the
  server's configured model allowlist; reject an incompatible automatic Opus 5
  request before launching it. No arbitrary fallback-target parameter.
- Sonnet 5 and Opus 4.8 remain directly selectable. Auto mode does not invent
  a fallback route for them; it still follows the buffered response contract.
- Runtime configuration must prevent an unconfigured fallback from running,
  not merely hide its answer afterward. Validate the observed target as a
  second check. Keep overload fallback unconfigured.

Example intended invocation:

```bash
uv run claude-proxy --model sonnet-5 --model opus-5 --model opus-4.8 \
  --refusal-fallback auto
```

## Buffering and native event handling

The buffering unit is one public response, ending at either a tool-call boundary
or a terminal answer/refusal. It is not an entire conversation: tool results
still come from the harness between responses.

In `auto` mode, hold text, thinking, tool calls, usage, and response headers until
the accepted boundary and its actual model are known. `stream: true` still returns
normal SSE frames, but only after generation for that response completes.
No custom client-side retraction protocol is required. Strict mode retains its
existing incremental streaming behavior.

Treat SDK refusal/fallback notices and any retraction events as a validated
transaction. Correlate session identity, native message identities, original
model, target model, and permitted transition. Discard only the unpublished
retracted leg; do not concatenate its text/thinking/tools with the replacement.
Retain the successful replacement's native signed history and tool correlation.

Retractions must never modify a previously committed public response or a tool
call already given to the harness. If the runtime requests that, terminate with
a safe, explicit protocol error rather than pretending external actions can be
undone. Unknown retraction IDs, unknown targets, mismatched sessions, unexpected
extra hops, and malformed notices likewise fail without publishing buffered
output. Preserve existing request-ID diagnostics.

Keep disconnect monitoring, generation deadlines, tool-result timeouts, cleanup,
and session-capacity limits active during buffering. Bound retained output to
avoid unbounded memory growth; document the chosen existing or new explicit
limit in the implementation plan and test its failure/cleanup behavior.

## Model reporting and usage

The response `model` must reflect the accepted SDK model, mapped to the canonical
public name. Read/validate actual model metadata, not only the requested alias or
the fallback notice. All SSE model fields must agree with that accepted model.

Return `X-Claude-Proxy-Requested-Model` and `X-Claude-Proxy-Actual-Model` on
successful responses. Add `X-Claude-Proxy-Fallback: true` when the response is
served by the session's fallback model, including subsequent responses after
the switch. Replay must reproduce the original response metadata.

Do not claim exact provenance for raw-model metadata that has not been observed.
Keep requested routing identity separate from active runtime model identity.
Clients can keep requesting `opus-5` throughout an auto-fallback conversation
without causing a reset to Opus 5 on every tool-result submission.

Usage must describe the accepted response using supported native counters.
Do not sum cumulative SDK Result usage into per-response usage or mix discarded
leg output counts into context usage. If fallback billing counters cannot be
represented faithfully, document that limitation instead of inventing totals.

## Session policy and replay

Include the effective fallback policy in request fingerprints and generation
configuration, not in the user-visible transcript. It must remain unchanged
during pending tool-result continuations. Reject a mid-tool policy change with
409 and a safe reason explaining that the boundary must be completed first.

Allow policy/model changes at completed assistant boundaries through the existing
transactional settings replacement path. In particular, changing auto to off
after a downgrade must restore the requested pinned model in a new native
session; it must not leave strict requests silently running on the fallback.

An exact request retry replays the accepted response and its model/fallback
metadata without another native generation. Rebase/import must preserve the
requested policy while deliberately re-establishing runtime state. Existing
transcript identity, signed thinking, image, and tool-result checks remain strict.

## Implementation boundaries

- Model catalog: one mapping/capability source used by CLI, parsing, SDK launch,
  session comparison, and response reporting.
- Request policy: resolved at the HTTP boundary and passed through canonical
  request/session types; no duplicated endpoint-specific policy logic.
- Native transaction handling: isolated from rendering, owns validation and
  discarded/accepted output selection.
- Response metadata: carried with committed/replayed response state; renderers
  must not query mutable session state to determine a past response's model.
- Existing actor/registry own lifecycle and tool boundaries. Do not build a
  second independent session manager or retry loop.

## Acceptance checks

1. Pin each public model and legacy alias to the intended SDK ID; verify
   canonical model listings, allowlist rejection, and thinking capability checks.
2. Strict mode behaves as today: incremental output and ordinary refusal, with
   no automatic downgrade. Test both HTTP dialects and tools/no-tools.
3. Auto mode with partial original text/thinking followed by native fallback
   exposes only replacement output, correct model headers/SSE fields, and valid
   usage. Test JSON and SSE, including a no-fallback successful response.
4. Fallback tool calls execute zero times inside the proxy and are returned once
   for caller execution. Tool results continue the fallback session. Replays do
   not duplicate calls or rerun generation.
5. Fail safely on malformed or cross-boundary retractions, unconfigured targets,
   deadline/disconnect/buffer-limit failures, and attempted extra fallback hops.
6. Changing policy after a completed turn restores the requested model; changing
   it during pending tools is rejected without damaging the valid continuation.
7. Log actual switch decisions and safe error causes under the active request
   ID, with no prompts, tool payloads, credentials, or raw refusal explanations.
8. Offline regression suite, lint, type checking, and independent review pass.
   Live checks use isolated sessions and harmless tools or the already-authorized
   reproduction; no generated tool execution and no running-proxy restart.

## Evidence gate before implementation of native fallback

Capture the installed SDK's complete event-order and retraction metadata shapes
under the approved opt-in policy, including actual model reporting and a
post-switch tool boundary. Existing evidence proves that a switch notice occurs,
but does not prove that simply ignoring it yields a correct replacement stream.
Use synthetic tests derived from these observations, not guessed SDK frames.

If the SDK cannot restrict fallback to the configured target or cannot complete
the transaction without retracting already committed public output, stop and
report that limitation; do not silently weaken the contract above.
