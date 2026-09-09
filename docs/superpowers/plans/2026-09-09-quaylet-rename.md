# Quaylet Rename Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Rename the project and GitHub repository to Quaylet with a clean cut, retaining the working Claude and opt-in ChatGPT backends.

**Architecture:** Rename the Python distribution/module and CLI, current protocol identifiers, diagnostics and native artifacts together. Keep provider names and provider-owned protocol fields intact. Use the new credential directory without runtime migration or legacy aliases; the coordinator may safely move the user's existing directory separately.

**Tech Stack:** Python 3.14, uv/hatchling, pytest, native macOS helpers, GitHub CLI.

**Spec:** User-approved in-chat scope, superseded by explicit clean-cut instruction: no legacy names or aliases required. Push ordering is discretionary.

## Global Constraints

- Work atop `03560d8` in the existing `openai-subscription` worktree.
- Do not modify model behavior, authentication mechanisms, dependencies, or provider-owned identifiers.
- New distribution/module/CLI: `quaylet`; auxiliary commands: `quaylet-capabilities`, `quaylet-probe`.
- New credential root: `~/.config/quaylet/`; no runtime fallback to the old directory.
- Rename proxy-owned `claude-sdk-proxy`, `claude_sdk_proxy`, `claude-proxy`, `claude_proxy`, and `CLAUDE_PROXY` identifiers consistently, including custom HTTP headers, native targets, schemas and test controls. Preserve true Claude SDK/CLI/provider identifiers.
- Preserve historical reports/designs and their observed names. Update only active user guidance; explain the clean break.
- Do not restart a service, modify Pi, move the checkout directory, publish a package, or merge automatically. Push the combined feature branch and rename GitHub after verification.
- Existing known offline failure is `tests/reset/test_domain.py::test_text_request_requires_alternating_messages_ending_in_user`; report it, do not change it for a rename.

### Task 1: Clean-cut package and runtime rename

**Files:** `src/claude_sdk_proxy/` → `src/quaylet/`, tests, `native/`, `Makefile`, `pyproject.toml`, `uv.lock`.

**Interfaces:** Produces installable Quaylet package, commands, protocol identifiers, and new credential location. Documentation uses these exact names. The coordinator owns Markdown documentation and remote operations; do not edit those.

- [ ] Add a focused behavior test in `tests/test_quaylet.py` before implementation. Use a subprocess executing `sys.executable -m quaylet.cli --help` and assert exit 0 and `--model` output. Exercise new package credential storage against a synthetic temporary home, saving/loading dummy credentials and asserting the file exists only under `.config/quaylet/`. Use `importlib.util.find_spec` inside test bodies so initial missing module yields assertion failure, not collection error.

```python
def test_quaylet_module_runs_help():
    result = subprocess.run(
        [sys.executable, "-m", "quaylet.cli", "--help"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "--model" in result.stdout
```

- [ ] Run focused tests; capture the expected missing-package failure.
- [ ] Move source to `src/quaylet/`; mechanically update imports, runtime-owned identifiers, tests and native target references. Use apply_patch for authored edits; mechanical bulk rewrite tools are permitted. Rename proxy-owned C filenames when appropriate; do not rename the actual Claude provider executable or SDK imports.
- [ ] Set pyproject project name to `quaylet`, description to `Local API gateway for Claude and ChatGPT subscriptions`, URLs to `https://github.com/bwangbos/quaylet`, three scripts to `quaylet.cli:main`, `quaylet.capability_cli:main`, `quaylet.probe_cli:main`; remove old script names; set mypy source path to `src/quaylet`.
- [ ] Regenerate uv lock and editable installation without upgrading dependencies. Run focused tests and installed command help for all three entry points. Build native targets and Python wheel/sdist, and smoke-test wheel in a separate environment.
- [ ] Run full offline suite once, plus ruff and mypy. Distinguish the known baseline failure from new regressions. Re-run changed tests after fixes.
- [ ] Self-review and commit only implementation/test/build files. Report exact evidence and any exceptions, without secrets.

## Coordinator delivery checklist

- [ ] Update README, docs index, OpenAI guide, and active gateway reference; leave historical evidence unchanged.
- [ ] Fresh code review of the rename and integration surface; resolve important findings.
- [ ] Verify safe credential move without reading or logging contents, preserving permissions and refusing overwrite or symlink targets.
- [ ] Rename GitHub to `bwangbos/quaylet`, update origin and description, and push feature branch with both backends. Do not merge without further authorization.
- [ ] Report new repository URL, launch command, pushed branch, verification results, and service/checkout status.
