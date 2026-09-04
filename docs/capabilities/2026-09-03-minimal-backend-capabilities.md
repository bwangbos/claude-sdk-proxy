# Minimal backend capability checkpoint

Date: 2026-09-03

> **Superseded:** The Agent SDK authentication/streaming failure and blanket
> structured-tools verdict below were invalidated by host-context probes and
> current SDK research later the same day. See the
> [fresh reassessment](../research/2026-09-03-subscription-proxy-reassessment.md).
> This file is retained as the historical checkpoint that led to the new work.

This checkpoint evaluates Claude Agent SDK 0.2.148, its bundled Claude CLI
2.1.251, and the installed Claude CLI 2.1.258 as possible trusted-local
transports. It is a capability result, not an HTTP server authorization.

## Deterministic findings

Both the Agent SDK and `claude -p` construct an inspectably isolated prompt for
one user turn. Their adapters disable documented ambient tools, settings,
skills, plugins, MCP servers, persistence, and slash-command behavior where
the corresponding surface exposes those controls.

Deterministic tests use injected async fakes and neither discover credentials
nor invoke Claude. Separately authorized live probes were run on 2026-09-03
with the `sonnet` alias. No prompt, generated text, credential, or raw process
diagnostic is retained in this report.

The documented input surfaces inspected for Agent SDK 0.2.148 and Claude CLI
2.1.258 do not support exact arbitrary assistant-history replay. They also do
not accept caller-defined raw API tool schemas without proxy-executed
MCP/built-in tools or prompt emulation; both alternatives are prohibited by
the approved design.

Therefore `compatibility_proxy_viable` and `agent_harness_viable` are false for
both backends at this checkpoint. Do not begin the agent-harness HTTP
compatibility implementation unless a documented mechanism changes one or both
failed capabilities.

## Observed result

| Backend | Authentication | Streaming | Prompt construction | Multi-turn | Structured tools | Compatibility proxy | Agent harness |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Agent SDK | fail | not reached (probe reports fail) | pass | fail | fail | false | false |
| `claude -p` | pass | pass | pass | fail | fail | false | false |

The direct `claude -p` probe emitted two text deltas, produced the required
exact output, reached its first delta in 1,551 ms, and completed in 1,866 ms.
It establishes `single_turn_text_viable=true` for that backend only.

The Agent SDK completed its initialization handshake, then returned an
`AssistantMessage` classified as `authentication_failed` and an error
`ResultMessage` with `terminal_reason=api_error`. The same result occurred with
both the SDK-bundled CLI and the authenticated standalone CLI selected through
`ClaudeAgentOptions.cli_path`. No `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
or `CLAUDE_CODE_OAUTH_TOKEN` environment variable was present. Taken together,
these observations localize the rejection to the Agent SDK execution path in
the current environment; its underlying cause remains undetermined. They do
not mean that the operator's normal Claude CLI login is absent. The generic probe payload
reports `streaming=fail` on any backend failure, but authentication stopped this
run before text streaming was independently reached or assessed.

After the adapter drained the SDK's terminal error before returning, the final
live confirmation failed cleanly in 326 ms with zero text deltas, one redacted
`BackendFailure` evidence value, and no stdout/stderr diagnostic leakage.

Anthropic's current help-center statement says Agent SDK, `claude -p`, and
third-party Agent SDK usage can draw from subscription limits. One plausible
inference is an account/product-state discrepancy, but the evidence does not
establish the cause; in particular, it does not show that subscription-backed
Agent SDK use is universally unsupported:
[Use the Claude Agent SDK with your Claude plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan).

## Reproduction

These commands invoke Claude and require separate operator approval before any
future rerun:

```bash
CLAUDE_PROXY_LIVE=1 CLAUDE_PROXY_MODEL=sonnet uv run claude-proxy-capabilities --backend agent-sdk --live --model sonnet --json
CLAUDE_PROXY_LIVE=1 CLAUDE_PROXY_MODEL=sonnet uv run claude-proxy-capabilities --backend claude-p --live --model sonnet --json
```

`sonnet` is the installed CLI's documented configurable alias. The operator
may authorize a different configured model. A successful live probe changes
only the single-turn transport gate; it cannot change either structural
failure or authorize the compatibility HTTP MVP.

## Product decision

The closest honest subscription-backed transport available on this machine is
the direct `claude -p` adapter. It is suitable for a deliberately labeled
single-turn text service. It cannot truthfully present itself as an OpenAI- or
Anthropic-compatible agent-harness endpoint because arbitrary assistant
history and caller-defined tool schemas cannot cross the backend boundary.
