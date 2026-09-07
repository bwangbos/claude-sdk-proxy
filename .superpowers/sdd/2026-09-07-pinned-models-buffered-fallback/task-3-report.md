# Task 3 implementer report

Status: DONE

Base: `9fa3963 fix: gate request fallback auto mode`
Branch: `codex/pinned-models-buffered-fallback`

## Implementation

- Added `FallbackBuffer`, defaulting to 64 MiB per public response. It counts UTF-8 text/thinking, signed/redacted completed thinking, and canonical JSON tool arguments before retaining normalized payload. A separate native admission counter checks raw signature, redacted, thinking, text and partial-JSON bytes before the raw validator retains them. Neither counter claims to bound Python RSS. Adjacent normalized text/thinking and retained raw text/JSON/thinking suffixes are coalesced instead of retaining per-delta objects. Discard and release reset both counters.
- Split native generation from public admission inside `SdkSession`. Strict identity is emitted after validating the exact pinned raw `message_start.model` and before usage/live deltas. Normal typed assistant model metadata must agree. The existing correlated `<synthetic>` no-fallback refusal diagnostic remains the only exception.
- Auto admission withholds identity and all normalized output until a complete terminal result or sealed native tool boundary. The observed transaction is original start, validated notice, raw refusal delta/stop, replacement start, accepted boundary. No synthetic assistant is required on the discarded leg. Only one allowlisted Opus 5 → Opus 4.8 transition is admitted. The session retains active backend model and fallback provenance afterward.
- Notice parsing accepts only the observed field schema with an empty `retracted_message_uuids` list. Nonempty/unknown/committed IDs and additional retraction schemas fail closed. The parser validates category/content/explanation types and session/model consistency. There is no arbitrary router or local retry.
- Original raw tool starts, registered invocations, and entered/parked callbacks reject switching with `fallback_tool_rollback_unsupported`. A read-only bridge activity property is the only bridge change. The existing cancel/disconnect cleanup closes the complete native session and drains callbacks; no partial reset or fabricated result exists.
- Buffer/protocol failure and cancellation close the SDK session and discard unpublished payload. Generator finalization at an already committed boundary does not itself destroy the actor-owned reusable session.
- Missing initial identity is reported as `backend_model_mismatch`; missing-start errors can include a safe `Agent SDK protocol failure` / `stream ended without result` suffix. Present but wrong raw/typed metadata uses the bare reason string.

## Evidence boundary

The live evidence remains exactly the Task 1 empty-original-leg, empty-retraction transaction. Tests for discarding closed original text and signed thinking are explicitly **synthetic**; they reuse the evidenced raw refusal termination schema and normal native blocks, without inventing a retraction frame or requiring a nonexistent synthetic diagnostic. Active/unclosed original blocks remain unsupported and fail closed. Discarded-leg refusal counters may be nonzero; the existing no-fallback refusal still requires zero output counters. Accepted usage comes solely from the replacement raw boundary, never cumulative Result totals or the discarded leg.

No new live probe, benchmark tool, Pi configuration change, service restart, push, or merge was performed. User-owned untracked Meridian research is untouched.

## Compatibility edits authorized by the ledger

- `app.py` consumes the first identity event as metadata before the existing first-usage streaming setup. This preserves strict Anthropic initial-usage frames; it does not enable auto or implement Task 4 headers/recovery.
- `anthropic_api.py` and `openai_api.py` ignore identity as content.
- `tool_session_actor.py` permits identity alongside usage in a validated empty-refusal transcript.
- Existing SDK fixtures now provide explicit pinned raw and typed models. The refusal-continuation fixture factory pins complete fixture envelopes to its selected model (including settings-switch scenarios). Expected native sequences include `ResponseIdentity`. No production validator exemption was added for fixtures.

Task 4 still owns public requested/actual/fallback headers, persisted/replayed identity metadata, policy/recovery behavior, and HTTP safe-reason mapping (including the three new native reason prefixes). Both public auto gates remain unchanged.

## TDD evidence

All commands were run from `/Users/bwang/Projects/claude-sdk-proxy`.

Initial `uv run pytest tests/gateway/test_sdk_fallback.py -q` could not initialize the sandbox-inaccessible default uv cache. Subsequent commands used an isolated writable cache:

```text
UV_CACHE_DIR=/private/tmp/claude-sdk-proxy-uv-cache uv run pytest tests/gateway/test_sdk_fallback.py -q
RED: 8 failed in 0.31s
```

Relevant expected failures: `ModuleNotFoundError: ...sdk_fallback` for the new collector; observed switch raised `ModelFallbackDisabled`; missing/wrong raw model reached `stream ended without result` instead of identity rejection; conflicting typed model did not raise. After initial implementation the same command produced `8 passed in 0.27s`.

Subsequent focused RED/GREEN cycles used:

```text
UV_CACHE_DIR=/private/tmp/claude-sdk-proxy-uv-cache uv run pytest tests/gateway/test_sdk_fallback.py -q --tb=short
```

- Synthetic closed original text discard: `1 failed, 27 passed in 0.35s`, failing at the original empty-only fallback gate; after allowing closed non-tool blocks through the evidenced refusal stop validation: `28 passed in 0.33s`.
- Coalesced tool/thinking suffix: after correcting a fixture import typo, `1 failed, 38 passed in 0.35s`, with eight individual character deltas instead of `ThinkingDelta(2, "reason-2")`; after coalescing: `39 passed in 0.32s`.
- Missing raw start/empty stream: `3 failed, 39 passed in 0.35s`, with generic protocol/EOF errors instead of `backend_model_mismatch`; after explicit missing-identity handling the gateway suite passed all 922 then-existing cases.
- Self-review improved malformed-notice tests to include an otherwise complete valid transaction, preventing missing-stop rejection from hiding notice-parser bugs. A malformed `content: []` then produced `1 failed, 42 passed in 0.34s` (`DID NOT RAISE BackendFailure`); adding content-type validation produced the final focused GREEN below.

The final focused tests also cover exact identity timing in strict versus auto mode, missing/conflicting model identity, no-fallback auto success/refusal, replacement signed history, replacement tools, second hop, missing/reordered termination, unconfigured targets, nonempty/unknown/committed retractions, UTF-8 buffer overflow, admission of signatures/redacted data/partial arguments, generation timeout, cancellation/disconnect, and original raw/registered/parked tool cleanup with no fabricated results or reusable epoch.

## Final verification

```text
UV_CACHE_DIR=/private/tmp/claude-sdk-proxy-uv-cache uv run pytest tests/gateway/test_sdk_fallback.py -q
........................................... [100%]
43 passed in 0.35s

UV_CACHE_DIR=/private/tmp/claude-sdk-proxy-uv-cache uv run pytest tests/gateway -q
923 passed in 1.91s

UV_CACHE_DIR=/private/tmp/claude-sdk-proxy-uv-cache uv run pytest tests/integration/test_official_tool_clients.py tests/integration/test_refusal_http.py -q --tb=short
.................. [100%]
18 passed in 4.58s

UV_CACHE_DIR=/private/tmp/claude-sdk-proxy-uv-cache uv run mypy src/claude_sdk_proxy
Success: no issues found in 48 source files

UV_CACHE_DIR=/private/tmp/claude-sdk-proxy-uv-cache uv run ruff check src tests
All checks passed!

git diff --check
(no output; exit 0)
```

The localhost integration command used approved escalation solely for isolated test sockets; it made no live SDK requests. The final gateway command includes the full normal/refusal/tool/thinking/usage regression coverage.

An earlier broad diagnostic command, `UV_CACHE_DIR=/private/tmp/claude-sdk-proxy-uv-cache uv run pytest tests/gateway tests/integration tests/unit -q --tb=line`, produced `108 failed, 1508 passed in 18.10s`: identity-incomplete older fixtures, identity handling of initial usage, and sandbox socket/attestation permissions. The affected gateway failures were resolved and rerun as above; affected HTTP tests were rerun with socket permission. A whole tracked release rerun belongs to Task 5 and was not duplicated here. Whole-tree `ruff format --check src tests/gateway` also reported 22 preexisting/unrelated files needing formatting; those were left alone. Touched implementation/test files were formatted, and whole-tree lint passes.

## Files changed

New: `src/claude_sdk_proxy/sdk_fallback.py`, `tests/gateway/test_sdk_fallback.py`.

Native implementation: `sdk_session.py`, `sdk_metadata.py`, `sdk_tool_protocol.py`, `tool_bridge.py`.

Compatibility consumers: `app.py`, `anthropic_api.py`, `openai_api.py`, `tool_session_actor.py`.

Fixtures/regressions: `tests/gateway/fakes.py`, `test_sdk_session.py`, `test_sdk_refusal.py`, `test_refusal_continuation.py`, `test_thinking_streams.py`, `test_usage_accounting.py`.

This report is the only SDD artifact added by this task.

## Self-review and concerns

- Reviewed the complete native diff and compatibility edits. Corrected wrapper finalization that initially closed reusable sessions after a committed response, and corrected the malformed-notice test masking described above.
- Confirmed strict refusal's zero-output validation and synthetic diagnostic correlation remain unchanged. Checked buffer reset per response and discarded leg; checked that original-tool failure never calls resolve or begins a replacement epoch.
- `sdk_session.py` was already large; this change follows its existing owner/epoch structure and adds a narrow admission wrapper rather than a second session manager. Coalescing uses ordinary string concatenation, so the bound is payload retention, not a promise of constant-time append or a total RSS bound.
- No known remaining correctness blocker within Task 3. Public identity reporting/recovery/error mapping remains deliberately gated Task 4 work. Native nonempty retractions and partial active-block rollback are not supported or claimed.
