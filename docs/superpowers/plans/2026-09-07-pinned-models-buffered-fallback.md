# Pinned Models and Buffered Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship pinned Sonnet 5, Opus 5, and Opus 4.8 names with opt-in buffered native refusal fallback and truthful response identities.

**Architecture:** Normalize model routing separately from requested and active session identity. The SDK boundary validates native legs and buffers auto-mode output; the existing registry/actors retain accepted identity with replay state. HTTP renders committed identity, never mutable session state.

**Tech Stack:** Python 3.14, existing Claude Agent SDK, Starlette, pytest/AnyIO, Ruff, mypy, existing Pi integration fixtures. No new dependency.

**Spec:** `docs/superpowers/specs/2026-09-07-pinned-models-buffered-fallback-design.md`

## Global constraints

- Strict mode remains the default and streams normally.
- This is not a general model router or a custom retry engine.
- Do not add Fable, Haiku, older model families, overload retry chains, prompt rewriting, or local model forwarding.
- Do not execute user-provided benchmark tools during verification.
- This release does not implement partial rollback of the tool bridge.
- Do not enlarge replay retention in this change.
- Preserve user-owned `docs/research/2026-09-06-meridian-head-to-head/` and unrelated Pi configuration.
- Work on `codex/pinned-models-buffered-fallback`; do not merge, push, or restart the running proxy without a subsequent request.
- Auto-buffer ceiling: 64 MiB of retained normalized event payload per public response. Count UTF-8 strings, canonical JSON tool arguments, and signature/redacted block data as admitted, not only at final serialization. Retain no per-delta object list: coalesce adjacent deltas. Exceeding the ceiling raises `fallback_buffer_limit`, discards output, and closes the session. This bounds payload retention, not total Python RSS.

## File/interface map

Create `model_catalog.py` for canonical names, backend IDs, and response mapping.
Create `fallback_policy.py` for transport policy and native target restrictions.
Create `sdk_fallback.py` for native leg state and bounded unpublished output.
Extend `domain.py` with immutable response identity and its event; `TextRequest`
gains resolved policy. Extend existing session objects rather than adding another
registry. Update HTTP renderers, CLI, tests, README, and the two existing local Pi
proxy providers as part of their consuming tasks.

New shared contracts:

```python
# model_catalog.py
def canonical_model(value: str) -> str: ...
def backend_model(value: str) -> str: ...

# fallback_policy.py
type RefusalFallback = Literal["off", "auto"]
def resolve_fallback(values: list[str], default: RefusalFallback) -> RefusalFallback: ...
def native_allowlist(model: str, configured: tuple[str, ...], policy: RefusalFallback) -> tuple[str, ...]: ...

# domain.py: also part of ConversationEvent
@dataclass(frozen=True, slots=True)
class ResponseIdentity:
    requested_model: str
    actual_model: str
    fallback: bool

# SDK factory extensions; omitted defaults retain strict behavior
# SdkSession(..., refusal_fallback="off", allowed_backend_models=(),
#            active_backend_model=None, fallback_provenance=False)
# model remains the canonical requested model; active_backend_model is a pinned
# SDK ID supplied only by validated known-session recovery, never public input.
```

The interface declarations above are signatures, not implementation stubs to
check into production. Every task below defines their behavior and consumers.

## Task 1: Complete the native evidence gate

**Files:** Update `docs/research/2026-09-07-buffered-fallback-evidence.md`.
Private temporary probes stay outside Git; add sanitized fixtures only to
`tests/gateway/test_sdk_fallback.py` when their corresponding parser is tested.

**Consumes:** Existing SDK session option/history builder and approved spec.
**Produces:** Observed model-identity/leg schemas and explicit supported boundaries.

- [x] Observe Opus 5 refusal followed by native Opus 4.8 raw/typed tool output with both exact models in `availableModels`.
- [x] Exclude Opus 4.8 while leaving native fallback enabled; observe normal no-fallback refusal rather than an unconfigured switch.
- [ ] Read the evidence note; independently inspect the installed SDK/runtime version before extending observations.
- [ ] Probe harmless strict text requests for all three exact IDs, one harmless tool-call boundary, and seeded history. Print model IDs/event types only; stop at tools without execution. Record whether `message_start.model` precedes every public-output candidate and whether synthetic refusal frames are distinguishable.
- [ ] Inspect SDK model capability metadata for Opus 4.8; verify accepted low/medium/high/xhigh/max configurations with harmless short requests before advertising those effort levels. If a level fails, document the supported subset rather than assuming Opus 5 parity.
- [ ] Probe nonempty retraction only if a benign captured workload produces it. If absent, document unsupported schemas explicitly; no synthetic invented native event types may enter the parser.
- [ ] Record source/event shapes, probe limits, and evidence gaps. Stop if early model identity or native target restriction cannot meet the spec. A tool-retraction case is a designed explicit rejection, not a reason to build bridge rollback.
- [ ] Commit the evidence/fixtures separately after checking that no private payload, tool argument, credential, or raw explanation is included.

## Task 2: Pinned catalog and resolved fallback policy

**Files:** Create `src/claude_sdk_proxy/model_catalog.py`, `fallback_policy.py`,
`tests/gateway/test_model_catalog.py`, `test_fallback_policy.py`.
Modify `cli.py`, `app.py`, `domain.py`, `thinking.py`, `openai_api.py`,
`anthropic_api.py`, `sdk_session.py`, and their existing tests.

**Consumes:** Task 1 capability evidence.
**Produces:** The catalog/policy contracts above; `TextRequest.refusal_fallback`.

- [ ] Write catalog tests with literal expectations:

```python
@pytest.mark.parametrize("given,public,native", [
    ("sonnet", "sonnet-5", "claude-sonnet-5"),
    ("sonnet-5", "sonnet-5", "claude-sonnet-5"),
    ("opus", "opus-5", "claude-opus-5"),
    ("opus-5", "opus-5", "claude-opus-5"),
    ("opus-4.8", "opus-4.8", "claude-opus-4-8"),
    ("claude-opus-4-8", "opus-4.8", "claude-opus-4-8"),
])
def test_pinned_route(given, public, native):
    assert canonical_model(given) == public
    assert backend_model(given) == native

def test_missing_native_fallback_target_is_rejected():
    with pytest.raises(RequestValidationError):
        native_allowlist("opus-5", ("opus-5",), "auto")

def test_duplicate_policy_header_is_rejected():
    with pytest.raises(RequestValidationError):
        resolve_fallback(["auto", "off"], "off")
```

- [ ] Run `.venv/bin/pytest -q tests/gateway/test_model_catalog.py tests/gateway/test_fallback_policy.py`; confirm RED before production changes.
- [ ] Implement literal canonical/native maps; recognized full IDs outside these three preserve their existing routing. Normalize allowlists before comparison and de-duplicate canonical advertised names while retaining configured order. Unknown names must not gain new capabilities.
- [ ] Resolve the new header once in `_handle`, before session selection. Validate exact `off`/`auto`, including repeated headers using the raw header list. Server default is off; absent header inherits it. CLI default model becomes `sonnet-5`.
- [ ] Pass pinned SDK ID at launch, disable classifier fallback in off mode, and configure exact native `availableModels` in the highest-priority runtime settings layer. Auto mode permits only the observed route with both endpoints configured; direct non-Opus-5 requests do not gain arbitrary routes.
- [ ] Gate the `auto` option as unavailable until Tasks 3–4 are integrated; never temporarily expose unbuffered native fallback. Test native option construction, ambient alias overrides, capability lookup, canonical listings, and invalid policy values in both dialects.
- [ ] Run affected suites and mypy; commit the independently usable pinned-model change with auto still gated.

## Task 3: Native identity, bounded buffering, and leg validation

**Files:** Create `src/claude_sdk_proxy/sdk_fallback.py`,
`tests/gateway/test_sdk_fallback.py`; modify `sdk_session.py`,
`sdk_metadata.py`, `sdk_tool_protocol.py`, `domain.py`, `tool_bridge.py` only
for read-only epoch/activity inspection, and refusal/session tests.

**Consumes:** Catalog, policy, and observed schemas.
**Produces:** `ResponseIdentity` as the first accepted event; auto output is
withheld to a validated public boundary. Strict emits identity before live deltas.

- [ ] Add native-sequence tests based on Task 1: two raw starts with Opus 5 then 4.8, a refusal notice between them, original raw refusal stop, replacement tool boundary. Assert no original-leg output is yielded and only one identity precedes replacement events.
- [ ] Write buffer unit tests against a dedicated collector `FallbackBuffer` with `append(event)`, `discard()`, `release()`, and constructor `limit_bytes=64*1024*1024`:

```python
def test_discard_removes_original_leg():
    buffer = FallbackBuffer(limit_bytes=32)
    buffer.append(TextDelta("discard me"))
    buffer.discard()
    buffer.append(TextDelta("accepted"))
    assert buffer.release() == (TextDelta("accepted"),)

def test_buffer_counts_utf8_before_retaining():
    buffer = FallbackBuffer(limit_bytes=3)
    with pytest.raises(BackendFailure, match="fallback_buffer_limit"):
        buffer.append(TextDelta("éé"))
```

- [ ] Observe RED. Implement the buffer as coalesced event payload, accounting on append and resetting on discard/release. Bound one HTTP response, not the entire SDK conversation. No public headers/deltas escape before an auto boundary.
- [ ] Model the native transaction as current leg → validated refusal notice → raw refusal termination → replacement raw start → accepted boundary. The discarded fallback leg is not the existing no-fallback synthetic refusal transaction: observed fallback emits no synthetic assistant. Reuse low-level usage/identity validation without requiring that absent diagnostic.
- [ ] Verify raw model identity at every message start; check typed assistant identity, except the existing strictly correlated synthetic diagnostic. Missing or conflicting identity fails with `backend_model_mismatch`. Permit only one configured Opus 5→4.8 transition; no second hop.
- [ ] Track all original-leg tool activity before accepting a switch. If present, raise `fallback_tool_rollback_unsupported`; allow existing owner cleanup to close/drain the whole bridge. Tests cover raw tool start, invocation registration, parked callback, no fabricated result, and no leaked IDs/epochs. Do not implement partial bridge reset.
- [ ] Handle only evidenced retraction frame shapes. A notice referencing unknown/already committed IDs or an unsupported retraction schema fails with no buffered output. Retain replacement signed thinking and normalize usage from the accepted leg only.
- [ ] Tests must cover no-fallback auto success/refusal, early model mismatch, post-start strict conflict, buffer overflow, timeout, disconnect, and malformed notices. Existing refusal and normal tool suites remain green. Commit without enabling the public auto option yet.

## Task 4: Session recovery, replay identity, and HTTP publication

**Files:** Modify `sessions.py`, `tool_session_actor.py`, `session_turn.py`,
`session_identity.py`, `app.py`, `openai_api.py`, `anthropic_api.py`,
`http_errors.py`; create `tests/gateway/test_fallback_sessions.py` and
`test_fallback_http.py`; extend `tests/integration/test_official_tool_clients.py`.

**Consumes:** Immutable response identity and native buffered stream.
**Produces:** Fully supported public auto policy, model headers, canonical response
models, transactional recovery, retained replay identity.

- [ ] Add fake-SDK HTTP tests with a blocked generation. Assert no HTTP start/output in auto mode while blocked; strict mode publishes after validated early identity. Release the accepted boundary and assert JSON/SSE model and headers:

```python
assert response.headers["x-claude-proxy-requested-model"] == "opus-5"
assert response.headers["x-claude-proxy-actual-model"] == "opus-4.8"
assert response.headers["x-claude-proxy-fallback"] == "true"
assert response.json["model"] == "opus-4.8"
assert b"discarded original output" not in response.body
```

- [ ] Observe RED. Store requested canonical model, active pinned backend model,
policy, and validated fallback provenance separately in each session. Consume
`ResponseIdentity` without adding it to user transcript blocks. Include resolved
policy in fingerprints/generation matching and retain identity in replay events.
- [ ] Make `_stream_response` wait for identity before rendering start headers,
then render all frames with its actual canonical model. Auto's identity is not
yielded until its accepted boundary, so ordinary streaming machinery can remain.
`_nonstream_response` likewise renders the committed identity. Error paths must
close the native session and never emit a successful terminal marker afterward.
- [ ] Test known downgraded recovery launching 4.8 directly, including complete
pending call/result seed history; test unknown fresh imports launching requested5
with no provenance. Test off/model change only at completed boundaries and
mid-tool 409 without damaging the valid continuation. Implement spec recovery
table through existing transactional replacement methods, not a new registry.
- [ ] Assert replay of a retained switch response and later fallback response
keeps immutable model headers. Exercise stale retry/eviction behavior without
adding durable replay. Ensure response metadata does not alter tool IDs or
signed-thinking identity.
- [ ] Test both official SDKs over real localhost HTTP, JSON/SSE, text/tools,
multi-round result submission, cancellations, and capacity cleanup. All callback
execution is deterministic test code, not execution of benchmark commands.
- [ ] Remove the temporary auto gate only when this task's contracts pass.
Run all gateway/integration tests and commit the complete auto-fallback feature.

## Task 5: User configuration, documentation, and final verification

**Files:** Modify `README.md`, `docs/README.md`, the evidence note, and relevant
Pi integration fixtures. Local `/Users/bwang/.pi/agent/models.json` is user config,
not a repository artifact; edit only its existing proxy providers with approval
required by filesystem permissions. Never print or commit unrelated credentials.

**Consumes:** Verified model capabilities and public HTTP behavior.
**Produces:** Discoverable pinned choices, documented buffering, release evidence.

- [ ] Update Pi's existing two proxy providers to the three explicit names and
verified reasoning/image capabilities. Preserve endpoint URLs, unrelated fields,
extensions, providers, and secrets. Do not globally enable fallback in Pi;
document server default/per-request override.
- [ ] Document exact model mappings, compatibility aliases, CLI/header examples,
delayed SSE behavior, actual/requested headers, fallback provenance across
recovery, bounded replay, and unsupported original-leg tool rollback. Document
64 MiB retained-payload ceiling and client timeout implications.
- [ ] Verify account access with harmless direct model checks. Verify the
authorized failing continuation with auto off and on in isolated sessions;
observe refusal versus replacement model, without executing generated tools.
Record live and synthetic coverage separately, including any unobserved cases.
- [ ] Run the tracked-snapshot release gate (preserve unrelated untracked research):

```bash
task_snapshot=$(mktemp -d /private/tmp/claude-buffered-release.XXXXXX)
git archive HEAD | tar -x -C "$task_snapshot"
ln -s /Users/bwang/Projects/claude-sdk-proxy/.venv "$task_snapshot/.venv"
UV_NO_SYNC=1 PYTHONPATH="$task_snapshot/src" make -C "$task_snapshot" release-offline
```

- [ ] Get independent code review, address important findings with RED tests,
and rerun affected gates. Check for orphaned lifecycle helpers. Commit final
documentation/evidence; report remaining limitations and leave merge/push and
running-proxy restart to the user.

## Self-review and handoff

Catalog and controls: Task 2. Identity and bounded native transaction: Task 3.
Recovery/replay and both wire protocols: Task 4. Pi/docs/release: Task 5.
Native feasibility: Task 1, with positive and negative allowlist observations
already recorded. Cross-boundary tool retraction is explicitly rejected, not an
unimplemented promise. The spec's requested/active identity distinction remains
the source of truth for every task.
