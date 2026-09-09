# Sparse terminal output live-fix report

## Status

Implemented the bounded correction in `EventTranslator`: when a completed
terminal response has an empty `output`, the translator reconstructs it only
from `response.output_item.done` items whose indexes exactly form the contiguous
range `0..n-1`. Empty output with added-only, incomplete, missing-zero, or
noncontiguous items remains an `invalid_response`. Non-empty terminal output
continues through the existing authoritative validation path unchanged.

The reconstructed native items retain their IDs, phases/status, call IDs,
reasoning summaries and ciphertext. The existing prefix accounting prevents
already-streamed text and reasoning summaries from being emitted twice. Usage
still comes from the terminal response.

## TDD evidence

### RED

Command:

```text
.venv/bin/pytest -q tests/openai_subscription/test_backend.py -k 'sparse_terminal'
```

Result before the production change:

```text
FF...                                                                    [100%]
2 failed, 3 passed, 22 deselected in 0.09s
```

Both dialect replay cases failed at `events.py:362` with
`SubscriptionFailure("invalid_response")` because terminal `output=[]` was
compared directly with the three completed streamed items. The three malformed
sparse cases already passed because the prior implementation rejected them.

### GREEN

Same focused command after the production change:

```text
.....                                                                    [100%]
5 passed, 22 deselected in 0.06s
```

The passing regression covers streamed text without duplication, function-call
arguments and native call IDs, reasoning summary/ciphertext, terminal usage,
Anthropic portable replay, OpenAI cache replay, added-only items, a missing zero
boundary, and a noncontiguous boundary.

## Verification

Focused subscription tests excluding the sandbox-incompatible OAuth listener:

```text
.venv/bin/pytest -q --strict-markers --forbid-skips -W error tests/openai_subscription --ignore=tests/openai_subscription/test_auth.py
204 passed in 1.13s
```

Static checks:

```text
.venv/bin/ruff check src tests/openai_subscription
All checks passed!

.venv/bin/mypy src
Success: no issues found in 55 source files
```

The unfiltered subscription command produced `220 passed, 8 failed`; every
failure was in `test_auth.py` because the managed sandbox denied binding
`127.0.0.1:1455` with `PermissionError: [Errno 1]`. No network, live provider,
credential, browser, or service action was attempted.

## Files changed

- `src/claude_sdk_proxy/openai_subscription/events.py`
- `tests/openai_subscription/test_backend.py`
- `.superpowers/sdd/2026-09-08-direct-openai/live-fix-report.md`

## Interfaces and self-review

No public interface changed. The only new translator state is the set of item
indexes observed at `output_item.done`. Reconstruction is confined to completed,
empty terminal output; the existing non-empty terminal conflict validation is
not bypassed. A mutation that omits done tracking, admits added-only items, or
relaxes contiguous indexing is caught by the regression cases.

No unrelated refactor was made. Remaining concern: the complete OAuth test file
cannot be verified inside this sandbox because it requires a local listener.

## Commit

Subject: `Handle sparse completed response output`. The final hash is reported
to the controller because recording a commit's own hash would change that hash.
