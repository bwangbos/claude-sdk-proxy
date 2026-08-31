# Phase 4 Gated Multimodal Content and Comparative Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add only empirically faithful base64 image, PDF document, and richer tool-result content for exact validated tuples, then publish redacted comparative diagnostics and a fail-closed final capability matrix.

**Architecture:** A strict content-evidence manifest binds every enabled content type to the exact SDK/CLI, Darwin/APFS, backend/auth/semantic, public-alias/backend-model, dialect, and streaming tuple. Canonical binary blocks enforce transport and decoded-size limits before SDK allocation and map only through documented structured Agent SDK forms. Diagnostics reuse the process-local keyed fingerprinter and versioned path policy; capabilities and parsers consume the same gate objects so advertised support cannot diverge from acceptance.

**Tech Stack:** Python 3.14, uv, AnyIO 4, h11 0.16, Pydantic 2, the pinned Claude Agent SDK/CLI pair, Phase 0 C17 Darwin lifecycle executable, pytest/HTTPX, Ruff, and mypy.

**Spec:** `docs/superpowers/specs/2026-08-29-claude-subscription-api-proxy-design.md`

## Global Constraints

- Phase 0, Phase 1, and Phase 2 must pass. Phase 3 is optional: Tasks 1–3, image/PDF diagnostics, benchmarks, and their capability rows must install and pass without importing any Phase 3 module. Phase 3 is required only for the conditional richer tool-result work in Task 4.
- Inherit the Phase 0 pytest release policy: every mandatory Phase 4 pytest command uses `--strict-markers --forbid-skips -W error`; a skipped or unexecuted async test is failed evidence, never a pass.
- V1 remains Darwin/macOS 14 or newer with a local APFS runtime root. No capability evidence transfers to another OS/build/filesystem tuple.
- `backend_kind` must be `agent_sdk_subscription`, `auth_source` must be `existing_claude_login`, and `semantic_class` must be `prompt_isolated_agent_sdk`.
- Add one content type at a time. A missing, stale, malformed, skipped, or partially passing evidence row is false.
- A public feature is enabled only for the exact public alias and exact backend model ID exercised by its live gate. Moving aliases and model fallback are forbidden.
- Every content row is bound to the exact per-model Phase 0 prerequisite digest, including the validated SDK/CLI, Darwin/build/boot, complete local-APFS mount identity, core gates, backend/auth/semantic tuple, and alias/backend-model pair. Generic filesystem labels never authorize content.
- Every input `image` and `document` row is keyed by one exact dialect-admitted thinking selection even though its SDK/usage/release binding fields remain null. OpenAI admits only the null key; Anthropic admits null plus every implemented enabled key. A public input-kind flag is true only when both modes pass for every key in that dialect's nonempty required set.
- Every `tool_result_*` row is additionally keyed by its exact implemented thinking selection and bound to both the exact Phase 0-derived tool-usage-binding schema/digest and the exact Phase 3 `ToolReleaseBinding` schema/digest for that usage binding, SDK record, dialect, and both streaming modes used by the candidate. That release binding transitively covers the three ordinary/tool-boundary/post-result usage rows and dialect mappings plus the strict Phase 3 framing-manifest and record schema versions, exact keys, canonical UTC validation timestamps, canonical redacted result digests, and complete gate maps; Phase 4 never reconstructs or weakens that projection.
- For each enabled dialect/content pair, streaming and non-streaming must both pass unless capabilities expose two separate mode flags. This plan enables a content pair only when both pass.
- Accept base64 payloads only. Reject remote URLs except allowlisted OpenAI `data:` image URLs, filesystem paths, audio, citations, search results, and unverified block variants.
- Enforce HTTP body, encoded-byte, decoded-byte, per-block, block-count, and aggregate-turn limits before allocating SDK content objects.
- Never convert binary content into filenames, textual descriptions, extracted PDF text, or proxy prompts.
- Binary encoded/decoded bytes, filenames, URLs, PDF text, tool payloads, credentials, and raw SDK frames are never logged, including with `PROXY_DEBUG_CONTENT=1`. The content-debug flag remains an explicit text-only operator opt-in.
- The default diagnostics surface contains only field presence, media type, sizes, event types, lifecycle/latency values, and process-local keyed fingerprints.
- Persistence evidence must prove **no prohibited persistent content and no leaked temporary artifacts**. It must never claim zero filesystem writes.
- Metadata-only credential paths are never opened. Unknown new paths fail the gate pending classification.
- Capability endpoints, startup diagnostics, and release manifests expose backend kind, auth source, semantic class, platform tuple, and public-alias-to-exact-backend-model mappings without credentials or session details.
- The mandatory benchmark target set is exactly `proxy` plus `normal_claude_code`; neither target needs a Platform API key and neither may skip. The raw Anthropic target is isolated under the registered `raw_platform_benchmark` marker in `tests/optional/`, runs only from a separately invoked optional command with an explicitly supplied Platform API key, and has no release or capability authority. That key is never inherited by the Agent SDK/CLI child and never changes v1 backend identity.

## Required Earlier-Phase Interfaces

```python
class PathPolicy:
    def classify(self, path: Path) -> PathClass: ...
    def may_open_content(self, path: Path) -> bool: ...

class CleanupRegistry:
    async def adopt(self, ticket: CleanupTicket) -> None: ...
    async def wait_empty(self) -> None: ...
```

Phase 4 uses the same `FeasibilityManifest`, `ProxyConfig`, `Dialect`, `DiagnosticSink`, `CapacityLedger`, actor, replay, and terminal interfaces validated by earlier phases. Runtime, backend, platform, and exact model identity values come from the manifest and validated config rather than parallel identity types. Core image/PDF modules must not import Phase 3. When Phase 3 is present, Task 1 preserves its already-installed tool variants, Task 4 owns the concrete `ToolBridge` rich-result conversion and `Phase3RichResultCandidateRunner` adapter, Task 5 accepts that adapter only through its local `CandidateRichResultRunner` structural protocol, and Task 8 accepts release evidence only through a local structural protocol keyed by the configured alias/backend-model digest resolver and dialect.

Task 4 alone additionally consumes this Phase 3 interface when it exists:

```python
class ToolBridge(Protocol):
    async def resolve_all(self, results: tuple[ToolResultBlock, ...]) -> None: ...
```

## File Map

- `src/claude_sdk_proxy/content_evidence.py`: exact tuple schema, strict loader, and fail-closed content gates.
- `src/claude_sdk_proxy/domain.py`: canonical image, PDF document, and richer tool-result blocks.
- `src/claude_sdk_proxy/anthropic_adapter.py`: Anthropic image/document/tool-result validation.
- `src/claude_sdk_proxy/openai_adapter.py`: allowlisted OpenAI data-image and independently gated tool-result mapping.
- `src/claude_sdk_proxy/backend.py`: low-level attested structured SDK content conversion shared by production gates and the sealed candidate probe.
- `src/claude_sdk_proxy/content_probe.py`: sealed live-opt-in candidate runner used only to bootstrap content evidence.
- `src/claude_sdk_proxy/probe_cli.py`: explicit candidate content-release command and manifest generation.
- `src/claude_sdk_proxy/tool_bridge.py`: conditional Phase 3 dependency modified only by Task 4 for gated richer MCP result conversion and the concrete Phase 3-backed candidate adapter.
- `src/claude_sdk_proxy/capabilities.py`: shared per-tuple capability assembly.
- `src/claude_sdk_proxy/diagnostics.py`: binary-safe keyed shapes and lifecycle spans.
- `src/claude_sdk_proxy/path_policy.py`: Phase 0 path classification plus state-event capture and safe canary comparison.
- `src/claude_sdk_proxy/benchmarks.py`: local comparative benchmark runner.
- `src/claude_sdk_proxy/server_cli.py`: benchmark command only; it exposes no candidate-content mode.
- `tests/unit/test_content_evidence.py`: strict manifest tests.
- `tests/unit/test_content_probe_candidate.py`: permit sealing, live opt-in, Phase 0 prerequisite, and public-surface exclusion tests.
- `tests/unit/test_multimodal.py`: base64, media, PDF, limit, and canonicalization tests.
- `tests/unit/test_rich_tool_results.py`: conditional Task 4 MCP conversion, result-set atomicity, and exact adapter binding/builder tests.
- `tests/unit/test_diagnostics.py`: binary/tool redaction and keyed-shape tests.
- `tests/unit/test_benchmarks.py`: aggregation and report redaction tests.
- `tests/integration/test_multimodal.py`: public image/PDF fixtures in all supported modes.
- `tests/integration/test_rich_tool_results.py`: conditional Task 4 dialect and actor tool-result loops.
- `tests/integration/test_multimodal_persistence.py`: path-policy and cleanup evidence.
- `tests/integration/test_capabilities.py`: authenticated exact capability projection.
- `tests/live/test_multimodal_backend.py`: real SDK semantic content gates.
- `tests/live/test_content_release.py`: exhaustive sealed candidate content matrix.
- `tests/live/test_rich_tool_results.py`: conditional Task 4 real MCP conversion gates.
- `tests/live/test_benchmarks.py`: comparative smoke tests.
- `tests/optional/test_raw_benchmarks.py`: separately invoked, non-authoritative raw Platform API comparison.
- `tests/live/test_release_matrix.py`: final no-skip advertised-feature matrix.
- `docs/feasibility/validated-content.json`: generated non-secret exact tuple evidence.
- `docs/multimodal.md`: accepted forms, modes, limits, and failures.
- `docs/benchmarks.md`: methodology and interpretation.
- `docs/verification.md`: final commands and release rules.
- `README.md`: final capability and residual-limit summary.

---

### Task 1: Define Exact Content Evidence and Canonical Binary Blocks

**Files:**
- Create: `src/claude_sdk_proxy/content_evidence.py`
- Modify: `src/claude_sdk_proxy/domain.py`
- Modify: `src/claude_sdk_proxy/transcript.py`
- Modify: `src/claude_sdk_proxy/replay.py`
- Create: `docs/feasibility/validated-content.json`
- Create: `tests/unit/test_content_evidence.py`
- Create: `tests/unit/test_multimodal.py`
- Modify: `tests/unit/test_transcript.py`
- Modify: `tests/unit/test_fingerprints.py`
- Modify: `tests/unit/test_replay.py`

**Interfaces:**
- Consumes: Phase 0 `FeasibilityManifest`, `SdkToolEvidenceManifest`, exclusive `sdk_tool_record_digest()`, `canonical_evidence_json()`, `Phase0PrerequisiteDigestResolver`, and exact-budget `UsageTupleKey`; Phase 1 `ProxyConfig`, `UsageMappingResolver`, `UsageRowBindings`, `ImplementedThinkingAllowlist`, exact model mappings, optional structural Phase 3 release bindings, `Dialect`, `CanonicalMessage`, `validate_continuation()`, and `fingerprint_request()`.
- Produces: `ContentKind`, `ToolThinkingKey`, source-snapshot-bound immutable `ModelSdkDigestResolver`, `ToolUsageBindingProjection`, immutable `ToolUsageBindingResolver`, `ToolUsageBindingResolverView`, `ToolReleaseBindingView`, and `ToolReleaseEvidenceView`, prerequisite/usage-bound `ContentEvidenceKey`, `ContentEvidenceRecord`, `ContentEvidenceManifest.load()`, `ContentEvidenceManifest.bind_current()`, `ContentGate.require()`, `BinaryLimits`, `ImageBlock` and `DocumentBlock` implementations of `CanonicalInputBlock`, expanded `CanonicalInputBlock`/`CanonicalAssistantOutputBlock`/`CanonicalTranscriptBlock` aliases and `CanonicalMessage.content` that preserve every Phase 1 thinking and optional Phase 3 tool variant, transcript-prefix comparison, and full-history request fingerprinting.

- [ ] **Step 1: Write failing evidence tests**

```python
# tests/unit/test_content_evidence.py
import pytest

from claude_sdk_proxy.content_evidence import (
    ContentEvidenceKey,
    ContentEvidenceManifest,
    ModelSdkDigestResolver,
    ToolThinkingKey,
)
from claude_sdk_proxy.domain import Dialect, ProxyError
from claude_sdk_proxy.validated import sdk_tool_record_digest


def test_content_gate_requires_exact_runtime_backend_model_and_both_modes(valid_content) -> None:
    no_thinking = ToolThinkingKey(mode="null", budget_tokens=None, effort=None)
    key = ContentEvidenceKey(
        kind="image",
        phase0_prerequisite_schema_version=1,
        phase0_prerequisite_digest="11" * 32,
        thinking=no_thinking,
        tool_usage_binding_schema_version=None,
        tool_usage_binding_digest=None,
        tool_release_schema_version=None,
        tool_release_digest=None,
        public_model="sonnet",
        backend_model_id="claude-sonnet-4-6-20260801",
        dialect=Dialect.ANTHROPIC,
    )
    manifest = ContentEvidenceManifest.model_validate(valid_content)
    assert manifest.supports(key) is True
    manifest.records[key.canonical_key()].streaming = False
    assert manifest.supports(key) is False


@pytest.mark.parametrize("mutation", [
    "phase0_digest", "phase0_schema", "mount_device", "mount_fsid", "mount_flags",
    "runtime_root_st_dev", "sdk", "cli", "os_build", "boot", "backend", "auth",
    "semantic", "alias", "model", "dialect", "thinking", "thinking_budget",
    "thinking_effort", "usage_binding_schema", "usage_binding_digest",
    "release_schema", "release_digest", "probe_digest",
])
def test_stale_or_mismatched_content_evidence_fails_closed(valid_content, mutation) -> None:
    manifest = ContentEvidenceManifest.model_validate(valid_content).mutated_for_test(mutation)
    with pytest.raises(ProxyError, match="content evidence"):
        manifest.validate_for_startup()


def test_stale_or_swapped_phase0_prerequisite_cannot_authorize_content(
    content_manifest, two_model_phase0, tool_usage_bindings
) -> None:
    current = Phase0PrerequisiteDigestResolver.from_manifest(two_model_phase0)
    gate = content_manifest.bind_current(
        phase0=current, model_sdk_digests=None,
        tool_usage_bindings=tool_usage_bindings,
        tool_release=None
    )
    no_thinking = ToolThinkingKey(mode="null", budget_tokens=None, effort=None)
    assert gate.supports_image(
        "sonnet", Dialect.ANTHROPIC, no_thinking
    ) is True

    for changed in (
        two_model_phase0.with_mount_device("disk9s9"),
        two_model_phase0.with_mount_fsid("ffff:eeee"),
        two_model_phase0.with_runtime_root_st_dev(999),
        two_model_phase0.with_sdk_version("0.2.149"),
    ):
        stale = content_manifest.bind_current(
            phase0=Phase0PrerequisiteDigestResolver.from_manifest(changed),
            model_sdk_digests=None,
            tool_usage_bindings=tool_usage_bindings,
            tool_release=None,
        )
        assert stale.supports_image(
            "sonnet", Dialect.ANTHROPIC, no_thinking
        ) is False

    swapped = current.with_swapped_digests_for_test("sonnet", "opus")
    with pytest.raises(ProxyError, match="content prerequisite evidence"):
        content_manifest.bind_current(
            phase0=swapped, model_sdk_digests=None,
            tool_usage_bindings=tool_usage_bindings,
            tool_release=None
        )


def test_input_content_aggregate_is_conservative_across_required_thinking(
    thinking_aware_content_manifest, phase0_resolver, tool_usage_bindings
) -> None:
    no_thinking = ToolThinkingKey(mode="null", budget_tokens=None, effort=None)
    enabled = ToolThinkingKey(mode="enabled", budget_tokens=4096, effort="high")
    gate = thinking_aware_content_manifest.bind_current(
        phase0=phase0_resolver,
        model_sdk_digests=None,
        tool_usage_bindings=tool_usage_bindings,
        tool_release=None,
    )
    assert gate.supports_image("sonnet", Dialect.ANTHROPIC, no_thinking) is True
    assert gate.supports_image("sonnet", Dialect.ANTHROPIC, enabled) is True
    assert gate.image_enabled_for("sonnet", Dialect.ANTHROPIC) is True
    assert gate.supports_document("sonnet", Dialect.ANTHROPIC, enabled) is True
    assert gate.document_enabled_for("sonnet", Dialect.ANTHROPIC) is True

    stale = thinking_aware_content_manifest.with_false_input_row(
        kind="image", dialect=Dialect.ANTHROPIC, thinking=enabled
    ).bind_current(
        phase0=phase0_resolver,
        model_sdk_digests=None,
        tool_usage_bindings=tool_usage_bindings,
        tool_release=None,
    )
    assert stale.supports_image("sonnet", Dialect.ANTHROPIC, no_thinking) is True
    assert stale.supports_image("sonnet", Dialect.ANTHROPIC, enabled) is False
    assert stale.image_enabled_for("sonnet", Dialect.ANTHROPIC) is False

    stale_document = thinking_aware_content_manifest.with_false_input_row(
        kind="document", dialect=Dialect.ANTHROPIC, thinking=enabled
    ).bind_current(
        phase0=phase0_resolver,
        model_sdk_digests=None,
        tool_usage_bindings=tool_usage_bindings,
        tool_release=None,
    )
    assert stale_document.supports_document(
        "sonnet", Dialect.ANTHROPIC, no_thinking
    ) is True
    assert stale_document.document_enabled_for(
        "sonnet", Dialect.ANTHROPIC
    ) is False


def test_rich_result_requires_exact_usage_and_phase3_release_binding(
    rich_content_manifest, phase0_resolver, model_sdk_digests,
    tool_usage_bindings, tool_release
) -> None:
    current = rich_content_manifest.bind_current(
        phase0=phase0_resolver,
        model_sdk_digests=model_sdk_digests,
        tool_usage_bindings=tool_usage_bindings,
        tool_release=tool_release,
    )
    thinking = ToolThinkingKey(mode="null", budget_tokens=None, effort=None)
    assert current.supports_tool_result_image(
        "sonnet", Dialect.ANTHROPIC, thinking
    ) is True
    for changed in (
        tool_release.with_changed_naming_rule_for_test(),
        tool_release.with_changed_nonstream_row_for_test(),
        tool_release.with_changed_stream_row_for_test(),
        tool_release.with_changed_release_schema_for_test(),
    ):
        stale = rich_content_manifest.bind_current(
            phase0=phase0_resolver,
            model_sdk_digests=model_sdk_digests,
            tool_usage_bindings=tool_usage_bindings,
            tool_release=changed,
        )
        assert stale.supports_tool_result_image(
            "sonnet", Dialect.ANTHROPIC, thinking
        ) is False
        assert stale.supports_tool_result_document(
            "sonnet", Dialect.ANTHROPIC, thinking
        ) is False


def test_swapped_tool_release_binding_cannot_cross_authorize_models(
    two_model_rich_manifest, phase0_resolver, model_sdk_digests, tool_usage_bindings,
    two_model_tool_release
) -> None:
    swapped = two_model_tool_release.with_swapped_release_bindings_for_test(
        "sonnet", "opus", Dialect.ANTHROPIC
    )
    with pytest.raises(ProxyError, match="tool-result prerequisite evidence"):
        two_model_rich_manifest.bind_current(
            phase0=phase0_resolver,
            model_sdk_digests=model_sdk_digests,
            tool_usage_bindings=tool_usage_bindings,
            tool_release=swapped,
        )


@pytest.mark.parametrize("case", ["missing", "stale"])
def test_missing_or_stale_usage_binding_disables_exact_rich_tuple(
    rich_content_manifest, phase0_resolver, model_sdk_digests,
    tool_usage_bindings, tool_release, case
) -> None:
    changed = tool_usage_bindings.for_failure_case(case)
    gate = rich_content_manifest.bind_current(
        phase0=phase0_resolver,
        model_sdk_digests=model_sdk_digests,
        tool_usage_bindings=changed,
        tool_release=tool_release,
    )
    thinking = ToolThinkingKey(mode="null", budget_tokens=None, effort=None)
    assert gate.supports_tool_result_image(
        "sonnet", Dialect.ANTHROPIC, thinking
    ) is False


@pytest.mark.parametrize(
    "operation_class", ["ordinary", "tool_use_boundary", "post_tool_result"]
)
def test_each_stale_usage_row_invalidates_matching_content_and_release_binding(
    rich_content_manifest, phase0_resolver, model_sdk_digests,
    tool_usage_bindings, tool_release, operation_class
) -> None:
    changed = tool_usage_bindings.with_changed_row_digest(
        "sonnet", Dialect.ANTHROPIC,
        ToolThinkingKey(mode="null", budget_tokens=None, effort=None),
        operation_class,
    )
    gate = rich_content_manifest.bind_current(
        phase0=phase0_resolver,
        model_sdk_digests=model_sdk_digests,
        tool_usage_bindings=changed,
        tool_release=tool_release,
    )
    assert gate.supports_tool_result_image(
        "sonnet", Dialect.ANTHROPIC,
        ToolThinkingKey(mode="null", budget_tokens=None, effort=None),
    ) is False


def test_stale_dialect_usage_mapping_invalidates_matching_rich_tuple(
    rich_content_manifest, phase0_resolver, model_sdk_digests,
    tool_usage_bindings, tool_release
) -> None:
    changed = tool_usage_bindings.with_changed_mapping_digest(
        "sonnet", Dialect.ANTHROPIC,
        ToolThinkingKey(mode="null", budget_tokens=None, effort=None),
        "tool_use_boundary",
    )
    gate = rich_content_manifest.bind_current(
        phase0=phase0_resolver,
        model_sdk_digests=model_sdk_digests,
        tool_usage_bindings=changed,
        tool_release=tool_release,
    )
    assert gate.supports_tool_result_image(
        "sonnet", Dialect.ANTHROPIC,
        ToolThinkingKey(mode="null", budget_tokens=None, effort=None),
    ) is False


@pytest.mark.parametrize("case", ["swapped_model", "swapped_dialect"])
def test_swapped_usage_binding_resolver_is_rejected(
    rich_content_manifest, phase0_resolver, model_sdk_digests,
    tool_usage_bindings, tool_release, case
) -> None:
    changed = tool_usage_bindings.for_failure_case(case)
    with pytest.raises(ProxyError, match="tool usage binding"):
        rich_content_manifest.bind_current(
            phase0=phase0_resolver,
            model_sdk_digests=model_sdk_digests,
            tool_usage_bindings=changed,
            tool_release=tool_release,
        )


def test_different_thinking_tuple_cannot_authorize_rich_content(
    rich_content_manifest, phase0_resolver, model_sdk_digests,
    tool_usage_bindings, tool_release
) -> None:
    enabled = ToolThinkingKey(mode="enabled", budget_tokens=4096, effort="high")
    no_thinking = ToolThinkingKey(mode="null", budget_tokens=None, effort=None)
    assert rich_content_manifest.has_rich_row(enabled)
    assert rich_content_manifest.has_rich_row(no_thinking)
    changed = tool_usage_bindings.with_swapped_thinking_bindings(enabled, no_thinking)
    with pytest.raises(ProxyError, match="tool usage binding.*thinking"):
        rich_content_manifest.bind_current(
            phase0=phase0_resolver,
            model_sdk_digests=model_sdk_digests,
            tool_usage_bindings=changed,
            tool_release=tool_release,
        )


def test_required_thinking_is_exactly_dialect_specific(
    tool_usage_bindings, backend_model_id
) -> None:
    no_thinking = ToolThinkingKey(mode="null", budget_tokens=None, effort=None)
    enabled = ToolThinkingKey(mode="enabled", budget_tokens=4096, effort="high")

    assert tool_usage_bindings.required_thinking(
        "sonnet", backend_model_id, Dialect.OPENAI
    ) == (no_thinking,)
    assert tool_usage_bindings.required_thinking(
        "sonnet", backend_model_id, Dialect.ANTHROPIC
    ) == (no_thinking, enabled)
    assert enabled not in tool_usage_bindings.required_thinking(
        "sonnet", backend_model_id, Dialect.OPENAI
    )


def test_tool_usage_projection_round_trips_exact_budget_without_class_fallback(
    tool_usage_bindings, backend_model_id
) -> None:
    exact = ToolThinkingKey(mode="enabled", budget_tokens=4096, effort="high")
    projection = tool_usage_bindings.resolve(
        "sonnet", backend_model_id, Dialect.ANTHROPIC, exact
    )
    assert projection.thinking == exact

    with pytest.raises(ProxyError, match="tool usage binding.*thinking"):
        tool_usage_bindings.resolve(
            "sonnet",
            backend_model_id,
            Dialect.ANTHROPIC,
            ToolThinkingKey(mode="enabled", budget_tokens=4097, effort="high"),
        )


def test_model_sdk_digest_resolver_uses_phase0_record_digest_and_source_snapshot(
    two_model_environment
) -> None:
    resolver = ModelSdkDigestResolver.from_phase0(
        feasibility=two_model_environment.feasibility,
        model_map=two_model_environment.model_map,
        phase0_prerequisites=two_model_environment.phase0_prerequisites,
        sdk_manifest=two_model_environment.sdk_manifest,
    )
    for public_alias, backend_model_id in two_model_environment.model_map.by_alias.items():
        record = two_model_environment.sdk_manifest.require_exact(backend_model_id)
        assert resolver.resolve(public_alias, backend_model_id) == (
            sdk_tool_record_digest(record)
        )

    with pytest.raises(ProxyError, match="SDK digest source snapshot"):
        resolver.validate_source(
            feasibility=two_model_environment.feasibility.with_sdk_version("0.2.149"),
            model_map=two_model_environment.model_map,
            phase0_prerequisites=two_model_environment.phase0_prerequisites,
            sdk_manifest=two_model_environment.sdk_manifest,
        )


def test_model_sdk_digest_resolver_rejects_swapped_model_digests(
    two_model_environment
) -> None:
    resolver = ModelSdkDigestResolver.from_phase0(
        feasibility=two_model_environment.feasibility,
        model_map=two_model_environment.model_map,
        phase0_prerequisites=two_model_environment.phase0_prerequisites,
        sdk_manifest=two_model_environment.sdk_manifest,
    ).with_swapped_digests_for_test("sonnet", "opus")
    with pytest.raises(ProxyError, match="SDK record digest.*model tuple"):
        resolver.validate_source(
            feasibility=two_model_environment.feasibility,
            model_map=two_model_environment.model_map,
            phase0_prerequisites=two_model_environment.phase0_prerequisites,
            sdk_manifest=two_model_environment.sdk_manifest,
        )
```

- [ ] **Step 2: Write failing binary validation tests**

```python
# tests/unit/test_multimodal.py
import pytest

from claude_sdk_proxy.domain import BinaryLimits, ImageBlock, ProxyError


def test_image_decodes_strict_base64_once() -> None:
    block = ImageBlock.from_base64(
        media_type="image/png",
        data="iVBORw0KGgo=",
        limits=BinaryLimits(max_encoded_bytes=32, max_decoded_bytes=16),
    )
    assert block.decoded == b"\x89PNG\r\n\x1a\n"
    assert "iVBOR" not in repr(block)


def test_encoded_and_decoded_limits_precede_sdk_allocation(sdk_factory) -> None:
    with pytest.raises(ProxyError, match="image exceeds"):
        ImageBlock.from_base64(
            media_type="image/png",
            data="YWFhYQ==",
            limits=BinaryLimits(max_encoded_bytes=16, max_decoded_bytes=3),
        )
    assert sdk_factory.allocations == 0


def test_multimodal_full_history_continuation_returns_only_new_turn(limits) -> None:
    image = ImageBlock.from_base64(
        media_type="image/png", data="iVBORw0KGgo=", limits=limits
    )
    recorded = (user_message(image), assistant_message("seen"))
    submitted = (*recorded, user_message(TextBlock("next")))
    assert validate_continuation(recorded=recorded, submitted=submitted) == (
        user_message(TextBlock("next")),
    )


def test_changed_binary_prefix_is_stale_and_exact_retry_fingerprint_is_stable(limits, hmac_key) -> None:
    first = ImageBlock.from_base64(
        media_type="image/png", data="iVBORw0KGgo=", limits=limits
    )
    changed = ImageBlock.from_base64(
        media_type="image/png", data="iVBORw0KGgs=", limits=limits
    )
    request = multimodal_request(user_message(first), head="head_1")
    assert fingerprint_request(hmac_key, request) == fingerprint_request(hmac_key, request)
    assert fingerprint_request(hmac_key, request) != fingerprint_request(
        hmac_key, multimodal_request(user_message(changed), head="head_1")
    )
    with pytest.raises(ProxyError, match="history"):
        validate_continuation(
            recorded=(user_message(first), assistant_message("seen")),
            submitted=(
                user_message(changed),
                assistant_message("seen"),
                user_message(TextBlock("next")),
            ),
        )
```

Add explicit cross-phase regressions rather than testing binary blocks only in text-only histories:

```python
# additions to tests/unit/test_transcript.py
from dataclasses import replace


def test_thinking_signatures_and_multimodal_order_survive_full_history(
    image_block, document_block
) -> None:
    recorded = (
        CanonicalMessage("user", (TextBlock("inspect"), image_block)),
        CanonicalMessage("assistant", (
            ThinkingBlock(thinking="visible reasoning", signature="sig-visible"),
            RedactedThinkingBlock(data="opaque-data", signature="sig-redacted"),
            TextBlock("done"),
        )),
    )
    new_turn = CanonicalMessage("user", (document_block, TextBlock("compare")))
    assert validate_continuation(recorded, (*recorded, new_turn)) == (new_turn,)

    changed = replace(
        recorded[1],
        content=(
            replace(recorded[1].content[0], signature="sig-changed"),
            *recorded[1].content[1:],
        ),
    )
    with pytest.raises(ProxyError, match="history"):
        validate_continuation(recorded, (recorded[0], changed, new_turn))


# additions to tests/unit/test_fingerprints.py
from dataclasses import replace


def test_thinking_signature_and_multimodal_block_order_affect_fingerprint(
    process_hmac_key, multimodal_request_factory, image_block, document_block
) -> None:
    blocks = (
        ThinkingBlock(thinking="visible reasoning", signature="sig-visible"),
        RedactedThinkingBlock(data="opaque-data", signature="sig-redacted"),
        TextBlock("done"),
    )
    base = multimodal_request_factory(
        messages=(
            CanonicalMessage("user", (TextBlock("inspect"), image_block, document_block)),
            CanonicalMessage("assistant", blocks),
        )
    )
    reordered = multimodal_request_factory(
        messages=(
            CanonicalMessage("user", (TextBlock("inspect"), document_block, image_block)),
            CanonicalMessage("assistant", blocks),
        )
    )
    changed_signature = multimodal_request_factory(
        messages=(
            base.messages[0],
            replace(
                base.messages[1],
                content=(replace(blocks[0], signature="sig-changed"), *blocks[1:]),
            ),
        )
    )
    assert fingerprint_request(process_hmac_key, base) != fingerprint_request(
        process_hmac_key, reordered
    )
    assert fingerprint_request(process_hmac_key, base) != fingerprint_request(
        process_hmac_key, changed_signature
    )


# additions to tests/unit/test_replay.py
def test_exact_replay_preserves_thinking_signatures_and_multimodal_order(
    replay_harness, multimodal_request_with_thinking
) -> None:
    body = b'exact framed response containing thinking and signature blocks'
    replay_harness.commit(multimodal_request_with_thinking, body=body)
    replay = replay_harness.retry(multimodal_request_with_thinking)
    assert replay.body == body
    assert replay_harness.backend_calls == 1

    for changed in (
        multimodal_request_with_thinking.with_reordered_binary_blocks(),
        multimodal_request_with_thinking.with_changed_thinking_signature(),
        multimodal_request_with_thinking.with_changed_redacted_thinking_signature(),
    ):
        with pytest.raises(ProxyError, match="idempotency|history"):
            replay_harness.retry(changed)
    assert replay_harness.backend_calls == 1
```

Also parameterize image/document media type, decoded length, digest, empty/adjacent text, and mixed histories in `tests/unit/test_transcript.py`; only an exact canonical prefix may return one genuinely new user turn. In `tests/unit/test_fingerprints.py` and `tests/unit/test_replay.py`, prove the full submitted history, immutable configuration, head, dialect, stream flag, parameter policy, and framing headers affect the HMAC. The same canonical request/idempotency key replays exact bytes without invoking the backend, while any binary block/order/digest or thinking/redacted-thinking payload/signature change conflicts or follows stale-prefix handling without selecting a session.

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_content_evidence.py tests/unit/test_multimodal.py tests/unit/test_transcript.py tests/unit/test_fingerprints.py tests/unit/test_replay.py -v`

Expected: FAIL because content evidence and canonical binary types do not exist.

- [ ] **Step 4: Implement the strict manifest and binary domain**

```python
# Core declarations for src/claude_sdk_proxy/content_evidence.py
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, Protocol

from .domain import (
    Dialect,
    ImplementedThinkingAllowlist,
    ThinkingConfig,
    UsageMappingResolver,
    UsageRowBindings,
)
from .validated import sdk_tool_record_digest

ContentKind = Literal["image", "document", "tool_result_image", "tool_result_document"]


@dataclass(frozen=True, slots=True)
class ModelSdkDigestResolver:
    _by_model: Mapping[tuple[str, str], str]
    _phase0_prerequisite_by_model: Mapping[tuple[str, str], str]

    @classmethod
    def from_phase0(
        cls,
        feasibility: FeasibilityManifest,
        model_map: ModelMap,
        phase0_prerequisites: Phase0PrerequisiteDigestResolver,
        sdk_manifest: SdkToolEvidenceManifest,
    ) -> "ModelSdkDigestResolver": ...

    def validate_source(
        self,
        feasibility: FeasibilityManifest,
        model_map: ModelMap,
        phase0_prerequisites: Phase0PrerequisiteDigestResolver,
        sdk_manifest: SdkToolEvidenceManifest,
    ) -> None: ...

    def resolve(self, public_alias: str, exact_backend_model_id: str) -> str: ...


@dataclass(frozen=True, slots=True)
class ToolThinkingKey:
    mode: Literal["null", "enabled"]
    budget_tokens: int | None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None

    @classmethod
    def from_request(
        cls, thinking: ThinkingConfig | None,
        effort: Literal["low", "medium", "high", "xhigh", "max"] | None,
    ) -> "ToolThinkingKey": ...


@dataclass(frozen=True, slots=True)
class ToolUsageBindingProjection:
    schema_version: Literal[1]
    digest: str
    phase0_prerequisite_digest: str
    runtime_digest: str
    public_model: str
    backend_model_id: str
    dialect: Dialect
    thinking: ToolThinkingKey
    usage_rows: UsageRowBindings


class ToolUsageBindingResolverView(Protocol):
    def required_thinking(
        self, public_model: str, backend_model_id: str, dialect: Dialect
    ) -> tuple[ToolThinkingKey, ...]: ...

    def resolve(
        self, public_model: str, backend_model_id: str, dialect: Dialect,
        thinking: ToolThinkingKey,
    ) -> ToolUsageBindingProjection: ...


@dataclass(frozen=True, slots=True)
class ToolUsageBindingResolver:
    _required: Mapping[tuple[str, str, Dialect], tuple[ToolThinkingKey, ...]]
    _by_tuple: Mapping[
        tuple[str, str, Dialect, ToolThinkingKey],
        ToolUsageBindingProjection | None,
    ]

    @classmethod
    def from_phase0(
        cls,
        feasibility: FeasibilityManifest,
        model_map: ModelMap,
        phase0_prerequisites: Phase0PrerequisiteDigestResolver,
        usage_mapping_resolver: UsageMappingResolver,
        thinking_allowlist: ImplementedThinkingAllowlist,
    ) -> "ToolUsageBindingResolver": ...

    def required_thinking(
        self, public_model: str, backend_model_id: str, dialect: Dialect
    ) -> tuple[ToolThinkingKey, ...]: ...

    def resolve(
        self, public_model: str, backend_model_id: str, dialect: Dialect,
        thinking: ToolThinkingKey,
    ) -> ToolUsageBindingProjection: ...


class ToolReleaseBindingView(Protocol):
    @property
    def schema_version(self) -> Literal[1]: ...
    @property
    def digest(self) -> str: ...


class ToolFramingThinkingIdentityView(Protocol):
    @property
    def mode(self) -> Literal["null", "enabled"]: ...
    @property
    def budget_tokens(self) -> int | None: ...
    @property
    def effort(self) -> Literal["low", "medium", "high", "xhigh", "max"] | None: ...


class ResolvedToolUsageBindingView(Protocol):
    @property
    def schema_version(self) -> Literal[1]: ...
    @property
    def digest(self) -> str: ...
    @property
    def thinking(self) -> ToolFramingThinkingIdentityView: ...
    @property
    def usage_rows(self) -> UsageRowBindings: ...


class ToolReleaseEvidenceView(Protocol):
    def resolve_usage_binding(
        self, sdk_record_digest: str, dialect: Dialect,
        usage_rows: UsageRowBindings,
    ) -> ResolvedToolUsageBindingView: ...

    def require_session_tools(
        self, sdk_record_digest: str, dialect: Dialect,
        usage_rows: UsageRowBindings,
    ) -> tuple[object, object]: ...

    def release_binding(
        self, sdk_record_digest: str, dialect: Dialect,
        usage_rows: UsageRowBindings,
    ) -> ToolReleaseBindingView: ...


@dataclass(frozen=True, slots=True)
class ContentEvidenceKey:
    kind: ContentKind
    phase0_prerequisite_schema_version: Literal[1]
    phase0_prerequisite_digest: str
    thinking: ToolThinkingKey
    tool_usage_binding_schema_version: Literal[1] | None
    tool_usage_binding_digest: str | None
    tool_release_schema_version: Literal[1] | None
    tool_release_digest: str | None
    public_model: str
    backend_model_id: str
    dialect: Dialect

    def canonical_key(self) -> str:
        payload = canonical_evidence_json({
            "backend_model_id": self.backend_model_id,
            "dialect": self.dialect.value,
            "kind": self.kind,
            "phase0_prerequisite_digest": self.phase0_prerequisite_digest,
            "phase0_prerequisite_schema_version": self.phase0_prerequisite_schema_version,
            "public_model": self.public_model,
            "thinking": {
                "budget_tokens": self.thinking.budget_tokens,
                "effort": self.thinking.effort,
                "mode": self.thinking.mode,
            },
            "tool_usage_binding_digest": self.tool_usage_binding_digest,
            "tool_usage_binding_schema_version": self.tool_usage_binding_schema_version,
            "tool_release_digest": self.tool_release_digest,
            "tool_release_schema_version": self.tool_release_schema_version,
        })
        return sha256(b"claude-sdk-proxy:content-evidence-key:v1\0" + payload).hexdigest()


class ContentEvidenceManifest:
    def bind_current(
        self,
        phase0: Phase0PrerequisiteDigestResolver,
        model_sdk_digests: ModelSdkDigestResolver | None,
        tool_usage_bindings: ToolUsageBindingResolverView,
        tool_release: ToolReleaseEvidenceView | None,
    ) -> "ContentGate": ...


class ContentGate:
    def require(
        self, kind: ContentKind, public_model: str, backend_model_id: str,
        dialect: Dialect, thinking: ToolThinkingKey,
    ) -> None: ...

    def supports_image(
        self, public_model: str, dialect: Dialect, thinking: ToolThinkingKey
    ) -> bool: ...

    def supports_document(
        self, public_model: str, dialect: Dialect, thinking: ToolThinkingKey
    ) -> bool: ...

    def image_enabled_for(self, public_model: str, dialect: Dialect) -> bool: ...
    def document_enabled_for(self, public_model: str, dialect: Dialect) -> bool: ...

    def supports_tool_result_image(
        self, public_model: str, dialect: Dialect, thinking: ToolThinkingKey
    ) -> bool: ...

    def supports_tool_result_document(
        self, public_model: str, dialect: Dialect, thinking: ToolThinkingKey
    ) -> bool: ...
```

`ModelSdkDigestResolver.from_phase0()` is implemented in Task 1 before any candidate consumes it. It calls Phase 0's `sdk_tool_record_digest(record)` for each exact configured alias/backend-model record and never re-encodes or re-hashes an SDK record locally. It stores immutable copies of the exact model-to-record-digest map and the corresponding Phase 0 prerequisite-digest map. `validate_source()` requires the current manifest, model map, prerequisite resolver, SDK manifest key set, exact record identities, and helper-returned record digests to equal that captured source; reject missing/extra/swapped/reused records, cross-snapshot objects, duplicate backend ambiguity, or fallback. Task 5 and Task 8 receive this same Task 1 object rather than constructing another resolver.

Each evidence record contains this complete key, separate `streaming` and `nonstreaming` booleans, UTC validation time, canonical redacted probe-result SHA-256, and exact content-test schema version. `ToolThinkingKey` accepts only `(mode="null", budget_tokens=None, effort=None)` or `(mode="enabled", positive exact budget_tokens, implemented effort-or-None)`. Input image/document keys require an exact non-null dialect-admitted `thinking` key and require all four tool-usage/release binding fields to be `None`. Every rich-result key also requires a non-null exact thinking selection; a rich row with either mode true additionally requires `tool_usage_binding_schema_version == 1`, `tool_release_schema_version == 1`, and both canonical digests. An explicit all-false row for an unresolved/absent Phase 3 tuple retains its exact thinking key but may set both binding pairs null and remains ineligible until regenerated by the sealed candidate. `load()` rejects unknown fields/schema versions, duplicate canonical keys, malformed or half-present bindings, inconsistent kind/thinking/binding combinations, true modes without evidence/bindings, and initial checked-in capabilities other than false.

`bind_current(phase0, model_sdk_digests, tool_usage_bindings, tool_release)` creates the only usable immutable `ContentGate`; `tool_usage_bindings` is mandatory because it owns the exact dialect-admitted thinking set even when Phase 3 is absent. For every row, resolve the current Phase 0 prerequisite digest, require exact equality, and require the row's thinking key to occur exactly once in `tool_usage_bindings.required_thinking(public_model, backend_model_id, dialect)`. OpenAI therefore admits only null/no-thinking input evidence, while Anthropic admits null plus every exact enabled tuple in the allowlist. Input image/document rows keep usage/release bindings null and may pass `model_sdk_digests=None` and `tool_release=None`.

For each rich-result row, additionally require `model_sdk_digests` and `tool_release`, resolve that model's exact SDK-record digest, and resolve the exact `(public model, backend model, dialect, thinking)` `ToolUsageBindingProjection`. Require the projection's prerequisite/schema/digest to equal the content key, call `tool_release.resolve_usage_binding(sdk_digest, dialect, projection.usage_rows)`, and require the returned rows to equal the projection's immutable rows and its framing-thinking identity to equal the exact usage-row identity corresponding to `projection.thinking`; OpenAI must resolve null only. Pass those same rows into `require_session_tools()` and `release_binding()` and require exact release schema/digest equality. Thus the stored release digest and Phase 4 usage-binding digest always describe the same Phase 0 snapshot, request thinking tuple, three usage rows/mappings, and tuple-specific Phase 3 framing rows. A stale or missing expected row/binding makes that exact tuple false with a content-free reason; malformed, swapped, cross-snapshot, duplicate, or cross-model/dialect/thinking resolver data rejects startup. There is no generic APFS, same-model-family, nearest-thinking, newest-record, or single-record fallback.

`image_enabled_for()` and `document_enabled_for()` apply conservative all-required-tuples semantics independently: each aggregate is true only when the required thinking set is nonempty and every exact thinking row for that input kind has both streaming modes true. Public image/PDF parsing first checks that shared aggregate and then requires the exact request `ToolThinkingKey`; if any Anthropic enabled row is false, image/document input is rejected for every Anthropic thinking selection, while OpenAI remains governed only by its null row. If configuration marks a content feature required, a false aggregate rejects startup before listener creation.

Refreshing Phase 0 invalidates every older content row because its prerequisite digest changes. Changing the admitted thinking set invalidates the input and rich matrices until every newly required row is generated. Refreshing a usage row/mapping changes its canonical tool-usage binding and the Phase 3 release binding, invalidating the corresponding thinking-specific `tool_result_*` rows. Refreshing other Phase 3 evidence invalidates rows whose release binding changes. A current image/PDF row cannot authorize a rich tool result, one thinking tuple cannot authorize another, and a current tool binding cannot rescue a stale Phase 0 or usage-binding digest. Candidate Task 5 must regenerate the complete affected matrix; neither loader nor capability assembly carries a prior true row forward.

`ToolUsageBindingResolver.from_phase0()` deterministically defines a separate required thinking set for each exact model/dialect. For `Dialect.OPENAI`, the set is exactly `(ToolThinkingKey(mode="null", budget_tokens=None, effort=None),)` because Phase 2 rejects OpenAI reasoning input; Anthropic allowlist entries must never be copied into the OpenAI set. For `Dialect.ANTHROPIC`, the set is no-thinking plus every exact enabled `ThinkingTuple` admitted by the shared `ImplementedThinkingAllowlist`, sorted with no-thinking first and then by exact budget and effort. Reject unknown dialects. For each dialect-valid required selection, use the same Phase 1 `UsageMappingResolver` to require exact `ordinary`, `tool_use_boundary`, and `post_tool_result` rows and their passing mapping for that dialect; every resolved Phase 0 `UsageTupleKey.budget_tokens` must equal the exact `ToolThinkingKey.budget_tokens`. Phase 4 never derives or parses a budget class or `tokens:N` surrogate. A structurally valid but unsupported dialect-valid selection remains in `_required` with `None` in `_by_tuple`, so aggregate tools fail closed without blocking unrelated text/image service; do not synthesize impossible OpenAI enabled-thinking entries. A resolved projection contains the exact three-row `UsageRowBindings`. Its digest is `sha256(b"claude-sdk-proxy:tool-usage-binding:v1\0" + canonical_evidence_json(projection)).hexdigest()`, where the projection contains schema version, source Phase 0 prerequisite/runtime digests, public alias, exact backend model, dialect, exact `ToolThinkingKey`, and—ordered ordinary/boundary/post-result—each complete canonical usage row, row digest, and passing dialect mapping. Reject unknown/duplicate selections, missing required-set entries, malformed resolved projections, cross-model/dialect/thinking rows, swapped digests, cross-Phase0 snapshots, and nearest/default/single-row fallback. Copy both maps and nested bindings immutably.

`ImageBlock` and `DocumentBlock` are frozen Pydantic models with private decoded bytes, media type, decoded length, and a canonical SHA-256 content digest used only for canonical transcript/fingerprint identity. Do not include bytes in dumps/repr. Enforce encoded length before strict base64 decode, decoded length immediately after decode, per-turn aggregate limits before backend construction, and a `%PDF-` signature for documents.

Extend the aliases already owned by Phase 1; do not redeclare `CanonicalMessage` from a text/tool-only union. The Phase 4 composition is:

```python
# Phase 4 extensions in src/claude_sdk_proxy/domain.py
CanonicalInputBlock: TypeAlias = TextBlock | ImageBlock | DocumentBlock
CanonicalAssistantOutputBlock: TypeAlias = TextBlock | ThinkingBlock | RedactedThinkingBlock
CanonicalTranscriptBlock: TypeAlias = CanonicalInputBlock | CanonicalAssistantOutputBlock

# When Phase 3 is selected, compose its variants into the same aliases:
# CanonicalInputBlock additionally includes ToolResultBlock.
# CanonicalAssistantOutputBlock additionally includes ToolUseBlock.
# CanonicalTranscriptBlock is still the union of those two aliases.
```

`CanonicalMessage.content` remains `tuple[CanonicalTranscriptBlock, ...]`. Thinking selection remains on the immutable `CanonicalRequest`/session configuration rather than inside `ImageBlock` or `DocumentBlock`; before admitting either binary alias, adapters derive `ToolThinkingKey.from_request(request.thinking, request.effort)` and pass it to the aggregate and exact content gates. Preserve the Phase 1 role invariant that thinking and redacted-thinking are assistant-only, including their exact payloads, optional redacted-thinking signature, required thinking signature, and block order. Images and documents are user-input blocks. When Phase 3 is selected, preserve its role invariants and exact `ToolUseBlock`/`ToolResultBlock` variants as well; Phase 4 may add rich content inside a tool result in Task 4 but must not replace either tool block type.

The canonical transcript/replay representation preserves every block in order. It encodes binary blocks only as `(kind, media_type, decoded_length, content_digest)`, while retaining thinking/redacted-thinking kind, payload, and signature exactly and retaining the Phase 3 tool identity/content fields exactly. It never serializes decoded or base64 bytes into logs, errors, tombstones, or replay metadata. Update `validate_continuation()` to compare this representation for every resent full-history block and return only the new user turn. Update `fingerprint_request()` and replay conflict lookup to cover the complete multimodal canonical history and immutable session configuration exactly as for text, thinking, and tools.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_content_evidence.py tests/unit/test_multimodal.py tests/unit/test_transcript.py tests/unit/test_fingerprints.py tests/unit/test_replay.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/content_evidence.py src/claude_sdk_proxy/domain.py src/claude_sdk_proxy/transcript.py src/claude_sdk_proxy/replay.py docs/feasibility/validated-content.json tests/unit/test_content_evidence.py tests/unit/test_multimodal.py tests/unit/test_transcript.py tests/unit/test_fingerprints.py tests/unit/test_replay.py
git commit -m "feat: define exact multimodal evidence contract"
```

---

### Task 2: Add Gated Base64 Image Inputs

**Files:**
- Modify: `src/claude_sdk_proxy/anthropic_adapter.py`
- Modify: `src/claude_sdk_proxy/openai_adapter.py`
- Modify: `src/claude_sdk_proxy/backend.py`
- Create: `tests/integration/test_multimodal.py`
- Create: `tests/live/test_multimodal_backend.py`

**Interfaces:**
- Consumes: `ContentGate`, `ImageBlock`, canonical request/session fingerprints, `PreparedBackend.start_turn()`, and `BackendOperation.events()`.
- Produces: gated Anthropic base64 image parsing, allowlisted OpenAI data-image parsing, and image support in the low-level `AttestedContentMapper` shared with Task 5's sealed candidate.

- [ ] **Step 1: Write failing public-mode and rejection tests**

```python
# tests/integration/test_multimodal.py
@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["one_shot", "automatic", "explicit"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("thinking", [None, {"type": "enabled", "budget_tokens": 4096}])
async def test_anthropic_image_supported_in_each_validated_mode_and_thinking(
    multimodal_client, mode, stream, thinking
) -> None:
    response = await multimodal_client.send_image(
        mode=mode, stream=stream, dialect="anthropic", thinking=thinking
    )
    assert response.status_code == 200
    assert response.backend_block_types == ("image",)
    assert response.backend_model_id == multimodal_client.exact_backend_model_id


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["https://example.test/a.png", "file:///tmp/a.png", "data:image/svg+xml;base64,PHN2Zz4="])
async def test_unverified_image_sources_reject_before_allocation(multimodal_client, source) -> None:
    response = await multimodal_client.send_openai_image_url(source)
    assert response.status_code == 400
    assert multimodal_client.lifecycle_reservations == 0
    assert multimodal_client.sdk_allocations == 0
```

- [ ] **Step 2: Write the live semantic and negative-control gate**

```python
# tests/live/test_multimodal_backend.py
@pytest.mark.live
@pytest.mark.anyio
@pytest.mark.parametrize("dialect", ["anthropic", "openai"])
@pytest.mark.parametrize("stream", [False, True])
async def test_image_semantic_delivery(
    candidate_content_probe, tool_usage_bindings, backend_model_id, dialect, stream
) -> None:
    cases = tuple(
        case for case in candidate_content_probe.required_cases()
        if case.key.kind == "image"
        and case.key.dialect.value == dialect
        and case.streaming is stream
    )
    assert {case.key.thinking for case in cases} == set(
        tool_usage_bindings.required_thinking(
            "sonnet", backend_model_id, Dialect(dialect)
        )
    )
    for case in cases:
        result = await candidate_content_probe.run(case)
        assert result.positive_token_seen is True
        assert result.dominant_color == "red"
        assert result.negative_token_seen is False
        assert result.omitted_mapper_control_failed is True
        assert result.model_identity_valid is True
        assert result.event_usage_stop_valid is True
```

- [ ] **Step 3: Run non-live tests to verify they fail**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/integration/test_multimodal.py -v -k image`

Expected: FAIL because image parsing/mapping is disabled.

- [ ] **Step 4: Implement exact dialect and SDK mapping**

Anthropic accepts only `{type:"image", source:{type:"base64", media_type, data}}`. OpenAI accepts only allowlisted `data:image/png|jpeg|gif|webp;base64,` URLs. Before decoding or lifecycle reservation, derive the exact request `ToolThinkingKey`, require `ContentGate.image_enabled_for(public_model, dialect)`, then call `ContentGate.require("image", public_model, exact_backend_model_id, dialect, thinking_key)`. This makes parser admission equal the conservative capability aggregate while still requiring the exact row. Convert `ImageBlock` through `AttestedContentMapper`, which requires the already-validated Phase 0 runtime/model record and positive child attestation before returning the pinned SDK asynchronous raw image block; never create a path or text substitute. Production adapters reach this mapper only after both content checks. Task 5's sealed candidate is the only caller allowed to reach it without an already-true content row. Include the canonical digest and immutable thinking configuration in request/transcript/idempotency fingerprints but not diagnostics. Drop duplicate encoded storage after successful canonicalization.

```python
# Low-level interface in src/claude_sdk_proxy/backend.py
class AttestedContentMapper:
    async def map_input(
        self,
        block: ImageBlock | DocumentBlock,
        *,
        phase0: FeasibilityManifest,
        exact_backend_model_id: str,
        attestation_gate: AttestationGate,
    ) -> SdkInputBlock: ...
```

`map_input()` calls `require_core_gates(phase0)`, checks that the current runtime and `exact_backend_model_id` match the manifest's exact tuple/map, calls `attestation_gate.revalidate()` immediately before mapping, and performs no content-evidence lookup itself. Production callers own the preceding `ContentGate.require()`; only Task 5's sealed candidate may call this low-level mapper while the content row is still false.

- [ ] **Step 5: Run non-live tests and commit**

The live test remains failing/opt-in until Task 5 implements `CandidateContentProbe`; no normal server mode may bootstrap its own content evidence.

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_multimodal.py tests/unit/test_content_evidence.py tests/integration/test_multimodal.py -v -k image`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/anthropic_adapter.py src/claude_sdk_proxy/openai_adapter.py src/claude_sdk_proxy/backend.py tests/integration/test_multimodal.py tests/live/test_multimodal_backend.py
git commit -m "feat: add gated base64 image inputs"
```

---

### Task 3: Add Gated Anthropic PDF Inputs

**Files:**
- Modify: `src/claude_sdk_proxy/anthropic_adapter.py`
- Modify: `src/claude_sdk_proxy/backend.py`
- Modify: `tests/unit/test_multimodal.py`
- Modify: `tests/integration/test_multimodal.py`
- Modify: `tests/live/test_multimodal_backend.py`

**Interfaces:**
- Consumes: `ContentGate`, `DocumentBlock`, and structured backend mapping.
- Produces: Anthropic-only base64 PDF parsing/mapping and document support in the low-level `AttestedContentMapper` shared with Task 5's sealed candidate.

- [ ] **Step 1: Write failing PDF validation and mode tests**

```python
# additions to tests/unit/test_multimodal.py
def test_document_requires_pdf_media_and_signature(document_factory) -> None:
    with pytest.raises(ProxyError, match="PDF signature"):
        document_factory(media_type="application/pdf", decoded=b"not-pdf")
    with pytest.raises(ProxyError, match="media type"):
        document_factory(media_type="text/plain", decoded=b"%PDF-1.7")


# additions to tests/integration/test_multimodal.py
@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["one_shot", "automatic", "explicit"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("thinking", [None, {"type": "enabled", "budget_tokens": 4096}])
async def test_anthropic_pdf_supported_in_each_validated_mode_and_thinking(
    multimodal_client, mode, stream, thinking
) -> None:
    response = await multimodal_client.send_pdf(
        mode=mode, stream=stream, thinking=thinking
    )
    assert response.status_code == 200
    assert response.backend_block_types == ("document",)
```

- [ ] **Step 2: Add the live PDF semantic gate**

```python
# additions to tests/live/test_multimodal_backend.py
@pytest.mark.live
@pytest.mark.anyio
@pytest.mark.parametrize("stream", [False, True])
async def test_pdf_semantic_delivery(
    candidate_content_probe, tool_usage_bindings, backend_model_id, stream
) -> None:
    cases = tuple(
        case for case in candidate_content_probe.required_cases()
        if case.key.kind == "document"
        and case.key.dialect is Dialect.ANTHROPIC
        and case.streaming is stream
    )
    assert {case.key.thinking for case in cases} == set(
        tool_usage_bindings.required_thinking(
            "sonnet", backend_model_id, Dialect.ANTHROPIC
        )
    )
    for case in cases:
        result = await candidate_content_probe.run(case)
        assert result.positive_token_seen is True
        assert result.negative_token_seen is False
        assert result.omitted_mapper_control_failed is True
        assert result.model_identity_valid is True
        assert result.event_usage_stop_valid is True
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_multimodal.py tests/integration/test_multimodal.py -v -k document`

Expected: FAIL because PDF parsing/mapping is disabled.

- [ ] **Step 4: Implement direct structured PDF mapping**

Require `application/pdf`, strict base64, `%PDF-`, and all configured limits. Reject URL, plaintext, custom source, OpenAI document input, title/context metadata not exactly preserved by the pinned SDK, and every unsupported document variant before reservation. Derive the exact request thinking key, require the conservative `document_enabled_for()` aggregate, then require the exact document/thinking row before decoding. Extend `AttestedContentMapper` to map bytes directly into the documented SDK raw document block after the same Phase 0 record and child-attestation checks used for images. The Task 5 candidate is the sole bootstrap caller that may reach the mapper before a true row exists. Do not extract text, create a temporary PDF, invoke a filesystem tool, or approximate support in OpenAI.

- [ ] **Step 5: Run non-live tests and commit**

The Task 5 candidate executes the live PDF matrix and is the only writer of true document rows.

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_multimodal.py tests/integration/test_multimodal.py -v -k document`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/anthropic_adapter.py src/claude_sdk_proxy/backend.py tests/unit/test_multimodal.py tests/integration/test_multimodal.py tests/live/test_multimodal_backend.py
git commit -m "feat: add gated Anthropic PDF inputs"
```

---

### Task 4 (Conditional on Phase 3): Add Independently Gated Rich Tool Results

**Files:**
- Modify: `src/claude_sdk_proxy/domain.py`
- Modify: `src/claude_sdk_proxy/tool_bridge.py` (Task 4 owns rich-result conversion and the concrete `Phase3RichResultCandidateRunner` adapter)
- Modify: `src/claude_sdk_proxy/anthropic_adapter.py`
- Modify: `src/claude_sdk_proxy/openai_adapter.py`
- Create: `tests/unit/test_rich_tool_results.py`
- Create: `tests/integration/test_rich_tool_results.py`
- Create: `tests/live/test_rich_tool_results.py`

**Interfaces:**
- Consumes: installed and passing Phase 3 concrete `ToolReleaseEvidence`, `ToolReleaseBinding`, `AttestedToolBridgeBuilder`, and `ToolBridge.resolve_all()`, plus `ContentGate`, `ImageBlock`, and `DocumentBlock`.
- Produces: `ToolResultText`, `ToolResultImage`, `ToolResultDocument`, `ToolResultContent`, `mcp_tool_result()`, independent dialect/content gates, and Task 4-owned concrete `Phase3RichResultCandidateRunner` structurally compatible with Task 5's local protocol.

If Phase 3 was not implemented, skip this entire task: do not import or modify `tool_bridge.py`, do not create/run the rich-result tests, leave every `tool_result_*` evidence row false, and continue with Task 5's image/PDF-only candidate matrix. This skip is an intentional phase selection, not passing rich-result evidence.

- [ ] **Step 1: Write failing atomic conversion tests**

```python
# tests/unit/test_rich_tool_results.py
import pytest

from claude_sdk_proxy.domain import ProxyError
from claude_sdk_proxy.tool_bridge import Phase3RichResultCandidateRunner


@pytest.mark.anyio
async def test_rich_result_set_converts_completely_before_any_callback_resolves(rich_bridge) -> None:
    rich_bridge.inject_conversion_failure(public_id="toolu_2")
    with pytest.raises(ProxyError, match="unsupported tool result"):
        await rich_bridge.resolve_all(rich_bridge.two_result_set())
    assert rich_bridge.resolved_count == 0


def test_openai_rich_results_remain_disabled_without_exact_row(content_gate) -> None:
    with pytest.raises(ProxyError, match="unsupported content"):
        content_gate.require_tool_result(kind="image", dialect="openai")


@pytest.mark.anyio
async def test_phase3_rich_candidate_uses_exact_sdk_record_and_validated_binding(
    phase0_manifest, tool_release, attested_tool_bridge_builder,
    attestation_gate, candidate_rich_case
) -> None:
    sdk_record_digest = candidate_rich_case.sdk_record_digest
    usage_rows = candidate_rich_case.usage_rows
    binding = tool_release.release_binding(
        sdk_record_digest, candidate_rich_case.dialect, usage_rows
    )
    runner = Phase3RichResultCandidateRunner(
        evidence=tool_release,
        builder=attested_tool_bridge_builder,
        attestation_gate=attestation_gate,
        definitions=candidate_rich_case.definitions,
    )

    await runner.run_candidate_rich_result(
        candidate_rich_case.content_case,
        phase0=phase0_manifest,
        sdk_record_digest=sdk_record_digest,
        dialect=candidate_rich_case.dialect,
        usage_rows=usage_rows,
        release_binding=binding,
    )

    assert attested_tool_bridge_builder.sdk_evidence_calls == (
        tool_release.require_sdk_record(sdk_record_digest),
    )
    assert attested_tool_bridge_builder.bridge.resolve_all_calls == (
        candidate_rich_case.tool_results,
    )
    assert tool_release.release_binding(
        sdk_record_digest, candidate_rich_case.dialect, usage_rows
    ) == binding
    assert runner.call_order == (
        "release_binding",
        "require_sdk_record",
        "builder.create",
        "release_binding",
        "bridge.resolve_all",
        "release_binding",
    )


@pytest.mark.anyio
async def test_phase3_rich_candidate_rejects_changed_release_binding(
    phase3_rich_result_runner, candidate_rich_case, tool_release
) -> None:
    binding = tool_release.release_binding(
        candidate_rich_case.sdk_record_digest,
        candidate_rich_case.dialect,
        candidate_rich_case.usage_rows,
    )
    phase3_rich_result_runner.change_binding_after_bridge_create()
    with pytest.raises(ProxyError, match="tool release binding changed"):
        await phase3_rich_result_runner.run_candidate_rich_result(
            candidate_rich_case.content_case,
            phase0=candidate_rich_case.phase0,
            sdk_record_digest=candidate_rich_case.sdk_record_digest,
            dialect=candidate_rich_case.dialect,
            usage_rows=candidate_rich_case.usage_rows,
            release_binding=binding,
        )
    assert phase3_rich_result_runner.bridge.resolve_all_calls == ()
    assert phase3_rich_result_runner.cleanup_transfers == 1
```

- [ ] **Step 2: Write the live MCP conversion matrix**

```python
# tests/live/test_rich_tool_results.py
from claude_sdk_proxy.domain import Dialect


@pytest.mark.live
@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["tool_result_image", "tool_result_document"])
@pytest.mark.parametrize("stream", [False, True])
async def test_rich_tool_result_round_trip(candidate_content_probe, kind, stream) -> None:
    case = next(
        case for case in candidate_content_probe.required_cases()
        if case.key.kind == kind
        and case.key.dialect is Dialect.ANTHROPIC
        and case.streaming is stream
    )
    result = await candidate_content_probe.run(case)
    assert result.callback_correlated is True
    assert result.semantic_canary_seen is True
    assert result.omitted_mapper_control_failed is True
    assert result.later_user_turn_succeeded is True
    assert result.event_usage_stop_valid is True
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_rich_tool_results.py tests/integration/test_rich_tool_results.py -v`

Expected: FAIL because richer result types and MCP conversion gates are absent.

- [ ] **Step 4: Implement bounded rich-result conversion**

Define a discriminated immutable result-content union. Validate every pending result and convert every block to the exact pinned MCP return shape before assigning or signaling any callback. Keep Phase 3 text/error behavior unchanged. Enable Anthropic image/PDF results only for exact true rows. Enable OpenAI rich results only if separate OpenAI rows prove a public representation, exact callback conversion, replay, streaming, and error semantics; otherwise reject them without resolving callbacks.

Extend only the existing Phase 3 `ToolBridge.resolve_all(results)` atomic path to accept the richer discriminated result union; do not add a rich-only or second callback-resolution path. Task 4 owns `Phase3RichResultCandidateRunner` in `tool_bridge.py`. It is constructed with the exact concrete Phase 3 `ToolReleaseEvidence`, `AttestedToolBridgeBuilder`, candidate-only `AttestationGate`, and deterministic candidate tool definitions. Its `release_binding(sdk_record_digest, dialect, usage_rows)` delegates directly to `ToolReleaseEvidence.release_binding()` with those three explicit arguments. Its candidate method is:

```python
@dataclass(frozen=True, slots=True)
class Phase3RichResultCandidateRunner:
    evidence: ToolReleaseEvidence
    builder: AttestedToolBridgeBuilder
    attestation_gate: AttestationGate
    definitions: tuple[ToolDefinition, ...]

    def release_binding(
        self, sdk_record_digest: str, dialect: Dialect,
        usage_rows: UsageRowBindings,
    ) -> ToolReleaseBinding: ...

    async def run_candidate_rich_result(
        self,
        case: ContentProbeCase,
        *,
        phase0: FeasibilityManifest,
        sdk_record_digest: str,
        dialect: Dialect,
        usage_rows: UsageRowBindings,
        release_binding: ToolReleaseBindingView,
    ) -> ContentProbeResult: ...
```

Use postponed annotations and `TYPE_CHECKING`-only imports for Task 5's `ContentProbeCase` and `ContentProbeResult`; `tool_bridge.py` must not import `content_probe.py` at runtime. Task 4's unit tests use structural stub case/result objects, while Task 5 owns the concrete types and verifies that this class satisfies its local protocol.

The concrete method annotates `release_binding` with Task 1's structural `ToolReleaseBindingView` so it exactly satisfies Task 5's local protocol, but it immediately requires the runtime object to be the concrete Phase 3 `ToolReleaseBinding`; a lookalike implementation is rejected. Before allocation, recompute `ToolReleaseEvidence.release_binding(sdk_record_digest, dialect, usage_rows)` and require exact dataclass equality with that supplied validated binding. Resolve `sdk_evidence = ToolReleaseEvidence.require_sdk_record(sdk_record_digest)` and pass that exact object—not a lookup by alias/model or a representative record—to `AttestedToolBridgeBuilder.create(..., sdk_evidence=sdk_evidence)`. Immediately after builder creation and before callback resolution, recompute the same release binding and reject drift. Then run the production mapper/event/actor path, convert the complete result set, and call only `bridge.resolve_all(results)`. After the probe and before returning, recompute the release binding a final time from the same three inputs and require equality with the supplied binding. A nonconcrete/missing/changed binding, missing digest, mismatched dialect/usage rows, builder identity mismatch, or callback conversion failure emits a failed candidate result and transfers cleanup ownership; it never resolves callbacks after detected drift or produces reusable evidence.

Before validating or converting any rich result, derive `ToolThinkingKey.from_request(session.thinking, session.effort)` and call `ContentGate.require(tool_result_kind, public_model, exact_backend_model_id, dialect, thinking_key)`. The gate must match that exact key's current usage-binding and usage-bound Phase 3 release digests. The conservative model/dialect rich-result aggregate must also be true; if one required thinking tuple is stale, every public rich-result parser rejects before callback resolution even when another tuple's row remains current.

- [ ] **Step 5: Run non-live tests and commit**

Task 5 conditionally executes the rich-result live rows through `CandidateContentProbe`; no production tool parser or bridge may self-enable them.

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_rich_tool_results.py tests/integration/test_rich_tool_results.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/domain.py src/claude_sdk_proxy/tool_bridge.py src/claude_sdk_proxy/anthropic_adapter.py src/claude_sdk_proxy/openai_adapter.py tests/unit/test_rich_tool_results.py tests/integration/test_rich_tool_results.py tests/live/test_rich_tool_results.py
git commit -m "feat: add gated rich tool results"
```

---

### Task 5: Generate Content Evidence Through a Sealed Candidate Probe

**Files:**
- Create: `src/claude_sdk_proxy/content_probe.py`
- Modify: `src/claude_sdk_proxy/backend.py`
- Modify: `src/claude_sdk_proxy/probe_cli.py`
- Create: `tests/unit/test_content_probe_candidate.py`
- Create: `tests/live/test_content_release.py`
- Modify: `tests/live/test_multimodal_backend.py`
- Modify if Phase 3 is selected: `tests/live/test_rich_tool_results.py`
- Modify: `docs/feasibility/README.md`
- Modify: `docs/feasibility/validated-content.json`

**Interfaces:**
- Consumes: Phase 0 `require_core_gates()`, its exact alias/backend-model map, usage schema and runtime records, Task 1 source-snapshot-bound immutable `ModelSdkDigestResolver` and `ToolUsageBindingResolverView`, Phase 1 implemented thinking selections/`UsageMappingResolver`, `AttestationGate`, Tasks 2–3 `AttestedContentMapper`, and optional Task 4 `Phase3RichResultCandidateRunner` accepted only through the local structural protocol.
- Produces: module-private `_CandidateContentPermit`, local `CandidateRichResultRunner` protocol, sealed `CandidateContentProbe.create()`, immutable thinking/usage/release-bound `ContentProbeCase`, exact immutable `ContentProbeResult`, `claude-proxy-probe content-release`, and a complete reloaded `ContentEvidenceManifest` covering image, PDF, and conditionally available rich tool-result rows.

- [ ] **Step 1: Write candidate sealing and normal-gate tests**

```python
# tests/unit/test_content_probe_candidate.py
from dataclasses import FrozenInstanceError
import json
import pickle

import pytest

import claude_sdk_proxy.content_probe as content_probe
from claude_sdk_proxy.config import ProxyConfig
from claude_sdk_proxy.content_evidence import ContentEvidenceManifest, ToolThinkingKey
from claude_sdk_proxy.content_probe import (
    CandidateContentProbe,
    ProbeCandidateError,
    ToolReleaseBindingSnapshot,
)
from claude_sdk_proxy.domain import Dialect


def test_candidate_requires_exact_live_opt_in_and_passing_phase0_core(
    monkeypatch, phase0_manifest, attested_content_mapper, model_sdk_digests,
    tool_usage_bindings
) -> None:
    monkeypatch.delenv("RUN_LIVE_CLAUDE_TESTS", raising=False)
    with pytest.raises(ProbeCandidateError, match="live opt-in"):
        CandidateContentProbe.create(
            phase0_manifest, attested_content_mapper, model_sdk_digests,
            tool_usage_bindings,
        )

    monkeypatch.setenv("RUN_LIVE_CLAUDE_TESTS", "1")
    with pytest.raises(ProbeCandidateError, match="Phase 0 core evidence"):
        CandidateContentProbe.create(
            phase0_manifest.with_failed_core_gate(), attested_content_mapper,
            model_sdk_digests,
            tool_usage_bindings,
        )


def test_candidate_permit_is_private_and_nonserializable(candidate_content_probe) -> None:
    assert not hasattr(content_probe, "CandidateContentPermit")
    permit = candidate_content_probe._permit_for_test()
    with pytest.raises(TypeError, match="candidate permit"):
        pickle.dumps(permit)
    with pytest.raises(TypeError, match="not JSON serializable"):
        json.dumps(permit)


def test_candidate_rejects_usage_resolver_from_another_phase0_snapshot(
    live_opt_in, phase0_manifest, attested_content_mapper, model_sdk_digests,
    tool_usage_bindings
) -> None:
    stale = tool_usage_bindings.with_source_phase0_digest("00" * 32)
    with pytest.raises(ProbeCandidateError, match="tool usage binding snapshot"):
        CandidateContentProbe.create(
            phase0_manifest, attested_content_mapper, model_sdk_digests, stale
        )


def test_candidate_rejects_sdk_digest_resolver_from_another_phase0_snapshot(
    live_opt_in, phase0_manifest, attested_content_mapper, model_sdk_digests,
    tool_usage_bindings
) -> None:
    stale = model_sdk_digests.with_source_phase0_digest("00" * 32)
    with pytest.raises(ProbeCandidateError, match="SDK digest snapshot"):
        CandidateContentProbe.create(
            phase0_manifest, attested_content_mapper, stale, tool_usage_bindings
        )


@pytest.mark.anyio
async def test_rich_candidate_passes_exact_resolved_sdk_digest_to_phase3_runner(
    live_opt_in, phase0_manifest, attested_content_mapper, model_sdk_digests,
    tool_usage_bindings, rich_result_runner
) -> None:
    probe = CandidateContentProbe.create(
        phase0_manifest,
        attested_content_mapper,
        model_sdk_digests,
        tool_usage_bindings,
        rich_result_runner,
    )
    case = next(
        case for case in probe.required_cases()
        if case.key.public_model == "sonnet"
        and case.key.dialect is Dialect.ANTHROPIC
        and case.key.kind == "tool_result_image"
        and case.key.thinking
            == ToolThinkingKey(mode="null", budget_tokens=None, effort=None)
        and case.streaming is False
    )
    projection = tool_usage_bindings.resolve(
        case.key.public_model, case.key.backend_model_id, case.key.dialect,
        case.key.thinking,
    )
    exact_release_args = (
        model_sdk_digests.resolve(
            case.key.public_model, case.key.backend_model_id
        ),
        case.key.dialect,
        projection.usage_rows,
    )
    assert rich_result_runner.release_binding_calls.count(exact_release_args) == 1
    assert case.release_binding == ToolReleaseBindingSnapshot(
        schema_version=rich_result_runner.validated_release_binding.schema_version,
        digest=rich_result_runner.validated_release_binding.digest,
    )
    frozen_cases = probe.required_cases()
    assert probe.required_cases() is frozen_cases
    assert rich_result_runner.release_binding_calls.count(exact_release_args) == 1

    result = await probe.run(case)

    assert result.case is case
    assert rich_result_runner.release_binding_calls.count(exact_release_args) == 3
    assert rich_result_runner.run_candidate_calls == ((
        case,
        phase0_manifest,
        exact_release_args[0],
        case.key.dialect,
        projection.usage_rows,
        rich_result_runner.validated_release_binding,
    ),)


@pytest.mark.anyio
async def test_absent_phase3_is_an_explicit_immutable_false_result(
    live_opt_in, phase0_manifest, attested_content_mapper, model_sdk_digests,
    tool_usage_bindings
) -> None:
    probe = CandidateContentProbe.create(
        phase0_manifest, attested_content_mapper, model_sdk_digests,
        tool_usage_bindings, rich_result_runner=None,
    )
    case = next(
        case for case in probe.required_cases()
        if case.key.kind == "tool_result_image"
        and case.key.dialect is Dialect.ANTHROPIC
        and case.streaming is False
    )
    assert case.disposition == "phase3_absent"
    assert case.sdk_record_digest is not None
    assert case.usage_projection is not None
    assert case.release_binding is None
    result = await probe.run(case)
    assert result.passed is False
    assert result.skipped is False
    assert result.failure_code == "phase3_absent"
    assert result.auth_source is None
    with pytest.raises(FrozenInstanceError):
        setattr(result, "passed", True)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("unavailable", "expected_disposition"),
    [("usage", "usage_unavailable"), ("release", "release_unavailable")],
)
async def test_unavailable_binding_is_false_without_probe_allocation(
    live_opt_in, candidate_snapshot_factory, unavailable, expected_disposition
) -> None:
    snapshot = candidate_snapshot_factory(
        unavailable=unavailable,
        public_alias="sonnet",
        dialect=Dialect.ANTHROPIC,
        thinking=ToolThinkingKey(
            mode="enabled", budget_tokens=4096, effort="high"
        ),
    )
    probe = CandidateContentProbe.create(
        snapshot.phase0, snapshot.mapper, snapshot.model_sdk_digests,
        snapshot.tool_usage_bindings, snapshot.rich_result_runner,
    )
    case = next(
        case for case in probe.required_cases()
        if case.key.kind == "tool_result_document"
        and case.key.dialect is Dialect.ANTHROPIC
        and case.key.thinking.mode == "enabled"
        and case.streaming is False
    )
    calls_before_run = snapshot.rich_result_runner.run_candidate_calls
    result = await probe.run(case)
    assert case.disposition == expected_disposition
    assert case.sdk_record_digest is not None
    if unavailable == "usage":
        assert case.usage_projection is None
    else:
        assert case.usage_projection is not None
    assert case.release_binding is None
    assert result.passed is False
    assert result.skipped is False
    assert result.failure_code == expected_disposition
    assert snapshot.rich_result_runner.run_candidate_calls == calls_before_run


@pytest.mark.anyio
async def test_unsupported_openai_document_is_false_and_never_allocates_mapper(
    candidate_content_probe, attested_content_mapper
) -> None:
    case = next(
        case for case in candidate_content_probe.required_cases()
        if case.key.kind == "document"
        and case.key.dialect is Dialect.OPENAI
        and case.streaming is False
    )
    assert case.key.thinking == ToolThinkingKey(
        mode="null", budget_tokens=None, effort=None
    )
    result = await candidate_content_probe.run(case)
    assert result.passed is False
    assert result.skipped is False
    assert result.failure_code == "unsupported_public_combination"
    assert attested_content_mapper.allocations_for(case) == 0


def test_candidate_is_absent_from_serve_config_router_and_capabilities(app) -> None:
    assert "content_candidate" not in ProxyConfig.model_fields
    assert all("candidate" not in route.path for route in app.router.routes)
    assert "candidate" not in app.capabilities.as_json()
    assert "content-release" not in serve_parser().format_help()
    assert "content-release" in probe_parser().format_help()


@pytest.mark.anyio
async def test_normal_content_gate_stays_false_until_generated_manifest_is_reloaded(
    candidate_content_probe, normal_server_factory, probe_cli_runner, tmp_path
) -> None:
    server = normal_server_factory(content_manifest=None)
    case = next(
        case for case in candidate_content_probe.required_cases()
        if case.key.kind == "image"
        and case.key.dialect is Dialect.ANTHROPIC
        and case.streaming is False
    )
    assert server.content_gate.supports(case.key) is False

    await candidate_content_probe.run(case)
    assert server.content_gate.supports(case.key) is False

    output = tmp_path / "validated-content.json"
    result = probe_cli_runner.invoke_content_release(output=output)
    assert result.exit_code == 0
    assert server.content_gate.supports(case.key) is False

    reloaded = ContentEvidenceManifest.load(output)
    restarted = normal_server_factory(content_manifest=reloaded)
    assert restarted.content_gate.supports(case.key) is True
```

- [ ] **Step 2: Run sealing tests to verify they fail**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_content_probe_candidate.py -v`

Expected: FAIL because the sealed candidate module and probe-only command do not exist.

- [ ] **Step 3: Implement the private permit and attested candidate runner**

```python
# Core declarations for src/claude_sdk_proxy/content_probe.py
ProbeDisposition = Literal[
    "ready",
    "unsupported_public_combination",
    "usage_unavailable",
    "phase3_absent",
    "release_unavailable",
]


@dataclass(frozen=True, slots=True)
class ToolReleaseBindingSnapshot:
    schema_version: Literal[1]
    digest: str

    def __post_init__(self) -> None: ...  # exact v1 and lowercase SHA-256


@dataclass(frozen=True, slots=True)
class ContentProbeCase:
    key: ContentEvidenceKey
    streaming: bool
    sdk_record_digest: str | None
    usage_projection: ToolUsageBindingProjection | None
    release_binding: ToolReleaseBindingSnapshot | None
    disposition: ProbeDisposition

    def __post_init__(self) -> None: ...  # enforce disposition/binding invariants
    def key_tuple(self) -> tuple[object, ...]: ...


@dataclass(frozen=True, slots=True)
class ContentProbeResult:
    case: ContentProbeCase
    passed: bool
    skipped: Literal[False]
    failure_code: ProbeDisposition | Literal["probe_failed"] | None
    auth_source: Literal["existing_claude_login"] | None
    model_identity_valid: bool
    cleanup_confirmed: bool
    positive_token_seen: bool
    negative_token_seen: bool
    dominant_color: Literal["red"] | None
    semantic_canary_seen: bool
    omitted_mapper_control_failed: bool
    callback_correlated: bool
    later_user_turn_succeeded: bool
    event_usage_stop_valid: bool
    redacted_result_digest: str

    def __post_init__(self) -> None: ...  # enforce ready/false result invariants
    def key_tuple(self) -> tuple[object, ...]:
        return self.case.key_tuple()


class _CandidateContentPermit:
    __slots__ = ("__nonce",)

    def __init__(self, nonce: object) -> None: ...
    def __reduce_ex__(self, protocol: int) -> NoReturn:
        raise TypeError("candidate permit is nonserializable")


class CandidateRichResultRunner(Protocol):
    def release_binding(
        self, sdk_record_digest: str, dialect: Dialect,
        usage_rows: UsageRowBindings,
    ) -> ToolReleaseBindingView: ...

    async def run_candidate_rich_result(
        self,
        case: ContentProbeCase,
        *,
        phase0: FeasibilityManifest,
        sdk_record_digest: str,
        dialect: Dialect,
        usage_rows: UsageRowBindings,
        release_binding: ToolReleaseBindingView,
    ) -> ContentProbeResult: ...


class CandidateContentProbe:
    _required_cases: tuple[ContentProbeCase, ...]

    @classmethod
    def create(
        cls,
        phase0: FeasibilityManifest,
        mapper: AttestedContentMapper,
        model_sdk_digests: ModelSdkDigestResolver,
        tool_usage_bindings: ToolUsageBindingResolverView,
        rich_result_runner: CandidateRichResultRunner | None = None,
    ) -> "CandidateContentProbe": ...

    def required_cases(self) -> tuple[ContentProbeCase, ...]: ...
    async def run(self, case: ContentProbeCase) -> ContentProbeResult: ...
```

`CandidateContentProbe.create()` is the only permit mint. Require the environment value to equal `RUN_LIVE_CLAUDE_TESTS=1`, call Phase 0 `require_core_gates()`, require the exact pinned runtime/platform/auth/semantic tuple, construct an immutable `Phase0PrerequisiteDigestResolver`, and validate that both `model_sdk_digests` and `tool_usage_bindings` were built from that same Phase 0 manifest/model map and, for usage bindings, the same usage schema/resolver and implemented-thinking snapshot. Copy all immutable snapshots. During `create()`, eagerly enumerate and freeze `_required_cases`; `required_cases()` only returns that same tuple and never resolves new evidence. For every rich tuple, resolve prerequisites in the fixed order SDK record digest, exact usage projection, runner presence, release binding, retaining every successfully frozen earlier field when a later prerequisite is unavailable. Construction resolves each unique ready rich prerequisite tuple `(sdk_record_digest, dialect, projection.usage_rows)` exactly once, calls `runner.release_binding(...)` once for that tuple, and copies only its validated schema version and lowercase digest into one `ToolReleaseBindingSnapshot` shared by the declared rich kind/mode cases for that tuple. No live probe or mapper allocation occurs during construction. `_CandidateContentPermit` validates a module-private nonce, is absent from `__all__`, rejects pickle/copy/JSON serialization, and is never accepted from a CLI argument, environment value, request, config file, or public constructor.

The candidate invokes `AttestedContentMapper` directly only after selecting the exact Phase 0 model record and passing per-child attestation. It uses private socketpairs and supervised child processes but never starts a TCP listener. Every generated key receives the selected pair's current Phase 0 prerequisite schema/digest; no generic filesystem label is written. `content_probe.py` defines `CandidateRichResultRunner` locally and never imports Phase 3. `required_cases()` is the only case-construction API: callers select an object by identity from that immutable tuple and pass it unchanged to `run()`; an equal-but-reconstructed or mutated case is rejected. Do not add any side constructor for image, document, or rich-result cases.

For each selected ready rich case, `run()` first verifies membership in the frozen tuple, revalidates the resolver source snapshots, re-resolves the exact immutable SDK record digest and thinking-specific `ToolUsageBindingProjection`, and requires equality with the values frozen in the case. It then obtains a concrete `validated_release_binding = runner.release_binding(sdk_record_digest, dialect, projection.usage_rows)` as the pre-execution check and requires its schema/digest to equal the frozen `ToolReleaseBindingSnapshot`. Pass that concrete object to `runner.run_candidate_rich_result(case, phase0=phase0, sdk_record_digest=sdk_record_digest, dialect=dialect, usage_rows=projection.usage_rows, release_binding=validated_release_binding)`. After the probe, call `release_binding()` again with the same three explicit arguments as the post-execution check, require it to match both the pre-execution object and frozen snapshot, and revalidate/re-resolve the SDK and usage snapshots. Thus the selected tuple's public resolver call count is exactly one shared construction snapshot plus one before and one after this execution; the concrete Task 4 runner separately enforces equality around builder execution. There is no alias/model adapter or positional/single-record lookup.

The immutable case invariants are exact. Every input `image`/`document` case has a non-null dialect-admitted thinking key and null SDK/usage/release fields. A ready rich case has non-null SDK digest, usage projection, and release snapshot, and the key's usage/release schemas and digests equal those snapshots. `usage_unavailable`, `phase3_absent`, and `release_unavailable` retain the exact thinking key but null only the fields that could not be resolved; `unsupported_public_combination` is explicit rather than omitted. `run()` never calls the mapper or rich runner for a non-ready disposition and returns an immutable false result.

The `candidate_snapshot_factory` test fixture builds a complete source-consistent Phase 0/model/usage snapshot for an honestly unavailable usage mapping, or an unchanged passing snapshot with an unavailable release binding. It must not manufacture `usage_unavailable` by mutating a resolver behind an unchanged Phase 0 manifest, because construction must reject that as cross-snapshot evidence instead of returning a false row.

`ContentProbeResult` is the only result type. `skipped` is literally `False`. A ready result has `failure_code=None` and `passed=True` only when authentication, exact model identity, cleanup, positive/negative semantic controls, kind-specific canaries, callback/later-turn controls where applicable, and event/usage stop validation all pass. Any failed ready probe has `passed=False` and `failure_code="probe_failed"`. A non-ready result has `passed=False`, a `failure_code` exactly equal to its case disposition, `auth_source=None`, conservative false/empty control fields, and a canonical content-free result digest. Every result digest is lowercase SHA-256 over the canonical redacted field projection; neither cases nor results contain binary bytes, extracted content, prompts, credentials, or raw frames.

The candidate stores the exact thinking key, usage-binding schema/digest, and validated release-binding schema/digest in each `tool_result_*` key. Any binding drift before, during, after, or before manifest commit makes that exact row false. Image, PDF, and conditional rich-result probes use the same production mapping code, actor/event normalization, cleanup ownership, and omission negative controls as serving, but the private permit bypasses only the not-yet-generated `ContentGate` row.

The candidate module is absent from `ProxyConfig`, `server_cli serve`, router/control construction, request schemas, and capability projection. Normal adapters always call `ContentGate.require()` before `AttestedContentMapper`; they cannot receive or mint the permit. Running the candidate mutates no process-global gate and cannot enable an already-running server.

- [ ] **Step 4: Implement and run the complete live candidate matrix**

```python
# tests/live/test_content_release.py
@pytest.mark.live
@pytest.mark.anyio
async def test_candidate_content_release_matrix(candidate_content_probe) -> None:
    cases = candidate_content_probe.required_cases()
    expected = candidate_content_probe.expected_case_keys()
    assert tuple(case.key_tuple() for case in cases) == expected

    results = [await candidate_content_probe.run(case) for case in cases]
    assert tuple(result.key_tuple() for result in results) == expected
    assert all(result.skipped is False for result in results)
    for result in results:
        if result.case.disposition == "ready":
            assert result.passed, result
            assert result.failure_code is None
            assert result.auth_source == "existing_claude_login"
            assert result.model_identity_valid
            assert result.cleanup_confirmed
        else:
            assert result.passed is False
            assert result.failure_code == result.case.disposition
            assert result.auth_source is None
    assert candidate_content_probe.covered_kinds == {
        "image", "document", "tool_result_image", "tool_result_document"
    }
```

`required_cases()` deterministically enumerates every Phase 0-configured `(public_alias, exact_backend_model_id)`, every applicable dialect, both streaming values, and all four content kinds. Every input and rich-result kind expands over exactly `tool_usage_bindings.required_thinking()` for that model/dialect: OpenAI has only the null/no-thinking key, while Anthropic has null plus the exact admitted enabled keys. Input cases retain that exact thinking key but have null SDK/usage/release fields. The candidate must neither generate nor silently skip an impossible OpenAI enabled-thinking case. A resolved rich key contains that thinking selection plus the current usage-binding and Phase 3 release-binding schema/digests; an unresolved/absent combination retains the thinking selection with the precise non-ready disposition and only unavailable bindings null. The generator groups exactly the `streaming=False` and `streaming=True` results for each complete `ContentEvidenceKey` into its `nonstreaming` and `streaming` booleans. Unsupported public combinations, including OpenAI document input, produce explicit false rows after their rejection controls. If Phase 3/Task 4 is absent, every required rich-result thinking combination is an explicit false row and not a skip. If present, each executes the full MCP callback, replay, stream, later-turn, and cleanup assertions with the exact SDK record digest and `UsageRowBindings` passed to Phase 3.

Run: `RUN_LIVE_CLAUDE_TESTS=1 uv run pytest --strict-markers --forbid-skips -W error tests/live/test_content_release.py tests/live/test_multimodal_backend.py -v -s`

Expected: every configured model/dialect/mode image/PDF case and every explicit unsupported row is accounted for with no skip.

If Phase 3 and Task 4 are selected, also run: `RUN_LIVE_CLAUDE_TESTS=1 uv run pytest --strict-markers --forbid-skips -W error tests/live/test_rich_tool_results.py -v -s`

Expected: every configured model/dialect/dialect-valid-thinking/mode rich-result candidate row is accounted for with no skip; OpenAI has no enabled-thinking row.

- [ ] **Step 5: Generate, reload, and verify the complete manifest**

Implement `content-release` only in `probe_cli.py`. Constructing `CandidateContentProbe` eagerly freezes the current immutable per-model Phase 0 digests, every per-model/dialect/thinking usage projection, and each available usage-bound Phase 3 binding into its cases. The CLI runs that complete deterministic tuple, then revalidates all source snapshots and resolves every prerequisite again before commit; any construction/pre-execution/post-execution/pre-commit difference makes the affected row false and rejects reuse of its prior evidence. It writes a same-directory mode-`0600` temporary through the repository's non-authoritative artifact writer, `F_FULLFSYNC`s it, atomically renames it, syncs the parent directory, reloads it with `ContentEvidenceManifest.load()`, binds it against the same still-current prerequisite/usage/release resolvers, and requires the regenerated record set to equal the candidate case set before success. It never preserves a stale true row from an older file; unavailable, failed, or changed-prerequisite rows are written false. This release artifact is content-addressed evidence, not a hostile same-UID integrity boundary.

Run: `RUN_LIVE_CLAUDE_TESTS=1 uv run claude-proxy-probe content-release --phase0 docs/feasibility/validated-environment.json --output docs/feasibility/validated-content.json`

Expected: a reloaded, content-free manifest covers every configured alias/backend-model, dialect, mode, and content kind; only complete passing pairs are true, and normal serving sees them only after loading this file at startup.

- [ ] **Step 6: Run tests and commit the sealed release path**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_content_probe_candidate.py tests/unit/test_content_evidence.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/content_probe.py src/claude_sdk_proxy/backend.py src/claude_sdk_proxy/probe_cli.py tests/unit/test_content_probe_candidate.py tests/live/test_content_release.py tests/live/test_multimodal_backend.py docs/feasibility/README.md docs/feasibility/validated-content.json
# If Phase 3 and Task 4 are selected:
git add tests/live/test_rich_tool_results.py
git commit -m "test: seal multimodal candidate evidence generation"
```

---

### Task 6: Extend Binary-Safe Diagnostics and Persistence Evidence

**Files:**
- Modify: `src/claude_sdk_proxy/diagnostics.py`
- Modify: `src/claude_sdk_proxy/router.py`
- Modify: `src/claude_sdk_proxy/backend.py`
- Modify: `src/claude_sdk_proxy/actor.py`
- Modify: `src/claude_sdk_proxy/path_policy.py`
- Modify: `tests/unit/test_diagnostics.py`
- Create: `tests/integration/test_multimodal_persistence.py`

**Interfaces:**
- Consumes: `DiagnosticSink`, `fingerprint_request()`, `PathPolicy`, `CleanupTicket`, canonical content blocks, and actor lifecycle marks.
- Produces: `BinaryDiagnosticShape`, `capture_path_events()`, `compare_and_scan_safe_paths()`, binary/tool-safe redaction, multimodal latency spans, and path-policy release evidence.

- [ ] **Step 1: Write failing binary redaction tests**

```python
# additions to tests/unit/test_diagnostics.py
def test_binary_and_tool_content_are_redacted_even_in_content_debug(redactor) -> None:
    value = {
        "messages": "caller text",
        "image": {"media_type": "image/png", "data": "BASE64-CANARY-12ab"},
        "document": {"decoded": b"PDF-CANARY-34cd"},
        "tool_result": "TOOL-CANARY-56ef",
        "authorization": "Bearer secret",
    }
    cleaned = redactor(include_text_content=True).clean(value)
    assert cleaned["messages"] == "caller text"
    assert cleaned["image"] == "[BINARY REDACTED]"
    assert cleaned["document"] == "[BINARY REDACTED]"
    assert cleaned["tool_result"] == "[TOOL CONTENT REDACTED]"
    assert cleaned["authorization"] == "[REDACTED]"
```

- [ ] **Step 2: Write the path-policy and cleanup test**

```python
# tests/integration/test_multimodal_persistence.py
@pytest.mark.anyio
async def test_multimodal_run_has_no_prohibited_content_or_leaked_artifact(multimodal_app, path_policy) -> None:
    canaries = (b"IMAGE-CANARY-12ab", b"PDF-CANARY-34cd", b"TOOL-CANARY-56ef")
    before = capture_path_events(path_policy, multimodal_app.monitored_roots)
    await multimodal_app.run_enabled_content_canaries(canaries)
    await multimodal_app.cleanup.confirmed()
    after = capture_path_events(path_policy, multimodal_app.monitored_roots)
    result = compare_and_scan_safe_paths(path_policy, before, after, canaries)
    assert result.prohibited_content_hits == ()
    assert result.leaked_temporary_artifacts == ()
    assert result.opened_metadata_only_paths == ()
    assert result.unknown_new_paths == ()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_diagnostics.py tests/integration/test_multimodal_persistence.py -v`

Expected: FAIL because binary/tool redaction and content path-policy evidence are incomplete.

- [ ] **Step 4: Implement keyed diagnostic shapes and path-policy integration**

```python
# Core declaration for src/claude_sdk_proxy/diagnostics.py
@dataclass(frozen=True, slots=True)
class BinaryDiagnosticShape:
    kind: str
    media_type: str
    encoded_bytes: int
    decoded_bytes: int
    keyed_fingerprint: str


def capture_path_events(policy: PathPolicy, roots: tuple[Path, ...]) -> StateEventSnapshot: ...


def compare_and_scan_safe_paths(
    policy: PathPolicy,
    before: StateEventSnapshot,
    after: StateEventSnapshot,
    canaries: tuple[bytes, ...],
) -> CanaryScanResult: ...
```

Emit only content kind, media type, encoded/decoded sizes, process-local keyed fingerprint, validation/decode/send/first-event/result/iterator/commit timings, queue high-water bytes, authoritative owned-process count, and terminal state. Never emit raw content, stable unkeyed content hashes, URLs, filenames, SDK session IDs, or credential values. Debug on/off must produce deeply equal backend input and identical backend-call count.

Apply the shared versioned path policy before and after live gates. Snapshot path/size/mtime events for the relevant Claude root, never open metadata-only credential paths, scan only safe/newly classified noncredential paths, fail on unknown paths, and distinguish confirmed removal from `cleanup_unconfirmed` retention. Without Phase 3, `run_enabled_content_canaries()` runs only image/PDF inputs and does not import or instantiate `ToolBridge`; with Phase 3 and Task 4 selected, it additionally runs the tool-result canary.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_diagnostics.py tests/integration/test_multimodal_persistence.py -v`

Expected: PASS with no prohibited persistent content and no leaked temporary artifacts.

```bash
git add src/claude_sdk_proxy/diagnostics.py src/claude_sdk_proxy/router.py src/claude_sdk_proxy/backend.py src/claude_sdk_proxy/actor.py src/claude_sdk_proxy/path_policy.py tests/unit/test_diagnostics.py tests/integration/test_multimodal_persistence.py
git commit -m "test: enforce multimodal diagnostics and persistence policy"
```

---

### Task 7: Build Reproducible Local Comparative Benchmarks

**Files:**
- Modify: `pyproject.toml`
- Create: `src/claude_sdk_proxy/benchmarks.py`
- Modify: `src/claude_sdk_proxy/server_cli.py`
- Create: `tests/unit/test_benchmarks.py`
- Create: `tests/live/test_benchmarks.py`
- Create: `tests/optional/test_raw_benchmarks.py`
- Create: `docs/benchmarks.md`

**Interfaces:**
- Consumes: proxy HTTP APIs, authoritative lifecycle process inventory, and—only in the separately selected optional target—a separately configured raw Anthropic client.
- Produces: `BenchmarkIdentity`, `BenchmarkSample`, `BenchmarkReport`, `MANDATORY_BENCHMARK_TARGETS`, `OPTIONAL_BENCHMARK_TARGETS`, `summarize()`, default mandatory-only `claude-proxy benchmark`, and explicit `--include-raw-anthropic` opt-in.

- [ ] **Step 1: Write failing aggregation and redaction tests**

```python
# tests/unit/test_benchmarks.py
from claude_sdk_proxy.benchmarks import (
    BenchmarkIdentity,
    BenchmarkSample,
    MANDATORY_BENCHMARK_TARGETS,
    OPTIONAL_BENCHMARK_TARGETS,
    summarize,
)


def test_summary_preserves_identity_and_stores_no_content() -> None:
    identity = BenchmarkIdentity(
        target="proxy",
        backend_kind="agent_sdk_subscription",
        auth_source="existing_claude_login",
        semantic_class="prompt_isolated_agent_sdk",
        os_build="24G90",
        filesystem="apfs-local",
        public_model="sonnet",
        backend_model_id="claude-sonnet-4-6-20260801",
    )
    report = summarize((
        BenchmarkSample(identity, first_token_ms=20, total_ms=40, process_count=3, peak_rss=100),
        BenchmarkSample(identity, first_token_ms=30, total_ms=50, process_count=3, peak_rss=110),
        BenchmarkSample(identity, first_token_ms=40, total_ms=60, process_count=3, peak_rss=120),
    ))
    assert report.median_first_token_ms == 30
    assert "response_text" not in report.model_dump()
    assert report.identity.backend_model_id == "claude-sonnet-4-6-20260801"


def test_default_benchmark_targets_exclude_optional_raw_platform(
    benchmark_cli_runner
) -> None:
    assert MANDATORY_BENCHMARK_TARGETS == ("proxy", "normal_claude_code")
    assert OPTIONAL_BENCHMARK_TARGETS == ("raw_anthropic",)
    result = benchmark_cli_runner.invoke(["benchmark", "--model", "sonnet"])
    assert result.exit_code == 0
    assert result.report.targets == MANDATORY_BENCHMARK_TARGETS
    assert result.raw_client_constructions == 0


def test_raw_target_requires_explicit_flag_and_platform_key(
    benchmark_cli_runner, monkeypatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_PLATFORM_API_KEY", raising=False)
    result = benchmark_cli_runner.invoke([
        "benchmark", "--model", "sonnet", "--include-raw-anthropic",
    ])
    assert result.exit_code == 2
    assert "ANTHROPIC_PLATFORM_API_KEY is required" in result.stderr
    assert result.raw_client_constructions == 0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_benchmarks.py -v`

Expected: FAIL because benchmark identities and reports do not exist.

- [ ] **Step 3: Implement benchmark collection and target isolation**

Define `MANDATORY_BENCHMARK_TARGETS = ("proxy", "normal_claude_code")` and `OPTIONAL_BENCHMARK_TARGETS = ("raw_anthropic",)` as immutable tuples. The default CLI and every release test iterate only the mandatory tuple. Run one warmup and five measured samples for each selected target. Record process startup, first byte, first model event when observable, first text token, total time, authoritative supervisor/anchor/CLI process count, peak resident memory, stop reason, usage, response byte count, and success. Store no prompt, response, image, PDF, tool payload, SDK session ID, or credential.

Use the lifecycle registry plus Darwin process identity APIs for the proxy process inventory. `--include-raw-anthropic` is rejected before client construction unless `ANTHROPIC_PLATFORM_API_KEY` is explicitly present, and then constructs a separate official Platform client; the Agent SDK child environment assertion must prove that variable is absent. Never infer or relabel raw semantics from a subscription-backed run. Raw results are diagnostic-only and are excluded from every evidence manifest, release case, capability calculation, default CLI report, and mandatory test command.

- [ ] **Step 4: Add the CLI and methodology documentation**

CLI syntax:

```text
claude-proxy benchmark --model sonnet --output probe-output/benchmark.json

# Optional diagnostic only; never a release command:
ANTHROPIC_PLATFORM_API_KEY=<separate-key> claude-proxy benchmark \
  --model sonnet --include-raw-anthropic \
  --output probe-output/benchmark-with-raw.json
```

Register the marker in `pyproject.toml` under pytest configuration as `raw_platform_benchmark: optional non-authoritative benchmark requiring a separately supplied Anthropic Platform API key`. Do not register or use a skip-on-missing-key marker. The optional raw test also carries `@pytest.mark.live` and `@pytest.mark.anyio`; every async benchmark test, mandatory or optional, carries `@pytest.mark.anyio` so pytest 8.4 executes it through the configured AnyIO plugin rather than raising the direct-async-test collection error.

```python
# tests/live/test_benchmarks.py
pytestmark = pytest.mark.live


def test_mandatory_benchmark_target_partition(benchmark_harness) -> None:
    assert MANDATORY_BENCHMARK_TARGETS == ("proxy", "normal_claude_code")
    assert "raw_anthropic" not in MANDATORY_BENCHMARK_TARGETS
    assert benchmark_harness.raw_client_constructions == 0


@pytest.mark.anyio
@pytest.mark.parametrize("target", MANDATORY_BENCHMARK_TARGETS)
async def test_mandatory_benchmark_target_executes_without_content(
    benchmark_harness, target
) -> None:
    report = await benchmark_harness.run(target)
    assert report.identity.target == target
    assert report.success is True
    assert report.sample_count == 5
    assert "response_text" not in report.model_dump()
    assert benchmark_harness.raw_client_constructions == 0


# tests/optional/test_raw_benchmarks.py
@pytest.mark.live
@pytest.mark.raw_platform_benchmark
@pytest.mark.anyio
async def test_explicit_raw_platform_comparison(raw_benchmark_harness) -> None:
    # The fixture fails at setup if the explicit nonempty key is absent; it never skips.
    report = await raw_benchmark_harness.run_explicit_raw_target()
    assert report.identity.target == "raw_anthropic"
    assert report.success is True
    assert "response_text" not in report.model_dump()
    assert raw_benchmark_harness.agent_sdk_child_received_platform_key is False
```

Document hardware, OS build, APFS volume, SDK/CLI, backend identity, exact model IDs, warmup/sample count, subscription-limit effects, nondeterminism, and why response equality is not a correctness criterion.

- [ ] **Step 5: Run unit and live benchmark tests**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit/test_benchmarks.py -v`

Expected: PASS.

Run: `RUN_LIVE_CLAUDE_TESTS=1 CLAUDE_PROXY_TEST_MODEL=sonnet uv run pytest --strict-markers --forbid-skips -W error tests/live/test_benchmarks.py -v -s`

Expected: the collected target set is exactly `MANDATORY_BENCHMARK_TARGETS`; proxy and normal-Claude-Code samples contain valid identity/timing/process/memory/stop/usage fields and no content, all async tests execute through AnyIO, and the repository `--forbid-skips` recorder observes zero skipped collection/setup/call/teardown reports. No raw target is collected, constructed, or required when the Platform key is absent.

Optional diagnostic command, never part of release verification and never a source of capability evidence:

Run: `RUN_LIVE_CLAUDE_TESTS=1 ANTHROPIC_PLATFORM_API_KEY=<separate-key> uv run pytest --strict-markers --forbid-skips -W error tests/optional/test_raw_benchmarks.py -m raw_platform_benchmark -v -s`

Expected when explicitly requested with a valid separate key: the one marker-selected `@pytest.mark.live`, `@pytest.mark.raw_platform_benchmark`, `@pytest.mark.anyio` raw comparison executes without a skip, the mandatory Agent SDK child lacks the Platform key, and the diagnostic report remains content-free. Absence of the key means this optional command is not invoked; it never turns a mandatory test into a skip or affects release success.

- [ ] **Step 6: Commit benchmarks**

```bash
git add pyproject.toml uv.lock src/claude_sdk_proxy/benchmarks.py src/claude_sdk_proxy/server_cli.py tests/unit/test_benchmarks.py tests/live/test_benchmarks.py tests/optional/test_raw_benchmarks.py docs/benchmarks.md
git commit -m "feat: add identified local comparison benchmarks"
```

---

### Task 8: Publish Capabilities and Run the Final Release Matrix

**Files:**
- Modify: `src/claude_sdk_proxy/capabilities.py`
- Modify: `src/claude_sdk_proxy/control.py`
- Modify: `README.md`
- Create: `docs/multimodal.md`
- Create: `docs/verification.md`
- Modify: `tests/integration/test_capabilities.py`
- Create: `tests/live/test_release_matrix.py`

**Interfaces:**
- Consumes: one schema-validated Phase 0 environment/usage snapshot, Phase 1 `UsageRowBindings`, Task 1 immutable `ToolUsageBindingResolver` and source-snapshot-bound `ModelSdkDigestResolver`, immutable per-model prerequisite resolution, optional structurally compatible Phase 3 `ToolReleaseEvidence`/usage-bound release bindings, `ContentEvidenceManifest`, runtime/backend/model identities, and all selected live gate reports.
- Produces: prerequisite/usage-bound `ContentGate`, `CapabilityDocument`, `ModelCapability`, `DialectCapability`, `assemble_capabilities()`, `validate_release_evidence()`, and final operator documentation.

- [ ] **Step 1: Write failing authenticated capability tests**

```python
# additions to tests/integration/test_capabilities.py
@pytest.mark.anyio
async def test_capabilities_report_exact_identity_and_per_dialect_content(client, identities) -> None:
    response = await client.get("/_proxy/capabilities", headers=identities.master_headers)
    body = response.json()
    assert body["backend_kind"] == "agent_sdk_subscription"
    assert body["auth_source"] == "existing_claude_login"
    assert body["semantic_class"] == "prompt_isolated_agent_sdk"
    assert body["platform"] == {"os_build": "24G90", "filesystem": "apfs-local"}
    assert body["models"]["sonnet"]["backend_model_id"] == "claude-sonnet-4-6-20260801"
    assert body["models"]["sonnet"]["anthropic"]["image"] is True
    assert body["models"]["sonnet"]["openai"]["document"] is False


@pytest.mark.anyio
async def test_health_is_anonymous_and_detail_free(client) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert set(response.json()) == {"status"}


@pytest.mark.anyio
async def test_parser_and_capability_fail_together_when_evidence_row_is_false(app_factory) -> None:
    app = app_factory.with_content_gate(kind="image", dialect="anthropic", enabled=False)
    assert app.capabilities.models["sonnet"].anthropic.image is False
    response = await app.client.send_anthropic_image()
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_content_type"


def test_phase4_without_phase3_keeps_image_pdf_independent(
    config, feasibility, phase0_prerequisite_digests, model_sdk_digests,
    tool_usage_bindings, content_evidence
) -> None:
    document = assemble_capabilities(
        config=config,
        feasibility=feasibility,
        phase0_prerequisite_digests=phase0_prerequisite_digests,
        model_sdk_digests=model_sdk_digests,
        tool_usage_bindings=tool_usage_bindings,
        content_evidence=content_evidence,
        tool_evidence=None,
    )
    assert document.models["sonnet"].anthropic.image is True
    assert document.models["sonnet"].anthropic.document is True
    for dialect in (document.models["sonnet"].anthropic, document.models["sonnet"].openai):
        assert dialect.tools is False
        assert dialect.one_shot_tools is False
        assert dialect.tool_result_image is False
        assert dialect.tool_result_document is False


def test_image_and_document_capabilities_are_conservative_across_thinking(
    app_factory
) -> None:
    app = app_factory.with_input_content_thinking_rows(
        anthropic_null="passing",
        anthropic_enabled_4096_high_image="false",
        anthropic_enabled_4096_high_document="false",
        openai_null="passing",
    )
    no_thinking = ToolThinkingKey(mode="null", budget_tokens=None, effort=None)
    enabled = ToolThinkingKey(mode="enabled", budget_tokens=4096, effort="high")
    anthropic = app.capabilities.for_dialect("sonnet", Dialect.ANTHROPIC)
    openai = app.capabilities.for_dialect("sonnet", Dialect.OPENAI)
    assert anthropic.image is False
    assert anthropic.document is False
    assert openai.image is True
    for thinking in (no_thinking, enabled):
        parser = app.parsers.for_model("sonnet", Dialect.ANTHROPIC)
        assert parser.accepts_image(thinking) is False
        assert parser.accepts_document(thinking) is False
    assert app.parsers.for_model(
        "sonnet", Dialect.OPENAI
    ).accepts_image(no_thinking) is True


@pytest.mark.parametrize("dialect", [Dialect.ANTHROPIC, Dialect.OPENAI])
def test_tools_capability_and_parser_require_the_exact_two_mode_gate(
    app_factory, dialect
) -> None:
    app = app_factory.with_exact_tool_evidence(dialect=dialect)
    capability = app.capabilities.for_dialect("sonnet", dialect)
    assert capability.tools is True
    assert capability.one_shot_tools is False
    assert app.parsers.for_dialect(dialect).parse(app.tool_request(dialect)).tools


@pytest.mark.parametrize("dialect", [Dialect.ANTHROPIC, Dialect.OPENAI])
@pytest.mark.parametrize("case", [
    "missing_digest",
    "mismatched_digest",
    "wrong_dialect",
    "missing_usage_binding",
    "stale_usage_row",
    "stale_usage_mapping",
    "swapped_usage_binding",
    "different_thinking_tuple",
    "missing_nonstream",
    "false_nonstream",
    "missing_stream",
    "false_stream",
])
def test_exact_tool_evidence_failure_disables_parser_and_advertisement(
    app_factory, dialect, case
) -> None:
    app = app_factory.with_tool_evidence_case(case, dialect=dialect)
    capability = app.capabilities.for_dialect("sonnet", dialect)
    assert capability.tools is False
    assert capability.one_shot_tools is False
    assert capability.tool_result_image is False
    assert capability.tool_result_document is False

    with pytest.raises(ProxyError, match="unsupported_tools"):
        app.parsers.for_dialect(dialect).parse(app.tool_request(dialect))
    assert app.tool_bridge_constructions == 0


def test_single_tools_boolean_is_conservative_across_every_admitted_thinking_tuple(
    app_factory
) -> None:
    app = app_factory.with_tool_thinking_tuples(
        no_thinking="passing",
        enabled_4096_high="stale_post_result_usage",
    )
    capability = app.capabilities.for_dialect("sonnet", Dialect.ANTHROPIC)
    assert capability.tools is False
    assert capability.tool_result_image is False
    for thinking in app.tool_usage_bindings.required_thinking(
        "sonnet", app.backend_model_id, Dialect.ANTHROPIC
    ):
        parser = app.parsers.for_model("sonnet", Dialect.ANTHROPIC)
        assert parser.accepts_tools(thinking) is capability.tools
        assert parser.accepts_rich_tool_results(thinking) is capability.tool_result_image


def test_all_thinking_tuple_bindings_enable_parser_and_capability_together(
    app_factory
) -> None:
    app = app_factory.with_all_tool_thinking_tuples_passing()
    capability = app.capabilities.for_dialect("sonnet", Dialect.ANTHROPIC)
    assert capability.tools is True
    assert capability.tool_result_image is True
    for thinking in app.tool_usage_bindings.required_thinking(
        "sonnet", app.backend_model_id, Dialect.ANTHROPIC
    ):
        parser = app.parsers.for_model("sonnet", Dialect.ANTHROPIC)
        assert parser.accepts_tools(thinking) is capability.tools
        assert parser.accepts_rich_tool_results(thinking) is capability.tool_result_image


def test_anthropic_enabled_tuple_failure_does_not_disable_valid_openai_tools(
    app_factory
) -> None:
    app = app_factory.with_cross_dialect_tool_thinking_tuples(
        anthropic_no_thinking="passing",
        anthropic_enabled_4096_high="stale_post_result_usage",
        openai_no_thinking="passing",
    )
    no_thinking = ToolThinkingKey(mode="null", budget_tokens=None, effort=None)

    assert app.tool_usage_bindings.required_thinking(
        "sonnet", app.backend_model_id, Dialect.OPENAI
    ) == (no_thinking,)
    assert app.capabilities.for_dialect("sonnet", Dialect.ANTHROPIC).tools is False
    assert app.capabilities.for_dialect("sonnet", Dialect.OPENAI).tools is True
    assert app.parsers.for_model(
        "sonnet", Dialect.OPENAI
    ).accepts_tools(no_thinking) is True
    with pytest.raises(ProxyError, match="unsupported_parameter"):
        app.parsers.for_model("sonnet", Dialect.OPENAI).parse(
            app.tool_request(Dialect.OPENAI, reasoning_effort="high")
        )


def test_one_model_missing_mode_cannot_disable_or_enable_the_other(
    two_model_app_factory
) -> None:
    app = two_model_app_factory.with_missing_tool_row(
        public_alias="opus", dialect=Dialect.OPENAI, streaming=True
    )
    assert app.capabilities.for_dialect("sonnet", Dialect.OPENAI).tools is True
    assert app.capabilities.for_dialect("opus", Dialect.OPENAI).tools is False
    assert app.parsers.for_model("sonnet", Dialect.OPENAI).accepts_tools is True
    assert app.parsers.for_model("opus", Dialect.OPENAI).accepts_tools is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/integration/test_capabilities.py -v`

Expected: FAIL because final identity/content capability assembly is incomplete.

- [ ] **Step 3: Implement one shared capability assembler**

```python
# Core declarations for src/claude_sdk_proxy/capabilities.py
class ToolReleaseEvidence(Protocol):
    def resolve_usage_binding(
        self, sdk_record_digest: str, dialect: Dialect,
        usage_rows: UsageRowBindings,
    ) -> ResolvedToolUsageBindingView: ...

    def require_session_tools(
        self, sdk_record_digest: str, dialect: Dialect,
        usage_rows: UsageRowBindings,
    ) -> tuple[object, object]: ...

    def release_binding(
        self, sdk_record_digest: str, dialect: Dialect,
        usage_rows: UsageRowBindings,
    ) -> ToolReleaseBindingView: ...


@dataclass(frozen=True, slots=True)
class DialectCapability:
    text: bool
    tools: bool
    image: bool
    document: bool
    tool_result_image: bool
    tool_result_document: bool
    one_shot_tools: bool = False


@dataclass(frozen=True, slots=True)
class ModelCapability:
    backend_model_id: str
    anthropic: DialectCapability
    openai: DialectCapability


def validate_release_evidence(
    tool_evidence: ToolReleaseEvidence | None,
    sdk_record_digest: str,
    dialect: Dialect,
    usage_rows: UsageRowBindings,
) -> bool: ...


def assemble_capabilities(
    config: ProxyConfig,
    feasibility: FeasibilityManifest,
    phase0_prerequisite_digests: Phase0PrerequisiteDigestResolver,
    model_sdk_digests: ModelSdkDigestResolver,
    tool_usage_bindings: ToolUsageBindingResolver,
    content_evidence: ContentEvidenceManifest,
    tool_evidence: ToolReleaseEvidence | None,
) -> CapabilityDocument: ...
```

`capabilities.py` owns the exact structural `ToolReleaseEvidence` protocol above, so it never imports a Phase 3 module. The concrete Phase 3 object satisfies it when installed. Consume the `Phase0PrerequisiteDigestResolver`, `ModelSdkDigestResolver`, and `ToolUsageBindingResolver` already constructed by Task 1 from one schema-validated Phase 0 manifest/config/usage snapshot; call `model_sdk_digests.validate_source(...)` at composition, then pass the same three immutable objects to parser, content binding, candidate generation, and capability construction. Task 8 does not redeclare, reconstruct, or independently hash SDK records.

First call `content_evidence.bind_current(phase0_prerequisite_digests, model_sdk_digests, tool_usage_bindings, tool_evidence)` and use the resulting immutable `ContentGate` for both request parsers and capability rows. For each configured model/dialect, obtain the nonempty exact list from `tool_usage_bindings.required_thinking()`. For each thinking selection, resolve its projection and `sdk_record_digest`, then call `tool_evidence.resolve_usage_binding(sdk_record_digest, dialect, projection.usage_rows)` and require exact row and thinking-identity agreement before `validate_release_evidence(...)`. That predicate calls the exact Phase 3 `require_session_tools(..., usage_rows)` contract, which requires the matching SDK record, all three bound passing usage rows/mappings, and both tuple-specific framing modes. Rich-result checks call `release_binding(..., usage_rows)` and compare it with the release schema/digest on the content row whose thinking and tool-usage-binding schema/digest match that same projection. Return false for expected unavailable/stale evidence; let malformed/cross-snapshot resolver programming errors fail startup. Do not inspect a generic key, accept one mode, reuse another model's/tuple's usage rows, or fall back across model/dialect/thinking.

Compute `image` and `document` independently of Phase 3 but over that same dialect-specific required thinking set. Each flag is true only when the set is nonempty and every exact input row for that kind has both modes true; otherwise the flag and parser aggregate are false for the whole model/dialect. The parser checks the aggregate before exact-row lookup, so one stale Anthropic enabled-thinking image/PDF row disables that Anthropic input kind without affecting OpenAI's null-only set or the other input kind. Do not advertise a no-thinking input row as coverage for enabled thinking.

Because the public schema has one `tools` boolean per model/dialect rather than per-thinking flags, apply conservative all-tuples semantics over that dialect's exact required set only: OpenAI aggregates exactly the null/no-thinking selection, while Anthropic aggregates null plus its exact admitted enabled selections. `tools` is true only when that dialect-specific set is nonempty and every required selection resolves and passes `validate_release_evidence()`; otherwise it is false and the parser rejects tools for every dialect-valid thinking selection of that model/dialect, including individually passing ones. An unavailable Anthropic enabled tuple cannot disable OpenAI tools, and no impossible OpenAI reasoning tuple participates in aggregation, parser equality, content binding, candidate generation, or release expansion. `tool_result_image` and `tool_result_document` are independently true only when `tools` is true and every required thinking selection also has a current bound content row with both modes true for that rich kind. The parser uses those same aggregate booleans before tuple-specific lookup, so parser acceptance equals advertisement exactly; Phase 2 still rejects any OpenAI reasoning input before tool tuple lookup. `one_shot_tools` remains false. When Phase 3 evidence is absent, all tool/rich flags and parser gates are false while input image/PDF stays independent. No empty-set truth, partial-tuple advertisement, or borrowing another tuple's release digest is allowed.

The assembler is the only source for both parser gates and `/_proxy/capabilities`. Include strict/compatibility ignored fields, supported thinking tuples, modes, limits, backend/auth/semantic identity, platform tuple, and exact model mappings. Require the master or active derived run token; no credential receives `401`. A retired digest never authenticates to capabilities. `/health` exposes only status.

- [ ] **Step 4: Implement the no-skip release matrix**

```python
# tests/live/test_release_matrix.py
@dataclass(frozen=True, slots=True)
class ReleaseCase:
    public_alias: str
    exact_backend_model_id: str
    phase0_prerequisite_digest: str
    dialect: Dialect
    thinking: ToolThinkingKey
    tool_usage_binding_schema_version: Literal[1] | None
    tool_usage_binding_digest: str | None
    streaming: bool
    feature: Literal[
        "image", "document", "tools", "tool_result_image", "tool_result_document"
    ]
    tool_release_schema_version: Literal[1] | None
    tool_release_digest: str | None

    def test_id(self) -> str:
        mode = "stream" if self.streaming else "nonstream"
        thinking = (
            f"{self.thinking.mode}-{self.thinking.budget_tokens}-{self.thinking.effort}"
        )
        return (
            f"{self.public_alias}-{self.dialect.value}-{thinking}-{mode}-{self.feature}"
        )


def test_release_cases_equal_every_advertised_model_dialect_mode_row(
    committed_capability_document
) -> None:
    cases = tuple(advertised_release_cases())
    expected = expand_advertised_content_and_tool_rows(
        committed_capability_document
    )
    assert len(cases) == len(set(cases))
    assert set(cases) == set(expected)


def test_release_cases_follow_each_assembled_dialect_capability(
    committed_capability_document, release_environment
) -> None:
    cases = tuple(advertised_release_cases())
    for public_alias, backend_model_id in release_environment.model_map.by_alias.items():
        for dialect in (Dialect.ANTHROPIC, Dialect.OPENAI):
            capability = committed_capability_document.for_dialect(
                public_alias, dialect
            )
            required = set(
                release_environment.tool_usage_bindings.required_thinking(
                    public_alias, backend_model_id, dialect
                )
            )

            for feature, advertised in (
                ("image", capability.image),
                ("document", capability.document),
                ("tools", capability.tools),
                ("tool_result_image", capability.tool_result_image),
                ("tool_result_document", capability.tool_result_document),
            ):
                feature_cases = tuple(
                    case for case in cases
                    if case.public_alias == public_alias
                    and case.exact_backend_model_id == backend_model_id
                    and case.dialect is dialect
                    and case.feature == feature
                )
                assert {
                    (case.thinking, case.streaming) for case in feature_cases
                } == (
                    {
                        (thinking, streaming)
                        for thinking in required
                        for streaming in (False, True)
                    }
                    if advertised else set()
                )
                if feature in {"image", "document"}:
                    assert all(
                        case.tool_usage_binding_schema_version is None
                        and case.tool_usage_binding_digest is None
                        and case.tool_release_schema_version is None
                        and case.tool_release_digest is None
                        for case in feature_cases
                    )

            if capability.tools is False:
                assert capability.tool_result_image is False
                assert capability.tool_result_document is False


@pytest.mark.parametrize("availability", ["phase3_absent", "mapping_unavailable"])
def test_honestly_false_tool_capability_emits_no_release_case_or_skip(
    release_environment, availability
) -> None:
    document, cases, public_alias, dialect = (
        release_environment.advertised_cases_for_tool_availability(availability)
    )
    capability = document.for_dialect(public_alias, dialect)
    assert capability.tools is False
    assert capability.tool_result_image is False
    assert capability.tool_result_document is False
    assert not any(
        case.public_alias == public_alias
        and case.dialect is dialect
        and case.feature in {
            "tools", "tool_result_image", "tool_result_document"
        }
        for case in cases
    )
    assert "skipped" not in ReleaseCase.__dataclass_fields__
    assert set(cases) == set(expand_advertised_content_and_tool_rows(document))


@pytest.mark.live
@pytest.mark.anyio
async def test_every_advertised_content_and_tool_row_has_current_live_evidence(
    release_harness, committed_capability_document
) -> None:
    cases = tuple(advertised_release_cases())
    expected = tuple(
        expand_advertised_content_and_tool_rows(committed_capability_document)
    )
    assert set(cases) == set(expected)
    assert len(cases) == len(expected)
    assert release_harness.skipped_cases == ()
    assert release_harness.executed_cases == ()
    if not cases:
        # An empty exact advertised non-text set is a valid, collected result.
        assert cases == ()
        assert expected == ()
        assert release_harness.skipped_cases == ()
        assert release_harness.executed_cases == ()
        return

    for case in cases:
        result = await release_harness.run_exact_release_case(case)
        assert result.passed, result.redacted_evidence
        assert result.skipped_cases == ()
        assert result.public_alias == case.public_alias
        assert result.backend_model_id == case.exact_backend_model_id
        assert result.phase0_prerequisite_digest == case.phase0_prerequisite_digest
        assert result.dialect == case.dialect
        assert result.thinking == case.thinking
        assert (
            result.tool_usage_binding_schema_version
            == case.tool_usage_binding_schema_version
        )
        assert result.tool_usage_binding_digest == case.tool_usage_binding_digest
        assert result.streaming is case.streaming
        assert result.feature == case.feature
        assert result.tool_release_schema_version == case.tool_release_schema_version
        assert result.tool_release_digest == case.tool_release_digest
    assert tuple(release_harness.executed_cases) == cases
    assert release_harness.skipped_cases == ()
```

`advertised_release_cases()` loads the committed validated config/manifests without a model-selection environment variable, binds content evidence to the current prerequisite/usage/release resolvers, assembles the authenticated capability document, and derives expectations separately for every configured model/dialect from that assembled document. Input cases expand independently for each true input flag, every exact dialect-admitted thinking key, and both streaming values; they carry the exact Phase 0 prerequisite digest and null tool-usage/release bindings. If `tools` is false, emit no base-tool or rich-result release case for that model/dialect; the assembler invariant also requires both rich flags false. If `tools` is true, base-tool cases expand for exactly that dialect's required `ToolThinkingKey` set and both streaming values. `tool_result_image` and `tool_result_document` are then checked independently: each true flag expands its own exact required-thinking/two-mode rows and bindings, while each false flag emits no case even when base tools are true. Every emitted tool or rich case carries that tuple's exact tool-usage-binding and Phase 3 release schemas/digests, and every rich case matches the same tuple-specific content row.

Phase 3 absence, a dialect-valid but honestly unavailable usage mapping, or stale evidence therefore yields false tool/rich capability flags and zero corresponding release cases. That is a passing negative capability outcome, not a skipped test, placeholder case, or collection failure. The live matrix is one always-collected async loop test, never an import-time parameterization; if the exact advertised non-text set is empty it explicitly asserts an empty expectation, zero executions, and zero skips, then passes. Collection or execution fails only if the emitted set differs from expansion of the assembled capability document, is duplicated, omits a row required by a true flag, invents a row for a false flag or impossible OpenAI reasoning tuple, contains a disabled/stale row, or resolves different bindings than the parser/capability gate. It may be nonempty solely because independent image/document flags are true; it need not contain tool rows. The release harness runs no-skip assertions for every emitted row and additionally runs authentication/model attestation, representative full-history harness one-shot/linear/stream/retry/cancellation cases, persistence policy, cleanup fault injection, abrupt-death reconciliation, and startup failures for stale manifests and unsupported platform tuples. An advertised row may not skip or substitute another model/dialect/thinking/mode result.

- [ ] **Step 5: Run the final verification commands**

Run: `uv run pytest --strict-markers --forbid-skips -W error tests/unit tests/integration -v`

Expected: the explicitly scoped unit and integration tests PASS with no warnings or skips; no `tests/live` or `tests/optional` node is collected.

Run: `RUN_LIVE_CLAUDE_TESTS=1 uv run pytest --strict-markers --forbid-skips -W error tests/live -v -s`

Expected: every advertised model/dialect/streaming/content/tool row, both mandatory benchmark targets, and representative harness preset PASS with zero skips. `tests/optional/test_raw_benchmarks.py` and the `raw_anthropic` target are outside `tests/live`, are not collected by this command, and have no release authority; disabled capability rows remain explicitly false.

Run: `uv run ruff check . && uv run mypy src/claude_sdk_proxy`

Expected: no findings or errors.

- [ ] **Step 6: Complete operator and verification documentation**

Document exact accepted image/PDF/tool-result forms, mode/dialect differences, limits, the Phase 0 prerequisite, thinking-specific tool-usage, and usage-bound Phase 3 release schemas, immutable per-model/dialect/thinking resolution, conservative all-thinking capability semantics, parser/capability equality, personal-local policy, prompt-isolated Agent SDK semantics, cleanup-unconfirmed behavior, default binary/tool redaction, text-only content-debug exception, benchmark interpretation, refresh/upgrade invalidation and regeneration rules, and the exact release commands above.

- [ ] **Step 7: Commit final capabilities and release evidence**

```bash
git add src/claude_sdk_proxy/capabilities.py src/claude_sdk_proxy/control.py README.md docs/multimodal.md docs/verification.md tests/integration/test_capabilities.py tests/live/test_release_matrix.py
git commit -m "docs: publish verified multimodal capabilities"
```
