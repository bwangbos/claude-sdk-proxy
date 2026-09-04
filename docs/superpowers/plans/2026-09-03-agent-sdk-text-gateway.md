# Agent SDK Text Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a localhost OpenAI Chat Completions and Anthropic Messages text gateway backed by one persistent Claude Agent SDK client per fresh, linear conversation.

**Architecture:** Both HTTP dialects parse into one strict text-only request. A small session registry validates append-only transcripts, treats duplicate transcript heads as one lineage, and owns persistent `ClaudeSDKClient` sessions. The SDK adapter streams normalized text/result events; dialect renderers translate those events without buffering streaming responses.

**Tech Stack:** Python 3.14, `claude-agent-sdk==0.2.152`, Starlette 1.x, Uvicorn 0.x, pytest, pytest-anyio, Ruff, mypy.

**Spec:** `docs/research/2026-09-03-subscription-proxy-reassessment.md`

## Global Constraints

- Bind to `127.0.0.1` by default and describe the server as private, local, and single-user.
- The Agent SDK is the only production backend; `claude -p` remains a capability comparator only.
- Accept only fresh conversations that begin with exactly one text user turn and exact append-only continuations.
- Never flatten assistant history into a prompt or synthesize Claude transcript JSONL.
- Accept protocol-required `max_tokens` as advisory; reject explicit temperature, top-p, top-k, and stop controls.
- Configuration-only duplicates are one lineage: return `409` while in flight and replay the completed response afterward.
- Independent identical conversations require `X-Claude-Proxy-Session`; the header value is client-chosen, validated, and echoed.
- Never run two turns concurrently through one persistent SDK client; every conversation is single-flight even when request fingerprints differ.
- Apply a configurable whole-turn timeout (default 300 seconds), measured from reservation and covering fresh-session connect/initialize, query, and receive. Before response start, failures become an HTTP error; after partial streaming output, failures become a dialect SSE error followed by stream termination.
- Commit transcript and replay state before yielding the terminal success event. Cancellation, disconnect, send failure, or timeout before that commit closes and removes the SDK session.
- Keep tools disabled in this plan. External tools remain a separate experimental increment.
- Do not use `--safe-mode`; it disables MCP/hooks needed by the later tool increment.
- Disable built-in tools and ambient settings with `tools=[]`, `allowed_tools=[]`, `setting_sources=[]`, `skills=[]`, `agents={}`, `plugins=[]`, `strict_mcp_config=True`, restricted mode, an empty per-session working directory, and disabled auto-memory/session persistence.
- Keep each production module at or below 300 lines and this increment at or below 1,500 non-test Python lines.

---

### Task 1: Define the text conversation contract and persistent SDK session

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/claude_sdk_proxy/domain.py`
- Create: `src/claude_sdk_proxy/sdk_session.py`
- Create: `tests/gateway/fakes.py`
- Create: `tests/gateway/test_sdk_session.py`
- Modify: `tests/reset/test_domain.py`

**Interfaces:**
- Produces: `TextRequest`, `ConversationEvent`, `SdkSession`, and `SdkSessionFactory`.
- `TextRequest` fields are `model`, `system`, `messages`, optional advisory `max_tokens`, `stream`, and OpenAI's safe `include_usage` rendering request.
- `SdkSession.start()` opens one client; `SdkSession.stream_turn(prompt)` yields `TextDelta | Completed`; `SdkSession.close()` is idempotent.
- `SdkSessionFactory` is `Callable[[str, str], SdkSessionProtocol]`, receiving `(model, system)`.

- [ ] **Step 1: Write failing domain and SDK-session tests**

Name the breaks: empty/invalid transcripts entering the engine; safe mode returning; a second SDK process being created for turn two; built-in tool output being accepted.

```python
def test_text_request_requires_alternating_messages_ending_in_user() -> None:
    with pytest.raises(ValueError, match="alternate"):
        TextRequest(
            model="sonnet",
            system="",
            messages=(CanonicalMessage("user", "one"), CanonicalMessage("user", "two")),
            max_tokens=1024,
            stream=False,
            include_usage=False,
        )


@pytest.mark.anyio
async def test_sdk_session_reuses_one_client_for_two_turns(tmp_path: Path) -> None:
    client = FakeSdkClient(
        responses=(
            sdk_response("one", session_id="sdk-1"),
            sdk_response("two", session_id="sdk-1"),
        )
    )
    session = SdkSession(
        model="sonnet",
        system="system",
        directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
        client_factory=lambda options: client.capture_options(options),
    )

    await session.start()
    first = [event async for event in session.stream_turn("first")]
    second = [event async for event in session.stream_turn("second")]
    await session.close()

    assert first == [TextDelta("one"), Completed("end_turn", {"output_tokens": 1})]
    assert second == [TextDelta("two"), Completed("end_turn", {"output_tokens": 1})]
    assert client.connect_count == 1
    assert client.prompts == ["first", "second"]
    assert client.disconnect_count == 1
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
.venv/bin/pytest -q tests/reset/test_domain.py tests/gateway/test_sdk_session.py
```

Expected: collection fails because `TextRequest` and `sdk_session` do not exist.

- [ ] **Step 3: Add direct runtime dependencies and lock them**

Set project dependencies to:

```toml
dependencies = [
    "claude-agent-sdk==0.2.152",
    "starlette>=1.6,<2",
    "uvicorn>=0.52,<1",
]
```

Regenerate only the affected lock entries:

```bash
UV_CACHE_DIR=/private/tmp/claude-sdk-proxy-uv-cache uv lock --upgrade-package claude-agent-sdk
```

- [ ] **Step 4: Implement the minimal contract and SDK adapter**

Use these public shapes in `domain.py`:

```python
@dataclass(frozen=True, slots=True)
class TextRequest:
    model: str
    system: str
    messages: tuple[CanonicalMessage, ...]
    max_tokens: int | None
    stream: bool
    include_usage: bool = False

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("model must not be empty")
        if self.max_tokens is not None and self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if not self.messages:
            raise ValueError("messages must not be empty")
        if any(not message.content for message in self.messages):
            raise ValueError("message content must not be empty")
        expected = "user"
        for message in self.messages:
            if message.role != expected:
                raise ValueError("messages must alternate user and assistant")
            expected = "assistant" if expected == "user" else "user"
        if self.messages[-1].role != "user":
            raise ValueError("conversation must end with a user message")

    @property
    def next_prompt(self) -> str:
        if self.messages[-1].role != "user":
            raise ValueError("conversation must end with a user message")
        return self.messages[-1].content


type ConversationEvent = TextDelta | Completed


class SdkSessionProtocol(Protocol):
    async def start(self) -> None: ...
    def stream_turn(self, prompt: str) -> AsyncIterator[ConversationEvent]: ...
    async def close(self) -> None: ...


type SdkSessionFactory = Callable[[str, str], SdkSessionProtocol]
```

In `tests/gateway/fakes.py`, define `sdk_response(text, session_id)` as a tuple
containing one real `StreamEvent` text delta and one real successful
`ResultMessage` with `usage={"output_tokens": 1}`. `FakeSdkClient` implements
`connect()`, `query(prompt)`, `receive_response()`, and `disconnect()`, records
counts/prompts/options, and emits one configured response per query.

`SdkSession` constructs exactly one `ClaudeSDKClient` with:

```python
ClaudeAgentOptions(
    model=model,
    system_prompt=system,
    tools=[],
    allowed_tools=[],
    skills=[],
    setting_sources=[],
    mcp_servers={},
    strict_mcp_config=True,
    permission_mode="dontAsk",
    agents={},
    plugins=[],
    cwd=session_directory,
    include_partial_messages=True,
    stderr=_discard_stderr,
    max_buffer_size=64 * 1024,
    env={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
    extra_args={
        "restricted": None,
        "disable-slash-commands": None,
        "no-session-persistence": None,
    },
)
```

Map only raw text deltas and one successful `ResultMessage`. Reject tool-use blocks/deltas and redact SDK exception contents as `BackendFailure("Agent SDK query failed")`. Always disconnect before cleaning the `TemporaryDirectory`.

- [ ] **Step 5: Run focused tests and confirm GREEN**

```bash
.venv/bin/pytest -q tests/reset/test_domain.py tests/gateway/test_sdk_session.py
.venv/bin/ruff check src/claude_sdk_proxy/domain.py src/claude_sdk_proxy/sdk_session.py tests/gateway/test_sdk_session.py
.venv/bin/mypy src/claude_sdk_proxy/domain.py src/claude_sdk_proxy/sdk_session.py
```

- [ ] **Step 6: Commit Task 1**

```bash
git add pyproject.toml uv.lock src/claude_sdk_proxy/domain.py src/claude_sdk_proxy/sdk_session.py tests/reset/test_domain.py tests/gateway/fakes.py tests/gateway/test_sdk_session.py
git commit -m "feat: add persistent Agent SDK text sessions"
```

---

### Task 2: Implement deterministic transcript matching and replay

**Files:**
- Create: `src/claude_sdk_proxy/sessions.py`
- Modify: `tests/gateway/fakes.py`
- Create: `tests/gateway/test_sessions.py`

**Interfaces:**
- Consumes: `TextRequest`, `SdkSessionFactory`, and `ConversationEvent`.
- Produces: `SessionRegistry.open_turn(request, explicit_id)`, `TurnLease.stream()`, `TurnLease.response_headers`, `TurnLease.abort()`, `SessionConflict`, `SessionMismatch`, `SessionTimeout`, and `SessionRegistry.close()`.

- [ ] **Step 1: Write failing registry tests**

Name the breaks: imported assistant history spawning a process; edited history being accepted; duplicates creating a second session; in-flight duplicates joining silently; explicit IDs coalescing identical conversations; shutdown leaking clients.

```python
@pytest.mark.anyio
async def test_completed_duplicate_replays_without_new_sdk_turn() -> None:
    factory = FakeSessionFactory(outputs=("answer",))
    registry = SessionRegistry(factory)
    request = first_request("hello")

    first = await registry.open_turn(request, explicit_id=None)
    assert await collect(first.stream()) == completed_events("answer")
    duplicate = await registry.open_turn(request, explicit_id=None)

    assert await collect(duplicate.stream()) == completed_events("answer")
    assert factory.created == 1
    assert factory.sessions[0].prompts == ["hello"]


@pytest.mark.anyio
async def test_in_flight_duplicate_is_409_conflict() -> None:
    factory = BlockingSessionFactory()
    registry = SessionRegistry(factory)
    lease = await registry.open_turn(first_request("hello"), explicit_id=None)
    consume = asyncio.create_task(collect(lease.stream()))
    await factory.started.wait()

    with pytest.raises(SessionConflict, match="in flight"):
        await registry.open_turn(first_request("hello"), explicit_id=None)

    factory.release.set()
    await consume


@pytest.mark.anyio
async def test_different_turn_cannot_enter_same_in_flight_conversation() -> None:
    factory = BlockingSessionFactory()
    registry = SessionRegistry(factory)
    first = await registry.open_turn(first_request("hello"), explicit_id="lineage")
    consume = asyncio.create_task(collect(first.stream()))
    await factory.started.wait()

    with pytest.raises(SessionConflict, match="conversation is busy"):
        await registry.open_turn(
            continuation_request("hello", "answer", "next"),
            explicit_id="lineage",
        )

    factory.release.set()
    await consume


@pytest.mark.anyio
async def test_explicit_ids_keep_identical_conversations_independent() -> None:
    factory = FakeSessionFactory(outputs=("a", "b"))
    registry = SessionRegistry(factory)
    request = first_request("same")

    one = await registry.open_turn(request, explicit_id="client-one")
    two = await registry.open_turn(request, explicit_id="client-two")
    await collect(one.stream())
    await collect(two.stream())

    assert factory.created == 2
```

- [ ] **Step 2: Run the registry tests and confirm RED**

```bash
.venv/bin/pytest -q tests/gateway/test_sessions.py
```

Expected: collection fails because `claude_sdk_proxy.sessions` does not exist.

- [ ] **Step 3: Implement canonical identity and admission**

Use stable JSON plus SHA-256 over `(model, system, ordered role/content pairs)`; never include `stream`, `include_usage`, or advisory `max_tokens` in transcript identity. Validate explicit IDs with `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`.

For every session retain:

```python
@dataclass(slots=True)
class _Conversation:
    external_id: str
    explicit: bool
    model: str
    system: str
    transcript: tuple[CanonicalMessage, ...]
    backend: SdkSessionProtocol
    in_flight_fingerprint: str | None = None
    replay: dict[str, tuple[ConversationEvent, ...]] = field(default_factory=dict)
```

Extend `tests/gateway/fakes.py` with `FakeConversationSession`: `start()`
increments `start_count`, `stream_turn(prompt)` records the prompt and yields
`TextDelta(configured_text)` plus `Completed("end_turn",
{"output_tokens": 1})`, and `close()` increments `close_count` idempotently.
`FakeSessionFactory.__call__(model, system)` consumes one configured output,
records each created session, and exposes `created == len(sessions)`.

Admission rules reserve state under one `asyncio.Lock` but never await backend
startup or I/O while holding that lock:

1. An explicit ID selects only its conversation.
2. Without an ID, fingerprint lookup considers implicit conversations only; it never discovers or attaches to an explicit-ID conversation.
3. Otherwise, the request prefix selects exactly one implicit conversation head.
4. A fresh request contains exactly one user message and creates a session.
5. A continuation equals the stored transcript plus exactly one user message.
6. Any other assistant history, edit, stale branch, model change, or system change raises `SessionMismatch` before an SDK call.

Before invoking the backend, reject any new turn when that conversation is already in flight, even if its request fingerprint differs. At reservation, record `deadline = asyncio.get_running_loop().time() + turn_timeout_seconds`. `TurnLease.stream()` enters `asyncio.timeout_at(deadline)` before calling `backend.start()`, so fresh-session connect/initialize, query, and receive share one deadline. When `Completed` arrives, commit assistant text and cache all events under the registry lock before yielding `Completed` to the caller. On backend error, timeout, consumer cancellation, explicit `TurnLease.abort()`, or generator close before commit, close and remove the failed conversation. `TurnLease.abort()` is idempotent and is the HTTP layer's cleanup hook for response send failures and disconnects.

Add focused tests proving timeout invalidation, cancelled consumer invalidation,
idempotent abort, commit-before-terminal-yield, implicit lookup ignoring explicit
sessions, and `SessionRegistry.close()` disconnecting every retained backend.

- [ ] **Step 4: Run registry tests and confirm GREEN**

```bash
.venv/bin/pytest -q tests/gateway/test_sessions.py
.venv/bin/ruff check src/claude_sdk_proxy/sessions.py tests/gateway/test_sessions.py
.venv/bin/mypy src/claude_sdk_proxy/sessions.py
```

- [ ] **Step 5: Commit Task 2**

```bash
git add src/claude_sdk_proxy/sessions.py tests/gateway/fakes.py tests/gateway/test_sessions.py
git commit -m "feat: add append-only conversation registry"
```

---

### Task 3: Parse and render both compatibility dialects

**Files:**
- Create: `src/claude_sdk_proxy/anthropic_api.py`
- Create: `src/claude_sdk_proxy/openai_api.py`
- Create: `tests/gateway/test_anthropic_api.py`
- Create: `tests/gateway/test_openai_api.py`

**Interfaces:**
- Consumes: request JSON mappings and normalized conversation events.
- Produces: `parse_anthropic_request()`, `parse_openai_request()`, non-streaming response mappings, and stateless SSE framing/event/error encoders.

- [ ] **Step 1: Write failing parser tests**

Name the breaks: roles or content being flattened; required Anthropic `max_tokens` ignored; explicit unsupported generation controls silently accepted; tools accidentally entering the text gateway; disallowed models accepted.

```python
def test_openai_parser_preserves_exact_text_roles() -> None:
    request = parse_openai_request(
        {
            "model": "sonnet",
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "answer"},
                {"role": "user", "content": "next"},
            ],
            "max_tokens": 321,
            "stream": True,
        },
        allowed_models=frozenset({"sonnet"}),
    )

    assert request.system == "system"
    assert request.messages == (
        CanonicalMessage("user", "hello"),
        CanonicalMessage("assistant", "answer"),
        CanonicalMessage("user", "next"),
    )
    assert request.max_tokens == 321
    assert request.stream is True


@pytest.mark.parametrize("field,value", [("temperature", 0), ("top_p", 1), ("stop", ["x"])])
def test_openai_parser_rejects_explicit_unsupported_controls(field: str, value: object) -> None:
    body = {"model": "sonnet", "messages": [{"role": "user", "content": "hi"}], field: value}
    with pytest.raises(UnsupportedFeature) as error:
        parse_openai_request(body, frozenset({"sonnet"}))
    assert error.value.field == field
```

- [ ] **Step 2: Write failing renderer tests**

Use literal protocol fixtures. Verify Anthropic event order (`message_start`, content start/delta/stop, `message_delta`, `message_stop`) and OpenAI chunk order (assistant role, text deltas, terminal finish chunk, `[DONE]`). Verify non-streaming IDs, model, text, stop reason, and usage mapping.

```python
def test_openai_completed_event_emits_requested_usage_then_done() -> None:
    usage = {"input_tokens": 2, "output_tokens": 1}
    chunks = encode_openai_event(
        request_id="chatcmpl_test",
        model="sonnet",
        event=Completed("end_turn", usage),
        include_usage=True,
    )
    assert b'"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}' in chunks[-2]
    assert chunks[-1] == b"data: [DONE]\n\n"
```

- [ ] **Step 3: Run dialect tests and confirm RED**

```bash
.venv/bin/pytest -q tests/gateway/test_anthropic_api.py tests/gateway/test_openai_api.py
```

Expected: collection fails because both API modules are missing.

- [ ] **Step 4: Implement strict text parsers**

Anthropic accepts only `model`, `system` as string, text messages, required positive `max_tokens`, and boolean `stream`. OpenAI accepts one optional leading system message, text user/assistant messages, boolean `stream`, at most one positive `max_tokens` or `max_completion_tokens` value, `store` only when exactly `false`, and `stream_options` only as `{"include_usage": true|false}`. The token field is optional. Both reject `tools`, tool roles/content blocks, images, audio, explicit unsupported controls, unknown models, consecutive system messages, and malformed JSON shapes with `RequestValidationError(field, reason)`.

- [ ] **Step 5: Implement literal non-streaming and SSE renderers**

Use compact `json.dumps(..., separators=(",", ":"), ensure_ascii=False)`. Map `end_turn` to Anthropic `end_turn` and OpenAI `stop`. Map missing SDK counters to zero without fabricating cache counters. Preserve text exactly. The parsers store OpenAI `stream_options.include_usage` in `TextRequest.include_usage`; the session fingerprint excludes it. When OpenAI requested `include_usage`, the `Completed` encoder emits the standard final usage chunk with `choices: []` before `[DONE]`; otherwise it omits that chunk. Render backend failures through the HTTP layer, not as successful terminal events.

Keep streaming translation stateless and synchronous:

```python
def encode_openai_start(request_id: str, model: str) -> tuple[bytes, ...]: ...
def encode_openai_event(
    request_id: str,
    model: str,
    event: ConversationEvent,
    include_usage: bool,
) -> tuple[bytes, ...]: ...
def encode_openai_error(code: str, message: str) -> tuple[bytes, ...]: ...

def encode_anthropic_start(request_id: str, model: str) -> tuple[bytes, ...]: ...
def encode_anthropic_event(
    request_id: str,
    model: str,
    event: ConversationEvent,
) -> tuple[bytes, ...]: ...
def encode_anthropic_error(code: str, message: str) -> tuple[bytes, ...]: ...
```

The custom ASGI response owns the live `AsyncIterator[ConversationEvent]` and
calls these encoders one event at a time; no renderer buffers or consumes the
backend iterator.

- [ ] **Step 6: Run dialect tests and confirm GREEN**

```bash
.venv/bin/pytest -q tests/gateway/test_anthropic_api.py tests/gateway/test_openai_api.py
.venv/bin/ruff check src/claude_sdk_proxy/anthropic_api.py src/claude_sdk_proxy/openai_api.py tests/gateway/test_anthropic_api.py tests/gateway/test_openai_api.py
.venv/bin/mypy src/claude_sdk_proxy/anthropic_api.py src/claude_sdk_proxy/openai_api.py
```

- [ ] **Step 7: Commit Task 3**

```bash
git add src/claude_sdk_proxy/anthropic_api.py src/claude_sdk_proxy/openai_api.py tests/gateway/test_anthropic_api.py tests/gateway/test_openai_api.py
git commit -m "feat: add OpenAI and Anthropic text dialects"
```

---

### Task 4: Expose the localhost ASGI application and CLI

**Files:**
- Create: `src/claude_sdk_proxy/app.py`
- Create: `src/claude_sdk_proxy/cli.py`
- Create: `tests/gateway/asgi_client.py`
- Create: `tests/gateway/test_app.py`
- Modify: `pyproject.toml`
- Modify: `Makefile`
- Modify: `tests/unit/test_make_dependencies.py`

**Interfaces:**
- Consumes: `SessionRegistry`, both dialect modules, configured model allowlist.
- Produces: `create_app()`, `claude-proxy` console entry point, `/health`, `/v1/models`, `/v1/messages`, and `/v1/chat/completions`.

- [ ] **Step 1: Write failing end-to-end ASGI tests**

The custom test client must invoke the real ASGI app and collect status, headers, and body without adding `httpx`. Name the breaks: routes missing; wrong dialect envelopes; session header not echoed; in-flight duplicates not returning 409; backend details leaking; pre-header versus midstream failure semantics; send failure not aborting the turn; shutdown not closing sessions.

`tests/gateway/asgi_client.py` defines `AsgiResponse(status: int, headers:
dict[str, str], body: bytes)` with a `.json` property,
`lifespan_app(app)` as an async context manager that drives
`lifespan.startup`/`lifespan.shutdown`, and `post_json(app, path, body,
headers=None, fail_send_after=None)`. `post_json` sends one
`http.request` event containing compact UTF-8 JSON, records
`http.response.start` plus every `http.response.body` event, and invokes the
application with an HTTP ASGI scope whose client is `127.0.0.1`. When
`fail_send_after` is reached, its ASGI `send` raises `ConnectionError` to prove
the route aborts the lease. All app and live tests run inside `lifespan_app` so
startup state exists and shutdown cleanup is observable.

```python
@pytest.mark.anyio
async def test_openai_nonstream_conversation_continues_through_one_sdk_session() -> None:
    factory = FakeSessionFactory(outputs=("first answer", "second answer"))
    app = create_app(models=("sonnet",), session_factory=factory)

    async with lifespan_app(app):
        first = await post_json(
            app,
            "/v1/chat/completions",
            {
                "model": "sonnet",
                "messages": [{"role": "user", "content": "first"}],
                "max_tokens": 128,
            },
        )
        second = await post_json(
            app,
            "/v1/chat/completions",
            {
                "model": "sonnet",
                "messages": [
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": "first answer"},
                    {"role": "user", "content": "second"},
                ],
                "max_tokens": 128,
            },
        )

    assert first.status == 200
    assert first.json["choices"][0]["message"]["content"] == "first answer"
    assert second.json["choices"][0]["message"]["content"] == "second answer"
    assert factory.created == 1
    assert factory.sessions[0].prompts == ["first", "second"]
```

- [ ] **Step 2: Run app tests and confirm RED**

```bash
.venv/bin/pytest -q tests/gateway/test_app.py
```

Expected: collection fails because `claude_sdk_proxy.app` does not exist.

- [ ] **Step 3: Implement routes and stable errors**

`create_app(models, session_factory, turn_timeout_seconds=300.0)` creates one registry in lifespan state. Route handlers parse `await request.json()`, pass `X-Claude-Proxy-Session` to admission, echo the resolved session ID, and return JSON or a small custom ASGI streaming response according to `TextRequest.stream`.

For streaming, call `await lease.stream().__anext__()` to prefetch the first
`ConversationEvent` before constructing any synthetic protocol framing or
sending `http.response.start`. A failure during admission or that backend-event
prefetch uses the normal HTTP error mapping. After headers start, catch backend/timeout failures
and send a dialect error SSE event (`event: error` for Anthropic; an OpenAI
`data: {"error":...}` record followed by `[DONE]`) while keeping status 200.
If ASGI `send` fails or the response task is cancelled, `await lease.abort()`
and `await stream.aclose()` before propagating cancellation/disconnect.

The custom response must also run a disconnect-listener task that keeps reading
ASGI receive events and calls `lease.abort()` immediately on
`http.disconnect`. Race the sender and listener; a disconnect cancels the
sender, while normal sender completion cancels the listener. Do not rely on
Uvicorn to cancel the application task or make `send()` fail.

Map failures deterministically:

```text
400 invalid_request      malformed JSON or field shape
400 unsupported_feature explicit unsupported protocol feature
404 model_not_found      model outside configured allowlist
409 request_in_flight    duplicate active transcript head
409 session_mismatch     edit, stale branch, imported history, or changed config
504 backend_timeout      whole-turn timeout before response start
502 backend_error        redacted SDK/process failure
```

Use OpenAI `{"error":{"message", "type", "param", "code"}}` on the OpenAI route and Anthropic `{"type":"error","error":{"type", "message"}}` on the Anthropic route. Never include raw exception strings for backend failures.

- [ ] **Step 4: Implement CLI and packaging**

Add:

```toml
[project.scripts]
claude-proxy = "claude_sdk_proxy.cli:main"
claude-proxy-capabilities = "claude_sdk_proxy.capability_cli:main"
claude-proxy-probe = "claude_sdk_proxy.probe_cli:main"
```

The CLI accepts `--host` defaulting to `127.0.0.1`, `--port` defaulting to `8317`, and repeatable `--model` defaulting to `sonnet`. Reject non-loopback hosts. Run `uvicorn.run(create_app(...), host=host, port=port)`.

Add a `gateway` Make target running `tests/gateway`, and include it in `check` without changing the existing unit/Darwin gates. Update `tests/unit/test_make_dependencies.py` so its expected Make dependency trace includes `gateway` exactly once in the intended order.

- [ ] **Step 5: Run app tests and confirm GREEN**

```bash
.venv/bin/pytest -q tests/gateway
.venv/bin/ruff check src/claude_sdk_proxy/app.py src/claude_sdk_proxy/cli.py tests/gateway
.venv/bin/mypy src/claude_sdk_proxy/app.py src/claude_sdk_proxy/cli.py
```

- [ ] **Step 6: Commit Task 4**

```bash
git add pyproject.toml Makefile src/claude_sdk_proxy/app.py src/claude_sdk_proxy/cli.py tests/gateway tests/unit/test_make_dependencies.py
git commit -m "feat: expose localhost compatibility gateway"
```

---

### Task 5: Prove the text gateway against the real authenticated SDK

**Files:**
- Create: `tests/live/test_gateway_text.py`
- Create: `tests/integration/test_pi_gateway.py`
- Create: `tests/fixtures/pi_text_client.mjs`
- Modify: `docs/feasibility/README.md`
- Modify: `docs/capabilities/2026-09-03-minimal-backend-capabilities.md`

**Interfaces:**
- Consumes: the installed host Claude login and finished gateway.
- Produces: a real Pi `openai-completions` network fixture, one bounded live SDK first-turn/continuation test, and user-facing launch/configuration instructions.

- [ ] **Step 1: Write the gated live test before running it**

```python
@pytest.mark.live
@pytest.mark.anyio
async def test_live_gateway_preserves_linear_context() -> None:
    if os.environ.get("CLAUDE_PROXY_LIVE") != "1":
        pytest.fail("live test requires CLAUDE_PROXY_LIVE=1")
    live_model = os.environ.get("CLAUDE_PROXY_LIVE_MODEL", "")
    if not live_model:
        pytest.fail("live test requires CLAUDE_PROXY_LIVE_MODEL")
    app = create_app(models=(live_model,))
    marker = "AMBER-731"
    first_body = {
        "model": live_model,
        "messages": [{"role": "user", "content": f"Remember {marker}. Reply READY."}],
        "max_tokens": 64,
    }
    async with lifespan_app(app):
        first = await post_json(app, "/v1/chat/completions", first_body)
        first_text = first.json["choices"][0]["message"]["content"]
        second = await post_json(
            app,
            "/v1/chat/completions",
            {
                "model": live_model,
                "messages": [
                    {"role": "user", "content": first_body["messages"][0]["content"]},
                    {"role": "assistant", "content": first_text},
                    {"role": "user", "content": "Return only the marker."},
                ],
                "max_tokens": 64,
            },
        )
    assert first.status == 200
    assert second.status == 200
    assert second.json["choices"][0]["message"]["content"] == marker
```

The helper must fail closed unless `CLAUDE_PROXY_LIVE=1` and `CLAUDE_PROXY_LIVE_MODEL` are present. It must use synthetic text only and never inspect credentials.

- [ ] **Step 2: Write a real Pi provider integration fixture**

`tests/fixtures/pi_text_client.mjs` imports the installed Pi AI package named by
`PI_AI_MODULE`, constructs an `openai-completions` model pointing at
`PROXY_BASE_URL`, and uses Pi's real streaming provider twice with an explicit
`X-Claude-Proxy-Session`. It prints one JSON result containing assembled text,
finish reasons, and errors. It accepts a scenario argument: `linear`, `retry`,
`abort`, or `timeout`. Leave Pi's normal `store: false` and
`stream_options: {"include_usage": true}` behavior enabled so the fixture
proves the proxy accepts both and returns the requested usage chunk.

`tests/integration/test_pi_gateway.py` starts Uvicorn on a kernel-selected
loopback port with injected fake sessions and runs that Node fixture as a real
subprocess. Assert:

```text
linear  -> first and continuation text succeed through one backend session
retry   -> identical completed request replays and creates no SDK turn
abort   -> AbortController closes the HTTP stream and registry removes/closes the session
timeout -> stalled backend yields Pi's surfaced stream error and registry removes/closes the session
```

Resolve `PI_AI_MODULE` from the installed `pi` executable's package root; if
Pi or its AI module is missing, fail with an installation instruction rather
than skip. This fixture performs localhost I/O only and must not invoke a real
model.

- [ ] **Step 3: Run offline verification**

```bash
.venv/bin/pytest -q --strict-markers --forbid-skips -W error tests/reset tests/gateway tests/integration
.venv/bin/ruff check .
.venv/bin/mypy src/claude_sdk_proxy
git diff --check
```

- [ ] **Step 4: Run the live gateway test in the normal host context**

```bash
CLAUDE_PROXY_LIVE=1 CLAUDE_PROXY_LIVE_MODEL=sonnet .venv/bin/pytest -q -m live tests/live/test_gateway_text.py
```

Expected: two HTTP turns pass through one SDK session and the second response returns the exact synthetic marker.

- [ ] **Step 5: Document usage and boundaries**

Document:

```bash
claude-proxy --model sonnet
```

Pi configuration uses `openai-completions`, `http://127.0.0.1:8317/v1`, any nonempty dummy API key, `supportsDeveloperRole: false`, and no sampling or reasoning options. State explicitly that imported histories, editing/branching, exact output-token caps, tools, public service, and restart recovery are unsupported in this text increment.

- [ ] **Step 6: Run final verification**

```bash
make check
CLAUDE_PROXY_LIVE=1 CLAUDE_PROXY_LIVE_MODEL=sonnet .venv/bin/pytest -q --strict-markers -m live tests/live/test_gateway_text.py
git diff --check
git status --short
```

- [ ] **Step 7: Commit Task 5**

```bash
git add tests/live/test_gateway_text.py tests/integration/test_pi_gateway.py tests/fixtures/pi_text_client.mjs docs/feasibility/README.md docs/capabilities/2026-09-03-minimal-backend-capabilities.md
git commit -m "docs: verify Agent SDK text gateway"
```

---

## Deferred follow-up: experimental tools

After this plan is green, write a separate plan for the MCP continuation bridge. That plan must keep tools feature-gated and prove exact tool-use ID correlation, identical and parallel calls, cancellation, timeout, disconnect, and stable external-name mapping before any generic tool-support claim.
