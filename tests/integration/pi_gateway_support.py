from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import urllib.request
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from claude_sdk_proxy.domain import Completed, ConversationEvent, TextDelta
from tests.gateway.fakes import FakeConversationSession

FIXTURE = Path(__file__).parents[1] / "fixtures" / "pi_text_client.mjs"
INSTALL_PI = "install pi with: npm install -g @earendil-works/pi-coding-agent"


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


class SequenceSessionFactory:
    def __init__(
        self, specifications: tuple[tuple[tuple[str, ...], bool], ...]
    ) -> None:
        self._specifications = iter(specifications)
        self.sessions: list[IntegrationSession] = []

    def __call__(self, model: str, system: str) -> IntegrationSession:
        del model, system
        outputs, stall = next(self._specifications)
        session = IntegrationSession(outputs, stall=stall)
        self.sessions.append(session)
        return session

    @property
    def created(self) -> int:
        return len(self.sessions)


def pi_ai_module() -> Path:
    executable = shutil.which("pi")
    if executable is None:
        pytest.fail(INSTALL_PI)
    resolved = Path(executable).resolve()
    candidates: Iterator[Path] = iter((resolved.parent, *resolved.parents))
    for parent in candidates:
        package = parent / "node_modules" / "@earendil-works" / "pi-ai"
        module = package / "dist" / "api" / "openai-completions.js"
        if package.joinpath("package.json").is_file() and module.is_file():
            return module
    pytest.fail(f"installed pi is missing @earendil-works/pi-ai; {INSTALL_PI}")


@asynccontextmanager
async def serve(app: Any) -> AsyncIterator[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        host, port = sock.getsockname()
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="error", lifespan="on")
        )
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
            await asyncio.wait_for(task, timeout=2)
    finally:
        sock.close()


async def run_pi(base_url: str, scenario: str) -> dict[str, Any]:
    env = {
        **os.environ,
        "PI_AI_MODULE": str(pi_ai_module()),
        "PROXY_BASE_URL": base_url,
    }
    process = await asyncio.create_subprocess_exec(
        "node",
        str(FIXTURE),
        scenario,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await communicate_or_reap(process, timeout_seconds=5)
    if process.returncode != 0:
        pytest.fail(
            f"Pi fixture failed ({process.returncode}): "
            f"{stderr.decode(errors='replace')}"
        )
    return json.loads(stdout)


async def communicate_or_reap(
    process: asyncio.subprocess.Process, timeout_seconds: float
) -> tuple[bytes, bytes]:
    communication = asyncio.create_task(process.communicate())
    try:
        async with asyncio.timeout(timeout_seconds):
            return await asyncio.shield(communication)
    finally:
        if not communication.done():
            await terminate_process(process)
            await communication


async def terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        async with asyncio.timeout(1):
            await process.wait()
    except TimeoutError:
        process.kill()
        await process.wait()


async def eventually_closed(session: IntegrationSession) -> None:
    async with asyncio.timeout(1):
        while session.close_count == 0:
            await asyncio.sleep(0.01)


async def recover_failed_session(base_url: str, scenario: str) -> dict[str, Any]:
    def send() -> dict[str, Any]:
        body = json.dumps(
            {
                "model": "sonnet",
                "messages": [{"role": "user", "content": scenario}],
                "stream": False,
            }
        ).encode()
        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            body,
            {
                "Content-Type": "application/json",
                "X-Claude-Proxy-Session": f"pi-{scenario}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return {"status": response.status, "body": json.load(response)}

    return await asyncio.to_thread(send)
