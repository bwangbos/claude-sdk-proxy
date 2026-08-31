# Phase 2 OpenAI Adapter and Harness Launcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the approved OpenAI Chat Completions-shaped text subset and a harness-neutral launcher that gives one ordinary agentic-harness process one automatic linear session without exposing the proxy master key or inheriting an uncontrolled environment.

**Architecture:** The OpenAI adapter translates strict wire input into the Phase 1 canonical request and consumes the same actor, exact-byte replay, capacity, deadline, and cleanup machinery; it never imports Agent SDK types. For an external server, the launcher resolves exactly one discriminated control-auth mode—configured in-memory `LOCAL_PROXY_API_KEY`, validated generated-key file, or explicit `--no-auth`—asks the server to mint one scoped run token, then builds a deny-by-default child environment containing only the selected provider base URL and derived token. It owns the harness process group and revokes the run on every exit path; a self-hosted option transfers a generated master key in parent memory and applies the same server cleanup gate.

**Tech Stack:** Python 3.14, uv, AnyIO 4, h11 0.16, Pydantic 2, HTTPX, OpenAI Python client, the complete Phase 1 proxy, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-08-29-claude-subscription-api-proxy-design.md`

## Global Constraints

- Phase 1 deterministic and live gates must pass unchanged before Phase 2 starts.
- `/v1/messages` remains canonical; OpenAI translation depends only on canonical proxy types and actor interfaces.
- Accept text-only Chat Completions with at most one leading plain-string `system` message. Reject structured system content, `developer`, later `system`, tool/function, multimodal, and unsupported roles before allocation or mutation.
- Reject `reasoning_effort`, sampling, stop, response-format, seed, logprobs, penalties, and `n != 1`; never invent SDK usage leaves, finish reasons, choices, or reasoning semantics. OpenAI-required `total_tokens` may be rendered only by the exact Phase 0 identity binding or checked-sum derivation proved for that tuple.
- Advertise and admit an OpenAI runtime/model/thinking tuple only when its exact ordinary OpenAI usage mapping passes. Every successful non-streaming usage object—and every streaming usage object requested with `stream_options.include_usage=true`—emits all present allowlisted SDK leaves losslessly plus only schema-declared required aggregates.
- Phase 2 cannot claim an OpenAI release unless at least one configured exact model has a passing ordinary no-thinking OpenAI mapping and passes the official-client non-streaming/streaming/replay matrix. Other model/thinking tuples remain honestly unavailable.
- The OpenAI compatibility profile accepts and reports only one of `max_tokens` or `max_completion_tokens`; supplying both is invalid.
- A generated run token binds one dialect, parameter policy, immutable absolute TTL, and at most one irreversible automatic conversation.
- The launcher never reads Claude credentials, copies the full parent environment, places the master key in a harness child, persists a derived token, or emits a token in logs or argv.
- External launch resolves exactly one of `ConfiguredKeyAuth`, `GeneratedKeyFileAuth`, or `NoAuth`; conflicts and missing auth fail closed, and only explicit `--no-auth` may select unauthenticated control requests.
- `LOCAL_PROXY_API_KEY`, `LOCAL_PROXY_API_KEY_FILE`, key-file paths, and control `Authorization`/`x-api-key` values are launcher-only inputs and never enter harness argv or environment. The derived run token is the harness's sole proxy credential.
- Every configured or recovered control-secret allocation has one movable owner and is zeroized on all exits; every key-read/header scratch buffer and descriptor closes in `finally`. A generated Phase 1 key file remains the producer lifecycle's property and is durably removed before that lifecycle completes.
- Harnesses that multiplex conversations require explicit session headers or an existing request hook; the launcher makes no universal zero-modification claim.
- Harnesses call vLLM/SGLang endpoints directly; this proxy adds no local-model forwarding.
- Inherit the Phase 0 repository release-test policy and the complete Phase 1 release gate. Every mandatory Phase 2 pytest invocation—including focused red/green commands, commands chained with other tools, and aggregate official-client/harness evidence—uses `--strict-markers --forbid-skips -W error`; every async test carries a registered execution marker. A skip, xfail represented as a skip, unknown marker, warning, or unhandled coroutine makes the command non-passing.
- Phase 2 defines no optional pytest command. Any future developer-only exception must use an explicitly `dev-*`-named target, be documented as non-authoritative, be unreachable from `check`, `phase1-release`, `phase2-check`, `phase2-release`, manifest generation, and downstream release dependencies, and must never create or refresh passing evidence.

## File Map

- `src/claude_sdk_proxy/openai_adapter.py`: strict Chat Completions parsing and exact JSON/SSE rendering.
- `Makefile`: inherited Phase 0 release flags plus Phase 2 targets that depend on the complete Phase 1 release gate.
- `src/claude_sdk_proxy/router.py`: `/v1/chat/completions` dispatch through existing selection and actors.
- `src/claude_sdk_proxy/control.py`: dialect-bound run creation already implemented in Phase 1.
- `src/claude_sdk_proxy/launcher_environment.py`: deny-by-default harness environment and protected-name rules.
- `src/claude_sdk_proxy/launcher_auth.py`: discriminated external auth-mode resolution, secret lifetime, and control authorization.
- `src/claude_sdk_proxy/launcher_keyfile.py`: deterministic CLI/environment generated-key-file source resolution plus descriptor-relative file validation and reading.
- `src/claude_sdk_proxy/launcher_process.py`: child process-group signals, wait, and exact exit code.
- `src/claude_sdk_proxy/launcher.py`: control client, run mint/revoke, self-hosted server, and orchestration.
- `src/claude_sdk_proxy/server_cli.py`: `claude-proxy run` command.
- `tests/unit/test_openai_adapter.py`: request/response/field policy.
- `tests/unit/test_launcher_environment.py`: environment allowlist and secret hygiene.
- `tests/unit/test_launcher_auth.py`: exact three-mode resolution, conflicts, no downgrade, and secret lifetime.
- `tests/unit/test_launcher_keyfile.py`: key-file ownership/path/mode/symlink checks.
- `tests/integration/test_openai_http.py`: non-streaming real-socket behavior.
- `tests/integration/test_openai_stream.py`: exact SSE and replay behavior.
- `tests/integration/test_launcher_lifecycle.py`: mint/spawn/signal/revoke paths.
- `tests/integration/test_launcher_auth_modes.py`: three external control-auth modes, conflicts, and no-downgrade behavior.
- `tests/integration/test_generated_key_contract.py`: real Phase 1 producer to Phase 2 reader/control-request round trip.
- `tests/integration/test_launcher_self_hosted.py`: in-memory master-key and server cleanup.
- `tests/integration/test_official_openai_client.py`: black-box OpenAI client evidence.
- `tests/integration/test_cross_dialect.py`: canonical equivalence and dialect isolation.
- `tests/integration/test_full_history_harness.py`: representative unmodified agentic-harness fixture.
- `docs/harnesses.md`: generic launcher, direct, and explicit-session contracts.

---

### Task 1: Translate Strict OpenAI Text Requests and Non-Streaming Responses

**Files:**
- Create: `src/claude_sdk_proxy/openai_adapter.py`
- Create: `tests/unit/test_openai_adapter.py`
- Create: `tests/unit/test_openai_errors.py`

**Interfaces:**
- Consumes: `CanonicalRequest`, `CanonicalMessage`, `CommittedReply`, `ImmutableHttpArtifact`, `ParameterPolicy`, `ModelMap`, `UsageMappingResolver`, `CanonicalUsage`, and `ProxyError` from Phase 1 plus the admission-resolved ordinary OpenAI `UsageEvidenceRow`/`DialectUsageMapping`.
- Produces: `parse_chat_completion_request()`, `render_chat_completion_artifact()`, `render_openai_error()`, and `OpenAIStreamEncoder`.

- [ ] **Step 1: Write request translation tests**

```python
BASE = {
    "model": "sonnet",
    "messages": [
        {"role": "system", "content": "caller system"},
        {"role": "user", "content": "hello"},
    ],
    "max_completion_tokens": 256,
}


def test_plain_system_and_user_text_are_preserved() -> None:
    parsed = parse_chat_completion_request(
        BASE, ParameterPolicy.MESSAGES_COMPAT, MODEL_MAP, USAGE_MAPPING_RESOLVER
    )
    assert parsed.request.system == "caller system"
    assert parsed.request.messages[-1].content[0].text == "hello"
    assert parsed.ignored_parameters == ("max_completion_tokens",)
```

Add explicit cases for no system message, full user/assistant history, adjacent-role rejection according to the canonical transcript grammar, and preservation of whitespace/Unicode/NUL rejection.

- [ ] **Step 2: Write unsupported-field tests**

Parameterize structured system content, second/later system, `developer`, `tool`, `function`, multimodal content, tools/tool choice, `reasoning_effort`, temperature, top-p, stop, response format, seed, logprobs, penalties, `n=2`, both token-limit fields, unknown fields, unavailable model aliases, and a model/thinking tuple whose ordinary OpenAI usage mapping is false or missing. Assert `400 unsupported_parameter`, `404 model_not_configured`, or the stable dialect-capability error before fingerprinting/session/allocation counters change. Accept only absent `stream_options` or exactly `{"include_usage": true|false}` on a streaming request; reject it for non-streaming requests and reject unknown nested keys.

- [ ] **Step 3: Write exact response/error golden tests**

Given model alias `sonnet`, text `hello`, canonical stop `end_turn`, and a passing ordinary OpenAI mapping, assert `chatcmpl_lp_*`, integer `created`, one choice at index 0, assistant string content, and `finish_reason="stop"`. Supply every allowlisted SDK leaf—including optional present/null/zero and nested detail fields—and assert each identity-bound value appears once at its exact OpenAI public path. Cover an SDK-supplied total identity binding and, in a separate row with no SDK total, the exact checked sum `prompt_tokens + completion_tokens -> total_tokens`; the aggregate is public-only and does not enter `CanonicalUsage.fields`. Assert byte-identical rendering after snapshot replay.

Inject an unknown SDK leaf, missing required leaf, changed scalar kind, duplicate/exact-prefix public collision, illegal OpenAI path, absent optional, nullable-to-nonnullable mapping, missing/nullable/negative/overflow checked-sum source, wrong row/dialect/mapping digest, and an unrepresentable SDK leaf. Optional absence alone omits that identity field; every other invalid case returns `502 sdk_protocol_error` with no partial usage artifact. Verify OpenAI-shaped envelopes for every Phase 1 error code and unknown stop reason.

- [ ] **Step 4: Run tests and confirm missing adapter**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_openai_adapter.py tests/unit/test_openai_errors.py -v`

Expected: FAIL because `openai_adapter.py` does not exist.

- [ ] **Step 5: Implement exhaustive wire models and renderers**

```python
@dataclass(frozen=True, slots=True)
class ParsedOpenAIRequest:
    request: CanonicalRequest
    ignored_parameters: tuple[str, ...]


def parse_chat_completion_request(
    body: Mapping[str, JsonValue], policy: ParameterPolicy, model_map: ModelMap,
    usage_mapping_resolver: UsageMappingResolver,
) -> ParsedOpenAIRequest: ...


def render_chat_completion_artifact(
    reply: CommittedReply, ignored: tuple[str, ...], *, created: int,
    usage_row: UsageEvidenceRow, usage_mapping: DialectUsageMapping,
) -> ImmutableHttpArtifact: ...


def render_openai_error(error: ProxyError) -> ImmutableHttpArtifact: ...
```

Use frozen Pydantic `extra="forbid"` models. Resolve `(runtime, model, thinking, ordinary, openai)` through the shared `UsageMappingResolver` during parsing/admission, before fingerprinting or reservation, and bind that row in `ImmutableSessionConfig.usage_rows`. Map only `end_turn -> stop`; render usage only through `CanonicalUsage.render(exact_row, exact_mapping)`. Do not hand-pick counters or synthesize absent SDK fields.

- [ ] **Step 6: Run tests and commit**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_openai_adapter.py tests/unit/test_openai_errors.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/openai_adapter.py tests/unit/test_openai_adapter.py tests/unit/test_openai_errors.py
git commit -m "feat: add strict OpenAI translation"
```

---

### Task 2: Add OpenAI HTTP and Commit-Safe Streaming Through the Existing Actor

**Files:**
- Modify: `src/claude_sdk_proxy/router.py`
- Modify: `src/claude_sdk_proxy/openai_adapter.py`
- Modify: `src/claude_sdk_proxy/capabilities.py`
- Modify: `tests/integration/test_capabilities.py`
- Create: `tests/integration/test_openai_http.py`
- Create: `tests/integration/test_openai_stream.py`
- Create: `tests/integration/test_cross_dialect.py`

**Interfaces:**
- Consumes: `ProxyRouter`, run/explicit/one-shot selection, actor admissions, replay snapshots, writer leases, and the shared `UsageMappingResolver`/session row binding.
- Produces: non-streaming and streaming `POST /v1/chat/completions` and exact OpenAI SSE artifacts.

- [ ] **Step 1: Write a real-socket non-streaming test**

Start `BoundedHttpServer` with the fake backend, POST an OpenAI request with a master key, and assert the exact artifact, ignored header, writer/accounting release, and one canonical backend turn.

```python
assert response.json()["choices"][0]["message"] == {"role": "assistant", "content": "hello"}
assert response.headers["x-claude-proxy-ignored-parameters"] == "max_completion_tokens"
```

- [ ] **Step 2: Write exact SSE, failure, and replay tests**

Assert the first chunk establishes `role="assistant"`; text deltas preserve byte order; the buffered terminal semantic chunk contains finish reason; with `stream_options.include_usage=true`, the separate terminal usage chunk has empty `choices` and the complete exact OpenAI mapping (every identity-bound nested/optional SDK leaf plus only declared derived total); and exact final bytes are `data: [DONE]\n\n`. With `include_usage=false` or absent, assert the OpenAI-defined wire omission while the committed canonical result still retains every SDK leaf. Disconnect after every header/event/byte boundary. Require `409 operation_in_progress` during execution, exact byte-zero replay after commit, and no finish/usage/`[DONE]` before result, iterator, usage mapping, and deadline validation. Replay both include-usage variants byte-for-byte from their distinct request fingerprints.

- [ ] **Step 3: Write dialect isolation and canonical equivalence tests**

Submit equivalent Anthropic and OpenAI text through separate sessions and assert byte-identical canonical system/user text, exact backend model ID, and identical canonical SDK usage leaves before dialect rendering. Assert the Anthropic and OpenAI mappings emit every leaf at their separate exact public paths. Assert an OpenAI-bound run token cannot call `/v1/messages`, an Anthropic-bound token cannot call `/v1/chat/completions`, and neither mismatch mutates its actor. In capability tests, give one tuple passing Anthropic but false OpenAI mapping and one passing both; assert OpenAI advertises/admits only the latter while Anthropic remains unaffected.

- [ ] **Step 4: Run tests and confirm route absence**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/integration/test_openai_http.py tests/integration/test_openai_stream.py tests/integration/test_cross_dialect.py -v`

Expected: FAIL with `404` for `/v1/chat/completions`.

- [ ] **Step 5: Implement route and stream encoder**

```python
class OpenAIStreamEncoder:
    def __init__(
        self, usage_row: UsageEvidenceRow, usage_mapping: DialectUsageMapping,
    ) -> None: ...
    def encode_nonterminal(self, event: CanonicalEvent) -> tuple[bytes, ...]: ...
    def encode_terminal_bundle(self, result: CanonicalResult, include_usage: bool) -> tuple[bytes, ...]: ...
    def encode_error(self, error: ProxyError) -> bytes: ...
```

The route performs only dialect validation/rendering. Before reservation it requires the exact ordinary OpenAI mapping through the shared resolver and verifies the session's bound row digest. `CapabilityProjector` uses that same object to expose OpenAI only for identical passing tuples; false/missing/unrepresentable mappings are unadvertised and rejected, never silently reduced to three counters. The route otherwise calls the same Phase 1 authentication, session selection, capacity reservation, actor, replay, disconnect, and cleanup paths.

- [ ] **Step 6: Run tests and commit**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/integration/test_openai_http.py tests/integration/test_openai_stream.py tests/integration/test_cross_dialect.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/router.py src/claude_sdk_proxy/openai_adapter.py src/claude_sdk_proxy/capabilities.py tests/integration/test_capabilities.py tests/integration/test_openai_http.py tests/integration/test_openai_stream.py tests/integration/test_cross_dialect.py
git commit -m "feat: expose OpenAI Chat Completions"
```

---

### Task 3: Resolve External Control Authentication and Build a Deny-by-Default Harness Environment

**Files:**
- Create: `src/claude_sdk_proxy/launcher_environment.py`
- Create: `src/claude_sdk_proxy/launcher_auth.py`
- Create: `src/claude_sdk_proxy/launcher_keyfile.py`
- Create: `tests/unit/test_launcher_environment.py`
- Create: `tests/unit/test_launcher_auth.py`
- Create: `tests/unit/test_launcher_keyfile.py`

**Interfaces:**
- Consumes: `Dialect`, Phase 1 redacted/zeroizable `SecretBytes`, Phase 1 `GeneratedMasterKeyFormatV1`, loopback proxy URL, derived run token, explicit pass-name list, Phase 1 `RuntimeRoot.open()`, CLI `--runtime-root`/`--api-key-file`/`--no-auth`, and parent `LOCAL_PROXY_API_KEY`/`LOCAL_PROXY_API_KEY_FILE`.
- Produces: `build_launcher_environment()`, `ProtectedEnvironmentName`, discriminated `ExternalAuthMode = ConfiguredKeyAuth | GeneratedKeyFileAuth | NoAuth`, `ControlAuthorization = AuthenticatedControl | UnauthenticatedControl`, exact `AuthenticatedControl.close()` ownership, `resolve_runtime_root()`, `resolve_master_key_file()`, `resolve_external_auth()`, `open_control_authorization()`, and `read_master_key_file(path, runtime_root)` with all-boundary FD/buffer cleanup.

- [ ] **Step 1: Write the exact default environment test**

```python
DEFAULT_NAMES = {
    "HOME", "USER", "LOGNAME", "SHELL", "PATH", "TMPDIR", "TMP", "TEMP",
    "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "TERM_PROGRAM", "COLORTERM", "NO_COLOR",
}


def test_anthropic_child_gets_only_allowlist_and_derived_key() -> None:
    parent = {name: f"value-{name}" for name in DEFAULT_NAMES} | {
        "LOCAL_PROXY_API_KEY": "master",
        "LOCAL_PROXY_API_KEY_FILE": "/private/runtime/master.key",
        "ANTHROPIC_API_KEY": "upstream",
        "SECRET_EXTRA": "no",
    }
    env = build_launcher_environment(parent, Dialect.ANTHROPIC, PROXY_URL, "run-derived", pass_names=())
    assert env["ANTHROPIC_BASE_URL"] == PROXY_URL
    assert env["ANTHROPIC_API_KEY"] == "run-derived"
    assert "LOCAL_PROXY_API_KEY" not in env
    assert "LOCAL_PROXY_API_KEY_FILE" not in env
    assert "SECRET_EXTRA" not in env
```

Repeat for `OPENAI_BASE_URL=<proxy>/v1` and `OPENAI_API_KEY`. Property-test arbitrary parent dictionaries; only default plus explicit names survive.

- [ ] **Step 2: Write protected-name and value tests**

Reject explicit pass-through of provider keys/base URLs, `LOCAL_PROXY_*`, `CLAUDE_*`, `ANTHROPIC_*`, cloud credentials, cookies, tokens, passwords, secrets, and case variants. Reject non-loopback URLs, control characters, and duplicate conflicting names. Never include environment values in exceptions.

- [ ] **Step 3: Write exact three-mode resolution and fail-closed tests**

Test this exhaustive discriminator before any key-file open, control request, or child spawn:

```python
def test_configured_key_needs_no_phase1_key_file(no_file_io: FileIoTrace) -> None:
    mode = resolve_external_auth(
        cli_api_key_file=None,
        cli_no_auth=False,
        parent_environment={"LOCAL_PROXY_API_KEY": VALID_MASTER_KEY},
    )
    assert isinstance(mode, ConfiguredKeyAuth)
    assert no_file_io.opens == []
    assert "master" not in repr(mode).lower()


def test_no_auth_is_never_inferred_from_missing_sources() -> None:
    with pytest.raises(LauncherConfigurationError, match="external auth mode"):
        resolve_external_auth(
            cli_api_key_file=None, cli_no_auth=False, parent_environment={}
        )


def test_no_auth_requires_explicit_flag() -> None:
    mode = resolve_external_auth(
        cli_api_key_file=None, cli_no_auth=True, parent_environment={}
    )
    assert isinstance(mode, NoAuth)


@pytest.mark.parametrize(
    ("cli_file", "no_auth", "environment"),
    [
        (KEY_PATH, False, {"LOCAL_PROXY_API_KEY": VALID_MASTER_KEY}),
        (None, True, {"LOCAL_PROXY_API_KEY": VALID_MASTER_KEY}),
        (KEY_PATH, True, {}),
        (None, True, {"LOCAL_PROXY_API_KEY_FILE": str(KEY_PATH)}),
        (KEY_PATH, False, {"LOCAL_PROXY_API_KEY_FILE": str(OTHER_KEY_PATH)}),
    ],
)
def test_external_auth_conflicts_fail_before_io(
    cli_file: Path | None,
    no_auth: bool,
    environment: Mapping[str, str],
    orchestration_trace: OrchestrationTrace,
) -> None:
    with pytest.raises(LauncherConfigurationError):
        resolve_external_auth(
            cli_api_key_file=cli_file,
            cli_no_auth=no_auth,
            parent_environment=environment,
        )
    assert orchestration_trace == OrchestrationTrace.zero()
```

Add a complete conflict table. `LOCAL_PROXY_API_KEY` alone selects `ConfiguredKeyAuth`; a CLI or environment key-file source alone selects `GeneratedKeyFileAuth`; `--no-auth` alone selects `NoAuth`. Reject configured-key plus either key-file source, configured-key plus `--no-auth`, key-file plus `--no-auth`, differing CLI/environment key-file paths, and all three together. Treat explicitly present empty configured-key/key-file environment values as invalid, not absent. Missing sources fail; unreadable/invalid files fail; an authenticated control request that receives `401` fails without retrying anonymously; a no-auth request never retries with ambient credentials. Errors and `repr` contain neither secret nor supplied path.

`LOCAL_PROXY_API_KEY` is bounded to 1–4096 visible ASCII bytes with no whitespace/control characters and copied immediately into redacted `SecretBytes`. `ConfiguredKeyAuth` exclusively owns it until `open_control_authorization()` atomically moves that same allocation into `AuthenticatedControl`; the moved-from mode is unusable and owns nothing. The resolver must not touch the filesystem in configured or no-auth mode. `open_control_authorization(GeneratedKeyFileAuth)` calls the validated reader exactly once and transfers its returned `SecretBytes` directly into `AuthenticatedControl`; no second token allocation is retained. `AuthenticatedControl` is then the sole owner across mint/revoke requests and `close()` zeroizes that exact allocation once on every normal exit, validation/control failure, cancellation, signal, spawn failure, and exception path. Request serialization owns and zeroizes its bounded mutable header buffer immediately after each send attempt; the control client stores no secret/header copy. `open_control_authorization(NoAuth)` returns `UnauthenticatedControl` with an empty immutable header set. No mode reads or searches for any Phase 1 startup output.

- [ ] **Step 4: Write deterministic runtime-root and descriptor-relative key-file tests**

The runtime root has exactly two trusted sources. An explicit absolute `--runtime-root` is authoritative. When the flag is absent, derive the default from `pwd.getpwuid(os.getuid()).pw_dir / "Library/Application Support/claude-sdk-proxy/runtime"`; never derive it from `HOME`, `TMPDIR`, the key-file parent, current working directory, or a global search. Test that a hostile `HOME` does not change the default, relative/control-character flag values fail without path disclosure, and an explicit nondefault root is preserved for later validation.

```python
def test_default_runtime_root_uses_os_account_database(monkeypatch, account_home: Path) -> None:
    monkeypatch.setenv("HOME", "/attacker/controlled")
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(account_home)))
    root = resolve_runtime_root(None, current_uid=os.getuid())
    assert root == account_home / "Library/Application Support/claude-sdk-proxy/runtime"


def test_invalid_runtime_root_precedes_key_open(runtime_trace, valid_key_path: Path) -> None:
    with pytest.raises(LauncherConfigurationError, match="runtime root"):
        read_master_key_file(valid_key_path, Path("relative-root"))
    assert runtime_trace.key_open_count == 0
    assert runtime_trace.key_read_bytes == 0


@pytest.mark.parametrize(
    "raw",
    [
        b"A" * 43,                       # missing LF / 43 bytes
        b"A" * 42 + b"\n",              # short token / 43 bytes
        b"A" * 44 + b"\n",              # long token / 45 bytes
        b"A" * 43 + b"\r\n",            # CRLF / 45 bytes
        b"A" * 42 + b"\n\n",            # embedded/second LF
        b"A" * 42 + b" \n",             # whitespace
        b"A" * 42 + b"\x00\n",          # NUL
        b"A" * 42 + b"=\n",             # forbidden padding
        b"A" * 42 + b"+\n",             # non-base64url alphabet
        b"A" * 42 + b"B\n",             # noncanonical pad bits
        b"A" * 42 + "é".encode() + b"\n", # non-ASCII / oversized
    ],
)
def test_generated_key_format_boundaries_are_rejected(
    valid_runtime_root: Path, write_candidate_key: Callable[[bytes], Path], raw: bytes
) -> None:
    path = write_candidate_key(raw)
    with pytest.raises(LauncherConfigurationError, match="generated key file"):
        read_master_key_file(path, valid_runtime_root)
```

Fault-inject the production reader at every boundary: validated root descriptor acquisition, descriptor-relative key `openat`, post-open/pre-`fstat`, identity checks, each bounded read, immediate-EOF check, decode, canonical re-encode/compare, construction of the recovered `SecretBytes`, and the exception boundary after construction but before return. For every injected failure assert the key FD and root/instance descriptor leases close exactly once, raw/read/decoded/re-encoded/rejected-token buffers are zeroized, a provisionally constructed recovered secret is zeroized if it was not returned, no control request or child spawn occurs, and diagnostics reveal only a stable stage/error code plus zeroized/descriptor-closed booleans—not the path, descriptor, bytes, hash, or exception text. On success, retain a test-only alias to the read buffer and assert it is already zero before `read_master_key_file()` returns; ownership of only the returned token allocation transfers to the caller.

Add table-driven `resolve_master_key_file()` cases with this exact policy: if only `--api-key-file` is present, use it; if only `LOCAL_PROXY_API_KEY_FILE` is present, use it; if both resolve to the same absolute normalized path, accept the CLI value as authoritative; if both are present and differ, reject before any control request or child spawn; an explicitly present empty environment value is invalid; if neither is present, return `None` for the auth discriminator to classify. Reject relative paths and control characters during resolution without including the supplied value in an error or representation.

Before opening the selected key file, `read_master_key_file(path, runtime_root)` validates the root itself through the Phase 1 `RuntimeRoot.open()` contract: absolute path, local APFS, current UID, exact mode `0700`, directory type, no symlink/cloud placeholder, and stable descriptor/path device+inode identity. It then opens the key descriptor-relative beneath that validated root and tests regular-file type, current UID, exact `0600`, no symlink, `O_NOFOLLOW|O_CLOEXEC`, `fstat` after open, link count one, same device, and stable descriptor/name inode identity. Read at most `GeneratedMasterKeyFormatV1.FILE_BYTES + 1` bytes and require exactly 44 bytes plus immediate EOF. One internal read guard exclusively owns the root/instance descriptor leases, key FD, mutable 45-byte read buffer, decoder scratch buffers, and any provisional recovered `SecretBytes`; all descriptors close and all nonreturned buffers/secrets zeroize in `finally`. Only a successful return transfers the fresh 43-byte canonical token `SecretBytes` without the LF; the raw/read allocation is already zeroized at that boundary. Thus the consumer enforces the producer's exact `[A-Za-z0-9_-]{43}\n` ASCII/UTF-8 byte contract, canonical 32-byte decode/re-encode, and raw/intermediate zeroization; it does not implement a looser 4-KiB format. Reject traversal, hard-link count greater than one, wrong length/alphabet/padding/whitespace/NUL/canonical pad bits, wrong root/device/owner/mode/type, root replacement, escape to a sibling instance, file replacement races, and data changing between `fstat`/read/EOF validation. An invalid root must fail before any key-file open/read. Assert resolution never causes `LOCAL_PROXY_API_KEY` or `LOCAL_PROXY_API_KEY_FILE` to enter the harness environment, and `--pass-env` rejects either protected name.

- [ ] **Step 5: Run tests and verify missing modules**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_launcher_environment.py tests/unit/test_launcher_auth.py tests/unit/test_launcher_keyfile.py -v`

Expected: FAIL because launcher environment, auth, and key-file modules are missing.

- [ ] **Step 6: Implement the exact builders**

```python
def build_launcher_environment(
    parent: Mapping[str, str],
    dialect: Dialect,
    proxy_url: str,
    derived_token: str,
    *,
    pass_names: tuple[str, ...],
) -> dict[str, str]: ...


@dataclass(slots=True, repr=False)
class ConfiguredKeyAuth:
    secret: SecretBytes


@dataclass(frozen=True, slots=True, repr=False)
class GeneratedKeyFileAuth:
    path: Path


@dataclass(frozen=True, slots=True)
class NoAuth:
    pass


ExternalAuthMode: TypeAlias = ConfiguredKeyAuth | GeneratedKeyFileAuth | NoAuth


@dataclass(slots=True, repr=False)
class AuthenticatedControl:
    secret: SecretBytes
    closed: bool = False
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class UnauthenticatedControl:
    headers: tuple[tuple[str, str], ...] = ()


ControlAuthorization: TypeAlias = AuthenticatedControl | UnauthenticatedControl


def read_master_key_file(path: Path, runtime_root: Path) -> SecretBytes: ...


def resolve_runtime_root(
    cli_path: Path | None,
    *,
    current_uid: int,
) -> Path: ...


def resolve_master_key_file(
    cli_path: Path | None,
    parent_environment: Mapping[str, str],
) -> Path | None: ...


def resolve_external_auth(
    *,
    cli_api_key_file: Path | None,
    cli_no_auth: bool,
    parent_environment: Mapping[str, str],
) -> ExternalAuthMode: ...


@contextmanager
def open_control_authorization(
    mode: ExternalAuthMode,
    runtime_root: Path,
) -> Iterator[ControlAuthorization]: ...
```

`resolve_runtime_root()` uses only the flag or OS account database rule above. `read_master_key_file()` calls `RuntimeRoot.open(runtime_root)` before opening any descendant and keeps the validated root descriptor open through containment checks and the key read, then closes every descriptor lease before returning. `open_control_authorization()` is the sole ownership-transfer boundary: its context manager moves a configured secret or newly read secret into one `AuthenticatedControl`, and its `finally` always calls the idempotent `close()` even if construction, mint, revoke, spawn, cancellation, or context exit raises. A failure between acquiring a secret and yielding the authorization closes the provisional secret itself. A moved-from `ConfiguredKeyAuth`, a closed authorization, and a returned read guard cannot be reused. Explicit `--pass-env NAME` copies only that name after protected-name validation. Both `LOCAL_PROXY_API_KEY` and `LOCAL_PROXY_API_KEY_FILE` are launcher control inputs only: consume them before constructing the child environment, never copy them to the child, and reject attempts to pass them explicitly. Provider base URL plus the derived run token are injected after allowlist copying; the token exists only in the returned provider-key value. `repr` for secrets, auth modes, control authorization, and launcher configuration is redacted.

- [ ] **Step 7: Run tests and commit**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_launcher_environment.py tests/unit/test_launcher_auth.py tests/unit/test_launcher_keyfile.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/launcher_environment.py src/claude_sdk_proxy/launcher_auth.py src/claude_sdk_proxy/launcher_keyfile.py tests/unit/test_launcher_environment.py tests/unit/test_launcher_auth.py tests/unit/test_launcher_keyfile.py
git commit -m "feat: isolate launcher credentials"
```

---

### Task 4: Mint, Launch, Signal, Revoke, and Self-Host One Harness Run

**Files:**
- Create: `src/claude_sdk_proxy/launcher_process.py`
- Create: `src/claude_sdk_proxy/launcher.py`
- Modify: `src/claude_sdk_proxy/server_cli.py`
- Create: `tests/integration/test_launcher_lifecycle.py`
- Create: `tests/integration/test_launcher_auth_modes.py`
- Create: `tests/integration/test_generated_key_contract.py`
- Create: `tests/integration/test_launcher_self_hosted.py`

**Interfaces:**
- Consumes: `POST /_proxy/runs`, `DELETE /_proxy/runs/{run_id}`, Phase 1 `InstanceRuntime.create_master_key()`/`GeneratedMasterKeyFormatV1`/exclusive `MasterKeyHandle` and durable cleanup receipt, Task 3 `ExternalAuthMode`/`ControlAuthorization`/`read_master_key_file()`, environment builder, and `ServerLifecycle`.
- Produces: `SelfHostedAuth`, `LauncherAuth = ExternalAuthMode | SelfHostedAuth`, `LauncherConfig`, `resolve_launcher_config()`, `ProxyControlClient.mint_run()`/`.revoke_run()`, `HarnessProcess`, `run_harness()`, and `claude-proxy run`.

- [ ] **Step 1: Write mint/spawn/revoke lifecycle tests**

Use a fake control server and child that reports only environment-name presence. Assert mint occurs before spawn; only the derived token reaches the child; spawn failure still revokes; normal exit revokes once; revoke closes an active automatic operation; and the exact child exit code is returned.

In `tests/integration/test_launcher_auth_modes.py`, start a matching Phase 1 server/fake control endpoint for each external mode and record authorization headers, filesystem calls, orchestration calls, child argv/environment names, and redacted diagnostics. Require these exact paths:

- Configured mode: resolve runtime root, resolve `ConfiguredKeyAuth` from `LOCAL_PROXY_API_KEY`, perform zero key-file lookup/open/stat/read calls even though Phase 1 created no key file, mint with authenticated control, then spawn.
- Generated-file mode: resolve runtime root, resolve CLI-only, environment-only, or identical CLI+environment `GeneratedKeyFileAuth`, enter `read_master_key_file(selected_key, selected_runtime_root)`, validate root, open/read once, transfer the recovered secret to `AuthenticatedControl`, mint with authenticated control, close/zeroize that authorization in the launcher `finally`, then spawn only after successful mint.
- No-auth mode: observe explicit `--no-auth`, resolve `NoAuth`, perform zero key-file operations, mint/revoke with no `Authorization` or `x-api-key` header, then spawn with only the derived provider token.

```python
@pytest.mark.anyio
async def test_configured_phase1_server_needs_no_key_file(configured_server, trace) -> None:
    result = await launch_against(
        configured_server,
        parent_environment={"LOCAL_PROXY_API_KEY": configured_server.master_key},
    )
    assert result.exit_code == 0
    assert trace.key_file_operations == []
    assert trace.control_auth_modes == ["authenticated", "authenticated"]
    assert result.child_secret_names == {configured_server.provider_key_name}


@pytest.mark.anyio
async def test_explicit_no_auth_sends_no_control_credential(no_auth_server, trace) -> None:
    result = await launch_against(no_auth_server, cli_no_auth=True, parent_environment={})
    assert result.exit_code == 0
    assert trace.key_file_operations == []
    assert trace.control_headers == [(), ()]
    assert result.child_secret_names == {no_auth_server.provider_key_name}


@pytest.mark.anyio
async def test_real_phase1_generated_key_round_trips_to_control_request(
    runtime_parent: Path, deadline: Deadline, capturing_control_server,
    server_lifecycle_factory, producer_buffer_trace, consumer_buffer_trace,
    runtime_sync_trace,
) -> None:
    root = RuntimeRoot.open(runtime_parent)
    lease = await root.acquire_reconciliation(deadline)
    instance = await lease.create_instance(current_owner_record())
    produced = instance.create_master_key()
    lifecycle = server_lifecycle_factory(instance=instance, master_key=produced)
    produced_path = produced.path
    assert produced_path is not None
    producer_secret_storage = produced.secret.value
    assert producer_buffer_trace.file_buffer_zeroized is True

    recovered = read_master_key_file(produced_path, runtime_parent)
    recovered_secret_storage = recovered.value
    assert consumer_buffer_trace.raw_read_buffer_zeroized is True
    authorization = AuthenticatedControl(recovered)
    try:
        assert authorization.secret.value == produced.secret.value
        assert authorization.secret.value is not produced.secret.value
        await capturing_control_server.client.mint_run(TEST_CONFIG, authorization)
        request = capturing_control_server.only_request
        assert request.headers.get_all("x-api-key") == [produced.secret.value.decode("ascii")]
        assert request.headers.get_all("authorization") == []
    finally:
        authorization.close()
        await lifecycle.stop_admission_and_cleanup()

    assert authorization.closed is True
    assert recovered.closed is True
    assert recovered_secret_storage == bytearray(len(recovered_secret_storage))
    assert producer_secret_storage == bytearray(len(producer_secret_storage))
    assert consumer_buffer_trace.all_buffers_zeroized is True
    assert producer_buffer_trace.all_buffers_zeroized is True
    assert produced.state is MasterKeyState.CLOSED
    assert produced.path is None
    assert produced.startup_message is None
    assert produced_path.exists() is False
    assert lifecycle.master_key_cleanup_receipt.parent_dirsynced is True
    assert runtime_sync_trace.events[-3:] == [
        "master_key_unlinkat", "master_key_delete_parent_fsync",
        "master_key_descriptors_closed",
    ]
    assert lifecycle.cleanup_only is False
```

The real producer-consumer test lives in `tests/integration/test_generated_key_contract.py`; it may inspect the credential only for exact equality assertions and retains test-only aliases to prove producer/recovered allocations become all-zero. It must drive the real `ServerLifecycle` cleanup, not call `unlink`, `SecretBytes.close()`, or `MasterKeyHandle.close_after_cleanup()` directly. The server lifecycle removes the generated file descriptor-relative, parent-syncs the deletion, closes all descriptor leases, and completes normal instance cleanup. Parameterize this fixture over normal completion, mint failure, revoke failure, harness spawn failure, launcher cancellation, server listener failure, and a cleanup `unlinkat`/parent-`fsync` transient failure; in every case the recovered authorization closes, all producer/consumer scratch and secret allocations zeroize, and the generated file is durably absent before lifecycle completion. A transient deletion failure must visibly enter cleanup-only state, retain exclusive handle/instance ownership, retry from its recorded boundary, and only then produce the receipt and complete.

`ProxyControlClient` uses exactly one `x-api-key` header for `AuthenticatedControl` and no `Authorization` header; it never serializes the LF. Its per-request mutable header serialization buffer is zeroized after send success/failure and no request object or diagnostic retains it. For every row, the control credential/path exists only in the launcher/control client; child argv, harness environment, exception text, and logs contain none of `LOCAL_PROXY_API_KEY`, `LOCAL_PROXY_API_KEY_FILE`, the selected path, master value, secret hash/fingerprint, descriptor, or control headers. Redacted diagnostics retain mode, stage, secret/buffer-zeroized booleans, descriptor-closed boolean, durable-delete/parent-sync booleans, retry count, and stable error code. Invalid/default-root lookup, empty source, source conflict, configured/no-auth/generated conflicts, server-mode mismatch, key validation failure, and control `401` assert zero child spawns. A failed authenticated request is never retried without credentials, and a failed no-auth request is never retried with an ambient key.

- [ ] **Step 2: Write signal and cancellation tests**

Run a child that forks one descendant. Deliver SIGINT and SIGTERM to the launcher, assert immediate server revocation is initiated, forward the same signal to the harness process group, wait boundedly, and return conventional signal exit status. Launcher task cancellation and parent control-channel loss follow the same `finally` revocation path.

- [ ] **Step 3: Write self-hosted server tests**

Assert the launcher-generated `SecretBytes` allocation moves exactly once to the in-process server without a key file or child environment entry; retain a test-only alias and prove server lifecycle cleanup zeroizes it on readiness failure, mint/spawn failure, cancellation, and normal completion. The listener is loopback; readiness precedes mint; harness exit revokes; server shutdown drains writers/actors/C17 allocations; and the launcher does not return while cleanup-only parenthood is required. Assert `--self-hosted` rejects `--no-auth`, `--api-key-file`, or presence of `LOCAL_PROXY_API_KEY`/`LOCAL_PROXY_API_KEY_FILE` before server construction rather than ignoring an external auth selector.

- [ ] **Step 4: Run tests and confirm launcher absence**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/integration/test_launcher_lifecycle.py tests/integration/test_launcher_auth_modes.py tests/integration/test_generated_key_contract.py tests/integration/test_launcher_self_hosted.py -v`

Expected: FAIL because launcher modules and CLI command are missing.

- [ ] **Step 5: Implement process and control interfaces**

```python
@dataclass(frozen=True, slots=True)
class SelfHostedAuth:
    pass


LauncherAuth: TypeAlias = ExternalAuthMode | SelfHostedAuth


@dataclass(frozen=True, slots=True)
class LauncherConfig:
    dialect: Dialect
    parameter_policy: ParameterPolicy
    ttl_seconds: int
    proxy_url: str
    command: tuple[str, ...]
    pass_env: tuple[str, ...]
    runtime_root: Path
    auth: LauncherAuth


def resolve_launcher_config(
    parsed: ParsedRunCommand,
    parent_environment: Mapping[str, str],
) -> LauncherConfig: ...


class ProxyControlClient:
    async def mint_run(
        self, config: LauncherConfig, authorization: ControlAuthorization
    ) -> MintedRun: ...
    async def revoke_run(
        self, run: MintedRun, authorization: ControlAuthorization
    ) -> None: ...


class HarnessProcess:
    @classmethod
    async def spawn(cls, command: tuple[str, ...], environment: Mapping[str, str]) -> "HarnessProcess": ...
    async def forward(self, signal_number: int) -> None: ...
    async def wait(self) -> int: ...


async def run_harness(config: LauncherConfig, parent_environment: Mapping[str, str]) -> int: ...
```

Use `start_new_session=True`. `run_harness()` enters `open_control_authorization()` before mint and keeps that single authorization owner through best-effort revoke; its outermost `finally` closes/zeroizes authorization even if mint never returns, spawn fails, signal forwarding/cancellation interrupts, revoke fails, or process waiting raises. It clears transient request/header buffers after each control attempt and retains only secret-free stage/error metadata. Never put either key or selected key path in argv, harness environment, diagnostics, exception strings, or new persisted files. Bound control HTTP and process waits with monotonic deadlines.

- [ ] **Step 6: Add the command and run tests**

Support:

```text
LOCAL_PROXY_API_KEY=configured-secret claude-proxy run --proxy-url http://127.0.0.1:8787 --dialect anthropic --policy messages_compat --ttl 1800 -- command arg
claude-proxy run --proxy-url http://127.0.0.1:8787 --dialect anthropic --policy messages_compat --ttl 1800 --runtime-root "/Users/me/Library/Application Support/claude-sdk-proxy/runtime" --api-key-file /absolute/runtime/instance/key -- command arg
LOCAL_PROXY_API_KEY_FILE=/absolute/runtime/instance/key claude-proxy run --proxy-url http://127.0.0.1:8787 --dialect openai --policy messages_compat --ttl 1800 --pass-env GH_CONFIG_DIR -- command arg
claude-proxy run --proxy-url http://127.0.0.1:8787 --no-auth --dialect openai --policy messages_compat --ttl 1800 -- command arg
claude-proxy run --self-hosted --dialect openai --policy messages_compat --ttl 1800 -- command arg
```

Reject empty commands, invalid TTLs, non-loopback URLs, missing external auth selection, and protected `--pass-env` names before minting. `--no-auth` is a positive external-mode selection, not a fallback; it conflicts with `--api-key-file`, `LOCAL_PROXY_API_KEY`, and `LOCAL_PROXY_API_KEY_FILE`. Configured-key and key-file sources conflict with each other. `--self-hosted` conflicts with all three external selectors, including ambient presence of either `LOCAL_PROXY_*` control variable and `--no-auth`.

Resolve `--runtime-root` or the OS-account-derived default and store it in `LauncherConfig.runtime_root`, then resolve exactly one `LauncherAuth` before making a control request. `ConfiguredKeyAuth` uses the already configured in-memory secret and never expects or synthesizes a Phase 1 file. `GeneratedKeyFileAuth` calls `read_master_key_file(mode.path, config.runtime_root)` and cannot infer trust from the key's parent directory. `NoAuth` sends an empty control-authorization header set and never probes for a key. In `SelfHostedAuth`, generate/transfer the master key only in launcher/server memory, pass `config.runtime_root` to the in-process server, and let the Phase 1 runtime-root owner create/validate its instance there. All root/source/conflict errors occur before any key read, control request, or harness spawn; after mode selection, any failure remains in that mode and cannot downgrade or search alternate sources.

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/integration/test_launcher_lifecycle.py tests/integration/test_launcher_auth_modes.py tests/integration/test_generated_key_contract.py tests/integration/test_launcher_self_hosted.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/claude_sdk_proxy/launcher_process.py src/claude_sdk_proxy/launcher.py src/claude_sdk_proxy/server_cli.py tests/integration/test_launcher_lifecycle.py tests/integration/test_launcher_auth_modes.py tests/integration/test_generated_key_contract.py tests/integration/test_launcher_self_hosted.py
git commit -m "feat: launch scoped harness runs"
```

---

### Task 5: Prove Official-Client and Harness-Neutral Compatibility

**Files:**
- Modify: `Makefile`
- Modify: `pyproject.toml`
- Create: `tests/integration/test_official_openai_client.py`
- Create: `tests/integration/test_full_history_harness.py`
- Create: `docs/harnesses.md`

**Interfaces:**
- Consumes: complete Phase 2 API and launcher.
- Produces: official-client evidence, a representative agentic-harness fixture, and honest generic setup documentation.

- [ ] **Step 1: Add and lock the official client fixture**

Add `openai>=2.8,<3` to the `dev` extra.

Run: `uv lock`

Expected: lock succeeds without changing the exact Agent SDK pin.

- [ ] **Step 2: Write official OpenAI client tests**

Use a real TCP server and the client's custom `base_url`/local key. Cover non-streaming, streaming iteration with `stream_options={"include_usage": True}`, streaming omission when false/absent, parsed errors, raw ignored-parameter headers, finish reasons, one-shot, explicit heads/idempotency through raw headers, automatic continuity, retry after disconnect, and cancellation. Supply a fake-backend ordinary result containing every identity-bound SDK usage leaf in a passing OpenAI mapping, including nested detail and present optional/null/zero cases; exercise both explicit SDK-total identity and schema-authorized checked-sum total fixtures. Assert the official client exposes every exact non-streaming value, parses the complete terminal streaming usage object, and observes byte-exact replay of both wire artifacts. Absent optional fields remain absent, not zero. Unknown SDK leaves and false/missing mappings must fail with no partial usage response and must not be advertised in capabilities.

- [ ] **Step 3: Write a representative full-history harness**

Create a subprocess fixture that reads only normal OpenAI or Anthropic base URL/key variables, sends three sequential full-history turns, streams the second, repeats it once as a retry, and exits during the third. Parameterize launcher startup over a configured-key Phase 1 server with no key file, a generated-key-file server, and an explicitly no-auth server. In all three cases assert the harness receives the same shape—provider base URL and one derived run token only—and receives no master/control variable, control header, key path, or auth-mode marker. Assert one run token, one bound actor, exact prefix validation, no duplicate backend turn, immediate revocation on exit, and complete C17 cleanup. Start a second fixture with a new token and prove it gets an independent conversation.

- [ ] **Step 4: Write the multiplexing boundary test**

Have one process interleave two initial conversations under one token and assert deterministic `409 session_ambiguous`. Repeat using two explicit session IDs/heads/idempotency keys and assert both proceed independently. This is the evidence for the documented zero-source-change limitation.

- [ ] **Step 5: Document generic use**

`docs/harnesses.md` must document direct one-shot configuration, launcher automatic mode, explicit headers for multiplexers, the OS-account-derived runtime-root default and `--runtime-root` override, exact default/pass-through environment names, and the three external auth commands. Document that `LOCAL_PROXY_API_KEY` selects configured in-memory auth and needs no Phase 1 key file; `--api-key-file`/`LOCAL_PROXY_API_KEY_FILE` select validated generated-key-file auth with the same-path exception; only explicit `--no-auth` selects no-auth; missing/conflicting/invalid modes fail with no downgrade. Include root/key-file validation, consumer secret/buffer zeroization, producer ownership and durable deletion on server lifecycle cleanup, cleanup-only retry semantics, and the guarantee that neither control variable, credential, header, nor path is inherited by the harness or retained in diagnostics. Explain that the harness always receives only a scoped derived provider token regardless of control mode. Also document retry aliasing, new-token requirement, cancellation/revocation, self-hosted conflicts/behavior, and compatibility limits. Mention Pi only as a non-normative example and state that local vLLM/SGLang servers are called directly by harnesses.

Extend the inherited `Makefile` using the Phase 0 `PYTEST_RELEASE_FLAGS` unchanged. `phase2-check` runs the complete Phase 1+2 unit/integration suite with those flags before Ruff and mypy. `phase2-release` depends on the complete `phase1-release` target and `phase2-check`, so Phase 0 evidence and the mandatory Phase 1 existing-login preflight/live gate cannot be bypassed. No deterministic-only target may claim Phase 2 release authority. Any future optional command must have a `dev-` prefix, be documented as non-authoritative, and have no dependency path into `check`, either phase release target, evidence generation, or any downstream release target.

- [ ] **Step 6: Run the Phase 2 gate**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit tests/integration -v`

Expected: PASS, including Phase 1 tests unchanged and no leaked token, writer, actor, process, workdir, journal, or cleanup slot.

Run: `uv run ruff check . && uv run mypy src/claude_sdk_proxy`

Expected: no findings.

- [ ] **Step 7: Commit**

```bash
git add Makefile pyproject.toml uv.lock tests/integration/test_official_openai_client.py tests/integration/test_full_history_harness.py docs/harnesses.md
git commit -m "docs: verify harness compatibility"
```
