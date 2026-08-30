# Phase 0 Agent SDK Feasibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove or disprove the policy, prompt-isolation, persistence, long-lived-session, streaming, and caller-owned-tool assumptions required by the proxy before building its public API.

**Architecture:** A small `claude_sdk_proxy` Python package owns the validated SDK/CLI version pair and constructs the only permitted `ClaudeAgentOptions`. Opt-in live probes exercise that boundary through the user's existing Claude login and emit redacted JSON reports. No HTTP server or production compatibility adapter is built in this phase.

**Tech Stack:** Python 3.12–3.14, uv, Claude Agent SDK 0.2.148, Claude CLI 2.1.251, pytest/AnyIO, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-08-29-claude-subscription-api-proxy-design.md`

## Global Constraints

- Personal, single-user, loopback-only use; do not implement or expose Claude login.
- Use `claude-agent-sdk==0.2.148` and require `claude --version` to report `2.1.251` for this validation run.
- Stop the project if current Anthropic policy disallows the personal existing-login workflow.
- Set the caller system prompt exactly, using an empty string when absent.
- Disable built-in tools, skills, settings, connectors, memory, workflows, subagents, compaction, transcript persistence, attribution, and nonessential traffic.
- Never read, copy, print, log, or forward Claude credentials.
- Live probes are opt-in with `RUN_LIVE_CLAUDE_TESTS=1` and must use redacted reports.
- Do not proceed to Phase 1 unless every core gate passes. A tool-gate failure disables tools for that exact model but does not block text-only Phase 1.

## File Map

- `pyproject.toml`: Python/dependency/tool configuration and console-script entry.
- `src/claude_sdk_proxy/__init__.py`: package version only.
- `src/claude_sdk_proxy/validated.py`: exact SDK/CLI pair and gate result types.
- `src/claude_sdk_proxy/isolation.py`: version checks and isolated `ClaudeAgentOptions` construction.
- `src/claude_sdk_proxy/probes.py`: live prompt, persistence, session, and tool probes.
- `src/claude_sdk_proxy/probe_cli.py`: redacted JSON probe CLI.
- `tests/unit/test_isolation.py`: pure option/version tests.
- `tests/unit/test_probe_reports.py`: report serialization/redaction tests.
- `tests/live/test_prompt_purity.py`: separate ambient-load, disk, attribution, near-context compaction, and structured-input gates.
- `tests/live/test_thinking.py`: per-model thinking/effort equivalence gate.
- `tests/live/test_session_continuity.py`: multi-turn and streaming lifecycle gates.
- `tests/live/test_tool_bridge.py`: blocking MCP callback and call-correlation gates.
- `docs/feasibility/README.md`: commands, evidence fields, and pass/fail policy.
- `docs/feasibility/validated-environment.json`: generated, non-secret validation result committed only after probes run.

---

### Task 1: Bootstrap the Validation Package

**Files:**
- Create: `pyproject.toml`
- Create: `src/claude_sdk_proxy/__init__.py`
- Create: `tests/unit/test_package.py`
- Create: `.gitignore`

**Interfaces:**
- Consumes: none.
- Produces: importable `claude_sdk_proxy` package with `__version__: str` and a reproducible uv environment.

- [ ] **Step 1: Write the package smoke test**

```python
# tests/unit/test_package.py
from claude_sdk_proxy import __version__


def test_package_version_is_explicit() -> None:
    assert __version__ == "0.1.0"
```

- [ ] **Step 2: Add the package and tool configuration**

```toml
# pyproject.toml
[project]
name = "claude-sdk-proxy"
version = "0.1.0"
requires-python = ">=3.12,<3.15"
dependencies = [
  "claude-agent-sdk==0.2.148",
]

[project.optional-dependencies]
dev = [
  "mypy>=1.17,<2",
  "pytest>=8.4,<10",
  "ruff>=0.12,<1",
]

[project.scripts]
claude-proxy-probe = "claude_sdk_proxy.probe_cli:main"

[build-system]
requires = ["hatchling>=1.27,<2"]
build-backend = "hatchling.build"

[tool.pytest.ini_options]
addopts = "-ra"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "ASYNC"]

[tool.mypy]
python_version = "3.12"
strict = true
packages = ["claude_sdk_proxy"]
```

```python
# src/claude_sdk_proxy/__init__.py
__version__ = "0.1.0"
```

```gitignore
.venv/
__pycache__/
.pytest_cache/
.ruff_cache/
.mypy_cache/
*.pyc
probe-output/
```

- [ ] **Step 3: Resolve and lock dependencies**

Run: `uv lock`

Expected: `uv.lock` pins `claude-agent-sdk==0.2.148` and all transitive dependencies without resolver errors.

- [ ] **Step 4: Run the smoke test and static checks**

Run: `uv run pytest tests/unit/test_package.py -v`

Expected: PASS.

Run: `uv run ruff check .`

Expected: no findings.

- [ ] **Step 5: Commit the bootstrap**

```bash
git add pyproject.toml uv.lock .gitignore src/claude_sdk_proxy/__init__.py tests/unit/test_package.py
git commit -m "build: bootstrap feasibility probe package"
```

---

### Task 2: Pin the Runtime and Construct the Isolation Profile

**Files:**
- Create: `src/claude_sdk_proxy/validated.py`
- Create: `src/claude_sdk_proxy/isolation.py`
- Create: `tests/unit/test_isolation.py`

**Interfaces:**
- Consumes: `claude_agent_sdk.ClaudeAgentOptions`.
- Produces: `SUPPORTED_AGENT_SDK_VERSION`, `SUPPORTED_CLAUDE_CLI_VERSION`, `IsolationConfig`, `assert_supported_runtime()`, and `build_agent_options()`.

- [ ] **Step 1: Write failing runtime and option tests**

```python
# tests/unit/test_isolation.py
from pathlib import Path

import pytest

from claude_sdk_proxy.isolation import (
    IsolationConfig,
    RuntimeMismatch,
    assert_supported_runtime,
    build_agent_options,
)


def test_runtime_gate_rejects_cli_mismatch() -> None:
    with pytest.raises(RuntimeMismatch, match="Claude CLI"):
        assert_supported_runtime(agent_sdk_version="0.2.148", cli_output="2.1.250")


def test_options_remove_ambient_context(tmp_path: Path) -> None:
    options = build_agent_options(
        IsolationConfig(model="claude-test", system_prompt="caller only", cwd=tmp_path)
    )
    assert options.system_prompt == "caller only"
    assert options.tools == []
    assert options.skills == []
    assert options.setting_sources == []
    assert options.mcp_servers == {}
    assert options.strict_mcp_config is True
    assert options.agents == {}
    assert options.cwd == str(tmp_path)
    assert options.env["CLAUDE_CODE_ATTRIBUTION_HEADER"] == "0"
    assert options.env["CLAUDE_CODE_SKIP_PROMPT_HISTORY"] == "1"
    assert options.env["DISABLE_COMPACT"] == "1"
    assert options.env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
    assert options.env["CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS"] == "1"
    assert options.env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_isolation.py -v`

Expected: FAIL because `claude_sdk_proxy.isolation` does not exist.

- [ ] **Step 3: Implement the exact runtime gate and isolation builder**

```python
# src/claude_sdk_proxy/validated.py
SUPPORTED_AGENT_SDK_VERSION = "0.2.148"
SUPPORTED_CLAUDE_CLI_VERSION = "2.1.251"
```

```python
# src/claude_sdk_proxy/isolation.py
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

from .validated import SUPPORTED_AGENT_SDK_VERSION, SUPPORTED_CLAUDE_CLI_VERSION


class RuntimeMismatch(RuntimeError):
    pass


ISOLATION_ENV: dict[str, str] = {
    "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
    "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
    "DISABLE_COMPACT": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1",
    "CLAUDE_CODE_DISABLE_POLICY_SKILLS": "1",
    "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1",
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    "CLAUDE_CODE_DISABLE_WORKFLOWS": "1",
    "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
}


@dataclass(frozen=True, slots=True)
class IsolationConfig:
    model: str
    system_prompt: str
    cwd: Path
    cli_path: str = "claude"


def assert_supported_runtime(*, agent_sdk_version: str, cli_output: str) -> None:
    if agent_sdk_version != SUPPORTED_AGENT_SDK_VERSION:
        raise RuntimeMismatch(
            f"Agent SDK {agent_sdk_version} != {SUPPORTED_AGENT_SDK_VERSION}"
        )
    cli_version = cli_output.split(" (", 1)[0].strip()
    if cli_version != SUPPORTED_CLAUDE_CLI_VERSION:
        raise RuntimeMismatch(
            f"Claude CLI {cli_version} != {SUPPORTED_CLAUDE_CLI_VERSION}"
        )


def build_agent_options(config: IsolationConfig) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=config.model,
        system_prompt=config.system_prompt,
        tools=[],
        skills=[],
        setting_sources=[],
        mcp_servers={},
        strict_mcp_config=True,
        agents={},
        cwd=str(config.cwd),
        cli_path=config.cli_path,
        env=dict(ISOLATION_ENV),
        include_partial_messages=True,
    )
```

- [ ] **Step 4: Run focused tests and type checks**

Run: `uv run pytest tests/unit/test_isolation.py -v`

Expected: PASS.

Run: `uv run mypy src/claude_sdk_proxy/isolation.py`

Expected: no errors. If the pinned SDK's annotations expose an option under a different public name, update the implementation and test together to the actual 0.2.148 public API; do not use a private transport field.

- [ ] **Step 5: Commit the isolation boundary**

```bash
git add src/claude_sdk_proxy/validated.py src/claude_sdk_proxy/isolation.py tests/unit/test_isolation.py
git commit -m "feat: define pinned isolated Agent SDK runtime"
```

---

### Task 3: Add Redacted Probe Reports and Disk Canaries

**Files:**
- Create: `src/claude_sdk_proxy/probes.py`
- Create: `src/claude_sdk_proxy/probe_cli.py`
- Create: `tests/unit/test_probe_reports.py`
- Create: `tests/live/test_prompt_purity.py`
- Create: `docs/feasibility/README.md`

**Interfaces:**
- Consumes: `IsolationConfig`, `build_agent_options()`.
- Produces: `ProbeResult`, `snapshot_claude_state()`, `run_prompt_purity_probe()`, and `claude-proxy-probe prompt-purity`.

- [ ] **Step 1: Write report and redaction tests**

```python
# tests/unit/test_probe_reports.py
from claude_sdk_proxy.probes import ProbeResult


def test_probe_report_contains_evidence_without_secrets() -> None:
    result = ProbeResult(
        name="prompt-purity",
        passed=True,
        evidence={"system_prompt": "caller-only", "authorization": "secret"},
    )
    payload = result.redacted_dict()
    assert payload["evidence"]["system_prompt"] == "caller-only"
    assert payload["evidence"]["authorization"] == "[REDACTED]"


def test_probe_report_redacts_nested_environment_and_exceptions() -> None:
    result = ProbeResult(
        name="nested",
        passed=False,
        evidence={
            "headers": {"Cookie": "session=secret"},
            "environment": {"CLAUDE_OAUTH_TOKEN": "secret"},
            "error": RuntimeError("Bearer secret"),
        },
    )
    encoded = str(result.redacted_dict())
    assert "session=secret" not in encoded
    assert "Bearer secret" not in encoded
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/test_probe_reports.py -v`

Expected: FAIL because `ProbeResult` does not exist.

- [ ] **Step 3: Implement reports and filesystem snapshots**

```python
# Add to src/claude_sdk_proxy/probes.py
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SECRET_FRAGMENTS = ("authorization", "api_key", "apikey", "token", "oauth", "cookie", "secret", "credential")
CONTENT_KEYS = {"messages", "system", "prompt", "tool_input", "tool_result"}


def redact(value: Any, *, key: str = "", include_content: bool = False) -> Any:
    lowered = key.lower().replace("-", "_")
    if any(fragment in lowered for fragment in SECRET_FRAGMENTS):
        return "[REDACTED]"
    if lowered in CONTENT_KEYS and not include_content:
        return "[CONTENT REDACTED]"
    if isinstance(value, dict):
        return {
            str(child_key): redact(
                child_value,
                key=str(child_key),
                include_content=include_content,
            )
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, key=key, include_content=include_content) for item in value]
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": "[REDACTED EXCEPTION]"}
    return value


@dataclass(frozen=True, slots=True)
class ProbeResult:
    name: str
    passed: bool
    evidence: dict[str, Any]

    def redacted_dict(self) -> dict[str, Any]:
        evidence = redact(self.evidence)
        return {"name": self.name, "passed": self.passed, "evidence": evidence}


def snapshot_claude_state(root: Path) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    for child in root.rglob("*"):
        if child.is_file():
            stat = child.stat()
            result[str(child.relative_to(root))] = (stat.st_mtime_ns, stat.st_size)
    return result
```

Implement `probe_cli.py` with `argparse`, refuse to run unless `RUN_LIVE_CLAUDE_TESTS=1`, write JSON only to an explicitly supplied `--output` path, and print only probe name/pass status to stderr.

- [ ] **Step 4: Implement independent ambient-context, persistence, and attribution gates**

```python
# tests/live/test_prompt_purity.py
import os
from pathlib import Path

import pytest

from claude_sdk_proxy.probes import run_prompt_purity_probe


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_CLAUDE_TESTS") != "1",
    reason="requires the user's existing Claude login",
)


AMBIENT_CANARIES = {
    "claude_md": "AMBIENT-CLAUDE-MD-8dd4",
    "settings": "AMBIENT-SETTINGS-a31f",
    "skill": "AMBIENT-SKILL-3ca7",
    "agent": "AMBIENT-AGENT-691e",
    "mcp": "AMBIENT-MCP-b7c2",
    "memory": "AMBIENT-MEMORY-57df",
    "plugin": "AMBIENT-PLUGIN-2be9",
    "hook": "AMBIENT-HOOK-01c6",
}


@pytest.mark.anyio
async def test_ambient_sources_are_not_loaded(tmp_path: Path) -> None:
    config_root = plant_harmless_ambient_canaries(tmp_path, AMBIENT_CANARIES)
    result = await run_prompt_purity_probe(
        model=os.environ["CLAUDE_PROXY_TEST_MODEL"],
        cwd=tmp_path,
        system_prompt="PURITY-SYSTEM-8dd4",
        user_prompt="Reply with exactly PURITY-USER-72bf.",
        config_root=config_root,
    )
    assert result.passed, result.redacted_dict()
    assert result.evidence["advertised_tools"] == []
    assert result.evidence["advertised_mcp_servers"] == []
    assert result.evidence["advertised_skills"] == []
    assert result.evidence["advertised_agents"] == []
    assert result.evidence["ambient_canaries_seen"] == []


@pytest.mark.anyio
async def test_no_state_writes_and_attribution_is_observable(tmp_path: Path) -> None:
    result = await run_prompt_purity_probe(
        model=os.environ["CLAUDE_PROXY_TEST_MODEL"],
        cwd=tmp_path,
        system_prompt="PURITY-SYSTEM-8dd4",
        user_prompt="Reply with exactly PURITY-USER-72bf.",
    )
    assert result.evidence["unexpected_state_writes"] == []
    assert result.evidence["attribution_observable"] is True
    assert result.evidence["attribution_block_seen"] is False
```

`plant_harmless_ambient_canaries()` writes only inside `tmp_path`. It plants project-level `CLAUDE.md`, `.claude/settings.json`, `.claude/skills/canary/SKILL.md`, `.claude/agents/canary.md`, `.claude/memory/MEMORY.md`, `.mcp.json`, and `.claude-plugin/plugin.json`, plus a synthetic `CLAUDE_CONFIG_DIR` containing user-level `CLAUDE.md`, settings, skills, agents, memory, plugins, MCP configuration, and a harmless hook whose only possible effect is creating a sentinel inside `tmp_path`. Do not copy or link credentials into the synthetic root; authentication must remain the CLI's existing supported account mechanism. If the pinned runtime cannot authenticate while using a clean synthetic configuration directory, record `synthetic_config_isolated=false` and fail the isolation gate rather than skipping user-level evidence. The probe records the public init/effective-configuration shape and redacted event shapes and fails if any planted literal, capability, project/user instruction, plugin, MCP server, or hook sentinel becomes visible.

`run_prompt_purity_probe()` snapshots every file below `~/.claude` before and after (metadata only: relative path, inode where available, size, and nanosecond mtime), consumes every SDK event through `ResultMessage`, and separately reports project-canary isolation, state mutation, and attribution observability. It must record init-advertised tools/MCP servers/skills/agents and inspect only public SDK events or a documented redacted debug-capture interface for the attribution marker. If effective prompt attribution cannot be strongly observed, return `passed=False` with `attribution_not_observable`; model output is never evidence of absence.

- [ ] **Step 5: Add a forced near-context compaction gate**

Add `test_compaction_disabled_near_threshold`. Build options with all three public controls set together: `DISABLE_COMPACT=1`, `CLAUDE_CODE_AUTO_COMPACT_WINDOW=100000`, and `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=1`. Send a deterministic caller-owned prompt of at least 3,000 tokens so the deliberately tiny threshold would compact if enabled. After the response, call the pinned SDK's public `client.get_context_usage()` and require `isAutoCompactEnabled is False`; also require zero `compact_boundary` events and no summary/compaction-shaped event. If either the usage flag or event boundary is unavailable, the gate fails as `compaction_not_observable`.

- [ ] **Step 6: Prove structured user-turn input without prompt flattening**

Use the pinned SDK's documented asynchronous raw user-message envelope to send one user turn containing two adjacent text blocks with distinct whitespace canaries. Record only block count/order, hashes, and public event shapes. Require the request to succeed without joining, trimming, or injecting separator text. Persist the boolean as `structured_user_input`; failure blocks Phase 1 because the production backend must accept structured turns, not a single flattened string.

- [ ] **Step 7: Document and run the prompt-isolation probes**

Document these exact commands in `docs/feasibility/README.md`:

```bash
RUN_LIVE_CLAUDE_TESTS=1 \
CLAUDE_PROXY_TEST_MODEL=sonnet \
uv run pytest tests/live/test_prompt_purity.py -v -s
```

Run the command with a real configured model. Expected: every independent test passes: no ambient canary is loaded, no file anywhere under `~/.claude` is added/removed/modified, attribution absence is observable, forced near-threshold compaction remains disabled, structured user blocks remain distinct, and no built-in capability is advertised. Any failure is a stop condition.

- [ ] **Step 8: Commit the prompt-purity probe**

```bash
git add src/claude_sdk_proxy/probes.py src/claude_sdk_proxy/probe_cli.py tests/unit/test_probe_reports.py tests/live/test_prompt_purity.py docs/feasibility/README.md
git commit -m "test: add fail-closed prompt purity probe"
```

---

### Task 4: Prove Long-Lived Session, Streaming, and Thinking Semantics

**Files:**
- Modify: `src/claude_sdk_proxy/probes.py`
- Create: `tests/live/test_session_continuity.py`
- Create: `tests/live/test_thinking.py`

**Interfaces:**
- Consumes: isolated options and `ClaudeSDKClient`.
- Produces: `run_session_continuity_probe()` and `run_thinking_probe()` with redacted event-shape, ordering, usage, stop, and latency evidence.

- [ ] **Step 1: Write the live continuity tests**

```python
# tests/live/test_session_continuity.py
import os

import pytest

from claude_sdk_proxy.probes import run_session_continuity_probe


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_CLAUDE_TESTS") != "1",
    reason="requires the user's existing Claude login",
)


@pytest.mark.anyio
async def test_two_queries_share_native_context_and_stream() -> None:
    result = await run_session_continuity_probe(
        model=os.environ["CLAUDE_PROXY_TEST_MODEL"],
        nonce="NATIVE-CONTEXT-f81c",
    )
    assert result.passed, result.redacted_dict()
    assert result.evidence["query_count"] == 2
    assert result.evidence["partial_text_events"] > 0
    assert result.evidence["result_messages"] == 2
    assert result.evidence["second_answer_contains_nonce"] is True
    assert result.evidence["session_ids"] == 1
    assert result.evidence["partial_event_order_valid"] is True
    assert result.evidence["usage_source"] == "sdk_result"
    assert result.evidence["stop_shape_observed"] is True
```

- [ ] **Step 2: Implement the continuity probe**

Use one `ClaudeSDKClient` async context. First query: `Remember the literal token NATIVE-CONTEXT-f81c and answer READY.` Drain through its `ResultMessage`. Second query: `Return only the literal token I asked you to remember.` Drain again. Record redacted public event shape keys, content-block types and indices, partial-event ordering, stop fields, exact usage-field names, result success, distinct non-secret SDK session-ID count, and monotonic latency boundaries. Store hashes/counts rather than reasoning or full message bodies. The probe fails if deltas arrive after their block stop, terminal success precedes the final partial event, usage cannot be attributed to the SDK result, or a stop shape is unknown.

- [ ] **Step 3: Add per-model thinking and effort equivalence tests**

In `tests/live/test_thinking.py`, parameterize every configured public model name over the Cartesian product of thinking `null`, disabled, adaptive, and enabled at the documented minimum/default/maximum accepted budgets with effort `null`, low, medium, high, xhigh, and max. Test exact combinations, not independent field booleans, because one option may alter or invalidate the other. Require redacted init/result evidence to show the exact requested pair and compatible public thinking/signature event shapes. Mark `xhigh` supported only when public evidence proves exact `xhigh`; Anthropic's documented fallback to `high` is a failed equivalence result, not support. Record stable canonical combination keys in a model-keyed boolean allowlist. Normalize enabled keys as `thinking=enabled;effort=<value>` and separately record the exact validated inclusive budget range for each enabled tuple; a budget outside that range is unsupported:

```json
{
  "sonnet": {
    "thinking=null;effort=null": true,
    "thinking=disabled;effort=null": true,
    "thinking=adaptive;effort=high": true,
    "thinking=adaptive;effort=xhigh": false
  }
}
```

The same model entry contains `enabled_budget_ranges`, for example `{"thinking=enabled;effort=high": {"min": 1024, "max": 32000}}`. Missing `medium`, `max`, absent-value, or enabled-budget evidence means that exact tuple is unsupported rather than inherited from another row.

Phase 1 may advertise or accept only combinations that are `true` for the resolved model. A requested combination without an exact `true` entry receives `400 unsupported_parameter`; never rely on SDK fallback.

- [ ] **Step 4: Run the live continuity and thinking gates**

Run: `RUN_LIVE_CLAUDE_TESTS=1 CLAUDE_PROXY_TEST_MODEL=sonnet uv run pytest tests/live/test_session_continuity.py -v -s`

Expected: PASS with two successful `ResultMessage` objects from one client/session and partial events before each result.

Run: `RUN_LIVE_CLAUDE_TESTS=1 CLAUDE_PROXY_TEST_MODEL=sonnet uv run pytest tests/live/test_thinking.py -v -s`

Expected: PASS for every combination the manifest will expose; unsupported exact-equivalence combinations are recorded `false` and rejected later.

- [ ] **Step 5: Commit the session and thinking probes**

```bash
git add src/claude_sdk_proxy/probes.py tests/live/test_session_continuity.py tests/live/test_thinking.py
git commit -m "test: prove native session and thinking semantics"
```

---

### Task 5: Spike the Suspended Caller-Owned Tool Bridge

**Files:**
- Modify: `src/claude_sdk_proxy/probes.py`
- Create: `tests/live/test_tool_bridge.py`

**Interfaces:**
- Consumes: pinned SDK `create_sdk_mcp_server`, `tool`, SDK messages/hooks, and one actor-owned receive loop.
- Produces: `run_tool_bridge_probe(model, scenario)` and a complete model-keyed boolean lifecycle matrix deciding whether `tools` may be enabled in Phase 3.

- [ ] **Step 1: Write the single-call lifecycle test**

```python
# tests/live/test_tool_bridge.py
import os

import pytest

from claude_sdk_proxy.probes import run_tool_bridge_probe


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_CLAUDE_TESTS") != "1",
    reason="requires the user's existing Claude login",
)


@pytest.mark.anyio
@pytest.mark.parametrize("delay_seconds", [1, 60, 600])
async def test_callback_survives_public_response_gap(delay_seconds: int) -> None:
    result = await run_tool_bridge_probe(
        scenario="delayed_nonstream", delay_seconds=delay_seconds
    )
    assert result.passed, result.redacted_dict()
    assert result.evidence["tool_response_committed_before_resolution"] is True
    assert result.evidence["continuation_received"] is True
    assert result.evidence["later_user_turn_succeeded"] is True
```

- [ ] **Step 2: Implement the single-call bridge probe**

Register an in-process MCP tool named `external_echo` whose handler stores its arguments, signals `callback_started`, then awaits an actor-owned `Future`. Continuously drain the SDK receive iterator in one task. Once the complete public tool-use block is observed and the handler is suspended, record the simulated first-HTTP-response boundary, wait the requested delay, resolve the future with `{"content": [{"type": "text", "text": "external result 39a1"}]}`, and require the continuation to contain that result's requested transformation. After `ResultMessage`, issue a normal follow-up query on the same client.

Do not use SDK deferred-tool APIs, resume, transcript prompting, or a second receive iterator.

- [ ] **Step 3: Write and implement the parallel correlation test**

```python
@pytest.mark.anyio
async def test_parallel_identical_calls_correlate_in_reverse_order() -> None:
    result = await run_tool_bridge_probe(scenario="parallel_reverse", delay_seconds=1)
    assert result.passed, result.redacted_dict()
    assert result.evidence["public_call_count"] == 2
    assert result.evidence["distinct_public_ids"] == 2
    assert result.evidence["reverse_results_correlated"] is True
```

Prompt Claude to make two `external_echo` calls with identical arguments. Capture public tool-use IDs from assistant events and any IDs available to hooks/permissions. Resolve the two handler futures in reverse order with distinguishable results. The probe passes only if a documented/public correlation path maps each result to the correct public call. Ordering guesses, argument equality, SDK-private JSON-RPC IDs, and single-call fallbacks fail the gate.

- [ ] **Step 4: Implement the complete public tool lifecycle matrix**

Add separately named live tests and manifest booleans for every required behavior below. Each test records redacted public event shapes, monotonic boundaries, callback state, result/stop/usage fields, and whether a later ordinary user turn succeeds.

| Manifest gate | Required probe |
|---|---|
| `tool_nonstream_round_trip` | Suspend after a complete call, commit the intermediate non-stream response, resolve, then receive final success. |
| `tool_stream_round_trip` | Stream name and JSON argument deltas, suspend only after complete validated JSON, withhold terminal success, resolve, then continue in order. |
| `tool_parallel_correlation` | Two identical-argument calls have distinct public IDs and reverse-order results correlate correctly. |
| `tool_disconnect_survival` | Disconnect after every SSE boundary in separate cases; bounded actor drain either completes/replays or deterministically loses the session. |
| `tool_exact_retry_attach` | An identical retry during the suspended operation attaches to the same operation and never invokes the tool twice. |
| `tool_cancellation` | Session deletion cancels every suspended callback and receive task; no continuation or orphan task remains. |
| `tool_idle_timeout` | Waiting callbacks use the tool-wait timeout rather than normal idle eviction. |
| `tool_interrupt` | Explicit interrupt/delete while waiting produces the documented tombstone and no late commit. |
| `tool_shutdown` | Process-lifecycle shutdown cancels bridges, actors, and pending futures within the configured bound. |
| `tool_event_ordering` | Tool name/argument deltas, complete calls, stops, result continuation, usage, and terminal result are monotonic and unambiguous. |
| `tool_stop_reason` | The intermediate public turn has exactly the tool stop reason and the final turn has the observed normal stop. |
| `tool_usage_exact` | Both intermediate and final usage are attributable to public SDK result fields; no estimated tokens. |
| `tool_argument_delta_fidelity` | Concatenated streamed JSON argument deltas parse to the exact SDK-complete input without rewriting. |
| `tool_schema_immutability` | Tool names/descriptions/input schemas cannot change after session creation or automatic first bind. |
| `tool_long_wait_600s` | A 600-second suspension survives without callback timeout, hidden query, or session loss. |

Every boolean is independent so a diagnostic report identifies the exact failure. Record the full matrix beneath the exact configured model used by the probe. Phase 3 is disabled for a resolved model unless all gates for that model are present and true for the pinned SDK/CLI pair; a missing model is false and there is no partial production tool mode.

- [ ] **Step 5: Run short lifecycle and parallel gates**

Run: `RUN_LIVE_CLAUDE_TESTS=1 CLAUDE_PROXY_TEST_MODEL=sonnet uv run pytest tests/live/test_tool_bridge.py -v -k 'not 600' -s`

Expected: all selected cases PASS. If the parallel test fails, record tools as disabled; do not weaken the assertion.

- [ ] **Step 6: Run the 600-second gate separately**

Run: `RUN_LIVE_CLAUDE_TESTS=1 CLAUDE_PROXY_TEST_MODEL=sonnet uv run pytest tests/live/test_tool_bridge.py -v -k '600' -s`

Expected: PASS without callback timeout, iterator cancellation, extra query, or session loss.

- [ ] **Step 7: Commit the tool-spike evidence code**

```bash
git add src/claude_sdk_proxy/probes.py tests/live/test_tool_bridge.py
git commit -m "test: spike suspended external tool bridge"
```

---

### Task 6: Record the Feasibility Decision

**Files:**
- Modify: `src/claude_sdk_proxy/validated.py`
- Create: `docs/feasibility/validated-environment.json`
- Modify: `docs/feasibility/README.md`
- Create: `tests/unit/test_validated_environment.py`

**Interfaces:**
- Consumes: redacted results from Tasks 3–5 and the policy check.
- Produces: a machine-readable gate record consumed by later plans; `CORE_GATES_PASSED` and model-keyed `tools_gate_passed(model)` decisions.

- [ ] **Step 1: Add a failing schema test for the committed report**

```python
# tests/unit/test_validated_environment.py
import json
from pathlib import Path


def test_validated_environment_has_all_gate_decisions() -> None:
    report = json.loads(
        Path("docs/feasibility/validated-environment.json").read_text(encoding="utf-8")
    )
    assert report["agent_sdk_version"] == "0.2.148"
    assert report["claude_cli_version"] == "2.1.251"
    assert set(report["gates"]) == {
        "personal_subscription_policy",
        "ambient_context_isolation",
        "no_transcript_persistence",
        "attribution_absent_and_observable",
        "no_compaction",
        "structured_user_input",
        "native_session_continuity",
        "streaming",
    }
    assert all(isinstance(value, bool) for value in report["gates"].values())
    assert set(report["tool_gates_by_model"]["sonnet"]) == {
        "tool_nonstream_round_trip", "tool_stream_round_trip",
        "tool_parallel_correlation", "tool_disconnect_survival",
        "tool_exact_retry_attach", "tool_cancellation", "tool_idle_timeout",
        "tool_interrupt", "tool_shutdown", "tool_event_ordering",
        "tool_stop_reason", "tool_usage_exact", "tool_argument_delta_fidelity",
        "tool_schema_immutability", "tool_long_wait_600s",
    }
    assert all(
        isinstance(value, bool)
        for model in report["tool_gates_by_model"].values()
        for value in model.values()
    )
    assert report["thinking_by_model"]["sonnet"]["thinking=null;effort=null"] is True
    assert all(
        isinstance(value, bool)
        for model in report["thinking_by_model"].values()
        for value in model.values()
    )
    assert all(
        set(bounds) == {"min", "max"} and 0 < bounds["min"] <= bounds["max"]
        for model in report["enabled_budget_ranges_by_model"].values()
        for bounds in model.values()
    )
```

- [ ] **Step 2: Generate the redacted report from actual probe results**

Before running, manually re-read the current Agent SDK overview and Claude-plan notice linked by the spec. Because policy interpretation is not a technical probe, require the explicit CLI acknowledgement `--ack-personal-local-use-policy`; the CLI records only the boolean acknowledgement and source URLs, not credentials or account information.

Run: `RUN_LIVE_CLAUDE_TESTS=1 CLAUDE_PROXY_TEST_MODEL=sonnet uv run claude-proxy-probe all --ack-personal-local-use-policy --output docs/feasibility/validated-environment.json`

Expected: JSON contains versions, configured-model names, UTC validation timestamp, primary policy URLs, core booleans, every tool-lifecycle boolean keyed by the exact probed model, the per-model thinking/effort matrix, structured event-shape counts, and latency totals. It contains no prompts, response text, SDK session IDs, filesystem contents, credentials, or environment values.

- [ ] **Step 3: Encode the release decision without overriding failures**

Set `CORE_GATES_PASSED` to the conjunction of every value in `gates`, including structured user input and attribution observability. Implement `tools_gate_passed(model)` as the conjunction of the complete required key set in `tool_gates_by_model[model]`; missing models/keys are false and no subset is selected. Thinking/effort acceptance is model-specific and consults `thinking_by_model` at request validation time. If core is false, stop after committing the evidence and do not execute later plans. If tools are false for a model, later text plans may proceed but that model must reject and hide Phase 3 tools.

- [ ] **Step 4: Run the complete local validation suite**

Run: `RUN_LIVE_CLAUDE_TESTS=1 CLAUDE_PROXY_TEST_MODEL=sonnet uv run pytest -v`

Expected: all unit tests and enabled live tests PASS; skipped live tests are not acceptable for declaring `CORE_GATES_PASSED=true`.

Run: `uv run ruff check .`

Expected: no findings.

Run: `uv run mypy src/claude_sdk_proxy`

Expected: no errors.

- [ ] **Step 5: Commit the feasibility verdict**

```bash
git add src/claude_sdk_proxy/validated.py docs/feasibility/README.md docs/feasibility/validated-environment.json tests/unit/test_validated_environment.py
git commit -m "docs: record Agent SDK feasibility gates"
```
