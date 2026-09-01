# Task 1 report — Bootstrap the Probe Package and Record Policy/Runtime Inputs

## Implementation

- Added the Python 3.14-only `claude-sdk-proxy` package with
  `claude-agent-sdk==0.2.148`, pytest `>=8.4,<10`, Ruff, and mypy tooling.
- Added the release pytest policy: `--forbid-skips` records collection and all
  test-phase skips (including xfail-as-skip), while optional developer runs
  remain explicit opt-ins.
- Added frozen `RuntimeTuple`, `PolicyEvidence`, and `CliIdentity` evidence
  types. Runtime comparison is exact across SDK, CLI, Darwin-major, and
  filesystem fields. Policy validation requires exactly the two approved
  primary HTTPS URLs and an affirmative flag.
- Added fail-closed CLI identity inspection: resolve one regular executable,
  invoke only `[resolved_path, "--version"]`, require version `2.1.251`, and
  return its resolved path, device, inode, mode, SHA-256, and parsed version.
- Added C17 native smoke, strict unit/live target definitions, and a release
  aggregate target. Every release pytest path expands
  `PYTEST_RELEASE_FLAGS := --strict-markers --forbid-skips -W error`.
- Documented the evidence handling and policy stop condition without storing
  policy-page bodies or credentials.

## TDD evidence

### Policy hook bootstrap

The binding brief requires the exact repository hook to exist before pytester
can copy that source into its isolated subprocess tree. I installed that
specified bootstrap hook, then ran its end-to-end proof:

```console
$ uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_pytest_policy.py -v
============================== 6 passed in 0.87s ===============================
```

The tests cover runtime skip, collection skip, xfail-as-skip, explicit opt-in,
single-backend AnyIO, and an unmarked coroutine failing the release gate.

### Validated runtime and policy types

Initial RED command after creating the tests and before adding
`validated.py`:

```console
$ uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_validated.py -v
E   ModuleNotFoundError: No module named 'claude_sdk_proxy.validated'
=============================== 1 error in 0.04s ===============================
```

Expanded contract RED command, still before production implementation:

```console
$ uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_validated.py -v
E   ModuleNotFoundError: No module named 'claude_sdk_proxy.validated'
=============================== 1 error in 0.04s ===============================
```

GREEN command after minimal implementation:

```console
$ uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_validated.py -v
============================== 15 passed in 9.13s ==============================
```

The tests exercise every tuple component, immutability, complete/negative
policy evidence, regular/executable CLI identity, exact CLI version rejection,
and nonregular path rejection.

### Native smoke target correction

The first full check produced the expected strict-C failure for its empty
translation unit:

```console
$ make check
/dev/null:1:1: error: ISO C requires a translation unit to contain at least one declaration [-Werror,-Wempty-translation-unit]
make: *** [native] Error 1
```

Root cause: the new `native` target sent `/dev/null` to Apple clang while the
required `-pedantic -Werror` flags make an empty C17 translation unit invalid.
The same clang and flags accepted a one-declaration smoke translation unit;
the target now pipes precisely that declaration into clang. This is a
mechanical build-bootstrap correction, not a behavior expansion.

## Policy recheck

The two official primary pages were retrieved transiently on 2026-08-31 and
their bodies were not retained:

| URL | Retrieved (UTC) | SHA-256 |
| --- | --- | --- |
| `https://code.claude.com/docs/en/agent-sdk/overview` | 2026-08-31T23:56:46Z | `2830a3e2b3623aa731e55bede28cf7ba652c524195b3082fce3f56f4aabc75e5` |
| `https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan` | 2026-08-31T23:56:46Z | `19ee9ebf0bbed7f2b6ec9562e730303269f97c232b3db04506a5c6f7c6d379ca` |

Narrow interpretation: `personal_local_use_allowed=false`. The Agent SDK
overview says unapproved third-party developers may not offer Claude-login or
rate limits in their products, while the current plan notice pauses a related
policy change and describes superseded text as no longer taking effect. That
does not unambiguously authorize this local proxy workflow. Per the approved
plan, later Task 10 evidence must record the false policy verdict and all live
subscription probes must stop unless current primary policy evidence becomes
affirmative and applicable.

## Full verification

```console
$ uv lock
Resolved 45 packages in 2ms

$ make check
... clang -std=c17 -Wall -Wextra -Werror -pedantic ...
============================== 21 passed in 0.30s ==============================
All checks passed!
Success: no issues found in 2 source files
```

The brief's exact command was also run:

```console
$ claude --version && uv lock && uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_validated.py -v && uv run ruff check . && uv run mypy src/claude_sdk_proxy
2.1.252 (Claude Code)
Resolved 45 packages in 2ms
============================== 15 passed in 0.23s ==============================
All checks passed!
Success: no issues found in 2 source files
```

Although that shell command returns success, its first output does not meet
the required pinned CLI value `2.1.251`. The validator confirms the fail-closed
result:

```console
$ uv run python -c '... read_cli_identity(Path("/Users/bwang/.local/bin/claude"))'
claude_sdk_proxy.validated.RuntimeMismatch: CLI version does not match the validated version
```

## Files changed

- `.gitignore`
- `Makefile`
- `pyproject.toml`
- `uv.lock`
- `src/claude_sdk_proxy/__init__.py`
- `src/claude_sdk_proxy/validated.py`
- `tests/conftest.py`
- `tests/unit/test_pytest_policy.py`
- `tests/unit/test_validated.py`
- `docs/feasibility/README.md`
- `.superpowers/sdd/2026-08-29-phase-0-feasibility/task-1-report.md`

## Self-review

- The policy self-tests install the actual repository hook rather than a
  duplicate implementation.
- Every release pytest target expands the single strict policy variable.
- `read_cli_identity()` invokes only the resolved executable and `--version`;
  it does not receive credential or page content.
- Documentation contains only evidence metadata and a narrow policy
  interpretation, never retrieved page bodies or credential data.
- The complete non-live release check has fresh green evidence.

## Concerns

1. The installed `claude` reports `2.1.252`, not the required `2.1.251`; it is
   deliberately rejected and prevents positive runtime attestation.
2. Current official policy is not an unambiguous affirmative authorization for
   this proxy workflow. The binding plan requires a false policy verdict and
   no live subscription probing.
