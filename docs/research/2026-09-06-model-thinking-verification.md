# Model and thinking-control verification

Date: 2026-09-06

This record separates what the local proxy and installed Pi client sent from
what the subscription backend accepted. It does not infer effort from answer
wording, and a visible thinking summary was not required for a passing probe.

## Environment

- Pi coding agent: `0.85.1`, using its installed `pi-ai` `streamSimple`
- Claude Agent SDK: `0.2.152`
- Claude Code runtime observed during the probes: `2.1.261`
- Proxy aliases: `sonnet` and `opus`
- Pi requests used the normal implicit transcript path. They did not send
  `X-Claude-Proxy-Session`.

Separate direct SDK probes recorded in the task research resolved the aliases
to `claude-sonnet-5` and `claude-opus-5`. The Task 4 Pi matrix below captured
the configured alias and SDK options independently; Pi's `assistant.model`
field only repeats its configured alias and is not evidence of backend model
identity.

## Bounded live matrix

Each advertised model/level tuple was generated once through actual Pi HTTP and
the real SDK. The two transports were interleaved to cover both without running
a duplicate 24-request sweep.

| Configured alias | Pi level | HTTP transport | SDK option | Result |
| --- | --- | --- | --- | --- |
| sonnet | off | OpenAI | disabled / no effort | completed |
| sonnet | low | Anthropic | adaptive / low | completed |
| sonnet | medium | OpenAI | adaptive / medium | completed |
| sonnet | high | Anthropic | adaptive / high | completed |
| sonnet | xhigh | OpenAI | adaptive / xhigh | completed |
| sonnet | max | Anthropic | adaptive / max | completed |
| opus | off | Anthropic | disabled / no effort | completed |
| opus | low | OpenAI | adaptive / low | completed |
| opus | medium | Anthropic | adaptive / medium | completed |
| opus | high | OpenAI | adaptive / high | completed |
| opus | xhigh | Anthropic | adaptive / xhigh | completed |
| opus | max | OpenAI | adaptive / max | completed |

The first matrix run completed all 12 backend calls. Seven assertions passed;
five active Anthropic rows failed because the test expected only
`{"type":"adaptive"}` while Pi correctly preserved
`display: "summarized"`. This was a test expectation error, not an SDK or
backend rejection. After correcting the expectation, a focused rerun of those
five rows passed (`5 passed, 8 deselected`). No second full sweep was run.

Commands:

```bash
CLAUDE_PROXY_LIVE=1 UV_CACHE_DIR=/private/tmp/claude-sdk-proxy-uv-cache \
  uv run pytest tests/live/test_thinking_controls.py -q -vv \
  -k advertised_model_level_matrix

CLAUDE_PROXY_LIVE=1 UV_CACHE_DIR=/private/tmp/claude-sdk-proxy-uv-cache \
  uv run pytest tests/live/test_thinking_controls.py -q -vv \
  -k 'advertised_model_level_matrix and anthropic and not none'
```

For OpenAI, `off` produced `reasoning_effort: "none"`; the other rows produced
their named effort. For Anthropic, active rows produced adaptive thinking with
`display: "summarized"` plus `output_config.effort`; `off` produced disabled
thinking and no effort. The SDK options matched those controls. Simple requests
may return no thinking block, and the OpenAI default display may omit a summary,
so completion and exact transmitted/SDK controls—not visible prose—were the
acceptance evidence.

## Installed Pi replay evidence

The deterministic integration uses the installed `streamSimple` client over a
real temporary loopback HTTP server. Both OpenAI and Anthropic transports cover:

1. Sonnet/high returning interleaved thinking and two parallel tool calls.
2. Tool results and a signed-thinking completed answer.
3. A completed-answer switch to Opus/low.
4. A second, unchanged Opus/low turn.
5. A switch back to Sonnet/high.

Both rows pass, preserve native signed thinking in the backend seed, retain
thinking/text/tool event boundaries, use image-capable Pi model metadata, and
close all SDK clients. The extra unchanged turn matters because Pi retains each
assistant message's producing-model tag; an older assistant may stay flattened
on every later replay, not just the first switch.

Installed Pi exposed one production mismatch before the fix. On a cross-model
turn, Pi's compatibility transform converts nonblank thinking to ordinary text
and drops redacted thinking. The implicit matcher treated that exact public
projection as a different transcript and imported it, losing the stored native
lineage. The focused regression failed for both dialects. The matcher now
recognizes only the exact Pi projection of a stored assistant message while
continuing with the stored native transcript. An edited character does not
match. Pending-tool, fixed-configuration, busy, stale, and ambiguity guards are
unchanged.

Focused evidence after the fix:

```text
tests/gateway/test_thinking_history.py
tests/gateway/test_settings_switch.py
tests/integration/test_pi_thinking.py
44 passed in 0.91s
```

## Live bidirectional post-tool switching

At commit `db0d31c`, actual Pi completed the following sequence through **both**
API transports, using the real subscription SDK and no custom session header:

1. Sonnet/high: two echo tool calls, tool results, and answer `4`.
2. Opus/low: answer `7`.
3. Sonnet/high: answer `15`.

Captured SDK model identities were `claude-sonnet-5`, `claude-opus-5`, and
`claude-sonnet-5`; configured efforts were `high`, `low`, and `high`. The tool
result request retained the first turn's effort. Neither run emitted a refusal
notice, retried a generation, or used a fallback. Both temporary servers and SDK
clients closed normally. The Anthropic check was:

```bash
CLAUDE_PROXY_LIVE=1 .venv/bin/pytest --strict-markers --forbid-skips -W error \
  tests/live/test_thinking_controls.py::test_live_pi_anthropic_bidirectional_switch_roundtrip -q
```

Result: `1 passed in 8.53s`. A separate controller-run OpenAI probe used the same
`LiveSdkFactory`, `serve`, and `run_pi_thinking` helpers with dialect `openai`,
scenario `roundtrip`, first model/effort `sonnet`/`high`, and second model/effort
`opus`/`low`. It exited successfully after checking the resolved models, outgoing
efforts `high/high/low/high`, stop reasons `toolUse/stop/stop/stop`, both tool
results, answers `4/7/15`, absent custom session headers, and no refusal notices.

These successful runs verify bidirectional switching; they do not guarantee
that the upstream service will answer every later request.

## Upstream refusal investigation and handling

The live Anthropic flow completed a Sonnet/high two-tool turn and its answer,
then the SDK emitted `model_refusal_no_fallback` with category
`reasoning_extraction` on the first Opus/low request. No thinking block had been
emitted by the Sonnet tool run, so this occurrence does not support a diagnosis
of invalid cross-model signature serialization. Two continuity-prompt variants
and one final independent-arithmetic variant produced the same upstream
refusal. The final bounded command was:

```bash
CLAUDE_PROXY_LIVE=1 UV_CACHE_DIR=/private/tmp/claude-sdk-proxy-uv-cache \
  uv run pytest \
  tests/live/test_thinking_controls.py::test_live_pi_anthropic_tool_replay_and_bidirectional_switch \
  -q -vv
```

Result: `1 failed in 7.40s`, at the first Opus turn after the successful Sonnet
tool continuation. The test remains an opt-in, honest detector; it is not
skipped, retried, or weakened. This was the initial result, before the independent
diagnosis and successful bidirectional runs recorded above.

The SDK history importer currently attributes historical assistant seed entries
to the target model. That is a provenance risk worth monitoring, but it was not
changed without evidence tying it to this refusal. Native signed history was
not stripped or converted as a workaround.

Independent native SDK controls subsequently reproduced the same refusal on an
unchanged Opus turn, including a pure Opus session that never switched models,
imported history, or resumed a session. A same-client public `set_model` control
also reproduced it. Faithful native-history and proxy-imported-history controls
both completed the first follow-up before the later refusal. Consequently,
switching, resume, importer provenance, and signed-history conversion are not
necessary causes; no evidence justified changing them as a workaround.

The observed SDK refusal has a complete raw message boundary with authoritative
input/cache usage and zero output, plus a correlated refusal notice, synthetic
diagnostic assistant, and terminal result. The proxy now recognizes that exact
validated sequence and exposes Anthropic `stop_reason: refusal` or OpenAI
`finish_reason: content_filter`. It does not expose the synthetic diagnostic as
an answer, substitute its zero usage, or retry/downgrade the model. Malformed,
uncorrelated, partial-output, and ordinary SDK error sequences still fail closed.
Regression coverage includes JSON/SSE, both dialects, replay/recovery, tool
boundaries, and cleanup.

The final review correction extends this deterministic evidence to empty refusals
with tools configured, including refusal after a completed tool-result boundary.
Anthropic JSON and assembled SSE consistently use `content: []`; replay normalizes
that empty assistant array to the existing canonical singleton empty text block.
The same singleton is accepted in either dialect because public transcripts cannot
authenticate a refusal reason. Empty users, malformed blocks, multiple empty text
blocks, incomplete tool results, and ordinary blockless successful SDK output are
still rejected. Tests cover exact retry without generation, implicit and explicit
continuation, model/effort switches with signed native history and seed preservation,
raw usage, absence of diagnostic text, durable state, and cleanup. Real loopback
HTTP exercises JSON/SSE in both dialects with and without tools. Non-string
`thinking.type` containers/scalars fail as request validation before session creation;
the raw tool-content refusal negative now has a correctly initialized positive
control. These are deterministic review-fix checks; prior live observations are
unchanged. The completed final release gate is recorded below.

Anthropic documents that automated Opus safeguards can flag normal conversations;
the account-specific trigger here remains unknown. See [Anthropic's explanation
of Opus model safeguards](https://support.claude.com/en/articles/16049681-why-claude-switched-models-in-your-conversation-with-opus-5).

## Final offline release gate and review

Source/test revision: `94a1769`. The controller exported only tracked files to a
fresh temporary snapshot, linked the installed environment, and ran:

```bash
UV_NO_SYNC=1 PYTHONPATH=/private/tmp/claude-thinking-final.fa0fp1/src \
  make -C /private/tmp/claude-thinking-final.fa0fp1 release-offline
```

The gate rebuilt native helpers and exited successfully: **1,757 tests passed**
(676 unit, 222 Darwin, 827 gateway, 32 integration), with strict markers, no
skips, and warnings treated as errors. Ruff passed for the full tracked snapshot;
mypy reported no issues in 44 source files. The post-run process check found no
proxy anchor, supervisor, or probe-child helpers left running.

The independent whole-branch reviewer identified three Important boundary bugs
and two minor issues. The grouped correction addressed tool-enabled refusal
handling, faithful Anthropic empty-refusal replay, malformed thinking-type
validation, README text selection, and the refusal-negative fixture. Scoped
re-review approved all five at `94a1769`, with no new breakage or remaining
findings. Live evidence remains the bounded matrix and successful bidirectional
runs above; the upstream refusal limitation is not claimed eliminated.

The feature was merged and pushed to `main` at `0df4a88`. A fresh tracked-snapshot
release gate on that merged revision again passed all 1,757 tests, Ruff, and mypy;
the post-run process check found no leftover proxy helpers.

## Local activation status

- The README configuration advertises only `off`, `low`, `medium`, `high`,
  `xhigh`, and `max`; Pi's `minimal` slot maps to `null` and is hidden.
- Model or effort changes are allowed only after a completed assistant answer,
  never while tool results are pending. System prompt, tool definitions, and API
  dialect remain fixed for a conversation.
- The OpenAI transport cannot authenticate public reasoning metadata. The proxy
  retains native signed history when it recognizes an exact replay.
- No local Pi user configuration was written and no running proxy was restarted
  during Task 4. Apply the reviewed README entries with a recoverable backup,
  then restart the proxy with `--model sonnet --model opus` and restart Pi.
