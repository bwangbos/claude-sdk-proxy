# Caller-tool namespace correction

## Reproduction

An Opus/high request from Pi 0.85.1's Anthropic provider emitted signed thinking
and text, then failed before publishing a tool call. An isolated reproduction
using the captured Pi payload reached the exact rejection in
`RawSdkMessageValidator._public_name`: the registered name was
`mcp__caller_tools_v1__web_search`, but the model emitted
`mcp__caller_tools__web_search`. The captured public request contained neither
namespace string. This was not an authentication, allowlist, or refusal failure.

## Correction and boundaries

The production MCP server is now named `caller_tools`; protocol version metadata
remains `1.0.0`. Shared constants drive registration, allowed tool names, raw/typed
validation, callback names, and imported-history fallback names. Unknown tools,
unrelated namespaces, and the old versioned prefix are still rejected. There is
no fuzzy matching, tool alias rewrite, retry, or added prompt instruction.

Caller-facing tool names and public call IDs are unchanged. Activation requires
a proxy restart; no local Pi configuration change is necessary. In-memory SDK
sessions do not survive that restart; clients must resend complete transcripts
and already-executed tool results for recovery. Historical feasibility artifacts
retain their original namespace because they document a different backend.

## Verification

- Before production edits, the new namespace fixtures failed three checks:
  the full tool/callback/result flow failed at the observed validator guard,
  SDK registration used the old name, and bridge invocations used the old name.
- After the correction, those affected SDK/bridge/history files passed 169 tests.
  A separate missing-map history case then failed on the old fallback name;
  fixing it to use the shared prefix brought the affected selection to 170 passes.
- Gateway and Pi/official-client integration coverage passed 860 tests before
  that final history-fallback case was added; lint and mypy (45 source files)
  passed afterward. Final release results are recorded separately below.
- The captured Pi request rerun with the corrected namespace reached a valid
  `tool_use` boundary containing `web_search` and `bash`, with two correlated
  public calls and no protocol failure. The probe stopped at that boundary and
  disconnected; it did not execute either requested operation. The SDK's pending
  callback cancellation diagnostic on disconnect is not an answered tool turn.

The complete result-submission path is covered deterministically through the
real adapter and MCP callback handler. Successful live admission of the captured
request does not guarantee that a model will never invent a different tool name.
No running server, Pi settings, or user research files were changed by this fix.

## Final offline gate

A fresh tracked snapshot of `41ef985` rebuilt the native helpers and passed
`make release-offline`: 676 unit, 222 Darwin lifecycle, 829 gateway, and 32
integration tests (**1,759 total**), with strict markers, skips forbidden, and
warnings treated as errors. Full-snapshot Ruff and mypy (45 source files) passed.
No live model calls were part of that deterministic gate.

Independent read-only review approved the correction with no findings. A
post-verification process check found no orphaned proxy anchor, supervisor, or
probe-child helpers. The running proxy was left untouched; restart it to load
the corrected namespace after choosing how to integrate this branch.
