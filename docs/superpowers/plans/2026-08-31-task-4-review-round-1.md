# Task 4 Review Round 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair every Task 4 review finding so native code alone issues and validates durable workdir, process, batch, action-lock, and deletion authority.

**Architecture:** Extend the fixed pointer-free journal payload with durable limits, intended/bound workdir identity, exact executor identity, and bounded syscall descriptors. Expose typed orchestration APIs whose native implementations acquire the canonical lock domains, perform real descriptor-relative or retained-parent checks, append and certify their own records, and return opaque receipts/tokens. Register live handles for child-side `pthread_atfork` closure and keep all corruption and pause operations behind the fault-build macro.

**Tech Stack:** Apple clang C17, Darwin `flock`/`openat`/`F_FULLFSYNC`/`proc_pidinfo`/`waitpid`, Python 3.14 `ctypes`, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-08-29-claude-subscription-api-proxy-design.md` section 12; controller rulings in the Task 4 review-round assignment.

## Global Constraints

- Personal, same-user, loopback-only existing-login use; never implement login, read credential contents, or accept credential material.
- Darwin/macOS 14+ on a verified local APFS runtime root only.
- Compile with Apple clang `-std=c17 -Wall -Wextra -Werror -pedantic`.
- Persist no prompt, transcript, response, tool payload, SDK transport bytes, proxy credentials, or Claude credentials.
- Every evidence pytest invocation uses `--strict-markers --forbid-skips -W error`.
- Do not perform live Claude calls in this repair.

---

### Task 1: Durable journal metadata and fail-closed scanning

**Files:**
- Modify: `native/lifecycle.h`
- Modify: `native/lifecycle.c`
- Modify: `src/claude_sdk_proxy/lifecycle.py`
- Modify: `src/claude_sdk_proxy/journal.py`
- Test: `tests/unit/test_journal.py`

**Interfaces:**
- Consumes: retained directory FDs and fixed allocation nonce.
- Produces: INTENT-bound limits and intended workdir identity, canonical header/payload equality checks, atomic append results, and locked scans.

- [x] Add tests proving reopen rejects changed limits, insufficient recovery tail is rejected, certified authority fails on unhealthy journals, public scan is lock-serialized, append returns its own exact state/sequence, and header/payload mismatches are noncanonical.
- [x] Run those tests against commit `7c4120a` and record the expected failures.
- [x] Add fixed ABI fields for durable limits and workdir identity; validate exact INTENT agreement on reopen and require the static recovery-tail minimum.
- [x] Lock public scans, return hash/sequence/state atomically from native append, reject unhealthy certification/revalidation, and compare cleanup epoch/type/duplicate parent across header and payload.
- [x] Run the focused tests and confirm they pass.

### Task 2: Native workdir binding and receipt gates

**Files:**
- Modify: `native/lifecycle.h`
- Modify: `native/lifecycle.c`
- Modify: `src/claude_sdk_proxy/journal.py`
- Test: `tests/unit/test_journal.py`
- Test: `tests/darwin/test_journal_lifecycle_crashes.py`

**Interfaces:**
- Consumes: durable create receipt and intended parent/name from INTENT.
- Produces: `Journal.create_workdir() -> WorkdirBoundReceipt`, native no-dependent-artifact certification, certified-binding-only deletion, and controlled native create/cleanup gates.

- [x] Add tests proving a caller-selected absent alias cannot authorize deletion, created workdirs are bound by parent/name/device/inode before a gate can release, complete-INTENT rollback requires a native absence transition, and receipt absence blocks actual marker creation.
- [x] Run those tests against the current production library and record the expected failures.
- [x] Implement descriptor-relative mode-0700 workdir creation/binding and certified absence/removal from the recorded identity only.
- [x] Implement native gate-capability checks so only durable create/bind/delete receipts can create controlled marker actions.
- [x] Run the focused tests and confirm they pass.

### Task 3: Executor identity, admitted batches, and reap proof

**Files:**
- Modify: `native/lifecycle.h`
- Modify: `native/lifecycle.c`
- Modify: `src/claude_sdk_proxy/lifecycle.py`
- Modify: `src/claude_sdk_proxy/journal.py`
- Test: `tests/unit/test_lifecycle.py`
- Test: `tests/darwin/test_journal_races.py`
- Test: `tests/darwin/test_journal_lifecycle_crashes.py`

**Interfaces:**
- Consumes: `PREPARED` candidate, exact live Darwin process, certified workdir binding, and canonical action-lock inode.
- Produces: native-observed `ProcessIdentity`, fixed `BatchDescriptor` arrays, lock-backed `AdmittedBatch`, native-computed completion bits, native DONE, retained-parent reap proof, retirement/replacement/successor APIs.

- [x] Add tests proving zero/incomplete identities never certify, caller-authored proof bits and direct batches are rejected, lease/claim deadlines are enforced, only an exact lock-backed admitted batch advances proof, and retained-parent `waitpid` evidence is required.
- [x] Add concurrent tests for batch-vs-retirement, executor death while holding action lock, interrupted-batch carry-forward, and successor rejection before prior absence.
- [x] Run each regression group against the current library and record its expected failure.
- [x] Implement native process observation, semantic transition entry points, token ownership, bounded descriptor execution, completion certification, retirement fencing, and reap-bound deletion authority.
- [x] Run the focused tests and confirm they pass.

### Task 4: Fork cleanup and fault-build-only corruption

**Files:**
- Modify: `native/lifecycle.h`
- Modify: `native/lifecycle.c`
- Modify: `src/claude_sdk_proxy/journal.py`
- Test: `tests/darwin/test_flock.py`
- Test: `tests/unit/test_journal.py`

**Interfaces:**
- Consumes: every live native journal handle.
- Produces: at-fork registry closure of journal/append/action FDs and fault-only native corruption injection.

- [x] Add a long-lived fork child test that inspects `/dev/fd` and proves none of the three protected inodes remain open, then proves another process acquires the lock after owner death.
- [x] Add a production-dylib test proving raw injection symbols are absent and fault-dylib tests proving native mismatch/torn-byte injection.
- [x] Run both tests against the current library and record the expected failures.
- [x] Register/unregister handles under a global at-fork mutex; close all inherited FDs in the child handler and invalidate inherited handles.
- [x] Delete Python CRC/record encoding and raw `os.write`; expose narrowly scoped native fault hooks under `CPL_ENABLE_FAULT_INJECTION` only.
- [x] Run the focused tests and confirm they pass.

### Task 5: Full verification and review evidence

**Files:**
- Modify: `.superpowers/sdd/2026-08-29-phase-0-feasibility/task-4-report.md`

**Interfaces:**
- Consumes: all repaired native/Python/test artifacts.
- Produces: exact RED/GREEN/full evidence and one review-round repair commit.

- [x] Run the complete focused Task 4 command with `make native` and strict pytest flags.
- [x] Run `make check` and `make darwin`.
- [x] Run strict production/fault dylib compiles, Apple clang static analysis, Ruff, mypy, ABI/ctypes layout parity, symbol-boundary inspection, `git diff --check`, and a secret/content/output scan.
- [x] Re-read every review finding and controller ruling against the diff; record any remaining concern rather than weakening a test.
- [x] Append `Review round 1` evidence to the Task 4 report.
- [x] Commit with a review-round fix subject and verify the final SHA and clean worktree.
