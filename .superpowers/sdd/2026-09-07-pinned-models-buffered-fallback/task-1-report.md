# Task 1 report: native evidence gate

## Changes

- Updated `docs/research/2026-09-07-buffered-fallback-evidence.md` with the
  installed runtime versions, sanitized strict model/event observations, Opus
  4.8 effort metadata and live results, and explicit unsupported boundaries.
- Kept all probe code under `/private/tmp`; no fixture or production-code change
  was needed because no new native event schema was observed.
- Preserved the unrelated untracked
  `docs/research/2026-09-06-meridian-head-to-head/` directory unchanged.

## Exact safe probe results

- Runtime: `claude-agent-sdk` 0.2.152; Claude Code 2.1.261 at
  `/Users/bwang/.local/bin/claude`.
- Direct strict text:
  - `claude-sonnet-5`: exact raw start before text; matching typed assistant;
    successful `end_turn`.
  - `claude-opus-5`: exact raw start before text; matching typed assistant;
    successful `end_turn`.
  - `claude-opus-4-8`: exact raw start before text; matching typed assistant;
    successful `end_turn`.
- Seeded text history on `claude-opus-4-8`: exact raw start before text;
  matching typed assistant; successful `end_turn`.
- Harmless Opus 4.8 tool boundary: exact raw start preceded initial text and
  later `tool_use`/`input_json_delta`; typed assistants matched; probe stopped at
  raw `message_stop(tool_use)` without tool execution or a fabricated result.
  Session closure canceled the bridge call and emitted the known bridge-closed
  teardown traceback.
- Runtime model metadata for `claude-opus-4-8` reports adaptive thinking,
  effort support, and exactly `low`, `medium`, `high`, `xhigh`, `max`.
- Adaptive Opus 4.8 requests at `low`, `medium`, `high`, `xhigh`, and `max` each
  had an exact early raw start, matching typed assistant, successful `end_turn`,
  and clean session closure.
- The pre-existing positive fallback observation remains Opus 5 refusal notice
  with empty retraction IDs followed by exact Opus 4.8 replacement identity and
  tool output. The pre-existing target-exclusion observation remains the normal
  `model_refusal_no_fallback` path with correlated `<synthetic>` assistant and no
  Opus 4.8 message.

## Observed limits and concerns

- No benign workload produced nonempty `retracted_message_uuids`; there is no
  observed native retraction event schema to implement.
- Original-leg tool activity during fallback was not observed. It remains an
  explicitly unsupported defensive-rejection case, not evidence for rollback.
- Image input capability on Opus 4.8 was not probed and must not be advertised
  from Task 1 evidence.
- Effort probes establish live configuration acceptance, not complete signed
  thinking/replay or exact usage-mapping capability.
- Seeded evidence covers ordinary text history, not signed-thinking, image, or
  pending-tool recovery imports.
- A sandboxed run lacked access to macOS authentication and failed before raw
  start; one approved host-permission control succeeded. No credential or raw
  diagnostic was recorded.
- Runtime `RateLimitEvent` placement varied and is not an identity or content
  boundary.

## Verification

- `UV_CACHE_DIR=/private/tmp/claude-task1-uv-cache uv run pytest
  --strict-markers --forbid-skips -W error tests/gateway/test_sdk_refusal.py
  tests/gateway/test_refusal_continuation.py -q`: **129 passed in 0.50s**.
- Self-review confirmed the Git diff contains only this report and the evidence
  note, with no prompt/response text, tool argument, credential, raw refusal
  explanation, session ID, or private transcript content.
