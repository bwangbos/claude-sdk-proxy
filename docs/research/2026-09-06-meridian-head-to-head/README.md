# Live comparison with Meridian — 2026-09-06

The generic OpenAI endpoint in this project preserved parallel tool-result
associations in 3/3 confirmation trials. Meridian 1.68.0 swapped the two values
in 3/3 confirmation trials. Both passed the equivalent native Anthropic case.
This establishes a specific compatibility advantage, not overall superiority.

## Environment and method

- This project: `ff48f9cfeeef11e354b47f186d8a57617eaf1c89`, Python SDK 0.2.152.
- Meridian: npm `@rynfar/meridian@1.68.0`, installed in a temporary directory;
  resolved TypeScript SDK 0.2.141. Source checkout inspected separately:
  `58f8a70402fce471712cd4650f721afcb80f05d7`.
- Same Mac and local Claude Max login; sequential requests through httpx.
- Both received `model: sonnet`, `max_tokens: 512`. The exact resolved upstream
  model was not independently attested, so timings are descriptive only.
- Our server: `claude-proxy --port 18317 --model sonnet`.
- Meridian: port 18318, loopback, temporary work/config directories,
  `MERIDIAN_DEFAULT_AGENT=passthrough`, `MERIDIAN_PASSTHROUGH=1`,
  `MERIDIAN_1M_CONTEXT_SUPPORT=0`, `MERIDIAN_NO_UPDATE_CHECK=1`.
- No client-specific session headers. Meridian selected its `openai` adapter
  for Chat Completions. Native Messages used the passthrough default.
- Random marker strings prevent accidental cross-case matching. Prompt templates
  and schemas are identical, but random values differ between executions.
- Caller tools are synthetic lookups performed by the benchmark script.

## Results

| Case | This project | Meridian |
| --- | --- | --- |
| OpenAI incremental text stream | Pass; 2 content chunks | Pass; 2 content chunks |
| Imported assistant-history marker, OpenAI | Pass | Pass |
| Reversed parallel results, OpenAI confirmation trials | 3/3 correct | 0/3 correct; values swapped |
| Imported assistant-history marker, Anthropic | Pass | Pass |
| Reversed parallel results, Anthropic | 1/1 correct | 1/1 correct |
| Duplicate text request | Same answer; ~1 ms on both dialects | Same answer; 2.479 s OpenAI / 2.744 s Anthropic |

The OpenAI tool test asks for `lookup(alpha)` and `lookup(beta)` in the same
response. The script returns each result using its correct call ID, in reverse
order, then checks the exact `alpha=value;beta=value` answer. For example,
Meridian received beta=`B-d081942a` and alpha=`A-281858c2`, but answered
`alpha=B-d081942a;beta=A-281858c2`.

The initial exploratory run showed the same error but its assertion only checked
that both values appeared. The confirmation runs check associations exactly and
save submitted messages. **Do not aggregate the original `passed` flags in
results.json:** its tool assertion was too weak and its replay assertion was too
strict (full response equality including generated IDs). Those original records
remain unmodified for auditability. The saved harness contains corrected checks.

## Mechanism and interpretation

Meridian's generic OpenAI translator puts prior turns into a system-prompt
`conversation_history` string. Its summary includes tool names and arguments,
but omits tool-use and result IDs. Only the final turn remains a native message.
This is consistent with the observed loss of association; it is not a complete
causal proof. The installed npm bundle contains this same transformation.
The inspected source exempts its Jcode integration when the expected user-agent
and session header are supplied. That specialized path was not tested.

Sources: [OpenAI translation](https://github.com/rynfar/meridian/blob/58f8a70402fce471712cd4650f721afcb80f05d7/src/proxy/openai.ts),
[endpoint adapter selection](https://github.com/rynfar/meridian/blob/58f8a70402fce471712cd4650f721afcb80f05d7/src/proxy/server.ts).

Our bridge keeps the pending SDK handlers alive and resolves results by call ID.
The native Anthropic Meridian case also preserved the mapping. Users choosing
Meridian can therefore avoid the demonstrated generic-OpenAI failure by using
its Anthropic endpoint for this case.

Both proxies returned the expected duplicate-request content with new response
IDs. Our implementation replays its cached result; Meridian logs showed another
SDK query for the OpenAI duplicate. Cached replay is an extra local guarantee,
not a universal requirement of the OpenAI API. It also means that independent
identical conversations require explicit session identification in our proxy.

## Indicative timing

| Measurement | This project | Meridian |
| --- | --- | --- |
| First content, one OpenAI streaming request | 1.339 s | 2.367 s |
| Full streamed response, same request | 1.388 s | 3.482 s |
| Median complete OpenAI tool round, 3 confirmation trials | 3.859 s | 5.988 s |
| Complete Anthropic tool round, one trial | 3.253 s | 4.807 s |

These are small sequential samples, with our trials first, different SDK
versions, and uncontrolled upstream load/cache state. They do not establish a
general speed advantage. Token fields were retained but not compared as costs:
cache accounting and internal SDK work differ between implementations.

## Reproduce

Start both servers on the ports above using a working Claude login. With this
repository's virtual environment:

```sh
H2H_OUTPUT=/tmp/h2h-openai.json .venv/bin/python docs/research/2026-09-06-meridian-head-to-head/benchmark.py
H2H_CASES=parallel_tools,parallel_tools,parallel_tools H2H_OUTPUT=/tmp/h2h-repeat.json .venv/bin/python docs/research/2026-09-06-meridian-head-to-head/benchmark.py
H2H_DIALECT=anthropic H2H_CASES=import,parallel_tools,replay H2H_OUTPUT=/tmp/h2h-anthropic.json .venv/bin/python docs/research/2026-09-06-meridian-head-to-head/benchmark.py
```

These commands use the subscription. Results are exploratory JSON records;
the script does not exit nonzero when an assertion fails. The Anthropic mode
supports the listed non-streaming cases only.

Not tested: actual Pi execution, images, full automatic compaction workflows,
restart recovery, concurrent request races, disconnect teardown, schema edge
cases, Responses API, or Meridian's specialized client integrations. Imported
marker recall is a narrow history test, not proof of general history fidelity.
