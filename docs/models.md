# Model catalog and selection

List selectable IDs, providers, image support, and thinking controls offline:

```bash
uv run quaylet models
```

This command needs neither login nor a running server and performs no account
discovery. It lists the complete bundled catalog; the HTTP `/v1/models` endpoint
instead lists only the models selected for that running server.

Select all catalog entries explicitly:

```bash
uv run quaylet --all-models
```

Or select any subset, without repeating a flag:

```bash
uv run quaylet --model sonnet-5 opus-4.8 gpt-6-astra gpt-5.6-terra
```

Repeated `--model` flags still work and can contain multiple names each.
`--all-models` and `--model` are mutually exclusive. Omitting both is an error.
Each API request must still specify its model. `GET /v1/models` returns the
configured canonical IDs with `owned_by` set to `anthropic` or `openai`.
These flags never change authentication, subscription limits, or Pi settings.

## Bundled catalog (2026-09-09)

| Quaylet model ID | Backend | Thinking controls | Images |
| --- | --- | --- | --- |
| `sonnet-5` | Claude Sonnet 5 | Off or adaptive: low, medium, high, xhigh, max | Yes |
| `opus-5` | Claude Opus 5 | Off or adaptive: low, medium, high, xhigh, max | Yes |
| `opus-4.8` | Claude Opus 4.8 | Off or adaptive: low, medium, high, xhigh, max | Yes |
| `haiku-4.5` | `claude-haiku-4-5-20251001` | Off or manual budget; no effort selector | Yes |
| `fable-5.1` | `claude-fable-5-1` | Adaptive required: low, medium, high, xhigh, max | Yes |
| `claude-opus-4-7` | Same exact Claude ID | Off or adaptive: low, medium, high, xhigh, max | Yes |
| `claude-opus-4-6` | Same exact Claude ID | Off or adaptive: low, medium, high, max | Yes |
| `claude-sonnet-4-6` | Same exact Claude ID | Off or adaptive: low, medium, high, max | Yes |
| `claude-opus-4-5-20251101` | Same exact Claude ID | Off or manual budget, optional low/medium/high effort | Yes |
| `claude-sonnet-4-5-20250929` | Same exact Claude ID | Off or manual budget; no effort selector | Yes |
| `gpt-6-astra` | ChatGPT subscription | low, medium, high, xhigh, max | Yes |
| `gpt-5.6-sol` | ChatGPT subscription | low, medium, high, xhigh, max | Yes |
| `gpt-5.6-terra` | ChatGPT subscription | low, medium, high, xhigh, max | Yes |
| `gpt-5.6-luna` | ChatGPT subscription | low, medium, high, xhigh, max | Yes |
| `gpt-5.5` | ChatGPT subscription | low, medium, high, xhigh | Yes |
| `gpt-5.3-codex-spark` | ChatGPT subscription | low, medium, high, xhigh | **No** |

`sonnet`, `opus`, and `haiku` normalize to their pinned catalog choices.
Full native IDs for the short names also normalize to those names.
Existing explicit Claude IDs outside this catalog remain pass-through; they are
not automatically advertised or granted thinking capabilities.

Manual budgets use Anthropic Messages `thinking: {"type":"enabled",
"budget_tokens":2048}`. Quaylet does not invent a budget from a Chat Completions
effort selector. Fable uses adaptive thinking when omitted and rejects explicit
disabled/manual thinking. Other Claude defaults are unchanged. ChatGPT omission
retains the upstream reasoning default; `none` and `ultra` remain unsupported.
Codex's `ultra` includes automatic delegation, which Quaylet does not implement.

Pi custom-provider entries must match these capabilities; listing a model in
Quaylet does not configure Pi. For Fable, use adaptive thinking and map `off`
and `minimal` to `null` (unavailable), with the five supported efforts mapped
to themselves. Haiku must not use `forceAdaptiveThinking`; use Anthropic
Messages for manual-budget thinking. Older Claude 4.6 entries do not accept
`xhigh`. For ChatGPT mappings and Spark's text-only profile, see the
[provider guide](openai-subscription.md#pi-and-other-harnesses).

The only verified automatic Claude fallback route remains Opus 5 → Opus 4.8.
Adding a model does not authorize arbitrary fallback destinations. For model
validation, use the default `--refusal-fallback off`.

## Scope and evidence

This is a maintained catalog, **not runtime/account discovery**. Availability
depends on the account, provider, tier, and retirement schedule. An entry means
Quaylet has routing and capability handling, not that every feature has been
live-verified on that model. Updating Quaylet can expand `--all-models`; explicit
lists are preferable when the allowlist must stay fixed.

Sources inspected on September 9, 2026:

- Installed Pi `@earendil-works/pi-coding-agent` **0.85.1** subscription catalog:
  eight IDs, including the six above and GPT-5.4/5.4 Mini. Existing Pi MIT notice
  remains in `third_party/pi-coding-agent-MIT.txt`.
- Local Codex **0.153.4** model metadata, fetched September 9: six public models
  above, their reasoning levels, and Spark's text-only modality. Only model
  metadata was inspected; no Pi/Codex credentials were read. Quaylet does not
  depend on this local cache at runtime.
- Claude Agent SDK **0.2.152**, bundled Claude Code **2.1.259** initialization
  model menu and embedded capability metadata. The menu includes Fable 5.1,
  Haiku 4.5, Sonnet 5, and Opus 5. Older entries use explicit IDs, not aliases.
- [Claude manual thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking)
  and [Haiku 4.5](https://platform.claude.com/docs/en/models/haiku-4-5/migration-guide)
  document manual-only thinking for Haiku and older models.

Live low-effort synthetic text requests through Quaylet's fixed subscription
endpoint completed on Terra, Luna, GPT-5.5, and Spark, with matching upstream
model identities. Observed latencies were 2.34, 2.41, 2.19, and 1.44 seconds,
respectively; each reported 19 input and 5 output tokens. These tiny requests
are availability checks, not a performance comparison or full tool/vision test.
Existing Astra/Sol evidence is in the [live report](verification/2026-09-08-openai-live.md).

A follow-up through Quaylet's Chat Completions frontend passed one caller-owned
tool round on all four new ChatGPT models, matching returned model identities
and checking usage totals. Terra, Luna, and GPT-5.5 received image tool results
and identified the red image; Spark received a text result. All four tests
passed in 12.22 seconds total. Reproduce this small opt-in battery without
starting or modifying a running server:

```bash
OPENAI_SUBSCRIPTION_LIVE=1 uv run pytest tests/live_openai/test_catalog.py -q
```

It uses the Quaylet-owned login, a 120-second deadline per model, and closes each
in-process application's HTTP pool. It does not establish every effort level,
repeated/parallel tool rounds, or model-specific compaction behavior.

GPT-5.4 and GPT-5.4 Mini returned HTTP 400 and are excluded; Pi's static list was
not sufficient proof of access. Hidden/internal Codex entries are excluded too.
No API-key, web-only, alternate-account, or automatic model fallback was used.

**New Claude live verification is blocked:** the account returned its weekly
quota error (reset reported as September 10 at 11 a.m. America/New_York). The
first checks surfaced this as Quaylet's existing `backend_model_mismatch`, because
the SDK labels its synthetic error message `<synthetic>` rather than the
requested model. Direct SDK error metadata confirmed rate limiting, not a
successful model response. Haiku, Fable, and older added Claude IDs therefore
remain live-unverified; no further quota retries were made. In particular,
Fable's runtime prompt behavior and tool/rebasing compatibility are not certified
by merely seeing it in the SDK menu.

No running service or Pi configuration was changed by this catalog update.

Offline verification: 2,291 tests passed, 75 live tests deselected, with 44
existing fork deprecation warnings (75.60 seconds). Ruff and mypy also passed.
After adding the offline `models` command, the merged `a7a559c` revision passed
**2,293 offline tests**, with 79 live tests deselected and 44 existing warnings
(70.21 seconds). Lint and type checks passed. Fresh read-only reviews of the
catalog and listing command found no actionable correctness issues.
Formatting passes for all changed Python files; a whole-tree formatting check
still reports 48 unchanged files. No unrelated formatting was applied.
