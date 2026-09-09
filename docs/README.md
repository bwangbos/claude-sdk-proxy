# Quaylet documentation

## Current usage and verification

- [Model catalog](models.md): all-model selection, multi-name flags, provider
  routing, per-model thinking/images, provenance, and verification limits.
- [Quaylet rename verification](verification/2026-09-09-quaylet-rename.md): clean-cut
  names, packaging, unchanged dependencies, offline evidence, and archive handling.
- [ChatGPT subscription backend](openai-subscription.md): login, exact models,
  fixed endpoint and billing boundary, replay/reasoning limitations, usage,
  diagnostics, and the explicitly gated live battery.
- [Direct ChatGPT live follow-up](verification/2026-09-08-openai-live.md):
  bounded Astra/Sol, tools, image-bearing results, compaction, cancellation,
  accounting, and stock Pi evidence, with explicit limitations.
- [Earlier direct ChatGPT checkpoint](verification/2026-09-08-openai-subscription.md):
  deterministic evidence before the live follow-up.

- [Opus 4.8 vision verification](research/2026-09-07-opus-4-8-vision-verification.md):
  direct images and image tool results through both APIs, image-history rebasing,
  stock Pi's Anthropic read-tool workflow, and the text/image configuration update.

- [Root README](../README.md): installation, canonical Pi configuration,
  model/thinking controls, images, compaction, diagnostics, and limitations.
- [Gateway reference](feasibility/README.md#current-runnable-gateway): complete
  tool requests, session rules, schema limits, timeouts, and release commands.
  Its final section is explicitly archived Phase 0 evidence.
- [Model/thinking verification](research/2026-09-06-model-thinking-verification.md):
  Sonnet/Opus effort matrix, real Pi bidirectional switching, native refusal
  investigation, final review, and the 1,757-test offline gate.
- [Pinned-model and buffered-fallback evidence](research/2026-09-07-buffered-fallback-evidence.md):
  exact Sonnet 5, Opus 5, and Opus 4.8 identities; Opus 4.8 text/tool/thinking
  capability; isolated opt-in fallback observations; synthetic defensive
  coverage; successful production-path fallback; the final 1,954-test checkpoint;
  and remaining live-evidence limitations. It also explains the bundled-runtime
  synthetic close that differed from the initial protocol assumptions.
- [Harness compatibility verification](research/2026-09-06-harness-compatibility-verification.md):
  request normalization, transcript recovery, usage, diagnostics, and the
  native-clock fix. Counts describe that earlier checkpoint, not today's suite.

Updating Git does not restart a running proxy or edit local Pi settings; follow
the root README to activate the revision you checked out.
Live observations are dated, account-specific evidence, not guarantees about
future provider behavior or model availability.

## Historical designs and research

- [September 3 capability checkpoint](capabilities/2026-09-03-minimal-backend-capabilities.md)
  and [subscription reassessment](research/2026-09-03-subscription-proxy-reassessment.md)
  preserve the investigation that led to the working SDK gateway.
- [`superpowers/specs/`](superpowers/specs/) and
  [`superpowers/plans/`](superpowers/plans/) preserve dated designs, implementation
  plans, and review decisions. They are not current API contracts or pending-work
  lists. Later features supersede earlier restrictions on tools, imported history,
  compaction, images, and thinking.
- The [legacy feasibility record](feasibility/README.md#superseded-legacy-phase-0-feasibility-record)
  documents an earlier architecture and negative gate results. Its commands,
  runtime pins, and policy findings must not be treated as current launch guidance.

Historical observations and failed probes are retained rather than rewritten
as successes. For current behavior, use the root README, gateway reference,
and the implementation/tests at the revision you are running.
Historical documents also retain the pre-Quaylet project, command, and header
names. Those are not aliases in the current clean-cut rename.
