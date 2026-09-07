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

- Early actual-model metadata for direct Sonnet 5/Opus 4.8 and the remaining
  strict text/tool/import cases; the current probe establishes imported Opus 5
  refusal and replacement Opus 4.8 tool output only.
- Opus 4.8 effort/image capabilities before updating Pi's advertised profile.
- Nonempty retraction ordering and discarded-leg tool activity, if obtainable
  without altering the benign workload. Do not invent native event schemas or
  weaken the spec to make an unobserved case appear supported.
- Purely synthetic defensive tests must reject original-leg tool activity and
  prove callback cleanup even if that event ordering is not observed live.

No full benchmark score, completed fallback tool round, or general mid-stream
retraction support is claimed by these probes.
