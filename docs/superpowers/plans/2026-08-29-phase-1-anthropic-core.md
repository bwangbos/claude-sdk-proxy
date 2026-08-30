# Phase 1 Anthropic Text Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a text-only Anthropic Messages-shaped localhost proxy with one-shot, automatic linear-session, and explicit-session modes backed by validated long-lived Claude Agent SDK clients.

**Architecture:** Starlette routes validate Anthropic wire input into canonical immutable domain objects. A backend protocol isolates the Agent SDK, while one actor per live session enforces opaque-head compare-and-swap, idempotency, linear state transitions, bounded streaming, and fail-closed session loss. Control-plane registries hold only in-memory sessions and derived run tokens.

**Tech Stack:** Python 3.12–3.14, uv, Starlette, Uvicorn, Pydantic 2, AnyIO, Claude Agent SDK pinned by Phase 0, pytest, HTTPX, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-08-29-claude-subscription-api-proxy-design.md`

## Global Constraints

- Do not begin unless `docs/feasibility/validated-environment.json` has every core gate set to `true`.
- Text only; reject image, document, URL, audio, citation, and tool blocks in Phase 1.
- Bind only loopback; reject unexpected `Host` and `Origin`; emit no permissive CORS headers.
- Default parameter policy is strict. The Messages compatibility profile accepts only `max_tokens` as ignored and returns `X-Claude-Proxy-Ignored-Parameters: max_tokens`.
- Preserve the caller's system prompt exactly and keep model/system/thinking/dialect/policy immutable per session.
- Never reconstruct assistant history as prompt text.
- One automatic run token owns exactly one linear conversation; identical requests alias as retries.
- Normal streaming commits only after successful `ResultMessage` and iterator completion. Any post-header failure loses the session.
- No database, durable transcript, retry orchestrator, telemetry, dashboard, or non-loopback mode.

## File Map

- `src/claude_sdk_proxy/domain.py`: canonical requests, events, policies, heads, and errors.
- `src/claude_sdk_proxy/config.py`: environment parsing and configured models/limits.
- `src/claude_sdk_proxy/backend.py`: narrow backend protocol, SDK backend, and event normalization.
- `src/claude_sdk_proxy/workdirs.py`: empty proxy-owned per-actor working directories and cleanup.
- `src/claude_sdk_proxy/session.py`: actor state machine, transcript validation, idempotency, and registry.
- `src/claude_sdk_proxy/diagnostics.py`: recursive redaction and lifecycle/latency events with content off by default.
- `src/claude_sdk_proxy/tokens.py`: derived run-token minting, validation, TTL, and revocation.
- `src/claude_sdk_proxy/anthropic.py`: Messages request/response validation and SSE translation.
- `src/claude_sdk_proxy/control.py`: sessions, runs, capabilities, health, and models handlers.
- `src/claude_sdk_proxy/app.py`: Starlette composition, security middleware, exception mapping.
- `src/claude_sdk_proxy/server_cli.py`: `claude-proxy serve` entry point.
- `tests/fakes.py`: deterministic backend used by protocol/state tests.
- `tests/unit/`: domain, config, token, and session tests.
- `tests/integration/`: black-box HTTP/SSE tests.

---

### Task 1: Add HTTP Dependencies and Canonical Domain Types

**Files:**
- Modify: `pyproject.toml`
- Create: `src/claude_sdk_proxy/domain.py`
- Create: `tests/unit/test_domain.py`

**Interfaces:**
- Consumes: Phase 0 package and validated version constants.
- Produces: `Dialect`, `ParameterPolicy`, `ThinkingConfig`, `ContentBlock`, `CanonicalRequest`, `CanonicalEvent`, `ProxyError`, and `canonical_request_hash()`.

- [ ] **Step 1: Add dependencies and lock them**

Add to `[project].dependencies`:

```toml
"pydantic>=2.11,<3",
"starlette>=0.47,<1",
"uvicorn>=0.35,<1",
```

Add to the `dev` extra:

```toml
"httpx>=0.28,<1",
```

Run: `uv lock`

Expected: lock succeeds without changing `claude-agent-sdk==0.2.148`.

- [ ] **Step 2: Write failing canonicalization tests**

```python
# tests/unit/test_domain.py
from claude_sdk_proxy.domain import (
    CanonicalRequest,
    Dialect,
    ParameterPolicy,
    TextBlock,
    canonical_request_hash,
)


def test_hash_is_stable_and_excludes_transport_metadata() -> None:
    first = CanonicalRequest(
        dialect=Dialect.ANTHROPIC,
        model="sonnet",
        system="caller",
        messages=(("user", (TextBlock(text="hello"),)),),
        stream=False,
        parameter_policy=ParameterPolicy.STRICT,
        metadata={"request_id": "one"},
    )
    second = first.model_copy(update={"metadata": {"request_id": "two"}})
    assert canonical_request_hash(first) == canonical_request_hash(second)


def test_text_is_not_trimmed_or_rewritten() -> None:
    block = TextBlock(text="  caller text\n")
    assert block.text == "  caller text\n"
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_domain.py -v`

Expected: FAIL because `domain.py` does not exist.

- [ ] **Step 4: Implement immutable domain types**

```python
# src/claude_sdk_proxy/domain.py
from enum import StrEnum
from hashlib import sha256
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Dialect(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class ParameterPolicy(StrEnum):
    STRICT = "strict"
    MESSAGES_COMPAT = "messages_compat"


class TextBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal["text"] = "text"
    text: str


ContentBlock = TextBlock


class AdaptiveThinking(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal["adaptive"]
    display: Literal["summarized", "omitted"] | None = None


class EnabledThinking(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal["enabled"]
    budget_tokens: int = Field(gt=0)
    display: Literal["summarized", "omitted"] | None = None


class DisabledThinking(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal["disabled"]


ThinkingConfig = Annotated[
    AdaptiveThinking | EnabledThinking | DisabledThinking,
    Field(discriminator="type"),
]


class CanonicalRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    dialect: Dialect
    model: str
    system: str = ""
    messages: tuple[tuple[Literal["user", "assistant"], tuple[ContentBlock, ...]], ...]
    stream: bool = False
    thinking: ThinkingConfig | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    parameter_policy: ParameterPolicy = ParameterPolicy.STRICT
    metadata: dict[str, Any] = Field(default_factory=dict)


class CanonicalEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal[
        "message_start", "thinking_delta", "signature_delta", "text_delta", "message_end"
    ]
    text: str | None = None
    signature: str | None = None
    stop_reason: Literal["end_turn"] | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class ProxyError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def canonical_request_hash(request: CanonicalRequest) -> str:
    payload = request.model_dump(mode="json", exclude={"metadata"})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(encoded.encode()).hexdigest()
```

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/unit/test_domain.py -v`

Expected: PASS.

```bash
git add pyproject.toml uv.lock src/claude_sdk_proxy/domain.py tests/unit/test_domain.py
git commit -m "feat: add canonical proxy domain types"
```

---

### Task 2: Define the Backend Protocol and Deterministic Fake

**Files:**
- Create: `src/claude_sdk_proxy/backend.py`
- Create: `src/claude_sdk_proxy/workdirs.py`
- Modify: `src/claude_sdk_proxy/isolation.py`
- Create: `tests/fakes.py`
- Create: `tests/unit/test_backend.py`
- Create: `tests/unit/test_workdirs.py`

**Interfaces:**
- Consumes: `CanonicalEvent` and Phase 0 isolation builder.
- Produces: `BackendSession` protocol with `send_user_turn(blocks)`, `events()`, and `close()`; `AgentSdkBackendSession`; `FakeBackendSession`; `FakeBackendFactory`.

- [ ] **Step 1: Write the fake-backend contract test**

```python
# tests/unit/test_backend.py
import pytest

from claude_sdk_proxy.domain import TextBlock
from tests.fakes import FakeBackendSession


@pytest.mark.anyio
async def test_backend_emits_result_before_iterator_finishes() -> None:
    backend = FakeBackendSession.scripted_text("hello")
    await backend.send_user_turn((TextBlock(text="first"), TextBlock(text=" second")))
    assert backend.sent_turns == [
        (TextBlock(text="first"), TextBlock(text=" second"))
    ]
    events = [event async for event in backend.events()]
    assert [event.type for event in events] == [
        "message_start", "text_delta", "message_end"
    ]
    assert backend.result_succeeded is True
    assert backend.iterator_finished is True
```

- [ ] **Step 2: Implement the public backend boundary and fake**

```python
# src/claude_sdk_proxy/backend.py
from collections.abc import AsyncIterator
from typing import Protocol

from .domain import CanonicalEvent, ContentBlock


class BackendSession(Protocol):
    result_succeeded: bool
    iterator_finished: bool

    async def send_user_turn(self, blocks: tuple[ContentBlock, ...]) -> None: ...
    def events(self) -> AsyncIterator[CanonicalEvent]: ...
    async def close(self) -> None: ...
```

```python
# tests/fakes.py
from collections.abc import AsyncIterator

from claude_sdk_proxy.domain import CanonicalEvent, ContentBlock


class FakeBackendSession:
    def __init__(self, script: list[CanonicalEvent]) -> None:
        self._script = script
        self.sent_turns: list[tuple[ContentBlock, ...]] = []
        self.result_succeeded = False
        self.iterator_finished = False

    @classmethod
    def scripted_text(cls, text: str) -> "FakeBackendSession":
        return cls([
            CanonicalEvent(type="message_start"),
            CanonicalEvent(type="text_delta", text=text),
            CanonicalEvent(
                type="message_end", stop_reason="end_turn", input_tokens=1, output_tokens=1
            ),
        ])

    async def send_user_turn(self, blocks: tuple[ContentBlock, ...]) -> None:
        if not blocks:
            raise ValueError("at least one block is required")
        self.sent_turns.append(blocks)

    async def events(self) -> AsyncIterator[CanonicalEvent]:
        for event in self._script:
            yield event
        self.result_succeeded = True
        self.iterator_finished = True

    async def close(self) -> None:
        self.iterator_finished = True


class FakeBackendFactory:
    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.created: list[FakeBackendSession] = []

    @classmethod
    def text(cls, response_text: str) -> "FakeBackendFactory":
        return cls(response_text)

    def __call__(self, config: object) -> FakeBackendSession:
        session = FakeBackendSession.scripted_text(self.response_text)
        self.created.append(session)
        return session
```

- [ ] **Step 3: Implement the SDK adapter against the validated pair**

The production backend factory creates one mode-`0700`, empty, proxy-owned temporary working directory per actor beneath a configured temp root; it never uses repository, process, or home cwd. Reject symlink/nonempty roots and pass the new directory into `IsolationConfig`. The actor owns its workdir and removes it on backend-construction rollback, ordinary close, loss cleanup, expiry, and shutdown. One-shot requests get the same isolated lifecycle. `tests/unit/test_workdirs.py` plants ambient canaries in the process/repository cwd, proves the backend receives a distinct empty directory, and injects every construction/close/loss/expiry/shutdown path to prove cleanup and no cross-session reuse.

`AgentSdkBackendSession` owns exactly one `ClaudeSDKClient`. Extend `IsolationConfig` with canonical `thinking` and `effort`, consult Phase 0's exact per-model matrix, convert accepted settings to the pinned SDK's public `ThinkingConfig` dictionary and `EffortLevel`, and keep them immutable. `send_user_turn()` maps the tuple one-for-one to the pinned SDK's documented asynchronous raw user-message envelope: one `role="user"` message whose `content` list retains block order, type, and bytes. Phase 1 text blocks become raw `{type: "text", text: ...}` blocks; later phases extend this exhaustive mapping. Never join blocks or call `client.query(str)` for production requests. Refuse startup unless `structured_user_input=true` in the Phase 0 manifest. `events()` is the only code that calls `receive_response()`; translate partial thinking, signature, and text blocks in order, require a successful `ResultMessage`, preserve exact usage when present, and set `iterator_finished` only after the iterator exits. Unknown SDK event/stop shapes raise `ProxyError(502, "sdk_protocol_error", ...)`. Never expose SDK types outside this module.

- [ ] **Step 4: Run tests and commit**

Run: `uv run pytest tests/unit/test_backend.py tests/unit/test_workdirs.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/backend.py src/claude_sdk_proxy/workdirs.py src/claude_sdk_proxy/isolation.py tests/fakes.py tests/unit/test_backend.py tests/unit/test_workdirs.py
git commit -m "feat: isolate the Agent SDK backend protocol"
```

---

### Task 3: Implement the Linear Session Actor

**Files:**
- Create: `src/claude_sdk_proxy/session.py`
- Create: `tests/unit/test_session.py`

**Interfaces:**
- Consumes: `BackendSession`, `CanonicalRequest`, request hashes.
- Produces: `ActorState`, `SessionConfig`, `SessionReply`, `SessionActor.execute()`, `SessionActor.close()`, and `SessionRegistry`.

- [ ] **Step 1: Write state, retry, and stale-head tests**

```python
# tests/unit/test_session.py
import pytest

from claude_sdk_proxy.domain import CanonicalRequest, Dialect, ParameterPolicy, TextBlock, ProxyError
from claude_sdk_proxy.session import SessionActor, SessionConfig
from tests.fakes import FakeBackendSession


def request(text: str) -> CanonicalRequest:
    return CanonicalRequest(
        dialect=Dialect.ANTHROPIC,
        model="sonnet",
        messages=(("user", (TextBlock(text=text),)),),
        parameter_policy=ParameterPolicy.MESSAGES_COMPAT,
    )


@pytest.mark.anyio
async def test_success_advances_head_and_retry_is_cached() -> None:
    actor = SessionActor(
        config=SessionConfig.from_request(request("one")),
        backend=FakeBackendSession.scripted_text("answer"),
    )
    first = await actor.execute(request("one"), head=actor.head, idempotency_key="req-1")
    retry = await actor.execute(request("one"), head=first.previous_head, idempotency_key="req-1")
    assert retry == first
    assert actor.head == first.next_head


@pytest.mark.anyio
async def test_stale_head_does_not_touch_backend() -> None:
    actor = SessionActor(
        config=SessionConfig.from_request(request("one")),
        backend=FakeBackendSession.scripted_text("answer"),
    )
    with pytest.raises(ProxyError, match="stale"):
        await actor.execute(request("one"), head="head_stale", idempotency_key="req-2")
```

- [ ] **Step 2: Implement actor states and immutable configuration**

```python
# Core declarations in src/claude_sdk_proxy/session.py
from dataclasses import dataclass
from enum import StrEnum
import secrets

import anyio

from .domain import CanonicalEvent, CanonicalRequest, Dialect, ParameterPolicy, ProxyError, ThinkingConfig


class ActorState(StrEnum):
    IDLE = "idle"
    GENERATING = "generating"
    LOST = "lost"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class SessionConfig:
    dialect: Dialect
    model: str
    system: str
    parameter_policy: ParameterPolicy
    thinking: ThinkingConfig | None
    effort: str | None

    @classmethod
    def from_request(cls, request: CanonicalRequest) -> "SessionConfig":
        return cls(
            request.dialect,
            request.model,
            request.system,
            request.parameter_policy,
            request.thinking,
            request.effort,
        )


@dataclass(frozen=True, slots=True)
class SessionReply:
    previous_head: str
    next_head: str
    events: tuple[CanonicalEvent, ...]


@dataclass(slots=True)
class OperationRecord:
    prior_head: str
    request_hash: str
    idempotency_key: str
    done: anyio.Event
    reply: SessionReply | None = None
    error: ProxyError | None = None


def new_head() -> str:
    return f"head_{secrets.token_urlsafe(24)}"
```

Add an `OperationRecord` containing prior head, request hash, idempotency key, an `anyio.Event`, and nullable reply/error fields. `execute()` takes the actor lock only for a short reservation critical section: validate state/config/head/transcript; attach/replay only when prior head, idempotency key, and request hash all match the active or cached record; reject reuse of the same key with any different hash or prior head as `409 idempotency_conflict`; reject a different in-flight key immediately with `409 session_busy`; or install a new record and provisional head. Release the lock before backend work. Exact retries await the same record event and receive the same reply/error without a second SDK call.

The actor starts the operation in its own session-scoped AnyIO task group before the HTTP request waits on the record; the initiating request coroutine never owns the model task. This applies to streaming, non-streaming, automatic, explicit, and one-shot operations, so transport cancellation/disconnect cannot accidentally cancel SDK work. The owning actor task calls `send_user_turn()`, drains every backend event, and requires both backend success flags, then reacquires the lock to atomically append public events, advance the head, cache the reply, fill the record, and signal waiters. Once `send_user_turn()` has been invoked, any failure—including before the first public delta—marks the session `LOST`, because native SDK state may have advanced. Only validation/construction failure proven to occur before that call may leave the session reusable. Never hold an AnyIO lock while awaiting model I/O, an operation event, or a client stream.

- [ ] **Step 3: Test nonblocking reservation, retry attachment, and failures**

Use a controllable fake backend whose `send_user_turn()` signals entry and waits on a test event. Start one execute call, then assert a different key returns `409 session_busy` before the backend is released and the same key with changed content or changed prior head returns `409 idempotency_conflict`. Start an exact same-head/idempotency/hash retry and assert it remains attached; release the backend and require both callers to receive equal `SessionReply` values while `sent_turns` has length one. After commit, repeat the changed-content and changed-head same-key cases against the cache and require `idempotency_conflict`. Cancel the initiating non-streaming HTTP waiter and prove the actor-owned shielded operation continues and an exact retry can attach. Repeat with failures immediately after `send_user_turn()` and after a text delta; both must produce the same `session_lost` error for all waiters. Also assert cache insertion and public-head activation occur only after `result_succeeded` and `iterator_finished` become true.

- [ ] **Step 4: Add randomized transition tests**

Parametrize over `IDLE`, `GENERATING`, `LOST`, and `CLOSED`; assert the allowed operation/error matrix from spec section 8. Cover `GENERATING -> CLOSED` when deletion/shutdown cancellation wins, `IDLE -> CLOSED` on expiry/deletion/shutdown, and `LOST -> CLOSED` after the lost tombstone cleanup period. Inject backend failure before `send_user_turn()`, immediately after it, and after a text delta; only the proven pre-send path may remain reusable, while both post-send paths poison the session. Closed tombstones retain only opaque ID, machine-readable reason, and expiry and return `410 session_closed`; lost tombstones return `409 session_lost` until cleanup.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/unit/test_session.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/session.py tests/unit/test_session.py
git commit -m "feat: add fail-closed linear session actor"
```

---

### Task 4: Implement Explicit Sessions, Automatic Transcript Binding, and Derived Run Tokens

**Files:**
- Create: `src/claude_sdk_proxy/tokens.py`
- Modify: `src/claude_sdk_proxy/session.py`
- Create: `tests/unit/test_tokens.py`
- Create: `tests/unit/test_registry.py`
- Create: `tests/unit/test_automatic_routing.py`

**Interfaces:**
- Consumes: `SessionConfig`, backend factory.
- Produces: `RunTokenRegistry.mint()`, `.resolve()`, `.revoke()`; `SessionRegistry.create()`, `.get()`, `.close()`, `.expire()`; automatic transcript validation and derived retry identities.

- [ ] **Step 1: Write token expiry and revocation tests**

```python
# tests/unit/test_tokens.py
from datetime import UTC, datetime, timedelta

import pytest

from claude_sdk_proxy.domain import Dialect, ParameterPolicy, ProxyError
from claude_sdk_proxy.tokens import RunTokenRegistry


def test_run_token_binds_dialect_policy_and_expires() -> None:
    now = datetime(2026, 8, 29, tzinfo=UTC)
    registry = RunTokenRegistry(master_key=b"k" * 32, clock=lambda: now)
    minted = registry.mint(Dialect.ANTHROPIC, ParameterPolicy.MESSAGES_COMPAT, 60)
    assert registry.resolve(minted.api_key).dialect is Dialect.ANTHROPIC
    registry.clock = lambda: now + timedelta(seconds=61)
    with pytest.raises(ProxyError, match="expired"):
        registry.resolve(minted.api_key)
```

- [ ] **Step 2: Implement HMAC-derived in-memory tokens**

Use 32 random bytes for `run_id` material and HMAC-SHA256 with the in-memory master key. Store only the token digest, dialect, policy, expiry, revocation flag, and optional bound session ID. Compare signatures with `hmac.compare_digest`. Never persist or log raw tokens.

- [ ] **Step 3: Implement registry TTL and tombstones**

`SessionRegistry.create(config)` returns `session_id`, initial `head`, and expiry. The initial head represents an empty native conversation with fully fixed configuration; backend creation may remain lazy until the first valid user turn. `close()` cancels a generating actor, drops transcript/cache/backend, and retains only ID/reason/expiry until tombstone TTL. `expire()` cancels and closes idle actors and, in Phase 3, waiting actors; it never silently drops a generating actor or lets cancellation return it to `IDLE`. Capacity returns `429 session_capacity`.

- [ ] **Step 4: Define and test automatic transcript binding**

The actor stores a canonical public transcript consisting only of accepted caller turns and successfully committed proxy replies. A run token starts unbound. Its first request must contain exactly one nonempty user turn and no assistant turn; after full validation, it atomically creates/binds one session. For a continuation, the complete resent wire transcript must equal the stored public transcript byte-for-byte and block-for-block, followed by exactly one new user turn. An exact duplicate of the currently executing or last committed request derives `idempotency_key = "auto:" + canonical_request_hash(request)` and attaches/replays. A shorter transcript, a second initial-shaped request after binding, an altered assistant prefix, an altered prior user block, more than one new turn, or a continuation ending in assistant returns the spec's exact `409 session_ambiguous` without touching the backend. Simultaneous identical first requests share one bind/operation; while that operation is active a simultaneous divergent request returns `409 session_busy`, and after commit it returns `409 session_ambiguous`; neither case creates two sessions.

Explicit mode instead requires the caller's session ID, exact head, and idempotency key and never derives them. One-shot mode accepts only a single initial user turn, creates no reusable binding, and rejects history with exact `422 history_unavailable`.

Add tests for first bind, ordinary continuation, exact committed retry, exact in-flight retry, simultaneous identical initial requests, simultaneous divergent initial requests, stale/modified assistant prefix, modified prior user content, second initial request, multi-new-turn continuation, and one-shot history. Each test asserts backend call count and session-registry cardinality.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/unit/test_tokens.py tests/unit/test_registry.py tests/unit/test_automatic_routing.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/tokens.py src/claude_sdk_proxy/session.py tests/unit/test_tokens.py tests/unit/test_registry.py tests/unit/test_automatic_routing.py
git commit -m "feat: add in-memory session and run-token registries"
```

---

### Task 5: Validate Anthropic Requests and Render Responses

**Files:**
- Create: `src/claude_sdk_proxy/anthropic.py`
- Create: `tests/unit/test_anthropic.py`

**Interfaces:**
- Consumes: canonical types and `SessionReply`.
- Produces: `parse_messages_request()`, `render_messages_response()`, `render_anthropic_error()`.

- [ ] **Step 1: Write validation and ignored-field tests**

```python
# tests/unit/test_anthropic.py
import pytest

from claude_sdk_proxy.anthropic import parse_messages_request
from claude_sdk_proxy.domain import ParameterPolicy, ProxyError


BASE = {"model": "sonnet", "max_tokens": 256, "messages": [
    {"role": "user", "content": "hello"}
]}


def test_strict_rejects_required_but_unenforceable_max_tokens() -> None:
    with pytest.raises(ProxyError, match="max_tokens"):
        parse_messages_request(BASE, ParameterPolicy.STRICT)


def test_messages_profile_reports_max_tokens_ignored() -> None:
    parsed = parse_messages_request(BASE, ParameterPolicy.MESSAGES_COMPAT)
    assert parsed.ignored_parameters == ("max_tokens",)
    assert parsed.request.messages[0][1][0].text == "hello"


@pytest.mark.parametrize("field", ["temperature", "top_p", "top_k", "stop_sequences"])
def test_messages_profile_still_rejects_other_controls(field: str) -> None:
    with pytest.raises(ProxyError, match=field):
        parse_messages_request({**BASE, field: 0.5}, ParameterPolicy.MESSAGES_COMPAT)
```

- [ ] **Step 2: Implement an extra-forbid Pydantic wire schema**

Accept only model, system string or text blocks, alternating text-only messages, max_tokens, stream, metadata, `thinking`, `output_config.effort`, and the explicitly rejected control fields needed to return precise errors. Set `CanonicalRequest.dialect=Dialect.ANTHROPIC`. Validate thinking as adaptive, positive fixed budget, or disabled and effort as the canonical allowlist. Normalize the pair—including explicit absent values—to one canonical tuple key and require that exact key to be `true` in Phase 0's `thinking_by_model` entry for the resolved model; enabled thinking must also fall within that tuple's exact `enabled_budget_ranges_by_model` bounds. Never combine independent rows or infer support for medium/max. Otherwise return `400 unsupported_parameter` before session creation. Preserve SDK thinking/signature blocks in response order and keep both settings immutable after session creation. Require `anthropic-version: 2023-06-01` at the route boundary. Preserve all text bytes. Generate `msg_lp_<random>` IDs and return exact SDK usage; missing usage raises `502 sdk_protocol_error`.

- [ ] **Step 3: Add response golden tests**

Assert exact non-streaming JSON keys, `type="message"`, role, content order, model, `stop_reason="end_turn"`, nullable `stop_sequence`, and nonzero SDK-supplied usage. Assert no proxy warning object is added; ignored parameters appear only in the response header.

- [ ] **Step 4: Run tests and commit**

Run: `uv run pytest tests/unit/test_anthropic.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/anthropic.py tests/unit/test_anthropic.py
git commit -m "feat: add strict Anthropic Messages translation"
```

---

### Task 6: Compose the HTTP App and Control Plane

**Files:**
- Create: `src/claude_sdk_proxy/config.py`
- Create: `src/claude_sdk_proxy/control.py`
- Create: `src/claude_sdk_proxy/app.py`
- Create: `src/claude_sdk_proxy/server_cli.py`
- Create: `tests/integration/test_control_api.py`
- Create: `tests/integration/test_messages.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: adapters, registries, backend factory.
- Produces: `create_app(config, backend_factory)`, `/health`, `/v1/models`, `/_proxy/capabilities`, `/_proxy/sessions`, `/_proxy/runs`, and non-streaming `/v1/messages`.

- [ ] **Step 1: Write black-box control and Messages tests**

```python
# tests/integration/test_messages.py
import httpx
import pytest

from claude_sdk_proxy.app import create_app
from claude_sdk_proxy.config import ProxyConfig
from tests.fakes import FakeBackendFactory


@pytest.mark.anyio
async def test_messages_compat_request_returns_ignored_header() -> None:
    app = create_app(ProxyConfig.for_tests(), FakeBackendFactory.text("hello"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/v1/messages",
            headers={"anthropic-version": "2023-06-01", "host": "testserver"},
            json={"model": "sonnet", "max_tokens": 10, "messages": [
                {"role": "user", "content": "hi"}
            ]},
        )
    assert response.status_code == 200
    assert response.headers["x-claude-proxy-ignored-parameters"] == "max_tokens"
    assert response.json()["content"] == [{"type": "text", "text": "hello"}]
```

- [ ] **Step 2: Implement immutable environment configuration**

`ProxyConfig` parses loopback host, port, optional local master API key, model mapping, strict direct policy, capacities, TTLs, byte limits, and debug flags. `validate()` rejects non-loopback hosts and invalid limits. `for_tests()` uses model `sonnet`, Messages compatibility policy, bounded in-memory limits, and no real SDK.

- [ ] **Step 3: Implement routes and selection precedence**

Middleware authenticates local credentials, validates Host/Origin/content type, assigns a random request ID, and redacts error logs. Route selection order is recognized run token, explicit session headers, then one-shot. Validate the entire request before creating/mutating a backend.

`POST /_proxy/sessions` accepts exactly dialect, configured model, system blocks, an empty Phase 1 tools list, thinking/effort, parameter policy, and bounded TTL. It validates all immutable fields before allocating and returns opaque session ID, initial head, dialect, policy, and UTC expiry. `POST /_proxy/runs` accepts exactly dialect, policy, and bounded TTL, registers the token in the already-running server's in-memory registry, and returns run ID, opaque derived API-key value, and expiry; it does not mint a self-verifying token unknown to that server. When a master key is configured, both control mutations require it. DELETE run revokes its token and closes the bound session; DELETE session cancels/cleans the actor and leaves a reason-only tombstone. Tests assert dialect/policy/TTL immutability, run-token precedence over explicit headers, expiry/revocation, lazy backend creation, and no allocation on invalid control input.

- [ ] **Step 4: Add the server command**

Add `claude-proxy = "claude_sdk_proxy.server_cli:main"` to `[project.scripts]`. `claude-proxy serve` validates the feasibility manifest and runtime pair before calling `uvicorn.run(app, host=config.host, port=config.port)`. It prints no environment or credential values.

- [ ] **Step 5: Run integration tests and commit**

Run: `uv run pytest tests/integration/test_control_api.py tests/integration/test_messages.py -v`

Expected: PASS.

```bash
git add pyproject.toml uv.lock src/claude_sdk_proxy/config.py src/claude_sdk_proxy/control.py src/claude_sdk_proxy/app.py src/claude_sdk_proxy/server_cli.py tests/integration/test_control_api.py tests/integration/test_messages.py
git commit -m "feat: expose Anthropic and control-plane HTTP APIs"
```

---

### Task 7: Add Commit-Safe SSE Streaming

**Files:**
- Modify: `src/claude_sdk_proxy/anthropic.py`
- Modify: `src/claude_sdk_proxy/session.py`
- Modify: `src/claude_sdk_proxy/app.py`
- Create: `tests/integration/test_anthropic_stream.py`

**Interfaces:**
- Consumes: actor canonical event iterator.
- Produces: `anthropic_sse()`, provisional-head response headers, bounded buffering, and lost-session failure behavior.

- [ ] **Step 1: Write an exact SSE golden test**

Feed start, two deltas, and end events through the fake backend. Assert event order `message_start`, `content_block_start`, two `content_block_delta`, `content_block_stop`, `message_delta`, `message_stop`; assert `message_stop` is not yielded until backend success and iterator completion are true.

- [ ] **Step 2: Write a post-header failure test**

Use a fake backend that yields one text delta then raises. Assert the stream emits a dialect error event, omits terminal success, and the next request receives `409 session_lost` for both the old and provisional head.

- [ ] **Step 3: Test disconnect and replay at every SSE boundary**

Parameterize disconnect immediately after each emitted boundary: response headers, `message_start`, `content_block_start`, every content delta, `content_block_stop`, and `message_delta`. For each case, issue an exact retry while the operation is running and after it commits. Require in-flight retries to attach to the same `OperationRecord`, completed retries to replay the bounded cached reply, backend call count to remain one, and the public head/cache to activate only after successful result plus iterator completion. When configured replay capacity is exceeded, require deterministic cancellation and `session_lost` for every head.

- [ ] **Step 4: Implement bounded SSE translation**

Use an AnyIO memory object stream with configured event/byte bounds. Emit text deltas immediately, retain only the terminal success event, and release it after result/iterator validation. Preallocate the provisional head before response headers. The actor-owned shielded task continues draining after a client disconnect while the bounded replay buffer has capacity so an exact retry can attach/replay. Overflow explicitly cancels the actor operation and marks the session `LOST`; transport/request-task cancellation never implicitly decides backend lifetime.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/integration/test_anthropic_stream.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/anthropic.py src/claude_sdk_proxy/session.py src/claude_sdk_proxy/app.py tests/integration/test_anthropic_stream.py
git commit -m "feat: add commit-safe Anthropic SSE streaming"
```

---

### Task 8: Add Redacted Production Diagnostics

**Files:**
- Create: `src/claude_sdk_proxy/diagnostics.py`
- Modify: `src/claude_sdk_proxy/app.py`
- Modify: `src/claude_sdk_proxy/backend.py`
- Modify: `src/claude_sdk_proxy/session.py`
- Create: `tests/unit/test_diagnostics.py`
- Create: `tests/integration/test_diagnostics.py`

**Interfaces:**
- Consumes: request, actor, backend, and streaming lifecycle boundaries.
- Produces: recursive `Redactor`, `DiagnosticSink`, and monotonic `LatencyTrace`; local debug output with content disabled by default.

- [ ] **Step 1: Write recursive redaction and default-off tests**

Test nested dict/list/tuple structures containing authorization, cookie, OAuth token, API key, credential, environment, exception, stderr, prompt, message, tool input, and tool result values. Require secret-bearing keys and exception/stderr messages to be redacted recursively. Require prompt/message/tool content to be omitted unless a separate explicit local content flag is enabled; enabling lifecycle debug alone must not enable content. Verify input objects are never mutated.

- [ ] **Step 2: Write full lifecycle instrumentation tests**

Run one successful request, one divergent request, one exact retry, one client disconnect, and one backend failure. Require local structured events for validation start/end, session lookup/reservation, backend send, first partial event, result receipt, iterator completion, commit/loss, retry attach/replay, and disconnect handling. Assert monotonic timestamps and derived duration fields, random request/session references instead of tokens, no raw request/response bodies, no environment values, no SDK session IDs, and no credential fragments.

- [ ] **Step 3: Implement diagnostics at the owning boundaries**

`diagnostics.py` owns recursive redaction, immutable event construction, and a sink that is no-op unless local debugging is enabled. `app.py` records transport/validation/disconnect boundaries, `session.py` records reservation/attach/commit/loss and public-head timing, and `backend.py` records SDK send/first-event/result/iterator timing. Diagnostic failures are swallowed after a minimal redacted stderr marker and must never change request behavior. No telemetry or network sink is permitted.

- [ ] **Step 4: Run tests and commit**

Run: `uv run pytest tests/unit/test_diagnostics.py tests/integration/test_diagnostics.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/diagnostics.py src/claude_sdk_proxy/app.py src/claude_sdk_proxy/backend.py src/claude_sdk_proxy/session.py tests/unit/test_diagnostics.py tests/integration/test_diagnostics.py
git commit -m "feat: add redacted local lifecycle diagnostics"
```

---

### Task 9: Complete Security, Conformance, and Documentation

**Files:**
- Create: `tests/integration/test_security.py`
- Create: `tests/integration/test_official_anthropic_client.py`
- Create: `README.md`
- Create: `docs/protocol.md`

**Interfaces:**
- Consumes: complete Phase 1 app.
- Produces: documented text-only Anthropic service and black-box client compatibility evidence.

- [ ] **Step 1: Add security matrix tests**

Test unexpected Host, browser Origin, non-JSON mutation, invalid API key, missing API key when configured, non-loopback config, oversized body, expired token, stale head, session busy, and tombstoned session. Assert no CORS wildcard and no secret values in captured logs.

- [ ] **Step 2: Add official Anthropic client tests**

Instantiate the official Anthropic Python client with `base_url` pointing at the ASGI test server and a local key. Test non-streaming and streaming Messages calls under the Messages compatibility profile, exact error parsing, and ignored-parameter header visibility through `with_raw_response`. Do not claim compatibility for strict mode.

- [ ] **Step 3: Write operator documentation**

`README.md` must state personal-local-only scope, policy dependency, installation, separate `claude` login prerequisite, `claude-proxy serve`, supported fields, strict versus Messages profile, session loss behavior, no-compaction limit, and curl examples. `docs/protocol.md` documents control request/response JSON, headers, state/error table, token precedence, and restart semantics.

- [ ] **Step 4: Run the full Phase 1 gate**

Run: `uv run pytest -v`

Expected: all Phase 0 unit tests and all Phase 1 tests PASS; live tests may skip unless this is a release validation run.

Run: `uv run ruff check .`

Expected: no findings.

Run: `uv run mypy src/claude_sdk_proxy`

Expected: no errors.

- [ ] **Step 5: Commit Phase 1 documentation and conformance**

```bash
git add README.md docs/protocol.md tests/integration/test_security.py tests/integration/test_official_anthropic_client.py
git commit -m "docs: document the Anthropic text proxy contract"
```
