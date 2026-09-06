# Harness compatibility fixes: verification

Base: `ff48f9c`. Scope: the approved custom-harness compatibility assessment.

## Changes verified

- Empty OpenAI tool-only assistant content canonicalizes consistently with null.
- Unfiltered official SDK assistant-message dumps and known nullable request
  optionals are accepted. Unknown fields and meaningful unsupported values reject.
- Returned implicit session IDs can be used in subsequent session headers.
- Complete result-ending transcripts import into native SDK history without
  replaying historical caller operations. Suspended replacement requires exact
  pending calls and result IDs; configuration, stale-head, and concurrency guards
  remain in place. Capacity, startup failure, expiry, and cancellation are covered.
- Per-response usage retains cache reads/writes and final output counts. Interim
  typed SDK output snapshots can increase before the final stream delta;
  cumulative Result usage is validated but is not published as a turn's usage.
- Completion timestamps, safe session-error reasons, request IDs, and opt-in
  metadata-only file/JSON diagnostics are implemented.
- OpenAI `user` remains advisory metadata, not a session key. The existing random
  public tool-ID generator is unchanged. Text-array tool results already worked.

## Final verification

- Gateway and real-HTTP client integrations: **590 passed**, with strict markers,
  no skipped tests, and warnings treated as errors.
- Live authenticated SDK checks: **8 passed** across both dialects, covering
  missing/suspended result-ending recovery and follow-up turns, ordinary caller
  tool rounds, and existing completed-history imports.
- Ruff, mypy (43 source files), and `git diff --check`: passed.
- Two fresh independent review scopes approved: compatibility/usage and
  recovery/diagnostics. The latter found expiry and cancellation-cleanup races;
  both received failing regressions, fixes, and a successful re-review.
- Final process inspection found no orphan proxy anchors, supervisors, or SDK
  probe processes. The user's running proxy was not restarted or modified.

The original harness's turn-14 incident cannot be attributed conclusively without
its request trace. Earlier live investigation also saw one unclassified HTTP 502
on a synthetic imported request; the final recovery matrix and subsequent wider
live run passed. These tests do not promise freedom from upstream/backend errors.

## Pre-existing full-release gate failure

`make release-offline` stops in the legacy unit suite: **108 failed, 565 passed**,
with native journal `LOCK_TIMEOUT` errors. The same representative failure
(`tests/unit/test_journal.py::test_first_valid_child_wins`) reproduces on an
untouched temporary export of `ff48f9c`, compiled independently.

The Python journal constructs absolute deadlines with `time.monotonic_ns()`
(`mach_absolute_time()` on this host), while `native/lifecycle.c` compares them
against `CLOCK_MONOTONIC_RAW`. At inspection, the latter was approximately
510 seconds ahead; the normal one-second deadlines therefore expired immediately.
Neither clock implementation was changed by this work. This remains a separate
legacy lifecycle issue, so the full release gate is **not green**.

No push, PR, merge, running-server restart, or local Pi configuration change was
performed as part of this implementation.
