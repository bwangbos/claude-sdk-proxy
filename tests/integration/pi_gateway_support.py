from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import urllib.request
from collections.abc import AsyncIterator, Iterable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from claude_sdk_proxy.domain import (
    CanonicalMessage,
    Completed,
    ConversationEvent,
    Dialect,
    InputUsage,
    TextDelta,
    ToolCall,
    ToolDefinition,
    ToolResultBlock,
)
from tests.gateway.fakes import FakeConversationSession

FIXTURE = Path(__file__).parents[1] / "fixtures" / "pi_text_client.mjs"
TOOL_FIXTURE = Path(__file__).parents[1] / "fixtures" / "pi_tool_client.mjs"
INSTALL_PI = (
    "install pi with: npm install -g @earendil-works/pi-coding-agent@0.84.4"
)


class IntegrationSession(FakeConversationSession):
    def __init__(self, outputs: tuple[str, ...], *, stall: bool = False) -> None:
        super().__init__("unused")
        self._outputs = iter(outputs)
        self._stall = stall

    async def stream_generation(
        self, prompt: str
    ) -> AsyncIterator[ConversationEvent]:
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
        self.histories: list[tuple[CanonicalMessage, ...]] = []

    def __call__(
        self,
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
        history: tuple[CanonicalMessage, ...] = (),
    ) -> IntegrationSession:
        del model, system, tools, dialect
        self.histories.append(history)
        outputs, stall = next(self._specifications)
        session = IntegrationSession(outputs, stall=stall)
        self.sessions.append(session)
        return session

    @property
    def created(self) -> int:
        return len(self.sessions)


class PiToolSession(FakeConversationSession):
    def __init__(self) -> None:
        super().__init__("unused")
        self.handler_count = 0
        self.results: list[tuple[tuple[str, str], ...]] = []
        self._resume = asyncio.Event()
        self._failure = asyncio.Event()

    async def stream_generation(
        self, prompt: str
    ) -> AsyncIterator[ConversationEvent]:
        self.prompts.append(prompt)
        boundaries: tuple[tuple[ConversationEvent, ...], ...] = (
            (
                InputUsage(3),
                ToolCall("call_first", "echo", {"value": "first"}),
                Completed("tool_use", {"input_tokens": 3, "output_tokens": 2}),
            ),
            (
                InputUsage(5),
                ToolCall("call_second", "echo", {"value": "second"}),
                Completed("tool_use", {"input_tokens": 5, "output_tokens": 2}),
            ),
            (
                InputUsage(8),
                TextDelta("pi tool loop complete"),
                Completed("end_turn", {"input_tokens": 8, "output_tokens": 4}),
            ),
        )
        for index, boundary in enumerate(boundaries):
            if index:
                await self._resume.wait()
                self._resume.clear()
            for event in boundary:
                yield event

    async def submit_tool_results(
        self, results: Iterable[ToolResultBlock]
    ) -> None:
        normalized = tuple(results)
        self.handler_count += 1
        self.results.append(
            tuple(
                (result.tool_call_id, "".join(result.content))
                for result in normalized
            )
        )
        self._resume.set()

    async def wait_failure(self) -> None:
        await self._failure.wait()


def pi_ai_module() -> Path:
    return _pi_module("pi-ai", Path("dist/api/openai-completions.js"))


def pi_agent_module() -> Path:
    return _pi_module("pi-agent-core", Path("dist/index.js"))


def pi_coding_agent_module() -> Path:
    return _pi_module("pi-coding-agent", Path("dist/bundle/cli.js"))


def _pi_module(package_name: str, relative_module: Path) -> Path:
    executable = shutil.which("pi")
    if executable is None:
        pytest.fail(INSTALL_PI)
    resolved = Path(executable).resolve()
    candidates: Iterator[Path] = iter((resolved.parent, *resolved.parents))
    for parent in candidates:
        package = parent / "node_modules" / "@earendil-works" / package_name
        module = package / relative_module
        if package.joinpath("package.json").is_file() and module.is_file():
            return module
    pytest.fail(
        f"installed pi is missing @earendil-works/{package_name}; {INSTALL_PI}"
    )


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
            yield f"http://{host}:{port}"
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=2)
    finally:
        sock.close()


async def run_pi(base_url: str, scenario: str) -> dict[str, Any]:
    env = {
        **os.environ,
        "PI_AI_MODULE": str(pi_ai_module()),
        "PROXY_BASE_URL": f"{base_url}/v1",
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


async def run_pi_tool(base_url: str) -> dict[str, Any]:
    env = {
        **os.environ,
        "PI_AI_MODULE": str(pi_ai_module()),
        "PI_AGENT_MODULE": str(pi_agent_module()),
        "PI_CODING_AGENT_MODULE": str(pi_coding_agent_module()),
        "PROXY_BASE_URL": f"{base_url}/v1",
    }
    process = await asyncio.create_subprocess_exec(
        "node",
        str(TOOL_FIXTURE),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await communicate_or_reap(process, timeout_seconds=5)
    if process.returncode != 0:
        pytest.fail(
            f"Pi tool fixture failed ({process.returncode}): "
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
            f"{base_url}/v1/chat/completions",
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
