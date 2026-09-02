# Task 4 Review Round 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining round-3 namespace-scan, bounded-recovery, deadline-admission, handle-close, and scanner-shape gaps without weakening the native authority boundary.

**Architecture:** Every dependent-namespace pass opens a new directory open-file description through the retained verified parent and validates its identity before enumeration, so scans never share a directory cursor. The eight-record recovery reserve allows one initial authority epoch and exactly one replacement before the fixed interrupted-batch-to-`DONE` tail. Fault builds expose one-shot barriers at final native mutation boundaries; after each barrier the implementation rechecks the operation deadline and any claim, lease, or authority deadline immediately before the syscall or append. Python owns the native pointer through an `OPEN`/`CLOSING`/`CLOSED` condition-protected state machine with one elected native closer and broadcast completion.

**Tech Stack:** Apple clang C17, Darwin descriptor-relative filesystem APIs and BSD `flock`, pthread synchronization, Python 3.14 `ctypes`/threading, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-08-29-claude-subscription-api-proxy-design.md` section 12; `.superpowers/sdd/2026-08-29-phase-0-feasibility/task-4-review-round-1.md`; round-3 Sol review supplied by the coordinator.

## Global Constraints

- Preserve all round-1 and round-2 native-only workdir, process, batch, reap, and deletion authority.
- Open a fresh `O_DIRECTORY|O_CLOEXEC` description for every dependent-namespace enumeration, anchored at and identity-equal to the retained verified workdir parent.
- Keep the maximum recovery tail equal to eight physical records by permitting initial authority epoch 1 and at most one replacement to epoch 2.
- Fence every reviewed mutation at its last native admission boundary, after any fault barrier and immediately before the syscall or authoritative append.
- Keep deterministic barriers and create-deadline overrides fault-build-only; production exports and behavior remain free of test controls.
- Do not perform live Claude calls, access credentials, persist caller content, or log caller-controlled values.
- Run all pytest evidence with `--strict-markers --forbid-skips -W error`.

---

### Task 1: Fresh dependent-namespace scans

**Files:**
- Modify: `native/lifecycle.h`
- Modify: `native/lifecycle.c`
- Modify: `src/claude_sdk_proxy/journal.py`
- Test: `tests/darwin/test_journal_lifecycle_crashes.py`

- [x] Add a barrier test that inserts `allocation.*` after the first absence scan and proves no partial-delete authority is minted.
- [x] Run the test against round 2 and record the authority-minting RED failure.
- [x] Replace `dup(parent_dirfd)` with a fresh descriptor-relative directory open for each pass and validate the fresh FD against the journal's retained parent identity before enumeration.
- [x] Add the fault-only post-first-scan pause and run the focused test green.

### Task 2: Finite replacement epochs and scanner shape

**Files:**
- Modify: `native/lifecycle.h`
- Modify: `native/lifecycle.c`
- Test: `tests/unit/test_journal.py`
- Test: `tests/unit/test_lifecycle.py`
- Test: `tests/darwin/test_journal_lifecycle_crashes.py`

- [x] Extend the exact-tail test to accept epoch 1 then epoch 2, reject epoch 3 without consuming bytes, and still use the remaining terminal capacity through `DONE`; separately reject a non-initial retirement epoch.
- [x] Add a fault-encoded record whose declared descriptor count is smaller than a nonzero trailing descriptor and prove replay treats it as stale/noncanonical.
- [x] Run the focused rows and record the unbounded-replacement RED failures (the descriptor row may characterize already-correct replay validation).
- [x] Define and document initial/max authority epochs and one maximum replacement; enforce both semantic APIs and replay transitions natively.
- [x] Run the focused rows green.

### Task 3: Final deadline admission boundaries

**Files:**
- Modify: `native/lifecycle.h`
- Modify: `native/lifecycle.c`
- Modify: `src/claude_sdk_proxy/journal.py`
- Test: `tests/darwin/test_journal_races.py`
- Test: `tests/darwin/test_journal_lifecycle_crashes.py`

- [x] Add fault-barrier rows for expiry immediately before create `openat`, the preallocation operation, intent write, journal `F_FULLFSYNC`, and parent `fsync`; assert no mutation beyond the already-admitted prefix and no receipt/handle.
- [x] Add barrier rows for prepared-claim and requested-lease expiry before activation append, successor-claim expiry before successor append, new retirement-authority expiry before retirement append, and new replacement-authority expiry before replacement append; assert no append.
- [x] Run the focused rows against round 2 and capture the exact RED failures.
- [x] Capture the create deadline before its first mutation, add fault-only create boundary configuration, and check the deadline after the barrier immediately before every reviewed create syscall.
- [x] Add lifecycle barriers and re-scan/revalidate current authority plus both operation and embedded deadlines at the final serialized append admission.
- [x] Run all deadline rows green.

### Task 4: Idempotent concurrent close

**Files:**
- Modify: `src/claude_sdk_proxy/journal.py`
- Test: `tests/unit/test_journal.py`

- [x] Add a regression with one in-flight native operation and two concurrent closers; use bounded joins and prove both closers return.
- [x] Run the row against round 2 and record its characterization result.
- [x] Replace `_closing` with explicit `OPEN`/`CLOSING`/`CLOSED` state, elect exactly one native closer, drain operation leases, publish `CLOSED`, and `notify_all()` after native close; integrate exclusive delete success/failure with the same state machine.
- [x] Run the close/delete focused rows green.

### Task 5: Verification, report, and commit

**Files:**
- Modify: `.superpowers/sdd/2026-08-29-phase-0-feasibility/task-4-report.md`

- [x] Run the complete focused Task 4 matrix and record count/timing.
- [x] Run `HOME=/private/tmp make check`, host-permission `HOME=/private/tmp make darwin`, strict production/fault clang compiles, Apple clang analyzer, ABI/export scans, content/output scan, and `git diff --check`.
- [x] Confirm production exports contain no fault hooks and C/Python ABI sizes remain equal.
- [x] Re-read every Critical/Important/Minor round-3 item against the final diff and record any concern.
- [x] Append `## Review round 3` evidence to the Task 4 report.
- [x] Commit with a round-3 repair subject, verify the SHA and clean tracked worktree, and report exact results.
