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
Neither clock implementation was changed by the compatibility commit. At that
checkpoint this separate legacy lifecycle issue left the full release gate red.

No push, PR, merge, running-server restart, or local Pi configuration change was
performed as part of this implementation.

## Clock-fix follow-up

The native lifecycle library, supervisor, and anchor now share
`CPL_DEADLINE_CLOCK = CLOCK_UPTIME_RAW`, matching Python's macOS monotonic clock.
[Apple documents that uptime clock as equivalent to `mach_absolute_time()`](https://developer.apple.com/documentation/kernel/1462446-mach_absolute_time).
Timeout durations and expiry guards are unchanged; only the clock domain is
corrected. Rebuild the complete native set with `make native`; do not replace
individual binaries/libraries inside a running legacy lifecycle allocation.

Three regressions compile the actual native components against test-only clocks
with a simulated ten-minute sleep offset. They failed before the fix and pass
afterward, checking future deadline admission, deadline generation, and expiry.
The formerly failing real journal operation also passes. A timing-sensitive
reconciliation test now waits for its terminal condition and always performs
exact-key teardown; production reconciliation safeguards were not changed.
An independent reviewer approved both changes.

Final `make release-offline` passed on a clean snapshot of the staged project:
676 unit + 222 macOS lifecycle + 570 gateway + 20 integration tests = **1,488
passed**, followed by Ruff and mypy. The ordinary working checkout passes the
same tests but its whole-directory lint step also includes the unrelated,
untracked `meridian-head-to-head/benchmark.py` (48 existing lint findings). Those
research files were neither modified nor included in the snapshot or commit.
