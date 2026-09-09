# Quaylet clean-cut rename — 2026-09-09

The rename is based on the previously reviewed Claude/ChatGPT implementation
at `03560d8`. Commit `bf4b596` renames the implementation; `21e9a82` updates
active documentation. This is a naming change, not a new provider implementation.

## Scope

- Python distribution/import namespace: `quaylet`.
- Commands: `quaylet`, `quaylet-capabilities`, and `quaylet-probe`.
- Proxy-owned headers: `X-Quaylet-*`; diagnostic/schema/native identifiers and
  test controls use the new name consistently.
- ChatGPT credential directory: `~/.config/quaylet/`, with no automatic
  migration or old-name fallback.
- Standard HTTP routes, port, model IDs, provider-owned controls, security
  enforcement, and all 53 third-party lockfile records are unchanged.

There are no legacy command, import, or custom-header aliases. The existing
checkout directory need not be renamed, and updating Git does not restart a
service or edit a harness configuration.

## Verification

| Check | Observed result |
| --- | --- |
| Pre-rename full offline baseline | 2,246 passed; 1 known failure; 75 live deselections |
| Focused package/credential/CLI/native checks | 54 passed |
| Current-schema and archived-evidence checks with authentication CLI tests | 147 passed |
| Post-rename full offline run at `bf4b596` | 2,249 passed; same 1 known failure; 75 live deselections |
| Independent repeat at `bf4b596` | Same counts, 70.76 seconds |
| Ruff / mypy | Passed; 55 source files type-checked |
| Native build | Passed with existing C17 warnings-as-errors flags |
| Python wheel and source distribution | Built successfully |
| Isolated wheel installation | New package imports; retired package does not; command entry points execute |

Both baseline and final full runs emitted the same 44 existing fork warnings.
The sole failure remains
`tests/reset/test_domain.py::test_text_request_requires_alternating_messages_ending_in_user`;
it predates this rename and was deliberately not changed. The suite is therefore
not reported as entirely green.

`quaylet` and `quaylet-capabilities` provide help output. The historical probe
command retains its existing behavior: `quaylet-probe --help` emits a structured
`invalid_invocation` record and exits zero. That check is only an entry-point
execution smoke, not a successful probe or new help functionality.

Historical reports and the archived validation manifest are unchanged. The
current loader explicitly rejects that manifest's retired schema. Tests audit
its historical facts as raw JSON and use synthetic current-schema data for
current loader/gate behavior; no historical evidence was rewritten as a new
verification result.

No live subscription calls were made for this rename. Earlier account-specific
observations remain in the [ChatGPT live report](2026-09-08-openai-live.md);
they do not independently verify the newly branded upstream identity.

A fresh Astra review of `03560d8..bf4b596` passed specification and code-quality
checks with no Critical or Important findings. It independently checked the
mechanical source scope, locked dependency records, wheel namespace and scripts,
and native library linkage. The known baseline failure remains disclosed.
