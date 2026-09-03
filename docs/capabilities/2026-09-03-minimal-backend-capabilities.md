# Minimal backend capability checkpoint

Date: 2026-09-03

This checkpoint evaluates Claude Agent SDK 0.2.148 and Claude CLI 2.1.258 as
possible trusted-local transports. It is a capability result, not an HTTP
server authorization.

## Deterministic findings

Both the Agent SDK and `claude -p` construct an inspectably isolated prompt for
one user turn. Their adapters disable documented ambient tools, settings,
skills, plugins, MCP servers, persistence, and slash-command behavior where
the corresponding surface exposes those controls.

Authentication and real streaming remain `untested` until an operator
explicitly runs the opt-in live probe. Deterministic tests use injected async
fakes and neither discover credentials nor invoke Claude.

The documented input surfaces inspected for Agent SDK 0.2.148 and Claude CLI
2.1.258 do not support exact arbitrary assistant-history replay. They also do
not accept caller-defined raw API tool schemas without proxy-executed
MCP/built-in tools or prompt emulation; both alternatives are prohibited by
the approved design.

Therefore `compatibility_proxy_viable` and `agent_harness_viable` are false for
both backends at this checkpoint, even if a later live single-turn text probe
passes. Do not begin the agent-harness HTTP compatibility implementation unless
a documented mechanism changes one or both failed capabilities.

## Structural result

| Backend | Authentication | Streaming | Prompt construction | Multi-turn | Structured tools | Compatibility proxy | Agent harness |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Agent SDK | untested | untested | pass | fail | fail | false | false |
| `claude -p` | untested | untested | pass | fail | fail | false | false |

A successful live probe can establish only `single_turn_text_viable`. It does
not change either structural failure or authorize the compatibility HTTP MVP.

## Separate operator approval

The authenticated probes are side-effecting and were not run during this
implementation. With separate approval, an operator may run:

```bash
CLAUDE_PROXY_LIVE=1 CLAUDE_PROXY_MODEL=sonnet uv run claude-proxy-capabilities --backend agent-sdk --live --model sonnet --json
CLAUDE_PROXY_LIVE=1 CLAUDE_PROXY_MODEL=sonnet uv run claude-proxy-capabilities --backend claude-p --live --model sonnet --json
```

`sonnet` is the installed CLI's documented configurable alias. The operator
may authorize a different configured model. Live output must not be committed
until it has been redacted and reviewed.
