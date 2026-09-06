# Model and Thinking Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let ordinary API clients, including Pi, select an allowed Claude model and supported thinking setting, including changes between completed turns.

**Architecture:** Keep one endpoint and the existing repeated `--model` allowlist. Normalize both HTTP dialects into one immutable thinking configuration, pass it through to the SDK, and preserve native thinking blocks separately from answer text. Reuse transactional transcript replacement for model/effort changes at completed assistant turns; do not add a second live-settings mutation path.

**Tech Stack:** Python 3.14, Starlette, Claude Agent SDK, pytest, installed Pi 0.85.1, Ruff, mypy.

**Spec:** The user-approved design in this conversation (September 6, 2026), restated in the acceptance contract below. This is a focused extension of existing request/session/stream paths, not a new provider framework.

## Global Constraints

- Work on `codex/model-thinking-controls` in the current checkout, as requested.
- Preserve `docs/research/2026-09-06-meridian-head-to-head/` and unrelated Pi configuration.
- No Pi extension, harness patch, model-per-effort alias, injected reasoning prompt, or local-model forwarding.
- No new dependencies or native lifecycle changes.
- Default requests retain today's disabled-thinking behavior where the model supports it; explicitly unsupported combinations produce actionable validation errors, not silent downgrades.
- Model availability is account-dependent. Do not label an SDK alias or effort combination live-verified without a successful probe.
- Keep system, tool, and dialect changes fail-closed. Relax only the explicitly approved model/thinking changes.
- Never switch during an active HTTP request or an unresolved tool-use loop. Returning tool results continues the same assistant turn, even though it is a new HTTP request.
- Preserve raw usage counters and existing cancellation-safe session replacement/cleanup.
- No deployment, running-proxy restart, GitHub push, or unrelated configuration changes as part of implementation.

## Acceptance Contract

1. Repeated `--model` values appear at `/v1/models` and independently select SDK models.
2. OpenAI accepts `reasoning_effort` with disabled (`none`), `low`, `medium`, `high`, `xhigh`, and `max` settings subject to model capability. Pi's `minimal` is hidden or explicitly mapped, never invented as an SDK effort value.
3. Anthropic accepts `thinking` (`disabled`, `adaptive`, or `enabled` with a positive supported budget) and `output_config.effort`; conflicting options fail before backend creation. Preserve model-dependent budget and maximum-output constraints. Null optional controls mean omitted.
4. Native thinking and signatures are never concatenated into answer text. Anthropic preserves signed/redacted thinking blocks. OpenAI exposes reasoning in Pi's supported reasoning field without falsely claiming that Chat Completions standardizes signed thinking replay.
5. Ordinary same-session tool continuations retain native reasoning. Imported/rebased Anthropic transcripts preserve supplied signed thinking exactly. OpenAI clients may omit reasoning on replay; that must not invalidate an otherwise faithful public transcript. Never fabricate a signature or treat an unsigned OpenAI reasoning string as authenticated native thinking.
6. A new user turn after a completed assistant answer may select a different model or thinking configuration. It uses the existing replacement transaction, preserving the external session ID when the predecessor is unambiguous. Stale retries, ambiguity, active work, and pending tool batches remain guarded.
7. Pi models use normal `models.json` entries and model-specific `thinkingLevelMap`; only verified levels are advertised. Keep OpenAI and Anthropic providers, image support, and unrelated providers intact.

## Task 1: Request Controls and SDK Configuration

**Files:**
- Create `src/claude_sdk_proxy/thinking.py` for normalization and supported model capability policy.
- Modify `domain.py`, `openai_api.py`, `anthropic_api.py`, `sdk_session.py`, `sessions.py`, `session_identity.py`, `session_turn.py`, and `tool_session_actor.py` under `src/claude_sdk_proxy/`.
- Create `tests/gateway/test_thinking_controls.py`; update test factory signatures in existing gateway/integration helpers.

**Interfaces:**
- `ThinkingOptions`: frozen dataclass with `mode: Literal["disabled", "adaptive", "enabled"] = "disabled"`, `effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None`, and `budget_tokens: int | None = None`.
- `parse_openai_thinking(body: Mapping[str, object], model: str) -> ThinkingOptions`.
- `parse_anthropic_thinking(body: Mapping[str, object], model: str) -> ThinkingOptions`.
- `TextRequest.thinking`, `Conversation.thinking`, `ToolSessionActor.thinking`, and keyword-only `SdkSessionFactory(..., thinking: ThinkingOptions = ThinkingOptions())` carry the same normalized value.
- `SdkSession(..., thinking=...)` sets `ClaudeAgentOptions.thinking` and `.effort` explicitly.

- [ ] Add table-driven parser regressions before implementation. Starting test:

```python
def test_openai_high_reaches_normalized_request():
    request = parse_openai_request(
        {"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "hello"}],
         "reasoning_effort": "high"},
        frozenset({"claude-sonnet-4-6"}),
    )
    assert request.thinking.mode == "adaptive"
    assert request.thinking.effort == "high"
```

- [ ] Run `uv run pytest tests/gateway/test_thinking_controls.py -q`; confirm failure is the current unsupported parameter, not fixture breakage.
- [ ] Add explicit cases for omitted/null controls, invalid strings, bool budgets, unsupported model/level pairs, disabled-plus-effort conflicts, unknown nested fields, and normalized equivalence across dialects. Resolve moving aliases against observed SDK model metadata; do not assume every Opus or Sonnet version has identical capabilities. Keep capability policy small and separate from routing.
- [ ] Implement the dataclass, parsers, and request/factory plumbing. Add normalized thinking to fingerprints and session configuration matching so distinct efforts cannot replay an old answer.
- [ ] At the real `SdkSession` boundary, use the existing `FakeSdkClient.capture_options` test seam to assert `thinking={"type": "adaptive"}` and `effort="high"` reach `ClaudeAgentOptions`, and that omitted settings still disable thinking. Fake only the external SDK client, not the parser/session constructor.
- [ ] Run `uv run pytest tests/gateway/test_thinking_controls.py tests/gateway/test_sdk_session.py tests/gateway/test_sessions.py -q`, then Ruff and mypy. Commit the tested slice.

## Task 2: Thinking Streams and Replay

**Files:**
- Modify `domain.py`, `sdk_text_protocol.py`, `sdk_tool_protocol.py`, `sdk_session.py`, `openai_api.py`, `anthropic_api.py`, `openai_tools.py`, `anthropic_tools.py`, `app.py`, `session_turn.py`, `tool_session_actor.py`, `session_identity.py`, and `sdk_history.py`.
- Create `tests/gateway/test_thinking_streams.py` and `tests/gateway/test_thinking_history.py`; extend raw SDK fixtures in `tests/gateway/fakes.py` only as needed.

**Interfaces:**
- Add immutable canonical `ThinkingBlock(thinking: str, signature: str)` and `RedactedThinkingBlock(data: str)` for assistant history.
- Add a typed reasoning event representation which preserves block boundaries, deltas, and signatures. Use `ThinkingDelta(index: int, text: str)` and `ThinkingCompleted(index: int, block: ThinkingBlock | RedactedThinkingBlock)` in `ConversationEvent`; completed blocks commit to history, text deltas only drive streaming.
- Preserve public transcript identity separately from native-only reasoning metadata for OpenAI. Anthropic signed blocks remain part of exact replay identity.

- [ ] Feed handwritten raw SDK frames into the real validators: message start, thinking block start, two thinking deltas, signature delta, block stop, answer text, message delta/stop, typed AssistantMessage. The first regression must fail because the current validators reject the thinking block.

```python
# Consumer-visible assertions for the fixture above:
assert response.json["choices"][0]["message"]["content"] == "answer"
assert response.json["choices"][0]["message"]["reasoning_content"] == "reasoning summary"
assert response.json["usage"]["completion_tokens"] == 17
```

- [ ] Test both no-tools and tool-enabled SDK streams, thinking before tools, interleaved thinking, empty/omitted display content, redacted blocks, malformed signatures/deltas, mismatched typed/raw blocks, and cancellation during thinking. Inspect installed SDK typed representations and Pi's actual parser before finalizing field handling.
- [ ] Implement event validation and dialect serialization. Anthropic must produce correct block indices and signature deltas; OpenAI reasoning chunks must be distinguishable from content chunks. Keep usage from SDK counters, not string lengths.
- [ ] Update response accumulation and transcript commits to preserve reasoning blocks. Update domain validation, image-budget traversal, canonicalization, and SDK history serialization for the new block types; audit exhaustive `else` branches that currently assume every non-text block is a tool result.
- [ ] Add round-trip tests: native signed reasoning -> Anthropic HTTP response -> parser -> seeded history preserves block bytes; OpenAI reasoning -> Pi assistant replay does not corrupt transcript selection; malformed/unsigned reasoning never becomes a native signed block. Native same-session tool continuation must not execute tools twice.
- [ ] Run `uv run pytest tests/gateway/test_thinking_streams.py tests/gateway/test_thinking_history.py tests/gateway/test_sdk_session.py tests/gateway/test_tool_http.py tests/gateway/test_transcript_recovery.py -q`; run lint/types and commit.

## Task 3: Safe Model and Thinking Changes Between Turns

**Files:**
- Modify `sessions.py`, `session_identity.py`, `session_turn.py`, and `tool_session_actor.py`.
- Create `tests/gateway/test_settings_switch.py` using existing text/tool factories and concurrency barriers.

**Interfaces:**
- Keep `SessionRegistry.open_turn(request, explicit_id)` unchanged.
- Introduce a narrow compatibility predicate separating fixed conversation configuration (system/tools/dialect) from replaceable generation configuration (model/thinking).
- Reuse `_new_entry`, `_install_rebase`, `_cancel_rebase`, and `_discard_entry`; do not duplicate ownership/capacity/cancellation logic.

- [ ] Write a regression using an explicit session ID: finish a first answer, append that answer and a new user message, change only model, open a second turn. Assert the second backend receives the new model and full history, the same public ID is returned, and the old backend closes once. Repeat with only effort changed.

```python
changed = replace(first_request, model="opus", messages=(
    *first_request.messages,
    CanonicalMessage.assistant_text("saved answer"),
    CanonicalMessage.user_text("continue"),
))
# After opening/consuming the first turn and opening changed:
assert second_lease.response_headers["X-Claude-Proxy-Session"] == first_id
assert factory.histories[-1] == changed.messages[:-1]
```

- [ ] Run `uv run pytest tests/gateway/test_settings_switch.py -q`; verify the current configuration mismatch is the failing behavior.
- [ ] Implement replacement only after busy/stale/fixed-config checks and at a completed assistant turn. For implicit clients, identify a unique exact transcript predecessor ignoring only model/thinking; reject ambiguity rather than selecting arbitrarily. Keep unrelated new conversations independent.
- [ ] Test unchanged-setting retries, changed-setting stale retries, simultaneous requests, multiple matching predecessors, pending results, invalidated predecessors, startup failure, cancellation during replacement, and capacity/new-owner races. A rejected switch must leave the original session usable.
- [ ] Run `uv run pytest tests/gateway/test_settings_switch.py tests/gateway/test_sessions.py tests/gateway/test_tool_sessions.py tests/gateway/test_transcript_recovery.py -q`; lint/types and commit.

## Task 4: Real Pi Integration, Live Evidence, and Configuration

**Files:**
- Create `tests/fixtures/pi_thinking_client.mjs`, `tests/integration/test_pi_thinking.py`, and `tests/live/test_thinking_controls.py`.
- Reuse `tests/integration/pi_gateway_support.py` subprocess/HTTP lifecycle helpers.
- Update `README.md` with both API dialects, supported controls, switching boundaries, and example Pi configuration.
- Record verified SDK/model combinations in `docs/research/2026-09-06-model-thinking-verification.md`.
- After verification, make only the requested model/thinking edits to the existing local Pi provider entries; preserve credentials and unrelated keys, and make a recoverable backup. Local Pi file edits require filesystem approval.

- [ ] Write a real `pi-ai` client fixture using `streamSimple`, `reasoning: true`, `compat.supportsReasoningEffort: true`, and `thinkingLevelMap`. Drive two completed turns with different model/effort selections through an actual local HTTP server backed by deterministic SDK fixtures. Capture outgoing request controls and incoming Pi thinking/text/tool events.

```javascript
const events = streamSimple(model, context, {
  apiKey: "local-placeholder",
  reasoning: "high",
  maxRetries: 0,
});
// Assert the returned Pi assistant has distinct thinking and text blocks,
// then append it unmodified and make the next request at a different level.
```

- [ ] Verify the fixture's options against the installed Pi types before running. Include OpenAI and Anthropic paths, image-capable model metadata, a full tool loop, and a completed-turn settings switch. Run `uv run pytest tests/integration/test_pi_thinking.py -q` and observe red before implementing the final integration glue.
- [ ] Run bounded live SDK probes for each model/level advertised in the local Pi config. Assert actual SDK options and returned model identity, completed response, thinking handling where returned, a tool continuation, and a post-answer switch. Do not infer effort success from answer wording or require adaptive thinking to appear on every trivial prompt. Use existing `CLAUDE_PROXY_LIVE=1` opt-in pattern and close all clients in `finally`/context managers.
- [ ] If a live combination fails, investigate with its actual diagnostic reason; do not advertise it or silently downgrade it. Record unavailable account models separately from code failures. No unbounded model/effort sweep.
- [ ] Add normal Pi entries for verified models under the existing local providers. Keep `reasoning: true` only where verified, hide unsupported levels via `thinkingLevelMap`, and leave local direct-model providers untouched. If the currently running proxy allowlist lacks the new models, report the needed restart command rather than restarting it without permission.
- [ ] Run the full offline release gate from a fresh tracked snapshot so user-owned untracked research is excluded: `UV_NO_SYNC=1 PYTHONPATH="$task_snapshot/src" make -C "$task_snapshot" release-offline`, with the existing `.venv` symlinked into that snapshot. Run the live test selection separately and report exact pass counts and limitations.
- [ ] Request an independent code review before declaring the feature complete. Address findings with regressions and repeat affected checks. Commit code/tests/docs only; do not stage local research or Pi secrets. Report branch, verification, configuration changes, and whether the running proxy still needs a restart.

## Research and Review Notes

- SDK reference: https://code.claude.com/docs/en/agent-sdk/python#thinkingconfig
- Effort capabilities: https://platform.claude.com/docs/en/build-with-claude/effort
- Thinking/tool replay: https://platform.claude.com/docs/en/docs/build-with-claude/extended-thinking
- Pi model maps: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/models.md
- Installed SDK exposes `set_model` but no equivalent public effort setter; one replacement path is deliberately simpler than two mutation mechanisms.
- Anthropic requires one thinking mode across an assistant turn including its tool-use loop. Therefore a result submission is not a settings-switch boundary. The normal Pi between-turn selector remains supported.
- Plan self-review: all seven acceptance requirements map to Tasks 1–4; no new provider adapter, process manager, routing framework, or native component is introduced.
