# Task 5 report — supervisor and anchor cleanup ownership

Status: DONE — Fix Round 5 implemented and verified

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

## Fix Round 4

Fix Round 4 base: `6963b6b935d373dd8d5df01cb086f4a352f3c64a`

Fix Round 4 commit: this commit

### Review finding disposition

- Actor-loss and wedged Python exception paths no longer call `poll`, `kill`,
  or `wait` on the original supervisor in their production evidence cleanup.
  Once anchor identity is known, journal, directory, control sockets, stderr,
  `Popen`, and exact anchor identity transfer atomically to an in-memory
  retained-chain owner before stack unwinding can close or reap them.
- The exception owner first appends and certifies durable `UNCONFIRMED` when
  the current Task 4 state permits it. If an injected supervisor has naturally
  become reapable after the exact cleanup request, it instead waits for the
  recorded lease, retires the exact executor, reaps it through Task 4,
  reconciles an interrupted exact batch where present, and only then persists
  `UNCONFIRMED`. A pre-request or wedged live supervisor remains live with all
  control and journal handles retained for a caller/reconciler.
- Normal lifecycle evidence is constructed before any test cleanup. The
  separately named test-only release path re-observes the exact fresh anchor,
  resumes it when stopped, signals only that anchor's fresh group, closes its
  retained control channels, waits for natural supervisor exit or recognizes
  its prior Task 4 reap, and proves both group absence and zero untracked
  orphans. It closes every retained FD exactly once and deliberately leaves the
  durable `UNCONFIRMED` journal and workdir present.
- The former wedged-path `process.poll()/kill()/wait()` sequence was removed.
  The live-executor rejection is now established by the failed Task 4
  retirement plus exact process observation, so observing evidence cannot
  accidentally reap the supervisor.

### TDD evidence

The first RED run failed because no retained-chain inspection/release contract
existed. After the initial retention slice, the 26-row matrix exposed that
post-request exception paths retained a reapable supervisor without completing
available Task 4 recovery. The final GREEN implementation distinguishes these
cases: two pre-request checkpoints retain the live actor, while every later
actor-loss checkpoint proves Task 4 supervisor reap before test teardown.

The deterministic matrix injects a Python exception after every meaningful
top-level checkpoint once anchor identity is known: 19 actor-loss points from
request preparation through ACK, death observation, retirement, exact-batch
reconciliation, successor activation, terminal certification, and evidence
capture; plus seven wedged points from executor observation through terminal
certification and evidence capture. Every row proves certified
`UNCONFIRMED`, exact anchor identity or absence, retained artifacts, live actor
or Task 4 reap, exact test-only group absence, zero orphan count, and no
`ResourceWarning`.

Final focused command:

```text
UV_CACHE_DIR=/private/tmp/claude-proxy-uv-cache uv run pytest --strict-markers --forbid-skips -W error tests/darwin/test_bootstrap.py tests/darwin/test_group_cleanup.py tests/darwin/test_anchor_fallback.py tests/darwin/test_reconciliation.py -v
```

It collected 63 tests and passed all 63 with zero skips, XPASS, or warnings in
18.64 seconds. A final bounded stress run repeated all 26 exception rows three
times; all 78 executions passed.

### Fix-round full verification

- `UV_CACHE_DIR=/private/tmp/claude-proxy-uv-cache make check`: 151 unit tests
  passed; Ruff and strict mypy were clean.
- Host-permission `UV_CACHE_DIR=/private/tmp/claude-proxy-uv-cache make
  darwin`: 168 Darwin tests passed with zero skips, XPASS, or warnings.
- `make -B native` rebuilt all seven native targets with Apple clang strict C17
  flags (`-Wall -Wextra -Werror -pedantic`).
- Apple clang static analysis of `native/lifecycle.c`, production and probe
  supervisor variants, `native/claude_anchor.c`, and
  `native/claude_probe_child.c` produced five empty 370-byte plist reports and
  no diagnostics.
- `file` reports arm64 Mach-O for both supervisors, the anchor, probe child,
  and both lifecycle dylibs. `otool -L` confirms all lifecycle consumers use
  the exact production `@rpath` install name.
- The production dylib exports no `_cpl_fault_*` symbol. The production
  supervisor contains no Task 5 injection selector or injection-name strings;
  the separately compiled probe supervisor contains the expected selectors.
  Existing compile-time and Python ABI assertions remained green.
- `git diff --check` is clean, and no analyzer output was written into the
  worktree.

No Claude CLI, model, credential, network peer, pre-existing process, or
pre-existing allocation was contacted or modified. Signals were confined to
fresh, exactly re-observed Task 5 groups under the user's scoped authorization.
No retained `UNCONFIRMED` artifact was deleted.

## Fix Round 5

Fix Round 5 base: `0f076cfb776615ac927d8fdadc52c50a59ae4cc0`

Fix Round 5 commit: this commit

### Review finding disposition

- Retained ownership now reserves one of 32 explicit cleanup-owner slots before
  temporary-instance creation or process spawn. Once the anchor identity frame
  is accepted, the first action is a lock-serialized promotion of that
  reservation to exactly one owner keyed by allocation nonce plus the complete
  anchor identity. The registry entry is installed before the reservation is
  marked transferred; every later `finally` decision reads that transfer state,
  so no fallible recovery or journal mutation occurs in the ownership gap.
- The prior unbounded newest-owner list is gone. Live and unconfirmed owners are
  held in a fixed-capacity dictionary with no eviction, exact-key inspection,
  bounded idempotent reconciliation, and exact-key release. Capacity exhaustion
  rejects before instance creation or `Popen`; pre-spawn construction failure
  rolls its reservation back. Release removes the key only after every handle
  is closed and complete group/process absence is independently proved.
- Cleanup delivery becomes ambiguous before byte zero. Deterministic boundaries
  cover zero-byte, partial-frame, full-frame, ACK-received, and ACK-accepted
  exceptions. The owner retains the exact certified request sequence/hash and
  received ACK payload. Recovery consults the canonical Task 4 head, exact
  request/ACK binding when it remains the certifiable bootstrap head, and
  current child wait state; the may-have-delivered flag is never an action
  decision input. A test deliberately clears that flag and still converges from
  canonical journal/process evidence. If a live executor has already published
  `BATCH_ACTIVE`, recovery retains that exact admitted prefix rather than
  appending a competing terminal; a later keyed retry converges after actor
  exit.
- Recovery is idempotent on the retained owner. Faults are injected after head
  certification, exit observation, executor retirement, native reap receipt,
  interrupted-batch reconciliation, UNCONFIRMED append, and UNCONFIRMED
  certification. The original exception remains primary while the exact owner,
  token, handles, and opaque native `ReapProof` remain reachable for retry.
- Inspection duplicates the retained directory descriptor, reopens the exact
  journal by nonce, and freshly certifies its canonical head. Task 4 reap proof
  requires the retained opaque native receipt to name the supervisor, a terminal
  certified state, and an independent `waitid` observation that it is no longer
  a child. Test-only release separately reopens the journal, re-observes the
  exact anchor, enumerates the entire group, signals only that fresh group, and
  waits until both enumeration is empty and the group capability is absent.

### TDD evidence

The initial RED reconciliation run failed 49 rows because keyed registry,
inspection, release, and recovery-edge interfaces did not exist. Subsequent RED
rows exposed three additional real gaps: a Task 4-reaped `Popen` wrapper could
still emit `ResourceWarning`; recovery-fault tests initially did not distinguish
the preserved original exception from a deliberately swallowed nested fault;
and a single immediate post-reap process-table snapshot could transiently report
a just-killed group member. The implementation now retains the exact native reap
receipt in the owner, records the wrapper's known terminal state without a
second wait, records nested-fault execution explicitly, and uses a bounded dual
enumeration/capability absence loop. The stopped-group regression passed five
consecutive repetitions.

Final focused command:

```text
UV_CACHE_DIR=/private/tmp/claude-proxy-uv-cache uv run pytest --strict-markers --forbid-skips -W error tests/darwin/test_bootstrap.py tests/darwin/test_group_cleanup.py tests/darwin/test_anchor_fallback.py tests/darwin/test_reconciliation.py -q
```

It collected 81 tests and passed all 81 with zero skips, XPASS, or warnings in
25.59 seconds. The 17-row transfer/write/recovery/release/capacity subset passed
three consecutive repetitions (51 executions), and the stopped-group teardown
row passed five consecutive repetitions.

### Fix-round full verification

- `UV_CACHE_DIR=/private/tmp/claude-proxy-uv-cache GOCACHE=/private/tmp/shn-go-cache
  make check`: 151 unit tests passed; Ruff and strict mypy were clean.
- Host-permission `UV_CACHE_DIR=/private/tmp/claude-proxy-uv-cache make darwin`:
  186 Darwin tests passed with zero skips, XPASS, or warnings.
- `make -B native` rebuilt all seven native targets with Apple clang strict C17
  flags (`-Wall -Wextra -Werror -pedantic`).
- Apple clang static analysis of the production lifecycle library, production
  and injection supervisors, anchor, and probe child produced five empty
  370-byte plist reports and no diagnostics.
- `file` reports arm64 Mach-O for both supervisors, the anchor, probe child, and
  both lifecycle dylibs. `otool -L` confirms every lifecycle consumer resolves
  the exact production `@rpath` install name.
- The production dylib exports all control/bootstrap entry points and no
  `_cpl_fault_*` symbol; the fault dylib exports the fault surface. The
  production supervisor contains no injection selector/stage strings, while
  the separately compiled probe supervisor contains the expected selectors.
- `git diff --check` is clean. No analyzer output or object was written into the
  worktree.

No Claude CLI, model, credential, network peer, pre-existing process, or
pre-existing allocation was contacted or modified. Signals remained confined
to freshly spawned, exactly re-observed Task 5 groups under the user's scoped
authorization. No retained `UNCONFIRMED` artifact was deleted.

## Remediation cycle 1

Remediation base: `5553980a92a9a35d5eed6f9d7f083afc151ce4a7`

Remediation commit: this commit

### Breaker-finding disposition

- Atomic retained ownership is now one fixed registry-entry transition. The
  owner is fully constructed before publication, and the single
  `entry.owner = owner` assignment is the whole `RESERVED -> OWNED` handoff.
  `except` and `finally` query that same entry, including after keyed release;
  they no longer depend on a caller assignment or a separately written
  transferred flag. A trace-injection test raises at every executed line of
  `_transfer` and proves each boundary leaves either a cancellable reservation
  or exactly one reachable keyed owner, never local cleanup after publication.
- `cpl_journal_mark_unconfirmed` now acquires the native action authority,
  freshly certifies the canonical head under that serialization, and rejects
  `BATCH_ACTIVE` and `RETIRING_BATCH` before the append boundary. A stale
  Python recovery scan followed by a real admission cannot overwrite the exact
  batch; retry converges only after batch completion. An abandoned local token
  also leaves the canonical exact batch intact and returns native `AUTHORITY`.
- Executor reap proof survives the native-call/Python-object handoff. Native
  confirmation first retains the exact random capability, certified hash, and
  identity in the journal handle and idempotently returns that same receipt on
  retry without another `waitpid`. The new read-only
  `cpl_journal_recover_executor_reap_proof` entry point reissues that retained
  opaque receipt after a successor transition; it cannot mint a receipt and
  existing native consumers still reject it against the wrong generation.
- Retained-owner inspection now duplicates the directory descriptor, reopens
  and certifies the exact nonce-bound journal, completely enumerates the exact
  group, probes group-capability absence, records the retained child as live,
  reapable, or reaped, and compares the stored Python receipt with the native
  recovered receipt. Teardown and identity-rejection evidence compute
  `untracked_orphan_count` from a final complete enumeration; no literal zero
  remains.
- Capacity cancellation is the first non-throwing state transition in both
  outer cleanup paths. A full registry rejects before `mkdtemp` or `Popen`, and
  a fallible pre-spawn cleanup cannot strand its reservation. Production keyed
  reconcile-and-release returns false until a freshly certified terminal head,
  empty exact-group enumeration, absent group capability, independently reaped
  retained child, and exact native reap receipt all agree. It keeps the key
  through injected control, stderr, journal, and directory-FD close failures;
  a retained release certification makes late partial-close retry safe, and
  the slot is removed only after every handle reports closed. The injections
  raise after the underlying close succeeds; retry observes socket/stream/
  journal closure directly and revalidates a raw directory FD by its retained
  device/inode, treating `EBADF` as the completed close rather than touching a
  possibly reused descriptor.

### TDD and mutation evidence

The first focused RED run produced the intended failures for four authority
breaks: `mark_unconfirmed` overwrote a freshly admitted batch, a second reap
confirmation returned `REAP_REQUIRED` after native success, the reservation
had no registry-queryable owner after caller handoff, and fallible pre-spawn
cleanup left registry count 1. The production-release row initially could not
reach its assertion in the sandbox because Darwin process inspection was
denied; the host run then exposed an invalid immutable-socket patch seam. After
moving injection to the production close boundary, the row exposed the real
already-reaped reconciliation gap and verified retry across every handle class.

The first broad GREEN run found two more real boundary defects. Normal test
release removed the registry key before the outer `finally`, causing a local
double close; retaining the reservation's exact registry-entry reference fixed
that without restoring a second ownership flag. Successor-prepared and
successor-active exceptions could no longer revalidate the prior executor's
receipt against the new generation; the native read-only receipt recovery API
now proves that exact historical reap without weakening generation checks.

A deliberate mutation removed only the final native
`BATCH_ACTIVE`/`RETIRING_BATCH` rejection. The stranded-batch regression failed
because `UNCONFIRMED` was appended. Restoring the guard made the same test pass,
proving the test covers the final serialized boundary rather than merely the
action-lock wait.

### Final verification

- Focused Task 5 plus action-race command: 119 passed, zero skipped, XPASS, or
  warnings in 33.42 seconds.
- `UV_CACHE_DIR=/private/tmp/claude-proxy-uv-cache
  GOCACHE=/private/tmp/shn-go-cache make check`: 151 unit tests passed; Ruff and
  strict mypy were clean.
- Host `UV_CACHE_DIR=/private/tmp/claude-proxy-uv-cache make darwin`: 192
  Darwin tests passed, zero skipped and zero XPASS, in 30.86 seconds.
- Three bounded repetitions of the 16-row ownership-transfer,
  recovery-failure, keyed-release, stale-scan, stranded-batch, and reap-handoff
  subset passed all 48 executions.
- `make -B native` rebuilt all seven native targets with Apple clang strict C17
  flags. Apple clang static analysis of production/fault lifecycle,
  production/injection supervisor, anchor, and probe child emitted six empty
  370-byte plist reports with no diagnostics.
- All executables and dylibs are arm64 Mach-O. Every lifecycle consumer resolves
  `@rpath/libclaude_proxy_lifecycle.dylib`; production exports the mark,
  confirm, and read-only reap-recovery entry points and no `_cpl_fault_*`
  symbol. The fault dylib retains the fault surface. Production supervisor
  strings contain no Task 5 injection selector or injection-stage name.
- Existing compile-time C ABI assertions and Python ctypes size assertions pass.
  `git diff --check` is clean, and no analyzer plist or object file is present
  in the worktree.

No Claude CLI, model, credential, network peer, pre-existing process, or
pre-existing allocation was contacted or modified. Signals were limited to
freshly spawned, exactly re-observed Task 5 groups, and removals were limited to
Task 4-verified temporary `allocation.workdir` paths under the user's explicit
authorization.

## Remediation cycle 2

Remediation base: `9e8dc2f85f2db3c59f67ce73401147fb16512a0b`

Remediation commit: this commit

### Breaker-finding disposition

- The pointer-free reap receipt is now a 224-byte C/Python ABI value carrying
  the exact allocation nonce, cleanup generation, authority epoch, complete
  112-byte `cpl_process_identity`, certified head hash, and opaque capability.
  Native confirmation stores that entire context before returning; idempotent
  confirmation and historical recovery reissue the same value. Every consumer
  compares the receipt with the journal's retained context and with the
  current chain's generation, authority epoch, and complete identity. Thus a
  changed boot ID, PID, start time, UID, process group, session, identity flags,
  executable device/inode/hash, generation, epoch, nonce, certified hash, or
  capability is rejected. A fault-build-only validator supplies a synthetic
  same-PID/different-start-time chain and proves that incarnation reuse cannot
  cross the boundary; that symbol is absent from the production dylib.
- Historical receipt recovery remains possible after a legitimate successor
  transition because recovery returns only the exact retained prior receipt.
  It does not mint a new receipt, and generation consumers accept it only while
  the canonical chain still identifies that exact prior actor and intended
  retirement transition. Python retained-owner comparison now includes every
  receipt field instead of PID/hash/capability alone.
- The retained parent directory descriptor now transfers into a one-shot
  `_DetachedFD` obligation and the owner field becomes `-1` inside the guarded
  close region before the syscall starts. A failure known to precede
  `obligation.begin()` safely restores ownership. Once begin runs, no error --
  including `EINTR`, `EBADF`, or a post-success injected exception -- can
  restore or retry the numeric descriptor. A deterministic regression closes
  the directory, immediately reopens the same directory into the reused
  numeric slot, raises an ambiguous error, retries keyed release, and proves
  the unrelated reopened descriptor remains valid while capacity is released.
- Socket, stderr-stream, and `Journal` close paths were audited against the
  same aliasing failure. Their owned objects publish durable closed state
  (`fileno() == -1` or `.closed`) as part of successful close, so a nested
  post-success exception cannot cause a numeric descriptor retry; a
  pre-syscall exception leaves the object visibly open and owned. Existing
  per-class post-success fault rows remain green. Only the raw integer required
  the explicit detached obligation.

### TDD and mutation evidence

The first receipt RED reported native/Python ABI size 72 instead of 224. After
the context fields were populated, the field-mutation RED showed a forged
generation was accepted by `prepare_successor`; the exact validator made the
full mutation table green. The synthetic same-PID chain test first failed
because the fault-only validator did not exist, then passed against changed
generation and changed process start time. The retained-owner comparison test
also first showed that altered generation compared equal before full receipt
comparison was added.

The descriptor-reuse RED reached the intended host boundary and ended with
`EBADF` on the unrelated reopened directory descriptor, proving retry closed
the reused number. The detached-obligation implementation made the same test
green, including a distinct pre-syscall fault and an ambiguous `EINTR` after
successful close. An initial sandbox run could not reach this assertion because
Darwin process observation returned native `PROCESS_IDENTITY`; diagnostic
instrumentation isolated that environment boundary and was removed before the
host-permission RED/GREEN cycle.

A deliberate mutation removed the receipt-generation comparison from
`valid_reap_proof`. The field-mutation regression immediately failed because
the changed-generation receipt prepared a successor. Restoring the comparison
made the mutation and same-PID rows pass again. Three bounded repetitions of
the four receipt rows plus keyed descriptor release passed all 15 executions.

### Final verification

- Focused Task 5 plus journal-action races: 122 passed, zero skipped or XPASS,
  in 34.24 seconds. One preceding broad run exposed a timing-sensitive
  canonical-request row under load (121 passed, one failed); the row passed in
  isolation and the complete focused rerun passed all 122.
- `UV_CACHE_DIR=/private/tmp/claude-proxy-uv-cache make check`: 151 unit tests
  passed; Ruff and strict mypy were clean.
- Host `UV_CACHE_DIR=/private/tmp/claude-proxy-uv-cache make darwin`: 195
  Darwin tests passed, zero skipped and zero XPASS, in 32.24 seconds.
- `make -B native` rebuilt all seven native targets with Apple clang strict C17
  flags. Apple clang static analysis of production/fault lifecycle,
  production/injection supervisor, anchor, and probe child emitted six empty
  370-byte plist reports with no diagnostics.
- All executables and dylibs are arm64 Mach-O. Every lifecycle consumer resolves
  `@rpath/libclaude_proxy_lifecycle.dylib`. Production exports confirmation,
  recovery, and successor entry points and no `_cpl_fault_*` symbol; the fault
  dylib alone exports `_cpl_fault_validate_reap_proof`. Production supervisor
  strings contain no Task 5 injection selector or injection-stage name.
- Native static ABI assertions and Python ctypes assertions agree on the
  224-byte receipt. `git diff --check` is clean, and no analyzer plist or object
  file is present in the worktree.

No Claude CLI, model, credential, network peer, pre-existing process, or
pre-existing allocation was contacted or modified. Signals were limited to
freshly spawned, exactly re-observed Task 5 groups, and removals were limited to
Task 4-verified temporary `allocation.workdir` paths under the user's explicit
authorization.

## Remediation cycle 3

Remediation base: `18034f8842a79f09eb37aac2d133a8ba29502e72`

Remediation commit: this commit

### Breaker-finding disposition

- The retained raw directory descriptor now has an explicit
  `OWNED -> DETACHED_IN_FLIGHT -> CLOSED_PROVED | AMBIGUOUS` state machine.
  Production detaches the numeric descriptor before beginning the one-shot
  close. Any `BaseException` before close-proof publication tombstones the
  owner field, publishes `AMBIGUOUS`, and returns false without ever touching
  that number again. A retry therefore cannot mistake `parent_dirfd == -1` for
  proof that the close succeeded.
- `CLOSED_PROVED` is published only after `os.close` returns. The deterministic
  post-proof injection boundary runs after that publication, so an exception
  there retains the key but a later retry can release capacity without another
  numeric close. The regression immediately reuses the closed number and proves
  that retry leaves the unrelated descriptor valid.
- An ambiguous close permanently retains the exact keyed owner and one bounded
  registry slot. Its read-only inspection reports only close state, detached
  status, release blocking, and exact-key owner count; it exposes no descriptor,
  path, or journal content. Both a pre-syscall `OSError` and a `BaseException`
  interruption leave the original descriptor open for the test to verify, and
  repeated production release attempts remain false with one close attempt.
- Socket, stderr-stream, and `Journal` close behavior was re-audited. Their
  owning objects expose durable closure through `fileno() == -1` or `.closed`,
  and the existing post-success exception rows prove retry consults that object
  state. They do not require the raw-integer state machine.

### TDD evidence

The two-row ambiguous-close RED raised the injected `OSError` and
`BaseException` out of production release instead of retaining an ambiguous
owner. The post-success RED failed because release had no boundary after close
proof publication. After the state machine was added, all three rows passed:
pre-syscall failures retained the key, exact owner, open original descriptor,
and full capacity without retry; post-proof failure retained the key while a
retry released it without closing a same-number replacement.

### Final verification

- Focused Task 5, reconciliation, and journal-action race suite: 124 passed,
  zero skipped or XPASS, in 29.80 seconds.
- `make check`: 151 unit tests passed; Ruff and strict mypy were clean.
- Host `make darwin`: 197 Darwin tests passed with zero skipped or XPASS; the
  final concise rerun completed in 40.50 seconds.
- Three additional bounded repetitions of the two ambiguous-close rows and the
  post-proof retry row passed all nine executions.
- `make -B native` rebuilt all seven native targets with Apple clang strict C17
  flags. Apple clang static analysis of production/fault lifecycle,
  production/injection supervisor, anchor, and probe child emitted six empty
  370-byte plist reports with no diagnostics.
- All executables and dylibs are arm64 Mach-O. Every lifecycle consumer resolves
  `@rpath/libclaude_proxy_lifecycle.dylib`; the production dylib exports no
  `_cpl_fault_*` symbol, while the fault dylib retains its fault surface. The
  production supervisor contains no Task 5 injection selector or stage strings,
  while the probe supervisor contains the expected test-only strings.
- Native compile-time ABI assertions passed during rebuild, Python reports the
  reap-proof ABI as 224 bytes, `git diff --check` is clean, and no analyzer
  plist or object file is present in the worktree.

No Claude CLI, model, credential, network peer, pre-existing process, or
pre-existing allocation was contacted or modified. Signals were limited to
freshly spawned, exactly re-observed Task 5 groups. No retained `UNCONFIRMED`
artifact was deleted.

## Task 6 integration correction

Integration base: `f6b0b0ce76b114d1ffeb2cf71facb2702bf914e1`

Integration commit: this commit

### Contract correction

- The native shim again reads and removes exactly the four Task 5 bootstrap
  descriptors: allocation nonce, instance directory, verified real CLI, and
  the proxy-owned control descriptor. The later ambient anchor-control and
  network-selector variables were removed from native parsing and every Python
  launch probe.
- `IDENTITY_ACK` now carries one fixed 48-byte versioned configuration value.
  The native supervisor rejects a missing payload, an unknown version, a proxy
  bit other than zero or one, any nonzero reserved field, or a sequence/hash
  that does not equal its certified canonical head. Only after that validation
  does it construct the child environment, so proxy names can enter `envp`
  only through the authenticated selector.
- The supervisor creates its own private relay and maps the anchor endpoint to
  fixed inherited descriptor 198 before `execve`. The exact anchor invocation
  remains `--allocation-nonce`, `--instance-dir`, `--control-fd`,
  `--real-cli`, `--`, followed by ordinary CLI argv. The `--control-fd` value
  is the original authenticated proxy-owned channel, retained by the anchor
  solely for post-supervisor-loss fallback.
- The anchor uses only the private relay for identity, arm, running, and normal
  cleanup control. It does not poll or read the original proxy channel while
  that relay is live. After authenticated `CLI_RUNNING` and private-relay EOF,
  it becomes the sole fallback reader. Both control descriptors are marked
  close-on-exec before the real CLI is released; the real probe child's
  exhaustive socket-descriptor check proves neither leaks into CLI execution.

### TDD and verification evidence

The focused RED collected 20 tests and ended with 19 failures and one pass.
Those failures exposed the six-variable bootstrap set, absent authenticated
configuration validation, separate fallback socket, missing no-reader-race
proof, and missing descriptor-leak evidence. After the correction, the same
focused bootstrap/fallback command passed all 20 tests in 2.72 seconds.

The full host-permission Darwin suite passed all 205 tests in 38.71 seconds
with zero skips or XPASS. Sandbox execution was not counted because macOS
`libproc` process observation is denied there; the bounded host run was local
only. `make check` passed 216 unit tests (including the concurrently prepared
Task 6 unit rows), Ruff, and strict mypy. `make native` rebuilt every touched
native target with strict C17 warnings-as-errors, compile-time ABI assertions
include the 48-byte supervisor configuration, and `git diff --check` is clean.

No Claude CLI, model, credential, network peer, pre-existing process, or
pre-existing allocation was contacted or modified. Signals were limited to
freshly spawned, exactly re-observed Task 5 probe groups. No live Task 6 gate
was executed.
