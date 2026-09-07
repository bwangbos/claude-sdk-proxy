# Buffered fallback evidence

> Subsequent capability update: [Opus 4.8 vision verification](2026-09-07-opus-4-8-vision-verification.md)
> verified image inputs and image tool results. The text-only Pi restriction
> recorded at the earlier checkpoints below has been removed. This does not
> establish image-bearing automatic fallback or expand native retraction support.

## Latest verified checkpoint

Merged implementation `15ccede` passed the complete tracked-snapshot release
gate on 2026-09-07: 676 unit, 222 Darwin lifecycle, 1,016 gateway, and 40
integration tests (1,954 total), plus Ruff and strict mypy. Final review and
scoped re-review had no remaining findings. Native message-ID separation and
amortized text/thinking/tool-argument accumulation are included in this revision.

Live production OpenAI JSON checks verified both strict Opus 5 refusal and
automatic replacement by Opus 4.8, with truthful requested/actual/fallback
metadata. The final native-ID/performance fix was also live-checked successfully.
Checks stopped at returned tool calls without executing them. Live post-tool
continuation and Anthropic/SSE fallback remain unverified; deterministic tests
cover those paths. The dated investigation and failures below are retained as
history, not unresolved release failures.

## Isolated native probes

On 2026-09-07 the already-authorized eight-message harness transcript was
imported into isolated SDK sessions. Production proxy processes and settings
were unchanged. No generated tool was executed or given a fabricated result.
The temporary probe is `/private/tmp/claude-buffered-fallback.PS7rFo/probe.py`;
it is not a shipped implementation or a portable fixture.

The probe used the production session option/history builder, then read the
native SDK stream directly, bypassing the strict proxy validator solely to
observe the opt-in protocol. It set the pinned model `claude-opus-5`,
`CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK=0`, and inline `--settings` JSON with
`availableModels` containing the permitted exact model IDs.

With Opus 5 and Opus 4.8 allowed, the observed sequence was:

1. Raw `message_start`, model `claude-opus-5`.
2. System `model_refusal_fallback`, original `claude-opus-5`, target
   `claude-opus-4-8`, category `cyber`, trigger `refusal`, direction `retry`,
   scope `session`, empty `retracted_message_uuids`.
3. Raw `message_delta` with `stop_reason=refusal`, then `message_stop`.
   No synthetic refusal assistant occurred on this discarded leg.
4. New raw `message_start`, model `claude-opus-4-8`.
5. Replacement text and tool-use blocks, including typed AssistantMessages
   naming `claude-opus-4-8`.
6. Raw `message_delta` with `stop_reason=tool_use`, then `message_stop`.

The probe stopped at that tool boundary. Native callbacks were canceled by
closing the session, producing the existing bridge-closed teardown traceback.
This does not prove post-result continuation or arbitrary callback rollback.

With only Opus 5 allowed, and the same fallback-enabled environment, the observed
sequence instead contained `model_refusal_no_fallback`, a correlated synthetic
refusal assistant, raw refusal stop, rate-limit metadata, and an error/refusal
ResultMessage. No Opus 4.8 message occurred. This supports native allowlist
enforcement for the observed route, not every possible future runtime route.

## Remaining evidence requirements

- Image input capability remains unverified for Opus 4.8 and must not be added
  to Pi's advertised profile from this evidence alone.
- Nonempty retraction ordering and discarded-leg tool activity, if obtainable
  without altering the benign workload. Do not invent native event schemas or
  weaken the spec to make an unobserved case appear supported.
- Purely synthetic defensive tests must reject original-leg tool activity and
  prove callback cleanup even if that event ordering is not observed live.

No full benchmark score, completed fallback tool round, or general mid-stream
retraction support is claimed by these probes.

## Direct strict identity and capability probes

On the same date, additional harmless probes used the installed
`claude-agent-sdk` 0.2.152 and reported system Claude Code 2.1.261. That version
check did not establish which executable the SDK selected; the later production
investigation below identified its bundled 2.1.259 runtime. Each probe used the
production `SdkSession` option/history builder, disabled refusal fallback, restricted
`availableModels` to the three exact configured IDs, imposed a 60-second
per-session timeout, and closed its isolated session. Output was limited to
event classes/types, model IDs, block types, stop reasons, metadata field names,
and pass/fail state. No prompt text, response text, tool arguments, credential,
or raw diagnostic was retained.

Harmless direct text requests passed for all three exact IDs:

| Requested SDK model | First public-output candidate | Early identity | Terminal result |
| --- | --- | --- | --- |
| `claude-sonnet-5` | raw text block start | preceding raw `message_start.model=claude-sonnet-5` | successful `end_turn` |
| `claude-opus-5` | raw text block start | preceding raw `message_start.model=claude-opus-5` | successful `end_turn` |
| `claude-opus-4-8` | raw text block start | preceding raw `message_start.model=claude-opus-4-8` | successful `end_turn` |

Each sequence was `SystemMessage(init)`, `SystemMessage(status)`, optional
`RateLimitEvent`, raw `message_start`, raw text start/delta, typed
`AssistantMessage`, raw content stop, raw `message_delta(end_turn)`, raw
`message_stop`, and successful `ResultMessage`. Rate-limit metadata can occur at
different positions and is not model identity or public content. Every typed
assistant in these probes named the same exact model as its raw start.

A seeded-history Opus 4.8 request, built by the production history seeder from a
small text-only user/assistant/user history, followed the same ordering. Its raw
`message_start.model=claude-opus-4-8` preceded the first text block and delta;
the typed assistant agreed and the result succeeded with `end_turn`. This proves
the required early identity on the observed text import shape, not all signed
thinking, image, or pending-tool recovery shapes.

A harmless Opus 4.8 tool request produced an initial text block followed by a
raw `tool_use` block and `input_json_delta` events. The exact raw
`message_start.model=claude-opus-4-8` preceded both the text and tool candidates,
and both typed assistant envelopes named `claude-opus-4-8`. The probe stopped at
raw `message_stop` with `stop_reason=tool_use`; it did not execute the tool or
provide a result. Closing the session canceled the already-entered bridge call
and produced the known bridge-closed teardown traceback. This proves early
identity and cleanup at the observed tool boundary, not post-result continuation
or rollback of original-leg tool activity.

The runtime's server-info model entry for `claude-opus-4-8` had fields
`supportsAdaptiveThinking`, `supportsEffort`, and `supportedEffortLevels`; it
reported effort support for exactly `low`, `medium`, `high`, `xhigh`, and `max`.
Independent short Opus 4.8 requests using adaptive thinking plus each one of
those five effort values all produced an exact early Opus 4.8 raw start, a
matching typed assistant, and a successful `end_turn` result. This establishes
live request acceptance for those configurations. It does not establish signed
thinking replay, exact usage mapping, output quality, or an image capability.

The earlier no-fallback refusal remains structurally distinguishable from a
normal model response: its typed assistant uses model `<synthetic>`, is
correlated with the refusal diagnostic, and accompanies an error/refusal result.
It must never supply actual-model identity. The successful strict probes emitted
an exact raw model start before content instead. The observed native fallback
notice still had an empty `retracted_message_uuids`; no benign probe produced a
nonempty retraction, and no native retraction event schema is claimed.

One sandboxed diagnostic run could not access the existing macOS authentication
state and ended synthetically before a raw start. Repeating one harmless case
with the approved host permission succeeded. This was a probe-environment
permission limitation, not model-availability evidence; live capability checks
must have access to the normal authenticated runtime without printing or
extracting credentials.

## Release implementation verification

The final implementation verification kept live observations distinct from
deterministic synthetic coverage. After explicit reauthorization, the captured
private eight-message request was sent exactly twice through the production
OpenAI-compatible HTTP endpoint: once with `off` and once with `auto`. The probe
recorded only HTTP status, model/provenance headers, response model, finish
reason, error reason, and tool-call count, then stopped at the response boundary
without executing a generated tool.

Strict `off` returned HTTP 200 with requested, actual, and response model all
`opus-5`, no fallback header, `finish_reason=content_filter`, and zero tool calls.
This verifies that the implemented default path exposes the refusal without a
downgrade.

The `auto` request failed safely with HTTP 502, no requested/actual/fallback
response headers, no response model, and no generated tool execution. Its
redacted public error did not include a machine-readable `reason`, so this run
does not establish whether the failure arose before or after a native switch.
No third transmission was authorized or attempted. Consequently, replacement
publication and fallback provenance are not live-verified through the production
HTTP path; the isolated native switch above and synthetic implementation tests
remain separate evidence, not a substitute for that missing comparison.

### Offline diagnosis of the auto failure

Redacted diagnostics localized the failure after
`model_fallback_accepted` (`claude-opus-5` to `claude-opus-4-8`) and before the
replacement leg, in `RawSdkMessageValidator._refusal_message_delta`. No prompt,
response text, tool argument, credential, or raw exception text was logged.

The SDK transport prefers its bundled CLI over the system installation. The
runtime used by this check was therefore the wheel's bundled Claude Code
2.1.259 Mach-O binary at
`.venv/lib/python3.14/site-packages/claude_agent_sdk/_bundled/claude` (SHA-256
`884baa38fe1a624be25c4a91568bf5a08b5cf4e7d7acf29b7760e3525d964898`), not the
separately installed `claude` 2.1.261 executable.

Static inspection of the embedded local runtime source explains the rejected
shape. Near binary offset 180053205, `onRefusalFallbackBanner` closes a partial
SDK stream through its `Qs` helper. That helper constructs `message_delta` with
`context_management: null` and this delta:

```json
{"container":null,"stop_details":null,"stop_reason":"refusal","stop_sequence":null}
```

It then emits `message_stop`. Earlier in the runtime's API-stream handler (near
offset 166278000), the original API refusal delta is used to create a
`fallback_request`; that branch returns before the normal raw `stream_event`
yield. Thus the post-banner closing delta exposed to the SDK consumer is the
adapter-generated close above, not a faithful copy of the original API refusal
details.

The proxy currently requires the discarded-leg delta to have exactly
`stop_reason`, `stop_sequence`, and `stop_details`, and then requires
`stop_details` to contain the correlated refusal category and explanation.
The runtime-generated `container` key causes the first exact-key check to fail;
its null `stop_details` would also fail the following details check. This fully
accounts for the observed failure location. It does not by itself authorize a
compatibility change: accepting this synthetic close safely requires a focused
contract decision and RED tests tying it to an already validated fallback
banner, rather than generally weakening refusal validation.

Deterministic production tests separately cover the implemented behavior for
both HTTP dialects and JSON/SSE responses: default and per-request policy,
configured-route enforcement, replacement-only publication, actual/requested
headers and SSE model identity, fallback provenance through replay and recovery,
bounded buffering, stale replay behavior, policy changes, malformed transitions,
extra hops, disconnect/deadline cleanup, and explicit rejection/drain of
discarded-leg tool activity. These fixtures are synthetic protocol coverage and
are not claims that every defensive event order was seen live.

The two existing Pi proxy providers were migrated to canonical `sonnet-5`,
`opus-5`, and `opus-4.8` entries without changing their endpoints, credentials,
compatibility settings, or unrelated providers. Sonnet 5 and Opus 5 retain the
previously verified text/image profiles. Opus 4.8 advertises text, tools, and
adaptive thinking but remains text-only in Pi because no live image-input check
for that exact model was completed. Pi does not enable refusal fallback globally;
the server default and request header remain the controls.

A final harmless direct text-access check repeated the exact pinned models with
fallback disabled. Sonnet 5, Opus 5, and Opus 4.8 each emitted an exact raw
`message_start.model` before text, a matching typed assistant model, and a
successful `end_turn` result. This reconfirmed account access without exercising
the private refusal reproduction or expanding the capability claims above.

## Bundled close compatibility fix and reauthorized verification

After the controller approved the exact correlated synthetic close and the user
authorized further bounded verification, the validator gained a discarded-leg-only
compatibility path. The no-fallback refusal validator remains unchanged. The path
requires the validated allowlisted banner, existing session/phase correlation,
the exact `Qs` delta and null context, and the exact seven `Xs` usage keys.
Nullable input/cache counters are treated as unavailable, never invented as zero;
present counters still reconcile against the raw start. Output tokens must remain
a nonnegative integer. Accepted public usage still comes only from the replacement.

Static inspection of `Xs` immediately following `Qs` shows `?? null` for
`output_tokens_details`, the three input/cache counters, `iterations`, and
`server_tool_use`; `output_tokens` is passed directly. A bounded structural-only
live diagnostic additionally observed `output_tokens_details` with integer
`thinking_tokens`, `server_tool_use` with integer `web_fetch_requests` and
`web_search_requests`, and empty `iterations`. Embedded source around offset
166307077 independently constructs those nested counters. The validator accepts
only null or these exact nonnegative-integer nested counter shapes, and null or
empty iterations. Unknown keys, malformed counters, and nonempty iterations fail
closed; no general iteration schema is claimed.

Three additional private-request transmissions occurred during this authorized
fix verification, all through the production application in isolated sessions:

1. The initial null-only ancillary validator failed safely with HTTP 502.
2. A metadata-only diagnostic reproduced that failure and recorded usage
   structure/types only, establishing the nested shapes above.
3. After the source-correlated fix, request
   `req_db9dd7b5bd8f44b0bfd756e8ba24d2df` returned HTTP 200 with requested model
   `opus-5`, actual/response model `opus-4.8`, fallback header `true`, finish reason
   `tool_calls`, and one returned tool call.

No generated tool was executed and no tool result was fabricated. The successful
probe stopped at the response boundary and closed the isolated session, producing
the previously known `SDK tool bridge closed` teardown traceback. This establishes
live replacement publication/provenance for the production OpenAI JSON path at
that boundary, not live post-result continuation or live Anthropic/SSE coverage.
Both dialects, JSON/SSE, replay, and strict refusal remain covered offline. The
running proxy was not restarted, and no Pi configuration was changed by this fix.
