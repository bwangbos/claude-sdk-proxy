from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from claude_sdk_proxy.app import create_app
from claude_sdk_proxy.domain import Completed, ConversationEvent, TextDelta
from tests.gateway.fakes import FakeConversationSession

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "pi_text_client.mjs"
_INSTALL_PI = "install pi with: npm install -g @earendil-works/pi-coding-agent"


class IntegrationSession(FakeConversationSession):
    def __init__(self, outputs: tuple[str, ...], *, stall: bool = False) -> None:
        super().__init__("unused")
        self._outputs = iter(outputs)
        self._stall = stall

    async def stream_turn(self, prompt: str) -> AsyncIterator[ConversationEvent]:
        self.prompts.append(prompt)
        yield TextDelta(next(self._outputs))
        if self._stall:
            await asyncio.Event().wait()
        yield Completed("end_turn", {"input_tokens": 2, "output_tokens": 1})


class OneSessionFactory:
    def __init__(self, session: IntegrationSession) -> None:
        self.session = session
        self.created = 0

    def __call__(self, model: str, system: str) -> IntegrationSession:
        del model, system
        self.created += 1
        return self.session


def _pi_ai_module() -> Path:
    executable = shutil.which("pi")
    if executable is None:
        pytest.fail(_INSTALL_PI)
    resolved = Path(executable).resolve()
    candidates: Iterator[Path] = iter((resolved.parent, *resolved.parents))
    for parent in candidates:
        package = parent / "node_modules" / "@earendil-works" / "pi-ai"
        module = package / "dist" / "api" / "openai-completions.js"
        if (package.joinpath("package.json").is_file() and module.is_file()):
            return module
    pytest.fail(f"installed pi is missing @earendil-works/pi-ai; {_INSTALL_PI}")


@asynccontextmanager
async def _serve(app: Any) -> AsyncIterator[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    host, port = sock.getsockname()
    config = uvicorn.Config(app, log_level="error", lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(2):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        yield f"http://{host}:{port}/v1"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=2)
        finally:
            sock.close()


async def _run_pi(base_url: str, scenario: str) -> dict[str, Any]:
    env = {
        **os.environ,
        "PI_AI_MODULE": str(_pi_ai_module()),
        "PROXY_BASE_URL": base_url,
    }
    process = await asyncio.create_subprocess_exec(
        "node",
        str(_FIXTURE),
        scenario,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
    if process.returncode != 0:
        pytest.fail(
            f"Pi fixture failed ({process.returncode}): "
            f"{stderr.decode(errors='replace')}"
        )
    return json.loads(stdout)


async def _eventually_closed(session: IntegrationSession) -> None:
    async with asyncio.timeout(1):
        while session.close_count == 0:
            await asyncio.sleep(0.01)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("scenario", "outputs", "stall"),
    [
        ("linear", ("first answer", "second answer"), False),
        ("retry", ("replayed answer",), False),
        ("abort", ("partial",), True),
        ("timeout", ("partial",), True),
    ],
)
async def test_real_pi_openai_provider_against_gateway(
    scenario: str, outputs: tuple[str, ...], stall: bool
) -> None:
    session = IntegrationSession(outputs, stall=stall)
    factory = OneSessionFactory(session)
    timeout = 0.1 if scenario == "timeout" else 5.0
    app = create_app(
        models=("sonnet",),
        session_factory=factory,
        turn_timeout_seconds=timeout,
    )

    async with _serve(app) as base_url:
        result = await _run_pi(base_url, scenario)
        if scenario in {"abort", "timeout"}:
            await _eventually_closed(session)

    assert result["scenario"] == scenario
    assert result["requestsUsePiDefaults"] is True
    if scenario == "linear":
        assert result["turns"] == [
            {"text": "first answer", "finishReason": "stop", "error": None},
            {"text": "second answer", "finishReason": "stop", "error": None},
        ]
        assert session.prompts == ["first", "second"]
        assert factory.created == 1
    elif scenario == "retry":
        assert result["turns"] == [
            {"text": "replayed answer", "finishReason": "stop", "error": None},
            {"text": "replayed answer", "finishReason": "stop", "error": None},
        ]
        assert session.prompts == ["retry"]
        assert factory.created == 1
    elif scenario == "abort":
        assert result["turns"][0]["text"] == "partial"
        assert result["turns"][0]["finishReason"] == "aborted"
        assert session.close_count == 1
    else:
        assert result["turns"][0]["text"] == "partial"
        assert result["turns"][0]["finishReason"] == "error"
        assert result["turns"][0]["error"]
        assert session.close_count == 1
