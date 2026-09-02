# Feasibility probes

This package records fail-closed evidence for the trusted-local Claude Agent SDK
feasibility checks. Release evidence must run with:

```console
uv run pytest --strict-markers --forbid-skips -W error
```

Live subscription checks are opt-in and require `RUN_LIVE_CLAUDE_TESTS=1`.
They must stop if the current policy evidence is absent, ambiguous, or negative.

## Release policy

Every release-evidence pytest command must use `--strict-markers --forbid-skips
-W error`. The `--forbid-skips` hook records collection and every test-phase
skip, including expected failures represented as skips, and changes the session
to failed. Optional developer-only checks may deliberately omit that flag, but
they are never release evidence.

## Runtime and policy inputs

The supported runtime tuple is Claude Agent SDK `0.2.148`, Claude CLI
`2.1.251`, Darwin major version `14`, and a local `apfs` mount. The probe
resolves and hashes one regular executable before accepting its version.

Before any subscription-backed check, obtain both official primary pages:

- <https://code.claude.com/docs/en/agent-sdk/overview>
- <https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan>

The final redacted manifest records each URL, retrieval UTC timestamp, page
SHA-256, and a narrow `personal_local_use_allowed` interpretation. It never
stores page bodies, credentials, prompts, or response content. An absent,
ambiguous, or negative interpretation disables all live subscription probes.

### Task 1 policy recheck

The required primary pages were retrieved on 2026-08-31. Their bodies were
hashed transiently and were not stored in this repository.

| Source | Retrieved (UTC) | SHA-256 |
| --- | --- | --- |
| Agent SDK overview | 2026-08-31T23:56:46Z | `2830a3e2b3623aa731e55bede28cf7ba652c524195b3082fce3f56f4aabc75e5` |
| Claude plan notice | 2026-08-31T23:56:46Z | `19ee9ebf0bbed7f2b6ec9562e730303269f97c232b3db04506a5c6f7c6d379ca` |

`personal_local_use_allowed` is **false** for this feasibility effort. The
current pages do not provide an unambiguous authorization for a local proxy to
offer existing Claude-login subscription access; the overview specifically
limits unapproved third-party offerings. This fail-closed result disables live
subscription probes unless current primary policy evidence later supplies an
affirmative, applicable authorization.

## Final Phase 0 verdict

The committed [`validated-environment.json`](validated-environment.json) is a
schema-valid, content-free record of the current environment. It is an honest
negative feasibility result, not passing release evidence:

- the installed CLI is `2.1.252`, while the supported tuple pins `2.1.251`;
- the personal-subscription policy verdict is non-affirmative;
- the public SDK/CLI surface does not prove per-child existing-login
  provenance, a pre-input network boundary, or lifetime/per-turn provenance;
- prompt isolation, attribution absence, compaction suppression, path-safe
  live persistence, exact backend identity, native continuity, streaming, and
  exact usage semantics were not run and remain false;
- no Task 9 live SDK-tool record exists, so optional tools remain disabled.

The deterministic Darwin/APFS, synchronization, lock, bounded-journal,
lifecycle, retaining-supervisor, environment-construction, and structured
string-input facts remain recorded as true. `load_manifest()` accepts this
record, while `require_core_gates()` deterministically rejects its explicit
false gate set. Phase 1 is therefore blocked.

The manifest contains only field names, enums, booleans, bounded integers,
paths and hashes for public executables/packages, policy source metadata,
typed usage shapes/digests, and the exact configured alias target. It contains
no prompt, response, observed usage value, session identifier, credential
path/value, environment value, credential, or transport byte.

## Manifest generation

The only generation command is an explicit live operation:

```console
RUN_LIVE_CLAUDE_TESTS=1 uv run claude-proxy-probe all \
  --ack-personal-local-use-policy \
  --output docs/feasibility/validated-environment.json
```

Caller acknowledgment is only an invocation guard and never counts as policy
proof. The command fails before creating or overwriting output while live
opt-in is absent or any policy/runtime/attestation prerequisite is false. The
current tuple therefore must not run this command to mint evidence. A future
affirmative implementation must rerun every gate and may write only by a
same-directory mode-`0600` temporary file, `F_FULLFSYNC`, descriptor-relative
rename, and parent-directory `fsync`.

Synthetic all-true manifests in unit tests exercise canonical digest and
resolver mechanics only. They are never committed evidence and cannot unblock
Phase 1.
