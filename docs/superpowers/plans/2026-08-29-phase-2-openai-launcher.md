# Phase 2 OpenAI Adapter and Harness Launcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a minimal OpenAI Chat Completions-shaped adapter and a harness-neutral launcher that gives ordinary single-conversation CLI harnesses automatic linear-session behavior without source modifications.

**Architecture:** OpenAI wire models translate into the existing canonical domain and reuse the Phase 1 actor/backend unchanged. The launcher obtains a short-lived derived run token from the local control plane, injects dialect-standard base-URL/API-key environment variables into a child process, forwards signals, and revokes the run token on exit.

**Tech Stack:** Existing Phase 1 Python stack, standard-library `argparse`/`subprocess`/`signal`, OpenAI Python client for black-box tests.

**Spec:** `docs/superpowers/specs/2026-08-29-claude-subscription-api-proxy-design.md`

## Global Constraints

- Phase 1 must pass unchanged before this plan starts.
- `/v1/messages` remains canonical; OpenAI code may depend on canonical types but not on Agent SDK types.
- Accept text-only Chat Completions with at most one leading `system` message; reject `developer`, later system, multimodal, tool, and unsupported roles.
- Never invent usage, finish reasons, sampling, multiple choices, logprobs, or response-format behavior.
- `messages_compat` accepts and reports only `max_tokens`/`max_completion_tokens` as ignored for the OpenAI dialect; all other unsupported controls stay strict.
- A generated run token binds one dialect, parameter profile, TTL, and one linear conversation.
- The launcher never reads Claude credentials and never places the local master key in the child environment.

## File Map

- `src/claude_sdk_proxy/openai_adapter.py`: Chat Completions validation and canonical translation.
- `src/claude_sdk_proxy/app.py`: `/v1/chat/completions` route.
- `src/claude_sdk_proxy/launcher.py`: control-plane client, environment construction, child lifecycle.
- `src/claude_sdk_proxy/server_cli.py`: `run` command dispatch.
- `tests/unit/test_openai_adapter.py`: request, response, and field-policy tests.
- `tests/unit/test_launcher.py`: environment and token-redaction tests.
- `tests/integration/test_openai.py`: non-streaming and SSE route tests.
- `tests/integration/test_launcher.py`: child-process/control-plane lifecycle tests.
- `tests/integration/test_official_openai_client.py`: black-box OpenAI SDK tests.
- `docs/harnesses.md`: generic configuration and launcher contract.

---

### Task 1: Translate Text-Only OpenAI Requests and Responses

**Files:**
- Create: `src/claude_sdk_proxy/openai_adapter.py`
- Create: `tests/unit/test_openai_adapter.py`

**Interfaces:**
- Consumes: `CanonicalRequest`, `CanonicalEvent`, `ParameterPolicy`.
- Produces: `parse_chat_completion_request()`, `render_chat_completion_response()`, and `render_openai_error()`.

- [ ] **Step 1: Write failing request tests**

```python
# tests/unit/test_openai_adapter.py
import pytest

from claude_sdk_proxy.domain import Dialect, ParameterPolicy, ProxyError
from claude_sdk_proxy.openai_adapter import parse_chat_completion_request


BASE = {
    "model": "sonnet",
    "messages": [
        {"role": "system", "content": "caller system"},
        {"role": "user", "content": "hello"},
    ],
    "max_completion_tokens": 256,
}


def test_openai_text_maps_without_rewriting() -> None:
    parsed = parse_chat_completion_request(BASE, ParameterPolicy.MESSAGES_COMPAT)
    assert parsed.request.dialect is Dialect.OPENAI
    assert parsed.request.system == "caller system"
    assert parsed.request.messages[0][1][0].text == "hello"
    assert parsed.ignored_parameters == ("max_completion_tokens",)


@pytest.mark.parametrize("role", ["developer", "tool", "function"])
def test_unsupported_roles_are_rejected(role: str) -> None:
    body = {"model": "sonnet", "messages": [{"role": role, "content": "x"}]}
    with pytest.raises(ProxyError, match="role"):
        parse_chat_completion_request(body, ParameterPolicy.MESSAGES_COMPAT)


def test_two_choices_are_rejected() -> None:
    with pytest.raises(ProxyError, match="n"):
        parse_chat_completion_request({**BASE, "n": 2}, ParameterPolicy.MESSAGES_COMPAT)


def test_reasoning_effort_is_rejected_in_minimal_openai_subset() -> None:
    with pytest.raises(ProxyError, match="reasoning_effort"):
        parse_chat_completion_request(
            {**BASE, "reasoning_effort": "high"}, ParameterPolicy.MESSAGES_COMPAT
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_openai_adapter.py -v`

Expected: FAIL because `openai_adapter.py` does not exist.

- [ ] **Step 3: Implement extra-forbid OpenAI wire models**

```python
# Core result type in src/claude_sdk_proxy/openai_adapter.py
from dataclasses import dataclass
from typing import Any

from .domain import CanonicalRequest


@dataclass(frozen=True, slots=True)
class ParsedOpenAIRequest:
    request: CanonicalRequest
    ignored_parameters: tuple[str, ...]


def openai_finish_reason(stop_reason: str) -> str:
    if stop_reason == "end_turn":
        return "stop"
    raise ValueError(f"unsupported stop reason: {stop_reason}")
```

Define frozen Pydantic models with `extra="forbid"` for `system`, `user`, and `assistant` text messages. Set `CanonicalRequest.dialect=Dialect.OPENAI`. Accept exactly one leading system message. Preserve adjacent same-role messages as distinct canonical turns; do not concatenate them. Parse `max_tokens` and `max_completion_tokens`, reject both appearing together, and allow one as ignored only under `MESSAGES_COMPAT`. Reject `reasoning_effort` explicitly rather than pretending it is equivalent to Anthropic thinking/effort. Also reject temperature, top_p, stop, tools, tool_choice, response_format, seed, logprobs, penalties, and `n != 1` with exact field names.

- [ ] **Step 4: Add exact response tests**

Given canonical text `hello`, SDK usage 3/4, and stop `end_turn`, assert a generated `chatcmpl_lp_*` ID, integer `created`, configured model, one choice at index 0, assistant text, `finish_reason="stop"`, and usage `prompt_tokens=3`, `completion_tokens=4`, `total_tokens=7`.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/unit/test_openai_adapter.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/openai_adapter.py tests/unit/test_openai_adapter.py
git commit -m "feat: add text-only OpenAI translation"
```

---

### Task 2: Add Chat Completions HTTP and SSE Routes

**Files:**
- Modify: `src/claude_sdk_proxy/app.py`
- Modify: `src/claude_sdk_proxy/openai_adapter.py`
- Create: `tests/integration/test_openai.py`

**Interfaces:**
- Consumes: Phase 1 session selection/actor and OpenAI adapter.
- Produces: non-streaming and streaming `POST /v1/chat/completions`.

- [ ] **Step 1: Write a non-streaming integration test**

```python
# tests/integration/test_openai.py
import httpx
import pytest

from claude_sdk_proxy.app import create_app
from claude_sdk_proxy.config import ProxyConfig
from tests.fakes import FakeBackendFactory


@pytest.mark.anyio
async def test_chat_completion_uses_canonical_backend() -> None:
    app = create_app(ProxyConfig.for_tests(), FakeBackendFactory.text("hello"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"host": "testserver"},
            json={"model": "sonnet", "max_tokens": 10, "messages": [
                {"role": "user", "content": "hi"}
            ]},
        )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"] == {"role": "assistant", "content": "hello"}
    assert response.headers["x-claude-proxy-ignored-parameters"] == "max_tokens"
```

- [ ] **Step 2: Write an SSE golden test**

Assert the first chunk establishes `role="assistant"`, text deltas preserve order, the final semantic chunk contains `finish_reason="stop"`, usage appears only if requested by supported `stream_options`, and the stream ends with `data: [DONE]`. `[DONE]` must remain buffered until successful SDK result and iterator completion.

- [ ] **Step 3: Implement the route through existing selection logic**

Add only dialect selection and translation in `app.py`; reuse authentication, run-token/session precedence, actor execution, provisional heads, limits, disconnect handling, and exception mapping. Bind a run token to `Dialect.OPENAI` on first use and reject cross-dialect reuse.

- [ ] **Step 4: Run tests and commit**

Run: `uv run pytest tests/integration/test_openai.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/app.py src/claude_sdk_proxy/openai_adapter.py tests/integration/test_openai.py
git commit -m "feat: expose OpenAI Chat Completions subset"
```

---

### Task 3: Build the Harness-Neutral Run Launcher

**Files:**
- Create: `src/claude_sdk_proxy/launcher.py`
- Modify: `src/claude_sdk_proxy/server_cli.py`
- Create: `tests/unit/test_launcher.py`
- Create: `tests/integration/test_launcher.py`

**Interfaces:**
- Consumes: `POST /_proxy/runs`, `DELETE /_proxy/runs/{run_id}`.
- Produces: `LauncherConfig`, `build_child_environment()`, `run_harness()`, and `claude-proxy run`.

- [ ] **Step 1: Write environment-construction tests**

```python
# tests/unit/test_launcher.py
from claude_sdk_proxy.domain import Dialect
from claude_sdk_proxy.launcher import build_child_environment


def test_anthropic_environment_contains_only_derived_key() -> None:
    env = build_child_environment(
        parent={"PATH": "/bin", "LOCAL_PROXY_API_KEY": "master-secret"},
        dialect=Dialect.ANTHROPIC,
        base_url="http://127.0.0.1:8317",
        derived_key="lp-derived",
    )
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8317"
    assert env["ANTHROPIC_API_KEY"] == "lp-derived"
    assert "LOCAL_PROXY_API_KEY" not in env


def test_openai_environment_uses_openai_names() -> None:
    env = build_child_environment(
        parent={"PATH": "/bin"},
        dialect=Dialect.OPENAI,
        base_url="http://127.0.0.1:8317/v1",
        derived_key="lp-derived",
    )
    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:8317/v1"
    assert env["OPENAI_API_KEY"] == "lp-derived"
```

- [ ] **Step 2: Implement environment construction**

Copy the parent environment except proxy master-key variables and preexisting provider keys/base URLs for the selected dialect, then set only the dialect's base URL and derived token. Never print the child environment. Preserve PATH, terminal, locale, and harness-specific variables.

- [ ] **Step 3: Write the process-lifecycle integration test**

Run a temporary Python child that prints whether its expected provider variables exist, waits for SIGTERM, and exits. Use an ASGI control-plane fake that records mint/revoke. Assert one token minted before spawn, master key absent in child, SIGTERM forwarded, child exit code returned, and token revoked in `finally` even when spawn or child fails.

- [ ] **Step 4: Implement `claude-proxy run`**

CLI syntax:

```text
claude-proxy run --dialect anthropic --policy messages_compat --ttl 1800 -- command arg1
claude-proxy run --dialect openai --policy messages_compat --ttl 1800 -- command arg1
```

Use HTTPX to mint the run token from the already-running local proxy. Start the child with `subprocess.Popen(..., start_new_session=True)`. Forward SIGINT/SIGTERM to the child process group, wait for exit, revoke the token in `finally`, and return the child code. Reject an empty command, non-loopback proxy URL, or TTL outside configured bounds before minting.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/unit/test_launcher.py tests/integration/test_launcher.py -v`

Expected: PASS.

```bash
git add src/claude_sdk_proxy/launcher.py src/claude_sdk_proxy/server_cli.py tests/unit/test_launcher.py tests/integration/test_launcher.py
git commit -m "feat: add generic session-aware harness launcher"
```

---

### Task 4: Prove OpenAI Client and Cross-Dialect Conformance

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/integration/test_official_openai_client.py`
- Create: `tests/integration/test_cross_dialect.py`
- Create: `docs/harnesses.md`

**Interfaces:**
- Consumes: complete Phase 2 API and launcher.
- Produces: black-box compatibility evidence and harness-neutral setup documentation.

- [ ] **Step 1: Add the OpenAI client test dependency**

Add `"openai>=1.100,<3"` to the dev extra, then run `uv lock`.

Expected: lock succeeds without changing the pinned Agent SDK.

- [ ] **Step 2: Test through the official OpenAI client**

Use the client's custom `base_url` and local API key against the ASGI app. Cover non-streaming, streaming iteration, parsed error envelopes, ignored-parameter raw headers, and automatic token continuity. Assert an OpenAI token cannot call `/v1/messages` and an Anthropic token cannot call `/v1/chat/completions`.

- [ ] **Step 3: Test canonical equivalence**

Submit equivalent system/user text once through each dialect using separate fake sessions. Assert the backend receives byte-identical canonical system and user text and the same model, while public IDs/envelopes differ by dialect.

- [ ] **Step 4: Document generic harness use**

Document direct one-shot configuration, `claude-proxy run` for one conversation per process, explicit headers for multiplexing clients, exact injected environment variables, retry aliasing, new-token requirement for a new conversation, and examples for arbitrary Anthropic- and OpenAI-configurable harnesses. Mention Pi only as one non-normative example.

- [ ] **Step 5: Run the Phase 2 gate and commit**

Run: `uv run pytest -v`

Expected: all non-live tests PASS.

Run: `uv run ruff check .`

Expected: no findings.

Run: `uv run mypy src/claude_sdk_proxy`

Expected: no findings or errors.

```bash
git add pyproject.toml uv.lock tests/integration/test_official_openai_client.py tests/integration/test_cross_dialect.py docs/harnesses.md
git commit -m "docs: verify and document harness compatibility"
```
