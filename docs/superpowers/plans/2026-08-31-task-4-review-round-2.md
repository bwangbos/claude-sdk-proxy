# Task 4 Review Round 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every round-2 authority, recovery-capacity, race, retry, deletion, and ABI gap without weakening the round-1 native trust boundary.

**Architecture:** Empty-partial recovery derives the only legal workdir and dependent-artifact namespace from a validated `*.journal` component; complete journals continue to bind the same deterministic name in `INTENT`. The native automaton requires a certified workdir before preparation, carries a nonzero takeover authority epoch through a fixed eight-record recovery lineage, validates every descriptor-bearing record during replay, and exposes semantic recovery entry points instead of caller-authored tail writes. Python protects each native pointer with concurrent call leases and makes close/delete exclusive, while fault builds alone add deterministic barriers and one-shot syscall failures.

**Tech Stack:** Apple clang C17, Darwin descriptor-relative filesystem APIs and BSD `flock`, pthread at-fork handlers, Python 3.14 `ctypes`/threading, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-08-29-claude-subscription-api-proxy-design.md` section 12; `.superpowers/sdd/2026-08-29-phase-0-feasibility/task-4-review-round-1.md`.

## Global Constraints

- Preserve the round-1 native-only workdir, process, batch, reap, and deletion authority.
- A no-`INTENT` caller alias is never evidence; scan the complete deterministic dependent-artifact prefix before minting partial-delete authority.
- Reserve exactly eight maximum-size physical records for the worst-case recovery sequence: retire admitted batch, replace expired recovery authority, record interrupted outcome, prepare successor, activate successor, admit cleanup batch, complete cleanup batch, and append `DONE`.
- Recheck monotonic deadlines immediately before irreversible syscalls and authority-bearing appends.
- Keep process identity fallbacks, corruption controls, barriers, and forced syscall failures fault-build-only; production identity remains fail closed.
- Do not perform live Claude calls, access credentials, persist caller content, or log caller-controlled values.
- Run all pytest evidence with `--strict-markers --forbid-skips -W error`.

---

### Task 1: Empty-partial namespace and bootstrap prerequisites

**Files:**
- Modify: `native/lifecycle.c`
- Modify: `src/claude_sdk_proxy/journal.py`
- Test: `tests/unit/test_journal.py`
- Test: `tests/unit/test_lifecycle.py`
- Test: `tests/darwin/test_journal_lifecycle_crashes.py`

**Interfaces:**
- Consumes: validated `*.journal` component, retained workdir-parent FD, canonical `INTENT`/`WORKDIR_BOUND` records.
- Produces: deterministic `*.workdir`, complete sibling-prefix absence scan, and binding-gated preparation/activation.

- [x] Add failing tests that create an empty partial plus the real `allocation.workdir`, reopen with `absent-alias`, and prove neither certification nor deletion succeeds; add another sibling in the `allocation.*` dependent namespace and prove it also blocks.
- [x] Add failing transition tests proving unbound `PREPARED`/`ACTIVE_READY` and preparation after `NO_DEPENDENT_ARTIFACT` are rejected.
- [x] Add a failing workdir-swap barrier test proving the name must resolve to the opened inode immediately before `WORKDIR_BOUND` append.
- [x] Run the focused tests and record the exact expected failures.
- [x] Derive `allocation.workdir` from `allocation.journal` in native create/open, require caller agreement, scan every non-journal/non-lock `allocation.*` sibling twice under destructive-action authority, and revalidate the workdir name/inode just before append.
- [x] Enforce `workdir_bound == 1`, nonzero bound identity, and `no_dependent_artifact == 0` in native preparation/activation transitions; keep absence terminal for bootstrap.
- [x] Run the Task 1 tests until green.

### Task 2: Exact recovery-tail authority and replay validation

**Files:**
- Modify: `native/lifecycle.h`
- Modify: `native/lifecycle.c`
- Modify: `src/claude_sdk_proxy/journal.py`
- Modify: `src/claude_sdk_proxy/lifecycle.py`
- Test: `tests/unit/test_journal.py`
- Test: `tests/unit/test_lifecycle.py`
- Test: `tests/darwin/test_journal_races.py`

**Interfaces:**
- Consumes: an expired prepared/active/admitted generation and native reap proof.
- Produces: eight-record byte reserve, recovery-mode authority epoch, native replacement/unconfirmed APIs, and replay-safe descriptor validation.

- [x] Add failing limit tests using literal record bytes (`108 + 1064`) for seven-record and eight-record tails, and prove `hard_limit > INT64_MAX` leaves no journal entry.
- [x] Add a failing end-to-end exhaustion test that fills the normal region, then performs the exact eight native transitions through `DONE` without caller-authored recovery append.
- [x] Add failing corruption tests for `descriptor_count > 4`, invalid descriptor order/requirements, and nonzero trailing descriptors; assert the forged physical record is stale and the prior head remains canonical.
- [x] Run the focused tests and record their RED output.
- [x] Encode `CPL_RECOVERY_RECORD_COUNT == 8` and `CPL_RECOVERY_BYTES`, reject insufficient/overflowing limits before `openat`, preserve takeover authority epoch through successor activation/batches, and select recovery class only in native semantic APIs for that lineage.
- [x] Add native authority-replacement and fixed-reason unconfirmed entry points; reject direct caller-authored recovery transitions.
- [x] Move descriptor/precondition shape checks into `cpl_lifecycle_apply` so scanner replay and semantic admission share the same validation.
- [x] Run the Task 2 tests until green.

### Task 3: Deadline, lease, and concurrency fencing

**Files:**
- Modify: `native/lifecycle.h`
- Modify: `native/lifecycle.c`
- Modify: `src/claude_sdk_proxy/journal.py`
- Test: `tests/darwin/test_journal_races.py`
- Test: `tests/darwin/test_flock.py`

**Interfaces:**
- Consumes: action-lock state, absolute lease/deadline, fault-only pipe barriers.
- Produces: immediate pre-syscall/pre-append rechecks and deterministic admission/retirement ordering evidence.

- [x] Add failing pre-expiry `BATCH_ACTIVE` retirement coverage and fault-barrier rows that release a retirement attempt just before versus just after lease expiry.
- [x] Add two barrier-controlled race tests: admission appends first and retirement preserves its exact batch; retirement appends first and later admission is rejected.
- [x] Add a fork test where an owner holding append authority dies while its inherited-FD child remains alive, then prove another process acquires and appends before that child exits.
- [x] Run these tests against the round-1 library and capture RED.
- [x] Add fault-only per-handle pause points around batch-admission and retirement CAS boundaries; recheck lease and operation deadlines immediately before retirement append, workdir unlink, journal unlink, and every parent sync.
- [x] Require lease expiry for both `ACTIVE_READY` and `BATCH_ACTIVE` retirement and preserve the exact admitted batch if admission wins.
- [x] Run the Task 3 tests until green.

### Task 4: Handle lifetime, retry reconciliation, and delete crash prefixes

**Files:**
- Modify: `native/lifecycle.h`
- Modify: `native/lifecycle.c`
- Modify: `src/claude_sdk_proxy/journal.py`
- Test: `tests/unit/test_journal.py`
- Test: `tests/darwin/test_journal_lifecycle_crashes.py`

**Interfaces:**
- Consumes: one owning `Journal`, admitted action token, retained parent, certified `DONE`/reap authority.
- Produces: close-safe call leases, resumable completed steps, fresh post-unlink parent sync, and every `CertifiedDone` crash prefix.

- [x] Add a subprocess regression proving close blocks while a real ctypes operation is paused and the operation completes safely after release.
- [x] Add a forced at-fork-registration-failure construction test and assert no handle is returned.
- [x] Add batch tests where waitpid has completed before a later failure and where workdir unlink succeeds but its parent sync fails once; retry must skip reaping and freshly sync absence before proof advances.
- [x] Restore `CertifiedDone` delete crash tests at `after_authority_revalidated`, `after_process_absence_verified`, `after_workdir_absence_verified`, `after_journal_unlinkat`, and `after_journal_unlink_parent_fsync`; add explicit live-executor and missing-reap denial.
- [x] Run these tests and capture RED before production changes.
- [x] Add concurrent operation leases and exclusive close/delete in Python so no ctypes call can observe freed native storage; propagate the stored `pthread_atfork` result from construction.
- [x] Skip already-completed descriptors on retry, reconcile an already-absent bound workdir with a fresh parent sync, and perform fresh exact process-absence validation before `CertifiedDone` unlink.
- [x] Run the Task 4 tests until green.

### Task 5: ABI parity, full verification, report, and commit

**Files:**
- Modify: `native/lifecycle.h`
- Modify: `native/lifecycle.c`
- Modify: `src/claude_sdk_proxy/lifecycle.py`
- Modify: `src/claude_sdk_proxy/journal.py`
- Modify: `.superpowers/sdd/2026-08-29-phase-0-feasibility/task-4-report.md`

**Interfaces:**
- Consumes: all mirrored C/Python structs and round-2 implementation/test artifacts.
- Produces: compile-time and import-time ABI size parity, complete evidence, and one clean commit.

- [x] Add named size constants and C `_Static_assert` plus Python `ctypes.sizeof` assertions for identity, descriptor, state, record, create/workdir/delete receipts, deletion authority, append result, action token, reap proof, chain, and certified head.
- [x] Run the exact focused GREEN command and record counts/timing.
- [x] Run `HOME=/private/tmp make check`, host-permission `HOME=/private/tmp make darwin`, standalone Ruff/mypy, strict production/fault clang compiles, Apple clang analyzer, ABI/export scans, content/output scan, and `git diff --check`.
- [x] Re-read every Critical/Important/Minor finding against the final diff and record any concern instead of weakening a test.
- [x] Append `## Review round 2` evidence to the Task 4 report.
- [x] Commit with a round-2 repair subject, verify the SHA and clean worktree, and report exact results.
