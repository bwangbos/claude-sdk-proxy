# Minimal Backend Capability Spike Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Determine, with a small reusable Python adapter for each backend, whether the installed Agent SDK and `claude -p` can support a pure streaming text proxy and an agent-harness-compatible proxy without prompt or tool emulation.

**Architecture:** Define one tiny canonical request/event model, then construct and inspect Agent SDK and `claude -p` invocations against fakes. A capability command reports structural and opt-in live evidence separately. This plan stops before HTTP; failure of the capability gate prevents us from building a compatibility server on false assumptions.

**Tech Stack:** Python 3.14, `claude-agent-sdk==0.2.148`, `asyncio`, standard-library dataclasses/JSON/argparse, pytest, Ruff, mypy. No HTTP dependency is added in this plan.

**Spec:** `docs/superpowers/specs/2026-09-03-minimal-subscription-proxy-reset-design.md`

## Global Constraints

- Python-only; add no native C, journal, ledger, database, durable recovery, flock, inode-provenance, or cleanup-successor behavior.
- Use only the official Agent SDK or documented `claude -p` flags; never extract OAuth credentials, reconstruct private requests, spoof metadata, or pass subscription tokens as API keys.
- Do not use `claude --bare`: installed CLI 2.1.258 documents that bare mode disables OAuth and keychain authentication.
- Never flatten assistant history or caller tool schemas into prompt text.
- Never use SDK MCP tools to execute harness-owned tools inside the proxy.
- Disable Claude built-in tools with `tools=[]` in SDK options and `--tools ""` in CLI arguments; `allowed_tools=[]` alone is not sufficient.
- Disable ambient settings, skills, agents, plugins, hooks, slash commands, MCP, session persistence, and project instruction discovery wherever the public backend surface permits.
- Each backend gets a fresh empty temporary working directory.
- No live/model/network/credential/profile command runs without a new explicit user authorization. Default tests use fakes only.
- Preserve the existing fail-closed policy evidence; do not claim vendor support or API equivalence.
- This spike adds at most 500 non-test Python lines and no production module over 220 lines.
- Do not modify or import the frozen native lifecycle/attestation implementation.

---

## File Map

- Create `src/claude_sdk_proxy/domain.py`: canonical request, events, capability statuses, reports, and viability gates.
- Create `src/claude_sdk_proxy/agent_sdk_backend.py`: inspectable Agent SDK option construction and one-shot streaming adapter.
- Create `src/claude_sdk_proxy/claude_p_backend.py`: documented CLI argv/input construction, streaming parser, and bounded child cleanup.
- Create `src/claude_sdk_proxy/capability_cli.py`: deterministic inspection plus explicitly gated live probe command.
- Modify `pyproject.toml`: add only the `claude-proxy-capabilities` console entry point.
- Create `tests/reset/test_domain.py`: hand-derived capability-gate tests.
- Create `tests/reset/test_agent_sdk_backend.py`: fake-query prompt-purity and event tests.
- Create `tests/reset/test_claude_p_backend.py`: fake executable argv/stdin/cwd/stream/cancellation tests.
- Create `tests/reset/test_capability_cli.py`: JSON report and live-gate tests.
- Create `tests/fixtures/fake_claude.py`: executable fake used only by CLI adapter tests.
- Create `tests/live/test_minimal_backend_capabilities.py`: opt-in authenticated smoke tests; never run by default.
- Create `docs/capabilities/2026-09-03-minimal-backend-capabilities.md`: structural evidence, live evidence status, and go/no-go conclusions.

---

### Task 1: Canonical capability boundary

**Files:**
- Create: `src/claude_sdk_proxy/domain.py`
- Create: `tests/reset/test_domain.py`

**Interfaces:**
- Produces: `CanonicalMessage`, `ToolDefinition`, `CanonicalRequest`, `TextDelta`, `Completed`, `BackendEvent`, `CapabilityStatus`, `CapabilityReport`, `UnsupportedFeature`, and `BackendFailure`.
- Consumes: no project runtime code.

- [ ] **Step 1: Write failing tests for the three distinct viability gates**

```python
from claude_sdk_proxy.domain import CapabilityReport, CapabilityStatus


def report(**overrides: CapabilityStatus) -> CapabilityReport:
    values: dict[str, CapabilityStatus] = {
        "authentication": "pass",
        "streaming": "pass",
        "prompt_construction": "pass",
        "multi_turn": "pass",
        "structured_tools": "pass",
    }
    values.update(overrides)
    return CapabilityReport(backend="fake", evidence=(), **values)


def test_single_turn_text_gate_does_not_require_history_or_tools() -> None:
    result = report(multi_turn="fail", structured_tools="fail")
    assert result.single_turn_text_viable is True
    assert result.compatibility_proxy_viable is False
    assert result.agent_harness_viable is False


def test_compatibility_gate_requires_multi_turn() -> None:
    assert report(multi_turn="fail").single_turn_text_viable is True
    assert report(multi_turn="fail").compatibility_proxy_viable is False
    assert report(structured_tools="fail").compatibility_proxy_viable is True


def test_agent_harness_gate_also_requires_tools() -> None:
    assert report(multi_turn="fail").agent_harness_viable is False
    assert report(structured_tools="untested").agent_harness_viable is False
    assert report().agent_harness_viable is True
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `uv run pytest tests/reset/test_domain.py -q`

Expected: collection fails because `claude_sdk_proxy.domain` does not exist.

- [ ] **Step 3: Implement the exact canonical types and gates**

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

CapabilityStatus: TypeAlias = Literal["pass", "fail", "untested"]
Role: TypeAlias = Literal["user", "assistant"]


@dataclass(frozen=True, slots=True)
class CanonicalMessage:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, object]


@dataclass(frozen=True, slots=True)
class CanonicalRequest:
    model: str
    system: str
    messages: tuple[CanonicalMessage, ...]
    tools: tuple[ToolDefinition, ...] = ()

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("model must not be empty")
        if not self.messages:
            raise ValueError("messages must not be empty")
        if any(not message.content for message in self.messages):
            raise ValueError("message content must not be empty")
        if any(
            re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", tool.name) is None
            for tool in self.tools
        ):
            raise ValueError("tool name is invalid")


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class Completed:
    stop_reason: str | None
    usage: dict[str, Any] | None


BackendEvent: TypeAlias = TextDelta | Completed


class UnsupportedFeature(ValueError):
    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"unsupported field {field}: {reason}")
        self.field = field
        self.reason = reason


class BackendFailure(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CapabilityReport:
    backend: str
    authentication: CapabilityStatus
    streaming: CapabilityStatus
    prompt_construction: CapabilityStatus
    multi_turn: CapabilityStatus
    structured_tools: CapabilityStatus
    evidence: tuple[str, ...]

    @property
    def single_turn_text_viable(self) -> bool:
        return all(
            value == "pass"
            for value in (
                self.authentication,
                self.streaming,
                self.prompt_construction,
            )
        )

    @property
    def compatibility_proxy_viable(self) -> bool:
        return self.single_turn_text_viable and self.multi_turn == "pass"

    @property
    def agent_harness_viable(self) -> bool:
        return (
            self.compatibility_proxy_viable and self.structured_tools == "pass"
        )
```

- [ ] **Step 4: Add validation tests with hand-derived expectations**

Add tests proving an empty model, empty message list, empty message content, or
tool name `"bad.name"` is rejected by `CanonicalRequest.__post_init__`. Use
literal inputs and assert the exact error strings shown in Step 3; do not call a
production validator to construct expected values.

- [ ] **Step 5: Implement only those validations and run GREEN**

Run: `uv run pytest tests/reset/test_domain.py -q`

Expected: all Task 1 tests pass.

- [ ] **Step 6: Run static checks and commit**

Run:

```bash
uv run ruff check src/claude_sdk_proxy/domain.py tests/reset/test_domain.py
uv run mypy src/claude_sdk_proxy/domain.py
git diff --check
```

Commit:

```bash
git add src/claude_sdk_proxy/domain.py tests/reset/test_domain.py
git commit -m "feat: define minimal backend capability boundary"
```

---

### Task 2: Agent SDK construction and streaming probe

**Files:**
- Create: `src/claude_sdk_proxy/agent_sdk_backend.py`
- Create: `tests/reset/test_agent_sdk_backend.py`

**Interfaces:**
- Consumes: `CanonicalRequest`, `BackendEvent`, `TextDelta`, `Completed`, `UnsupportedFeature`, and `BackendFailure` from Task 1; installed `ClaudeAgentOptions`, `query`, `AssistantMessage`, `ToolUseBlock`, `StreamEvent`, and `ResultMessage`.
- Produces: `AgentSdkBackend.build_options(request, cwd) -> ClaudeAgentOptions`, `AgentSdkBackend.stream(request) -> AsyncIterator[BackendEvent]`, and `AgentSdkBackend.structural_report() -> CapabilityReport`.

- [ ] **Step 1: Write the failing prompt-purity construction test**

```python
def test_build_options_disables_ambient_agent_behavior(tmp_path: Path) -> None:
    request = CanonicalRequest(
        model="claude-test",
        system="caller-system",
        messages=(CanonicalMessage("user", "caller-message"),),
    )
    options = AgentSdkBackend().build_options(request, tmp_path)

    assert options.model == "claude-test"
    assert options.system_prompt == "caller-system"
    assert options.tools == []
    assert options.allowed_tools == []
    assert options.skills == []
    assert options.setting_sources == []
    assert options.mcp_servers == {}
    assert options.strict_mcp_config is True
    assert options.permission_mode == "dontAsk"
    assert options.agents == {}
    assert options.plugins == []
    assert options.cwd == tmp_path
    assert options.include_partial_messages is True
    assert options.extra_args == {
        "safe-mode": None,
        "restricted": None,
        "disable-slash-commands": None,
        "no-session-persistence": None,
        "system-prompt-snapshot": "off",
    }
```

- [ ] **Step 2: Run the test and verify RED**

Run: `uv run pytest tests/reset/test_agent_sdk_backend.py::test_build_options_disables_ambient_agent_behavior -q`

Expected: collection fails because `AgentSdkBackend` does not exist.

- [ ] **Step 3: Implement the SDK option builder**

```python
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol

from claude_agent_sdk import ClaudeAgentOptions, query


class QueryFn(Protocol):
    def __call__(
        self, *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[Any]: ...


class AgentSdkBackend:
    def __init__(self, query_fn: QueryFn = query) -> None:
        self._query = query_fn

    def build_options(
        self, request: CanonicalRequest, cwd: Path
    ) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            model=request.model,
            system_prompt=request.system,
            tools=[],
            allowed_tools=[],
            skills=[],
            setting_sources=[],
            mcp_servers={},
            strict_mcp_config=True,
            permission_mode="dontAsk",
            agents={},
            plugins=[],
            cwd=cwd,
            include_partial_messages=True,
            extra_args={
                "safe-mode": None,
                "restricted": None,
                "disable-slash-commands": None,
                "no-session-persistence": None,
                "system-prompt-snapshot": "off",
            },
        )
```

Do not supply `env`: an empty SDK `env` mapping has backend-specific inheritance semantics, and authentication remains SDK-owned. Inspection logs may report environment names observed at the process boundary, never values.

- [ ] **Step 4: Write RED tests for unsupported exact replay and tools**

```python
def test_sdk_rejects_assistant_history_instead_of_flattening() -> None:
    request = CanonicalRequest(
        model="claude-test",
        system="",
        messages=(
            CanonicalMessage("user", "first"),
            CanonicalMessage("assistant", "prior answer"),
            CanonicalMessage("user", "next"),
        ),
    )
    with pytest.raises(UnsupportedFeature, match="messages"):
        AgentSdkBackend().prompt_for(request)


def test_sdk_rejects_caller_tools_instead_of_using_mcp() -> None:
    request = CanonicalRequest(
        model="claude-test",
        system="",
        messages=(CanonicalMessage("user", "use a tool"),),
        tools=(ToolDefinition("weather", "Get weather", {"type": "object"}),),
    )
    with pytest.raises(UnsupportedFeature, match="tools"):
        AgentSdkBackend().prompt_for(request)
```

Expected production rule: only exactly one user message is accepted by the spike. This is evidence of the public SDK boundary, not the final HTTP policy.

- [ ] **Step 5: Implement the exact one-user prompt gate and structural report**

```python
def prompt_for(self, request: CanonicalRequest) -> str:
    if request.tools:
        raise UnsupportedFeature("tools", "Agent SDK tools are built-in or MCP-executed")
    if len(request.messages) != 1 or request.messages[0].role != "user":
        raise UnsupportedFeature("messages", "public query input cannot replay assistant history")
    return request.messages[0].content


def structural_report(self) -> CapabilityReport:
    return CapabilityReport(
        backend="agent-sdk",
        authentication="untested",
        streaming="untested",
        prompt_construction="pass",
        multi_turn="fail",
        structured_tools="fail",
        evidence=(
            "ClaudeAgentOptions disables built-ins and ambient sources",
            "query accepts string or user-message iterable; assistant replay is not claimed",
            "caller tools would require proxy-executed MCP and are rejected",
        ),
    )
```

- [ ] **Step 6: Write a failing fake-query streaming test**

Inject a `query_fn` async iterator that records `prompt` and `options`, yields one `StreamEvent` whose raw event is a `content_block_delta/text_delta` containing `"hel"`, then `"lo"`, followed by one `ResultMessage` with literal stop reason and usage. Assert the adapter yields exactly:

```python
[TextDelta("hel"), TextDelta("lo"), Completed("end_turn", {"output_tokens": 1})]
```

Also make the fake yield an `AssistantMessage` containing a `ToolUseBlock` and
assert `UnsupportedFeature("tools", ...)` rather than forwarding a Claude
built-in tool.

- [ ] **Step 7: Implement minimal event mapping and run GREEN**

Map only:

```python
event["type"] == "content_block_delta"
and event["delta"]["type"] == "text_delta"
```

to `TextDelta`, and `ResultMessage` to `Completed`. Ignore documented SDK lifecycle/system messages. Reject tool-use deltas because this adapter has not passed caller-tool capability.

The streaming method has this exact control flow (with imports for
`AssistantMessage`, `ResultMessage`, `StreamEvent`, `ToolUseBlock`,
`TemporaryDirectory`, and the Task 1 domain types):

```python
async def stream(self, request: CanonicalRequest) -> AsyncIterator[BackendEvent]:
    prompt = self.prompt_for(request)
    terminal_seen = False
    with TemporaryDirectory(prefix="claude-proxy-") as directory:
        options = self.build_options(request, Path(directory))
        async for message in self._query(prompt=prompt, options=options):
            if isinstance(message, StreamEvent):
                event = message.event
                if event.get("type") != "content_block_delta":
                    continue
                delta = event.get("delta")
                if not isinstance(delta, dict):
                    raise BackendFailure("invalid Agent SDK delta")
                if delta.get("type") == "text_delta":
                    text = delta.get("text")
                    if not isinstance(text, str):
                        raise BackendFailure("invalid Agent SDK text delta")
                    yield TextDelta(text)
                elif delta.get("type") in {"input_json_delta", "tool_use"}:
                    raise UnsupportedFeature("tools", "Claude built-in tool event")
            elif isinstance(message, AssistantMessage) and any(
                isinstance(block, ToolUseBlock) for block in message.content
            ):
                raise UnsupportedFeature("tools", "Claude built-in tool event")
            elif isinstance(message, ResultMessage):
                if terminal_seen:
                    raise BackendFailure("duplicate Agent SDK result")
                terminal_seen = True
                if message.is_error:
                    raise BackendFailure("Agent SDK query failed")
                yield Completed(message.stop_reason, message.usage)
        if not terminal_seen:
            raise BackendFailure("Agent SDK stream ended without result")
```

Run: `uv run pytest tests/reset/test_agent_sdk_backend.py -q`

Expected: all Task 2 tests pass without launching Claude.

- [ ] **Step 8: Run static checks and commit**

```bash
uv run ruff check src/claude_sdk_proxy/agent_sdk_backend.py tests/reset/test_agent_sdk_backend.py
uv run mypy src/claude_sdk_proxy/agent_sdk_backend.py
git diff --check
git add src/claude_sdk_proxy/agent_sdk_backend.py tests/reset/test_agent_sdk_backend.py
git commit -m "feat: probe Agent SDK backend capabilities"
```

---

### Task 3: `claude -p` construction and streaming probe

**Files:**
- Create: `src/claude_sdk_proxy/claude_p_backend.py`
- Create: `tests/reset/test_claude_p_backend.py`
- Create: `tests/fixtures/fake_claude.py`

**Interfaces:**
- Consumes: Task 1 domain types.
- Produces: `ClaudePBackend.build_argv(request) -> tuple[str, ...]`, `ClaudePBackend.encode_input(request) -> bytes`, `ClaudePBackend.stream(request) -> AsyncIterator[BackendEvent]`, and `ClaudePBackend.structural_report() -> CapabilityReport`.

- [ ] **Step 1: Write the failing exact-argv test**

```python
def test_cli_argv_uses_documented_subscription_compatible_isolation() -> None:
    request = CanonicalRequest(
        model="claude-test",
        system="caller-system",
        messages=(CanonicalMessage("user", "caller-message"),),
    )
    argv = ClaudePBackend(executable=Path("/usr/bin/claude")).build_argv(request)

    assert argv == (
        "/usr/bin/claude",
        "--print",
        "--verbose",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--input-format", "stream-json",
        "--no-session-persistence",
        "--safe-mode",
        "--restricted",
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--tools", "",
        "--setting-sources", "",
        "--permission-mode", "dontAsk",
        "--system-prompt", "caller-system",
        "--system-prompt-snapshot", "off",
        "--model", "claude-test",
    )
    assert "--bare" not in argv
```

- [ ] **Step 2: Run the test and verify RED**

Run: `uv run pytest tests/reset/test_claude_p_backend.py::test_cli_argv_uses_documented_subscription_compatible_isolation -q`

Expected: collection fails because `ClaudePBackend` does not exist.

- [ ] **Step 3: Implement exact argv and one-user stream-json input**

Implement the constructor, prompt gate, argv, and input encoding exactly as:

```python
class ClaudePBackend:
    def __init__(self, executable: Path) -> None:
        self._executable = executable

    def prompt_for(self, request: CanonicalRequest) -> str:
        if request.tools:
            raise UnsupportedFeature(
                "tools", "claude --tools selects built-ins, not caller schemas"
            )
        if len(request.messages) != 1 or request.messages[0].role != "user":
            raise UnsupportedFeature(
                "messages", "documented stream-json cannot replay assistant history"
            )
        return request.messages[0].content

    def build_argv(self, request: CanonicalRequest) -> tuple[str, ...]:
        return (
            str(self._executable),
            "--print",
            "--verbose",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--input-format", "stream-json",
            "--no-session-persistence",
            "--safe-mode",
            "--restricted",
            "--disable-slash-commands",
            "--strict-mcp-config",
            "--tools", "",
            "--setting-sources", "",
            "--permission-mode", "dontAsk",
            "--system-prompt", request.system,
            "--system-prompt-snapshot", "off",
            "--model", request.model,
        )

    def encode_input(self, request: CanonicalRequest) -> bytes:
        content = self.prompt_for(request)
        payload = {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
        }
        return (json.dumps(payload, separators=(",", ":")) + "\n").encode()
```

`encode_input()` must return this exact UTF-8 JSON line for the test request, with compact separators and a final newline:

```json
{"type":"user","message":{"role":"user","content":"caller-message"},"parent_tool_use_id":null}
```

Reject `tools` and any message list other than exactly one user message with the same explicit `UnsupportedFeature` contract as Task 2. Do not translate either into prompt prose.

- [ ] **Step 4: Create a real fake executable and RED subprocess test**

The executable test fixture must:

1. write `sys.argv[1:]`, one stdin line, `os.getcwd()`, and sorted environment names to the JSON path named by `FAKE_CLAUDE_CAPTURE`;
2. emit two literal `{"type":"stream_event","event":...}` lines containing
   `content_block_delta/text_delta` values `"hel"` and `"lo"`;
3. emit one literal `{"type":"result",...}` line with all required CLI result
   fields plus `stop_reason="end_turn"` and `usage={"output_tokens":1}`;
4. exit zero.

The test asserts exact argv, exact stdin JSON, a fresh empty cwd, and exactly the three normalized events. It asserts environment names only; no environment values may appear in captured diagnostics.

- [ ] **Step 5: Implement bounded subprocess streaming**

Use `asyncio.create_subprocess_exec` with `stdin=PIPE`, `stdout=PIPE`, and
`stderr=PIPE`, the fresh temporary directory as `cwd`, and `env=None` so normal
CLI-owned authentication can use the inherited environment without the proxy
reading credential values. Write the single input line, drain, close stdin,
and parse stdout line-by-line. Drain stderr concurrently into a buffer capped
at 64 KiB so a verbose child cannot deadlock or allocate without bound.

The line parser accepts only these two output shapes:

```python
def event_from_line(line: bytes) -> BackendEvent | None:
    try:
        payload = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BackendFailure("invalid claude -p JSON") from error
    if not isinstance(payload, dict):
        raise BackendFailure("invalid claude -p event")
    if payload.get("type") == "stream_event":
        event = payload.get("event")
        if not isinstance(event, dict):
            raise BackendFailure("invalid claude -p stream event")
        if event.get("type") != "content_block_delta":
            return None
        delta = event.get("delta")
        if not isinstance(delta, dict):
            raise BackendFailure("invalid claude -p delta")
        if delta.get("type") == "text_delta" and isinstance(
            delta.get("text"), str
        ):
            return TextDelta(delta["text"])
        if delta.get("type") in {"input_json_delta", "tool_use"}:
            raise UnsupportedFeature("tools", "Claude built-in tool event")
        return None
    if payload.get("type") == "assistant":
        message = payload.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_use"
            for block in content
        ):
            raise UnsupportedFeature("tools", "Claude built-in tool event")
        return None
    if payload.get("type") == "result":
        if payload.get("is_error") is not False:
            raise BackendFailure("claude -p query failed")
        stop_reason = payload.get("stop_reason")
        usage = payload.get("usage")
        if stop_reason is not None and not isinstance(stop_reason, str):
            raise BackendFailure("invalid claude -p stop reason")
        if usage is not None and not isinstance(usage, dict):
            raise BackendFailure("invalid claude -p usage")
        return Completed(stop_reason, usage)
    return None
```

The stream loop tracks whether a `Completed` event was seen, rejects a second
terminal event, waits for the child and stderr-drain task on every exit path,
and raises `BackendFailure("claude -p stream ended without result")` when a
zero-exit child omits the result event.

On cancellation:

```python
process.terminate()
try:
    await asyncio.wait_for(process.wait(), timeout=1.0)
except TimeoutError:
    process.kill()
    await process.wait()
raise
```

Malformed JSON, an unexpected event shape, nonzero exit, or a stream without a terminal result raises a typed `BackendFailure` whose public string contains no raw stderr.

- [ ] **Step 6: Add RED/GREEN tests for cancellation and stderr bounds**

Add fake modes selected only by `FAKE_CLAUDE_MODE`:

- `hang`: emit one delta, install a SIGTERM marker, and wait;
- `stderr`: write more than 64 KiB to stderr, then exit nonzero;
- `malformed`: write a non-JSON stdout line.

Prove cancellation terminates the child within two seconds, stderr is truncated and absent from the public exception, and malformed output fails deterministically. Run:

`uv run pytest tests/reset/test_claude_p_backend.py -q`

- [ ] **Step 7: Implement the structural capability report**

The exact statuses are:

```python
CapabilityReport(
    backend="claude-p",
    authentication="untested",
    streaming="untested",
    prompt_construction="pass",
    multi_turn="fail",
    structured_tools="fail",
    evidence=(
        "documented safe/restricted/settings/MCP/tool/session flags are explicit",
        "--bare is excluded because it disables subscription authentication",
        "documented stream-json input does not claim assistant-history replay",
        "documented --tools accepts Claude built-ins, not caller schemas",
    ),
)
```

- [ ] **Step 8: Run static checks and commit**

```bash
uv run ruff check src/claude_sdk_proxy/claude_p_backend.py tests/reset/test_claude_p_backend.py tests/fixtures/fake_claude.py
uv run mypy src/claude_sdk_proxy/claude_p_backend.py
git diff --check
git add src/claude_sdk_proxy/claude_p_backend.py tests/reset/test_claude_p_backend.py tests/fixtures/fake_claude.py
git commit -m "feat: probe claude print backend capabilities"
```

---

### Task 4: Capability command, opt-in live probe, and decision report

**Files:**
- Create: `src/claude_sdk_proxy/capability_cli.py`
- Create: `tests/reset/test_capability_cli.py`
- Create: `tests/live/test_minimal_backend_capabilities.py`
- Create: `docs/capabilities/2026-09-03-minimal-backend-capabilities.md`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: both adapters' `structural_report()` and `stream()` methods.
- Produces: console command `claude-proxy-capabilities`; stable JSON schema with `backend`, five capability statuses, `single_turn_text_viable`, `compatibility_proxy_viable`, `agent_harness_viable`, `evidence`, and latency fields.

- [ ] **Step 1: Write failing deterministic CLI tests**

Invoke `capability_cli.main()` with captured stdout and assert `--backend all --json` returns two reports in literal order `agent-sdk`, `claude-p`. Without `--live`, both reports must contain:

```json
{
  "authentication": "untested",
  "streaming": "untested",
  "prompt_construction": "pass",
  "multi_turn": "fail",
  "structured_tools": "fail",
  "single_turn_text_viable": false,
  "compatibility_proxy_viable": false,
  "agent_harness_viable": false
}
```

Also assert `--live` exits 2 with a clear message unless
`CLAUDE_PROXY_LIVE=1` is present. Tests must not set that variable or invoke a
real backend.

- [ ] **Step 2: Run the tests and verify RED**

Run: `uv run pytest tests/reset/test_capability_cli.py -q`

Expected: collection fails because `capability_cli` does not exist.

- [ ] **Step 3: Implement deterministic JSON output and live gate**

Use `argparse` with:

```text
--backend agent-sdk|claude-p|all
--json
--live
--model (required value for live probes)
--claude-path PATH
```

`--live` requires all three conditions:

```python
args.live
and os.environ.get("CLAUDE_PROXY_LIVE") == "1"
and args.model is not None
```

The command never discovers credentials and never prints environment values.
Serialize reports through one explicit helper so field names and ordering do not
depend on dataclass internals:

```python
def report_payload(report: CapabilityReport) -> dict[str, object]:
    return {
        "backend": report.backend,
        "authentication": report.authentication,
        "streaming": report.streaming,
        "prompt_construction": report.prompt_construction,
        "multi_turn": report.multi_turn,
        "structured_tools": report.structured_tools,
        "single_turn_text_viable": report.single_turn_text_viable,
        "compatibility_proxy_viable": report.compatibility_proxy_viable,
        "agent_harness_viable": report.agent_harness_viable,
        "evidence": list(report.evidence),
        "metrics": {
            "latency_ms": None,
            "time_to_first_delta_ms": None,
            "text_delta_count": 0,
            "subprocess_count": None,
            "output_exact": None,
        },
    }
```

The live path replaces only the five literal metric values in that nested
mapping; the structural path leaves them exactly as shown. For `claude-p`, the
subprocess count is `1`; for the Agent SDK it is `null` because the public
`query()` surface does not expose process creation.

For a live run, copy the selected backend's structural report with
`dataclasses.replace`. Set `authentication="pass"` only after a non-error
`Completed` event. Set `streaming="pass"` only when at least one `TextDelta`
arrived before that terminal event; otherwise set it to `"fail"`. Catch typed
backend failures and return failed authentication/streaming statuses plus only
the exception class name as evidence. Do not serialize exception text because
future backend errors may contain sensitive material.

- [ ] **Step 4: Add opt-in live tests without running them**

Mark both tests `@pytest.mark.live`. Each skips unless
`CLAUDE_PROXY_LIVE == "1"` and `CLAUDE_PROXY_MODEL` is nonempty. The text probe
uses an empty system prompt and one user message:

```text
Reply with exactly: proxy-ok
```

It records only event types, text-delta count, total latency, time to first
delta, subprocess count where observable, and whether the terminal text exactly
equals `proxy-ok`. It records no raw credentials, environment values, prompts,
model output, or backend stderr. Latencies are monotonic-clock milliseconds
rounded to an integer; unavailable measurements serialize as `null`.

Do not add a live multi-turn or tool test that emulates unsupported inputs. The
structural failures remain failures unless a documented backend surface is
identified and separately designed.

- [ ] **Step 5: Write the capability report from deterministic evidence**

The report must state:

- Agent SDK and `claude -p` prompt construction are inspectably isolated for a
  single user turn.
- Authentication and real streaming remain `untested` until the operator runs
  the opt-in probe.
- Exact arbitrary assistant-history replay is not supported by the documented
  input surfaces inspected for SDK 0.2.148 and Claude CLI 2.1.258.
- Caller-defined raw API tool schemas are not supported without proxy-executed
  MCP/built-in tools or prompt emulation, both prohibited by the design.
- Consequently, `compatibility_proxy_viable` and `agent_harness_viable` are
  false for both backends at this checkpoint even if the live single-turn text
  probe later passes.
- Do not begin the agent-harness HTTP compatibility implementation unless a
  documented mechanism changes one or both failed capabilities.

- [ ] **Step 6: Add the console entry point and run deterministic GREEN**

Add to `[project.scripts]`:

```toml
claude-proxy-capabilities = "claude_sdk_proxy.capability_cli:main"
```

Run:

```bash
uv run pytest --strict-markers --forbid-skips -W error tests/reset
uv run ruff check src/claude_sdk_proxy/domain.py src/claude_sdk_proxy/agent_sdk_backend.py src/claude_sdk_proxy/claude_p_backend.py src/claude_sdk_proxy/capability_cli.py tests/reset tests/fixtures/fake_claude.py
uv run mypy src/claude_sdk_proxy/domain.py src/claude_sdk_proxy/agent_sdk_backend.py src/claude_sdk_proxy/claude_p_backend.py src/claude_sdk_proxy/capability_cli.py
git diff --check
```

Expected: all deterministic tests and static checks pass without launching
Claude or accessing the network.

- [ ] **Step 7: Run existing regression gates**

Run: `make check`

Expected: the existing unit and Darwin suites, Ruff, and mypy pass. Do not run
`tests/live` or any target that opts into authenticated Claude use.

- [ ] **Step 8: Verify the simplicity budget**

Run:

```bash
wc -l src/claude_sdk_proxy/domain.py src/claude_sdk_proxy/agent_sdk_backend.py src/claude_sdk_proxy/claude_p_backend.py src/claude_sdk_proxy/capability_cli.py
```

Expected: total production lines are at most 500 and every listed file is at
most 220 lines. If either limit is exceeded, simplify before commit; do not
raise the budget in implementation.

- [ ] **Step 9: Commit the deterministic spike**

```bash
git add pyproject.toml src/claude_sdk_proxy/capability_cli.py tests/reset/test_capability_cli.py tests/live/test_minimal_backend_capabilities.py docs/capabilities/2026-09-03-minimal-backend-capabilities.md
git commit -m "feat: report minimal backend capabilities"
```

- [ ] **Step 10: Stop at the live side-effect gate**

Do not run the live probe during plan execution without new explicit user
authorization. Present these commands for the operator's separate approval:

```bash
CLAUDE_PROXY_LIVE=1 CLAUDE_PROXY_MODEL=sonnet uv run claude-proxy-capabilities --backend agent-sdk --live --model sonnet --json
CLAUDE_PROXY_LIVE=1 CLAUDE_PROXY_MODEL=sonnet uv run claude-proxy-capabilities --backend claude-p --live --model sonnet --json
```

`sonnet` is the installed CLI's documented configurable alias. The operator may
choose a different configured model when authorizing the live probe. Do not
commit live output until it is redacted and reviewed.

---

## Plan completion gate

This plan produces a capability result, not an HTTP server. After independent
review and deterministic verification:

- A passing opt-in probe establishes only `single_turn_text_viable`; it does not
  authorize the compatibility HTTP MVP.
- The compatibility HTTP plan requires a documented, non-emulated route for
  assistant-history replay. Agent-harness compatibility additionally requires
  caller-defined structured tool schemas. The current inspected surfaces do
  not provide either route.
- If those capabilities remain false, stop rather than disguising transcripts
  or tools as prompt text.
