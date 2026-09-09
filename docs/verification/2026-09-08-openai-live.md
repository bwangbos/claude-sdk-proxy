# Direct ChatGPT live verification — 2026-09-08

This is the live follow-up to the earlier
[deterministic checkpoint](2026-09-08-openai-subscription.md). It records one
explicitly authorized, bounded run through a fresh proxy-owned OAuth login. It
was verified as authenticated and unexpired before the run. This is evidence
for the tested account and date, not a general authorization,
performance, billing, or availability claim.

## Corrections found before the final battery

The first live Astra request reached the upstream service and received HTTP 200
but the proxy returned `invalid_response`. Structure-only diagnostics found a
completed message in `response.output_item.done` followed by
`response.completed` with an empty `output` and valid terminal usage. Commit
`2da2725` reconstructs that sparse terminal form only from a complete,
contiguous set of done items. Added-only, incomplete, missing, noncontiguous,
and conflicting full-terminal forms remain fail-closed. The correction and its
text, tool, reasoning, usage, and both-dialect replay tests were independently
reviewed.

A later Sol compacted continuation exposed a second schema mismatch: a valid
reasoning item contained the official optional `content` field. The installed
OpenAI SDK schema defines it as nullable or a list of exact
`reasoning_text`/string-text objects, with `encrypted_content` and `status` also
nullable. Commit `2655f08` accepts those official forms without dropping
plaintext reasoning content or opaque replay metadata, still rejects malformed
content, and preserves the exact native item in both replay dialects. This
correction was also independently reviewed. The final battery ran at
`2655f08`.

## Final bounded live battery

All six probes passed. Times below include the complete external runner elapsed
time and pytest's complete test duration from the sanitized result record; they
are not time-to-first-token measurements or a benchmark.

| Probe | Result | Runner elapsed (pytest reported) |
| --- | --- | ---: |
| Astra low-effort response, model identity, and usage accounting | pass | 2.01 s (1.84 s) |
| Sol low-effort response, model identity, and usage accounting | pass | 2.03 s (1.85 s) |
| Astra one-tool round, image tool result, and compacted continuation | pass | 6.70 s (6.54 s) |
| Sol one-tool round, image tool result, and compacted continuation | pass | 8.37 s (8.20 s) |
| In-flight upstream disconnect and local cleanup | pass | 9.27 s (9.03 s) |
| Stock Pi Anthropic-path Astra smoke with temporary configuration | pass | 5.90 s (5.68 s) |

The first two probes requested `gpt-6-astra` and `gpt-5.6-sol`, respectively,
with `reasoning_effort=low`. Each verified the requested-model routing header,
a nonempty upstream-reported actual-model identity, equality between that
reported identity and the response model, nonempty content, nonnegative prompt
and completion token counts, and exact total-token arithmetic. The test allows
dated upstream model identifiers, so it does not claim the reported identifier
must equal the requested route label.

Both tool probes required exactly one `inspect_color` call. They returned a tool
result containing the text `red` and an embedded solid-red PNG, then verified a
normal continuation and a separately compacted-history answer. This proves that
the image-bearing tool-result path was accepted for both models; because the
tool-result text also named the color, it is not a text-free image-vision oracle.
Only a single tool round and low reasoning effort were exercised.

The cancellation probe disconnected after visible content while the observed
real upstream stream was still nonterminal. It then verified closure of that
local upstream HTTP connection, release of the proxy turn lease, and absence of
a replay-cache entry. It cannot observe or prove provider-side compute
cessation, billing cessation, or credit behavior.

The stock Pi smoke used `gpt-6-astra` through the Anthropic-compatible path and
a temporary `PI_CODING_AGENT_DIR`; it produced nonempty output. It did not edit
the user's Pi configuration. The isolated proxy used the fresh proxy-owned
login on port 18318, and its test-server process exited cleanly after the run.
The isolated process was 27766; a post-run check found no listener on that port.
The normal service and Pi configuration were left untouched. No credentials,
account identifiers, prompts, transcripts, tool payloads, reasoning plaintext,
or ciphertext are retained in this durable record.

## Non-live verification after the battery

The complete non-live run at the final code produced 2,246 passes, 75 live
deselections, the one previously supplied reset/domain alternation baseline
failure, and 44 inherited macOS fork deprecation warnings. The subscription
suite separately passed 237 tests, and Ruff plus strict mypy passed across 55
source files. The baseline failure and warnings are outside this backend change.

## Boundaries of this evidence

This run does not establish broad model quality, latency, throughput, image
vision, multiple tool rounds, reasoning efforts other than `low`, included-plan
allowances, credit consumption, or provider billing behavior. It is not an
endorsement of the unofficial endpoint and does not replace checking the terms
and limits applicable to the account.
