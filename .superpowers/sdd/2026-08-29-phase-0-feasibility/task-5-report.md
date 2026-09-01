# Task 5 report — supervisor and anchor cleanup ownership

Status: DONE — Fix Round 1 implemented and verified

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
