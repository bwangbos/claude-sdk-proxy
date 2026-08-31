# Phase 1 Anthropic Text Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the approved text-only Anthropic Messages-shaped localhost proxy with one-shot, automatic linear-session, and explicit-session modes, exact retry semantics, bounded HTTP and response resources, and crash-safe ownership of every Agent SDK/CLI allocation.

**Architecture:** A Python 3.14 AnyIO service uses h11 only as a bounded HTTP/1.1 state parser and routes validated requests into one actor per native Claude session. Actors own canonical transcript validation, heads, deadlines, exact-byte replay artifacts, and SDK operations, while the Phase 0 C17 Darwin library plus supervisor/anchor binaries own journals, process identities, the supervisor/anchor/CLI tree, and destructive cleanup authority. The Agent SDK boundary is inaccessible until each child proves existing-login provenance and exact model identity under the validated runtime manifest.

**Tech Stack:** Python 3.14, uv, AnyIO 4, h11 0.16, Pydantic 2, Claude Agent SDK/CLI exact versions from `docs/feasibility/validated-environment.json`, Phase 0 C17 Darwin lifecycle dylib/supervisor/anchor, pytest, HTTPX, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-08-29-claude-subscription-api-proxy-design.md`

## Global Constraints

- Support only Darwin/macOS 14 or newer with the runtime root on a local APFS volume; all other platforms and filesystems fail startup.
- Require every core Phase 0 manifest gate to be `true` and require the running SDK, CLI, lifecycle executable, OS, filesystem, auth-evidence schema, and model mappings to match that manifest exactly.
- Use only `backend_kind=agent_sdk_subscription`, `auth_source=existing_claude_login`, and `semantic_class=prompt_isolated_agent_sdk`.
- Bind only an explicit loopback address. `/health` is the sole anonymous endpoint and exposes no backend, model, session, or capability detail.
- Text only. Reject image, document, URL, audio, citation, search-result, non-empty tool, and unvalidated thinking tuples before allocation or SDK mutation.
- Accept only one plain-string system prompt, including empty. Reject Anthropic system arrays, structured blocks, cache metadata, preset/file forms, and any proxy-added system text.
- Implement thinking absence plus exact manifest-passing `enabled` tuples only. Reject `disabled` and `adaptive` modes and never advertise them, even when Phase 0 records observational evidence for those modes.
- Preserve every SDK-supplied usage leaf admitted by the typed feasibility schema and emit it exactly once through the request dialect's passing mapping; unknown/missing/type-changed fields, mapping collisions, or false/missing dialect mappings fail rather than being dropped or approximated.
- The direct-request policy is strict. The advertised Anthropic harness profile accepts only `max_tokens` as ignored and reports it on every affected response.
- Never flatten or inject prior assistant history. Canonical transcript fingerprints validate an already-selected live session only.
- Enforce every expiry and operation decision with an injectable monotonic clock under the owning lock; wall time is reporting only.
- Do not persist prompts, transcripts, responses, tools, run tokens, derived keys, or Claude credential contents. The generated local master-key file is the only proxy credential write, remains exclusively owned, and must be descriptor-relatively unlinked plus parent-directory-synced before lifecycle completion.
- A numeric PID or PGID is observation-only. Python code never signals a target process directly; all process and file actions go through the Phase 0 lifecycle authority.
- No vLLM/SGLang forwarding, raw Messages backend, non-loopback serving, database, external cache, telemetry sink, or tool bridge is part of Phase 1.
- Inherit the Phase 0 repository release-test policy without weakening it. Every mandatory Phase 1 pytest invocation—including focused red/green commands, chained commands, deterministic aggregate tests, and live evidence—uses `--strict-markers --forbid-skips -W error`; all async tests carry a registered execution marker. A skip, xfail represented as a skip, unknown marker, warning, or unhandled coroutine makes the command non-passing.
- Phase 1 defines no optional pytest command. Any future developer-only exception must use an explicitly `dev-*`-named target, be documented as non-authoritative, be excluded from `check`, `phase1-check`, `phase1-live`, `phase1-release`, manifest generation, and every downstream release dependency, and must never create or refresh passing evidence.
- Authenticated existing-Claude-login evidence is a mandatory Phase 1 live prerequisite. Missing opt-in, missing/expired login, ambiguous auth source, or failed preflight exits nonzero; `pytest.skip`, `pytest.importorskip`, skip markers, and silent pass-through are forbidden in the mandatory live suite.

## File Map

- `pyproject.toml`: Python 3.14 and pinned runtime dependencies.
- `Makefile`: inherited Phase 0 `PYTEST_RELEASE_FLAGS` plus non-bypassable Phase 1 check/live/release dependencies.
- `src/claude_sdk_proxy/domain.py`: canonical requests, blocks, events, transcript entries, principals, errors, and immutable HTTP artifacts.
- `src/claude_sdk_proxy/config.py`: manifest-backed model/usage/thinking mappings, limits, and startup validation.
- `src/claude_sdk_proxy/clock.py`: wall/monotonic clock protocol and immutable deadlines.
- `src/claude_sdk_proxy/deadline_scheduler.py`: owner-scoped monotonic timer tasks and exact-once deadline handles.
- `src/claude_sdk_proxy/runtime_root.py`: APFS runtime-root plus exclusive generated-master-key creation, cleanup, durable deletion, and secret/buffer ownership.
- `src/claude_sdk_proxy/lifecycle.py`: production allocation wrapper over Phase 0 `Journal`, `Lifecycle`, supervisor, anchor, and C17 journal ABI.
- `src/claude_sdk_proxy/environment.py`: Phase 0 exact Agent SDK/CLI child environment, extended only with production configuration loading.
- `src/claude_sdk_proxy/attestation.py`: Phase 0 per-child auth/provider/endpoint/version gate, wired into every production backend.
- `src/claude_sdk_proxy/backend.py`: SDK-independent backend protocol and validated Agent SDK implementation.
- `src/claude_sdk_proxy/capacity.py`: atomic lifecycle, operation, replay, waiter, writer, and queue reservations.
- `src/claude_sdk_proxy/replay.py`: HMAC request fingerprints, exact-byte artifacts, snapshots, and writer leases.
- `src/claude_sdk_proxy/tokens.py`: master principals, signed run tokens, active/retired keyed digests.
- `src/claude_sdk_proxy/runs.py`: irreversible run registry and shared terminal cells.
- `src/claude_sdk_proxy/transcript.py`: canonical selected-session prefix and new-turn validation.
- `src/claude_sdk_proxy/sessions.py`: session registry, tombstones, and actor construction.
- `src/claude_sdk_proxy/actor.py`: session automaton, operation tasks, retries, deadlines, commits, and teardown transfer.
- `src/claude_sdk_proxy/capabilities.py`: authenticated, manifest-derived capability projection shared by every dialect.
- `src/claude_sdk_proxy/anthropic_adapter.py`: strict Messages request and response/SSE conversion.
- `src/claude_sdk_proxy/http_types.py`: framework-independent bounded request/response types.
- `src/claude_sdk_proxy/http_transport.py`: AnyIO+h11 connection admission, parsing, deadlines, and writes.
- `src/claude_sdk_proxy/router.py`: security checks, credential selection, endpoint dispatch, and error mapping.
- `src/claude_sdk_proxy/control.py`: health, models, capabilities, sessions, and runs.
- `src/claude_sdk_proxy/diagnostics.py`: local structured diagnostics and recursive redaction.
- `src/claude_sdk_proxy/server.py`: startup, serving, cleanup-only mode, and shutdown drain.
- `src/claude_sdk_proxy/server_cli.py`: `claude-proxy serve` command.
- `tests/fakes.py`: deterministic clocks, lifecycle/backend scripts, and socket helpers.
- `tests/unit/`: pure contract and state-machine tests.
- `tests/integration/`: real-socket, C17 lifecycle, SDK-boundary, and black-box client tests.
- `tests/live/`: mandatory exact-tuple existing-login preflight and subscription evidence; no skip path has release authority.

---

### Task 1: Establish Canonical Types, Clocks, Configuration, and Exact Dependencies

**Files:**
- Modify: `Makefile`
- Modify: `pyproject.toml`
- Create: `src/claude_sdk_proxy/domain.py`
- Create: `src/claude_sdk_proxy/clock.py`
- Create: `src/claude_sdk_proxy/deadline_scheduler.py`
- Create: `src/claude_sdk_proxy/config.py`
- Create: `tests/unit/test_domain.py`
- Create: `tests/unit/test_config.py`
- Create: `tests/unit/test_deadline_scheduler.py`

**Interfaces:**
- Consumes: Phase 0 `FeasibilityManifest`, `UsageScalarKind`, `UsageOperationClass`, `UsageDialect`, exact-budget `UsageTupleKey`, `DialectUsageMapping`, `UsageEvidenceSchema`/`UsageEvidenceRow`, and exact runtime constants.
- Produces: `Clock`, `Deadline`, `DeadlineScheduler`, `DeadlineHandle`, redacted/zeroizable `SecretBytes`, `ProxyConfig.load()`, discriminated `ProxyAuthMode` (`ConfiguredMasterKey`, `GeneratedMasterKey`, or `NoAuth`), `Dialect`, `CanonicalInputBlock`, `CanonicalAssistantOutputBlock`, `CanonicalTranscriptBlock`, `TextBlock`, `ThinkingBlock`, `RedactedThinkingBlock`, `CanonicalMessage`, `ThinkingConfig`, `ThinkingTuple`, `ImplementedThinkingAllowlist`, `UsageMappingResolver`, `UsageRowBinding`, `UsageRowBindings`, `CanonicalUsageField`, `CanonicalUsage`, `ParameterPolicy`, `ModelMap`, `ImmutableSessionConfig`, `CanonicalRequest`, `CanonicalEvent`, `CanonicalResult`, `ImmutableHttpArtifact`, `Principal`, `ProxyError`, and `new_opaque_id()`.

- [ ] **Step 1: Pin the Phase 1 dependencies and Python version**

Set `requires-python = ">=3.14,<3.15"`, retain the exact Phase 0 Agent SDK pin, and add:

```toml
"anyio>=4.10,<5",
"h11>=0.16,<0.17",
"pydantic>=2.11,<3",
```

Add `httpx>=0.28,<1` to the `dev` extra and set Ruff/mypy targets to Python 3.14.

- [ ] **Step 2: Write failing canonical-domain and deadline tests**

```python
def test_deadline_uses_monotonic_for_runtime_and_wall_for_reporting(fake_clock: FakeClock) -> None:
    deadline = Deadline.from_ttl(fake_clock, 30)
    fake_clock.advance_monotonic(30)
    fake_clock.advance_wall(-3600)
    assert deadline.expired(fake_clock)
    assert deadline.expires_at == datetime(2026, 8, 31, 12, 0, 30, tzinfo=UTC)


def test_canonical_request_preserves_text_bytes() -> None:
    block = TextBlock(text="  hello\n")
    assert block.text == "  hello\n"


def test_usage_mapping_resolver_requires_the_exact_request_budget(
    usage_mapping_resolver, runtime_digest, backend_model_id
) -> None:
    row, _ = usage_mapping_resolver.require(
        runtime_digest,
        backend_model_id,
        ThinkingConfig(type="enabled", budget_tokens=4096),
        "high",
        UsageOperationClass.ORDINARY,
        Dialect.ANTHROPIC,
    )
    assert row.key.budget_tokens == 4096
    assert row.key.effort == "high"

    with pytest.raises(ProxyError, match="unsupported_parameter"):
        usage_mapping_resolver.require(
            runtime_digest,
            backend_model_id,
            ThinkingConfig(type="enabled", budget_tokens=4097),
            "high",
            UsageOperationClass.ORDINARY,
            Dialect.ANTHROPIC,
        )

    null_row, _ = usage_mapping_resolver.require(
        runtime_digest,
        backend_model_id,
        None,
        None,
        UsageOperationClass.ORDINARY,
        Dialect.ANTHROPIC,
    )
    assert null_row.key.budget_tokens is None
    assert null_row.key.effort is None
```

Add domain tests that construct interleaved `ThinkingBlock`, `RedactedThinkingBlock`, and `TextBlock` assistant content and assert exact payload/signature/order equality; reject either thinking block in a user message; and reject a thinking-bearing transcript when its exact manifest tuple gate is disabled. Build a fixture manifest containing passing `null`, `disabled`, `adaptive`, and `enabled` thinking rows: assert the implementation allowlist contains only passing `enabled` rows with a passing ordinary Anthropic usage mapping, ordinary `thinking=None` remains supported only with that mapping, and disabled/adaptive or mapping-false rows are rejected and absent from capabilities even when Phase 0 observed their SDK shapes as passing.

Add typed usage tests covering every allowed scalar kind, nested paths, optional absence/presence/null, exact large integer/string/boolean preservation, stable schema/row/mapping ordering and digests, missing required fields, unknown SDK leaves, duplicate or exact/prefix-colliding mappings, illegal dialect paths, and type confusion (`bool` is not an integer). Assert each identity-bound SDK value appears once at its exact public path, nested detail survives leaf-for-leaf, and absent optionals do not become zero. Cover explicit-total identity and schema-authorized checked-sum total derivation; reject optional/missing/nullable/noninteger/negative/overflow sources and prove no derived aggregate is misreported as an SDK field. A false or missing runtime/model/thinking/operation/dialect mapping must reject before reservation. Add scheduler tests for deadline ordering, delayed wakeups, reschedule generations, cancel-versus-fire races, monotonic-only behavior under wall-clock jumps, callback exceptions, and exact-once shutdown cleanup.

Add configuration tests for the exact server authentication discriminator: a present valid `LOCAL_PROXY_API_KEY` selects `ConfiguredMasterKey` and forbids a generated key file; no configured key selects `GeneratedMasterKey` by default; explicit `--no-auth` selects `NoAuth` and creates/exposes no control credential. Reject empty/invalid configured keys, configured-key plus generated-key override, configured-key plus `--no-auth`, and generated-key override plus `--no-auth`; absence of credentials must never silently select `NoAuth`.

- [ ] **Step 3: Run the focused tests and confirm the missing-module failure**

Run: `uv lock && uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_domain.py tests/unit/test_config.py tests/unit/test_deadline_scheduler.py -v`

Expected: dependency resolution succeeds; tests fail because the new modules do not exist.

- [ ] **Step 4: Implement the frozen public contracts**

```python
class Clock(Protocol):
    def monotonic(self) -> float: ...
    def wall(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class Deadline:
    monotonic_at: float
    expires_at: datetime

    @classmethod
    def from_ttl(cls, clock: Clock, ttl_seconds: int) -> "Deadline": ...
    def expired(self, clock: Clock) -> bool: ...


class Dialect(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str
    kind: str = "text"


@dataclass(frozen=True, slots=True)
class ThinkingBlock:
    thinking: str
    signature: str
    kind: str = "thinking"


@dataclass(frozen=True, slots=True)
class RedactedThinkingBlock:
    data: str
    signature: str | None
    kind: str = "redacted_thinking"


CanonicalInputBlock: TypeAlias = TextBlock
CanonicalAssistantOutputBlock: TypeAlias = TextBlock | ThinkingBlock | RedactedThinkingBlock
CanonicalTranscriptBlock: TypeAlias = CanonicalInputBlock | CanonicalAssistantOutputBlock


@dataclass(frozen=True, slots=True)
class CanonicalMessage:
    role: Literal["user", "assistant"]
    content: tuple[CanonicalTranscriptBlock, ...]

    def __post_init__(self) -> None:
        if self.role == "user" and any(not isinstance(block, TextBlock) for block in self.content):
            raise ValueError("user messages accept only CanonicalInputBlock")


@dataclass(frozen=True, slots=True)
class ThinkingConfig:
    type: Literal["enabled"]
    budget_tokens: int


@dataclass(frozen=True, slots=True)
class ThinkingTuple:
    backend_model_id: str
    budget_tokens: int
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None


@dataclass(frozen=True, slots=True)
class ImplementedThinkingAllowlist:
    enabled_tuples: frozenset[ThinkingTuple]

    @classmethod
    def from_manifest(
        cls, manifest: FeasibilityManifest, model_map: "ModelMap"
    ) -> "ImplementedThinkingAllowlist": ...

    def require(
        self, backend_model_id: str, config: ThinkingConfig | None,
        effort: Literal["low", "medium", "high", "xhigh", "max"] | None,
    ) -> None: ...

    def project(self, backend_model_id: str) -> tuple[ThinkingTuple, ...]: ...


UsageScalar: TypeAlias = int | str | bool | None


@dataclass(frozen=True, slots=True)
class CanonicalUsageField:
    sdk_path: tuple[str, ...]
    kind: UsageScalarKind
    value: UsageScalar


@dataclass(frozen=True, slots=True)
class CanonicalUsage:
    schema_digest: str
    row_digest: str
    operation_class: UsageOperationClass
    fields: tuple[CanonicalUsageField, ...]

    @classmethod
    def from_sdk(
        cls, raw: Mapping[str, JsonValue], schema: UsageEvidenceSchema,
        row: UsageEvidenceRow,
    ) -> "CanonicalUsage": ...

    def require_integer(self, sdk_path: tuple[str, ...]) -> int: ...
    def render(
        self, row: UsageEvidenceRow, mapping: DialectUsageMapping,
    ) -> Mapping[str, JsonValue]: ...


@dataclass(frozen=True, slots=True)
class UsageMappingResolver:
    schema: UsageEvidenceSchema

    def require(
        self, runtime_digest: str, backend_model_id: str,
        thinking: ThinkingConfig | None,
        effort: Literal["low", "medium", "high", "xhigh", "max"] | None,
        operation_class: UsageOperationClass, dialect: Dialect,
    ) -> tuple[UsageEvidenceRow, DialectUsageMapping]: ...
    def require_bound(
        self, bindings: "UsageRowBindings",
        operation_class: UsageOperationClass, dialect: Dialect,
    ) -> tuple[UsageEvidenceRow, DialectUsageMapping]: ...


@dataclass(frozen=True, slots=True)
class UsageRowBinding:
    operation_class: UsageOperationClass
    row_digest: str


@dataclass(frozen=True, slots=True)
class UsageRowBindings:
    entries: tuple[UsageRowBinding, ...]

    def require(self, operation_class: UsageOperationClass) -> str: ...


class ParameterPolicy(StrEnum):
    STRICT = "strict"
    MESSAGES_COMPAT = "messages_compat"


@dataclass(slots=True, repr=False)
class SecretBytes:
    value: bytearray
    closed: bool = False
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True, repr=False)
class ConfiguredMasterKey:
    secret: SecretBytes


@dataclass(frozen=True, slots=True)
class GeneratedMasterKey:
    pass


@dataclass(frozen=True, slots=True)
class NoAuth:
    pass


ProxyAuthMode: TypeAlias = ConfiguredMasterKey | GeneratedMasterKey | NoAuth


@dataclass(frozen=True, slots=True)
class ModelMap:
    by_alias: Mapping[str, str]

    def resolve(self, public_alias: str) -> str: ...


@dataclass(frozen=True, slots=True)
class ImmutableSessionConfig:
    dialect: Dialect
    model_alias: str
    backend_model_id: str
    system: str
    thinking: ThinkingConfig | None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None
    parameter_policy: ParameterPolicy
    usage_rows: UsageRowBindings


class CanonicalRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    dialect: Dialect
    model_alias: str
    backend_model_id: str
    system: str
    messages: tuple[CanonicalMessage, ...]
    stream: bool
    thinking: ThinkingConfig | None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None
    parameter_policy: ParameterPolicy
    metadata: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class ImmutableHttpArtifact:
    status: int
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes
    result_head: str | None
    accounted_bytes: int


@dataclass(frozen=True, slots=True)
class CanonicalResult:
    stop_reason: Literal["end_turn"]
    usage: CanonicalUsage
    backend_model_id: str


class DeadlineScheduler:
    async def start(self, task_group: anyio.abc.TaskGroup) -> None: ...
    async def arm(
        self, owner_key: str, generation: int, deadline: Deadline,
        callback: Callable[[str, int], Awaitable[None]],
    ) -> "DeadlineHandle": ...


class DeadlineHandle:
    async def cancel_once(self) -> bool: ...
```

`CanonicalInputBlock` is the user-to-backend subset and is text-only in Phase 1. `CanonicalAssistantOutputBlock` is the assistant/reply union; `CanonicalMessage` accepts the wider transcript union but enforces the input subset for `role="user"`. `ThinkingBlock` preserves the exact public thinking string and signature, while `RedactedThinkingBlock` preserves the exact opaque data string and an exact optional signature when the validated SDK tuple exposes one. Neither normalizes Unicode, decodes/re-encodes opaque data, merges adjacent blocks, nor changes block order.

Phase 1's implementation policy is deliberately smaller than Phase 0's observation matrix. No-thinking is represented only by absent `thinking` plus absent effort. The only implemented structured mode is `ThinkingConfig(type="enabled", budget_tokens=...)`; `disabled` and `adaptive` are unimplemented and receive `400 unsupported_parameter` even if their Phase 0 evidence rows passed. `UsageMappingResolver.require()` constructs exactly one Phase 0 `UsageTupleKey`: absent thinking maps to `(thinking_mode="null", effort=None, budget_tokens=None)`, while enabled thinking maps to the request's exact positive integer `budget_tokens` and exact effort-or-`None`. It performs equality lookup on all key fields and never formats `tokens:N`, derives a budget class, rounds, buckets, or falls back to a neighboring budget. `require_bound()` verifies that the retained row has that same exact budget identity. At startup `ImplementedThinkingAllowlist.from_manifest()` takes the immutable intersection of configured exact backend IDs, passing Phase 0 thinking rows, passing ordinary Anthropic usage mappings resolved by that one shared resolver, and the hard-coded `{enabled}` implementation-mode set. Its `ThinkingTuple.budget_tokens` is copied directly from `UsageTupleKey.budget_tokens`; ordinary absent thinking is subject to the same exact mapping resolution.

The parser and `CapabilityProjector` receive the same `ProxyConfig.thinking_allowlist` and `ProxyConfig.usage_mapping_resolver` objects: admission calls `require()` for `(runtime, model, thinking, ordinary, anthropic)`, while capability rendering projects the identical passing set. There is no second filter or manifest reread, so a tuple cannot be accepted without being advertised or advertised without being accepted. A nonpositive/unlisted budget, effort without enabled thinking, disabled/adaptive object, or false/missing thinking-or-usage mapping is rejected before fingerprint/capacity/allocation; an unexpected backend thinking event is `502 sdk_protocol_error` and loses a possibly mutated session.

`CanonicalUsage.from_sdk()` is the sole SDK-usage normalizer. It receives the exact bound feasibility row, recursively enumerates every SDK leaf, and requires an exact match to its allowlisted `UsageFieldSpec` paths and scalar kinds. It preserves each supplied value in schema order with its SDK path only; public naming belongs exclusively to a `DialectUsageMapping`. Required missing fields, unknown leaves or nested objects, non-lossless scalar types, negative count fields, wrong operation class, and row/schema digest mismatch are `502 sdk_protocol_error`. Optional absent fields remain absent; `None` is accepted only for an explicitly nullable SDK field; zero is preserved only when supplied and never invented. `bool` is never accepted as an integer.

`CanonicalUsage.render(row, mapping)` validates that the row/schema/operation/mapping digests and dialect binding match the canonical value. It then emits every present SDK leaf exactly once through its identity binding, omits only absent optional leaves, and evaluates only declared `checked_sum` public aggregates with exact bounded integer arithmetic. It rejects missing, duplicate, conflated, exact-or-prefix-colliding, nullable-to-nonnullable, or illegal public paths and rejects every undeclared derivation. An SDK-supplied total is identity-bound and never recomputed or overwritten; if the schema instead declares an OpenAI-required total as a checked sum, that public aggregate is derived from all declared required sources but is not added to `CanonicalUsage.fields` or described as SDK-supplied. `CanonicalResult` owns the complete immutable `CanonicalUsage`; adapters receive an exact passing row/mapping and may not retain only headline counters or drop any allowlisted leaf.

`DeadlineScheduler` is a monotonic-clock heap/condition task, not a request-driven expiry check. It owns no registry state: a fired callback is advisory and carries an owner key plus generation. Every callback must acquire the owning run-registry or actor lock, verify that generation and current deadline are still authoritative, recompute all expiries from the injected monotonic clock, and no-op if superseded. Each `DeadlineHandle` has exact-once cancel/fire state; scheduler shutdown joins its task and leaves no callback running.

`ModelMap` copies its source mapping into an immutable mapping, rejects duplicate aliases/backend ambiguity, and resolves only exact configured public aliases. `ImmutableSessionConfig` is derived once from the first validated request and is compared structurally on every continuation. Its `UsageRowBindings` contains exactly the rows admission proved for that session: Phase 1 binds only `ordinary`, while Phase 3 must add the tool-operation rows before enabling tools. These definitions live in `domain.py` and `deadline_scheduler.py` (`ModelMap` validation/loading lives in `config.py`) so every later task imports rather than redeclares them.

`SecretBytes.close()` overwrites every byte of its existing mutable allocation in place, preserves that zero-filled allocation for verification, sets `closed=True`, and is idempotent; every consumer rejects a closed instance. `ProxyConfig.load()` must validate the manifest, loopback host, exact aliases/backend IDs, positive bounded TTLs/deadlines/capacities, tools disabled, `load_usage_evidence()`/`exact_usage_schema`, one immutable `UsageMappingResolver`, one `ImplementedThinkingAllowlist` derived from it, and exactly one `ProxyAuthMode`. `LOCAL_PROXY_API_KEY` is accepted only as bounded nonempty visible ASCII without control characters, copied into redacted/zeroizable `SecretBytes`, and never represented or logged. `NoAuth` exists only after the explicit CLI/config selection; missing or invalid credential material never falls back to it.

- [ ] **Step 5: Run the tests and commit**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_domain.py tests/unit/test_config.py tests/unit/test_deadline_scheduler.py -v`

Expected: PASS.

```bash
git add pyproject.toml uv.lock src/claude_sdk_proxy/domain.py src/claude_sdk_proxy/clock.py src/claude_sdk_proxy/deadline_scheduler.py src/claude_sdk_proxy/config.py tests/unit/test_domain.py tests/unit/test_config.py tests/unit/test_deadline_scheduler.py
git commit -m "feat: define proxy contracts and limits"
```

---

### Task 2: Own the APFS Runtime Root and Generated Master Key

**Files:**
- Create: `src/claude_sdk_proxy/runtime_root.py`
- Create: `tests/unit/test_runtime_root.py`
- Create: `tests/integration/test_runtime_root_darwin.py`

**Interfaces:**
- Consumes: `ProxyConfig`, Phase 0 Darwin capability report.
- Produces: `RuntimeRoot.open()`, `RuntimeRoot.acquire_reconciliation()`, `RootReconciliationLease`, `OwnerRecord`, `InstanceLifetimeLock`, `RootReconciliationLease.create_instance()`, `RootReconciliationLease.claim_abandoned_instance()`, shared `GeneratedMasterKeyFormatV1`, `MasterKeyState`, `MasterKeyCreationBoundary`, `MasterKeyAbsenceBasis`, `MasterKeyFileIdentity`, `MasterKeyCleanupReceipt`, `InstanceRuntime.create_master_key()`, and exclusive `MasterKeyHandle.close_after_cleanup()`.

- [ ] **Step 1: Write root-lock, lifetime-lock, owner-record, mode, and synchronization tests**

```python
@pytest.mark.anyio
async def test_generated_key_is_private_and_only_path_is_reported(
    runtime_parent: Path, deadline: Deadline, runtime_sync_trace: RuntimeSyncTrace
) -> None:
    root = RuntimeRoot.open(runtime_parent)
    reconciliation = await root.acquire_reconciliation(deadline)
    instance = await reconciliation.create_instance(current_owner_record())
    key = instance.create_master_key()
    assert key.secret is not None
    assert key.path is not None
    assert key.startup_message is not None
    key_stat = os.stat(key.path, follow_symlinks=False)
    assert stat.S_IMODE(key_stat.st_mode) == 0o600
    assert key_stat.st_uid == os.getuid()
    assert key_stat.st_nlink == 1
    assert key_stat.st_size == 44
    assert stat.S_IMODE(os.stat(instance.path).st_mode) == 0o700
    assert key.startup_message == f"local API key file: {key.path}"
    assert key.secret.value.decode() not in key.startup_message
    raw = key.path.read_bytes()
    assert len(raw) == GeneratedMasterKeyFormatV1.FILE_BYTES == 44
    assert re.fullmatch(rb"[A-Za-z0-9_-]{43}\n", raw)
    assert raw[:-1] == key.secret.value
    decoded = base64.urlsafe_b64decode(raw[:-1] + b"=")
    assert len(decoded) == GeneratedMasterKeyFormatV1.ENTROPY_BYTES == 32
    assert runtime_sync_trace.events[-2:] == ["master_key_fullfsync", "instance_parent_fsync"]
```

Add production-boundary fault tests against the real `runtime_root.py` syscall adapter—not a second fake implementation. Retain aliases only to the mutable test-owned backing arrays and inspect state/descriptor traces, never diagnostic secret values:

```python
@pytest.mark.parametrize(
    (
        "fault", "expected_state", "expected_boundary", "secret_installed",
        "file_entry_created", "created_identity_present", "absence_basis",
        "file_unlinked",
    ),
    [
        (
            "after_entropy", MasterKeyState.CREATING,
            MasterKeyCreationBoundary.PRE_OPEN, False, False, False,
            MasterKeyAbsenceBasis.NEVER_CREATED, False,
        ),
        (
            "after_token_encode", MasterKeyState.CREATING,
            MasterKeyCreationBoundary.PRE_OPEN, False, False, False,
            MasterKeyAbsenceBasis.NEVER_CREATED, False,
        ),
        (
            "before_openat", MasterKeyState.CREATING,
            MasterKeyCreationBoundary.PRE_OPEN, True, False, False,
            MasterKeyAbsenceBasis.NEVER_CREATED, False,
        ),
        (
            "after_openat", MasterKeyState.CREATING,
            MasterKeyCreationBoundary.ENTRY_CREATED, True, True, False,
            MasterKeyAbsenceBasis.VERIFIED_UNLINK, True,
        ),
        (
            "after_fstat", MasterKeyState.CREATING,
            MasterKeyCreationBoundary.ENTRY_CREATED, True, True, True,
            MasterKeyAbsenceBasis.VERIFIED_UNLINK, True,
        ),
        (
            "after_name_identity", MasterKeyState.CREATING,
            MasterKeyCreationBoundary.ENTRY_CREATED, True, True, True,
            MasterKeyAbsenceBasis.VERIFIED_UNLINK, True,
        ),
        (
            "during_write", MasterKeyState.CREATING,
            MasterKeyCreationBoundary.ENTRY_CREATED, True, True, True,
            MasterKeyAbsenceBasis.VERIFIED_UNLINK, True,
        ),
        (
            "after_write", MasterKeyState.CREATING,
            MasterKeyCreationBoundary.ENTRY_CREATED, True, True, True,
            MasterKeyAbsenceBasis.VERIFIED_UNLINK, True,
        ),
        (
            "after_fullfsync", MasterKeyState.CREATING,
            MasterKeyCreationBoundary.ENTRY_CREATED, True, True, True,
            MasterKeyAbsenceBasis.VERIFIED_UNLINK, True,
        ),
        (
            "after_file_close", MasterKeyState.CREATING,
            MasterKeyCreationBoundary.ENTRY_CREATED, True, True, True,
            MasterKeyAbsenceBasis.VERIFIED_UNLINK, True,
        ),
        (
            "after_parent_fsync", MasterKeyState.CREATING,
            MasterKeyCreationBoundary.ENTRY_DURABLE, True, True, True,
            MasterKeyAbsenceBasis.VERIFIED_UNLINK, True,
        ),
        (
            "after_handle_publish_before_return", MasterKeyState.PUBLISHED,
            MasterKeyCreationBoundary.RETURN_READY, True, True, True,
            MasterKeyAbsenceBasis.VERIFIED_UNLINK, True,
        ),
    ],
)
def test_every_create_and_return_fault_transfers_or_completes_cleanup(
    instance, fault, expected_state, expected_boundary, secret_installed,
    file_entry_created, created_identity_present, absence_basis, file_unlinked,
    master_key_faults, sensitive_buffer_trace, fd_trace, master_key_trace,
) -> None:
    master_key_faults.raise_at(fault)
    with pytest.raises(InjectedMasterKeyFault):
        instance.create_master_key()
    receipt = instance.cleanup_provisional_master_key_until_durable()
    cleanup_calls = tuple(master_key_trace.calls)
    assert master_key_faults.exclusive_owner_registered_before_entropy is True
    snapshot = master_key_faults.handle_snapshot_at_fault
    assert snapshot.state is expected_state
    assert snapshot.creation_boundary is expected_boundary
    assert snapshot.secret_installed is secret_installed
    assert snapshot.file_entry_created is file_entry_created
    assert snapshot.created_identity_present is created_identity_present
    assert sensitive_buffer_trace.all_entropy_token_and_file_buffers_zeroized
    assert fd_trace.all_opened_key_and_directory_leases_closed
    assert instance.master_key_entry_exists_descriptor_relative() is False
    assert instance.master_key_delete_parent_fsync_confirmed is True
    assert instance.unowned_master_key_resources == ()
    assert receipt == MasterKeyCleanupReceipt(
        secret_zeroized=True,
        file_entry_created=file_entry_created,
        file_unlinked=file_unlinked,
        absence_basis=absence_basis,
        durable_absence=True,
        parent_dirsynced=True,
        descriptors_closed=True,
    )
    calls = tuple(master_key_trace.calls)
    assert instance.cleanup_provisional_master_key_until_durable() is receipt
    assert tuple(master_key_trace.calls) == calls
    if absence_basis is MasterKeyAbsenceBasis.NEVER_CREATED:
        assert "master_key_fstatat" not in cleanup_calls
        assert "master_key_unlinkat" not in cleanup_calls


def test_unconfirmed_open_reconciles_absence_without_claiming_an_unlink(
    instance, master_key_faults, master_key_trace
) -> None:
    master_key_faults.raise_during_openat_before_result()
    with pytest.raises(InjectedMasterKeyFault):
        instance.create_master_key()
    snapshot = master_key_faults.handle_snapshot_at_fault
    assert snapshot.state is MasterKeyState.CREATING
    assert snapshot.creation_boundary is MasterKeyCreationBoundary.OPEN_UNCONFIRMED
    assert snapshot.secret_installed is True
    assert snapshot.file_entry_created is False
    assert snapshot.created_identity_present is False
    receipt = instance.cleanup_provisional_master_key_until_durable()
    assert receipt == MasterKeyCleanupReceipt(
        secret_zeroized=True,
        file_entry_created=False,
        file_unlinked=False,
        absence_basis=MasterKeyAbsenceBasis.RECONCILED_ABSENT,
        durable_absence=True,
        parent_dirsynced=True,
        descriptors_closed=True,
    )
    assert master_key_trace.calls[-2:] == [
        "master_key_reconcile_absent", "master_key_delete_parent_fsync",
    ]


def test_unexplained_enoent_after_created_entry_never_completes(
    created_key, master_key_faults
) -> None:
    master_key_faults.make_created_entry_disappear_without_handle_unlink()
    with pytest.raises(RuntimeOwnershipError, match="unexplained disappearance"):
        created_key.close_after_cleanup(cleanup_done=True)
    assert created_key.state is MasterKeyState.CLEANUP_PENDING
    assert created_key.cleanup_receipt is None


def test_known_unlinked_retries_only_parent_sync_and_returns_exact_receipt(
    generated_key, master_key_faults, master_key_trace
) -> None:
    master_key_faults.fail_once_after_unlink_before_parent_fsync()
    with pytest.raises(InjectedMasterKeyFault):
        generated_key.close_after_cleanup(cleanup_done=True)
    assert generated_key.state is MasterKeyState.CLEANUP_PENDING
    assert generated_key.file_entry_created is True
    assert generated_key.file_unlinked is True
    assert generated_key.cleanup_receipt is None
    assert master_key_trace.calls.count("master_key_unlinkat") == 1

    receipt = generated_key.close_after_cleanup(cleanup_done=True)
    assert receipt == MasterKeyCleanupReceipt(
        secret_zeroized=True,
        file_entry_created=True,
        file_unlinked=True,
        absence_basis=MasterKeyAbsenceBasis.VERIFIED_UNLINK,
        durable_absence=True,
        parent_dirsynced=True,
        descriptors_closed=True,
    )
    assert master_key_trace.calls.count("master_key_unlinkat") == 1


def test_close_after_cleanup_is_ordered_durable_and_idempotent(
    generated_key, master_key_trace
) -> None:
    secret = generated_key.secret
    assert secret is not None
    secret_storage = secret.value
    with pytest.raises(RuntimeOwnershipError, match="cleanup not complete"):
        generated_key.close_after_cleanup(cleanup_done=False)
    assert generated_key.state is MasterKeyState.PUBLISHED

    receipt = generated_key.close_after_cleanup(cleanup_done=True)
    assert receipt == MasterKeyCleanupReceipt(
        secret_zeroized=True,
        file_entry_created=True,
        file_unlinked=True,
        absence_basis=MasterKeyAbsenceBasis.VERIFIED_UNLINK,
        durable_absence=True,
        parent_dirsynced=True,
        descriptors_closed=True,
    )
    assert generated_key.state is MasterKeyState.CLOSED
    assert secret.closed is True
    assert secret_storage == bytearray(len(secret_storage))
    assert generated_key.secret is None
    assert generated_key.path is None
    assert generated_key.startup_message is None
    calls = master_key_trace.calls
    assert generated_key.close_after_cleanup(cleanup_done=True) is receipt
    assert master_key_trace.calls == calls
```

Add parameterized creation failures for every `MasterKeyCreationBoundary`: before/after entropy transfer while `secret` is optional, before `openat`, while `openat` outcome is unconfirmed, immediately after returned FD but before stored identity, after identity, wrong UID/mode/type/link/device/name identity, short writes, failed `F_FULLFSYNC`, failed file-descriptor close, failed parent-directory `fsync`, and after `RETURN_READY`/internal publication but before Python return. Add cleanup failures before/after secret zeroization, pre-open parent sync, unconfirmed-open reconciliation, descriptor-relative identity `fstatat`, replacement/symlink/inode mismatch, before/after `unlinkat`, after successful `unlinkat` but before parent `fsync`, retry `fsync`, and before/after final descriptor-lease release. Explicitly inject all three absence cases: known-not-created, known-unlinked, and unexplained disappearance. For every boundary assert the exact state/boundary/optional fields and receipt booleans, no `fstatat`/`unlinkat` for `PRE_OPEN`, no unlink of a mismatched entry, no second unlink after a known successful unlink, no completion receipt before directory sync, identical-object idempotence for both never-created and verified-unlink receipts, idempotent concurrent close, every producer entropy/token/file buffer zeroized, every opened key FD closed, and either durable absence or exclusive ownership by cleanup-only state—never an unowned path, file, descriptor, or secret.

Add separate-process and fault-injection tests proving:

- `RootReconciliationLease` is a nonblocking/deadline-bounded exclusive Darwin BSD `flock` over the stable root lock inode and serializes every startup scan, create, claim, signal request, and deletion;
- a newly created instance's owner record and lifetime-lock inode are file- and parent-directory-synchronized before key, journal, workdir, or process creation is possible;
- a held `InstanceLifetimeLock` causes reconciliation to skip that directory without opening its owner record or allocation entries;
- after acquiring an abandoned lifetime lock, takeover proves the recorded boot/PID-start/UID/executable identity absent, durably replaces the owner record, and retains the lifetime lock through all reconciliation and instance removal; and
- process death at every owner temporary-create/write/full-sync/rename/parent-sync boundary leaves either the old or new complete record, never grants action from owner metadata, and is safely repeatable by a later root-lease holder.

Require the Phase 0 manifest's `darwin_local_apfs`, `required_sync_primitives`, and `bsd_flock_model` gates plus their evidence matrices to contain passing named cases for `root_reconciliation_lock`, `instance_lifetime_lock`, `owner_record_create`, and `owner_record_replace`. Missing evidence fails startup; this is a core manifest gate, not a best-effort runtime check.

- [ ] **Step 2: Run tests and verify the missing implementation**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_runtime_root.py tests/integration/test_runtime_root_darwin.py -v`

Expected: FAIL because `RuntimeRoot` is undefined.

- [ ] **Step 3: Implement descriptor-relative runtime ownership**

```python
class RuntimeRoot:
    @classmethod
    def open(cls, path: Path, *, expected_uid: int | None = None) -> "RuntimeRoot": ...
    async def acquire_reconciliation(self, deadline: Deadline) -> "RootReconciliationLease": ...


@dataclass(frozen=True, slots=True)
class OwnerRecord:
    schema_version: Literal[1]
    instance_nonce: str
    boot_id: str
    os_session_id: str
    proxy_pid: int
    proxy_start_time_ns: int
    proxy_uid: int
    proxy_executable_device: int
    proxy_executable_inode: int
    proxy_executable_sha256: str
    takeover_counter: int


class RootReconciliationLease:
    async def create_instance(self, owner: OwnerRecord) -> "InstanceRuntime": ...
    async def claim_abandoned_instance(
        self, name: str, replacement_owner: OwnerRecord
    ) -> "ClaimedInstance | None": ...
    async def close(self) -> None: ...


class InstanceLifetimeLock:
    @property
    def held(self) -> bool: ...
    async def close_after_instance_removed(self, instance_removed: bool) -> None: ...


class GeneratedMasterKeyFormatV1:
    ENTROPY_BYTES: Final[int] = 32
    ENCODED_BYTES: Final[int] = 43
    FILE_BYTES: Final[int] = 44
    ALPHABET: Final[bytes] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

    @classmethod
    def generate(cls) -> SecretBytes: ...
    @classmethod
    def encode_file(cls, secret: SecretBytes) -> bytearray: ...
    @classmethod
    def decode_file(cls, raw: bytearray) -> SecretBytes: ...


class InstanceRuntime:
    path: Path
    dir_fd: int
    owner: OwnerRecord
    lifetime_lock: InstanceLifetimeLock
    def create_master_key(self) -> "MasterKeyHandle": ...


class ClaimedInstance(InstanceRuntime):
    previous_owner: OwnerRecord


class MasterKeyState(StrEnum):
    CREATING = "creating"
    PUBLISHED = "published"
    CLEANUP_PENDING = "cleanup_pending"
    CLOSED = "closed"


class MasterKeyCreationBoundary(StrEnum):
    PRE_OPEN = "pre_open"
    OPEN_UNCONFIRMED = "open_unconfirmed"
    ENTRY_CREATED = "entry_created"
    ENTRY_DURABLE = "entry_durable"
    RETURN_READY = "return_ready"


class MasterKeyAbsenceBasis(StrEnum):
    NEVER_CREATED = "never_created"
    RECONCILED_ABSENT = "reconciled_absent"
    VERIFIED_UNLINK = "verified_unlink"


@dataclass(frozen=True, slots=True)
class MasterKeyFileIdentity:
    device: int
    inode: int
    uid: int
    mode: Literal[0o600]
    link_count: Literal[1]


@dataclass(frozen=True, slots=True)
class MasterKeyCleanupReceipt:
    secret_zeroized: Literal[True]
    file_entry_created: bool
    file_unlinked: bool
    absence_basis: MasterKeyAbsenceBasis
    durable_absence: Literal[True]
    parent_dirsynced: Literal[True]
    descriptors_closed: Literal[True]


class MasterKeyHandle:
    secret: SecretBytes | None
    created_file_identity: MasterKeyFileIdentity | None
    file_entry_created: bool
    file_unlinked: bool
    creation_boundary: MasterKeyCreationBoundary
    cleanup_receipt: MasterKeyCleanupReceipt | None
    path: Path | None
    startup_message: str | None
    state: MasterKeyState
    def close_after_cleanup(
        self, cleanup_done: bool,
    ) -> MasterKeyCleanupReceipt: ...
    def diagnostic_state(self) -> Mapping[str, JsonValue]: ...
```

The transition table is closed and monotonic. `CREATING/PRE_OPEN` permits `secret=None|SecretBytes`, requires no entry and no identity, and covers every point before the `openat` invocation. `CREATING/OPEN_UNCONFIRMED` requires the installed secret but still has no proven entry or identity. `CREATING/ENTRY_CREATED` requires `file_entry_created=True`; `created_file_identity` may be `None` only for the bounded interval immediately after the returned FD, while that FD is still exclusively owned and can supply the identity. `ENTRY_DURABLE` and `RETURN_READY` require an installed secret, a created entry, and stored identity. Creation advances only `PRE_OPEN -> OPEN_UNCONFIRMED -> ENTRY_CREATED -> ENTRY_DURABLE -> RETURN_READY`; successful publication changes only `CREATING/RETURN_READY -> PUBLISHED/RETURN_READY`. Any caught creation fault or an approved returned-handle close changes `CREATING|PUBLISHED -> CLEANUP_PENDING` without moving the creation boundary backward. Only a complete immutable receipt changes `CLEANUP_PENDING -> CLOSED`; no other transition or reopening is valid. `file_unlinked=True` can first appear only in `CLEANUP_PENDING` after identity-verified `unlinkat`, implies `file_entry_created=True`, and is never reset. Closing clears the optional secret and identity references only after their zeroization/cleanup obligations have been discharged. A receipt's booleans freeze the historical creation/unlink facts; `secret_zeroized=True` means no secret-bearing allocation remains live, including the case where creation failed before a `SecretBytes` was installed.

Use one canonical nonduplicated descriptor and in-process mutex per root/lifetime lock domain, `openat(O_RDONLY|O_NOFOLLOW|O_CLOEXEC)`, verified regular-file/UID/mode/device/inode identity, `flock(LOCK_EX|LOCK_NB)` retried only under the monotonic deadline, and the Phase 0 fork/exec descriptor rules. Lock files remain stable and are never renamed or unlinked while their protected root/instance exists. Releasing the root lease does not release any created or claimed lifetime lock.

Create an owner record with `openat(O_CREAT|O_EXCL)` mode `0600`, a complete bounded write, `F_FULLFSYNC`, and parent-directory `fsync`. Replace it only after the abandoned lifetime lock is held and prior-owner absence is proved: write/full-sync a mode-`0600` same-directory temporary record, `renameat` over the owner path, then `fsync` the instance directory. The replacement preserves `instance_nonce`, uses the current proxy identity, and sets `takeover_counter = previous.takeover_counter + 1`; it is diagnostic metadata and never authorizes allocation action. Refuse any key/allocation creation unless the caller's lifetime lock remains held. Refuse deletion until `cleanup_done=True`.

`GeneratedMasterKeyFormatV1` is the single producer/consumer byte contract. `generate()` obtains exactly 32 bytes (256 bits) from the OS CSPRNG into a mutable entropy buffer, encodes them as canonical unpadded RFC 4648 base64url, transfers exactly 43 ASCII/UTF-8-identical token bytes into one `SecretBytes`, and zeroizes the entropy and encoder scratch buffers in `finally` on every success/failure path. The token alphabet is exactly `[A-Za-z0-9_-]`; padding, whitespace, controls, NUL, and non-ASCII are forbidden. `encode_file()` returns a newly owned mutable buffer containing exactly those 43 bytes followed by one `LF` byte, for an exact file size of 44 bytes—no BOM, CR, second LF, trailing bytes, or other whitespace. Its caller owns and zeroizes that buffer. `decode_file()` requires exact length/alphabet/final LF, base64url-decodes to exactly 32 bytes, re-encodes and constant-time-compares the canonical 43-byte token, transfers only the fresh 43-byte token allocation into a returned `SecretBytes`, and zeroizes raw, decoded, re-encoding, and rejected-output buffers in `finally` on success and every exception.

`MasterKeyHandle` is the exclusive owner from before entropy acquisition until durable deletion completes. `InstanceRuntime.create_master_key()` first constructs one internal `CREATING` handle with `secret=None`, `created_file_identity=None`, `file_entry_created=False`, `file_unlinked=False`, `creation_boundary=PRE_OPEN`, no receipt, the retained verified instance-directory descriptor lease, and the fixed validated basename. The handle invokes `GeneratedMasterKeyFormatV1.generate()` inside its private creation guard and directly adopts the sole successful `SecretBytes` allocation before any later step or callback can observe it; until that transfer `secret` remains `None`. The format helper owns/zeroizes entropy and encoder scratch until transfer; the handle thereafter owns the token, mutable file buffer, any open key FD, basename, and directory lease. No other object may close, unlink, copy, or zeroize those resources.

Immediately before invoking `openat`, set `creation_boundary=OPEN_UNCONFIRMED`. Only a returned key FD proves this process created the `O_EXCL` entry; at that exact boundary set `file_entry_created=True` and `creation_boundary=ENTRY_CREATED`. Capture its `MasterKeyFileIdentity` from the owned FD and then require the descriptor/name identity match. Verify current UID, regular-file type, link count one, exact mode `0600`, and same device as the retained mode-`0700` instance directory; perform one complete bounded 44-byte write, `F_FULLFSYNC` the file, close the key FD, then `fsync` the instance directory. After that sync set `creation_boundary=ENTRY_DURABLE`; immediately before the return boundary set `RETURN_READY`. The mutable file buffer is zeroized immediately after the write attempt and every key FD is closed in `finally`. Only after file sync, FD close, and parent sync all succeed may the handle atomically enter `PUBLISHED`, construct `path`/`startup_message`, and return.

Every exception—including after internal publication but before the Python return boundary—is caught by `create_master_key()`. It publishes no startup path, transitions the still-exclusive provisional handle to `CLEANUP_PENDING`, transfers it to `InstanceRuntime`/`ServerLifecycle` cleanup ownership, and enters cleanup-only mode. That owner zeroizes every remaining mutable buffer; if `secret is not None`, it closes/zeroizes that exact allocation and then sets `secret=None`; it closes any key FD in all branches.

Cleanup selects exactly one creation-boundary branch:

- `PRE_OPEN` with `file_entry_created=False` is authoritative proof that this handle never invoked `openat`. It performs no `fstatat` and no `unlinkat`. Under the still-held instance lifetime lock and directory lease, it records `absence_basis=NEVER_CREATED`, performs a current `fsync(instance_dir_fd)` to satisfy the uniform durable-absence receipt contract, and may then close descriptors and produce `file_entry_created=False, file_unlinked=False, durable_absence=True`.
- `OPEN_UNCONFIRMED` with no returned FD is not treated as known absence. Reconciliation may use descriptor-relative no-follow lookup once: `ENOENT` followed by current parent `fsync` yields `RECONCILED_ABSENT`; an entry with no recorded created identity is unexplained and is never unlinked. This branch remains cleanup-only until absence is proved or operator/reconciliation policy resolves corruption.
- `file_entry_created=True` requires a `created_file_identity` before any name lookup or removal. If a fault occurred immediately after FD return, cleanup first derives that identity from the still-owned FD; absence of both the identity and that FD is unresolved ownership corruption, never permission to unlink. Cleanup then uses descriptor-relative `fstatat(..., AT_SYMLINK_NOFOLLOW)` to require the fixed name still has the exact created regular-file/UID/mode/link-count/device/inode identity. Only that verified branch may call `unlinkat`; success sets `file_unlinked=True` and requires parent `fsync` before `absence_basis=VERIFIED_UNLINK` and a receipt.

`ENOENT` has three noninterchangeable meanings. `PRE_OPEN` never issues the lookup and records known-not-created. `file_unlinked=True` means this handle already performed the verified unlink; retry does not unlink or identity-check again and requires a fresh successful parent sync. `file_entry_created=True, file_unlinked=False` plus `ENOENT` is unexplained disappearance, remains `CLEANUP_PENDING`, and cannot produce a receipt. An `unlinkat` success followed by `fsync` failure records the known-unlinked state; retry performs only the missing directory sync. Any identity/unlink/sync/descriptor failure keeps exclusive cleanup ownership and the verified directory lease, emits only redacted stage/error/retry diagnostics, and is retried by reconciliation. Startup failure cannot return or let the process exit while such ownership is unresolved.

For a returned handle, `close_after_cleanup(False)` raises without changing state, secret, file, creation boundary, or descriptor ownership. The first `close_after_cleanup(True)` serializes concurrent callers, transitions `PUBLISHED -> CLEANUP_PENDING`, requires `creation_boundary=RETURN_READY`, `file_entry_created=True`, and exact identity, closes/zeroizes its non-`None` `secret` and sets it to `None`, then follows the verified-unlink branch above. It releases the verified directory lease only after parent sync, clears public `path`/`startup_message` and retained identity, and transitions to `CLOSED` with one immutable `MasterKeyCleanupReceipt(secret_zeroized=True, file_entry_created=True, file_unlinked=True, absence_basis=VERIFIED_UNLINK, durable_absence=True, parent_dirsynced=True, descriptors_closed=True)`. A failure remains `CLEANUP_PENDING` and is retryable from the exact recorded boundary; diagnostic/error output still cannot expose the retained path. Once `CLOSED`, every call returns the identical receipt object and performs no syscall or zeroization again. Pre-open provisional cleanup reaches the same `CLOSED` state and receipt type with the never-created field values. `ServerLifecycle` cannot report shutdown complete, release the instance lifetime lock, or remove the instance directory until it holds a receipt with durable absence and closed descriptors. Abandoned-instance reconciliation never reads the key: under the claimed lifetime lock it applies the corresponding boundary-aware absence/unlink-and-parent-sync protocol before instance removal.

`diagnostic_state()` exposes only state, creation boundary, whether a secret is installed/zeroized, whether buffers are zeroized, whether a key FD/directory lease remains owned, whether created identity is present (not its values), file-entry-created/unlinked, absence basis, durable-absence and parent-sync booleans, retry count, and a stable error code. It never contains the path, basename, file identity values, secret bytes/hash/fingerprint, raw buffers, descriptor numbers, exception text, or startup message. Tests retain test-only aliases to mutable arrays solely to prove zeroization; production diagnostics never do.

`GeneratedMasterKey` calls `create_master_key()`, transfers the returned handle—not a copied secret—to `ServerLifecycle`, and publishes only its path after `PUBLISHED`. Authentication first requires `state=PUBLISHED` and `secret is not None`, then receives a revocable read-only borrow while the handle remains the sole owner; it may compare request bytes but may not close, retain, stringify, or copy the secret. Shutdown revokes/drains all borrows before `close_after_cleanup(True)`. `ConfiguredMasterKey` retains only the redacted in-memory `SecretBytes` and creates no key file; `NoAuth` creates neither a master-key file nor an externally usable master credential (an independent in-memory signing secret may still mint scoped run tokens). Reject simultaneous modes. Tests assert configured and no-auth startup execute zero master-key file create/open/write/stat calls. Generated-mode startup, listener failure, serving failure, cancellation, normal shutdown, and cleanup-only recovery all converge on the same handle cleanup protocol; no path drops ownership or directly unlinks the key.

- [ ] **Step 4: Run tests and commit**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_runtime_root.py tests/integration/test_runtime_root_darwin.py -v`

Expected: PASS on the supported Darwin fixture; explicit skip elsewhere.

```bash
git add src/claude_sdk_proxy/runtime_root.py tests/unit/test_runtime_root.py tests/integration/test_runtime_root_darwin.py
git commit -m "feat: secure the local runtime root"
```

---

### Task 3: Bind Python Allocations to the Phase 0 C17 Lifecycle Authority

**Files:**
- Modify: `src/claude_sdk_proxy/lifecycle.py`
- Create: `tests/fakes.py`
- Create: `tests/unit/test_lifecycle_client.py`
- Create: `tests/integration/test_lifecycle_protocol.py`

**Interfaces:**
- Consumes: Phase 0 `platform.require_supported_platform()`, descriptor-relative `journal.Journal.create_at()`/`.delete_at()` wrappers over `cpl_journal_create_at`/`cpl_journal_delete_at`, `lifecycle.Lifecycle`, `build/lib/libclaude_proxy_lifecycle.dylib`, `build/bin/claude-proxy-supervisor`, `build/bin/claude-proxy-anchor`, and the fixed binary control frames.
- Produces: `LifecycleManager.reconcile_startup()`, `.reserve()`, `AllocationReservation.materialize()`, `AllocationHandle.launch_transport()`, `.transfer_to_cleanup()`, and `CleanupTicket.wait()`.

- [ ] **Step 1: Write a fake-protocol contract test**

```python
@pytest.mark.anyio
async def test_no_workdir_or_process_precedes_durable_intent(fake_native: FakeLifecycleNative) -> None:
    manager = LifecycleManager(fake_native, cleanup_slots=1)
    reservation = await manager.reserve(AllocationKind.SESSION)
    allocation = await reservation.materialize()
    assert fake_native.commands == [
        ("reserve", allocation.nonce),
        ("journal_create_exclusive", allocation.nonce),
        ("journal_preallocated", allocation.nonce),
        ("intent_appended_complete", allocation.nonce),
        ("intent_fullsynced", allocation.nonce),
        ("intent_parent_dirsynced", allocation.nonce),
        ("workdir_created", allocation.nonce),
        ("workdir_recorded", allocation.nonce),
    ]

    ticket = await allocation.transfer_to_cleanup(TerminalCause.COMPLETE)
    await ticket.wait()
    assert fake_native.commands[-4:] == [
        ("done_certified", allocation.nonce),
        ("journal_unlinked", allocation.nonce),
        ("journal_unlink_parent_dirsynced", allocation.nonce),
        ("cleanup_slot_released", allocation.nonce),
    ]
```

Assert `intent_parent_dirsynced` is a hard gate: the fake must reject both workdir creation and `workdir_recorded` if that receipt is missing, even when `INTENT` file full-sync succeeded. Add the terminal trace `DONE certified -> journal_unlinked -> journal_unlink_parent_dirsynced -> cleanup_slot_released`; omit/fail any step and assert the slot plus instance lifetime ownership remain held. A pre-materialization rollback creates no workdir/process, and `UNCONFIRMED` retains the journal and slot.

Fault-inject the Python wrapper at every production native boundary: exclusive `openat`, `F_PREALLOCATE`, complete `INTENT` append, journal `F_FULLFSYNC`, create parent-directory `fsync`, complete `DONE` append, `DONE` full-sync/durable-head certification, `unlinkat`, and delete parent-directory `fsync`. For every prefix assert the next dependent action was never invoked, no false receipt was returned, and retry/reconciliation is idempotent.

- [ ] **Step 2: Run the contract tests and observe failure**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_lifecycle_client.py -v`

Expected: FAIL because the typed lifecycle client does not exist.

- [ ] **Step 3: Implement the exact Python boundary**

```python
class LifecycleManager:
    async def reconcile_startup(self) -> ReconcileReport: ...
    async def reserve(self, kind: AllocationKind) -> "AllocationReservation": ...


class AllocationReservation:
    nonce: str
    async def materialize(self) -> "AllocationHandle": ...
    async def rollback(self) -> None: ...


class AllocationHandle:
    nonce: str
    workdir: Path
    async def launch_transport(
        self, *, cli_path: Path, environment: Mapping[str, str]
    ) -> "LifecycleTransport": ...
    async def transfer_to_cleanup(self, cause: TerminalCause) -> "CleanupTicket": ...


class CleanupTicket:
    async def wait(self) -> CleanupOutcome: ...
```

`materialize()` passes only a verified retained instance-directory FD plus a single validated basename to `Journal.create_at()`. That production call owns the indivisible commit sequence: `openat(O_CREAT|O_EXCL|O_APPEND, 0600)`, bounded `F_PREALLOCATE`, one complete `INTENT` append, journal `F_FULLFSYNC`, then containing-directory `fsync`. It returns `JournalCreateReceipt(intent_parent_dirsynced=True)` only after all five succeed. Python cannot create the workdir, append `WORKDIR`, or launch any process before consuming that receipt; file full-sync without parent sync is not durable creation authority.

After cleanup reaches certified `DONE` and confirmed process/workdir absence, `CleanupTicket` calls only `Journal.delete_at()` with the retained verified directory FD, basename, and `CertifiedDone` authority. Reconciliation may instead supply `UnreleasedPartialCreate` only when native scan proves no complete canonical `INTENT` and the dependent-artifact scan is empty. No other deletion authority exists. The production call validates that authority, performs `unlinkat` then containing-directory `fsync`, and returns `JournalDeleteReceipt(journal_unlink_parent_dirsynced=True)` only after both succeed. The cleanup-ownership slot and claimed instance lifetime lock cannot release on `DONE`, path absence, or successful `unlinkat` alone; they release only after consuming that receipt. Failure retains ownership and retries through reconciliation.

All other appends use `Journal.append/scan/certify_head`; state reduction uses `Lifecycle.apply(state, record)`. `launch_transport()` sets the supervisor-only `LOCAL_PROXY_ALLOCATION_NONCE`, `LOCAL_PROXY_INSTANCE_DIR`, `LOCAL_PROXY_REAL_CLAUDE`, and `LOCAL_PROXY_CONTROL_FD` values and supplies `build/bin/claude-proxy-supervisor` as `ClaudeAgentOptions.cli_path`. The supervisor strips those variables before launching the real CLI. The wrapper accepts only `SUPERVISOR_IDENTITY`, `IDENTITY_ACK`, `ANCHOR_IDENTITY`, `ANCHOR_ACK`, `CLI_ARMED`, `ARMED_ACK`, `CLI_RUNNING`, `CLEANUP_REQUEST`, `SELF_TERM_REQUEST`, and `CONTROL_ERROR`; every acknowledgement follows `Journal.certify_head()`. Python may request actions and observe records, but exposes no `kill`, `signal_pid`, `signal_pgid`, `unlink_workdir`, raw journal create/delete syscall, or arbitrary action-descriptor method.

- [ ] **Step 4: Test against the real Phase 0 executable**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/integration/test_lifecycle_protocol.py -v`

Expected: PASS for fragmented fixed frames, invalid nonce, pre-ack exit code 75, supervisor/anchor/helper death, `DONE`, and `UNCONFIRMED`; real-library create/delete fault injection proves the exact receipt gates and no journal contains the prompt/token canaries.

- [ ] **Step 5: Commit**

```bash
git add src/claude_sdk_proxy/lifecycle.py tests/fakes.py tests/unit/test_lifecycle_client.py tests/integration/test_lifecycle_protocol.py
git commit -m "feat: bind allocations to lifecycle authority"
```

---

### Task 4: Enforce Cleanup Automaton, Reconciliation, and Shutdown at the Server Boundary

**Files:**
- Modify: `src/claude_sdk_proxy/lifecycle.py`
- Create: `src/claude_sdk_proxy/server.py`
- Create: `tests/integration/test_cleanup_automaton.py`
- Create: `tests/integration/test_startup_reconciliation.py`
- Create: `tests/integration/test_shutdown_parenthood.py`

**Interfaces:**
- Consumes: `RootReconciliationLease`, retained `InstanceLifetimeLock` objects, `OwnerRecord`, `CleanupTicket`, Phase 0 journal states and fault-injection hooks.
- Produces: `CleanupRegistry`, `ServerLifecycle.start()`, `.begin_shutdown()`, and `.wait_until_done_or_cleanup_only()`.

- [ ] **Step 1: Write the normative transition-table test**

```python
LEGAL = {
    "NONE": {"PREPARED"},
    "PREPARED": {"ACTIVE_READY", "RETIRING_IDLE", "UNCONFIRMED"},
    "ACTIVE_READY": {"BATCH_ACTIVE", "DONE", "RETIRING_IDLE", "UNCONFIRMED"},
    "BATCH_ACTIVE": {"ACTIVE_READY", "RETIRING_BATCH", "UNCONFIRMED"},
    "RETIRING_BATCH": {"RETIRING_BATCH", "RETIRING_IDLE", "UNCONFIRMED"},
    "RETIRING_IDLE": {"RETIRING_IDLE", "PREPARED", "UNCONFIRMED"},
    "DONE": set(),
    "UNCONFIRMED": set(),
}
```

Drive the native fault harness through every edge, repeated batches, retirement-before/after-admission, authority replacement, interrupted batches, and tail exhaustion. Assert every unlisted edge is rejected before action.

- [ ] **Step 2: Write startup and shutdown race tests**

Test held versus abandoned lifetime locks, owner-record create/replacement failure at every durability boundary, every bootstrap partial state, corrupt records, executor retirement, live anchor without supervisor, stubborn descendants, writer drain, and an allocation that remains `UNCONFIRMED`. Assert startup holds `RootReconciliationLease` for the complete scan/claim phase; never opens journals beneath a held lifetime lock; retains every successfully claimed abandoned `InstanceLifetimeLock` after releasing the root lease; and cannot accept work until all manifest evidence named in Task 2 has passed.

Drive startup through every production journal prefix. For create prefixes before a complete canonical `INTENT`, prove no workdir/process could have been authorized and remove the partial entry only through `Journal.delete_at()` plus its parent-sync receipt. For a complete/full-synced `INTENT` whose pre-crash create parent-sync completion is unknowable, require a fresh successful containing-directory `fsync` before treating the entry as durably created or replaying it. For deletion prefixes, retry `delete_at()` when a certified-`DONE` journal is present; when the path is absent after a possible pre-crash `unlinkat`, require a fresh successful containing-directory `fsync` before treating deletion as durable or releasing recovered cleanup/lifetime ownership. Inject failure in each recovery sync/retry and assert cleanup-only retention. Neither a present directory entry nor `ENOENT` alone is durable-state evidence.

```python
assert await server.begin_shutdown() is ShutdownState.DRAINING
assert server.may_exit is False
assert server.health is Health.CLEANUP_ONLY
```

- [ ] **Step 3: Run the tests and confirm missing orchestration**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/integration/test_cleanup_automaton.py tests/integration/test_startup_reconciliation.py tests/integration/test_shutdown_parenthood.py -v`

Expected: FAIL because `CleanupRegistry` and `ServerLifecycle` are missing.

- [ ] **Step 4: Implement cleanup ownership without duplicating native authority**

```python
class CleanupRegistry:
    async def adopt(self, ticket: CleanupTicket) -> None: ...
    async def wait_empty(self) -> None: ...
    @property
    def unresolved(self) -> tuple[CleanupStatus, ...]: ...


class ServerLifecycle:
    async def start(self) -> None: ...
    async def begin_shutdown(self) -> ShutdownState: ...
    async def wait_until_done_or_cleanup_only(self) -> ShutdownState: ...
```

Normal shutdown stops admission, terminalizes actors, drains all writer leases, adopts every cleanup ticket, and exits only when all allocations are `DONE`, reaped, journal-deleted, and slot-released. An unresolved allocation keeps the process in cleanup-only mode.

`start()` acquires the root reconciliation lease before enumerating instance entries. For each abandoned instance it first takes and retains that instance's lifetime lock, proves the previous `OwnerRecord` process identity absent, and completes the durable owner replacement before reading allocation journals. A held lifetime lock is skipped without opening owner/allocation state. Root-lease release never closes claimed lifetime locks; a claimed lock is closed only after all owned allocations are confirmed, the instance directory is durably removed, and the cleanup slot is released. Owner-record content is never passed to the native action API as authority.

Reconciliation uses the same descriptor-relative native create/delete wrapper as normal allocation; it has no path-based shortcut. It classifies a journal only after canonical scan plus durable-head certification and a successful current containing-directory sync. An incomplete create is nonauthoritative and authorizes no dependent artifact; an incomplete delete retains recovered cleanup ownership until a successful current parent sync establishes absence. Startup never reconstructs a missing create/delete receipt from path observation, buffered metadata, or owner records.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/integration/test_cleanup_automaton.py tests/integration/test_startup_reconciliation.py tests/integration/test_shutdown_parenthood.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/lifecycle.py src/claude_sdk_proxy/server.py tests/integration/test_cleanup_automaton.py tests/integration/test_startup_reconciliation.py tests/integration/test_shutdown_parenthood.py
git commit -m "feat: reconcile and drain lifecycle ownership"
```

---

### Task 5: Construct the Exact Child Environment and Attest Every SDK Child

**Files:**
- Modify: `src/claude_sdk_proxy/environment.py`
- Modify: `src/claude_sdk_proxy/attestation.py`
- Modify: `tests/unit/test_environment.py`
- Modify: `tests/unit/test_attestation.py`
- Create: `tests/integration/test_child_attestation.py`

**Interfaces:**
- Consumes: Phase 0 environment/auth evidence schemas and `LifecycleTransport` initialization events.
- Produces: production use of Phase 0 `build_child_environment()`, `environment_fingerprint()`, `extract_child_attestation()`, `AttestationGate.release()`, and `AttestationGate.revalidate()` for every child/turn.

- [ ] **Step 1: Write the exact environment matrix test**

```python
ALLOWED = {"HOME", "USER", "LOGNAME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "CLAUDE_CONFIG_DIR"}
AUTH_PROVIDER_ENDPOINT_OVERRIDES = {
    "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
}
STRIPPED = {
    "LOCAL_PROXY_API_KEY", "LOCAL_PROXY_API_KEY_FILE", "RANDOM_HOST_VALUE",
}


@pytest.mark.parametrize("name", sorted(AUTH_PROVIDER_ENDPOINT_OVERRIDES))
def test_auth_provider_or_endpoint_override_rejects_before_return(name: str) -> None:
    with pytest.raises(EnvironmentPolicyError):
        build_child_environment({"HOME": "/tmp/home", name: "secret"}, environment_config)


def test_unallowlisted_and_proxy_only_names_are_stripped() -> None:
    source = {**{name: "x" for name in ALLOWED}, **{name: "secret" for name in STRIPPED}}
    env = build_child_environment(source, environment_config)
    assert STRIPPED.isdisjoint(env)
    assert set(env) == ALLOWED | FIXED_ENVIRONMENT_NAMES | {"PATH"}
    assert env["DISABLE_COMPACT"] == "1"
    assert env["CLAUDE_CODE_SKIP_PROMPT_HISTORY"] == "1"
```

The builder has exactly two outcomes for inherited names. Presence of any API-key, OAuth, cloud-provider credential/configuration, or custom-endpoint override raises `EnvironmentPolicyError` before an environment is returned. Arbitrary unallowlisted host values and proxy-only keys are silently omitted; supervisor bootstrap values are injected separately from validated proxy configuration and are stripped again before the real CLI. Also assert the rebuilt `PATH`, the exact inherited-name allowlist, all fixed isolation variables, opt-in-only network-proxy variables, and rejection of ambiguous unrecognized `ANTHROPIC_*`, `CLAUDE_*`, or `LOCAL_PROXY_*` names. No test may treat an auth/provider/endpoint override as merely sanitized.

- [ ] **Step 2: Write per-child attestation failures**

Parameterize API-key, API-key-helper, cloud provider, custom endpoint, unknown auth, wrong executable/version, environment mismatch, evidence disappearance, and mode change. Assert `release_input` remains false.

- [ ] **Step 3: Run tests and verify failure**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_environment.py tests/unit/test_attestation.py -v`

Expected: FAIL because the production backend has not yet constructed its child from the Phase 0 environment or required the Phase 0 gate before every turn.

- [ ] **Step 4: Wire the Phase 0 gate into the production child path**

```python
env = build_child_environment(source_environment, environment_config)
fingerprint = environment_fingerprint(env)
attestation = extract_child_attestation(init_event, manifest)
gate = AttestationGate(attestation, manifest)
turn = gate.queue_turn(system=session.system, blocks=new_blocks)
await gate.release(turn)
if manifest.auth_revalidation == "each_turn":
    gate.revalidate(next_init_evidence)
```

The gate consumes only non-secret evidence and never opens stored profile or credential contents.

- [ ] **Step 5: Run live-boundary tests and commit**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_environment.py tests/unit/test_attestation.py tests/integration/test_child_attestation.py -v`

Expected: PASS, including traces proving no caller content reaches a model/network peer before initial attestation.

```bash
git add src/claude_sdk_proxy/environment.py src/claude_sdk_proxy/attestation.py tests/unit/test_environment.py tests/unit/test_attestation.py tests/integration/test_child_attestation.py
git commit -m "feat: attest every subscription child"
```

---

### Task 6: Implement the SDK-Independent Backend and Exact Model Gate

**Files:**
- Create: `src/claude_sdk_proxy/backend.py`
- Modify: `tests/fakes.py`
- Create: `tests/unit/test_backend.py`
- Create: `tests/integration/test_backend_model_identity.py`

**Interfaces:**
- Consumes: `AllocationHandle`, child environment, `AttestationGate`, canonical blocks, exact backend model ID.
- Produces: `BackendFactory.prepare()`, `PreparedBackend.initialize()`, `PreparedBackend.watch_liveness()`, `PreparedBackend.start_turn()`, `BackendOperation.events()`, and `mutation_possible`.

- [ ] **Step 1: Write backend protocol tests**

```python
@dataclass(frozen=True, slots=True)
class BackendLoss:
    cause: BackendLossCause
    cleanup_ticket: CleanupTicket


class BackendLossCause(StrEnum):
    CHILD_EXITED = "child_exited"
    SDK_TRANSPORT_LOST = "sdk_transport_lost"
    SUPERVISOR_CHANNEL_LOST = "supervisor_channel_lost"
    AUTH_ATTESTATION_LOST = "auth_attestation_lost"


class PreparedBackend(Protocol):
    async def initialize(self) -> AttestedChild: ...
    async def watch_liveness(self) -> BackendLoss: ...
    async def revalidate_before_turn(self) -> None: ...
    async def start_turn(self, blocks: tuple[CanonicalInputBlock, ...]) -> "BackendOperation": ...
    async def close(self) -> CleanupTicket: ...


class BackendOperation(Protocol):
    @property
    def mutation_possible(self) -> bool: ...
    def events(self) -> AsyncIterator[CanonicalEvent]: ...
    async def cancel(self) -> None: ...
```

Assert the fake flips `mutation_possible=True` immediately before its simulated SDK query call, never after it.

`watch_liveness()` is the single actor-owned future for unexpected child, SDK transport, supervisor control-channel, or attestation-lifetime loss. It completes once with a content-free `BackendLoss(cause, cleanup_ticket)` and never consumes SDK response events. Intentional `close()` cancels/joins the watcher without synthesizing a loss. Add fake tests for loss-before-first-turn, loss-during-event-iteration, watcher/close races, duplicate native notifications, and exact-one `CleanupTicket` transfer.

- [ ] **Step 2: Write model/result protocol tests**

Inject missing, mismatched, fallback, and later-changing model IDs; missing usage; unsuccessful result; unknown stop/event; invalid thinking/signature ordering; and iterator failure. Feed an ordinary feasibility row with required/optional/nested integer/string/boolean/nullable usage leaves and separate passing Anthropic/OpenAI mappings; assert every SDK-supplied field/path/kind/value reaches `CanonicalResult.usage`, optional absence stays absent, and missing required, extra unknown, changed type/path, negative count, wrong operation class, false dialect mapping, or row/schema digest mismatch releases no content/success and produces `502 sdk_protocol_error`. For an enabled exact tuple, assert normalization produces immutable `ThinkingBlock` and `RedactedThinkingBlock` values with exact payload/signature/order. For disabled/adaptive configurations—even passing Phase 0 rows—assert construction is rejected before backend preparation; for an unexpected backend thinking event outside the selected enabled tuple, assert no content or success artifact is released, `502 sdk_protocol_error` is selected, and a reusable session becomes `LOST` after possible mutation.

- [ ] **Step 3: Run tests and observe missing backend**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_backend.py tests/integration/test_backend_model_identity.py -v`

Expected: FAIL because the backend protocol and adapter are missing.

- [ ] **Step 4: Implement the pinned SDK adapter**

Construct one `ClaudeSDKClient` per backend with exact model ID, caller system string, the implementation-allowlisted feasibility-approved enabled thinking/effort tuple (or no thinking), `tools=[]`, `skills=[]`, `setting_sources=[]`, empty agents, strict MCP, and no MCP servers. Send one structured raw user message preserving block order; never forward request metadata. Buffer events through the first authoritative model-bearing envelope; validate every later model field; preserve approved thinking, redacted-thinking, signature, and delta ordering; require the session's bound `ordinary` row digest, normalize the complete raw SDK usage mapping through `CanonicalUsage.from_sdk(raw, usage_schema, exact_row)`, then require successful `ResultMessage` and iterator completion. The backend never hand-picks input/output counters, renders a public dialect, or silently ignores an SDK usage key.

Start native child/control monitoring during `initialize()` and expose its terminal receive cell only through `watch_liveness()`. The prepared backend owns that receive side until the session actor adopts it; neither an HTTP task nor a response iterator may own or poll child liveness. A native loss atomically detaches the backend's cleanup ticket so later `close()` is idempotent and cannot transfer the same allocation twice.

```python
class BackendFactory:
    async def prepare(
        self, config: ImmutableSessionConfig, allocation: AllocationHandle
    ) -> PreparedBackend: ...
```

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_backend.py tests/integration/test_backend_model_identity.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/backend.py tests/fakes.py tests/unit/test_backend.py tests/integration/test_backend_model_identity.py
git commit -m "feat: gate the Agent SDK backend"
```

---

### Task 7: Implement Atomic Lifecycle and Operation Capacity Bundles

**Files:**
- Create: `src/claude_sdk_proxy/capacity.py`
- Create: `tests/unit/test_capacity.py`
- Create: `tests/unit/test_lifecycle_capacity.py`

**Interfaces:**
- Consumes: positive limits from `ProxyConfig`.
- Produces: `CapacityLedger.reserve_run_lifecycle()`, `.reserve_session_lifecycle()`, `.reserve_one_shot()`, `.reserve_operation()`, `.reserve_retry_waiter()`, `.reserve_replay_writer()`, `OperationLease`, `RetryWaiterLease`, `ReplayWriterLease`, and exact-once lease release.

- [ ] **Step 1: Write atomic reservation fault tests**

```python
@pytest.mark.parametrize("failed_component", ["entry", "session_bytes", "global_bytes", "generation", "writer", "queue"])
def test_operation_bundle_rolls_back_every_component(failed_component: str) -> None:
    ledger = CapacityLedger.for_tests(fail_on=failed_component)
    before = ledger.snapshot()
    with pytest.raises(CapacityError):
        ledger.reserve_operation(OperationDemand.one_segment(4096))
    assert ledger.snapshot() == before
```

Test run creation atomically reserves run + future automatic-session + cleanup slots; explicit creation reserves session + cleanup; one-shot reserves ephemeral cleanup; no terminal slot is evicted.

Add atomic fault tests for the two post-lookup bundles. `reserve_retry_waiter(queue_bytes)` reserves exactly one retry-waiter, response-writer, and bounded writer-queue allocation or rolls all three back. `reserve_replay_writer(queue_bytes)` reserves exactly one response-writer and bounded writer-queue allocation or rolls both back. Exhaust every new-operation-only component (idempotency entry, replay bytes, generation slot) while leaving writer capacity and prove an already committed replay still admits; exhaust replay-writer capacity and prove it does not consume or probe new-operation capacity. Conflict, in-progress-stream, invalid-head, and invalid-transcript paths must leave the complete ledger snapshot unchanged.

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_capacity.py tests/unit/test_lifecycle_capacity.py -v`

Expected: FAIL because `CapacityLedger` is absent.

- [ ] **Step 3: Implement exact demand and lease types**

```python
@dataclass(frozen=True, slots=True)
class OperationDemand:
    idempotency_entries: int
    reserved_replay_bytes: int
    generations: int
    response_writers: int
    writer_queue_bytes: int


class CapacityLedger:
    def reserve_run_lifecycle(self) -> RunLifecycleLease: ...
    def reserve_session_lifecycle(self) -> SessionLifecycleLease: ...
    def reserve_one_shot(self) -> CleanupLease: ...
    def reserve_operation(self, demand: OperationDemand) -> OperationLease: ...
    def reserve_retry_waiter(self, queue_bytes: int) -> RetryWaiterLease: ...
    def reserve_replay_writer(self, queue_bytes: int) -> ReplayWriterLease: ...
```

`RetryWaiterLease` owns only retry-waiter/writer/queue counters; `ReplayWriterLease` owns only writer/queue counters. Neither can be converted into an `OperationLease` or charge idempotency, replay-storage, generation, session, or cleanup capacity. Every lease uses one internal released-state CAS and raises on double transfer or double release during tests. Entry/session/global replay exhaustion maps to `409 idempotency_capacity`; generation, retry-waiter, and response-writer exhaustion map to their documented `429` codes; lifecycle exhaustion maps to `429 lifecycle_capacity` before any ID, token, journal, workdir, or process exists.

- [ ] **Step 4: Run tests and commit**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_capacity.py tests/unit/test_lifecycle_capacity.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/capacity.py tests/unit/test_capacity.py tests/unit/test_lifecycle_capacity.py
git commit -m "feat: reserve bounded proxy resources"
```

---

### Task 8: Implement HMAC Fingerprints, Exact Replay Artifacts, and Unified Writer Leases

**Files:**
- Create: `src/claude_sdk_proxy/replay.py`
- Create: `tests/unit/test_fingerprints.py`
- Create: `tests/unit/test_replay.py`
- Create: `tests/unit/test_writer_leases.py`

**Interfaces:**
- Consumes: `CanonicalRequest`, normalized endpoint/header inputs, `OperationLease`, `RetryWaiterLease`, `ReplayWriterLease`, `Clock`.
- Produces: `fingerprint_request()`, `ReplayReservation`, `ReplayArtifact`, `ReplaySnapshot`, `WriterLease`, and `WriterLease.finish_once()`.

- [ ] **Step 1: Write fingerprint coverage tests**

```python
def test_fingerprint_covers_every_semantic_and_framing_input(process_hmac_key: bytes) -> None:
    base = FingerprintInput(method="POST", endpoint="/v1/messages", head="head_a", body=BODY, stream=True, policy="messages_compat", framing_headers=(("anthropic-version", "2023-06-01"),))
    assert fingerprint_request(process_hmac_key, base) != fingerprint_request(process_hmac_key, replace(base, head="head_b"))
    assert fingerprint_request(process_hmac_key, base) == fingerprint_request(process_hmac_key, replace(base, host="ignored.example"))
```

Cover endpoint/dialect, head, canonical body/history/config, stream, policy, and allowlisted framing headers; prove credentials, Host, Origin, Date, request ID, and connection headers are excluded. Include exact thinking text, redacted opaque data, optional signatures, block types, and block order: changing any byte/value or order changes the HMAC, while an identical canonical resend is stable. A disabled exact tuple rejects before fingerprint/admission rather than silently omitting thinking.

- [ ] **Step 2: Write replay and writer race tests**

Test immutable status/headers/result-head/body bytes, reference counts after owner drop, admission versus terminal races, forced close at the monotonic write deadline, and exact-once release on completion/disconnect/timeout. Assert thinking/redacted-thinking payload, signature, block order, every mapped canonical usage field/value (including nested optional fields), and SSE bytes survive commit, snapshot, and historical replay exactly; replay compares the complete serialized bytes, not reconstructed usage. Assert lease start points separately: original stream immediately before headers; successful non-stream original at commit; post-reservation JSON error immediately before it becomes writable; promoted waiter at commit/error using only its `RetryWaiterLease`; historical replay at snapshot admission using only its `ReplayWriterLease`. A detached slow original cannot cancel a bounded operation or retain capacity after its lease. Prove committed replay remains admissible when every new-operation-only capacity is exhausted.

- [ ] **Step 3: Run tests and verify failure**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_fingerprints.py tests/unit/test_replay.py tests/unit/test_writer_leases.py -v`

Expected: FAIL because replay types are missing.

- [ ] **Step 4: Implement replay ownership**

```python
def fingerprint_request(key: bytes, value: FingerprintInput) -> bytes: ...


class ReplayReservation:
    def append(self, chunk: bytes) -> None: ...
    def commit(self, status: int, headers: tuple[tuple[bytes, bytes], ...], result_head: str) -> ReplayArtifact: ...
    def abandon(self) -> None: ...


class ReplayArtifact:
    def admit_snapshot(self, lease: ReplayWriterLease | RetryWaiterLease) -> ReplaySnapshot: ...
    def drop_owner(self) -> None: ...


class WriterLease:
    def start(self, clock: Clock) -> None: ...
    def finish_once(self, cause: WriterFinish) -> bool: ...
```

`WriterLease` adopts exactly one original-operation writer component, `RetryWaiterLease`, or `ReplayWriterLease` and returns every adopted counter through its single finish CAS. Account serialized headers, body/SSE, fingerprint/key metadata, result head, and fixed overhead continuously from reservation through the final snapshot release. Never evict a committed artifact while its session remains live and nonterminal; new-operation capacity exhaustion never blocks an existing replay, while replay-writer exhaustion rejects only that snapshot admission.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_fingerprints.py tests/unit/test_replay.py tests/unit/test_writer_leases.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/replay.py tests/unit/test_fingerprints.py tests/unit/test_replay.py tests/unit/test_writer_leases.py
git commit -m "feat: cache exact bounded responses"
```

---

### Task 9: Implement Master Authentication, Signed Run Tokens, and Retired Digests

**Files:**
- Create: `src/claude_sdk_proxy/tokens.py`
- Create: `src/claude_sdk_proxy/runs.py`
- Create: `tests/unit/test_tokens.py`
- Create: `tests/unit/test_runs.py`
- Create: `tests/unit/test_credential_matrix.py`

**Interfaces:**
- Consumes: master key, `Clock`, `DeadlineScheduler`, run lifecycle leases, dialect/policy/TTL.
- Produces: `TokenCodec.mint()`, `RunRegistry.create()`, `.authenticate()`, `.bind()`, `.retire()`, `TerminalCell`, `MasterPrincipal`, and `RunPrincipal`.

- [ ] **Step 1: Write token and irreversible-run tests**

```python
@pytest.mark.anyio
async def test_run_never_rebinds_after_terminal_cell_closes(registry: RunRegistry) -> None:
    minted = await registry.create(RunSpec.anthropic(ttl=60))
    bound = await registry.bind(minted.token, "ses_a")
    assert bound.terminal_cell.close(TerminalReason.SESSION_LOST)
    with pytest.raises(ProxyError, match="session_lost"):
        await registry.bind(minted.token, "ses_b")
```

Test signed-token forgery, keyed digest lookup, dialect binding, monotonic expiry, explicit revocation, stable terminal cell, and token values absent from stored records/log representations. A terminal-bound but unexpired active token may still use its own models/capabilities endpoints, while every inference attempt returns its terminal-cell result and can never bind again. Create an unbound run, advance only the fake monotonic clock past TTL without making another request, and assert the registry-owned scheduled callback retires it, releases its future session/cleanup reservations exactly once, retains the retired digest through tombstone TTL, and leaves no deadline task/handle.

- [ ] **Step 2: Write the retired-digest authorization table**

For an exact retired digest, assert only its bound dialect inference path and `DELETE /_proxy/runs/{exact_run_id}` return the stored `410`; all model/capability/session/run/other-dialect/other-ID paths return `401`; after token-tombstone expiry every path returns `401`.

- [ ] **Step 3: Run tests and observe missing registry**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_tokens.py tests/unit/test_runs.py tests/unit/test_credential_matrix.py -v`

Expected: FAIL because token and run registries are absent.

- [ ] **Step 4: Implement token classes and lock order**

```python
class TokenCodec:
    def mint(self, run_id: str) -> str: ...
    def verify_and_digest(self, token: str) -> bytes | None: ...


class RunRegistry:
    async def start_deadline_owner(self, task_group: anyio.abc.TaskGroup) -> None: ...
    async def create(self, spec: RunSpec) -> MintedRun: ...
    async def authenticate(self, token: str, endpoint: EndpointIdentity) -> RunLookup: ...
    async def bind(self, token: str, session_id: str) -> BoundRun: ...
    async def retire(self, token: str, reason: TerminalReason) -> RetiredRun: ...
```

Use run-registry lock → actor lock; actor code never acquires run registry; teardown starts after both are released. The registry owns one scheduler handle for every active run from publication, including an unbound run that receives no later request, plus one handle for retired-digest expiry. A callback acquires the registry lock, validates run ID/generation/current deadline, recomputes monotonic expiry, and either reschedules or performs the same irreversible transition as explicit retirement; it never trusts timer punctuality. Retire removes active authorization, cancels/transfers the active handle exactly once, terminalizes the bound actor, transfers cleanup, then publishes the non-authorizing digest record and its tombstone handle atomically.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_tokens.py tests/unit/test_runs.py tests/unit/test_credential_matrix.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/tokens.py src/claude_sdk_proxy/runs.py tests/unit/test_tokens.py tests/unit/test_runs.py tests/unit/test_credential_matrix.py
git commit -m "feat: add scoped run credentials"
```

---

### Task 10: Validate Selected-Session Transcripts and Implement Session Tombstones

**Files:**
- Create: `src/claude_sdk_proxy/transcript.py`
- Create: `src/claude_sdk_proxy/sessions.py`
- Create: `tests/unit/test_transcript.py`
- Create: `tests/unit/test_sessions.py`
- Create: `tests/unit/test_automatic_binding.py`

**Interfaces:**
- Consumes: canonical messages, lifecycle leases, backend factory, run terminal cell, `DeadlineScheduler`.
- Produces: `SessionSpec`, `validate_continuation()`, `SessionRegistry.create_explicit()`, `.bind_automatic()`, `.create_one_shot()`, `.lookup()`, `.terminalize()`, and reason-only tombstones.

- [ ] **Step 1: Write transcript validation tests**

```python
def test_continuation_returns_only_the_new_user_turn() -> None:
    result = validate_continuation(recorded=(USER_1, ASSISTANT_1), submitted=(USER_1, ASSISTANT_1, USER_2))
    assert result == (USER_2,)
```

Reject altered prior user/assistant blocks, unknown history, stale prefixes, branches, more than one new turn, assistant-ending requests, immutable config changes, and any transcript-based global lookup. Record assistant content interleaving thinking, redacted-thinking, and text blocks; require exact thinking/redacted payloads, optional signatures, block types, and order on continuation. Any signature/payload/order change is a stale/divergent prefix. If the immutable session tuple's thinking gate is disabled, reject thinking-bearing history before backend allocation or transcript fingerprinting.

- [ ] **Step 2: Write lifecycle and tombstone tests**

Test explicit lazy creation, automatic simultaneous identical first bind, divergent first-bind race, run future-slot transfer, one-shot ephemeral cleanup reservation, live/tombstone capacity, stable reason mapping, and removal semantics: lost tombstone → `404`; expired/closed tombstone → `404` after its TTL. Create an idle explicit session, make no inference request, advance only monotonic time beyond its TTL, and assert its actor-owned deadline task transitions it to `CLOSED(session_expired)`, rolls back an unmaterialized backend/cleanup reservation or transfers a materialized allocation exactly once, publishes the reason-only tombstone, and later removes that tombstone on its own scheduled TTL.

- [ ] **Step 3: Run tests and confirm missing registries**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_transcript.py tests/unit/test_sessions.py tests/unit/test_automatic_binding.py -v`

Expected: FAIL because transcript/session modules are absent.

- [ ] **Step 4: Implement selected-session-only validation**

```python
@dataclass(frozen=True, slots=True)
class SessionSpec:
    config: ImmutableSessionConfig
    ttl_seconds: int


class SessionRegistry:
    async def create_explicit(self, spec: SessionSpec, principal: Principal) -> SessionHandle: ...
    async def bind_automatic(self, run: BoundOrUnboundRun, request: CanonicalRequest) -> SessionHandle: ...
    async def create_one_shot(self, request: CanonicalRequest, principal: Principal) -> SessionHandle: ...
    async def lookup(self, session_id: str, principal: Principal) -> SessionLookup: ...
    async def terminalize(self, session_id: str, reason: TerminalReason) -> CleanupTicket: ...
```

The registry stores no transcript in tombstones and never evicts a live or terminal authoritative record early. Actor construction arms the session deadline before publishing the actor handle; tombstone publication arms its removal deadline. Both callbacks validate opaque session ID, generation, current `Deadline`, and state under the owner lock. Request-time expiry checks remain mandatory race checks but are not the mechanism that makes idle records expire.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_transcript.py tests/unit/test_sessions.py tests/unit/test_automatic_binding.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/transcript.py src/claude_sdk_proxy/sessions.py tests/unit/test_transcript.py tests/unit/test_sessions.py tests/unit/test_automatic_binding.py
git commit -m "feat: select and retain linear sessions"
```

---

### Task 11: Implement the Session Actor, Idempotency Ordering, and Failure Table

**Files:**
- Create: `src/claude_sdk_proxy/actor.py`
- Modify: `tests/fakes.py`
- Create: `tests/unit/test_actor_state.py`
- Create: `tests/unit/test_idempotency.py`
- Create: `tests/unit/test_actor_failures.py`
- Create: `tests/unit/test_actor_deadlines.py`

**Interfaces:**
- Consumes: backend, transcript validator, capacity ledger, terminal cell, `DeadlineScheduler`, deadlines.
- Produces: `ActorRequest`, `CommittedReply`, `SessionActor.admit()`, `SessionActor.set_pending_tool_deadline()`, `OperationRecord`, `AdmissionResult`, `SessionActor.close()`, and states `IDLE`, `GENERATING`, `WAITING_FOR_TOOLS`, `LOST`, `CLOSED`.

- [ ] **Step 1: Write lookup-order and concurrency tests**

Assert the exact admission sequence and ledger snapshots at every return point:

1. authentication and session selection occur outside the actor, then the actor lock rechecks terminal cell and run/session/operation deadlines;
2. idempotency lookup occurs before head or transcript validation, with explicit records scoped by `(principal, session_id, idempotency_key)` and automatic retry identity scoped to its run-owned session;
3. an exact committed retry atomically calls only `reserve_replay_writer()` and admits a snapshot before stale-head rejection;
4. same key/different fingerprint returns `409`, an exact streaming retry returns `409 operation_in_progress`, and either path reserves nothing;
5. an exact non-streaming in-flight retry atomically calls only `reserve_retry_waiter()` and shares the actor-owned result future;
6. only a new key/request proceeds to expected-head and full transcript validation; and
7. only after all prior checks pass does the actor atomically call `reserve_operation()`, create `ReplayReservation`, install a provisional operation/head, and start backend work.

Exhaust idempotency/replay-storage/generation capacity and prove committed replay still succeeds when replay-writer capacity remains. Prove conflict and stale-head/transcript errors do not call any reservation API, and waiter/replay lease failures do not mutate the operation or artifact.

- [ ] **Step 2: Write capacity and commit tests**

Inject failure at every operation-bundle component after head/transcript validation and assert no provisional head, operation record, state, or cache change. Assert success commits transcript—including exact immutable thinking/redacted-thinking blocks—active head, exact artifact, and `IDLE` only after model identity, successful result, usage, iterator completion, and under-lock deadline checks.

- [ ] **Step 3: Write every post-reservation failure row**

Parameterize pre-mutation proved rollback; pre-mutation but unusable client; context exhaustion; operation timeout; expiry/deletion/shutdown; SDK unsuccessful result; protocol/usage/model failure; response overflow; proxy failure before/after mutation; transport loss. Assert current response, state, head, replay ownership, tombstone cause, cleanup transfer, and later lookup.

Add a dedicated actor-owned liveness-watcher matrix. Start the watcher immediately after backend initialization and before publishing `IDLE`; complete `PreparedBackend.watch_liveness()` from each of `IDLE`, `GENERATING`, and a synthetic `WAITING_FOR_TOOLS` fixture. Race it against commit, expiry, deletion, shutdown, operation failure, and explicit close. Assert the first actor-lock transition wins; an automatic actor compare-and-swaps its shared `TerminalCell` before publishing `LOST`; all actor-owned transcript/system/prompt/response/idempotency/backend references are scrubbed in that same transition; the backend allocation moves to `CleanupRegistry` exactly once; no pending writer gains snapshot authority; and duplicate watcher/close/native notifications neither change the stable terminal cause nor release any lease twice.

Add owned-deadline tests that require no incoming request to make progress. For an idle session, advance beyond session TTL and observe closure. For a backend whose SDK iterator hangs forever after mutation, advance beyond the active-operation deadline and assert the actor callback cancels the backend operation, selects `session_closed(operation_timeout)`, scrubs content, transfers cleanup, and releases every operation/writer lease exactly once. Delay a timer wake until run, session, synthetic pending-tool, and operation deadlines are all expired; under the actor lock, recompute and choose `run > session > pending-tool > operation`, independent of callback delivery order. Race delayed callbacks with success commit, run revocation, deletion, liveness loss, and shutdown; stale generations no-op and the first authoritative terminal transition owns exact-once cleanup.

- [ ] **Step 4: Run tests and confirm actor absence**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_actor_state.py tests/unit/test_idempotency.py tests/unit/test_actor_failures.py tests/unit/test_actor_deadlines.py -v`

Expected: FAIL because `SessionActor` is missing.

- [ ] **Step 5: Implement actor admission and owned operation tasks**

```python
@dataclass(frozen=True, slots=True)
class ActorRequest:
    principal: Principal
    canonical: CanonicalRequest
    expected_head: str | None
    idempotency_key: str | None
    fingerprint: bytes
    deadline: Deadline


@dataclass(frozen=True, slots=True)
class CommittedReply:
    message_id: str
    content: tuple[CanonicalAssistantOutputBlock, ...]
    result: CanonicalResult
    result_head: str


class SessionActor:
    async def admit(self, request: ActorRequest) -> AdmissionResult: ...
    async def set_pending_tool_deadline(
        self, deadline: Deadline | None, generation: int
    ) -> None: ...
    async def close(self, reason: TerminalReason) -> CleanupTicket: ...


AdmissionResult = NewOperation | ReplaySnapshot | RetryWaiter | OperationInProgress
```

`ActorRequest` contains validated request identity only; callers cannot pre-reserve or inject replay/operation capacity. `SessionActor.admit()` owns the seven-step ordering above and constructs all leases/reservations under its lock after the applicable checks. Committed replay and conflict decisions therefore cannot be blocked or distorted by speculative new-operation reservation.

Operations, the session deadline handle, the active-operation deadline handle, the reserved pending-tool deadline extension point, and the one backend-liveness watcher run in the actor-owned task group, never the HTTP request task. The session handle is armed before actor publication; an operation handle is armed atomically with operation publication and canceled on commit/rollback/terminalization. `set_pending_tool_deadline()` is an actor-lock-protected internal extension API: Phase 1 rejects non-`None` use outside synthetic tests because tools are disabled, while Phase 3 may enable it only alongside the approved tool state transition; replacement/cancellation is generation-checked and exact-once. The watcher is started and adopted before the initialized actor becomes externally visible. Watcher and timer callbacks report through the same serialized actor transition function used by requests, expiry, and deletion; callbacks acquire the lock, reject stale generation/deadline/state, recompute monotonic deadlines, and apply `run > session > pending-tool > operation` when several are expired. If one wins from `IDLE`, `GENERATING`, or `WAITING_FOR_TOOLS`, automatic-session terminal-cell CAS precedes terminal publication, content-bearing references are cleared under the actor lock, and exactly one detached cleanup ticket is handed to `CleanupRegistry`. A hung mutated SDK operation is explicitly canceled and joined under the bounded cleanup path. The lock is not held during SDK I/O, watcher/timer waiting, cleanup, or writer delivery. Request-time checks remain race guards only. Tools remain disabled, so no Phase 1 request enters `WAITING_FOR_TOOLS`; the synthetic state test preserves the future tool-phase invariant.

- [ ] **Step 6: Run tests and commit**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_actor_state.py tests/unit/test_idempotency.py tests/unit/test_actor_failures.py tests/unit/test_actor_deadlines.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/actor.py tests/fakes.py tests/unit/test_actor_state.py tests/unit/test_idempotency.py tests/unit/test_actor_failures.py tests/unit/test_actor_deadlines.py
git commit -m "feat: enforce linear actor semantics"
```

---

### Task 12: Validate and Render the Anthropic Messages Subset

**Files:**
- Create: `src/claude_sdk_proxy/anthropic_adapter.py`
- Create: `tests/unit/test_anthropic_adapter.py`
- Create: `tests/unit/test_anthropic_errors.py`

**Interfaces:**
- Consumes: canonical types, proxy errors, exact SDK usage/model events, and the admission-resolved ordinary Anthropic `UsageEvidenceRow`/`DialectUsageMapping`.
- Produces: `parse_messages_request()`, `render_messages_artifact()`, `AnthropicStreamEncoder`, and `render_anthropic_error()`.

- [ ] **Step 1: Write strict request-schema tests**

```python
BASE = {"model": "sonnet", "max_tokens": 256, "messages": [{"role": "user", "content": "hello"}]}


def test_messages_profile_reports_only_max_tokens() -> None:
    body = json.dumps(BASE, separators=(",", ":")).encode("utf-8")
    headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
    parsed = parse_messages_request(
        body,
        headers,
        policy=ParameterPolicy.MESSAGES_COMPAT,
        model_map=MODEL_MAP,
        thinking_allowlist=THINKING_ALLOWLIST,
    )
    assert parsed.ignored_parameters == ("max_tokens",)
```

Reject missing/wrong `anthropic-version`, unknown fields, strict `max_tokens`, temperature/top-p/top-k/stop, arrays or structured system input, tools, multimodal blocks, invalid history, unvalidated thinking, and wrong model before allocation. The parser receives `ProxyConfig.thinking_allowlist` and calls its `require()` method; it has no direct manifest access. Accept absent thinking/effort or an exact allowlisted `enabled` tuple only. Reject `disabled`, `adaptive`, effort-without-enabled, and false/missing enabled tuples before fingerprint, capacity, allocation, or backend work. For an accepted enabled tuple, parse assistant `ThinkingBlock`/`RedactedThinkingBlock` history without normalization and preserve exact payloads, optional signatures, block types, and order.

- [ ] **Step 2: Write exact response and error golden tests**

Assert proxy-generated `msg_lp_*`, configured public alias, ordered assistant content, allowlisted `end_turn`, null stop sequence, ignored header, and exact Anthropic envelopes for every documented error code. Use an ordinary feasibility usage row containing every supported required/optional/nested scalar kind and a passing exhaustive Anthropic mapping. Golden-test that non-streaming JSON emits each supplied SDK leaf once at its exact mapped public path/value, nested details survive leaf-for-leaf, absent optionals are omitted, supplied zero/null remain exact, and nothing is invented. Inject duplicate/exact-prefix collisions, illegal public paths, a mapping from the wrong row/dialect, and one unrepresentable field; each must fail closed rather than emit a partial usage object. Golden-test interleaved thinking, signature, redacted-thinking, and text blocks plus exact `content_block_start`/`thinking_delta`/`signature_delta`/`content_block_stop` stream order. The buffered terminal `message_delta` must carry the same complete usage mapping, and committed non-streaming and SSE artifacts must replay byte-for-byte with every usage field intact. Missing required usage, an unknown SDK field/path/type, unknown stop reason, thinking outside the shared implementation allowlist, reordered signature events, or missing/changed payload/signature is `502 sdk_protocol_error`; a possibly mutated reusable session becomes `LOST`.

- [ ] **Step 3: Run tests and verify failure**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_anthropic_adapter.py tests/unit/test_anthropic_errors.py -v`

Expected: FAIL because the adapter is missing.

- [ ] **Step 4: Implement exhaustive wire conversion**

```python
def parse_messages_request(
    body: bytes, headers: Mapping[str, str], policy: ParameterPolicy,
    model_map: ModelMap, thinking_allowlist: ImplementedThinkingAllowlist,
    usage_mapping_resolver: UsageMappingResolver,
) -> ParsedAnthropicRequest: ...
def render_messages_artifact(
    reply: CommittedReply, ignored: tuple[str, ...],
    usage_row: UsageEvidenceRow, usage_mapping: DialectUsageMapping,
) -> ImmutableHttpArtifact: ...


class AnthropicStreamEncoder:
    def __init__(
        self, usage_row: UsageEvidenceRow, usage_mapping: DialectUsageMapping,
    ) -> None: ...
    def encode_nonterminal(self, event: CanonicalEvent) -> tuple[bytes, ...]: ...
    def encode_terminal_bundle(self, result: CanonicalResult) -> tuple[bytes, ...]: ...
    def encode_error(self, error: ProxyError) -> bytes: ...
```

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_anthropic_adapter.py tests/unit/test_anthropic_errors.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/anthropic_adapter.py tests/unit/test_anthropic_adapter.py tests/unit/test_anthropic_errors.py
git commit -m "feat: add Anthropic wire translation"
```

---

### Task 13: Build the Bounded AnyIO+h11 HTTP Transport

**Files:**
- Create: `src/claude_sdk_proxy/http_types.py`
- Create: `src/claude_sdk_proxy/http_transport.py`
- Create: `tests/unit/test_http_parser.py`
- Create: `tests/integration/test_http_deadlines.py`
- Create: `tests/integration/test_http_capacity.py`

**Interfaces:**
- Consumes: connection/control-writer capacity, HTTP limits/deadlines, `Clock`.
- Produces: `BoundedHttpServer.serve()`, `HttpApplication.handle()`, `HttpRequest`, `HttpResponse`, and bounded streaming body writers.

- [ ] **Step 1: Write parser limit tests**

Parameterize request-line, URI, header count, per-header bytes, aggregate headers, declared body, incremental body, conflicting content lengths, transfer encoding, and maximum requests per connection. Assert exact `400`, `413`, or `431` and exact-once slot release.

- [ ] **Step 2: Write real-socket slow-peer tests**

Using `anyio.connect_tcp`, send one byte at a time across accept-to-header, header-to-body, total pre-operation, and keep-alive-idle deadlines. Assert `408` where writable, otherwise close; no peer retains a connection/request/control-writer slot beyond its deadline.

- [ ] **Step 3: Run tests and confirm transport absence**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_http_parser.py tests/integration/test_http_deadlines.py tests/integration/test_http_capacity.py -v`

Expected: FAIL because `BoundedHttpServer` is missing.

- [ ] **Step 4: Implement transport admission before application bytes**

```python
class HttpApplication(Protocol):
    async def handle(self, request: HttpRequest) -> HttpResponse: ...


class BoundedHttpServer:
    async def serve(self, listener: anyio.abc.SocketListener, app: HttpApplication) -> None: ...
```

Reserve one connection plus fixed control/error writer before reading. Apply explicit byte/count checks around h11; reject unsupported transfer encodings and ambiguous framing; cap keep-alive request count. All response types use `WriterLease`; connection/request/control state has one finish CAS.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_http_parser.py tests/integration/test_http_deadlines.py tests/integration/test_http_capacity.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/http_types.py src/claude_sdk_proxy/http_transport.py tests/unit/test_http_parser.py tests/integration/test_http_deadlines.py tests/integration/test_http_capacity.py
git commit -m "feat: add bounded local HTTP transport"
```

---

### Task 14: Compose Authentication, Control Endpoints, Selection, and Commit-Safe SSE

**Files:**
- Create: `src/claude_sdk_proxy/capabilities.py`
- Create: `src/claude_sdk_proxy/control.py`
- Create: `src/claude_sdk_proxy/router.py`
- Modify: `src/claude_sdk_proxy/server.py`
- Create: `src/claude_sdk_proxy/server_cli.py`
- Create: `tests/integration/test_control_api.py`
- Create: `tests/integration/test_capabilities.py`
- Create: `tests/integration/test_messages.py`
- Create: `tests/integration/test_anthropic_stream.py`
- Create: `tests/integration/test_security.py`

**Interfaces:**
- Consumes: HTTP transport, adapters, registries, actors, replay/writer leases, server lifecycle.
- Produces: `CapabilityProjector.for_principal()`, all Phase 1 endpoints, `ProxyRouter.handle()`, and `claude-proxy serve`.

- [ ] **Step 1: Write endpoint/credential/security matrix tests**

Cover anonymous, master, derived, retired, invalid, and explicitly selected `--no-auth`; both `x-api-key` and `Authorization: Bearer`; expected Host; loopback; JSON content type; browser Origin allowlist; no permissive CORS; run-token precedence; derived token with explicit-session headers; session ownership; and every control endpoint. Parameterize startup/control behavior over `ConfiguredMasterKey`, `GeneratedMasterKey`, and `NoAuth`: configured mode creates no key file, generated mode creates exactly the validated key file, and no-auth mode accepts control requests only without credential headers. Authenticated modes reject missing credentials; no-auth mode rejects master-looking credential headers instead of silently accepting an authenticated client under weaker semantics. In `tests/integration/test_capabilities.py`, assert `/v1/models` lists only configured aliases with exact attested backend IDs and `/_proxy/capabilities` reports the manifest-derived backend/auth/semantic/platform tuple, supported dialect/mode/policy/model IDs, string-only system/text-only content, `tools=false`, and `multimodal=false`, without secret or ambient Claude configuration detail. Feed passing Phase 0 rows for null, disabled, adaptive, enabled-allowed, enabled-false, enabled-unconfigured, and ordinary Anthropic-mapping-false models; assert capabilities advertise only the tuples returned by the exact shared `ProxyConfig.thinking_allowlist.project()` and `usage_mapping_resolver` objects, while the parser accepts exactly that same set plus ordinary absent thinking when its mapping passes. Assert a runtime/model/thinking/dialect tuple with a stable SDK shape but false/missing Anthropic mapping is omitted and rejected before allocation. Assert master/run principal filtering, terminal-but-active run access, retired-token `401`, and startup refusal if the projector cannot represent the exact validated tuple or typed usage schema.

- [ ] **Step 2: Write SSE commit and replay tests**

Assert exact Anthropic frame order, including interleaved thinking/redacted-thinking/text block starts, payload deltas, signature deltas, stops, the complete feasibility-derived usage object, and exact committed replay bytes. Parameterize every required/optional nested usage field and assert the terminal SSE bundle preserves every supplied value, absent optionals remain absent, and replay is byte-identical. Disconnect after headers and every event/byte boundary. Require lazy client construction and proved-safe rollback conditions before headers; preterminal deltas spool from byte zero only after exact model identity and tuple gate; success stop/usage/`message_stop` stay withheld until commit; failure emits a terminal error; retries replay exact committed bytes even with new-operation capacity exhausted; live streaming retries get `409 operation_in_progress` without reservation; conflict/stale requests reserve nothing; disconnect alone does not cancel the actor-owned operation; and spool overflow produces `LOST`.

- [ ] **Step 3: Run tests and verify missing composition**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/integration/test_capabilities.py tests/integration/test_control_api.py tests/integration/test_messages.py tests/integration/test_anthropic_stream.py tests/integration/test_security.py -v`

Expected: FAIL because router/control/CLI composition is missing.

- [ ] **Step 4: Implement deterministic request selection and control handlers**

```python
@dataclass(frozen=True, slots=True)
class CapabilityProjector:
    manifest: FeasibilityManifest
    model_map: ModelMap
    thinking_allowlist: ImplementedThinkingAllowlist
    usage_mapping_resolver: UsageMappingResolver

    def for_principal(self, principal: Principal) -> Mapping[str, JsonValue]: ...


class ProxyRouter(HttpApplication):
    async def handle(self, request: HttpRequest) -> HttpResponse: ...


async def create_explicit_session(request: HttpRequest, principal: MasterPrincipal) -> HttpResponse: ...
async def create_run(request: HttpRequest, principal: MasterPrincipal) -> HttpResponse: ...
async def delete_session(request: HttpRequest, principal: Principal) -> HttpResponse: ...
async def delete_run(request: HttpRequest, lookup: RunLookup) -> HttpResponse: ...
```

Selection is recognized derived run, otherwise explicit headers, otherwise one-shot. Under the actor lock, replay admission is the linearization point; terminal transitions block later snapshots but do not mutate admitted bytes. Streaming headers expose only a provisional head; commit activates it before atomically enqueueing the terminal-success bundle.

Implement `CapabilityProjector` in `capabilities.py`; `control.py` only authenticates and serializes its immutable projection. The server constructs it with the exact `thinking_allowlist` and `usage_mapping_resolver` already frozen inside `ProxyConfig`; identity (`is`) is asserted in composition tests, and neither parser nor projector may rebuild either view. It advertises no `disabled`/`adaptive` thinking mode, no enabled tuple outside `thinking_allowlist.project(model)`, and no model/dialect tuple without a passing ordinary mapping. Phase 1 creates this module and `tests/integration/test_capabilities.py` as the stable extension seam: later tool and multimodal phases modify these existing files rather than referring to files that do not yet exist. Capability values come only from validated immutable `ProxyConfig`; request data and ambient environment never affect them.

- [ ] **Step 5: Add and test the server command**

Add `claude-proxy = "claude_sdk_proxy.server_cli:main"`. `serve` validates Phase 0 and platform gates, resolves exactly one auth mode before reconciliation/listen, starts the run-registry and actor-owned `DeadlineScheduler` tasks before publishing the listener, and enters cleanup-only mode rather than abandoning parenthood. It prints the bind address in every mode, prints a key-file path only after a generated `MasterKeyHandle` reaches `PUBLISHED`, and never prints configured key material or invents a file path for `ConfiguredMasterKey`/`NoAuth`. Shutdown stops admission, terminalizes owners, cancels/joins every deadline handle and scheduler callback, completes the existing writer/allocation drain, then calls the generated handle's `close_after_cleanup(True)`. It reports completion and removes/releases the instance only after the durable master-key deletion receipt; any zeroization, descriptor-close, `unlinkat`, or parent-sync failure retains ownership and keeps the server in cleanup-only retry/reconciliation state.

Expose the three exact server commands/configurations consumed by the Phase 2 launcher: `LOCAL_PROXY_API_KEY=<secret> claude-proxy serve ...` selects `ConfiguredMasterKey`; `claude-proxy serve ...` selects `GeneratedMasterKey` and reports its validated key-file path; `claude-proxy serve --no-auth ...` explicitly selects `NoAuth`. Reject `--no-auth` with a configured key or any generated-key override before runtime-root mutation. A configured or no-auth server creates no master-key file.

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/integration/test_capabilities.py tests/integration/test_control_api.py tests/integration/test_messages.py tests/integration/test_anthropic_stream.py tests/integration/test_security.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/claude_sdk_proxy/capabilities.py src/claude_sdk_proxy/control.py src/claude_sdk_proxy/router.py src/claude_sdk_proxy/server.py src/claude_sdk_proxy/server_cli.py tests/integration/test_capabilities.py tests/integration/test_control_api.py tests/integration/test_messages.py tests/integration/test_anthropic_stream.py tests/integration/test_security.py
git commit -m "feat: expose the Anthropic proxy"
```

---

### Task 15: Add Redacted Diagnostics, Official-Client Evidence, Live Gates, and Documentation

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `src/claude_sdk_proxy/diagnostics.py`
- Create: `tests/unit/test_diagnostics.py`
- Create: `tests/integration/test_official_anthropic_client.py`
- Create: `tests/integration/test_resource_races.py`
- Create: `tests/live/test_phase1_subscription.py`
- Create: `README.md`
- Create: `docs/protocol.md`

**Interfaces:**
- Consumes: the complete Phase 1 service.
- Produces: content-off local diagnostics, compatibility evidence, live subscription gate, and operator contract.

- [ ] **Step 1: Write recursive redaction and content-mode tests**

Test nested authorization, cookies, OAuth, API keys, credential-looking environment values, exceptions, stderr, prompts, messages, and tool fields. Add every generated-master-key creation/cleanup state and failure code: diagnostics may retain state, retry count, zeroization/descriptor/unlink/parent-sync booleans, and stable error code, but must reject or redact the key path/basename, startup message, descriptor number, secret bytes/hash/fingerprint, scratch buffers, and raw exception text. `PROXY_DEBUG=1` records shapes/fingerprints only; `PROXY_DEBUG_CONTENT=1` permits canonical content but still redacts all credentials.

- [ ] **Step 2: Write official-client and full-history tests**

Add the exact official client pin `anthropic==0.120.2` to the `dev` extra in `pyproject.toml`; this is the current verified PyPI release when the plan was written.

Run: `uv lock`

Expected: `uv.lock` resolves `anthropic==0.120.2` exactly before the official-client fixture is written or run.

Use this pinned official Anthropic client against a real local socket. Cover one-shot, automatic full-history continuation, explicit heads/idempotency, non-streaming, streaming, raw ignored headers, parsed errors, retries, cancellation, and a second run token for a second conversation. Supply a fake-backend ordinary result containing every identity-bound SDK field in the validated Anthropic usage mapping, including all nested details and present optional/null/zero cases. Assert the official client exposes every exact non-streaming public value, parses the complete terminal streaming usage, and observes byte-exact replay of both artifacts; absent optional fields remain absent rather than zero. Run a second fixture with an unknown SDK leaf and a third with a false mapping and assert protocol failure/no partial usage. The fixture must import the public client package and assert `anthropic.__version__ == "0.120.2"` before exercising compatibility.

At the top of `tests/live/test_phase1_subscription.py`, define the mandatory preflight fixture and first test. Every other live test in the module must request `required_existing_login`; every async test is marked with both registered markers. The fixture performs a content-free child initialization through the production environment, supervisor, and `AttestationGate`, requires the exact manifest tuple and `auth_source=existing_claude_login`, closes the child through normal cleanup, and converts every absent/expired/ambiguous-login outcome into `pytest.fail()` with a stable content-free reason. `live_subscription_harness.expected_attestation` is the frozen value obtained by applying Phase 0 `extract_child_attestation()` to the manifest's exact executable/environment/provider/endpoint/auth tuple, not a hand-built relaxed comparison. The fixture contains no call to `pytest.skip`, `pytest.importorskip`, or a skip marker.

```python
@pytest.fixture
async def required_existing_login(live_subscription_harness) -> ChildAttestation:
    if os.environ.get("RUN_LIVE_CLAUDE_TESTS") != "1":
        pytest.fail("mandatory Phase 1 live opt-in is absent", pytrace=False)
    try:
        attestation = await live_subscription_harness.initialize_without_user_turn()
    except AttestationError:
        await live_subscription_harness.close_preflight_child_and_confirm_cleanup()
        pytest.fail("mandatory existing Claude login is unavailable", pytrace=False)
    await live_subscription_harness.close_preflight_child_and_confirm_cleanup()
    assert attestation == live_subscription_harness.expected_attestation
    assert attestation.auth_source == "existing_claude_login"
    return attestation


@pytest.mark.live
@pytest.mark.anyio
async def test_required_existing_login_preflight(required_existing_login) -> None:
    assert required_existing_login.auth_source == "existing_claude_login"
```

- [ ] **Step 3: Write the comprehensive resource-race test**

Race expiry, deletion, shutdown, run revocation, original writers, promoted waiters, replay snapshots, operation completion, and every response byte. Assert stable terminal cause, no late commit, bounded lease completion, exact-once counter release, and cleanup-slot retention through durable journal deletion.

- [ ] **Step 4: Implement diagnostics and documentation**

```python
class DiagnosticSink:
    def emit(self, event: DiagnosticEvent) -> None: ...


class Redactor:
    def redact(self, value: JsonValue, *, content_enabled: bool) -> JsonValue: ...
```

Document personal-local-only scope, policy dependency, trusted-local boundary, exact supported tuple, separate `claude` login prerequisite, serve command, configured or generated master key, strict versus Messages-compatible policy, all modes/headers/errors, session loss, no compaction, cleanup-only behavior, and honest prompt-isolated semantics. Add path-policy tests that snapshot the relevant Claude root, scan content-safe paths only, reject unknown new paths, and prove no prompt/transcript/response/debug-title/memory/tool canary or leaked temporary artifact remains.

Extend the inherited Phase 0 `Makefile` without redefining `PYTEST_RELEASE_FLAGS`. `phase1-check` runs the complete deterministic Phase 1 unit/integration suite with `$(PYTEST_RELEASE_FLAGS)` before Ruff and mypy. `phase1-live-preflight` requires `RUN_LIVE_CLAUDE_TESTS=1` and runs only `test_required_existing_login_preflight` with those flags. `phase1-live` depends on that preflight and runs the complete live module with the same flags. `phase1-release` depends on the inherited Phase 0 `check`, `phase1-check`, and `phase1-live`; no deterministic-only or developer target may claim Phase 1 release authority. If a future optional developer target is added, require a `dev-` prefix and prohibit any dependency edge from these targets, manifest generation, or downstream release targets.

- [ ] **Step 5: Run the deterministic Phase 1 gate**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit tests/integration -v`

Expected: PASS with no leaked deadline handle/task, workdir, allocation journal, writer reference, lifecycle slot, or subprocess after ordinary completion.

Run: `uv run ruff check . && uv run mypy src/claude_sdk_proxy`

Expected: no findings.

- [ ] **Step 6: Run the opt-in live gate and commit**

Run: `RUN_LIVE_CLAUDE_TESTS=1 uv run pytest --strict-markers --forbid-skips -W error tests/live/test_phase1_subscription.py::test_required_existing_login_preflight -v`

Expected: PASS only when a content-free production child positively attests the exact existing Claude login and closes cleanly. Missing opt-in or login, expired/ambiguous auth, attestation mismatch, or cleanup failure exits nonzero; none is a skip.

Run: `RUN_LIVE_CLAUDE_TESTS=1 uv run pytest --strict-markers --forbid-skips -W error tests/live/test_phase1_subscription.py -v`

Expected: PASS for the exact validated subscription/runtime tuple with zero skipped reports. The module contains no skip path; missing opt-in or existing login is a failed mandatory prerequisite.

```bash
git add Makefile pyproject.toml uv.lock src/claude_sdk_proxy/diagnostics.py tests/unit/test_diagnostics.py tests/integration/test_official_anthropic_client.py tests/integration/test_resource_races.py tests/live/test_phase1_subscription.py README.md docs/protocol.md
git commit -m "docs: validate the Anthropic proxy"
```
