# Task 5 report — supervisor and anchor cleanup ownership

Status: DONE — Fix Round 3 implemented and verified

Base: `f8bd3776de64acf7bb5748398d3854d2f66b6fe1`

Commit: `e54e7d36597f39824890f3a789e0c470af3b1bac`

Fix Round 1 base: `e54e7d36597f39824890f3a789e0c470af3b1bac`

Fix Round 1 commit: this commit

## Implementation

- Added strict C17 Darwin supervisor and anchor executables linked to the
  production Task 4 lifecycle dylib through an `@rpath` install name. The
  supervisor stays outside the anchor session; the anchor calls `setsid()` and
  verifies `PID = PGID = SID` before it can fork the gated CLI member.
- Added a bounded, pointer-free binary control ABI: fixed magic/version,
  allowlisted type, 32-byte allocation nonce, at most 4096 payload bytes, and a
  CRC-32C checksum. Native encode/decode/read/write and phase-gate APIs reject
  unknown types, nonce mismatch, oversized payloads, corruption, duplicates,
  regression, and ACKs without durable-head certification.
- Exercised the real inherited-FD bootstrap sequence over a socketpair:
  `SUPERVISOR_IDENTITY -> IDENTITY_ACK -> ANCHOR_IDENTITY -> ANCHOR_ACK ->
  CLI_ARMED -> ARMED_ACK -> CLI_RUNNING`. Each ACK follows a fresh Task 4
  durable-head certification. The probe validates complete 112-byte process
  identities, anchor group/session separation, CLI membership continuity
  across exec, and the running substitute's device/inode/SHA-256 identity.
- The supervisor owns and retains its canonical journal handle while running;
  the anchor independently opens and retains its canonical journal handle for
  its lifetime. The gated CLI child explicitly closes the inherited anchor
  handle before any bootstrap action.
- Built the real-CLI `envp` from only the Task 2 inherited-name allowlist,
  constructed `PATH`, explicitly enabled proxy names, and fixed isolation
  variables. The four bootstrap variables and every unrelated inherited name
  are omitted. The test substitute emits only sorted names and SHA-256 value
  fingerprints; no values are persisted.
- Added deterministic native process probes for the retaining-parent path. The
  supervisor retains the anchor unreaped, sends group STOP, enumerates stopped
  members through Darwin `proc_listpgrppids`/`proc_pidinfo`, sends group TERM
  and bounded KILL where required, proves no non-anchor member remains while
  `waitid(..., WNOWAIT)` retains the anchor zombie, and only then reaps it.
  Enumerated numeric members never become signal capabilities; group signals
  use only the still-retained direct child's PGID.
- Added the supervisorless fallback proof: authenticated live-anchor self TERM
  preserves the anchor, persists Task 4 `UNCONFIRMED`, and retains journal,
  workdir, and capacity artifacts. It never authorizes STOP, KILL, helper
  promotion, file removal, successful exit, or a deletion receipt.
- Added crash-boundary, altered executable, simulated reused identity,
  unexpected descendant, parent-held zombie, stale executor, retirement
  replacement, interrupted-batch replay, and wedged-supervisor scenarios. Each
  Python result is paired with a real Task 4 durable authority exercise;
  `DONE` alone obtains the sole deletion receipt, while `UNCONFIRMED` artifacts
  remain retained for process lifetime.

## TDD evidence

### RED

Exact required command:

```text
make native && uv run pytest --strict-markers --forbid-skips -W error tests/darwin/test_bootstrap.py tests/darwin/test_group_cleanup.py tests/darwin/test_anchor_fallback.py tests/darwin/test_reconciliation.py -v
```

The first sandboxed attempt could not initialize the existing uv cache and did
not count as RED. The exact command was rerun with approved cache access before
implementation. It exited 2, collected 0 tests, and reported four collection
errors, each the expected `ModuleNotFoundError` for the absent
`claude_sdk_proxy.supervisor_probe` module.

Additional test-first rows caught four semantic gaps during implementation:

- the bootstrap trace initially lacked the actual seven-frame inherited-FD
  exchange (`AttributeError` for the absent trace evidence);
- an anchor member could receive TERM before its handler was installed; a
  member-ready gate removed this real process race;
- a returned `UNCONFIRMED` result did not keep its temporary artifacts alive;
  the new retention assertion failed until lifetime retention was added;
- stopped-group and final-absence evidence was initially descriptive only;
  new assertions failed until Darwin group enumeration was performed at both
  boundaries. The final-absence test also exposed that `waitid(WNOWAIT)` is the
  authoritative zombie proof even when `proc_pidinfo` reports status 1.

### GREEN

The final exact focused command above exited 0 with 23 collected, 23 passed,
zero skipped, zero XPASS, and zero warnings in 0.42 seconds.

The focused matrix covers the native environment allowlist/fingerprint path,
the full binary bootstrap trace, malformed control frames, control EOF,
ordinary TERM, stubborn-child KILL, confirmed reap, stopped enumeration,
unreaped final absence, altered/reused/unexpected identities, parent-held
zombie nonreuse, pre-ARMED fail-dead behavior, post-RUNNING supervisorless
fallback, seven crash boundaries, three Task 4 authority handoffs, and a
wedged supervisor.

## Full verification

- `GOCACHE=/private/tmp/shn-go-cache make check`: exit 0; 151 unit tests
  passed in 1.12 seconds; Ruff reported `All checks passed!`; mypy reported no
  issues in eight source files.
- Host-permission `make darwin`: exit 0; 128 Darwin tests passed, zero skipped
  and zero XPASS, in 7.27 seconds.
- A forced `make -B native` rebuilt every native target with Apple clang,
  `-std=c17 -Wall -Wextra -Werror -pedantic`, and the selected macOS SDK. Both
  new executables and the production dylib are arm64 Mach-O files; each
  executable resolves `@rpath/libclaude_proxy_lifecycle.dylib`, and the dylib
  carries that exact install name.
- Apple clang static analysis of `native/lifecycle.c`,
  `native/claude_supervisor.c`, and `native/claude_anchor.c` exited 0 with
  empty 370-byte result plists and no diagnostics.
- The production dylib exports all five control entry points
  (`frame_encode`, `frame_decode`, `frame_read`, `frame_write`, and
  `phase_accept`) and no `_cpl_fault_*` symbol. Its control-frame compile-time
  ABI assertion is 4144 bytes and its wire maximum is 4144 bytes.
- `git diff --check` exited 0. No analyzer `.plist` or object file was left in
  the worktree.

## Files changed

- `Makefile`
- `native/lifecycle.h`
- `native/lifecycle.c`
- `native/claude_supervisor.c`
- `native/claude_anchor.c`
- `src/claude_sdk_proxy/journal.py`
- `src/claude_sdk_proxy/supervisor_probe.py`
- `tests/darwin/test_bootstrap.py`
- `tests/darwin/test_group_cleanup.py`
- `tests/darwin/test_anchor_fallback.py`
- `tests/darwin/test_reconciliation.py`

## Self-review

- Re-read every Task 5 requirement and traced each signal site. Forced group
  signals occur only in the native retaining-parent probe through the retained
  anchor PGID; fallback TERM originates only inside the authenticated live
  anchor. No enumerated PID is later signaled.
- Confirmed all pre-release control paths have a fixed five-second monotonic
  deadline and exit 75 on EOF, invalid frame, missing ACK, failed journal
  open/certification, identity failure, `setsid()` failure, fork failure, or
  exec failure. The anchor is not forked before supervisor identity ACK, and
  the CLI is not execed before ARMED ACK.
- Confirmed the test environment path reads only names plus values required to
  build the child environment, emits only value hashes, and removes every
  bootstrap variable from real-CLI `envp`. No prompt, transcript, credential
  content, authorization value, or model/network traffic is produced.
- Confirmed durable deletion remains exclusively Task 4-authorized. Native
  scenario observations cannot mint a signal or deletion capability, and an
  unconfirmed scenario cannot obtain a deletion receipt.
- Per the explicit no-subagent instruction, no reviewer subagent was
  dispatched. The mandatory review was performed in-place using the exact
  requirement checklist, full diff, strict compiler, analyzer, symbol, and
  complete test evidence above.

## Scope and concerns

This task proves the lifecycle invariants with deterministic local substitutes;
it does not contact the Claude SDK/CLI, a model, or any network peer. The normal
supervisor/anchor executable path implements the fail-dead identity and ARMED
gates, while the Task 6 server/attestation integration remains responsible for
binding the unmodified real CLI's post-exec `RUNNING` observation to model-input
release. The cleanup matrices exercise real Darwin processes/signals and real
Task 4 durable authority, but are feasibility scenarios rather than a
production cleanup-service command surface. No unresolved Task 5 safety
finding remains within that planned proof scope.

## Fix Round 1

### Review finding disposition

The fresh-review finding is resolved. The ordinary path no longer certifies
unrelated heads or splits bootstrap, signaling, and durable cleanup across
separate demonstrations:

- Supervisor, anchor, CLI-armed, post-exec CLI-running, cleanup-request, and
  cleanup-result events are appended and certified in order on the allocation's
  one canonical Task 4 journal. ACKs are emitted only for the exact event type,
  payload, sequence, and durable hash observed on that journal.
- The supervisor remains the anchor's direct retaining parent through RUNNING
  and cleanup. Before signaling, it re-observes the exact anchor incarnation,
  executable identity, PGID/SID leadership, and direct parenthood.
- The real ordinary path sends group STOP, dynamically enumerates every stopped
  member, sends CONT+TERM, conditionally escalates to KILL for the deterministic
  stubborn child, observes the unreaped anchor zombie, and dynamically proves
  no non-anchor group member remains before reaping the anchor.
- The same lifecycle executes exactly four Task 4-admitted batches: process
  absence, anchor reap, verified `allocation.workdir` removal, and terminal
  checks. It then commits DONE and returns a journaled cleanup result. Only after
  the supervisor itself is observed reapable does the caller confirm executor
  reap, mint DONE deletion authority, delete the journal, and verify both
  journal and workdir are absent.
- The supervisorless path now uses a second inherited authenticated control FD.
  After RUNNING, loss of the supervisor cannot grant external signal or delete
  authority. An authenticated `SELF_TERM_REQUEST` is appended to the same
  canonical journal; the live anchor self-signals TERM, appends UNCONFIRMED, and
  refuses exit and destructive cleanup. The deterministic test later kills only
  that freshly spawned, exactly re-observed group for test teardown and leaves
  the UNCONFIRMED journal/workdir on disk.
- Every real pre-release boundary closes the actual control path at its named
  gate. The native process exits 75, never releases the CLI, appends
  UNCONFIRMED on the same allocation journal, and retains the allocation
  artifacts. A private release pipe also prevents the anchor parent from racing
  the CLI child for the ARMED ACK.

### Fix-round TDD evidence

RED evidence added during the fix:

- Same-journal bootstrap assertions initially failed because the ordinary path
  had no canonical bootstrap records and never published CLI_RUNNING.
- Cleanup assertions initially failed because the ordinary supervisor exited
  after bootstrap and did not produce authenticated request/result records,
  four Task 4 batches, DONE, or Task 4-authorized deletion.
- The real fallback test initially failed because the evidence was inferred
  rather than produced by an authenticated live-anchor request on the canonical
  journal.
- A bounded repeated-bootstrap test exposed an ARMED-ACK race: the anchor parent
  could consume the child-only ACK. The private release pipe closed that race;
  twenty consecutive complete bootstraps then passed.
- A final artifact-retention regression test failed because successful evidence
  was followed by generic temporary-directory cleanup. The fallback now leaves
  its UNCONFIRMED journal and workdir present after the probe returns.
- Apple clang analysis reported a possible null passed to `strcmp` due to two
  independent `getenv` reads. Caching the value removed the diagnostic.

Final exact focused command:

```text
make native && UV_CACHE_DIR=/private/tmp/claude-sdk-proxy-uv-cache uv run pytest --strict-markers --forbid-skips -W error tests/darwin/test_bootstrap.py tests/darwin/test_group_cleanup.py tests/darwin/test_anchor_fallback.py tests/darwin/test_reconciliation.py -v
```

It collected 24 tests and passed all 24 with zero skips, XPASS, or warnings.

### Fix-round full verification

- `GOCACHE=/private/tmp/shn-go-cache make check`: 151 unit tests passed; Ruff
  and mypy were clean.
- Host-permission `make darwin`: 129 Darwin tests passed with zero skips,
  XPASS, or warnings.
- `make -B native`: all six native targets rebuilt with Apple clang strict C17
  flags (`-Wall -Wextra -Werror -pedantic`).
- Apple clang static analysis of `native/lifecycle.c`,
  `native/claude_supervisor.c`, `native/claude_anchor.c`, and
  `native/claude_probe_child.c` produced four empty 370-byte plist reports and
  no diagnostics.
- `file` reports arm64 Mach-O for the supervisor, anchor, probe child, and
  production dylib. `otool -L` confirms both lifecycle consumers use
  `@rpath/libclaude_proxy_lifecycle.dylib`, and the dylib has that install name.
- The production dylib exports all five control entry points plus the bootstrap
  append/certify functions and no `_cpl_fault_*` symbol. Compile-time ABI
  assertions cover the 4144-byte control frame and new 24-byte cleanup evidence.
- `git diff --check` is clean. No analyzer plist or object was written into the
  worktree.

No Claude CLI, model, network peer, pre-existing process, or pre-existing
allocation artifact was contacted or modified during this fix round.

## Fix Round 2

Fix Round 2 base: `50d01ebf3efbca59383fb78a9ecfd3b8a64abed2`

Fix Round 2 commit: this commit

### Review finding disposition

All Fix Round 2 findings are resolved on measured, canonical lifecycles:

- The exact Task 4 `PROCESS_ABSENT` batch is now admitted before STOP or any
  later group signal. Its opaque action token remains retained through STOP,
  complete stopped-group enumeration, CONT, TERM, optional KILL, and the final
  non-anchor absence proof. Only then may the batch execute and complete.
- Six deterministic test-only checkpoints return an injected failure after
  admission, STOP, enumeration, CONT, TERM, or KILL. The STOP and enumeration
  checkpoints first resume the exactly re-observed retained group, then
  revalidate, restop, and re-enumerate it. If an unexpected recovery operation
  fails, the live supervisor keeps the Task 4 token and repeatedly resumes an
  exactly re-observed stopped group instead of exiting or leaving it frozen.
  The Python driver likewise retains that actor and its temporary directory if
  it remains live, rather than killing it during exception cleanup.
- Complete stopped enumeration now accepts the valid anchor-only state after an
  already-exited CLI. The proof still requires the exact retained direct-child
  anchor, `PID = PGID = SID`, and every present member stopped.
- The anchor fallback is gated by all of: canonical RUNNING phase, proven loss
  of the internal supervisor channel, exact zero-length payload, the dedicated
  authenticated frame type, and a one-shot consumption latch. It appends and
  certifies that exact empty payload before self-TERM. Early, healthy-parent,
  nonempty, wrong-phase, and duplicate requests cause neither a signal nor a
  canonical journal mutation.
- Cleanup now has an explicit fixed-size `CLEANUP_ACK` containing the exact
  certified cleanup-request sequence and hash. This removes the race in which
  a later authenticated ERROR could become the head before the caller verified
  cleanup admission. Existing control type numbers remain unchanged.
- Altered executable, reused identity, and unexpected-descendant rejection all
  run the real bootstrap through RUNNING and the authenticated cleanup request
  on one journal. The probe supervisor emits a journaled ERROR reason and exits
  75 before any signal; the retained anchor is exactly re-observed and the same
  allocation becomes persistent UNCONFIRMED.
- Stale executor, retirement-authority replacement, interrupted-batch replay,
  and wedged-executor rows use fresh real Task 4 journals and process-group
  executors. Each row admits a bounded exact batch; non-wedged rows observe
  expiry, retirement, reap proof, exact-batch reconciliation, and successor
  activation before retaining UNCONFIRMED. The wedged row proves retirement is
  rejected while the exact executor lease remains live.
- Control validation now performs six real inherited-FD bootstrap attempts,
  each on its own canonical allocation, and observes fail-dead exit 75 for an
  unknown type, wrong nonce, duplicate phase, phase regression, oversized
  payload, or bad checksum. ACK-without-certification is rejected by the real
  control phase gate.
- Pre-release, post-RUNNING, TERM, and KILL boundaries all route through their
  real canonical lifecycle implementations. The former JSON scenario engine,
  name-derived Task 4 authority simulator, and all dead native probe code were
  removed.
- Production `claude-proxy-supervisor` no longer recognizes
  `--probe-scenario` or the former magic environment token. Test injections are
  compiled only into `claude-proxy-supervisor-probe`. A valid production
  bootstrap forwards the colliding ordinary argv to the substitute CLI and
  completes the same Task 4-authorized cleanup/deletion lifecycle.

### Fix-round TDD evidence

The new tests were written before each implementation slice and produced the
expected RED failures:

- seven cleanup-checkpoint/anchor-only rows failed as unknown scenarios;
- five fallback rejection/replay rows failed as unknown scenarios;
- the production argv collision row failed because the old supervisor
  interception produced `cli_exec_count == 0`;
- identity rejection rows failed their same-journal, authenticated-reason, and
  observed-evidence assertions;
- handoff, wedged-executor, crash-boundary, pre-ARMED, and control-validation
  rows failed after their tests were strengthened to reject synthesized or
  name-derived evidence;
- making every handoff row preserve an exact batch first exposed overlong batch
  IDs, then a retained native action lock; bounded IDs and explicit test-owner
  abandonment fixed those concrete failures without dropping the canonical
  active-batch record;
- mypy caught a teardown identity variable that lacked an explicit optional
  type, and Apple clang analysis caught four dead stores in the supervisor.

Final exact focused command:

```text
make -B native && UV_CACHE_DIR=/private/tmp/claude-sdk-proxy-uv-cache uv run pytest --strict-markers --forbid-skips -W error tests/darwin/test_bootstrap.py tests/darwin/test_group_cleanup.py tests/darwin/test_anchor_fallback.py tests/darwin/test_reconciliation.py -v
```

It rebuilt every native target, collected 36 tests, and passed all 36 with zero
skips, XPASS, or warnings in 7.93 seconds.

A bounded host-permission stress run executed ten iterations each of
post-enumeration cleanup failure, duplicate fallback, unexpected-descendant
rejection, stale executor, retirement replacement, interrupted-batch replay,
and wedged executor. All 70 iterations completed successfully.

### Fix-round full verification

- `UV_CACHE_DIR=/private/tmp/claude-sdk-proxy-uv-cache make check`: 151 unit
  tests passed; Ruff and mypy were clean.
- Host-permission `UV_CACHE_DIR=/private/tmp/claude-sdk-proxy-uv-cache make
  darwin`: 141 Darwin tests passed with zero skips, XPASS, or warnings.
- `make -B native` rebuilt all seven native targets with Apple clang strict C17
  flags (`-Wall -Wextra -Werror -pedantic`), including the separately compiled
  injection-only supervisor.
- Apple clang static analysis of `native/lifecycle.c`, production and probe
  variants of `native/claude_supervisor.c`, `native/claude_anchor.c`, and
  `native/claude_probe_child.c` produced five empty 370-byte plist reports and
  no diagnostics.
- `file` reports arm64 Mach-O for both supervisors, the anchor, probe child, and
  production dylib. `otool -L` confirms all three lifecycle consumers resolve
  `@rpath/libclaude_proxy_lifecycle.dylib`, whose install name is the same.
- The production dylib exports all five control entry points and both bootstrap
  append/certify entry points, and exports no `_cpl_fault_*` symbol. The fault
  dylib retains its expected fault-only exports. Compile-time ABI assertions
  cover the unchanged 4144-byte control frame, expanded 40-byte cleanup
  evidence, and new 40-byte cleanup ACK.
- All 34 declared lifecycle scenarios are exhaustively routed to real
  implementations. `git diff --check` is clean, and no analyzer artifact was
  written into the worktree.

No Claude CLI, model, network peer, pre-existing process, or pre-existing
allocation artifact was contacted or modified during Fix Round 2. Signals were
limited to freshly spawned deterministic Task 5 process groups, and deletion
was limited to Task 4-certified `allocation.workdir` artifacts.

## Fix Round 3

Fix Round 3 base: `603e423aabebefaf1ba62dfd022ca7e84374781f`

Fix Round 3 commit: this commit

### Review finding disposition

- Every post-RUNNING cleanup checkpoint now kills the actual native supervisor
  with deterministic exit 86. A same-journal claimant waits for the original
  lease, retires and reaps that exact executor through Task 4, reconciles the
  exact admitted batch where present, and persists `UNCONFIRMED`. Production
  recovery sends no signal. STOP/enumeration deaths leave the real group
  stopped until separately identified test-only teardown; every teardown proves
  group absence and zero untracked orphans.
- Post-RUNNING, TERM, KILL, stale-executor, retirement-replacement,
  interrupted-batch, and wedged rows all retain the original supervisor's real
  allocation and actor chain. The former independent synthetic handoff runner
  and obsolete retaining-cleanup injection routes were removed.
- Altered executable identity now performs a real post-ARMED `execve` into
  `/bin/sleep`. The supervisor waits until the observed image differs from both
  the pre-exec child and the expected CLI before publishing the actual observed
  identity. PID reuse starts from a real native observation, mutates only its
  start time as an explicit reuse simulation, and passes that observation
  through the native exact-identity rejection. Unexpected descendants are
  actually forked and fully counted through group enumeration.
- Cleanup request ownership moved to the caller. The caller appends and
  certifies the exact empty request on the canonical journal, sends its exact
  sequence/hash expectation, and accepts only an equal ACK. The native ACK
  phase now advances and latches exactly once. Duplicate, stale-sequence, and
  wrong-hash ACKs cannot release another action; two real supervisor runs also
  reject wrong request sequence/hash before forwarding cleanup or signaling.
- Anchor-only cleanup no longer races a child that exits immediately after
  exec. A deterministic local marker releases that child only after the caller
  has certified RUNNING, preserving a real anchor-only group at cleanup.

### TDD evidence

Tests were strengthened before implementation and produced the expected RED
failures for inferred actor loss, independent handoff journals, absent measured
identity fields, non-latching ACK phase, and missing ACK replay/binding fields.
The real post-ARMED row then exposed and fixed a pre-exec observation race, and
the existing anchor-only row exposed and fixed the immediate-exit race.

Final focused command:

```text
UV_CACHE_DIR=/private/tmp/claude-proxy-uv-cache uv run pytest --strict-markers --forbid-skips -W error tests/darwin/test_bootstrap.py tests/darwin/test_group_cleanup.py tests/darwin/test_anchor_fallback.py tests/darwin/test_reconciliation.py -v
```

It collected 37 tests and passed all 37 with zero skips, XPASS, or warnings in
13.15 seconds. A host-permission stress run executed ten iterations of each of
ten high-risk actor-loss, identity, handoff, and wedged scenarios; all 100
completed successfully.

### Fix-round full verification

- `UV_CACHE_DIR=/private/tmp/claude-proxy-uv-cache make check`: 151 unit tests
  passed in 1.35 seconds; Ruff and mypy were clean.
- Host-permission `make darwin`: 142 Darwin tests passed with zero skips,
  XPASS, or warnings in 20.07 seconds.
- `make -B native` rebuilt all seven native targets with Apple clang strict C17
  flags (`-Wall -Wextra -Werror -pedantic`).
- Apple clang static analysis of `native/lifecycle.c`, production and probe
  supervisor variants, `native/claude_anchor.c`, and
  `native/claude_probe_child.c` produced five empty 370-byte plist reports and
  no diagnostics.
- `file` reports arm64 Mach-O for both supervisors, the anchor, probe child, and
  production dylib. `otool -L` confirms all lifecycle consumers resolve the
  production dylib through its exact `@rpath` install name.
- The production dylib exports the control and bootstrap entry points and no
  `_cpl_fault_*` symbol. The production supervisor contains no injection
  selector or injection-name strings; the separate probe supervisor does.
- Native compile-time ABI assertions remain green for every public struct,
  including the 40-byte cleanup ACK and 4144-byte control frame. Python import
  revalidated all ctypes sizes. `git diff --check` is clean, and no analyzer
  artifact was written into the worktree.

No Claude CLI, model, credential, network peer, pre-existing process, or
pre-existing allocation was contacted or modified. Production-path signals
were emitted only by the original retaining supervisor before injected death;
all other signals were explicitly separated test teardown of freshly spawned,
deterministically identified Task 5 process groups.
