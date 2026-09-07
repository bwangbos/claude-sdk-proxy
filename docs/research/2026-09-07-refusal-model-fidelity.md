# Classifier refusal and strict model fidelity

## Observed failure

The custom harness captured a `model_refusal_fallback` system event with
`trigger=refusal`, `direction=retry`, `scope=session`, original model
`claude-opus-5`, proposed fallback `claude-opus-4-8`, and category `cyber`.
The trace shows `validate_system_message` rejecting this subtype. The generic
502 hid that cause. The separate `SDK tool bridge closed` traceback can be
cleanup of parked callbacks, not the original failure.

The private payload and trace remain outside the repository. Their contents
and any model-generated tool commands are not published or executed here.

## Decision

Enforce strict model fidelity for all sessions, rather than allow silent mixed
model results. The installed bundled runtime reads
`CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK` in its refusal-fallback eligibility gate.
The proxy now sets it to `1` in SDK options. By contrast, `fallback_model=None`
only omits `--fallback-model`; the CLI describes that option as fallback for
overload/unavailability. It did not disable the observed classifier downgrade.

The no-fallback validator accepts the five documented named categories while
retaining transaction/session correlation, zero-output refusal checks, and
strict tool boundaries. The captured fallback notice is recognized explicitly:
if it still arrives, the proxy reports `model_fallback_disabled`, logs safe model
IDs, and does not consume a replacement answer. It does not fabricate a completed
refusal or usage totals from a switch notice alone. This remains an error if the
runtime fails to honor the strict policy.

There is no permissive fallback mode and no exact-version response-model change.
The response model remains the requested alias. Alias resolution can still change
between runs; strict downgrade prevention is not exact-version pinning.

## Diagnostics

Opt-in diagnostics record backend failure stage, exception class, allowlisted
reason, and proxy code locations before redaction. They exclude exception text,
source text, local variables, request payloads, and upstream refusal explanations.
Existing request-ID response headers remain available. These logs are useful for
identifying the rejecting code path, not a substitute for a complete SDK trace.

## Verification and limitations

- Baseline: 52 existing refusal tests passed.
- New tests first reproduced missing runtime configuration and category rejection.
- Regression coverage includes named categories, both HTTP dialects in JSON/SSE,
  refusal replay/recovery, explicit unexpected-fallback errors, metadata redaction,
  and request correlation for first requests and tool continuations (both runner
  and watcher failures). Terminal error-result and missing-result diagnostics are
  covered as well.
- Offline checks: 676 unit and 222 Darwin lifecycle tests passed for the base
  fix; after the live follow-up, all 853 gateway and 32 integration tests passed
  again (1,783 tests across those suites). Tracked Python lint and mypy passed.
  The final gateway/integration rerun required loopback socket permission; the
  sandboxed attempt could not bind the integration servers. No live requests
  were involved in those suites. No orphaned lifecycle helpers were found.
- Independent review found two diagnostics gaps (stale continuation request IDs
  and unlogged terminal SDK failures); both were reproduced, fixed, and approved
  on re-review.
- The private replay was initially blocked pending permission to transmit the
  transcript to Anthropic. The user subsequently explicitly approved it.
- Live replay exposed two additional schema gaps: `stop_details` can omit
  `fallback_has_prefill_claim`, and a validated `RateLimitEvent` can appear after
  the raw refusal stop but before `ResultMessage`. Both were reproduced in RED
  tests, fixed, and independently reviewed. A present prefill field must still
  be literal false; ordinary metadata validation/session correlation still apply.
- Two consecutive final replays of the exact eight-message transcript through
  the OpenAI endpoint returned HTTP 200, model alias `opus`, and
  `finish_reason: content_filter`. SDK metadata reported
  `model_refusal_no_fallback`, original model `claude-opus-5`, category `cyber`.
  Request IDs: `req_e8571129305441dfb58a86689e53f513` and
  `req_fa26396107624ecca2bc4215d1651050`.
- Classifier outcomes varied: an earlier replay returned legitimate tool calls.
  No generated tool was executed or given a result; closing that suspended probe
  produced the expected bridge-closed cleanup traceback. An earlier pre-fix
  replay also hit the refusal-order guard; its precise ordering was not captured
  and it did not recur in the final two replays. These checks do not establish
  support for arbitrary mid-stream retractions or every upstream event ordering.
- The running proxy was not restarted. This verifies the supplied continuation
  in an imported isolated session, not a complete benchmark run or its score.

Reference: [Anthropic refusals and fallback](https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback).
