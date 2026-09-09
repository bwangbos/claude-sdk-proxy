# Direct ChatGPT backend verification — 2026-09-08

This record separates deterministic observations at commit-under-test from live
claims. It does not replace the usage and limitation contract in the
[opt-in guide](../openai-subscription.md).

## Deterministically observed

- The complete subscription test directory passed: 217 tests using mock HTTP,
  fake credentials, and local client accumulators. This covers authentication
  storage contracts, fixed headers/endpoint, translation, replay bounds,
  cancellation, lifecycle, usage shapes, both frontend dialects, and CLI wiring.
- A stock Anthropic `0.125.0` stream accumulator assembled interleaved reasoning,
  answer text, and the portable carrier. Its normal Pydantic request
  serialization (`exclude_unset=True`, `mode="json"`, `by_alias=True`) was
  submitted as a continuation; the next native input preserved the reasoning
  ciphertext plus message ID and phase.
- The mock-backed stock Pi image/tool smoke passed with a temporary
  `PI_CODING_AGENT_DIR`; the user's Pi configuration was not edited.
- Ruff, strict mypy, and whitespace checks passed.
- The final full non-live run produced 2,226 passes, 76 live deselections, and
  the one supplied reset/domain alternation baseline failure. Its 44 macOS fork
  deprecation warnings are also inherited baseline output.

Evidence is preserved in
`.superpowers/sdd/2026-09-08-direct-openai/task-4-report.md` and
`task-4-full-suite.log` in the implementation worktree.

## Not live-verified

No Task 4 direct-ChatGPT live test was run. In particular, this checkpoint does
not establish current authorization for the unofficial endpoint, model access,
included-plan accounting or credit behavior, actual tool/image/reasoning output,
live compaction, or live cancellation behavior. No latency or performance claim
is made.

After the operator explicitly completes `uv run claude-proxy login openai`, the
reproduction commands are:

```bash
# Terminal 1
uv run claude-proxy --model gpt-6-astra --model gpt-5.6-sol

# Terminal 2
OPENAI_SUBSCRIPTION_LIVE=1 make live-openai
```

The target is isolated from the existing Claude live targets and has two-minute
HTTP deadlines. A result from another date/account is new evidence and should be
recorded separately rather than rewriting this checkpoint.
