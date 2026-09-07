# Buffered fallback evidence

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
`claude-agent-sdk` 0.2.152 and Claude Code 2.1.261. Each probe used the production
`SdkSession` option/history builder, disabled refusal fallback, restricted
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
