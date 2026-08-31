# Phase 3 Gated Caller-Owned Tool Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add caller-defined external tools for automatic and explicit live sessions only, while preserving exact public call/result semantics, the approved actor/replay/deadline contracts, and bounded native cleanup.

**Architecture:** Immutable public tool definitions become one session-local in-process MCP server after the Phase 0 SDK evidence gate and per-child authentication/model attestation pass. One actor-owned receive loop correlates finalized public calls with suspended callbacks, commits a replayable `WAITING_FOR_TOOLS` segment, and accepts one fully validated continuation that reserves all capacity before resolving callbacks. Production tool support composes the exact Phase 0 SDK/runtime/model evidence record with separate Phase 3 framing rows keyed by dialect, exact thinking identity, canonical ordinary/tool-boundary/post-result usage binding, and stream mode; it is advertised only when every typed record for that same tuple passes.

**Tech Stack:** Python 3.14, uv, AnyIO 4, h11 0.16, Pydantic 2, the Phase 0-pinned Claude Agent SDK/CLI pair, Phase 0 C17 Darwin lifecycle executable, pytest/HTTPX, Ruff, and mypy.

**Spec:** `docs/superpowers/specs/2026-08-29-claude-subscription-api-proxy-design.md`

## Global Constraints

- Phase 0, Phase 1, and Phase 2 release gates must pass before this plan starts.
- Inherit the Phase 0 pytest release policy: every mandatory Phase 3 pytest command uses `--strict-markers --forbid-skips -W error`; a skipped or unexecuted async test is failed evidence, never a pass.
- V1 remains Darwin/macOS 14 or newer on a local APFS runtime root; tool support cannot bypass the startup platform gate.
- `backend_kind` must be `agent_sdk_subscription`, `auth_source` must be `existing_claude_login`, and `semantic_class` must be `prompt_isolated_agent_sdk`.
- A resolved model is tool-enabled only when its exact Phase 0 SDK/runtime/model record passes, its ordinary/tool-boundary/post-result usage rows all have exhaustive passing mappings for the selected dialect, and every required Phase 3 framing/lifecycle gate is true for both streaming and non-streaming rows keyed to that exact dialect/thinking/usage binding. Missing, stale, mismatched, unrepresentable, unprobed, or skipped evidence is false.
- One-shot requests with non-empty tools are rejected before lifecycle reservation, workdir creation, SDK construction, or native mutation.
- Keep `tools=[]`; expose only one proxy-owned in-process MCP server and its exact generated names. Ambient and built-in Claude Code tools remain disabled.
- The harness owns approval, execution, retries, and results. The proxy never executes a caller tool and never serializes a tool result as a user prompt.
- Tool schemas, model, system string, thinking tuple, dialect, and parameter policy are immutable for a session.
- Initial tool results are bounded text plus an explicit error flag where the public dialect supports it. Richer results remain disabled until Phase 4.
- A public tool-use segment commits only after the complete call set, exact public-ID correlation, every suspended callback, terminal-success bundle, replay bytes, head, cache, and `WAITING_FOR_TOOLS` state commit atomically.
- A tool-result request contains all and only pending IDs, no unrelated user text, and a new explicit-mode idempotency key. It reserves the complete operation/capacity bundle before creating a head or resolving any callback.
- Runtime decisions use under-lock monotonic deadlines. Precedence is run expiry, session expiry, pending-tool timeout, then operation timeout.
- Any terminal transition publishes the automatic-run terminal cell first, transfers SDK/process/workdir ownership to the common teardown registry exactly once, and preserves only response snapshots admitted before terminalization.
- Default diagnostics contain no schema, argument, result, callback, prompt, credential, or raw SDK content.
- Persistence evidence must prove **no prohibited persistent content and no leaked temporary artifacts**. It must never claim zero filesystem writes.

## Required Phase 1 Interfaces

Phase 3 consumes these exact contracts from the approved Phase 1 implementation:

```python
class CapacityLedger:
    def reserve_operation(self, demand: OperationDemand) -> OperationLease: ...

class OperationLease:
    def release(self) -> None: ...

class CleanupRegistry:
    async def adopt(self, ticket: CleanupTicket) -> None: ...
    async def wait_empty(self) -> None: ...

class TerminalCell:
    def close(self, reason: TerminalReason) -> bool: ...

class PreparedBackend:
    async def initialize(self) -> AttestedChild: ...
    async def revalidate_before_turn(self) -> None: ...
    async def watch_liveness(self) -> BackendLoss: ...
    async def start_turn(self, blocks: tuple[CanonicalInputBlock, ...]) -> BackendOperation: ...
    async def close(self) -> CleanupTicket: ...

class BackendOperation:
    @property
    def mutation_possible(self) -> bool: ...
    def events(self) -> AsyncIterator[CanonicalEvent]: ...
    async def cancel(self) -> None: ...

class PathPolicy:
    def classify(self, path: Path) -> PathClass: ...
    def may_open_content(self, path: Path) -> bool: ...
```

## File Map

- `src/claude_sdk_proxy/domain.py`: immutable tool definitions, calls, results, pending sets, and canonical fingerprints.
- `src/claude_sdk_proxy/tool_evidence.py`: strict Phase 0 SDK evidence loader, Phase 3 framing-evidence loader, and composed release decision.
- `src/claude_sdk_proxy/capabilities.py`: gate-derived tool capability projection.
- `src/claude_sdk_proxy/tool_bridge.py`: low-level Phase-0-attested MCP construction plus production release-gated factory, public-ID callback correlation, atomic resolution, and scrubbing.
- `src/claude_sdk_proxy/tool_probe.py`: sealed live-opt-in candidate runner used only to bootstrap Phase 3 framing evidence.
- `src/claude_sdk_proxy/backend.py`: attested tool-aware SDK construction and event normalization.
- `src/claude_sdk_proxy/actor.py`: tool boundary commit, `WAITING_FOR_TOOLS`, continuation reservation, deadlines, replay, and terminal transfer.
- `src/claude_sdk_proxy/control.py`: gate-aware explicit-session tool creation.
- `src/claude_sdk_proxy/router.py`: automatic first-bind construction, one-shot rejection, and route integration.
- `src/claude_sdk_proxy/config.py`: tool limits, evidence path, feature toggle, and pending-tool timeout.
- `src/claude_sdk_proxy/anthropic_adapter.py`: Anthropic schemas, tool-use/result translation, and terminal SSE bundle.
- `src/claude_sdk_proxy/openai_adapter.py`: OpenAI function tools, calls, tool messages, and terminal chunks.
- `src/claude_sdk_proxy/diagnostics.py`: keyed tool-shape diagnostics with content off by default.
- `src/claude_sdk_proxy/path_policy.py`: Phase 0 classification plus state-event capture and safe canary comparison.
- `tests/unit/test_tool_domain.py`: canonicalization, bounds, and result-set invariants.
- `tests/unit/test_tool_evidence.py`: exact tuple and hard-gate validation.
- `tests/unit/test_tool_bridge.py`: callback correlation, attestation, resolution, cancellation, and scrubbing.
- `tests/unit/test_tool_actor.py`: actor, capacity, deadline, replay, terminal, and cleanup transitions.
- `tests/integration/test_tool_session_creation.py`: explicit/automatic construction, rollback, and one-shot rejection.
- `tests/integration/test_anthropic_tools.py`: Anthropic HTTP/SSE tool loops.
- `tests/integration/test_openai_tools.py`: OpenAI HTTP/SSE tool loops.
- `tests/integration/test_official_anthropic_client.py`: official Anthropic client tool-boundary/result usage evidence.
- `tests/integration/test_official_openai_client.py`: official OpenAI client tool-boundary/result usage evidence.
- `tests/integration/test_tool_diagnostics_persistence.py`: redaction, path-policy, and journal-content tests.
- `tests/live/test_proxy_tool_loop.py`: exact end-to-end subscription tool matrix.
- `docs/feasibility/validated-tool-release.json`: generated redacted Phase 3 framing rows bound to the exact Phase 0 SDK-record, thinking, and tool-usage-binding tuple.
- `docs/protocol.md`: tool state, replay, deadline, error, and result-subset contract.
- `docs/harnesses.md`: advertised tool presets only after the production gate passes.

---

### Task 1: Extend Canonical Tool Results and Add the Composed Evidence Gate

**Files:**
- Modify: `src/claude_sdk_proxy/domain.py`
- Create: `src/claude_sdk_proxy/tool_evidence.py`
- Modify: `src/claude_sdk_proxy/capabilities.py`
- Create: `tests/unit/test_tool_domain.py`
- Create: `tests/unit/test_tool_evidence.py`
- Modify: `tests/unit/test_transcript.py`
- Modify: `tests/unit/test_fingerprints.py`
- Modify: `tests/unit/test_sessions.py`

**Interfaces:**
- Consumes: Phase 0 `FeasibilityManifest`/`SdkToolEvidenceManifest`, exclusive `sdk_tool_record_digest()`, and exact-budget ordinary/tool-boundary/post-result usage rows, Phase 1 `ProxyConfig`, `UsageMappingResolver`, `UsageRowBindings`, and `CanonicalResult(stop_reason="end_turn")`, its exact public-alias/backend-model mappings, and `Dialect`.
- Produces: `ToolDefinition`, `ToolUseBlock`, `ToolResultBlock`, `PendingToolSet`, `ToolLimits`, tool-aware `CanonicalInputBlock`/`CanonicalAssistantOutputBlock`/`CanonicalTranscriptBlock`/`CanonicalMessage`/`CanonicalRequest`/`ImmutableSessionConfig`, Phase-3-extended `CanonicalResult`, `ToolThinkingIdentity`, `ResolvedToolUsageBinding`, tuple-specific `ToolFramingEvidenceKey`, `canonical_tool_usage_binding_digest()`, `canonical_framing_result_digest()`, strict `ToolFramingEvidenceRecord`, `ToolFramingEvidenceManifest.load()`, snapshot-safe `ToolReleaseEvidence.load(phase0_path, framing_path, usage_mapping_resolver)`, `ToolReleaseBinding`, `ToolReleaseEvidence.require_session_tools(sdk_record_digest, dialect, usage_rows)`, `ToolReleaseEvidence.release_binding(sdk_record_digest, dialect, usage_rows)`, `tools_enabled_for()`, and `tool_capability_projection()`.

- [ ] **Step 1: Write failing domain and evidence tests**

```python
# tests/unit/test_tool_evidence.py
import pytest

from claude_sdk_proxy.domain import Dialect, UsageOperationClass
from claude_sdk_proxy.tool_evidence import (
    REQUIRED_FRAMING_GATES,
    ToolEvidenceError,
    ToolFramingEvidenceKey,
    ToolFramingEvidenceManifest,
    ToolReleaseEvidence,
    canonical_tool_usage_binding_digest,
    canonical_framing_result_digest,
    tools_enabled_for,
)
from claude_sdk_proxy.validated import REQUIRED_SDK_TOOL_GATES, sdk_tool_record_digest


def test_tool_release_composes_sdk_and_framing_evidence(validated_environment) -> None:
    release = validated_environment.tool_release_evidence()
    digest = validated_environment.configured_sdk_record_digest
    usage_rows = validated_environment.tool_usage_rows(Dialect.ANTHROPIC)
    release.require_session_tools(digest, Dialect.ANTHROPIC, usage_rows)
    binding = release.resolve_usage_binding(digest, Dialect.ANTHROPIC, usage_rows)
    for streaming in (False, True):
        key = ToolFramingEvidenceKey(
            digest, Dialect.ANTHROPIC, binding.thinking,
            binding.digest, streaming,
        )
        for gate in REQUIRED_FRAMING_GATES:
            broken = release.with_broken_test_row(key, gate)
            with pytest.raises(ToolEvidenceError, match="session tool evidence"):
                broken.require_session_tools(digest, Dialect.ANTHROPIC, usage_rows)


def test_tool_release_binding_changes_for_any_bridge_prerequisite(validated_environment) -> None:
    release = validated_environment.tool_release_evidence()
    digest = validated_environment.configured_sdk_record_digest
    usage_rows = validated_environment.tool_usage_rows(Dialect.ANTHROPIC)
    original = release.release_binding(digest, Dialect.ANTHROPIC, usage_rows)
    assert original.schema_version == 1
    for changed in (
        release.with_changed_sdk_record(digest),
        release.with_changed_naming_rule(digest),
        release.with_changed_framing_result_digest(
            digest, Dialect.ANTHROPIC, usage_rows, False
        ),
        release.with_changed_framing_result_digest(
            digest, Dialect.ANTHROPIC, usage_rows, True
        ),
        release.with_changed_framing_validation_time(
            digest, Dialect.ANTHROPIC, usage_rows, False
        ),
        release.with_changed_framing_validation_time(
            digest, Dialect.ANTHROPIC, usage_rows, True
        ),
    ):
        assert changed.release_binding(digest, Dialect.ANTHROPIC, usage_rows) != original

    for invalid in (
        release.with_changed_framing_thinking_identity(
            digest, Dialect.ANTHROPIC, usage_rows, False
        ),
        release.with_changed_framing_usage_binding_digest(
            digest, Dialect.ANTHROPIC, usage_rows, True
        ),
    ):
        with pytest.raises(ToolEvidenceError, match="session tool evidence"):
            invalid.release_binding(digest, Dialect.ANTHROPIC, usage_rows)

    for operation_class in (
        UsageOperationClass.ORDINARY,
        UsageOperationClass.TOOL_USE_BOUNDARY,
        UsageOperationClass.POST_TOOL_RESULT,
    ):
        changed_rows = usage_rows.with_replaced_digest(operation_class, "00" * 32)
        with pytest.raises(ToolEvidenceError, match="session tool evidence"):
            release.release_binding(digest, Dialect.ANTHROPIC, changed_rows)

    for streaming in (False, True):
        invalid = release.with_changed_framing_gate(
            digest, Dialect.ANTHROPIC, usage_rows, streaming
        )
        with pytest.raises(ToolEvidenceError, match="session tool evidence"):
            invalid.release_binding(digest, Dialect.ANTHROPIC, usage_rows)


@pytest.mark.parametrize(
    "mutation",
    [
        "manifest_schema", "record_schema", "unknown_record_field", "missing_record_field",
        "malformed_validated_at", "malformed_result_digest", "missing_gate", "extra_gate",
        "non_boolean_gate", "sdk_digest_key", "dialect_key", "thinking_key",
        "usage_binding_digest_key", "streaming_key",
        "duplicate_canonical_key",
    ],
)
def test_framing_loader_rejects_mutation_of_every_hashed_field(
    valid_framing_json, mutation, tmp_path
) -> None:
    path = write_json(tmp_path, mutate_framing_json(valid_framing_json, mutation))
    with pytest.raises(ToolEvidenceError, match="tool framing evidence"):
        ToolFramingEvidenceManifest.load(path)


def test_canonical_framing_result_digest_is_content_free_and_stable(
    redacted_framing_result
) -> None:
    first = canonical_framing_result_digest(redacted_framing_result)
    reordered = dict(reversed(tuple(redacted_framing_result.items())))
    assert canonical_framing_result_digest(reordered) == first
    with pytest.raises(ToolEvidenceError, match="redacted framing result"):
        canonical_framing_result_digest({**redacted_framing_result, "response_text": "secret"})


@pytest.mark.parametrize(
    "case",
    [
        "missing_digest", "mismatched_digest", "wrong_dialect",
        "missing_nonstream", "missing_stream", "missing_tool_boundary_usage_row",
        "false_post_result_usage_mapping", "wrong_usage_row_digest",
        "wrong_thinking_key", "wrong_tool_usage_binding_digest",
        "borrowed_null_framing_for_enabled",
    ],
)
def test_any_structural_evidence_mismatch_disables_session_tools(
    case, release_evidence
) -> None:
    evidence, digest, dialect, usage_rows = release_evidence.for_failure_case(case)
    assert tools_enabled_for(evidence, digest, dialect, usage_rows) is False


def test_null_framing_rows_cannot_authorize_enabled_thinking(validated_environment) -> None:
    release = validated_environment.tool_release_evidence()
    digest = validated_environment.configured_sdk_record_digest
    null_rows = validated_environment.tool_usage_rows(
        Dialect.ANTHROPIC, thinking="null"
    )
    enabled_rows = validated_environment.tool_usage_rows(
        Dialect.ANTHROPIC, thinking="enabled"
    )
    assert release.framing_key(digest, Dialect.ANTHROPIC, null_rows, False) != (
        release.framing_key(digest, Dialect.ANTHROPIC, enabled_rows, False)
    )
    borrowed = release.with_only_framing_rows_for(null_rows)
    assert tools_enabled_for(
        borrowed, digest, Dialect.ANTHROPIC, enabled_rows
    ) is False


def test_tool_usage_binding_preserves_exact_budget_and_rejects_mixed_rows(
    validated_environment
) -> None:
    release = validated_environment.tool_release_evidence()
    digest = validated_environment.configured_sdk_record_digest
    rows_4096 = validated_environment.tool_usage_rows(
        Dialect.ANTHROPIC, thinking="enabled", budget_tokens=4096, effort="high"
    )
    rows_4097 = validated_environment.tool_usage_rows(
        Dialect.ANTHROPIC, thinking="enabled", budget_tokens=4097, effort="high"
    )
    binding_4096 = release.resolve_usage_binding(
        digest, Dialect.ANTHROPIC, rows_4096
    )
    binding_4097 = release.resolve_usage_binding(
        digest, Dialect.ANTHROPIC, rows_4097
    )
    assert binding_4096.thinking.budget_tokens == 4096
    assert binding_4097.thinking.budget_tokens == 4097
    assert binding_4096.digest != binding_4097.digest

    mixed = rows_4096.with_replaced_digest(
        UsageOperationClass.POST_TOOL_RESULT,
        rows_4097.require(UsageOperationClass.POST_TOOL_RESULT),
    )
    with pytest.raises(ToolEvidenceError, match="exact thinking identity"):
        release.resolve_usage_binding(digest, Dialect.ANTHROPIC, mixed)


def test_release_loader_requires_same_validated_usage_snapshot(
    phase0_path, framing_path, validated_usage_mapping_resolver
) -> None:
    loaded = ToolReleaseEvidence.load(
        phase0_path, framing_path, validated_usage_mapping_resolver
    )
    assert loaded.usage_schema_digest == validated_usage_mapping_resolver.schema.schema_digest
    with pytest.raises(ToolEvidenceError, match="usage snapshot"):
        ToolReleaseEvidence.load(
            phase0_path,
            framing_path,
            validated_usage_mapping_resolver.with_changed_schema_digest("00" * 32),
        )


def test_phase0_loader_and_phase3_loader_own_disjoint_gate_schemas(validated_environment) -> None:
    release = validated_environment.tool_release_evidence()
    digest = validated_environment.configured_sdk_record_digest
    sdk_record = release.require_sdk_record(digest)
    usage_rows = validated_environment.tool_usage_rows(Dialect.ANTHROPIC)
    framing_record = release.framing_manifest.require_row(
        release.framing_key(digest, Dialect.ANTHROPIC, usage_rows, False)
    )
    assert set(sdk_record.gates) == REQUIRED_SDK_TOOL_GATES
    assert set(framing_record.gates) == REQUIRED_FRAMING_GATES
    assert set(sdk_record.gates).isdisjoint(framing_record.gates)


def test_phase3_sdk_index_uses_only_the_phase0_record_digest(validated_environment) -> None:
    release = validated_environment.tool_release_evidence()
    assert dict(release.sdk_records_by_digest) == {
        sdk_tool_record_digest(record): record
        for record in release.sdk_manifest.records
    }


def test_one_shot_nonempty_tools_fail_before_reservation(tool_request, app_harness) -> None:
    response = app_harness.post_one_shot(tool_request)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_tools"
    assert app_harness.lifecycle_reservations == 0
    assert app_harness.backend_constructions == 0
```

```python
# tests/unit/test_tool_domain.py
from dataclasses import replace

import pytest

from claude_sdk_proxy.domain import PendingToolSet, ProxyError, ToolResultBlock, ToolUseBlock


def test_results_match_all_pending_ids_once_and_preserve_pending_order() -> None:
    pending = PendingToolSet.from_calls((
        ToolUseBlock(id="toolu_1", name="echo", input={"x": 1}),
        ToolUseBlock(id="toolu_2", name="echo", input={"x": 1}),
    ))
    ordered = pending.validate_results((
        ToolResultBlock(tool_use_id="toolu_2", text="second"),
        ToolResultBlock(tool_use_id="toolu_1", text="first", is_error=True),
    ))
    assert tuple(item.tool_use_id for item in ordered) == ("toolu_1", "toolu_2")


@pytest.mark.parametrize("kind", ["missing", "duplicate", "unknown", "mixed_user_text"])
def test_invalid_result_sets_do_not_resolve_callbacks(kind, invalid_result_case) -> None:
    with pytest.raises(ProxyError, match="pending tool set"):
        invalid_result_case(kind).validate()
    assert invalid_result_case(kind).resolved_count == 0


@pytest.mark.parametrize("stop_reason", ["end_turn", "tool_use", "tool_calls"])
def test_canonical_result_accepts_the_phase3_stop_contract(stop_reason, result_factory) -> None:
    result = result_factory(stop_reason=stop_reason)
    assert result.stop_reason == stop_reason


def test_canonical_history_preserves_assistant_calls_and_user_results(tool_request_factory) -> None:
    request = tool_request_factory(messages=(
        CanonicalMessage("user", (TextBlock("calculate"),)),
        CanonicalMessage("assistant", (ToolUseBlock("call_1", "echo", {"x": 1}),)),
        CanonicalMessage("user", (ToolResultBlock("call_1", "one"),)),
    ))
    assert request.messages[1].content[0].id == "call_1"
    assert request.messages[2].content[0].tool_use_id == "call_1"


@pytest.mark.parametrize(
    "content",
    [
        (TextBlock("mixed"), ToolResultBlock("call_1", "one")),
        (ThinkingBlock("secret", "sig"),),
        (RedactedThinkingBlock("opaque", "sig"),),
        (ToolUseBlock("call_1", "echo", {}),),
    ],
)
def test_user_turn_rejects_mixed_results_and_assistant_only_blocks(content) -> None:
    with pytest.raises(ValueError, match="all text or all tool results"):
        CanonicalMessage("user", content)


def test_thinking_payload_signatures_and_order_survive_tool_history(
    tool_request_factory, process_hmac_key
) -> None:
    assistant_blocks = (
        ThinkingBlock(thinking="reason-a", signature="sig-a"),
        ToolUseBlock(id="call_1", name="echo", input={"x": 1}),
        RedactedThinkingBlock(data="opaque-b", signature="sig-b"),
        TextBlock(text="working"),
    )
    messages = (
        CanonicalMessage("user", (TextBlock("calculate"),)),
        CanonicalMessage("assistant", assistant_blocks),
        CanonicalMessage("user", (ToolResultBlock("call_1", "one"),)),
    )
    request = tool_request_factory(messages=messages)
    assert request.messages[1].content == assistant_blocks
    assert validate_continuation(recorded=messages[:2], submitted=messages) == (messages[2],)
    reordered = tool_request_factory(messages=(messages[0], replace(messages[1], content=tuple(reversed(assistant_blocks))), messages[2]))
    assert fingerprint_request(process_hmac_key, request) != fingerprint_request(
        process_hmac_key, reordered
    )


def test_tool_schema_is_part_of_fingerprint_and_immutable_session_config(
    tool_request_factory, process_hmac_key
) -> None:
    first = tool_request_factory(tools=(ECHO_V1,))
    changed = tool_request_factory(tools=(ECHO_V2,))
    assert fingerprint_request(process_hmac_key, first) != fingerprint_request(
        process_hmac_key, changed
    )
    with pytest.raises(ProxyError, match="immutable session config"):
        validate_continuation_config(first.immutable_config(), changed)


def test_tool_session_config_binds_every_required_usage_operation(tool_request) -> None:
    config = tool_request.immutable_config()
    assert {entry.operation_class.value for entry in config.usage_rows.entries} == {
        "ordinary", "tool_use_boundary", "post_tool_result",
    }
    with pytest.raises(ProxyError, match="immutable session config"):
        validate_continuation_config(
            config,
            replace(
                config,
                usage_rows=config.usage_rows.with_replaced_digest(
                    UsageOperationClass.TOOL_USE_BOUNDARY, "00" * 32
                ),
            ),
        )
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_tool_domain.py tests/unit/test_tool_evidence.py tests/unit/test_transcript.py tests/unit/test_fingerprints.py tests/unit/test_sessions.py -v`

Expected: FAIL because the tool domain/evidence loader and tool-aware canonical history/config extensions do not exist.

- [ ] **Step 3: Implement immutable tool types, bounds, and strict evidence loading**

```python
# Core declarations for src/claude_sdk_proxy/tool_evidence.py
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal

from .domain import (
    Dialect,
    JsonValue,
    UsageMappingResolver,
    UsageOperationClass,
    UsageRowBindings,
)
from .validated import (
    DialectUsageMapping,
    SdkToolEvidenceRecord,
    SdkToolEvidenceManifest,
    UsageEvidenceRow,
    canonical_evidence_json,
    load_manifest,
    load_sdk_tool_evidence,
    load_usage_evidence,
    phase0_prerequisite_digest,
    sdk_tool_record_digest,
)

REQUIRED_FRAMING_GATES = frozenset({
    "tool_wire_round_trip",
    "tool_disconnect_survival_each_boundary",
    "tool_exact_retry_attach",
    "tool_capacity_rejection",
    "tool_absolute_run_ttl",
    "tool_absolute_session_ttl",
    "tool_pending_timeout",
    "tool_operation_timeout",
    "tool_deletion",
    "tool_launcher_exit",
    "tool_callback_corruption",
    "tool_graceful_shutdown",
    "tool_abrupt_proxy_death",
    "tool_cleanup_confirmed",
    "tool_stubborn_descendant",
    "tool_wire_event_ordering",
    "tool_thinking_event_ordering",
    "tool_thinking_signature_fidelity",
    "tool_terminal_bundle_exact",
    "tool_wire_stop_reason",
    "tool_wire_usage_exact",
    "tool_replay_byte_exact",
})


@dataclass(frozen=True, slots=True)
class ToolThinkingIdentity:
    mode: Literal["null", "enabled"]
    budget_tokens: int | None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None


@dataclass(frozen=True, slots=True)
class ResolvedToolUsageBinding:
    schema_version: Literal[1]
    digest: str
    thinking: ToolThinkingIdentity
    usage_rows: UsageRowBindings


def canonical_tool_usage_binding_digest(
    sdk_record_digest: str,
    dialect: Dialect,
    thinking: ToolThinkingIdentity,
    resolved_rows_and_mappings: tuple[
        tuple[UsageEvidenceRow, DialectUsageMapping], ...
    ],
) -> str: ...


@dataclass(frozen=True, slots=True)
class ToolFramingEvidenceKey:
    sdk_record_digest: str
    dialect: Dialect
    thinking: ToolThinkingIdentity
    tool_usage_binding_digest: str
    streaming: bool

    def canonical_key(self) -> str:
        payload = canonical_evidence_json({
            "dialect": self.dialect.value,
            "sdk_record_digest": self.sdk_record_digest,
            "streaming": self.streaming,
            "thinking": {
                "budget_tokens": self.thinking.budget_tokens,
                "effort": self.thinking.effort,
                "mode": self.thinking.mode,
            },
            "tool_usage_binding_digest": self.tool_usage_binding_digest,
        })
        return sha256(b"claude-sdk-proxy:tool-framing-key:v1\0" + payload).hexdigest()


@dataclass(frozen=True, slots=True)
class ToolFramingEvidenceRecord:
    schema_version: Literal[1]
    key: ToolFramingEvidenceKey
    validated_at_utc: str
    result_digest: str
    gates: Mapping[str, bool]


def canonical_framing_result_digest(redacted_result: Mapping[str, JsonValue]) -> str: ...


@dataclass(frozen=True, slots=True)
class ToolFramingEvidenceManifest:
    schema_version: Literal[1]
    records: Mapping[ToolFramingEvidenceKey, ToolFramingEvidenceRecord]

    @classmethod
    def load(cls, path: Path) -> "ToolFramingEvidenceManifest": ...
    def require_row(self, key: ToolFramingEvidenceKey) -> ToolFramingEvidenceRecord: ...


@dataclass(frozen=True, slots=True)
class ToolReleaseBinding:
    schema_version: Literal[1]
    digest: str


@dataclass(frozen=True, slots=True)
class ToolReleaseEvidence:
    sdk_manifest: SdkToolEvidenceManifest
    framing_manifest: "ToolFramingEvidenceManifest"
    sdk_records_by_digest: Mapping[str, SdkToolEvidenceRecord]
    usage_mapping_resolver: UsageMappingResolver
    phase0_prerequisite_digests_by_sdk_digest: Mapping[str, str]
    usage_schema_digest: str

    @classmethod
    def load(
        cls,
        phase0_path: Path,
        framing_path: Path,
        usage_mapping_resolver: UsageMappingResolver,
    ) -> "ToolReleaseEvidence": ...

    def require_sdk_record(self, sdk_record_digest: str) -> SdkToolEvidenceRecord:
        try:
            return self.sdk_records_by_digest[sdk_record_digest]
        except KeyError as exc:
            raise ToolEvidenceError("session tool evidence: SDK digest") from exc

    def resolve_usage_binding(
        self, sdk_record_digest: str, dialect: Dialect,
        usage_rows: UsageRowBindings,
    ) -> ResolvedToolUsageBinding: ...

    def framing_key(
        self, sdk_record_digest: str, dialect: Dialect,
        usage_rows: UsageRowBindings, streaming: bool,
    ) -> ToolFramingEvidenceKey:
        binding = self.resolve_usage_binding(sdk_record_digest, dialect, usage_rows)
        return ToolFramingEvidenceKey(
            sdk_record_digest, dialect, binding.thinking, binding.digest, streaming
        )

    def require_session_tools(
        self, sdk_record_digest: str, dialect: Dialect,
        usage_rows: UsageRowBindings,
    ) -> tuple[ToolFramingEvidenceRecord, ToolFramingEvidenceRecord]:
        sdk_record = self.require_sdk_record(sdk_record_digest)
        if not sdk_record.all_sdk_tool_gates_passed():
            raise ToolEvidenceError("session tool evidence: Phase 0 SDK gates")
        binding = self.resolve_usage_binding(sdk_record_digest, dialect, usage_rows)
        nonstream = self.framing_manifest.require_row(
            ToolFramingEvidenceKey(
                sdk_record_digest, dialect, binding.thinking, binding.digest, False
            )
        )
        stream = self.framing_manifest.require_row(
            ToolFramingEvidenceKey(
                sdk_record_digest, dialect, binding.thinking, binding.digest, True
            )
        )
        return nonstream, stream

    def release_binding(
        self, sdk_record_digest: str, dialect: Dialect,
        usage_rows: UsageRowBindings,
    ) -> ToolReleaseBinding: ...


def tools_enabled_for(
    evidence: ToolReleaseEvidence, sdk_record_digest: str, dialect: Dialect,
    usage_rows: UsageRowBindings,
) -> bool:
    try:
        evidence.require_session_tools(sdk_record_digest, dialect, usage_rows)
    except ToolEvidenceError:
        return False
    return True
```

Define Pydantic tool types with `frozen=True` and `extra="forbid"`; copy each JSON schema into a recursively immutable canonical JSON tree so nested caller mappings/lists cannot mutate a session after validation. Extend Phase 1's aliases without discarding its thinking types: `CanonicalInputBlock` becomes `TextBlock | ToolResultBlock`, `CanonicalAssistantOutputBlock` adds `ToolUseBlock` while retaining `TextBlock | ThinkingBlock | RedactedThinkingBlock`, and `CanonicalTranscriptBlock` remains their union. A validated user new turn is either one or more `TextBlock` values or the special nonempty all-`ToolResultBlock` turn containing exactly the pending IDs; mixed text/results, thinking, redacted thinking, and tool-use blocks are invalid for users. Assistant messages may contain any ordered `CanonicalAssistantOutputBlock` sequence but never tool results. Extend `CanonicalRequest` with `tools: tuple[ToolDefinition, ...]`, and extend `ImmutableSessionConfig` with the same canonical immutable tuple. Assistant thinking/tool-use history and matching user tool-result turns remain part of the canonical transcript used only for selected-session prefix validation; tool results are resolved through MCP callbacks and are never flattened into a new SDK user prompt or passed to `PreparedBackend.start_turn()`.

```python
# Phase 3 extensions in src/claude_sdk_proxy/domain.py
CanonicalInputBlock: TypeAlias = TextBlock | ToolResultBlock
CanonicalAssistantOutputBlock: TypeAlias = (
    TextBlock | ThinkingBlock | RedactedThinkingBlock | ToolUseBlock
)
CanonicalTranscriptBlock: TypeAlias = CanonicalInputBlock | CanonicalAssistantOutputBlock


@dataclass(frozen=True, slots=True)
class CanonicalMessage:
    role: Literal["user", "assistant"]
    content: tuple[CanonicalTranscriptBlock, ...]

    def __post_init__(self) -> None:
        if self.role == "user":
            all_text = bool(self.content) and all(isinstance(block, TextBlock) for block in self.content)
            all_results = bool(self.content) and all(
                isinstance(block, ToolResultBlock) for block in self.content
            )
            if not (all_text or all_results):
                raise ValueError("user turn must be all text or all tool results")
        elif any(isinstance(block, ToolResultBlock) for block in self.content):
            raise ValueError("assistant messages cannot contain tool results")


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
    tools: tuple[ToolDefinition, ...]


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
    tools: tuple[ToolDefinition, ...]
```

This is an extension of Phase 1's session contract, not a replacement: before creating a tool-capable session, construction preserves the exact Phase 1 `ordinary` binding and adds exact `tool_use_boundary` and `post_tool_result` row digests for the selected runtime/model/thinking tuple. All three rows must have passing mappings for the session dialect. Tool parsing, replay, evidence loading, or continuation must never recompute, omit, substitute, or change those immutable bindings.

Extend Phase 1's `CanonicalResult.stop_reason` from `Literal["end_turn"]` to `Literal["end_turn", "tool_use", "tool_calls"]`. Backend normalization produces `end_turn` or `tool_use`; after the actor commits a `tool_use` boundary it derives the dialect-facing canonical result as `tool_use` for Anthropic or `tool_calls` for OpenAI. The adapters accept only their matching canonical value. Unknown or cross-dialect combinations remain `502 sdk_protocol_error`.

Enforce configured counts and UTF-8/JSON byte limits before canonical hashing. Canonical tool fingerprints preserve definition-list order and normalize schema object keys; request fingerprints cover canonical tools, assistant text/thinking/redacted-thinking/tool-use blocks, result turns, exact thinking/redacted-thinking payloads and optional signatures, and their order. Transcript-prefix validation accepts only exact prior block types/payloads/signatures/tool IDs/results and returns only a valid next ordinary-user turn or complete pending-result turn. It never drops thinking adjacent to a tool call, normalizes a signature, or reorders a tool boundary. Session continuation compares the full `ImmutableSessionConfig`, so changing a name, description, schema, definition order, dialect, model, system, thinking, effort, parameter policy, or any ordinary/tool-boundary/post-result usage-row binding fails before reservation or callback resolution. Update `tests/unit/test_transcript.py`, `tests/unit/test_fingerprints.py`, and `tests/unit/test_sessions.py` for positive multi-cycle thinking+tool history, exact thinking/redacted signatures/order, changed/missing/duplicate call/result blocks, reorder attacks, immutable-tool mismatches, and each usage-row-digest mismatch.

`ToolReleaseEvidence.load(phase0_path, framing_path, usage_mapping_resolver)` must use only the Phase 0 APIs declared in Task 10: call `phase0 = load_manifest(phase0_path)`, `loaded_usage_schema = load_usage_evidence(phase0)`, and `sdk_manifest = load_sdk_tool_evidence(phase0)`. The supplied resolver is the one immutable `ProxyConfig.usage_mapping_resolver` already used by Phase 1 admission; do not construct a second resolver in Phase 3. Before accepting it, require its schema version/digest to equal `loaded_usage_schema`, canonical-encode both complete validated schema projections and require byte equality, and verify every SDK record's runtime/backend model occurs only in rows from that schema. For every canonical SDK-record digest, resolve its exact configured alias/backend pair and retain the declared `phase0_prerequisite_digest(phase0, alias, backend_model_id)` in an immutable `phase0_prerequisite_digests_by_sdk_digest` map plus the exact `usage_schema_digest`; this binds the resolver and SDK index to the same loaded Phase 0 model/runtime/platform snapshot without inventing a second manifest encoder. Reject a resolver with merely overlapping rows, a changed digest, changed canonical schema, a different prerequisite digest, or mutable/nonvalidated data. The SDK loader validates exactly `REQUIRED_SDK_TOOL_GATES`, generated SDK MCP names, and each SDK/runtime/model tuple. Phase 3 calls Phase 0's `sdk_tool_record_digest(record)` for every record, rejects duplicate/colliding returned digests, and builds the immutable `sdk_records_by_digest` index; it never redefines the projection/domain or performs positional/"only record" lookup. Unit tests load a valid resolver, then independently mutate the Phase 0 usage digest, one canonical usage field/mapping while retaining a forged digest, runtime/model identity, prerequisite digest, and resolver object; every mismatch rejects construction before any capability decision.

Load production framing through `ToolFramingEvidenceManifest.load(path)`. The top-level object has exactly `schema_version: 1` and `records`; every record has exactly `schema_version: 1`, the complete structural `key`, `validated_at_utc`, `result_digest`, and `gates`. The key contains the SDK-record digest, dialect, exact `ToolThinkingIdentity`, canonical `tool_usage_binding_digest`, and streaming boolean; `canonical_key()` hashes that complete projection under its V1 domain. Accept `validated_at_utc` only as a canonical UTC RFC 3339 value with whole seconds and trailing `Z`; accept digests only as lowercase 64-character SHA-256 hex. Require the gate map's key set to equal `REQUIRED_FRAMING_GATES` and every value to be a literal boolean, preserving honest `False` observations. Copy records and gate maps into immutable mappings. Reject unknown/missing fields, unsupported schemas, malformed timestamps/digests/enums/booleans/thinking identities, a serialized map key that differs from the embedded exact key, duplicate canonical keys, or key collisions. `require_row()` rejects a missing row or any false gate and never falls back across SDK digest, dialect, thinking identity, tool-usage-binding digest, or streaming mode.

`canonical_tool_usage_binding_digest()` accepts exactly the SDK-record digest, dialect, `ToolThinkingIdentity`, and ordered `ordinary`/`tool_use_boundary`/`post_tool_result` complete validated rows plus their exact passing dialect mappings. It requires all three Phase 0 `UsageTupleKey` values to share the SDK record's runtime/model and exactly the same thinking mode, effort, and exact `budget_tokens`, and requires their operation classes to match their positions. Null is exactly `(mode="null", effort=None, budget_tokens=None)`; enabled requires one exact positive integer budget and exact effort-or-`None`; disabled/adaptive identities and any OpenAI identity other than null are rejected. `ToolThinkingIdentity` is copied directly from those Phase 0 fields and contains no budget class. The candidate and serving aggregate additionally require that exact budget/effort pair in the shared Phase 1 allowlist. There is no `tokens:N` parsing, range/bucket equivalence, or independently derived budget identity. It then hashes the complete projection under `claude-sdk-proxy:tool-framing-usage-binding:v1\0`. `ResolvedToolUsageBinding.digest` is that value; supplied `UsageRowBindings` must name exactly those three row digests with no missing/extra/duplicate entry.

`canonical_framing_result_digest()` validates a content-free redacted result tree with exactly these fields: exact thinking identity, ordered wire event-type names, thinking/tool block-order match, thinking-signature fidelity, canonical stop reason, complete terminal-bundle match, exact-usage-match boolean, exact-replay-match boolean, lifecycle outcome, cleanup outcome, and stable error code or `null`. It rejects thinking/tool text, signature values, arguments, results, raw frames, usage values, session IDs, paths, timestamps, bytes, floats, and unknown fields. It computes `sha256(b"claude-sdk-proxy:tool-framing-result:v1\0" + canonical_evidence_json(redacted_result)).hexdigest()` using Phase 0's encoder. The candidate writes only that digest into the row. `ToolReleaseEvidence.require_session_tools(sdk_record_digest, dialect, usage_rows)` first resolves that exact digest in the loaded Phase 0 SDK manifest, resolves one exact `ResolvedToolUsageBinding` through the retained snapshot-validated resolver, then requires both `streaming=False` and `streaming=True` framing rows whose SDK digest, dialect, thinking identity, and tool-usage-binding digest all match. A row probed for null thinking or any other usage binding cannot authorize an enabled-thinking session.

After that exact validation succeeds, `release_binding()` computes `sha256(b"claude-sdk-proxy:tool-release:v1\0" + canonical_evidence_json(projection)).hexdigest()` and returns `ToolReleaseBinding(schema_version=1, digest=...)`. The projection is an exact immutable JSON tree containing the release-binding schema version, selected Phase 0 prerequisite digest and usage-schema digest, complete canonical SDK evidence record and its digest, exact dialect, exact thinking identity, canonical tool-usage-binding digest, the complete three usage rows plus their passing dialect mappings and digests, framing-manifest schema version, and the complete matching non-streaming and streaming records. Each projected framing record includes its record schema version, complete tuple-specific key, canonical UTC validation timestamp, canonical redacted result digest, and the complete sorted gate map. Use Phase 0's `canonical_evidence_json()` and exclude only the binding's own digest field; no `repr`, default JSON encoder, map iteration order, or locale-dependent formatting participates. Missing/false rows raise `ToolEvidenceError` rather than producing a binding. Unsupported schema changes reject loading; once a new schema is explicitly implemented it must use a new domain/version and cannot validate as v1. Any valid SDK record, naming rule, prerequisite, usage row/mapping, thinking identity, framing row/result/timestamp refresh creates a new binding and invalidates Phase 4 rich-result evidence until it is rerun.

`tools_enabled_for()` is the boolean wrapper for one exact `(sdk_record_digest, dialect, UsageRowBindings)` tuple; every call supplies all four function arguments, including the exact bindings. It resolves the thinking/usage-binding identity from those exact rows and cannot select a representative framing row. Any caller exposing one `tools` boolean across multiple thinking tuples, including Phase 4 capability assembly, must supply the nonempty deterministic set valid for that exact dialect: OpenAI supplies only the null/no-thinking tuple because Phase 2 rejects reasoning, while Anthropic supplies null plus its exact implemented/admitted enabled tuples. Set the aggregate true only when this exact predicate succeeds for every tuple in that dialect-specific set; parser admission must consume that same aggregate before tuple-specific lookup. An Anthropic-only enabled tuple must never disable OpenAI tools, and callers must never synthesize an OpenAI reasoning case. Any missing/mismatched SDK, usage-row, usage-mapping, dialect-valid thinking tuple, tool-usage-binding digest, or streaming-mode evidence therefore makes parser acceptance, bridge construction, and aggregate capability output false. `tool_capability_projection()` follows those conservative semantics rather than selecting one representative/default usage binding. Add explicit tests that backend normalization accepts only the extended result contract, the actor commits only a tool stop, both adapters map their allowed wire stop, and documented protocol values match the tested literals.

- [ ] **Step 4: Run domain/evidence tests and static checks**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_tool_domain.py tests/unit/test_tool_evidence.py tests/unit/test_transcript.py tests/unit/test_fingerprints.py tests/unit/test_sessions.py -v`

Expected: PASS across canonical tool types/history, fingerprints, immutable session configuration, and the composed evidence gate.

Run: `uv run mypy src/claude_sdk_proxy/domain.py src/claude_sdk_proxy/tool_evidence.py src/claude_sdk_proxy/capabilities.py`

Expected: no errors.

- [ ] **Step 5: Commit the tool contract and hard gate**

```bash
git add src/claude_sdk_proxy/domain.py src/claude_sdk_proxy/tool_evidence.py src/claude_sdk_proxy/capabilities.py tests/unit/test_tool_domain.py tests/unit/test_tool_evidence.py tests/unit/test_transcript.py tests/unit/test_fingerprints.py tests/unit/test_sessions.py
git commit -m "feat: define gated external tool contract"
```

---

### Task 2: Build the Attested Correlated MCP Bridge

**Files:**
- Create: `src/claude_sdk_proxy/tool_bridge.py`
- Modify: `src/claude_sdk_proxy/backend.py`
- Modify: `src/claude_sdk_proxy/attestation.py`
- Modify: `tests/unit/test_backend.py`
- Create: `tests/unit/test_tool_bridge.py`
- Create: `tests/integration/test_tool_session_creation.py`

**Interfaces:**
- Consumes: arbitrary accepted immutable `ToolDefinition` tuples, composed `ToolReleaseEvidence`, `AttestationGate`, `CleanupRegistry`, `AllocationHandle`, and the exact Phase 0 public-ID correlation/`SdkMcpNamingRule` mechanism.
- Produces: `PendingCallback`, `ToolBridge`, internal `AttestedToolBridgeBuilder.create()`, production `ToolBridgeFactory.create()`, `ToolBridge.all_suspended()`, `ToolBridge.resolve_all()`, `ToolBridge.cancel_and_scrub()`, and tool-aware `PreparedBackend` construction.

- [ ] **Step 1: Write failing correlation, attestation, and rollback tests**

```python
# tests/unit/test_tool_bridge.py
import pytest

from claude_sdk_proxy.domain import ToolResultBlock


@pytest.mark.anyio
async def test_reverse_results_resolve_exact_public_callbacks(bridge) -> None:
    first = bridge.suspend_for_test("toolu_1", "echo", {"x": 1})
    second = bridge.suspend_for_test("toolu_2", "echo", {"x": 1})
    await bridge.resolve_all((
        ToolResultBlock(tool_use_id="toolu_2", text="second"),
        ToolResultBlock(tool_use_id="toolu_1", text="first"),
    ))
    assert (await first)["content"][0]["text"] == "first"
    assert (await second)["content"][0]["text"] == "second"


@pytest.mark.anyio
async def test_schema_never_reaches_model_before_child_attestation(bridge_factory, trace) -> None:
    await bridge_factory.create_with_blocked_attestation(schema_canary="SCHEMA-CANARY-91d2")
    assert trace.local_option_serializations == 1
    assert trace.model_submissions == 0
    assert trace.network_payload_contains("SCHEMA-CANARY-91d2") is False


@pytest.mark.anyio
async def test_bridge_derives_and_observes_per_session_generated_names_without_override(
    bridge_factory, release_evidence, attestation_gate, immutable_session_config,
    configured_sdk_record_digest,
) -> None:
    definitions = (tool("echo"), tool("snake_case"), tool("dash-name"))
    config = immutable_session_config(dialect=Dialect.ANTHROPIC, tools=definitions)
    digest = configured_sdk_record_digest
    bridge, backend = await bridge_factory.create(
        config,
        sdk_record_digest=digest,
        evidence=release_evidence,
        attestation_gate=attestation_gate,
    )
    rule = release_evidence.require_sdk_record(digest).naming_rule
    expected = {definition.name: rule.derive(definition.name) for definition in definitions}
    assert bridge.generated_name_mapping == expected
    assert bridge.observed_generated_name_mapping == expected
    assert backend.allowed_tools == tuple(expected[definition.name] for definition in definitions)
    assert "CLAUDE_AGENT_SDK_MCP_NO_PREFIX" not in backend.child_environment


@pytest.mark.anyio
@pytest.mark.parametrize("caller_name", ["", "naïve", "工具", "x" * 65])
async def test_invalid_caller_tool_name_rejects_before_sdk_construction(
    bridge_factory, release_evidence, attestation_gate, immutable_session_config,
    configured_sdk_record_digest, caller_name,
) -> None:
    digest = configured_sdk_record_digest
    config = immutable_session_config(tools=(tool(caller_name),))
    with pytest.raises(ProxyError, match="invalid tool name"):
        await bridge_factory.create(
            config,
            sdk_record_digest=digest,
            evidence=release_evidence,
            attestation_gate=attestation_gate,
        )
    assert bridge_factory.sdk_server_constructions == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    ["missing_digest", "mismatched_digest", "wrong_dialect", "missing_nonstream", "missing_stream"],
)
async def test_production_factory_requires_exact_session_evidence_before_construction(
    bridge_factory, release_evidence, attestation_gate, immutable_session_config, case
) -> None:
    evidence, digest, dialect = release_evidence.for_failure_case(case)
    config = immutable_session_config(dialect=dialect, tools=(tool("echo"),))
    with pytest.raises(ProxyError, match="tool release evidence"):
        await bridge_factory.create(
            config,
            sdk_record_digest=digest,
            evidence=evidence,
            attestation_gate=attestation_gate,
        )
    assert bridge_factory.sdk_server_constructions == 0
```

```python
# additions to tests/unit/test_backend.py
@pytest.mark.parametrize(
    ("sdk_stop", "expected"),
    [("end_turn", "end_turn"), ("tool_use", "tool_use")],
)
def test_backend_normalizes_only_supported_sdk_result_stops(backend_normalizer, sdk_stop, expected) -> None:
    assert backend_normalizer.result(stop=sdk_stop).stop_reason == expected


def test_backend_rejects_unknown_sdk_result_stop(backend_normalizer) -> None:
    with pytest.raises(ProxyError, match="sdk_protocol_error"):
        backend_normalizer.result(stop="mystery_stop")
```

```python
# tests/integration/test_tool_session_creation.py
@pytest.mark.anyio
async def test_backend_construction_failure_transfers_all_new_resources(app_harness) -> None:
    response = await app_harness.create_tool_session(fail_backend_after_bridge=True)
    assert response.status_code == 503
    assert app_harness.registry_size == 0
    assert app_harness.cleanup_transfers == 1
    assert app_harness.unowned_resources == ()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    ["missing_digest", "mismatched_digest", "wrong_dialect", "missing_nonstream", "missing_stream"],
)
async def test_structural_evidence_mismatch_rejects_parser_before_bridge(
    app_harness, case
) -> None:
    response = await app_harness.create_tool_session(evidence_case=case)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_tools"
    assert app_harness.lifecycle_reservations == 0
    assert app_harness.bridge_constructions == 0
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_tool_bridge.py tests/integration/test_tool_session_creation.py -v`

Expected: FAIL because `ToolBridge` and attested tool construction do not exist.

- [ ] **Step 3: Implement public-ID correlation and attested construction**

```python
# Core declarations for src/claude_sdk_proxy/tool_bridge.py
from dataclasses import dataclass
from typing import Any

import anyio


@dataclass(slots=True)
class PendingCallback:
    public_id: str
    tool_name: str
    arguments: dict[str, Any] | None
    ready: anyio.Event
    value: dict[str, Any] | None = None


class ToolBridge:
    def all_suspended(self, public_ids: tuple[str, ...]) -> bool: ...
    async def resolve_all(self, results: tuple[ToolResultBlock, ...]) -> None: ...
    async def cancel_and_scrub(self, reason: str) -> None: ...


class ToolBridgeFactory:
    async def create(
        self,
        config: ImmutableSessionConfig,
        *,
        sdk_record_digest: str,
        attestation_gate: AttestationGate,
        evidence: ToolReleaseEvidence,
    ) -> tuple[ToolBridge, PreparedBackend]: ...


class AttestedToolBridgeBuilder:
    async def create(
        self,
        definitions: tuple[ToolDefinition, ...],
        *,
        attestation_gate: AttestationGate,
        sdk_evidence: SdkToolEvidenceRecord,
    ) -> tuple[ToolBridge, PreparedBackend]: ...
```

For each session, validate every immutable caller definition with the Phase 0 `SdkMcpNamingRule`, reject duplicate caller or derived names, and construct exactly one SDK MCP server with the attested fixed server identity. Derive the expected mapping for all definitions, observe the actual generated mapping from the documented SDK surface after construction, and require exact key/value/order equality before model submission. This is a per-session mapping, not the representative Phase 0 observation map. A mismatch fails closed and transfers cleanup ownership. Require one distinct public ID for each suspended callback before `all_suspended()` can succeed. Validate the complete result set before assigning any value, then assign every value before signaling any callback. `cancel_and_scrub()` wakes all handlers with a controlled exception and replaces argument/result references with `None`.

Extend the Phase 1 prepared backend with exactly one in-process MCP server, `tools=[]`, and `allowed_tools` populated only from that validated per-session generated mapping in caller-definition order. Do not set `CLAUDE_AGENT_SDK_MCP_NO_PREFIX` or another naming override. `AttestedToolBridgeBuilder` is the shared low-level constructor requiring passing Phase 0 SDK evidence and child attestation; it is not imported by the public router. Production `ToolBridgeFactory.create()` accepts the already-bound immutable session config plus the exact SDK record digest selected from the configured alias/backend tuple; it derives definitions and dialect only from that config, calls `evidence.require_session_tools(sdk_record_digest, config.dialect, config.usage_rows)` before SDK server/allocation construction, requires the resolved SDK record's backend model/runtime identity to equal the immutable config and current validated runtime, then resolves that same digest's naming rule and delegates. Neither request metadata nor a caller can supply or replace any digest. A later Phase 4 candidate adapter may use this same low-level builder only after validating the exact `ToolReleaseBinding`, calling `ToolReleaseEvidence.require_sdk_record(sdk_record_digest)`, and passing that returned record unchanged as `sdk_evidence`; it must resolve callback results through the existing atomic `ToolBridge.resolve_all()` method and may not add a rich-only resolution method or select an SDK record by alias/model fallback. Normalize every SDK tool stop into `CanonicalResult(stop_reason="tool_use", usage.operation_class=TOOL_USE_BOUNDARY)`, including later cycles; normalize only a non-tool terminal after resolved callbacks as `usage.operation_class=POST_TOOL_RESULT`. Each uses its exact bound row. `tests/unit/test_backend.py` covers accepted/missing/unknown SDK stop/result/usage shapes and wrong operation rows. No user turn is released until executable, environment, endpoint/provider, auth source, exact model, all usage mappings, evidence, and generated names pass. Every partial failure calls `AllocationHandle.transfer_to_cleanup()`, and the resulting `CleanupTicket` is adopted by `CleanupRegistry`.

- [ ] **Step 4: Run bridge/session-creation tests**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_tool_bridge.py tests/unit/test_attestation.py tests/unit/test_backend.py tests/integration/test_tool_session_creation.py -v`

Expected: PASS, including reverse correlation, no pre-attestation model submission, and exact-once rollback transfer.

- [ ] **Step 5: Commit the attested bridge**

```bash
git add src/claude_sdk_proxy/tool_bridge.py src/claude_sdk_proxy/backend.py src/claude_sdk_proxy/attestation.py tests/unit/test_tool_bridge.py tests/integration/test_tool_session_creation.py
git commit -m "feat: add attested correlated MCP bridge"
```

---

### Task 3: Integrate Tool Waits with Capacity, Replay, Deadlines, and Teardown

**Files:**
- Modify: `src/claude_sdk_proxy/actor.py`
- Modify: `src/claude_sdk_proxy/control.py`
- Modify: `src/claude_sdk_proxy/router.py`
- Modify: `src/claude_sdk_proxy/config.py`
- Create: `tests/unit/test_tool_actor.py`

**Interfaces:**
- Consumes: `ToolBridge`, `PendingToolSet`, `CapacityLedger`, `OperationLease`, `TerminalCell`, `CleanupRegistry`, `ReplaySnapshot`, `PreparedBackend.watch_liveness()`, Phase 1 `CommittedReply`/`AdmissionResult`, `Deadline`, async `DeadlineScheduler.arm()`, `DeadlineHandle.cancel_once()`, `SessionActor.set_pending_tool_deadline()`, and the Phase 1 actor-owned task group/lock/clock.
- Produces: `ActorState.WAITING_FOR_TOOLS`, `SessionActor.commit_tool_boundary() -> CommittedReply`, `SessionActor.submit_tool_results() -> AdmissionResult`, activation of Phase 1's reserved pending-tool deadline branch, and tool-aware terminal/liveness-loss transitions.

- [ ] **Step 1: Write failing atomic-commit and capacity tests**

```python
# tests/unit/test_tool_actor.py
import pytest


@pytest.mark.anyio
async def test_tool_boundary_commits_only_after_all_callbacks_and_terminal_bundle(actor) -> None:
    actor.backend.emit_two_tool_calls()
    await actor.backend.suspend_callback("toolu_1")
    assert actor.state.value == "generating"
    await actor.backend.suspend_callback("toolu_2")
    reply = await actor.finish_tool_boundary()
    assert actor.state.value == "waiting_for_tools"
    assert actor.head == reply.result_head
    assert actor.committed_artifact.body.endswith(b"message_stop\n\n")


@pytest.mark.anyio
async def test_capacity_failure_precedes_head_and_callback_resolution(actor) -> None:
    actor.capacity.fail_component("response_writer")
    old_head = actor.head
    response = await actor.submit_tool_results_for_test()
    assert response.code == "response_writer_capacity"
    assert actor.head == old_head
    assert actor.bridge.resolved_count == 0
    assert actor.capacity.outstanding == 0
```

- [ ] **Step 2: Write deadline, replay, and terminal-race tests**

```python
@pytest.mark.anyio
@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [
        (("run", "session", "tool", "operation"), "run_expired"),
        (("session", "tool", "operation"), "session_expired"),
        (("tool", "operation"), "tool_result_timeout"),
        (("operation",), "operation_timeout"),
    ],
)
async def test_under_lock_deadline_precedence(actor, elapsed, expected) -> None:
    actor.clock.expire(*elapsed)
    result = await actor.submit_tool_results_for_test(timer_callback_delayed=True)
    assert result.reason == expected


@pytest.mark.anyio
async def test_pending_tool_deadline_uses_actor_owned_scheduler_and_cancels_once(actor) -> None:
    await actor.enter_waiting_for_tools()
    handle = actor.deadline_scheduler.handle_for(
        actor.owner_key, actor.pending_tool_generation
    )
    assert actor.deadline_scheduler.task_group is actor.task_group
    assert actor.independent_tool_timer_tasks == 0
    await actor.submit_tool_results_for_test()
    assert handle.cancel_once_calls == 1
    await actor.close_for_test("shutdown")
    assert handle.cancel_once_calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("winner", "expected_state", "expected_reason"),
    [
        ("response", "generating", None),
        ("timeout", "closed", "tool_result_timeout"),
        ("cancel", "closed", "deleted"),
        ("loss", "lost", "backend_lost"),
    ],
)
async def test_response_timeout_cancel_and_loss_races_consume_one_generation_once(
    actor, winner, expected_state, expected_reason
) -> None:
    await actor.enter_waiting_for_tools()
    generation = actor.pending_tool_generation
    handle = actor.deadline_scheduler.handle_for(actor.owner_key, generation)
    outcome = await actor.race_pending_tool_transition(winner=winner)
    assert outcome.accepted_transitions == 1
    assert actor.state.value == expected_state
    assert actor.terminal_reason == expected_reason
    assert handle.cancel_once_calls == 1
    assert actor.pending_tool_deadline_handle is None
    await actor.deliver_stale_pending_tool_wake(generation)
    assert actor.state.value == expected_state
    assert actor.cleanup_transfer_count <= 1


@pytest.mark.anyio
async def test_terminal_transition_publishes_run_cell_and_transfers_once(actor) -> None:
    await actor.close_for_test("tool_result_timeout")
    assert actor.run_terminal_cell.reason == "tool_result_timeout"
    assert actor.cleanup_transfer_count == 1
    assert actor.content_owner_references == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("dialect", "expected"),
    [("anthropic", "tool_use"), ("openai", "tool_calls")],
)
async def test_actor_derives_the_dialect_facing_tool_stop(actor, dialect, expected) -> None:
    reply = await actor.commit_complete_tool_boundary(dialect=dialect)
    assert reply.result.stop_reason == expected


@pytest.mark.anyio
@pytest.mark.parametrize("cause", ["child_exit", "sdk_transport_lost", "supervisor_channel_lost"])
async def test_waiting_for_tools_liveness_loss_is_terminal_and_exact_once(
    automatic_actor, cause
) -> None:
    await automatic_actor.enter_waiting_for_tools_with_suspended_callbacks()
    automatic_actor.backend.complete_liveness_loss(cause)
    await automatic_actor.wait_for_serialized_transition()
    assert automatic_actor.transition_order[:2] == ("terminal_cell_closed", "state_lost")
    assert automatic_actor.run_terminal_cell.reason == "backend_lost"
    assert automatic_actor.state.value == "lost"
    assert automatic_actor.bridge.pending_content_references == 0
    assert automatic_actor.content_owner_references == 0
    assert automatic_actor.pending_writer_snapshot_authority == 0
    assert automatic_actor.cleanup_transfer_count == 1
    automatic_actor.backend.complete_duplicate_liveness_loss(cause)
    await automatic_actor.close_for_test("shutdown")
    assert automatic_actor.cleanup_transfer_count == 1
```

- [ ] **Step 3: Run the actor tests to verify they fail**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_tool_actor.py -v`

Expected: FAIL because tool continuation does not use the approved reservation/deadline/terminal contracts.

- [ ] **Step 4: Implement the tool-aware actor transitions**

```python
# Phase 3 additions to the existing Phase 1 SessionActor only.
class SessionActor:
    async def commit_tool_boundary(
        self,
        *,
        calls: tuple[ToolUseBlock, ...],
        terminal_bundle: bytes,
        replay_bytes: bytes,
    ) -> CommittedReply: ...

    async def submit_tool_results(
        self,
        request: CanonicalRequest,
        *,
        head: str,
        idempotency_key: str,
    ) -> AdmissionResult: ...
```

`commit_tool_boundary()` checks exact model identity, complete calls, suspended callbacks, SDK event ordering, backend `CanonicalResult(stop_reason="tool_use", usage.operation_class=TOOL_USE_BOUNDARY)`, the session's bound row digest, and the selected dialect's exact passing mapping before atomically committing replay bytes, head, idempotency record, pending set, a Phase 1 `Deadline`, and `WAITING_FOR_TOOLS`. Increment the actor's pending-tool generation, then call the existing `await SessionActor.set_pending_tool_deadline(deadline, generation)`; Phase 3 enables the non-`None` branch that Phase 1 reserved. That method calls the existing async `await DeadlineScheduler.arm(owner_key, generation, deadline, actor_deadline_callback)`, stores the returned Phase 1 `DeadlineHandle` under the actor lock, and cancels any replaced handle with `await old_handle.cancel_once()`. Do not introduce a tool-specific deadline value, scheduler, handle, sleeper, callback-owned timer, HTTP-task timer, or second scheduler. It then derives the reply's dialect-facing `CanonicalResult` as `tool_use` for Anthropic or `tool_calls` for OpenAI without changing canonical usage. `tests/unit/test_tool_actor.py` proves both valid derivations and rejects `end_turn`, unknown stops, dialect/value mismatches, wrong operation class, and mapping/row digest drift. `submit_tool_results()` performs scoped replay/conflict lookup, current-head/transcript validation, validates both bound outcomes the resumed SDK turn may produce (`TOOL_USE_BOUNDARY` for another call cycle or `POST_TOOL_RESULT` for a non-tool terminal), under-lock deadline checks, and complete capacity reservation before creating the next head. Set `mutation_possible=True` immediately before `resolve_all()`.

Reuse Phase 1's concrete actor reply contract rather than adding tool-only reply classes: retain `CommittedReply.content: tuple[CanonicalAssistantOutputBlock, ...]` and extend that alias with `ToolUseBlock` as above, have `commit_tool_boundary()` return that `CommittedReply` without losing or reordering thinking/redacted-thinking blocks around tool calls, and have `submit_tool_results()` return the existing `AdmissionResult` union whose `NewOperation` eventually commits or streams through the existing writer/replay path.

Apply the approved post-reservation failure table. Only a positively proved pre-resolution failure with a usable prior native session can restore `WAITING_FOR_TOOLS`; unknown or post-resolution failure becomes `LOST`. Terminal causes publish the automatic run cell, block new snapshot admission, transfer resources once, and allow only already admitted immutable snapshots to drain under their write lease.

The Phase 1 scheduler callback receives the exact actor `owner_key` and pending-tool `generation` supplied to `arm()` and only wakes the serialized actor transition. Under the actor lock, reject a callback unless both values match the current actor and pending generation, the stored handle/deadline are current, and state is still `WAITING_FOR_TOOLS`; the scheduler never chooses a terminal cause. Every wake, result submission, retry, request cancellation/deletion, and liveness callback rechecks deadlines under that lock in this fixed precedence: absolute run expiry, absolute session expiry, pending-tool deadline, operation deadline. Equality counts as expired. The winning response, timeout, cancellation/deletion, liveness loss, terminalization, or shutdown clears the stored handle and invokes the same Phase 1 `await handle.cancel_once()` path exactly once; a fired handle may report `False`, but no path calls it again. Losing or delayed callbacks fail owner/generation/state checks and cannot resolve callbacks, change a terminal cause, transfer cleanup, or cancel a newer generation. Tests release response/timeout/cancel/loss contenders in controlled orders and assert one accepted transition, one `cancel_once()` invocation, no retained handle, and stale-wake no-op behavior.

The Phase 1 actor-owned watcher remains active throughout `WAITING_FOR_TOOLS`. Child exit, SDK transport loss, and supervisor control-channel loss all enter the same serialized transition: under the actor lock, close the automatic run's `TerminalCell` first with stable `backend_lost`, publish `LOST`, clear transcript/system/tool definitions/pending calls/results/idempotency/backend references, detach the cleanup ticket, and revoke pending writer authority. Outside the lock, call `ToolBridge.cancel_and_scrub()` to wake handlers with content-free failure and adopt the one detached ticket in `CleanupRegistry`. Duplicate watcher completions, native notifications, callback cancellation, and later `close()` cannot change the terminal cause or transfer/release anything twice.

- [ ] **Step 5: Run actor tests and commit**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_tool_actor.py tests/unit/test_actor_state.py tests/unit/test_actor_failures.py tests/unit/test_actor_deadlines.py -v`

Expected: PASS across capacity rollback, replay, delayed timers, terminal races, and cleanup transfer.

```bash
git add src/claude_sdk_proxy/actor.py src/claude_sdk_proxy/control.py src/claude_sdk_proxy/router.py src/claude_sdk_proxy/config.py tests/unit/test_tool_actor.py
git commit -m "feat: integrate tool waits with session lifecycle"
```

---

### Task 4: Add Anthropic Tool Wire and Streaming Semantics

**Files:**
- Modify: `src/claude_sdk_proxy/anthropic_adapter.py`
- Create: `tests/integration/test_anthropic_tools.py`
- Modify: `tests/integration/test_official_anthropic_client.py`

**Interfaces:**
- Consumes: canonical tool types, `SessionActor.submit_tool_results()`, and bound passing Anthropic mappings for `tool_use_boundary`/`post_tool_result`.
- Produces: Anthropic tool definition/result parsing, `tool_use` blocks, `stop_reason=tool_use`, terminal SSE bundles, and exact replay bytes.

- [ ] **Step 1: Write failing non-streaming and one-shot tests**

```python
# tests/integration/test_anthropic_tools.py
@pytest.mark.anyio
async def test_two_call_round_trip_resolves_by_public_id(client, tool_session) -> None:
    first = await client.post("/v1/messages", headers=tool_session.headers, json=tool_session.first_body)
    assert first.status_code == 200
    calls = first.json()["content"]
    assert [block["id"] for block in calls] == ["toolu_1", "toolu_2"]
    assert first.json()["stop_reason"] == "tool_use"
    second = await client.post(
        "/v1/messages",
        headers=tool_session.next_headers(first),
        json=tool_session.results_body(order=("toolu_2", "toolu_1")),
    )
    assert second.status_code == 200
    assert tool_session.backend.new_user_turn_count == 1
    assert tool_session.bridge.resolution_order == ("toolu_1", "toolu_2")


@pytest.mark.anyio
async def test_one_shot_tools_reject_before_backend(client, one_shot_tool_body, backend_factory) -> None:
    response = await client.post("/v1/messages", json=one_shot_tool_body)
    assert response.status_code == 400
    assert backend_factory.created == []


@pytest.mark.anyio
@pytest.mark.parametrize("operation_class", ["tool_use_boundary", "post_tool_result"])
async def test_anthropic_tool_usage_emits_every_mapped_sdk_leaf(
    client, tool_session, operation_class
) -> None:
    response = await tool_session.run_nonstreaming(operation_class)
    assert response.json()["usage"] == tool_session.expected_public_usage(operation_class)
    assert tool_session.assert_every_identity_bound_leaf_emitted_once(response)
```

- [ ] **Step 2: Write the terminal-success streaming test**

```python
@pytest.mark.anyio
async def test_tool_success_frames_wait_for_callbacks_and_commit(stream_client, tool_session) -> None:
    stream = await stream_client.open(tool_session.streaming_body)
    await tool_session.backend.emit_complete_calls(callbacks_suspended=False)
    assert stream.contains_event("message_delta") is False
    assert stream.contains_event("message_stop") is False
    await tool_session.backend.suspend_all_callbacks()
    await tool_session.actor.commit_pending_boundary()
    assert stream.events[-2].type == "message_delta"
    assert stream.events[-2].stop_reason == "tool_use"
    assert stream.events[-1].type == "message_stop"
    assert stream.events[-2].usage == tool_session.expected_public_usage(
        "tool_use_boundary"
    )
    assert stream.replay_bytes == stream.original_bytes


@pytest.mark.anyio
@pytest.mark.parametrize("stop_reason", ["end_turn", "tool_calls", "unknown"])
async def test_anthropic_rejects_non_tool_use_canonical_stops(adapter, stop_reason) -> None:
    with pytest.raises(ProxyError, match="sdk_protocol_error"):
        adapter.encode_tool_boundary(injected_result(stop_reason=stop_reason))
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/integration/test_anthropic_tools.py -v`

Expected: FAIL because Anthropic tool parsing and terminal-bundle gating are absent.

- [ ] **Step 4: Implement exact Anthropic tool semantics**

Parse bounded immutable tool definitions on automatic first bind or explicit creation. Before allocation require all three session usage bindings and their passing Anthropic mappings. Render public SDK IDs unchanged. Parse a result turn separately from an ordinary user turn and reject missing, duplicate, unknown, partial, oversized, non-text, mixed, or changed-schema input before capacity reservation. Convert results only to MCP return values. Accept only the actor's Anthropic-facing `CanonicalResult(stop_reason="tool_use")` and encode Anthropic `stop_reason=tool_use`; integration tests reject `end_turn`, `tool_calls`, unknown stops, wrong usage operation classes, and wrong/false mapping digests. Render terminal usage only with the exact operation row/mapping and emit every present identity-bound leaf once, including nested detail/optional/null/zero values. For streaming, spool all bytes from byte zero and retain the complete stop/usage `message_delta` plus `message_stop` until the tool-boundary commit succeeds. Unknown argument deltas or stop/usage shapes return `502 sdk_protocol_error` and move the session to `LOST`.

Extend the pinned official Anthropic client fixture with non-streaming and streaming tool-use boundary plus post-tool-result cases. Inject every allowlisted SDK usage leaf for each operation class, assert every public mapped value is exposed exactly, assert absent optionals remain absent, and repeat each request through exact byte replay. An unrepresentable leaf or missing/false tool-operation mapping must be absent from capabilities and fail before tool bridge/allocation, never return partial usage.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/integration/test_anthropic_tools.py tests/integration/test_official_anthropic_client.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/anthropic_adapter.py tests/integration/test_anthropic_tools.py tests/integration/test_official_anthropic_client.py
git commit -m "feat: add Anthropic caller-owned tools"
```

---

### Task 5: Add OpenAI Tool Wire and Streaming Semantics

**Files:**
- Modify: `src/claude_sdk_proxy/openai_adapter.py`
- Create: `tests/integration/test_openai_tools.py`
- Modify: `tests/integration/test_official_openai_client.py`

**Interfaces:**
- Consumes: canonical tool types, the tool-aware actor, and bound passing OpenAI mappings for `tool_use_boundary`/`post_tool_result`.
- Produces: OpenAI function-tool parsing, assistant `tool_calls`, `tool` messages, `finish_reason=tool_calls`, and replayable `[DONE]` streams.

- [ ] **Step 1: Write failing OpenAI loop and rejection tests**

```python
# tests/integration/test_openai_tools.py
@pytest.mark.anyio
async def test_openai_reverse_results_preserve_call_identity(client, openai_tool_session) -> None:
    first = await client.post("/v1/chat/completions", json=openai_tool_session.first_body)
    calls = first.json()["choices"][0]["message"]["tool_calls"]
    assert [call["id"] for call in calls] == ["call_1", "call_2"]
    assert first.json()["choices"][0]["finish_reason"] == "tool_calls"
    second = await client.post(
        "/v1/chat/completions",
        json=openai_tool_session.results_body(reverse=True),
    )
    assert second.status_code == 200
    assert openai_tool_session.bridge.resolution_order == ("call_1", "call_2")


@pytest.mark.anyio
@pytest.mark.parametrize("operation_class", ["tool_use_boundary", "post_tool_result"])
async def test_openai_tool_usage_emits_every_mapped_sdk_leaf(
    client, openai_tool_session, operation_class
) -> None:
    response = await openai_tool_session.run_nonstreaming(operation_class)
    assert response.json()["usage"] == openai_tool_session.expected_public_usage(
        operation_class
    )
    assert openai_tool_session.assert_every_identity_bound_leaf_emitted_once(response)


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["tool_choice", "parallel_tool_calls", "functions", "function_call"])
async def test_unsupported_tool_controls_reject_before_mutation(client, field, body_factory) -> None:
    response = await client.post("/v1/chat/completions", json=body_factory(field))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_parameter"
```

- [ ] **Step 2: Write the OpenAI terminal-bundle test**

```python
@pytest.mark.anyio
async def test_finish_usage_and_done_are_released_after_tool_commit(openai_stream) -> None:
    await openai_stream.emit_calls(callbacks_suspended=False)
    assert openai_stream.has_finish_reason is False
    assert openai_stream.has_done is False
    await openai_stream.suspend_and_commit()
    assert openai_stream.final_semantic_chunk["choices"][0]["finish_reason"] == "tool_calls"
    assert openai_stream.usage_chunk["choices"] == []
    assert openai_stream.usage_chunk["usage"] == openai_stream.expected_public_usage
    assert openai_stream.frames[-1] == b"data: [DONE]\n\n"
    assert openai_stream.replay_bytes == openai_stream.original_bytes


@pytest.mark.anyio
@pytest.mark.parametrize("stop_reason", ["end_turn", "tool_use", "unknown"])
async def test_openai_rejects_non_tool_calls_canonical_stops(adapter, stop_reason) -> None:
    with pytest.raises(ProxyError, match="sdk_protocol_error"):
        adapter.encode_tool_boundary(injected_result(stop_reason=stop_reason))
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/integration/test_openai_tools.py -v`

Expected: FAIL because OpenAI tool mapping and terminal-bundle gating are absent.

- [ ] **Step 4: Implement exact OpenAI mapping**

Accept function tools only and require all three session usage bindings plus passing OpenAI mappings before allocation. Serialize SDK argument objects as compact JSON without semantic changes. Preserve IDs and order. Resolve reversed tool messages by ID. Accept only the actor's OpenAI-facing `CanonicalResult(stop_reason="tool_calls")` and encode wire `finish_reason="tool_calls"`; integration tests reject `end_turn`, `tool_use`, unknown stops, wrong usage operation classes, and wrong/false mapping digests. Render terminal usage only with the exact operation row/mapping: every present identity-bound SDK leaf appears once at its legal OpenAI public path, nested detail/optional/null/zero values remain exact, and only a declared required checked-sum total may be derived. OpenAI has no supported structured error flag in this subset, so tool failures remain caller-supplied text and capabilities document that limitation. Reject legacy functions, selection/parallel controls, unknown IDs, mixed pending content, and non-text tool results before reservation. With `stream_options.include_usage=true`, retain the complete empty-choices usage chunk, finish chunk, and `[DONE]` until commit; with false/absent, retain canonical usage but apply the documented wire omission. Spool the exact stream from byte zero for replay.

Extend the official OpenAI client fixture with non-streaming and `include_usage=true` streaming tool-use boundary plus post-tool-result cases. Inject every allowlisted SDK usage leaf for each operation class, including nested details and present optional/null/zero values; cover both explicit SDK-total identity and checked-sum-total rows. Assert every exact public value is exposed, absent optionals remain absent, and replay is byte-identical. An unrepresentable leaf or missing/false operation mapping must be absent from capabilities and rejected before bridge/allocation, never reduced to prompt/completion/total.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/integration/test_openai_tools.py tests/integration/test_official_openai_client.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/openai_adapter.py tests/integration/test_openai_tools.py tests/integration/test_official_openai_client.py
git commit -m "feat: add OpenAI caller-owned tools"
```

---

### Task 6: Add Tool Diagnostics and Persistence Evidence

**Files:**
- Modify: `src/claude_sdk_proxy/diagnostics.py`
- Modify: `src/claude_sdk_proxy/capabilities.py`
- Modify: `src/claude_sdk_proxy/path_policy.py`
- Create: `tests/integration/test_tool_diagnostics_persistence.py`
- Modify: `tests/integration/test_capabilities.py`

**Interfaces:**
- Consumes: `DiagnosticSink`, the Phase 1 process-local replay HMAC key, `PathPolicy`, cleanup journals, and tool evidence.
- Produces: `diagnostic_fingerprint(key: bytes, value: bytes) -> bytes`, `capture_path_events(policy, roots)`, `compare_and_scan_safe_paths(policy, before, after, canaries)`, redacted tool lifecycle events, path-policy evidence, and authenticated tool capability output.

- [ ] **Step 1: Write failing redaction and path-policy tests**

```python
# tests/integration/test_tool_diagnostics_persistence.py
@pytest.mark.anyio
async def test_default_tool_diagnostics_never_contain_payloads(tool_app, captured_logs) -> None:
    await tool_app.run(schema="SCHEMA-CANARY-a911", argument="ARG-CANARY-b722", result="RESULT-CANARY-c533")
    encoded = captured_logs.text
    assert "SCHEMA-CANARY-a911" not in encoded
    assert "ARG-CANARY-b722" not in encoded
    assert "RESULT-CANARY-c533" not in encoded
    assert "tool_call_count" in encoded
    assert "keyed_fingerprint" in encoded


@pytest.mark.anyio
async def test_tool_run_leaves_no_prohibited_content_or_temporary_artifact(tool_app, path_policy) -> None:
    before = capture_path_events(path_policy, tool_app.monitored_roots)
    await tool_app.run(schema="SCHEMA-CANARY-a911", argument="ARG-CANARY-b722", result="RESULT-CANARY-c533")
    await tool_app.cleanup.confirmed()
    after = capture_path_events(path_policy, tool_app.monitored_roots)
    result = compare_and_scan_safe_paths(
        path_policy, before, after,
        (b"SCHEMA-CANARY-a911", b"ARG-CANARY-b722", b"RESULT-CANARY-c533"),
    )
    assert result.prohibited_content_hits == ()
    assert result.leaked_temporary_artifacts == ()
    assert result.opened_metadata_only_paths == ()


# additions to tests/integration/test_capabilities.py
@pytest.mark.parametrize(
    "case",
    ["missing_digest", "mismatched_digest", "wrong_dialect", "missing_nonstream", "missing_stream"],
)
def test_structural_tool_evidence_mismatch_disables_single_capability_flag(
    capability_projector, case
) -> None:
    projection = capability_projector.for_evidence_case(case)
    assert projection.tools is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/integration/test_tool_diagnostics_persistence.py -v`

Expected: FAIL because tool-specific keyed diagnostics and path-policy evidence are absent.

- [ ] **Step 3: Implement redacted events and shared capability projection**

```python
# Additions to src/claude_sdk_proxy/path_policy.py and diagnostics.py
def capture_path_events(policy: PathPolicy, roots: tuple[Path, ...]) -> StateEventSnapshot: ...


def compare_and_scan_safe_paths(
    policy: PathPolicy,
    before: StateEventSnapshot,
    after: StateEventSnapshot,
    canaries: tuple[bytes, ...],
) -> CanaryScanResult: ...


def diagnostic_fingerprint(key: bytes, value: bytes) -> bytes: ...
```

Emit request/session hashes, tool count, generated-name count, schema/argument/result byte counts, callback state transitions, stop/usage event types, queue/process/latency values, and process-local keyed fingerprints. Never log definitions, arguments, results, callback values, raw MCP frames, credentials, or SDK stderr. Assert allocation journals contain only the lifecycle fields allowed by spec section 12.

Normal serving uses one shared `ToolReleaseEvidence` composition of the loaded `SdkToolEvidenceManifest`, the already-created snapshot-validated Phase 1 `UsageMappingResolver`, and `ToolFramingEvidenceManifest` for parser acceptance and `/_proxy/capabilities`; never maintain a second resolver/boolean or merge their disjoint gate maps. The parser and bridge call the same `require_session_tools(exact_sdk_digest, request_or_session_dialect, usage_rows)` API. The single capability `tools` boolean is true only if that call succeeds for every required thinking/usage binding in the dialect-specific set—OpenAI null only, Anthropic null plus admitted enabled—which necessarily covers the exact ordinary/tool-boundary/post-result rows/mappings and both matching tuple-specific streaming modes. Missing or false usage mapping, missing Phase 3 evidence, or any structural mismatch keeps that public tool parser/capability false. The Task 7 candidate probe is not a router mode and cannot affect this decision. Capabilities include backend kind, auth source, semantic class, platform tuple, public alias, exact backend model, supported dialects/modes, result subset, and limits. `/health` remains detail-free and anonymous.

- [ ] **Step 4: Run diagnostics, persistence, and capability tests**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/integration/test_tool_diagnostics_persistence.py tests/integration/test_capabilities.py -v`

Expected: PASS with no prohibited persistent content and no leaked temporary artifacts.

- [ ] **Step 5: Commit diagnostics and evidence integration**

```bash
git add src/claude_sdk_proxy/diagnostics.py src/claude_sdk_proxy/capabilities.py src/claude_sdk_proxy/path_policy.py tests/integration/test_tool_diagnostics_persistence.py tests/integration/test_capabilities.py
git commit -m "test: enforce tool diagnostics and persistence policy"
```

---

### Task 7: Run the Complete Production Tool Gate

**Files:**
- Modify: `src/claude_sdk_proxy/probe_cli.py`
- Create: `src/claude_sdk_proxy/tool_probe.py`
- Create: `tests/unit/test_tool_probe_candidate.py`
- Create: `tests/live/test_proxy_tool_loop.py`
- Modify: `tests/integration/test_full_history_harness.py`
- Create: `docs/feasibility/validated-tool-release.json`
- Modify: `docs/protocol.md`
- Modify: `docs/harnesses.md`

**Interfaces:**
- Consumes: complete Phase 3 production internals without public admission, typed Phase 0 `SdkToolEvidenceManifest`, existing Claude login, lifecycle supervisor, and `tests/integration/test_full_history_harness.py` from Phase 2.
- Produces: sealed `CandidateToolProbe`, `claude-proxy-probe tools-release`, typed `ToolFramingEvidenceManifest` rows in `docs/feasibility/validated-tool-release.json`, concrete representative-harness tool cases, plus a no-skip composed release verdict required to advertise tool support.

- [ ] **Step 1: Implement the sealed candidate path and complete live matrix**

```python
# tests/unit/test_tool_probe_candidate.py
def test_candidate_requires_exact_live_opt_in_and_passing_phase0(
    monkeypatch, phase0_manifest, proxy_config
) -> None:
    monkeypatch.delenv("RUN_LIVE_CLAUDE_TESTS", raising=False)
    with pytest.raises(ProbeCandidateError, match="live opt-in"):
        CandidateToolProbe.create(
            phase0_manifest,
            proxy_config.usage_mapping_resolver,
            proxy_config.thinking_allowlist,
        )
    monkeypatch.setenv("RUN_LIVE_CLAUDE_TESTS", "1")
    with pytest.raises(ProbeCandidateError, match="Phase 0 SDK tool evidence"):
        CandidateToolProbe.create(
            phase0_manifest.with_failed_sdk_tool_gate(),
            proxy_config.usage_mapping_resolver,
            proxy_config.thinking_allowlist,
        )


def test_candidate_is_absent_from_public_configuration_router_and_capabilities(app) -> None:
    assert "tool_candidate" not in ProxyConfig.model_fields
    assert all("candidate" not in route.path for route in app.router.routes)
    assert "candidate" not in app.capabilities.as_json()
    assert "tools-release" not in serve_parser().format_help()


def test_normal_serve_requires_release_evidence(normal_server_factory, phase0_manifest) -> None:
    server = normal_server_factory(phase0=phase0_manifest, phase3_release=None)
    assert server.capabilities.tools is False
    assert server.post_tool_request().error.code == "unsupported_tools"


def test_candidate_emits_complete_strict_framing_records(candidate_probe) -> None:
    manifest, redacted_results = candidate_probe.build_complete_manifest_for_test()
    assert manifest.schema_version == 1
    assert set(manifest.records) == set(candidate_probe.expected_framing_keys())
    for key, record in manifest.records.items():
        assert record.schema_version == 1
        assert record.key == key
        assert record.validated_at_utc.endswith("Z")
        assert set(record.gates) == REQUIRED_FRAMING_GATES
        assert all(value is True for value in record.gates.values())
        assert record.result_digest == canonical_framing_result_digest(
            redacted_results[key]
        )


# tests/live/test_proxy_tool_loop.py
FRAMING_SCENARIOS = tuple(sorted(REQUIRED_FRAMING_GATES))


@pytest.mark.live
@pytest.mark.anyio
@pytest.mark.parametrize("dialect", ["anthropic", "openai"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("scenario", FRAMING_SCENARIOS)
async def test_tool_release_matrix(candidate_probe, dialect, streaming, scenario) -> None:
    usage_cases = candidate_probe.required_usage_cases(Dialect(dialect))
    assert usage_cases
    if dialect == "openai":
        assert [case.thinking.mode for case in usage_cases] == ["null"]
    else:
        assert usage_cases[0].thinking.mode == "null"
        assert {case.thinking.mode for case in usage_cases} <= {"null", "enabled"}
    for usage_case in usage_cases:
        result = await candidate_probe.run_tool_scenario(
            dialect=Dialect(dialect),
            thinking=usage_case.thinking,
            usage_rows=usage_case.usage_rows,
            tool_usage_binding_digest=usage_case.digest,
            streaming=streaming,
            scenario=scenario,
        )
        assert result.passed, result.redacted_evidence
        assert result.built_in_tools == ()
        assert result.auth_source == "existing_claude_login"
        assert result.backend_model_id == candidate_probe.exact_backend_model_id
        assert result.thinking == usage_case.thinking
        assert result.tool_usage_binding_digest == usage_case.digest
        assert result.usage_operation_classes == {
            "tool_use_boundary", "post_tool_result",
        }
        assert result.thinking_tool_event_order_exact is True
        assert result.thinking_signature_fidelity is True
        assert result.terminal_bundle_exact is True
        assert result.every_present_sdk_usage_leaf_emitted_once is True
        assert result.byte_exact_replay is True
```

`CandidateToolProbe.create(phase0, usage_mapping_resolver, thinking_allowlist)` is the only bootstrap path. It requires the environment value `RUN_LIVE_CLAUDE_TESTS=1`, loads and validates the exact passing Phase 0 SDK/runtime/model record, verifies the supplied immutable resolver and allowlist are the same Phase 1 snapshots derived from that Phase 0 manifest, mints a module-private nonserializable permit, and invokes `AttestedToolBridgeBuilder` directly. It uses the production actor/adapters/h11 transport over private socketpairs and child processes, but never starts a TCP listener. It is absent from `ProxyConfig`, `server_cli serve`, `ProxyRouter`, control endpoints, and capability projection; no request header, body, environment-backed serve option, or config file can select it. The CLI subcommand is implemented in `probe_cli.py`, not `server_cli.py`. Normal `ToolBridgeFactory` and every normal parser continue to require passing `ToolReleaseEvidence`.

The shared candidate runner first builds the complete deterministic admitted usage-case set from the same immutable Phase 1 resolver and thinking allowlist used by serving: OpenAI contains exactly the null/no-thinking case; Anthropic contains null plus every exact admitted enabled tuple whose ordinary/tool-boundary/post-result mappings pass. It emits explicit false/ineligible cases for required tuples with unresolved mappings and never probes or writes an OpenAI enabled-thinking row. For every eligible tuple, dialect, and streaming value, it returns exactly one `REQUIRED_FRAMING_GATES` boolean per scenario under a `ToolFramingEvidenceKey` containing the exact loaded Phase 0 SDK-record digest, dialect, exact `ToolThinkingIdentity`, canonical tool-usage-binding digest, and streaming mode. Every case exercises both `tool_use_boundary` and `post_tool_result`, injects every allowlisted SDK usage leaf for those exact bound rows, preserves thinking/redacted-thinking/tool block ordering and signature fidelity, verifies the exact terminal stop/usage bundle, and verifies exact per-dialect identity/derived rendering plus byte-exact replay; the corresponding gate is false if any block/delta/signature/terminal field/usage leaf is lost, duplicated, coerced, relocated, reordered, partially emitted, or represented by an unauthorized aggregate.

For each complete tuple-specific key the candidate constructs the strictly allowlisted content-free aggregate result tree from Task 1, computes `canonical_framing_result_digest()`, and emits one immutable `ToolFramingEvidenceRecord(schema_version=1, key=..., validated_at_utc=<canonical whole-second UTC Z>, result_digest=..., gates=<complete exact map>)` inside `ToolFramingEvidenceManifest(schema_version=1, ...)`. Tests assert every field and recompute both the usage-binding and result digests from retained in-memory redacted projections before serialization; raw probe content/signatures are never written. `claude-proxy-probe tools-release` invokes the same candidate runner, collects the complete exact gate set, durably writes the manifest, then reloads through `ToolReleaseEvidence.load(phase0_path, framing_path, proxy_config.usage_mapping_resolver)` and requires every intended tuple-specific usage/framing row before success. The release command rejects SDK, Phase 0 prerequisite, usage-schema, usage-row/mapping, thinking, or usage-binding drift and never copies Phase 3 gates into the Phase 0 record. Phase 0's parallel correlation, SDK cancellation/interrupt/shutdown, argument-delta fidelity, schema immutability, usage shapes/mappings, and 600-second suspension remain prerequisites through the referenced records rather than duplicate Phase 3 gates.

The abrupt-death and stubborn-descendant cases must inspect the durable cleanup journal, supervisor/anchor identities, workdir, and cleanup slot. Accept only confirmed cleanup or the exact fail-closed `cleanup_unconfirmed` state; never signal a reused numeric identity and never report erasure while the child remains unconfirmed.

- [ ] **Step 2: Run the complete Phase 3 framing matrix**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_tool_probe_candidate.py -v`

Expected: candidate admission is available only through the live probe CLI, while normal configuration/router/capability paths remain release-gated.

Run: `RUN_LIVE_CLAUDE_TESTS=1 CLAUDE_PROXY_TEST_MODEL=sonnet uv run pytest --strict-markers --forbid-skips -W error tests/live/test_proxy_tool_loop.py -v -s`

Expected: every dialect-admitted thinking/usage-binding tuple and streaming/non-streaming framing row PASS with no skips: OpenAI null only; Anthropic null plus every admitted enabled tuple; exhaustive thinking/tool ordering/signature, terminal-bundle, tool-boundary/post-result usage mapping, and byte-exact replay evidence all pass.

- [ ] **Step 3: Generate and verify the composed release manifest**

Run: `RUN_LIVE_CLAUDE_TESTS=1 CLAUDE_PROXY_TEST_MODEL=sonnet uv run claude-proxy-probe tools-release --phase0 docs/feasibility/validated-environment.json --output docs/feasibility/validated-tool-release.json`

Expected: a mode-`0600`, content-free framing manifest whose every row references the loaded Phase 0 SDK-record digest plus exact thinking and canonical tool-usage-binding identity, and whose release binding covers that same tuple's exact ordinary/tool-boundary/post-result usage row/mapping digests. The command fails closed unless every required dialect-admitted tuple has both streaming rows and the referenced Phase 0 record—including its already-proved usage shapes/mappings and 600-second SDK suspension gate—is complete and true; it does not rerun or duplicate SDK-level gates as dialect evidence.

- [ ] **Step 4: Add and run the representative harness tool preset tests**

```python
# additions to tests/integration/test_full_history_harness.py
@pytest.mark.anyio
@pytest.mark.parametrize("streaming", [False, True])
async def test_full_history_harness_tool_loop(harness, tool_enabled_server, streaming) -> None:
    result = await harness.run_tool_loop(server=tool_enabled_server, streaming=streaming)
    assert result.completed is True
    assert result.tool_call_ids == result.tool_result_ids
    assert result.received_master_key is False
    assert result.received_derived_run_token is True


@pytest.mark.anyio
async def test_full_history_harness_tool_retry_replays_committed_segment(
    harness, tool_enabled_server
) -> None:
    first, retry = await harness.run_tool_retry(server=tool_enabled_server)
    assert retry.status == 200
    assert retry.body == first.body
    assert retry.sdk_turns_started == first.sdk_turns_started


@pytest.mark.anyio
async def test_full_history_harness_tool_cancellation_is_bounded(
    harness, tool_enabled_server
) -> None:
    result = await harness.cancel_while_waiting_for_tool(server=tool_enabled_server)
    assert result.pending_callbacks == 0
    assert result.cleanup_transfers == 1
    assert result.unowned_resources == ()
```

`tool_enabled_server` is an ordinary server fixture that loads the just-generated `validated-tool-release.json` through `ToolReleaseEvidence.load(phase0_path, framing_path, proxy_config.usage_mapping_resolver)` and resolves the exact request/session `UsageRowBindings`; it never uses `CandidateToolProbe`, its permit, or the low-level builder directly.

Run: `RUN_LIVE_CLAUDE_TESTS=1 CLAUDE_PROXY_TEST_MODEL=sonnet uv run pytest --strict-markers --forbid-skips -W error tests/integration/test_full_history_harness.py -v -k tool -s`

Expected: one full-history harness completes streaming and non-streaming tool loops, retries a committed segment, handles cancellation, and receives only a derived per-run token.

- [ ] **Step 5: Document and verify the release contract**

Document immutable tools, generated SDK MCP naming, the canonical `end_turn`/`tool_use`/`tool_calls` result contract and adapter mappings, the three immutable usage operation rows, per-dialect exhaustive mapping rule, OpenAI checked-total rule, one-shot rejection, exact result subset, all-results-at-once rule, heads/idempotency/replay, in-flight retry behavior, deadlines, terminal causes, cleanup-unconfirmed behavior, dialect differences, and exact composed capability tuple. State that an unrepresentable SDK usage leaf disables only that runtime/model/thinking/operation/dialect tuple; it is never dropped or placed in an undocumented extension. Do not advertise tool support when any required SDK, usage, or framing row is false or skipped.

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_tool_domain.py tests/unit/test_tool_evidence.py tests/unit/test_tool_bridge.py tests/unit/test_tool_actor.py tests/unit/test_tool_probe_candidate.py tests/integration/test_tool_session_creation.py tests/integration/test_anthropic_tools.py tests/integration/test_openai_tools.py tests/integration/test_official_anthropic_client.py tests/integration/test_official_openai_client.py tests/integration/test_tool_diagnostics_persistence.py tests/integration/test_full_history_harness.py -v`

Expected: PASS.

Run: `uv run ruff check . && uv run mypy src/claude_sdk_proxy`

Expected: no findings or errors.

- [ ] **Step 6: Commit the production tool gate**

```bash
git add src/claude_sdk_proxy/probe_cli.py src/claude_sdk_proxy/tool_probe.py tests/unit/test_tool_probe_candidate.py tests/live/test_proxy_tool_loop.py tests/integration/test_full_history_harness.py docs/feasibility/validated-tool-release.json docs/protocol.md docs/harnesses.md
git commit -m "test: verify gated external tool lifecycle"
```
