import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from claude_sdk_proxy.domain import (
    BackendEvent,
    BackendFailure,
    CanonicalRequest,
    CapabilityReport,
    Completed,
    TextDelta,
    UnsupportedFeature,
)

_MAX_STDERR_BYTES = 64 * 1024


def event_from_line(line: bytes) -> BackendEvent | None:
    try:
        payload: Any = json.loads(line)
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
        if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
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


async def _drain_stderr(stderr: asyncio.StreamReader) -> bytes:
    captured = bytearray()
    while chunk := await stderr.read(8 * 1024):
        remaining = _MAX_STDERR_BYTES - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
    return bytes(captured)


async def _terminate_and_wait(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=1.0)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


async def _cleanup_process(
    process: asyncio.subprocess.Process, stderr_task: asyncio.Task[bytes]
) -> None:
    await _terminate_and_wait(process)
    await stderr_task


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
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--input-format",
            "stream-json",
            "--no-session-persistence",
            "--safe-mode",
            "--restricted",
            "--disable-slash-commands",
            "--strict-mcp-config",
            "--tools",
            "",
            "--setting-sources",
            "",
            "--permission-mode",
            "dontAsk",
            "--system-prompt",
            request.system,
            "--system-prompt-snapshot",
            "off",
            "--model",
            request.model,
        )

    def encode_input(self, request: CanonicalRequest) -> bytes:
        content = self.prompt_for(request)
        payload = {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
        }
        return (json.dumps(payload, separators=(",", ":")) + "\n").encode()

    def structural_report(self) -> CapabilityReport:
        return CapabilityReport(
            backend="claude-p",
            authentication="untested",
            streaming="untested",
            prompt_construction="pass",
            multi_turn="fail",
            structured_tools="fail",
            evidence=(
                "documented safe/restricted/settings/MCP/tool/session flags are "
                "explicit",
                "--bare is excluded because it disables subscription authentication",
                "documented stream-json input does not claim assistant-history replay",
                "documented --tools accepts Claude built-ins, not caller schemas",
            ),
        )

    async def stream(self, request: CanonicalRequest) -> AsyncIterator[BackendEvent]:
        encoded_input = self.encode_input(request)
        completed: Completed | None = None
        with TemporaryDirectory(prefix="claude-proxy-") as directory:
            process = await asyncio.create_subprocess_exec(
                *self.build_argv(request),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=directory,
                env=None,
            )
            if (
                process.stdin is None
                or process.stdout is None
                or process.stderr is None
            ):
                await _terminate_and_wait(process)
                raise BackendFailure("unable to create claude -p pipes")
            stderr_task = asyncio.create_task(_drain_stderr(process.stderr))
            try:
                process.stdin.write(encoded_input)
                await process.stdin.drain()
                process.stdin.close()
                await process.stdin.wait_closed()
                while line := await process.stdout.readline():
                    if completed is not None:
                        raise BackendFailure("claude -p event after result")
                    event = event_from_line(line)
                    if isinstance(event, TextDelta):
                        yield event
                    elif isinstance(event, Completed):
                        completed = event
                return_code = await process.wait()
                if return_code != 0:
                    raise BackendFailure("claude -p query failed")
                if completed is None:
                    raise BackendFailure("claude -p stream ended without result")
            finally:
                await _cleanup_process(process, stderr_task)
        yield completed
