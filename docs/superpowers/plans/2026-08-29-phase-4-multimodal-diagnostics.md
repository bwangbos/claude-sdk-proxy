# Phase 4 Multimodal and Diagnostic Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add only empirically faithful base64 image/document inputs, extend the existing redacted diagnostics for binary-content lifecycle evidence, and produce comparative latency/resource/prompt-purity evidence for the finished proxy.

**Architecture:** New canonical binary-source blocks are allowlisted by dialect and converted only through documented Agent SDK content forms proven by live fixtures. Structured diagnostics observe existing boundaries through a redacting sink without changing requests. A separate benchmark command runs controlled prompt suites and writes local JSON reports that are never telemetry.

**Tech Stack:** Existing Phase 2 or Phase 3 stack, standard-library logging/resource/time, pytest, optional psutil for portable child-process memory sampling.

**Spec:** `docs/superpowers/specs/2026-08-29-claude-subscription-api-proxy-design.md`

## Global Constraints

- Text proxy phases must pass before this plan starts; tool support remains independently gated.
- Add one content type at a time and keep it disabled unless its live Agent SDK and dialect conformance tests pass.
- Accept base64 payloads only; reject remote URLs, filesystem paths, audio, citations, search results, and unverified blocks.
- Enforce decoded byte limits before allocating SDK input objects; never log or persist encoded/decoded content.
- Diagnostics are local, disabled by default, redacted regardless of content-debug mode, and must not add model calls.
- Benchmarks compare shapes/latency/resources, not deterministic response text.
- A raw Anthropic comparison runs only when the user separately supplies a legitimate Platform API key; subscription credentials are never reused.

## File Map

- `src/claude_sdk_proxy/domain.py`: canonical image/document blocks.
- `src/claude_sdk_proxy/anthropic.py`: Anthropic image/document validation.
- `src/claude_sdk_proxy/openai_adapter.py`: allowlisted OpenAI image input mapping.
- `src/claude_sdk_proxy/backend.py`: documented SDK content-block conversion.
- `src/claude_sdk_proxy/diagnostics.py`: existing Phase 1 redaction/events extended with binary metadata only.
- `src/claude_sdk_proxy/content_gates.py`: versioned multimodal evidence loading and fail-closed validation.
- `src/claude_sdk_proxy/benchmarks.py`: local benchmark runner and report types.
- `src/claude_sdk_proxy/server_cli.py`: `benchmark` command.
- `tests/unit/test_multimodal.py`: decoding/limit/content tests.
- `tests/unit/test_diagnostics.py`: binary redaction and no-content defaults.
- `tests/integration/test_multimodal.py`: public dialect fixtures.
- `tests/live/test_multimodal_backend.py`: real SDK conformance.
- `tests/integration/test_diagnostics.py`: request/event/latency log coverage.
- `tests/live/test_benchmarks.py`: opt-in comparative benchmark smoke test.
- `docs/multimodal.md`: exact accepted forms and limits.
- `docs/feasibility/validated-content.json`: generated image/document booleans tied to SDK/CLI/model evidence.
- `docs/benchmarks.md`: methodology and interpretation.

---

### Task 1: Extend Structured Diagnostics for Binary Content

**Files:**
- Modify: `src/claude_sdk_proxy/diagnostics.py`
- Modify: `src/claude_sdk_proxy/app.py`
- Modify: `src/claude_sdk_proxy/backend.py`
- Modify: `src/claude_sdk_proxy/session.py`
- Modify: `tests/unit/test_diagnostics.py`
- Modify: `tests/integration/test_diagnostics.py`

**Interfaces:**
- Consumes: request/session/backend lifecycle boundaries.
- Produces: binary-safe extensions to the Phase 1 `DiagnosticSink`, `Redactor`, `LatencyTrace`, and JSON-line lifecycle events.

- [ ] **Step 1: Write redaction tests with canary secrets**

```python
# tests/unit/test_diagnostics.py
from claude_sdk_proxy.diagnostics import Redactor


def test_redactor_removes_headers_tokens_and_binary_content_by_default() -> None:
    value = {
        "authorization": "Bearer secret-123",
        "x-api-key": "local-secret",
        "messages": [{"role": "user", "content": "private prompt"}],
        "image": {"media_type": "image/png", "data": "private-base64"},
        "document": {"media_type": "application/pdf", "decoded": b"private"},
        "model": "sonnet",
    }
    assert Redactor(include_content=False).clean(value) == {
        "authorization": "[REDACTED]",
        "x-api-key": "[REDACTED]",
        "messages": "[CONTENT REDACTED]",
        "image": "[BINARY REDACTED]",
        "document": "[BINARY REDACTED]",
        "model": "sonnet",
    }
```

- [ ] **Step 2: Extend recursive redaction without weakening Phase 1 guarantees**

Extend `Redactor.clean()` to replace encoded image/document data, decoded bytes, filenames, URLs, and binary exceptions with fixed markers at every nesting depth. Even `PROXY_DEBUG_CONTENT=1` may expose caller text only; it never exposes binary bytes/base64. Preserve Phase 1 credential, environment, stderr, and exception redaction. `DiagnosticSink` remains local-only and network-handler-free.

- [ ] **Step 3: Extend the existing monotonic spans**

Add binary-validation start/end, decode completion, backend structured-turn send, first SDK event, public terminal, SDK result, iterator completion, and actor commit/loss marks to the existing trace. Log only media type, encoded/decoded byte counts, digest prefix, duration, queue high-water bytes, backend process count, and terminal state. Instrument `session.py` because it owns commit/loss timing.

- [ ] **Step 4: Instrument without changing request content**

Emit incoming_request, normalized_request, sdk_options, sdk_input, sdk_event, public_event, warning, and latency events only when `PROXY_DEBUG=1`. Tests compare backend inputs with debug on/off for deep equality and assert no extra backend call or title request.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/unit/test_diagnostics.py tests/integration/test_diagnostics.py -v`

Expected: PASS with secret canaries absent from captured logs.

```bash
git add src/claude_sdk_proxy/diagnostics.py src/claude_sdk_proxy/app.py src/claude_sdk_proxy/backend.py src/claude_sdk_proxy/session.py tests/unit/test_diagnostics.py tests/integration/test_diagnostics.py
git commit -m "feat: extend diagnostics for binary content"
```

---

### Task 2: Add Canonical Base64 Image Inputs

**Files:**
- Modify: `src/claude_sdk_proxy/domain.py`
- Modify: `src/claude_sdk_proxy/anthropic.py`
- Modify: `src/claude_sdk_proxy/openai_adapter.py`
- Modify: `src/claude_sdk_proxy/backend.py`
- Create: `src/claude_sdk_proxy/content_gates.py`
- Modify: `src/claude_sdk_proxy/server_cli.py`
- Create: `tests/unit/test_multimodal.py`
- Create: `tests/unit/test_content_gates.py`
- Create: `tests/integration/test_multimodal.py`
- Create: `tests/live/test_multimodal_backend.py`
- Create: `docs/feasibility/validated-content.json`

**Interfaces:**
- Consumes: existing content union and pinned SDK public content-block type.
- Produces: `ImageBlock`, validated Anthropic base64 image sources, allowlisted OpenAI data-URL images, and a versioned fail-closed content evidence artifact.

- [ ] **Step 1: Write decoding and size-limit tests**

```python
# tests/unit/test_multimodal.py
import pytest

from claude_sdk_proxy.domain import ImageBlock, ProxyError


def test_image_decodes_strict_base64_once() -> None:
    image = ImageBlock.from_base64(media_type="image/png", data="iVBORw0KGgo=")
    assert image.media_type == "image/png"
    assert image.decoded == b"\x89PNG\r\n\x1a\n"


def test_image_rejects_oversized_decoded_payload() -> None:
    with pytest.raises(ProxyError, match="image exceeds"):
        ImageBlock.from_base64(
            media_type="image/png", data="YWFhYQ==", max_decoded_bytes=3
        )
```

- [ ] **Step 2: Implement an immutable binary block**

Decode with `base64.b64decode(data, validate=True)`, allow only `image/jpeg`, `image/png`, `image/gif`, and `image/webp`, enforce configured encoded and decoded limits, and keep decoded bytes out of model dumps/logs. Hash bytes for transcript validation; do not retain duplicate encoded and decoded representations after backend construction.

- [ ] **Step 3: Add the fail-closed content evidence schema**

`validated-content.json` contains only `schema_version`, exact Agent SDK/CLI versions, `generated_at`, and model-keyed `image`/`document` booleans. Beside each boolean it stores either `null` or an evidence record with the gate's validation UTC timestamp and SHA-256 digest of the canonical redacted live-probe result. `content_gates.py` rejects missing files, unknown schema versions, version mismatch, malformed booleans, true values without evidence, unknown models, or evidence digest mismatch. The initial checked-in artifact has both booleans `false` and null evidence; only `claude-proxy content-gate --type image|document --model ... --output docs/feasibility/validated-content.json` may atomically rewrite it after the corresponding live probe passes. The CLI preserves the other content type's prior proven value only when its versions/model/evidence digest still match.

Tests prove hand-edited, stale-version, stale-model, absent, and partial artifacts fail closed. Unit/integration fixtures inject a separately marked test-only evidence object so tests never depend on a developer's live artifact. The parsers call the gate before accepting/decoding an image or document, and capabilities read the same production artifact; disabled content returns `400 unsupported_content_type` before backend/session mutation.

- [ ] **Step 4: Implement dialect parsing and backend mapping**

Anthropic accepts only `{type:"image", source:{type:"base64", media_type, data}}`. OpenAI accepts only `image_url` values beginning with `data:image/png;base64,`, `data:image/jpeg;base64,`, `data:image/gif;base64,`, or `data:image/webp;base64,` and rejects HTTP(S). Convert the canonical block through the existing `send_user_turn()` structured-message mapping to the exact documented asynchronous raw SDK image block proven by the pinned live probe; never convert images into filenames or textual descriptions.

- [ ] **Step 5: Run the live image conformance gate and persist evidence**

Generate two tiny fixtures at test time from deterministic bytes: a solid red image that visibly renders randomized token `IMAGE-CANARY-<nonce>` in high-contrast text and a solid blue negative-control image rendering a different nonce. In separate fresh sessions, request an exact compact JSON answer containing the dominant color and visible token. The positive probe passes only if the output contains the positive nonce and `red`, never the negative nonce, while native event/usage/stop structure remains valid. Run a dropped-block negative control through an intentionally broken test mapper and require the semantic assertion to fail, proving that text-only success or image omission cannot set the gate. Store only booleans, fixture hashes, and output hashes—not image data or response content. Run:

`RUN_LIVE_CLAUDE_TESTS=1 CLAUDE_PROXY_TEST_MODEL=sonnet uv run pytest tests/live/test_multimodal_backend.py -v -k image -s`

Expected: PASS with positive semantic delivery, a failing omission negative control, no filesystem writes, and no built-in tool exposure. On failure, keep image capability false and do not advertise it.

Then run: `RUN_LIVE_CLAUDE_TESTS=1 CLAUDE_PROXY_TEST_MODEL=sonnet uv run claude-proxy content-gate --type image --model sonnet --output docs/feasibility/validated-content.json`.

Expected: the writer reruns or consumes cryptographically bound results from that exact invocation, atomically records `image=true` for the model, and leaves `document=false`. A skipped test, stale report, or manually supplied boolean is rejected.

- [ ] **Step 6: Run tests and commit**

Run: `uv run pytest tests/unit/test_multimodal.py tests/unit/test_content_gates.py tests/integration/test_multimodal.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/domain.py src/claude_sdk_proxy/anthropic.py src/claude_sdk_proxy/openai_adapter.py src/claude_sdk_proxy/backend.py src/claude_sdk_proxy/content_gates.py src/claude_sdk_proxy/server_cli.py tests/unit/test_multimodal.py tests/unit/test_content_gates.py tests/integration/test_multimodal.py tests/live/test_multimodal_backend.py docs/feasibility/validated-content.json
git commit -m "feat: add gated base64 image inputs"
```

---

### Task 3: Add Anthropic Base64 Document Inputs

**Files:**
- Modify: `src/claude_sdk_proxy/domain.py`
- Modify: `src/claude_sdk_proxy/anthropic.py`
- Modify: `src/claude_sdk_proxy/backend.py`
- Modify: `tests/unit/test_multimodal.py`
- Modify: `tests/integration/test_multimodal.py`
- Modify: `tests/live/test_multimodal_backend.py`
- Modify: `docs/feasibility/validated-content.json`

**Interfaces:**
- Consumes: binary-block validation pattern from Task 2.
- Produces: `DocumentBlock` and Anthropic-only base64 PDF support.

- [ ] **Step 1: Add PDF signature/media/limit tests**

Require `application/pdf`, strict base64, a decoded `%PDF-` prefix, configured size limits, optional caller title/context fields only if the pinned SDK preserves them exactly, and rejection of URL/plain-text/custom document sources.

- [ ] **Step 2: Implement the documented SDK document mapping and gate rejection**

Add `DocumentBlock` with private decoded bytes and stable digest. Before decoding, require `document=true` for the exact model in `validated-content.json`; otherwise reject before session/backend mutation. Map the Anthropic document source through `send_user_turn()` directly into the pinned SDK's documented asynchronous raw document input block. Do not extract PDF text, invoke filesystem tools, write a temporary file, or implement OpenAI document input.

- [ ] **Step 3: Run the live document gate**

Generate `one-page.pdf` with a randomized, clearly rendered literal `DOCUMENT-CANARY-<nonce>` and a second negative-control PDF with a different nonce. In separate fresh sessions, require the response to return exactly the positive literal and never the negative literal. An intentionally omitted/corrupted-document mapper must fail the semantic assertion. The gate requires both semantic delivery and native event/usage/stop flow, plus no state writes or tools; acceptance alone cannot pass. Persist only fixture/output hashes and booleans. Run:

`RUN_LIVE_CLAUDE_TESTS=1 CLAUDE_PROXY_TEST_MODEL=sonnet uv run pytest tests/live/test_multimodal_backend.py -v -k document -s`

Expected: PASS. On failure, document capability stays false.

Then run: `RUN_LIVE_CLAUDE_TESTS=1 CLAUDE_PROXY_TEST_MODEL=sonnet uv run claude-proxy content-gate --type document --model sonnet --output docs/feasibility/validated-content.json`.

Expected: the bound writer records `document=true` without weakening the existing image evidence. A skipped, stale, mismatched-version, or mismatched-model run cannot update the artifact.

- [ ] **Step 4: Run tests and commit**

Run: `uv run pytest tests/unit/test_multimodal.py tests/integration/test_multimodal.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/domain.py src/claude_sdk_proxy/anthropic.py src/claude_sdk_proxy/backend.py tests/unit/test_multimodal.py tests/integration/test_multimodal.py tests/live/test_multimodal_backend.py docs/feasibility/validated-content.json
git commit -m "feat: add gated Anthropic PDF inputs"
```

---

### Task 4: Build Reproducible Local Benchmarks

**Files:**
- Modify: `pyproject.toml`
- Create: `src/claude_sdk_proxy/benchmarks.py`
- Modify: `src/claude_sdk_proxy/server_cli.py`
- Create: `tests/unit/test_benchmarks.py`
- Create: `tests/live/test_benchmarks.py`
- Create: `docs/benchmarks.md`

**Interfaces:**
- Consumes: proxy HTTP APIs and optional separate raw Anthropic API configuration.
- Produces: `BenchmarkSample`, `BenchmarkReport`, and `claude-proxy benchmark`.

- [ ] **Step 1: Add resource sampling dependency**

Add `"psutil>=7,<8"` to the dev extra and run `uv lock`.

- [ ] **Step 2: Write report aggregation tests**

```python
# tests/unit/test_benchmarks.py
from claude_sdk_proxy.benchmarks import BenchmarkSample, summarize


def test_summary_uses_median_and_preserves_each_sample() -> None:
    samples = [
        BenchmarkSample("proxy", 10, 20, 30, 1, 100),
        BenchmarkSample("proxy", 20, 30, 40, 1, 110),
        BenchmarkSample("proxy", 30, 40, 50, 1, 120),
    ]
    report = summarize(samples)
    assert report.median_first_token_ms == 30
    assert len(report.samples) == 3
```

- [ ] **Step 3: Implement benchmark collection**

For each target, run one warmup and five measured requests using the identical caller system/user text and model alias. Record process startup, first byte, first model event when observable, first text token, total time, subprocess count, and peak resident memory. Store no response text; store only byte counts, stop reason, usage, and a success boolean.

Targets are `proxy`, `normal_claude_code`, and optional `raw_anthropic`. The raw target requires a separately supplied `ANTHROPIC_PLATFORM_API_KEY`; reject obvious subscription/local proxy token formats and never fall back to Claude login.

- [ ] **Step 4: Add the benchmark CLI and docs**

CLI syntax:

```text
claude-proxy benchmark --model sonnet --output probe-output/benchmark.json
```

Document hardware/OS/runtime versions, warmup, sample count, nondeterminism, subscription-limit effects, and why text equality is not a correctness metric.

- [ ] **Step 5: Run the live benchmark smoke test**

Run: `RUN_LIVE_CLAUDE_TESTS=1 CLAUDE_PROXY_TEST_MODEL=sonnet uv run pytest tests/live/test_benchmarks.py -v -s`

Expected: proxy and normal-Claude-Code targets each produce valid timings, process counts, memory values, stop reasons, and no stored response content. Raw target skips unless a separate Platform key is explicitly present.

- [ ] **Step 6: Commit benchmark support**

```bash
git add pyproject.toml uv.lock src/claude_sdk_proxy/benchmarks.py src/claude_sdk_proxy/server_cli.py tests/unit/test_benchmarks.py tests/live/test_benchmarks.py docs/benchmarks.md
git commit -m "feat: add local proxy comparison benchmarks"
```

---

### Task 5: Publish the Final Capability and Verification Matrix

**Files:**
- Modify: `src/claude_sdk_proxy/control.py`
- Modify: `README.md`
- Create: `docs/verification.md`
- Create: `tests/integration/test_capabilities.py`

**Interfaces:**
- Consumes: feasibility and content gate results.
- Produces: truthful `/_proxy/capabilities`, final operator documentation, and release verification commands.

- [ ] **Step 1: Write capability-manifest tests**

Assert text reflects every Phase 0 core gate; external tools require the conjunction of every named lifecycle gate for the exact model in `tool_gates_by_model`; images/documents reflect only the exact model-keyed values in `validated-content.json`; thinking/effort reflects the Phase 0 per-model matrix; unsupported sampling fields remain false; strict and compatibility ignored-field lists are exact; SDK/CLI versions and personal-local scope are present; no credential/session details are exposed. Flip every gate false one at a time and omit a model entirely; require the corresponding model capability and parser acceptance to disappear.

- [ ] **Step 2: Implement capability assembly from committed evidence**

Load only the versioned Phase 0 environment manifest and `validated-content.json` through their strict loaders at startup. Verify schema versions, SDK/CLI versions, configured models, timestamps, and evidence digests. Do not dynamically probe undocumented model endpoints. Startup fails if enabled configuration contradicts evidence. `/v1/models` lists configured models separately from `/_proxy/capabilities`.

- [ ] **Step 3: Complete verification documentation**

Document unit/integration/live commands, prompt/disk/compaction/attribution canaries, official-client fixtures, tool lifecycle matrix, multimodal gates, benchmark methodology, expected skips, and the exact rule that any upgrade invalidates live evidence until rerun.

- [ ] **Step 4: Run final verification**

Run: `uv run pytest -v`

Expected: all non-live tests PASS.

Run: `RUN_LIVE_CLAUDE_TESTS=1 CLAUDE_PROXY_TEST_MODEL=sonnet uv run pytest tests/live -v -s`

Expected: every capability advertised as true has a passing live gate.

Run: `uv run ruff check .`

Expected: no findings.

Run: `uv run mypy src/claude_sdk_proxy`

Expected: no errors.

- [ ] **Step 5: Commit final capability documentation**

```bash
git add src/claude_sdk_proxy/control.py README.md docs/verification.md tests/integration/test_capabilities.py
git commit -m "docs: publish verified proxy capabilities"
```
