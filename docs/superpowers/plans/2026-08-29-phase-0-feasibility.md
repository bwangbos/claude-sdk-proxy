# Phase 0 Trusted-Local Feasibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a fail-closed, machine-readable verdict proving whether the pinned Claude Agent SDK/CLI tuple can support the approved prompt-isolated, existing-login, Darwin/APFS lifecycle design before any HTTP proxy is built.

**Architecture:** Phase 0 builds a small Python probe package plus three Apple-clang C17 executables: a Darwin capability probe, the SDK executable shim/supervisor, and a durable process-group anchor. Core gates run in dependency order—platform primitives, journal authority, process ownership, per-child authentication, then model semantics—so a successful Claude response cannot hide a failed safety or provenance assumption. Tool bridging is a separate optional gate whose failure disables tools without blocking the text-only proxy.

**Tech Stack:** macOS 14+ on local APFS, Python 3.14, uv, `claude-agent-sdk==0.2.148`, Claude CLI `2.1.251`, Apple clang with C17, pytest/AnyIO, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-08-29-claude-subscription-api-proxy-design.md`

## Global Constraints

- Personal, same-user, loopback-only existing-login use; never implement login, read credential contents, or accept credential material.
- Recheck the two official policy sources in Task 1 on the execution date; a disallowing or ambiguous policy verdict stops subscription-backed work.
- Validate SDK `0.2.148` and installed CLI `2.1.251` initially, but commit them as supported only after every applicable live gate passes.
- Support only Darwin/macOS 14 or newer with the runtime root on a local APFS mount whose exact required operations pass Task 3.
- Use Apple clang `-std=c17 -Wall -Wextra -Werror -pedantic`; Rust/Cargo is not part of Phase 0.
- Construct the real CLI environment from the exact allowlist in spec section 7; never forward the proxy master key, run token, API-key override, OAuth override, provider override, or unrecognized `ANTHROPIC_*`, `CLAUDE_*`, or `LOCAL_PROXY_*` value.
- Use the real existing-login configuration root without opening credential contents. A clean synthetic configuration root is supplemental evidence, never a core gate.
- Accept one plain-string system prompt only. Set built-in tools, skills, settings, connectors, memory, workflows, subagents, and ambient MCP servers to empty or disabled.
- Require per-child positive `existing_claude_login` evidence before releasing caller configuration or user content to a model or other network peer.
- Map each public model alias to one exact backend model ID and buffer all content until authoritative response identity matches it.
- Persist no prompts, transcripts, responses, tool payloads, SDK transport bytes, proxy credentials, or Claude credentials in reports or lifecycle journals.
- Scope durability claims to process crashes while the same kernel and local APFS mount remain available; reboot, power-loss, and storage rollback are not passing evidence.
- Core-gate failure stops Phase 1. Optional tool-gate failure records tools disabled for that exact SDK/runtime/model tuple. Phase 0 proves only SDK-level MCP naming, correlation, suspension, and lifecycle behavior; Anthropic/OpenAI framing and streaming release evidence belong to Phase 3.
- Live tests require `RUN_LIVE_CLAUDE_TESTS=1`; no skipped live test may contribute a passing gate.
- Every mandatory Phase 0, Phase 3, and Phase 4 pytest evidence command uses `--strict-markers --forbid-skips -W error`. Pytest 8.4+ directly errors on an unhandled async test and no longer exposes the legacy `PytestUnhandledCoroutineWarning` category, so the portable release contract promotes every remaining warning instead of naming the removed class. The policy self-tests below prove that an unmarked coroutine cannot pass. Optional developer-only commands may omit `--forbid-skips`, but their results are never release evidence.

## File Map

- `pyproject.toml`: Python 3.14 package, pinned SDK, test and static-analysis configuration.
- `Makefile`: reproducible Apple-clang builds and aggregate validation commands.
- `native/darwin_probe.c`: APFS, sync, process-identity, and BSD-lock probe executable.
- `native/lifecycle.h`: C ABI for journal handles, canonical scans, durable certification, and lifecycle transitions.
- `native/lifecycle.c`: shared journal/lock/authority implementation linked by Python, supervisor, and anchor.
- `native/claude_supervisor.c`: SDK executable shim, environment scrubber, retaining parent/reaper, and gated CLI relay.
- `native/claude_anchor.c`: durable session/group leader and supervisorless cooperative fallback.
- `src/claude_sdk_proxy/validated.py`: manifest schema, pinned tuple, and core/optional verdict readers.
- `src/claude_sdk_proxy/environment.py`: exact environment construction and ambiguity rejection.
- `src/claude_sdk_proxy/isolation.py`: string-only Agent SDK option construction.
- `src/claude_sdk_proxy/platform.py`: typed wrapper around `darwin_probe` evidence.
- `src/claude_sdk_proxy/journal.py`: bounded records, canonical scanning, append admission, and durable-head certification.
- `src/claude_sdk_proxy/lifecycle.py`: approved cleanup transition table and admitted-batch validation.
- `src/claude_sdk_proxy/supervisor_probe.py`: bootstrap, group cleanup, and crash-injection orchestration.
- `src/claude_sdk_proxy/attestation.py`: public init evidence and exact backend-model validation.
- `src/claude_sdk_proxy/path_policy.py`: credential-metadata and content-safe path classification.
- `src/claude_sdk_proxy/probes.py`: redacted live prompt/session/model/thinking/tool probes.
- `src/claude_sdk_proxy/usage_evidence.py`: typed per-tuple SDK-usage field schema and lossless public mappings.
- `src/claude_sdk_proxy/probe_cli.py`: explicit live-probe and manifest-generation CLI.
- `tests/unit/`: pure schema, environment, redaction, journal, lifecycle, and event tests.
- `tests/conftest.py`: repository-wide `--forbid-skips` collection/runtime skip recorder and release-session failure hook.
- `tests/unit/test_pytest_policy.py`: subprocess-style pytester proofs for the release test policy and AnyIO execution.
- `tests/darwin/`: local APFS, lock, crash, process-group, and reconciliation tests.
- `tests/live/`: opt-in existing-login, prompt-purity, model, session, streaming, thinking, and tool tests.
- `docs/feasibility/README.md`: exact commands, evidence interpretation, and stop policy.
- `docs/feasibility/validated-environment.json`: generated redacted verdict consumed by later phases.

---

### Task 1: Bootstrap the Probe Package and Record Policy/Runtime Inputs

**Files:**
- Create: `pyproject.toml`
- Create: `Makefile`
- Create: `.gitignore`
- Create: `src/claude_sdk_proxy/__init__.py`
- Create: `src/claude_sdk_proxy/validated.py`
- Create: `tests/conftest.py`
- Create: `tests/unit/test_pytest_policy.py`
- Create: `tests/unit/test_validated.py`
- Create: `docs/feasibility/README.md`

**Interfaces:**
- Consumes: official policy URLs from spec section 20 and installed `claude` executable.
- Produces: `RuntimeTuple`, `PolicyEvidence`, `read_cli_identity(path: Path) -> CliIdentity`, and `validate_policy(evidence: PolicyEvidence) -> None`.

- [ ] **Step 0: Install and self-test the repository-wide pytest release policy**

`tests/conftest.py` owns the gate. It records skipped collection reports and skipped setup/call/teardown reports; this intentionally includes expected-failure reports represented by pytest as skips. It changes the session status only when `--forbid-skips` is present, so explicitly optional developer suites can still exercise platform or dependency skips without becoming release evidence.

```python
# tests/conftest.py
from __future__ import annotations

import pytest

pytest_plugins = ("pytester",)
_SKIPPED_REPORTS: set[tuple[str, str]] = set()


@pytest.fixture
def anyio_backend() -> str:
    """Run the single supported async backend exactly once per test."""
    return "asyncio"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.getgroup("release-policy").addoption(
        "--forbid-skips",
        action="store_true",
        default=False,
        help="fail this pytest session if any collection or test report is skipped",
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    _SKIPPED_REPORTS.clear()


def _record_skip(report: pytest.CollectReport | pytest.TestReport) -> None:
    if report.skipped:
        _SKIPPED_REPORTS.add((report.nodeid, getattr(report, "when", "collect")))


def pytest_collectreport(report: pytest.CollectReport) -> None:
    _record_skip(report)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    _record_skip(report)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not session.config.getoption("--forbid-skips") or not _SKIPPED_REPORTS:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        rendered = ", ".join(
            f"{nodeid}[{when}]" for nodeid, when in sorted(_SKIPPED_REPORTS)
        )
        reporter.write_sep("=", f"release policy forbids skipped reports: {rendered}")
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
```

Write end-to-end hook tests with pytester. `install_policy()` reads the actual repository conftest, removes only its outer `pytester` plugin declaration, and installs that exact source in pytester's isolated tree; the tests must not carry a second hand-written policy implementation.

```python
# tests/unit/test_pytest_policy.py
from pathlib import Path

import pytest


def install_policy(pytester: pytest.Pytester) -> None:
    source = (Path(__file__).parents[1] / "conftest.py").read_text()
    pytester.makeconftest(source.replace('pytest_plugins = ("pytester",)\n', ""))


@pytest.mark.parametrize(
    "body",
    [
        "import pytest\n\ndef test_runtime_skip(): pytest.skip('optional')\n",
        "import pytest\npytest.skip('optional', allow_module_level=True)\n",
        "import pytest\n\n@pytest.mark.xfail(reason='not release evidence')\ndef test_xfail(): assert False\n",
    ],
)
def test_forbid_skips_fails_every_pytest_skip_report(pytester, body: str) -> None:
    install_policy(pytester)
    pytester.makepyfile(body)
    result = pytester.runpytest("--forbid-skips", "-q")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*release policy forbids skipped reports:*"])


def test_skip_policy_is_explicitly_opt_in(pytester) -> None:
    install_policy(pytester)
    pytester.makepyfile("import pytest\n\ndef test_optional(): pytest.skip('optional')\n")
    result = pytester.runpytest("-q")
    result.assert_outcomes(skipped=1)
    assert result.ret == pytest.ExitCode.OK


def test_required_anyio_gate_executes_with_zero_skips(pytester) -> None:
    install_policy(pytester)
    pytester.makepyfile(
        "import pytest\n\n@pytest.mark.anyio\nasync def test_async(): assert True\n"
    )
    result = pytester.runpytest(
        "--strict-markers",
        "--forbid-skips",
        "-W", "error",
        "-q",
    )
    result.assert_outcomes(passed=1, skipped=0)
    assert result.ret == pytest.ExitCode.OK


def test_unmarked_coroutine_cannot_pass_release_gate(pytester) -> None:
    install_policy(pytester)
    # Keep this deliberately unmarked negative fixture out of the plan's
    # literal async-test scan while generating exactly that source for pytest.
    pytester.makepyfile("async def " + "test_unmarked(): pass\n")
    result = pytester.runpytest(
        "--strict-markers",
        "--forbid-skips",
        "-W", "error",
        "-q",
    )
    assert result.ret != pytest.ExitCode.OK
```

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_pytest_policy.py -v`

Expected: all policy self-tests PASS, including an exact zero-skipped marked coroutine and a nonzero unmarked-coroutine subprocess.

- [ ] **Step 1: Write failing tuple and policy tests**

```python
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_sdk_proxy.validated import PolicyEvidence, RuntimeMismatch, RuntimeTuple


def test_runtime_tuple_is_exact() -> None:
    expected = RuntimeTuple("0.2.148", "2.1.251", 14, "apfs")
    with pytest.raises(RuntimeMismatch):
        expected.require(RuntimeTuple("0.2.148", "2.1.250", 14, "apfs"))


def test_policy_requires_two_current_primary_sources() -> None:
    evidence = PolicyEvidence(
        checked_at=datetime.now(UTC), source_urls=(), personal_local_use_allowed=False
    )
    with pytest.raises(RuntimeMismatch, match="policy"):
        evidence.require_allowed()
```

- [ ] **Step 2: Run the tests and confirm the intended failures**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_validated.py -v`

Expected: FAIL because `claude_sdk_proxy.validated` does not exist.

- [ ] **Step 3: Add the package, exact types, and build commands**

Set `requires-python = ">=3.14,<3.15"`, pin `claude-agent-sdk==0.2.148`, constrain the test runner to `pytest>=8.4,<10`, define `claude-proxy-probe = "claude_sdk_proxy.probe_cli:main"`, and register the exact pytest markers `anyio` and `live` so `--strict-markers` is authoritative. In `validated.py`, implement frozen dataclasses with `RuntimeTuple.require()` comparing all four fields and `PolicyEvidence.require_allowed()` requiring these exact HTTPS URLs plus an affirmative boolean:

```python
POLICY_URLS = (
    "https://code.claude.com/docs/en/agent-sdk/overview",
    "https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan",
)
```

Make `read_cli_identity()` resolve a regular executable, run only `[resolved_path, "--version"]`, require exact output version `2.1.251`, and return path, `st_dev`, `st_ino`, mode, SHA-256, and version. `Makefile` targets are `native`, `unit`, `darwin`, `live-core`, `live-tools`, and `check`; `native` invokes `xcrun --find clang` with the global C17 flags. Define `PYTEST_RELEASE_FLAGS := --strict-markers --forbid-skips -W error` and require every pytest invocation reachable from `unit`, `darwin`, `live-core`, `live-tools`, or `check` to expand that variable. A separately named optional developer target may omit `--forbid-skips`, but `check` and every manifest/release target must never depend on it.

- [ ] **Step 4: Recheck policy and verify the bootstrap**

Read both primary pages on the execution date. Record their URLs, retrieval UTC timestamps, SHA-256 hashes of downloaded pages, and the narrow boolean interpretation in the eventual manifest; do not copy page bodies into the repository.

Run: `claude --version && uv lock && uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_validated.py -v && uv run ruff check . && uv run mypy src/claude_sdk_proxy`

Expected: CLI reports `2.1.251`; lock succeeds; tests and static checks pass. If policy is ambiguous or disallows this workflow, commit `personal_local_use_allowed=false` evidence in Task 10 and stop all live subscription probes.

- [ ] **Step 5: Commit the bootstrap**

```bash
git add pyproject.toml uv.lock Makefile .gitignore src/claude_sdk_proxy tests/conftest.py tests/unit/test_pytest_policy.py tests/unit/test_validated.py docs/feasibility/README.md
git commit -m "build: bootstrap trusted-local feasibility probes"
```

---

### Task 2: Enforce the Exact Child Environment and String-Only SDK Profile

**Files:**
- Create: `src/claude_sdk_proxy/environment.py`
- Create: `src/claude_sdk_proxy/isolation.py`
- Create: `tests/unit/test_environment.py`
- Create: `tests/unit/test_isolation.py`

**Interfaces:**
- Consumes: verified CLI directory and optional `CLAUDE_CONFIG_DIR`/network-proxy/pass-through configuration.
- Produces: `EnvironmentAmbiguityError`, `build_child_environment(source: Mapping[str, str], config: EnvironmentConfig) -> dict[str, str]`, `environment_fingerprint(env: Mapping[str, str]) -> str`, and `build_agent_options(config: IsolationConfig) -> ClaudeAgentOptions`.

- [ ] **Step 1: Write failing allowlist and option tests**

```python
def test_child_environment_is_deny_by_default(tmp_path: Path) -> None:
    source = {
        "HOME": str(tmp_path), "LANG": "en_US.UTF-8", "PATH": "/host/bin",
        "LOCAL_PROXY_API_KEY": "proxy-only", "LOCAL_PROXY_RUN_TOKEN": "proxy-only",
        "CLAUDE_UNKNOWN": "unrelated", "RANDOM_HOST_VALUE": "unrelated",
    }
    env = build_child_environment(source, EnvironmentConfig(cli_dir=Path("/opt/claude")))
    assert env["PATH"] == "/opt/claude:/usr/bin:/bin:/usr/sbin:/sbin"
    assert set(env).isdisjoint({
        "LOCAL_PROXY_API_KEY", "LOCAL_PROXY_RUN_TOKEN", "CLAUDE_UNKNOWN",
        "RANDOM_HOST_VALUE",
    })


@pytest.mark.parametrize(
    "name",
    [
        "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "AWS_ACCESS_KEY_ID",
        "GOOGLE_APPLICATION_CREDENTIALS", "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK",
    ],
)
def test_auth_provider_and_custom_endpoint_overrides_reject(name: str, tmp_path: Path) -> None:
    with pytest.raises(EnvironmentAmbiguityError, match="authentication or provider"):
        build_child_environment(
            {"HOME": str(tmp_path), name: "override"},
            EnvironmentConfig(cli_dir=Path("/opt/claude")),
        )


def test_system_prompt_must_be_one_string(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="plain string"):
        IsolationConfig(model_id="claude-exact", system_prompt=[{"type": "text"}], cwd=tmp_path)
```

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_environment.py tests/unit/test_isolation.py -v`

Expected: FAIL because both modules are absent.

- [ ] **Step 3: Implement the environment and option boundaries**

Define the inherited-name set exactly as `HOME USER LOGNAME TMPDIR TMP TEMP LANG LC_ALL LC_CTYPE TZ SSL_CERT_FILE SSL_CERT_DIR CLAUDE_CONFIG_DIR`. Construct `PATH` exactly as tested. Pass uppercase/lowercase HTTP proxy variables only when `network_proxy=True`; pass additional names only from `EnvironmentConfig.pass_names`. Before filtering, reject any recognized or pattern-classified API-key, OAuth, cloud-provider, provider-selection, or custom-endpoint override because silently stripping one could change authentication provenance. Strip rather than reject proxy-only names (`LOCAL_PROXY_API_KEY`, master/run tokens, allocation metadata) and unrelated unallowlisted host names, including unrelated reserved-prefix names. Set every isolation variable from spec section 7 to its fixed value. Hash sorted `name=NULvalue` entries with SHA-256 for attestation, but expose only names and the fingerprint in diagnostics.

`IsolationConfig.__post_init__()` rejects non-`str` system values and non-absolute/non-owned workdirs. `build_agent_options()` sets the exact backend ID, caller string, `tools=[]`, `skills=[]`, `setting_sources=[]`, `mcp_servers={}`, `strict_mcp_config=True`, `agents={}`, verified supervisor path as `cli_path`, empty mode-`0700` cwd, fixed environment, and partial messages. Do not use a system-prompt preset.

- [ ] **Step 4: Prove all forbidden classes and exact option fields**

Parameterize rejection tests over `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, provider credential/selection overrides, and custom endpoint overrides. Separately parameterize stripping tests over every proxy-only key, unrelated unknown reserved-prefix names, and arbitrary unallowlisted host values. Assert explicit HTTP-proxy opt-in and explicit pass-name behavior separately; neither mechanism may admit an authentication/provider/endpoint override.

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_environment.py tests/unit/test_isolation.py -v && uv run mypy src/claude_sdk_proxy/environment.py src/claude_sdk_proxy/isolation.py`

Expected: all cases pass and mypy reports no errors.

- [ ] **Step 5: Commit the isolation profile**

```bash
git add src/claude_sdk_proxy/environment.py src/claude_sdk_proxy/isolation.py tests/unit/test_environment.py tests/unit/test_isolation.py
git commit -m "feat: enforce exact Agent SDK child profile"
```

---

### Task 3: Prove the Darwin 14 and Local-APFS Primitive Contract

**Files:**
- Create: `native/darwin_probe.c`
- Create: `src/claude_sdk_proxy/platform.py`
- Create: `tests/darwin/test_platform.py`
- Create: `tests/darwin/test_sync_crashes.py`
- Create: `tests/darwin/test_root_ownership_primitives.py`

**Interfaces:**
- Consumes: runtime-root path and a probe subcommand.
- Produces: executable `build/bin/darwin-probe`, immutable `MountIdentity`, JSON `PlatformEvidence`, `require_supported_platform(root: Path) -> PlatformEvidence`, `preallocate(fd: int, length: int) -> None`, and `fullfsync(fd: int) -> None`.

- [ ] **Step 1: Write failing platform tests**

```python
def test_runtime_root_reports_required_tuple(runtime_root: Path) -> None:
    evidence = require_supported_platform(runtime_root)
    assert evidence.darwin_major >= 23
    assert evidence.filesystem_type == "apfs"
    assert evidence.is_local is True
    assert evidence.fullfsync_file and evidence.fsync_directory
    assert evidence.preallocate and evidence.renameat and evidence.unlinkat
    assert evidence.mount_identity == MountIdentity(
        filesystem_type="apfs",
        is_local=True,
        mount_device=evidence.mount_device,
        mount_fsid=evidence.mount_fsid,
        mount_flags=evidence.mount_flags,
        runtime_root_st_dev=runtime_root.stat().st_dev,
    )
```

Add crash cases parameterized over `after_create`, `after_preallocate`, `after_append`, `after_fullfsync`, `after_renameat`, `after_unlinkat`, and `after_directory_fsync`; each child calls `_exit(91)` at the named boundary and the parent validates only the prefix authorized by completed syncs. Add named evidence cases `root_reconciliation_lock`, `instance_lifetime_lock`, `owner_record_create`, and `owner_record_replace`: the lock cases prove dedicated canonical-FD BSD `flock` ownership and process-exit release, while owner-record cases prove mode-`0600` create and same-directory full-synced temporary-file `renameat` plus parent-directory `fsync` replacement.

- [ ] **Step 2: Compile/run and confirm failure**

Run: `make native && uv run pytest --strict-markers --forbid-skips -W error tests/darwin/test_platform.py tests/darwin/test_sync_crashes.py tests/darwin/test_root_ownership_primitives.py -v`

Expected: build or import failure because the probe and wrapper are absent.

- [ ] **Step 3: Implement exact C17 probe operations**

Use `uname`, `statfs`, `getattrlist`, `sysctlbyname("kern.boottime")`, `proc_pidinfo`, `openat` with `O_CREAT|O_EXCL|O_APPEND|O_NOFOLLOW|O_CLOEXEC`, `fcntl(F_PREALLOCATE)`, complete bounded `write`, `fcntl(F_FULLFSYNC)`, `renameat`, `unlinkat`, and `fsync` on an open directory FD. Add fixed probe subcommands for the root reconciliation lock, instance lifetime lock, owner-record create, and owner-record replacement sequences; each emits its exact named boolean and fails when any lock/owner/mode/write/full-sync/rename/directory-sync invariant fails. Emit one JSON object to stdout and no path contents. Exit `64` for invocation error, `65` for unsupported tuple, and `74` for failed required syscall.

Reject non-APFS, nonlocal mounts, symlinks, wrong owner/mode, cloud placeholder flags, and Darwin versions below 23. Record OS build, boot time, exact `MountIdentity(filesystem_type, is_local, mount_device, mount_fsid, mount_flags, runtime_root_st_dev)`, and syscall booleans. Normalize numeric fields as unsigned decimal integers, `mount_fsid` as one canonical lowercase hexadecimal pair, and mount flags as a sorted tuple of canonical flag names; never use the generic string `"apfs-local"` as mount identity.

- [ ] **Step 4: Run the positive, negative, and process-crash matrix**

Run: `make native && uv run pytest --strict-markers --forbid-skips -W error tests/darwin/test_platform.py tests/darwin/test_sync_crashes.py tests/darwin/test_root_ownership_primitives.py -v`

Expected: local APFS cases pass; injected nonlocal/FUSE/wrong-mode evidence is rejected; every crash case yields its exact permitted prefix with no inferred power-loss claim.

- [ ] **Step 5: Commit the platform gate**

```bash
git add native/darwin_probe.c src/claude_sdk_proxy/platform.py tests/darwin/test_platform.py tests/darwin/test_sync_crashes.py tests/darwin/test_root_ownership_primitives.py Makefile
git commit -m "test: prove Darwin APFS lifecycle primitives"
```

---

### Task 4: Implement and Stress the Bounded Journal, Locks, and Authority Automaton

**Files:**
- Modify: `Makefile`
- Create: `native/lifecycle.h`
- Create: `native/lifecycle.c`
- Create: `src/claude_sdk_proxy/journal.py`
- Create: `src/claude_sdk_proxy/lifecycle.py`
- Create: `tests/unit/test_journal.py`
- Create: `tests/unit/test_lifecycle.py`
- Create: `tests/darwin/test_flock.py`
- Create: `tests/darwin/test_journal_races.py`
- Create: `tests/darwin/test_journal_lifecycle_crashes.py`

**Interfaces:**
- Consumes: Task 3 file/sync APIs and stable append/action lock files.
- Produces: `build/lib/libclaude_proxy_lifecycle.dylib`, descriptor-relative C functions `cpl_journal_create_at`, `cpl_journal_open_at`, `cpl_journal_delete_at`, `cpl_journal_append`, `cpl_journal_scan`, `cpl_journal_certify`, and `cpl_lifecycle_apply`; Python wrappers `Journal.create_at() -> tuple[Journal, JournalCreateReceipt]`, `Journal.open_at()`, `Journal.delete_at(authority: CertifiedDone | UnreleasedPartialCreate) -> JournalDeleteReceipt`, `Journal.append(record, record_class)`, `Journal.scan() -> CanonicalChain`, `Journal.certify_head(deadline_ns) -> CertifiedHead`, typed deletion authorities `CertifiedDone`/`UnreleasedPartialCreate`, receipts `JournalCreateReceipt(intent_parent_dirsynced)`/`JournalDeleteReceipt(journal_unlink_parent_dirsynced)`, and `Lifecycle.apply(state, record) -> State`.

- [ ] **Step 1: Write failing canonical-chain and transition tests**

```python
def test_first_valid_child_wins(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    intent = journal.append(intent_record(), RecordClass.NORMAL)
    winner = journal.append(prepared_record(parent=intent.hash, generation=1), RecordClass.NORMAL)
    journal.raw_append_for_test(prepared_record(parent=intent.hash, generation=2).encode())
    assert journal.scan().head.hash == winner.hash


def test_retiring_batch_preserves_only_admitted_batch() -> None:
    active = State.batch_active(generation=2, batch_nonce="b1")
    retired = Lifecycle.apply(active, Record.retiring_batch(2, "b1", authority_epoch=1))
    assert retired.exact_batch == "b1"
    with pytest.raises(IllegalTransition):
        Lifecycle.apply(retired, Record.batch_active(2, "b2"))


def test_workdir_waits_for_durable_intent_parent_entry(lifecycle_factory) -> None:
    allocation = lifecycle_factory.pause_create_before("intent_parent_dirsynced")
    assert allocation.create_receipt is None
    assert allocation.workdir_exists is False
    allocation.finish_create()
    assert allocation.create_receipt == JournalCreateReceipt(intent_parent_dirsynced=True)
    allocation.create_workdir()
    assert allocation.workdir_exists is True


def test_cleanup_slot_waits_for_durable_journal_unlink(lifecycle_factory) -> None:
    allocation = lifecycle_factory.done_with_process_and_workdir_absent()
    authority = allocation.certify_done()
    assert isinstance(authority, CertifiedDone)
    allocation.pause_delete_before("journal_unlink_parent_dirsynced")
    assert allocation.delete_receipt is None
    assert allocation.cleanup_slot_releasable is False
    allocation.finish_delete(authority)
    assert allocation.delete_receipt == JournalDeleteReceipt(
        journal_unlink_parent_dirsynced=True
    )
    assert allocation.cleanup_slot_releasable is True


def test_partial_create_cleanup_requires_native_authority(lifecycle_factory) -> None:
    allocation = lifecycle_factory.crashed_after_preallocate()
    authority = allocation.certify_unreleased_partial_create()
    assert isinstance(authority, UnreleasedPartialCreate)
    receipt = allocation.delete_partial_create(authority)
    assert receipt == JournalDeleteReceipt(journal_unlink_parent_dirsynced=True)
```

- [ ] **Step 2: Run unit and Darwin tests to confirm failure**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_journal.py tests/unit/test_lifecycle.py tests/darwin/test_flock.py tests/darwin/test_journal_races.py tests/darwin/test_journal_lifecycle_crashes.py -v`

Expected: collection fails because journal/lifecycle modules do not exist.

- [ ] **Step 3: Implement the bounded binary journal and full transition table**

Implement the authoritative logic once in `native/lifecycle.c`; `journal.py` loads the dylib through `ctypes.CDLL` with fully declared argument/result types, owns one opaque `cpl_journal *` handle per lock domain, and never duplicates its FD. `lifecycle.h` returns `0` on success and stable positive error enums; output structs contain fixed-size hashes/state fields and no pointers to caller-owned memory. Use a fixed record header containing magic, format version, allocation nonce, record length, sequence, cleanup epoch, previous canonical hash, payload type, and CRC-32C; hash canonical encoded records with SHA-256. One bounded C `write()` under append-admission `pthread_mutex_t` plus nonblocking BSD `flock` is the only append operation. Enforce distinct normal and recovery-tail ceilings against physical EOF; preallocate the hard maximum and count torn/stale bytes. Scanner resynchronization accepts only valid magic/length/checksum records and the first legal child of the current canonical head.

Expose this exact ownership-oriented, descriptor-relative C ABI. All names are single validated path components; the library rejects `/`, `.`, `..`, symlinks, wrong owner/mode, and directory-FD/device drift. `cpl_journal_close()` is the sole standalone close operation; successful `cpl_journal_delete_at()` closes and invalidates `*inout_j` itself:

```c
typedef struct cpl_journal cpl_journal;
enum cpl_storage_state {
    CPL_STORAGE_NONE = 0,
    CPL_INTENT_PARENT_DIRSYNCED = 1,
    CPL_JOURNAL_UNLINK_PARENT_DIRSYNCED = 2,
};
enum cpl_delete_authority_kind {
    CPL_DELETE_CERTIFIED_DONE = 1,
    CPL_DELETE_UNRELEASED_PARTIAL_CREATE = 2,
};
struct cpl_create_receipt { enum cpl_storage_state state; uint8_t intent_hash[32]; };
struct cpl_delete_authority {
    enum cpl_delete_authority_kind kind;
    uint8_t allocation_nonce[32];
    uint8_t certified_hash[32];
};
struct cpl_delete_receipt { enum cpl_storage_state state; bool slot_releasable; };
int cpl_journal_create_at(int parent_dirfd, const char *journal_name,
    const uint8_t nonce[32], uint64_t normal_limit, uint64_t hard_limit,
    cpl_journal **out, struct cpl_create_receipt *receipt);
int cpl_journal_open_at(int parent_dirfd, const char *journal_name,
    const uint8_t nonce[32], uint64_t normal_limit, uint64_t hard_limit,
    cpl_journal **out);
int cpl_journal_append(cpl_journal *j, const uint8_t *record,
    uint32_t record_len, uint32_t record_class, uint64_t deadline_ns,
    uint8_t out_hash[32]);
int cpl_journal_scan(cpl_journal *j, struct cpl_chain *out);
int cpl_journal_certify(cpl_journal *j, uint64_t deadline_ns,
    struct cpl_certified_head *out);
int cpl_lifecycle_apply(const struct cpl_state *current,
    const struct cpl_record *record, struct cpl_state *out);
int cpl_journal_delete_at(cpl_journal **inout_j,
    int parent_dirfd, const char *journal_name,
    int workdir_parent_dirfd, const char *workdir_name,
    const struct cpl_delete_authority *authority,
    uint64_t deadline_ns, struct cpl_delete_receipt *receipt);
void cpl_journal_close(cpl_journal *j);
```

`cpl_journal_create_at()` performs one indivisible production sequence owned entirely by `native/lifecycle.c`: `openat(parent_dirfd, journal_name, O_RDWR|O_CREAT|O_EXCL|O_APPEND|O_NOFOLLOW|O_CLOEXEC, 0600)`, verify regular file/owner/mode/device, `fcntl(F_PREALLOCATE)` through the hard limit, append exactly one canonical `INTENT` record with one bounded `write()`, `fcntl(F_FULLFSYNC)` the journal FD, then `fsync(parent_dirfd)`. It zeroes the output initially and returns a `CPL_INTENT_PARENT_DIRSYNCED` receipt only after the parent-directory sync succeeds; the Python wrapper then constructs exactly `JournalCreateReceipt(intent_parent_dirsynced=True)`. The Python allocation path must not create or expose a workdir, spawn a supervisor, or release caller input before consuming that receipt. It never recreates or truncates an existing journal; recovery uses `cpl_journal_open_at()` under reconciliation.

`cpl_journal_delete_at()` accepts exactly two authority kinds. Normal cleanup requires a `CertifiedDone` wrapper over `CPL_DELETE_CERTIFIED_DONE`; native code independently re-certifies the canonical `DONE` head, its process-absence/reap proof, and the recorded identities through the retained-parent/reconciliation rules. Partial-create rollback requires an `UnreleasedPartialCreate` wrapper over `CPL_DELETE_UNRELEASED_PARTIAL_CREATE`; only native reconciliation may construct it, after a descriptor-relative scan proves there is no complete canonical `INTENT` and the allocation's dependent-artifact scan is empty. A caller-provided enum or hash is never trusted as proof, and no other deletion authority exists.

For either authority, native code requires `fstatat(workdir_parent_dirfd, workdir_name, ..., AT_SYMLINK_NOFOLLOW)` to report `ENOENT`, revalidates the journal name/FD inode pair, calls `unlinkat(parent_dirfd, journal_name, 0)`, then `fsync(parent_dirfd)`. It returns `CPL_JOURNAL_UNLINK_PARENT_DIRSYNCED` with `slot_releasable=true` only after that sync; the Python wrapper then constructs exactly `JournalDeleteReceipt(journal_unlink_parent_dirsynced=True)`. Every earlier/error return leaves the receipt zeroed and `slot_releasable=false`. No Python/native caller may report erasure or release the cleanup/lifecycle slot from `DONE`, process absence, workdir absence, successful `unlinkat`, or journal-FD close alone.

Implement every state and edge in spec section 12 verbatim: `PREPARED`, `ACTIVE_READY`, repeatable `BATCH_ACTIVE`, `RETIRING_IDLE`, `RETIRING_BATCH`, authority replacement, next generation, `DONE`, and `UNCONFIRMED`. `certify_head()` performs snapshot head+EOF, unlock, `F_FULLFSYNC`, relock/rescan, and bounded retry until both values are unchanged.

- [ ] **Step 4: Exercise all lock, corruption, and race cases**

Test same-process task contention, separate opens, rejected `dup`, unrelated-FD close, fork/exec inheritance, owner exit, process-exit release, concurrent sibling appends, short/torn/checksum-invalid records, normal-ceiling rejection, recovery-tail admission, hard-limit rejection, certification races, executor death during a batch, retirement winning before admission, and retirement preserving an admitted batch.

Build a fault-injection test dylib from the exact production `native/lifecycle.c`/`lifecycle.h` sources with only `CPL_ENABLE_FAULT_INJECTION` adding an `_exit(91)` boundary hook; do not use `darwin_probe` or a reimplemented Python/generic file sequence for this gate. In forked children call the real `cpl_journal_create_at()` at `after_openat`, `after_preallocate`, `after_intent_append`, `after_journal_fullfsync`, and `after_intent_parent_fsync`, and call the real `cpl_journal_delete_at()` with both a `CertifiedDone` and an `UnreleasedPartialCreate` authority at `after_authority_revalidated`, `after_process_absence_verified` (normal only), `after_workdir_absence_verified`, `after_journal_unlinkat`, and `after_journal_unlink_parent_fsync`. The parent reopens/reconciles through the production ABI and accepts only the authorized prefix. Assert every create crash leaves no workdir and no spawn/input-release authority unless `Journal.create_at()` returned `JournalCreateReceipt(intent_parent_dirsynced=True)`. Assert no delete attempt releases the cleanup slot before `Journal.delete_at()` or an explicit absent-after-crash reconciliation path has freshly synced the parent directory and returned `JournalDeleteReceipt(journal_unlink_parent_dirsynced=True)`; path absence alone never suffices. Injected false `DONE`, live process, missing reap proof, fabricated/wrong-kind authority, a supposedly partial journal with a complete `INTENT`, nonempty dependent artifacts, present/symlinked workdir, inode swap, and parent-dir FD swap all fail before unlink.

Run: `make native && uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_journal.py tests/unit/test_lifecycle.py tests/darwin/test_flock.py tests/darwin/test_journal_races.py tests/darwin/test_journal_lifecycle_crashes.py -v`

Expected: every matrix row passes; no workdir/process/input action precedes `JournalCreateReceipt(intent_parent_dirsynced=True)`, no cleanup/lifetime slot release precedes `JournalDeleteReceipt(journal_unlink_parent_dirsynced=True)`, and tail exhaustion yields `UNCONFIRMED` with no action-authorizing head.

- [ ] **Step 5: Commit the durable authority core**

```bash
git add native/lifecycle.h native/lifecycle.c src/claude_sdk_proxy/journal.py src/claude_sdk_proxy/lifecycle.py tests/unit/test_journal.py tests/unit/test_lifecycle.py tests/darwin/test_flock.py tests/darwin/test_journal_races.py tests/darwin/test_journal_lifecycle_crashes.py Makefile
git commit -m "feat: add bounded durable cleanup authority"
```

---

### Task 5: Prove Supervisor, Anchor, Retained-Parent, and Recovery Semantics

**Files:**
- Create: `native/claude_supervisor.c`
- Create: `native/claude_anchor.c`
- Create: `src/claude_sdk_proxy/supervisor_probe.py`
- Create: `tests/darwin/test_bootstrap.py`
- Create: `tests/darwin/test_group_cleanup.py`
- Create: `tests/darwin/test_anchor_fallback.py`
- Create: `tests/darwin/test_reconciliation.py`

**Interfaces:**
- Consumes: allocation nonce, journal/lock paths, verified real-CLI identity, and inherited control FD.
- Produces: `build/bin/claude-proxy-supervisor`, `build/bin/claude-proxy-anchor`, and `run_lifecycle_scenario(name: str) -> LifecycleEvidence`.

- [ ] **Step 1: Write failing process-ownership scenarios**

```python
@pytest.mark.parametrize("boundary", [
    "supervisor_before_identity", "anchor_before_identity", "cli_before_armed",
    "after_armed_before_exec", "after_running", "during_term_batch", "during_kill_batch",
])
def test_crash_boundary_is_reconcilable(boundary: str) -> None:
    result = run_lifecycle_scenario(boundary)
    assert result.outcome in {"done", "unconfirmed"}
    assert result.unsafe_numeric_signal_count == 0


def test_supervisorless_running_anchor_stays_unconfirmed() -> None:
    result = run_lifecycle_scenario("kill_supervisor_after_running")
    assert result.outcome == "unconfirmed"
    assert result.anchor_alive and not result.stop_or_kill_used
    assert not result.workdir_removed and not result.helper_promoted
```

- [ ] **Step 2: Compile/run and confirm failure**

Run: `make native && uv run pytest --strict-markers --forbid-skips -W error tests/darwin/test_bootstrap.py tests/darwin/test_group_cleanup.py tests/darwin/test_anchor_fallback.py tests/darwin/test_reconciliation.py -v`

Expected: build or import failure for the missing lifecycle executables.

- [ ] **Step 3: Implement gated bootstrap and retaining-parent cleanup**

The supervisor and anchor link `libclaude_proxy_lifecycle` and hold their own canonical journal/lock handles for their explicit domains. The supervisor remains outside the target group, retains the anchor as an unreaped direct child, and uses Task 4 records for every gate/action. The anchor calls `setsid()` and records `PID=PGID=SID`; the CLI member records boot/PID-start/UID/executable/PGID/SID and exact path/device/inode/hash before `ARMED`. Control EOF, failed append/CAS, or acknowledgement timeout before release calls `_exit(75)` without spawning or execing the next stage.

Immediately before `execve()` of the verified real CLI, the supervisor constructs `envp` from the Task 2 inherited-name allowlist, constructed `PATH`, explicitly enabled network-proxy names, fixed isolation variables, and optional selected `CLAUDE_CONFIG_DIR`. It omits all other inherited values. A test substitute executable dumps names and SHA-256 value fingerprints over the control pipe so `tests/darwin/test_bootstrap.py` can compare the native result with `build_child_environment()` without recording values.

The SDK invokes `claude-proxy-supervisor` with the ordinary Claude CLI argv. The shim reads only `LOCAL_PROXY_ALLOCATION_NONCE`, `LOCAL_PROXY_INSTANCE_DIR`, `LOCAL_PROXY_REAL_CLAUDE`, and `LOCAL_PROXY_CONTROL_FD` for its own bootstrap, then removes all four from real-CLI `envp`. It launches the anchor exactly as:

```text
claude-proxy-anchor --allocation-nonce HEX --instance-dir ABSOLUTE_PATH
  --control-fd FD --real-cli ABSOLUTE_PATH -- CLI_ARGV...
```

Define a bounded binary control frame in `lifecycle.h` as magic/version/type/payload-length/allocation-nonce/payload/checksum. Allow only `SUPERVISOR_IDENTITY`, `IDENTITY_ACK`, `ANCHOR_IDENTITY`, `ANCHOR_ACK`, `CLI_ARMED`, `ARMED_ACK`, `CLI_RUNNING`, `CLEANUP_REQUEST`, `SELF_TERM_REQUEST`, and `CONTROL_ERROR`; reject unknown type, wrong nonce, duplicate phase, payload over 4096 bytes, and phase regression. Every acknowledgement follows durable-head certification, not merely receipt of a frame.

Only the retaining supervisor may use `killpg(SIGSTOP)`, enumerate with Darwin process APIs, `killpg(SIGTERM)`, `killpg(SIGKILL)`, confirm absence while keeping the anchor unreaped, then `waitpid` and remove the verified workdir through admitted batches. Numeric PID/PGID observations never authorize later signals. After supervisor loss in `RUNNING`, the anchor may cooperatively `killpg(SIGTERM)` while preserving itself, then must certify `UNCONFIRMED`, stay alive, and refuse STOP/KILL, exit, helper promotion, and file removal.

- [ ] **Step 4: Run crash, identity-reuse, and reconciliation matrices**

Include parent-held zombie nonreuse, altered executable identity, reused PID simulation, unexpected descendant, wedged supervisor, stale executor, retirement replacement, interrupted batch replay, pre-`ARMED` fail-dead cleanup, post-`RUNNING` supervisorless fallback, ordinary TERM success, stubborn-child KILL, and confirmed reap. Assert every signal originates from a retained parent or authenticated self-control channel.

Run: `make native && uv run pytest --strict-markers --forbid-skips -W error tests/darwin/test_bootstrap.py tests/darwin/test_group_cleanup.py tests/darwin/test_anchor_fallback.py tests/darwin/test_reconciliation.py -v`

Expected: retaining-supervisor cases reach `DONE`; supervisorless `RUNNING` reaches persistent `UNCONFIRMED`; no test signals a reused/observed-only identifier.

- [ ] **Step 5: Commit the lifecycle executables**

```bash
git add native/claude_supervisor.c native/claude_anchor.c src/claude_sdk_proxy/supervisor_probe.py tests/darwin Makefile
git commit -m "test: prove supervisor and anchor cleanup ownership"
```

---

### Task 6: Gate Every Child on Existing-Login Attestation Before Model Input

**Files:**
- Create: `src/claude_sdk_proxy/attestation.py`
- Create: `tests/unit/test_attestation.py`
- Create: `tests/live/test_child_attestation.py`
- Create: `tests/live/test_preinput_gate.py`

**Interfaces:**
- Consumes: public SDK `SystemMessage(subtype="init")`, verified environment/executable evidence, and supervisor control trace.
- Produces: `ChildAttestation`, `extract_child_attestation(event, manifest) -> ChildAttestation`, and `AttestationGate.release(turn) -> None`.

- [ ] **Step 1: Write failing positive and negative attestation tests**

```python
def test_api_key_source_never_opens_gate(manifest: FeasibilityManifest) -> None:
    event = init_event(auth_source="api_key", provider="anthropic", endpoint="default")
    with pytest.raises(AttestationError, match="existing_claude_login"):
        extract_child_attestation(event, manifest)


def test_user_input_waits_for_attestation(gate: AttestationGate) -> None:
    gate.queue_turn(system="CANARY-SYSTEM", blocks=[{"type": "text", "text": "CANARY-USER"}])
    assert gate.relayed_model_bytes == 0
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_attestation.py tests/live/test_child_attestation.py tests/live/test_preinput_gate.py -v`

Expected: unit import failure; live tests skip unless explicitly enabled.

- [ ] **Step 3: Implement the fail-closed evidence parser and gate**

Accept only the versioned public, non-secret init fields recorded for SDK `0.2.148`/CLI `2.1.251`: exact executable identity, environment fingerprint, provider/endpoint selection, and auth-source enum `existing_claude_login`. Missing, duplicate, unknown, API-key, API-key-helper, cloud-provider, custom-endpoint, or changed evidence raises `AttestationError`. Store field names/enums/hashes only.

Connect the SDK child without releasing a user turn. The supervisor trace permits local serialization of immutable options but counts bytes delivered to model/network peers; no caller system string, MCP schema, or user block may cross before attestation. Prove either the init evidence states source immutability or call `AttestationGate.revalidate()` immediately before every turn. If the public runtime cannot expose adequate evidence before caller configuration reaches a network peer, record the core gate false and stop Phase 1 rather than inspecting credentials or private runtime data.

- [ ] **Step 4: Run live positive and override-negative cases**

Run: `RUN_LIVE_CLAUDE_TESTS=1 uv run pytest --strict-markers --forbid-skips -W error tests/live/test_child_attestation.py tests/live/test_preinput_gate.py -v -s`

Expected: existing login opens the gate; API-key/OAuth/provider/endpoint override cases, non-secret profile changes between startup/connection/turns, absent evidence, and ambiguous evidence close the child with zero released model turns.

- [ ] **Step 5: Commit the per-child provenance gate**

```bash
git add src/claude_sdk_proxy/attestation.py tests/unit/test_attestation.py tests/live/test_child_attestation.py tests/live/test_preinput_gate.py
git commit -m "test: require per-child existing-login attestation"
```

---

### Task 7: Prove Prompt Purity, Compaction Suppression, and Path-Safe Persistence

**Files:**
- Create: `src/claude_sdk_proxy/path_policy.py`
- Create: `src/claude_sdk_proxy/probes.py`
- Create: `src/claude_sdk_proxy/probe_cli.py`
- Create: `tests/unit/test_redaction.py`
- Create: `tests/unit/test_path_policy.py`
- Create: `tests/live/test_prompt_purity.py`
- Create: `tests/live/test_compaction.py`

**Interfaces:**
- Consumes: attested client, real login root metadata, and proxy-owned temporary workdir.
- Produces: `ProbeResult.redacted_dict()`, `PathPolicy.classify(path) -> PathClass`, and `run_prompt_purity_probe() -> ProbeResult`.

- [ ] **Step 1: Write failing redaction/path-policy tests**

```python
def test_report_never_contains_content_or_credentials() -> None:
    result = ProbeResult("purity", False, {
        "system_prompt": "SYSTEM-CANARY", "messages": ["USER-CANARY"],
        "authorization": "Bearer SECRET", "shape": {"blocks": 1},
    })
    encoded = json.dumps(result.redacted_dict())
    assert all(value not in encoded for value in ("SYSTEM-CANARY", "USER-CANARY", "SECRET"))


def test_credential_paths_are_metadata_only(policy: PathPolicy) -> None:
    assert policy.classify(Path(".credentials.json")) is PathClass.CREDENTIAL_METADATA_ONLY
    assert policy.may_open_content(Path(".credentials.json")) is False
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_redaction.py tests/unit/test_path_policy.py -v`

Expected: FAIL because probe and policy modules are absent.

- [ ] **Step 3: Implement exact content redaction and versioned path policy**

Redact all credential-key fragments and all caller-content keys including `system`, `system_prompt`, `messages`, `prompt`, `text`, `tool_input`, and `tool_result`; redact exception messages completely. Snapshot relative path, inode, mode, size, and nanosecond mtime only for known credential paths. Content-scan only proxy-owned safe paths and newly created paths classified noncredential; unknown new paths fail without opening. The real login root is never modified for canaries. A synthetic-root test may plant user-level canaries but cannot affect the core verdict.

- [ ] **Step 4: Run independent live purity gates**

Use unique harmless project-workdir canaries for `CLAUDE.md`, settings, skills, agents, MCP, memory, plugin, and hook sentinel. Require empty advertised tools/MCP/skills/agents, observable absence of attribution, no ambient canary, and exact adjacent structured text-block hashes/order. Force a low compaction threshold with `DISABLE_COMPACT=1`; require public context usage to report auto-compaction disabled, zero `compact_boundary`, and no summary-shaped event. Require explicit context exhaustion rather than summary injection.

Run: `RUN_LIVE_CLAUDE_TESTS=1 uv run pytest --strict-markers --forbid-skips -W error tests/live/test_prompt_purity.py tests/live/test_compaction.py -v -s`

Expected: all core cases pass on the real existing login; prohibited persistent content and leaked temporary artifacts are absent. Synthetic-root authentication failure is recorded as supplemental false, not core false.

- [ ] **Step 5: Commit prompt-purity probes**

```bash
git add src/claude_sdk_proxy/path_policy.py src/claude_sdk_proxy/probes.py src/claude_sdk_proxy/probe_cli.py tests/unit/test_redaction.py tests/unit/test_path_policy.py tests/live/test_prompt_purity.py tests/live/test_compaction.py
git commit -m "test: prove prompt purity and persistence policy"
```

---

### Task 8: Prove Exact Model Identity, Native Sessions, Streaming, and Thinking Tuples

**Files:**
- Modify: `src/claude_sdk_proxy/attestation.py`
- Modify: `src/claude_sdk_proxy/probes.py`
- Create: `src/claude_sdk_proxy/usage_evidence.py`
- Create: `tests/unit/test_model_identity.py`
- Create: `tests/unit/test_usage_evidence.py`
- Create: `tests/live/test_model_identity.py`
- Create: `tests/live/test_session_continuity.py`
- Create: `tests/live/test_streaming.py`
- Create: `tests/live/test_thinking.py`
- Create: `tests/live/test_usage.py`

**Interfaces:**
- Consumes: attested client and configured `public_alias -> exact_backend_model_id` map.
- Produces: `ModelIdentityGate.observe(event) -> tuple[CanonicalEvent, ...]`, `UsageScalarKind`, `UsageOperationClass`, `UsageDialect`, `UsageMappingFailure`, exact-budget `UsageTupleKey`, `UsageFieldSpec`, `UsageIdentityBinding`, `UsageDerivedField`, `DialectUsageMapping`, `UsageEvidenceRow`, `UsageEvidenceSchema`, `run_usage_probe()`, `run_session_probe()`, `run_stream_probe()`, and `run_thinking_probe()`.

- [ ] **Step 1: Write failing pre-content identity tests**

```python
def test_content_is_buffered_until_exact_model_identity() -> None:
    gate = ModelIdentityGate(expected="claude-sonnet-4-5-exact")
    assert gate.observe(text_delta("secret")) == ()
    with pytest.raises(ModelIdentityError):
        gate.observe(message_start(model="fallback-model"))
    assert gate.released_content_count == 0


def test_usage_tuple_budget_is_exact_and_round_trips(valid_usage_row_json) -> None:
    enabled = {
        **valid_usage_row_json,
        "key": {
            **valid_usage_row_json["key"],
            "thinking_mode": "enabled",
            "budget_tokens": 4096,
        },
    }
    row = UsageEvidenceSchema.from_json({"schema_version": 1, "rows": [enabled]}).rows[0]
    assert row.key.budget_tokens == 4096
    assert row.to_json()["key"]["budget_tokens"] == 4096

    changed = UsageEvidenceSchema.from_json({
        "schema_version": 1,
        "rows": [{**enabled, "key": {**enabled["key"], "budget_tokens": 4097}}],
    }).rows[0]
    assert changed.key != row.key
    assert changed.row_digest != row.row_digest


def test_usage_tuple_rejects_noncanonical_budget_identity(valid_usage_row_json) -> None:
    for key in (
        {**valid_usage_row_json["key"], "thinking_mode": "null", "budget_tokens": 4096},
        {**valid_usage_row_json["key"], "thinking_mode": "null", "effort": "high"},
        {**valid_usage_row_json["key"], "thinking_mode": "enabled", "budget_tokens": None},
        {**valid_usage_row_json["key"], "thinking_mode": "enabled", "budget_tokens": 0},
        {**valid_usage_row_json["key"], "thinking_mode": "enabled", "budget_tokens": True},
        {**valid_usage_row_json["key"], "thinking_mode": "adaptive", "budget_tokens": 4096},
        {**valid_usage_row_json["key"], "budget_class": "tokens:4096"},
    ):
        with pytest.raises(
            EvidenceSchemaError,
            match="thinking identity|budget_tokens|effort|unknown field",
        ):
            UsageEvidenceSchema.from_json({
                "schema_version": 1, "rows": [{**valid_usage_row_json, "key": key}],
            })
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_model_identity.py -v`

Expected: FAIL because `ModelIdentityGate` is absent.

- [ ] **Step 3: Implement identity buffering and redacted semantic probes**

Buffer every event until the first authoritative public message-start/assistant envelope identifies the exact configured backend ID. Require every later model-bearing event to agree. Missing, fallback, or mismatched identity discards buffered content and raises `ModelIdentityError`; no success framing is releasable. Generic moving aliases are forbidden in the manifest.

For one `ClaudeSDKClient`, issue two completed turns and prove the second remembers a nonce without reinjecting history. Record event type/index/order, SDK usage fields, stop shape, partial-event timing, and one non-secret session-ID count. Test streaming/nonstreaming semantic equivalence. Probe thinking `null`, disabled, adaptive, and enabled over every supported effort and exact budget boundary; preserve block order, payload hashes, signatures, delta order, stop, and usage. Unknown shapes mark only that exact tuple false.

`run_usage_probe()` builds a typed, content-free schema for every exact `(runtime tuple, backend model ID, thinking mode, effort, budget_tokens, operation class)` row. `budget_tokens` is the exact request integer, never a bucket, range, label, or formatted surrogate. The canonical no-thinking key is exactly `(thinking_mode="null", effort=None, budget_tokens=None)`; an enabled key requires a positive JSON integer `budget_tokens` (with `bool` rejected) and the exact observed effort-or-`None`. Disabled/adaptive observation rows have `budget_tokens=None` and cannot borrow an enabled budget. `budget_class` is not a V1 field and is rejected as unknown rather than derived independently. Task 8 records `ordinary`; Task 9 extends the same schema with `tool_use_boundary` and `post_tool_result` rows. Each `UsageFieldSpec` records one exact nested SDK leaf path, one supported scalar kind (`nonnegative_integer`, `string`, `boolean`, or an explicitly nullable form), and required/optional presence. Nested SDK objects are represented by leaves; no object is an untyped dictionary.

Operation classes are mutually exclusive result contexts: `tool_use_boundary` applies whenever the normalized SDK stop is tool use, including a later tool cycle; `post_tool_result` applies to a non-tool terminal result produced after resolving a pending result set; `ordinary` applies to a non-tool terminal result produced without a preceding result set. The backend selects the class from validated actor context plus normalized stop—never from usage shape—and a tool-capable session binds all classes it might produce before allocation.

Public mapping is a separate per-dialect proof, not a field on the SDK schema. Each `DialectUsageMapping` is either explicitly false or maps every allowlisted SDK leaf exactly once to a legal public leaf for that dialect. An identity binding may rename a path but may not coerce, merge, split, or discard its value. No two bindings may share an SDK path or public path, and no public scalar path may equal or be a prefix of another mapped or derived path. A derived public field is allowed only when the public dialect requires it and the mapping names a deterministic operation and all source SDK paths; V1 supports only `checked_sum` over required, present, nonnegative integer leaves. Derived fields are public aggregates, never represented as SDK-supplied leaves. In particular, OpenAI `total_tokens` is identity-mapped if the SDK supplies an exact total leaf; otherwise it may be a schema-authorized checked sum of the exact prompt/completion source leaves. Never synthesize zero, an optional counter, nested detail, or any other SDK-looking value. Checked sums reject missing/nullable sources, `bool`, negative values, and configured integer-bound overflow.

A dialect row passes only when streaming and non-streaming SDK results expose the same complete leaf set/types and the mapping is lossless and representable by that dialect's documented usage schema. Optional absence omits only that leaf's public identity binding at render time; optional presence is emitted exactly. `None` is permitted only when both the SDK field and public leaf are nullable. Nested detail leaves are validated and mapped individually. Unknown SDK leaves/types, missing required leaves, exact-or-prefix collisions, duplicate/conflated mappings, illegal public paths, or any non-representable leaf make only that exact runtime/model/thinking/operation/dialect mapping false. A passing mapping has an empty `failure_reasons`; a false mapping has no usable identity/derived bindings and one or more sorted typed reasons. Store schema names/types/mappings and digests, never observed usage values. A false or missing mapping is never approximated: later capability projection omits it and admission rejects it before allocation.

For the V1 OpenAI Chat Completions dialect, the complete legal usage-leaf set is exactly `prompt_tokens`, `completion_tokens`, `total_tokens`, `prompt_tokens_details.cached_tokens`, `prompt_tokens_details.audio_tokens`, `completion_tokens_details.reasoning_tokens`, `completion_tokens_details.audio_tokens`, `completion_tokens_details.accepted_prediction_tokens`, and `completion_tokens_details.rejected_prediction_tokens`. The baseline semantic bindings are SDK `input_tokens -> prompt_tokens`, `output_tokens -> completion_tokens`, and `cache_read_input_tokens -> prompt_tokens_details.cached_tokens`; a probed SDK total may bind to `total_tokens`, otherwise the only allowed total derivation is checked `input_tokens + output_tokens`. Any other binding requires a field-specific semantic proof encoded in the evidence row and a golden official-client test; matching scalar types or similar spelling is insufficient. For example, `cache_creation_input_tokens`, cost, duration, service-tier, and server-tool request counts have no V1 OpenAI usage leaf and therefore make that exact OpenAI mapping false when present in the SDK schema. They are never folded into cached tokens, renamed into an undocumented extension, or dropped. The Anthropic mapping likewise uses only public usage leaves accepted by the pinned Messages response/client schema; each SDK leaf has its own identity binding, never a catch-all object.

```python
class UsageScalarKind(StrEnum):
    NONNEGATIVE_INTEGER = "nonnegative_integer"
    STRING = "string"
    BOOLEAN = "boolean"


class UsageOperationClass(StrEnum):
    ORDINARY = "ordinary"
    TOOL_USE_BOUNDARY = "tool_use_boundary"
    POST_TOOL_RESULT = "post_tool_result"


class UsageDialect(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class UsageMappingFailure(StrEnum):
    SDK_SHAPE_UNSTABLE = "sdk_shape_unstable"
    UNREPRESENTABLE_SDK_FIELD = "unrepresentable_sdk_field"
    ILLEGAL_PUBLIC_PATH = "illegal_public_path"
    PATH_COLLISION = "path_collision"
    NULLABILITY_MISMATCH = "nullability_mismatch"
    INVALID_DERIVATION = "invalid_derivation"


@dataclass(frozen=True, slots=True)
class UsageTupleKey:
    runtime_digest: str
    backend_model_id: str
    thinking_mode: Literal["null", "disabled", "adaptive", "enabled"]
    effort: str | None
    budget_tokens: int | None
    operation_class: UsageOperationClass


@dataclass(frozen=True, slots=True)
class UsageFieldSpec:
    sdk_path: tuple[str, ...]
    kind: UsageScalarKind
    required: bool
    nullable: bool


@dataclass(frozen=True, slots=True)
class UsageIdentityBinding:
    sdk_path: tuple[str, ...]
    public_path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UsageDerivedField:
    public_path: tuple[str, ...]
    operation: Literal["checked_sum"]
    source_sdk_paths: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class DialectUsageMapping:
    dialect: UsageDialect
    identity_bindings: tuple[UsageIdentityBinding, ...]
    derived_fields: tuple[UsageDerivedField, ...]
    passed: bool
    failure_reasons: tuple[UsageMappingFailure, ...]
    mapping_digest: str


@dataclass(frozen=True, slots=True)
class UsageEvidenceRow:
    key: UsageTupleKey
    fields: tuple[UsageFieldSpec, ...]
    sdk_shape_passed: bool
    dialect_mappings: tuple[DialectUsageMapping, ...]
    row_digest: str


@dataclass(frozen=True, slots=True)
class UsageEvidenceSchema:
    schema_version: Literal[1]
    rows: tuple[UsageEvidenceRow, ...]
    schema_digest: str

    def require_mapping(
        self, key: UsageTupleKey, dialect: UsageDialect,
    ) -> tuple[UsageEvidenceRow, DialectUsageMapping]: ...
```

- [ ] **Step 4: Run positive and injected-negative suites**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_model_identity.py tests/unit/test_usage_evidence.py -v`

Expected: injected missing/extra/type-changed/duplicate/prefix-colliding SDK and public paths fail only their exact tuple/dialect, and every passing ordinary row has stable row/mapping/schema digests plus exhaustive lossless Anthropic and OpenAI mappings. Cases cover optional absent/present/null, nested detail leaves, explicit SDK totals, checked-sum OpenAI totals, illegal or overflowing sums, and an unrepresentable SDK leaf producing an explicit false dialect mapping.

Run: `RUN_LIVE_CLAUDE_TESTS=1 CLAUDE_PROXY_TEST_MODEL_ID=claude-sonnet-4-5-20250929 uv run pytest --strict-markers --forbid-skips -W error tests/live/test_model_identity.py tests/live/test_session_continuity.py tests/live/test_streaming.py tests/live/test_thinking.py tests/live/test_usage.py -v -s`

Expected: exact live identity, two-turn continuity, and text streaming pass; injected missing/mismatch/fallback events release zero content; thinking output records a complete model/configuration tuple matrix without treating fallback as support; usage evidence records every ordinary SDK leaf name/type and separate Anthropic/OpenAI mapping verdicts for non-streaming and streaming without storing observed values. A dialect with no legal exhaustive mapping is recorded false rather than weakened.

- [ ] **Step 5: Commit the core model/session evidence**

```bash
git add src/claude_sdk_proxy/attestation.py src/claude_sdk_proxy/probes.py src/claude_sdk_proxy/usage_evidence.py tests/unit/test_model_identity.py tests/unit/test_usage_evidence.py tests/live/test_model_identity.py tests/live/test_session_continuity.py tests/live/test_streaming.py tests/live/test_thinking.py tests/live/test_usage.py
git commit -m "test: prove exact Agent SDK model semantics"
```

---

### Task 9: Execute the Optional SDK-Level Suspended External-Tool Gate

**Files:**
- Modify: `src/claude_sdk_proxy/validated.py`
- Modify: `src/claude_sdk_proxy/probes.py`
- Modify: `src/claude_sdk_proxy/usage_evidence.py`
- Modify: `tests/unit/test_usage_evidence.py`
- Create: `tests/unit/test_sdk_tool_evidence.py`
- Create: `tests/live/test_tool_bridge.py`

**Interfaces:**
- Consumes: pinned SDK `create_sdk_mcp_server`, immutable tool schema, one actor-owned receive loop, and Task 8's `UsageEvidenceSchema`/mapping validator.
- Produces: `ToolNameError`, `SdkMcpNamingRule`, `SdkToolEvidenceKey`, `SdkToolEvidenceRecord`, `SdkToolEvidenceManifest`, `run_tool_bridge_probe(model_id: str, scenario: str, delay_seconds: int) -> ProbeResult`, tool-operation usage rows for `tool_use_boundary` and `post_tool_result`, and a complete exact-SDK/runtime/model evidence record. The SDK-tool key has no public HTTP dialect or proxy streaming field; the usage schema retains its separate per-dialect mappings.

- [ ] **Step 1: Write failing correlation and suspension tests**

```python
# tests/unit/test_sdk_tool_evidence.py
def test_sdk_tool_evidence_schema_has_no_http_dialect_or_streaming_key(valid_sdk_tool_record) -> None:
    manifest = SdkToolEvidenceManifest.from_records((valid_sdk_tool_record,))
    assert set(manifest.only_record.gates) == REQUIRED_SDK_TOOL_GATES
    assert not hasattr(manifest.only_record.key, "dialect")
    assert not hasattr(manifest.only_record.key, "streaming")


@pytest.mark.parametrize("caller_name", ["echo", "snake_case", "dash-name", "x" * 64])
def test_versioned_sdk_naming_rule_derives_every_supported_name(
    validated_naming_rule, caller_name
) -> None:
    generated = validated_naming_rule.derive(caller_name)
    assert generated == validated_naming_rule.representative_observations[caller_name]


@pytest.mark.parametrize("caller_name", ["", "naïve", "工具", "x" * 65])
def test_public_tool_name_subset_rejects_unicode_empty_and_over_bound(
    validated_naming_rule, caller_name
) -> None:
    with pytest.raises(ToolNameError):
        validated_naming_rule.derive(caller_name)


# tests/live/test_tool_bridge.py
@pytest.mark.anyio
@pytest.mark.parametrize("delay_seconds", [1, 60, 600])
async def test_callback_survives_response_gap(delay_seconds: int) -> None:
    result = await run_tool_bridge_probe(EXACT_MODEL, "delayed_sdk_callback", delay_seconds)
    assert result.passed and result.evidence["later_user_turn_succeeded"]


@pytest.mark.anyio
async def test_identical_parallel_calls_correlate_in_reverse() -> None:
    result = await run_tool_bridge_probe(EXACT_MODEL, "parallel_reverse", 1)
    assert result.evidence["distinct_public_ids"] == 2
    assert result.evidence["reverse_results_correlated"] is True


@pytest.mark.parametrize("operation_class", ["tool_use_boundary", "post_tool_result"])
def test_tool_usage_rows_have_exhaustive_dialect_verdicts(
    completed_usage_schema, operation_class
) -> None:
    row = completed_usage_schema.only_tool_row(operation_class)
    assert row.sdk_shape_passed is True
    assert {mapping.dialect.value for mapping in row.dialect_mappings} == {
        "anthropic", "openai",
    }
    for mapping in row.dialect_mappings:
        if mapping.passed:
            assert {binding.sdk_path for binding in mapping.identity_bindings} == {
                field.sdk_path for field in row.fields
            }


@pytest.mark.anyio
async def test_generated_sdk_mcp_rule_and_representative_names_are_recorded_without_override() -> None:
    result = await run_tool_bridge_probe(EXACT_MODEL, "generated_name_rule", 1)
    assert result.evidence["naming_rule_version"] == 1
    assert result.evidence["server_identity"] == "caller_tools_v1"
    assert set(result.evidence["observed_generated_names"]) == {
        "echo", "snake_case", "dash-name", "x" * 64,
    }
    for caller_name, generated_name in result.evidence["observed_generated_names"].items():
        assert result.naming_rule.derive(caller_name) == generated_name
    assert "CLAUDE_AGENT_SDK_MCP_NO_PREFIX" not in result.child_environment
```

- [ ] **Step 2: Run the short cases and confirm failure**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_sdk_tool_evidence.py -v && RUN_LIVE_CLAUDE_TESTS=1 uv run pytest --strict-markers --forbid-skips -W error tests/live/test_tool_bridge.py -v -k 'not 600' -s`

Expected: import or assertion failure before the evidence schema and probe exist.

- [ ] **Step 3: Implement the actor-owned suspended callback probe**

Create one in-process MCP server with fixed versioned server identity `caller_tools_v1`. Let the pinned SDK generate every MCP tool name with its default behavior; do not set `CLAUDE_AGENT_SDK_MCP_NO_PREFIX` or any other naming override. Probe representative public names `echo`, `snake_case`, `dash-name`, and the exact 64-ASCII-character boundary, observe the exact canonical-definition-to-SDK-name mapping through documented public SDK configuration/events, and prove each generated name invokes only its intended handler. Define the V1 caller-name subset as nonempty ASCII `[A-Za-z0-9_-]{1,64}` and reject empty, Unicode, whitespace/control/separator, duplicate, and over-bound names before SDK construction. If the pinned SDK's observed generated mapping is not a deterministic injective function of `(rule_version=1, server_identity="caller_tools_v1", caller_name)` for every accepted probe name, record the optional gate false.

Encode that observed behavior as `SdkMcpNamingRule(version, server_identity, caller_name_pattern, caller_name_max_bytes, generated_name_template_or_algorithm, representative_observations)`. `derive(caller_name)` first enforces the public subset, then deterministically returns the expected SDK name; `observe_generated_names(server, definitions)` reads the same documented public SDK surface used by the live probe. Do not treat the representative mapping as an allowlist of tool names: it is evidence for the versioned rule used to derive and validate an arbitrary session's accepted definitions.

Use an accepted representative handler for the suspension/correlation cases. Its handler records arguments, signals callback start, and awaits an actor-owned future while the single receive loop continues draining. Resolve two identical-argument calls in reverse order using only documented public correlation IDs. Never execute the tool in the proxy, start a second receive iterator, use a private JSON-RPC ID, flatten a transcript, or infer correlation from argument equality.

Define the exact record schema in `validated.py`. `SdkToolEvidenceKey` has exactly `runtime_digest`, `sdk_version`, `cli_version`, `cli_executable_device`, `cli_executable_inode`, `cli_executable_sha256`, `darwin_version`, `darwin_build`, `boot_id`, `mount_identity`, `backend_class`, `auth_class`, `semantic_class`, and `backend_model_id`. Its `mount_identity` is the complete Task 2 object with exactly `filesystem_type`, `is_local`, `mount_device`, `mount_fsid`, `mount_flags`, and `runtime_root_st_dev`. `SdkToolEvidenceRecord` contains exactly `schema_version`, that `key`, `naming_rule`, and `gates`. The naming-rule object has exactly `version`, `server_identity`, `caller_name_pattern`, `caller_name_max_bytes`, `generated_name_template_or_algorithm`, and `representative_observations`. The gate object has exactly the following keys and literal boolean values:

```python
REQUIRED_SDK_TOOL_GATES = frozenset({
    "generated_name_deterministic",
    "generated_name_injective",
    "caller_name_rejection",
    "caller_name_bounds",
    "callback_round_trip",
    "distinct_public_id_propagation",
    "parallel_reverse_correlation",
    "single_receive_loop_suspension",
    "cancellation",
    "interrupt",
    "shutdown",
    "sdk_event_ordering",
    "sdk_result_usage_shape",
    "argument_delta_fidelity",
    "schema_immutability",
    "suspension_600_seconds",
})
```

Reject missing, extra, or non-boolean SDK gates, duplicate tuple keys, unknown naming-rule versions, changed server identity/algorithm, or observations inconsistent with `derive()`.

During the same live loop, extend Task 8's usage evidence with exact `tool_use_boundary` and `post_tool_result` rows for each probed runtime/model/thinking tuple. Observe both streaming and non-streaming terminal SDK usage shapes; run the identical field, collision, optional/null, derived-total, and representability validator; and record separate Anthropic/OpenAI mapping verdicts. The SDK tool record's `SDK result/usage shape` gate requires those SDK shapes to be complete and stable, but does not make a false public dialect mapping true. Do not include SSE boundaries, HTTP disconnect/retry behavior, proxy TTLs/deadlines, or proxy cleanup in this Phase 0 record; Phase 3 must prove and compose those production properties separately.

- [ ] **Step 4: Run the complete optional matrix**

Run: `RUN_LIVE_CLAUDE_TESTS=1 uv run pytest --strict-markers --forbid-skips -W error tests/live/test_tool_bridge.py -v -k 'not 600' -s`

Expected: each short matrix row records a boolean without weakening assertions, and each tool boundary/result usage row records exact streaming/non-streaming SDK shapes plus explicit Anthropic/OpenAI mapping verdicts.

Run: `RUN_LIVE_CLAUDE_TESTS=1 uv run pytest --strict-markers --forbid-skips -W error tests/live/test_tool_bridge.py -v -k '600' -s`

Expected: the callback remains suspended for 600 seconds without timeout, hidden query, duplicate invocation, or session loss. Any false/missing row records tools disabled but does not change the core verdict.

- [ ] **Step 5: Commit the optional tool evidence code**

```bash
git add src/claude_sdk_proxy/validated.py src/claude_sdk_proxy/probes.py src/claude_sdk_proxy/usage_evidence.py tests/unit/test_usage_evidence.py tests/unit/test_sdk_tool_evidence.py tests/live/test_tool_bridge.py
git commit -m "test: gate suspended external tool bridging"
```

---

### Task 10: Generate and Enforce the Final Feasibility Manifest

**Files:**
- Modify: `src/claude_sdk_proxy/validated.py`
- Modify: `src/claude_sdk_proxy/probe_cli.py`
- Modify: `docs/feasibility/README.md`
- Create: `docs/feasibility/validated-environment.json`
- Create: `tests/unit/test_validated_environment.py`

**Interfaces:**
- Consumes: redacted evidence from Tasks 1–9.
- Produces: `load_manifest(path: Path) -> FeasibilityManifest`, `load_usage_evidence(manifest: FeasibilityManifest) -> UsageEvidenceSchema`, `load_sdk_tool_evidence(manifest: FeasibilityManifest) -> SdkToolEvidenceManifest`, `require_core_gates(manifest) -> None`, `sdk_tool_gate_passed(manifest, exact_model_id: str) -> bool`, `canonical_evidence_json(value) -> bytes`, `sdk_tool_record_digest(record: SdkToolEvidenceRecord) -> str`, `phase0_prerequisite_digest(manifest, public_alias, exact_backend_model_id) -> str`, and immutable `Phase0PrerequisiteDigestResolver`.

- [ ] **Step 1: Write a failing complete-schema test**

```python
def test_manifest_has_every_core_domain() -> None:
    manifest = load_manifest(Path("docs/feasibility/validated-environment.json"))
    assert set(manifest.core_gates) == {
        "personal_subscription_policy", "exact_runtime_tuple", "darwin_local_apfs",
        "required_sync_primitives", "bsd_flock_model", "bounded_journal",
        "root_reconciliation_lock", "instance_lifetime_lock",
        "owner_record_create", "owner_record_replace",
        "durable_head_certification", "cleanup_automaton", "retaining_supervisor",
        "anchor_unconfirmed_fallback", "exact_environment", "per_child_auth_attestation",
        "preinput_network_gate", "auth_source_lifetime", "prompt_isolation",
        "attribution_absent_observable", "compaction_disabled", "path_safe_persistence",
        "structured_user_input", "exact_backend_model", "native_session_continuity",
        "streaming_event_contract", "exact_usage_schema",
    }
    assert all(manifest.core_gates.values())


def test_phase0_prerequisite_digest_binds_exact_model_runtime_and_mount(
    validated_manifest
) -> None:
    original = phase0_prerequisite_digest(
        validated_manifest, "sonnet", "claude-sonnet-4-6-20260801"
    )
    for changed in (
        validated_manifest.with_mount_device("disk9s9"),
        validated_manifest.with_mount_fsid("ffff:eeee"),
        validated_manifest.with_runtime_root_st_dev(999),
        validated_manifest.with_os_build("24G91"),
        validated_manifest.with_sdk_version("0.2.149"),
        validated_manifest.with_cli_hash("00" * 32),
        validated_manifest.with_backend_model("sonnet", "claude-other-exact"),
    ):
        assert phase0_prerequisite_digest(
            changed, "sonnet", changed.model_map["sonnet"]
        ) != original


def test_phase0_digest_resolver_rejects_swapped_or_missing_models(two_model_manifest) -> None:
    resolver = Phase0PrerequisiteDigestResolver.from_manifest(two_model_manifest)
    assert resolver.resolve("sonnet", two_model_manifest.model_map["sonnet"]) != resolver.resolve(
        "opus", two_model_manifest.model_map["opus"]
    )
    with pytest.raises(ManifestError, match="exact model prerequisite digest"):
        resolver.with_swapped_digests_for_test("sonnet", "opus").validate(two_model_manifest)


def test_sdk_tool_record_digest_is_canonical_and_binds_every_record_field(
    valid_sdk_tool_record
) -> None:
    original = sdk_tool_record_digest(valid_sdk_tool_record)
    assert sdk_tool_record_digest(
        valid_sdk_tool_record.with_reordered_serialized_fields()
    ) == original
    reloaded = load_sdk_tool_evidence(
        manifest_with_sdk_records((valid_sdk_tool_record,))
    ).only_record
    assert sdk_tool_record_digest(reloaded) == original
    for field in (
        "runtime_digest", "sdk_version", "cli_version",
        "cli_executable_device", "cli_executable_inode", "cli_executable_sha256",
        "darwin_version", "darwin_build", "boot_id", "mount_identity",
        "backend_class", "auth_class", "semantic_class", "backend_model_id",
        "representative_observations", "gate_value",
    ):
        changed = valid_sdk_tool_record.with_digest_field_mutation(field)
        assert sdk_tool_record_digest(changed) != original
    for gate in REQUIRED_SDK_TOOL_GATES:
        changed = valid_sdk_tool_record.with_gate(
            gate, not valid_sdk_tool_record.gates[gate]
        )
        assert sdk_tool_record_digest(changed) != original

    for invalid in (
        valid_sdk_tool_record.with_record_schema_version(2),
        valid_sdk_tool_record.with_naming_rule_version(2),
        valid_sdk_tool_record.with_changed_server_identity("other_server"),
        valid_sdk_tool_record.with_changed_caller_name_pattern(".*"),
        valid_sdk_tool_record.with_changed_caller_name_max_bytes(65),
        valid_sdk_tool_record.with_changed_algorithm("unvalidated"),
        valid_sdk_tool_record.with_unknown_gate("future_gate", True),
        valid_sdk_tool_record.with_unknown_field("record_digest", "00" * 32),
    ):
        with pytest.raises(ManifestError, match="SDK tool record schema"):
            sdk_tool_record_digest(invalid)
```

- [ ] **Step 2: Run the schema test and confirm failure**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_validated_environment.py -v`

Expected: FAIL because the final manifest does not exist.

- [ ] **Step 3: Implement deterministic manifest generation and verdict rules**

`claude-proxy-probe all --ack-personal-local-use-policy --output docs/feasibility/validated-environment.json` requires live opt-in, merges only redacted evidence, writes mode `0600` via same-directory temporary file plus `F_FULLFSYNC`, `renameat`, and parent-directory `fsync`, and never overwrites a mismatched tuple without rerunning every gate. Include policy URLs/timestamps/page hashes, UTC validation time, Python/SDK/CLI path/version/hash, macOS build/boot tuple, APFS mount identity, syscall/lock/journal/supervisor matrices, the four named root/instance/owner evidence cases from Task 3, environment allowed names/fingerprint algorithm, accepted public auth-evidence shape, path-policy version, exact alias/backend-model map, thinking tuples, the typed per-tuple usage schema/digest from Task 8, and the Task 9 SDK tool evidence matrix. Exclude prompts, response text, observed usage values, session IDs, paths to credential files, environment values, credentials, and transport bytes.

`require_core_gates()` is the conjunction of the exact key set in Step 1 and rejects missing/false keys. `load_usage_evidence()` validates Task 8/9 field kinds, nested SDK and per-dialect public paths, requiredness/nullability, exact runtime/model/thinking/operation keys, identity coverage, checked-sum sources, exact-or-prefix collisions, legal dialect paths, boolean verdicts, and mapping/row/schema digests. It enforces canonical budget identity: `null` is exactly `(effort=None, budget_tokens=None)`, `enabled` has an exact positive non-boolean integer `budget_tokens`, every non-enabled mode has `budget_tokens=None`, and `budget_class` or any other unknown key field is rejected. Exact `budget_tokens` participates in row and schema digests. `UsageEvidenceSchema.require_mapping()` returns only an exact passing row/mapping pair; missing or false mappings raise and never fall back across model, exact budget, effort, operation class, or dialect.

`exact_usage_schema` requires a stable ordinary SDK-shape row and passing ordinary Anthropic mapping for the no-thinking (`null`) tuple of every configured exact backend model, plus a structurally valid record for every probed thinking row. Ordinary OpenAI mappings and all tool-operation mappings may honestly be false without failing the Phase 1 core gate; Phase 2/3 must omit those capabilities and reject their admission. A false optional thinking row remains recorded false and does not fail the core gate. Any later accepted thinking/dialect/operation tuple must resolve one exact passing mapping, or it is unsupported. `load_sdk_tool_evidence()` validates only the exact Task 9 SDK record schema and returns its typed manifest. `sdk_tool_gate_passed()` requires every SDK gate, the exact supported `SdkMcpNamingRule` version/server identity, internally consistent representative observations, and stable tool-operation SDK usage rows for the exact runtime/model key; it returns false for missing models/keys and never treats representative names as a production allowlist. It does not convert false public usage mappings into support and neither accepts nor rejects Phase 3 dialect/framing gates because those are stored in a separate Phase 3 release-evidence manifest. A false core verdict is committed honestly and blocks later implementation plans.

Define one canonical evidence encoder for cross-phase digests. It accepts only the already schema-validated immutable JSON tree, encodes UTF-8 with object keys sorted by Unicode code point, arrays in preserved order, integers in minimal decimal form, booleans/null as JSON literals, strings with minimal JSON escaping, and no floats, duplicate keys, NaN, paths, bytes, or unknown fields.

`sdk_tool_record_digest(record)` is the only SDK-tool-record identity function exported to later phases. It accepts only a frozen `SdkToolEvidenceRecord` produced by `load_sdk_tool_evidence()` and revalidates its nested invariants. It constructs exactly this projection and no other fields:

```python
projection = {
    "digest_schema_version": 1,
    "record": {
        "schema_version": record.schema_version,
        "key": {
            "runtime_digest": record.key.runtime_digest,
            "sdk_version": record.key.sdk_version,
            "cli_version": record.key.cli_version,
            "cli_executable_device": record.key.cli_executable_device,
            "cli_executable_inode": record.key.cli_executable_inode,
            "cli_executable_sha256": record.key.cli_executable_sha256,
            "darwin_version": record.key.darwin_version,
            "darwin_build": record.key.darwin_build,
            "boot_id": record.key.boot_id,
            "mount_identity": {
                "filesystem_type": record.key.mount_identity.filesystem_type,
                "is_local": record.key.mount_identity.is_local,
                "mount_device": record.key.mount_identity.mount_device,
                "mount_fsid": record.key.mount_identity.mount_fsid,
                "mount_flags": record.key.mount_identity.mount_flags,
                "runtime_root_st_dev": record.key.mount_identity.runtime_root_st_dev,
            },
            "backend_class": record.key.backend_class,
            "auth_class": record.key.auth_class,
            "semantic_class": record.key.semantic_class,
            "backend_model_id": record.key.backend_model_id,
        },
        "naming_rule": {
            "version": record.naming_rule.version,
            "server_identity": record.naming_rule.server_identity,
            "caller_name_pattern": record.naming_rule.caller_name_pattern,
            "caller_name_max_bytes": record.naming_rule.caller_name_max_bytes,
            "generated_name_template_or_algorithm": (
                record.naming_rule.generated_name_template_or_algorithm
            ),
            "representative_observations": (
                record.naming_rule.representative_observations
            ),
        },
        "gates": record.gates,
    },
}
return sha256(
    b"claude-sdk-proxy:sdk-tool-record:v1\0"
    + canonical_evidence_json(projection)
).hexdigest()
```

`mount_flags` preserves its validated canonical tuple order, while observation and gate objects are ordered only by `canonical_evidence_json()`. No container position, public alias, dialect, streaming flag, usage row, timestamp, observed content/value, or supplied digest field participates. A schema change requires a new projection and domain. Reordered maps are stable; changing any projected leaf, tuple order, observation, gate name, or gate value changes the digest.

`load_sdk_tool_evidence()` is the sole constructor of digestible records and never accepts or stores an independently supplied record digest. Phase 3 and Phase 4 import `sdk_tool_record_digest()` from `validated.py` and use it exclusively for SDK-record identities and indexes; neither phase may reproduce this projection, hash source JSON bytes or `repr`, or introduce another SDK-record domain separator.

`phase0_prerequisite_digest()` computes `sha256(b"claude-sdk-proxy:phase0-prerequisite:v1\0" + canonical_evidence_json(projection)).hexdigest()`, where `projection` contains the Phase 0 manifest schema version, complete validated core-gate map, policy/source digests, SDK/CLI version/path identity/hash, Darwin version/build/boot tuple, exact `MountIdentity`, sync/lock/lifecycle/path-policy evidence, backend/auth/semantic classes, and the selected `(public_alias, exact_backend_model_id)`. The digest excludes only its own stored digest field and contains no secrets or raw content.

`Phase0PrerequisiteDigestResolver.from_manifest()` calls `require_core_gates()`, computes one digest for every exact alias/backend-model pair, copies them into an immutable mapping keyed by that pair, and rejects missing/extra/swapped mappings or duplicate backend ambiguity. Any Phase 0 manifest refresh, schema change, runtime/CLI change, OS/boot/mount identity change, gate change, or model-map change creates new prerequisite digests and invalidates dependent later-phase evidence until those gates are rerun.

- [ ] **Step 4: Run all validation and static checks**

Run: `make check`

Expected: native build, unit tests, Darwin tests, Ruff, and mypy pass.

Run: `RUN_LIVE_CLAUDE_TESTS=1 CLAUDE_PROXY_TEST_MODEL_ID=claude-sonnet-4-5-20250929 uv run claude-proxy-probe all --ack-personal-local-use-policy --output docs/feasibility/validated-environment.json`

Expected: a mode-`0600`, schema-valid, content-free manifest whose `core_gates` are all true before Phase 1 proceeds; tool booleans may be false.

Run: `RUN_LIVE_CLAUDE_TESTS=1 CLAUDE_PROXY_TEST_MODEL_ID=claude-sonnet-4-5-20250929 uv run pytest --strict-markers --forbid-skips -W error -v && uv run ruff check . && uv run mypy src/claude_sdk_proxy`

Expected: no enabled live test is skipped; tests and static checks pass.

- [ ] **Step 5: Commit the feasibility verdict**

```bash
git add src/claude_sdk_proxy/validated.py src/claude_sdk_proxy/probe_cli.py docs/feasibility/README.md docs/feasibility/validated-environment.json tests/unit/test_validated_environment.py
git commit -m "docs: record trusted-local feasibility verdict"
```
