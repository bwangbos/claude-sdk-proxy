import asyncio
import json
import os
import signal
from pathlib import Path

import pytest

from claude_sdk_proxy.claude_p_backend import ClaudePBackend, _drain_stderr
from claude_sdk_proxy.domain import (
    BackendEvent,
    BackendFailure,
    CanonicalMessage,
    CanonicalRequest,
    CapabilityReport,
    Completed,
    TextDelta,
    ToolDefinition,
    UnsupportedFeature,
)

FAKE_CLAUDE = Path(__file__).parents[1] / "fixtures" / "fake_claude.py"


def request() -> CanonicalRequest:
    return CanonicalRequest(
        model="claude-test",
        system="caller-system",
        messages=(CanonicalMessage("user", "caller-message"),),
    )


def test_cli_argv_uses_documented_subscription_compatible_isolation() -> None:
    argv = ClaudePBackend(executable=Path("/usr/bin/claude")).build_argv(request())

    assert argv == (
        "/usr/bin/claude",
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
        "caller-system",
        "--system-prompt-snapshot",
        "off",
        "--model",
        "claude-test",
    )
    assert "--bare" not in argv


def test_encode_input_is_a_compact_single_user_json_line() -> None:
    encoded = ClaudePBackend(Path("/usr/bin/claude")).encode_input(request())

    assert encoded == (
        b'{"type":"user","message":{"role":"user","content":"caller-message"},'
        b'"parent_tool_use_id":null}\n'
    )


def test_encode_input_rejects_caller_tools() -> None:
    unsupported = CanonicalRequest(
        model="claude-test",
        system="caller-system",
        messages=(CanonicalMessage("user", "caller-message"),),
        tools=(ToolDefinition("weather", "Get weather", {"type": "object"}),),
    )

    with pytest.raises(UnsupportedFeature) as error:
        ClaudePBackend(Path("/usr/bin/claude")).encode_input(unsupported)

    assert error.value.field == "tools"
    assert error.value.reason == "claude --tools selects built-ins, not caller schemas"


def test_encode_input_rejects_assistant_history() -> None:
    unsupported = CanonicalRequest(
        model="claude-test",
        system="caller-system",
        messages=(
            CanonicalMessage("user", "first"),
            CanonicalMessage("assistant", "prior answer"),
        ),
    )

    with pytest.raises(UnsupportedFeature) as error:
        ClaudePBackend(Path("/usr/bin/claude")).encode_input(unsupported)

    assert error.value.field == "messages"
    assert (
        error.value.reason
        == "documented stream-json cannot replay assistant history"
    )


def test_stream_executes_isolated_cli_with_exact_input_and_normalizes_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = tmp_path / "capture.json"
    monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(capture))

    async def collect() -> list[BackendEvent]:
        return [event async for event in ClaudePBackend(FAKE_CLAUDE).stream(request())]

    assert asyncio.run(collect()) == [
        TextDelta("hel"),
        TextDelta("lo"),
        Completed("end_turn", {"output_tokens": 1}),
    ]
    captured = json.loads(capture.read_text(encoding="utf-8"))
    assert captured["argv"] == [
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
        "caller-system",
        "--system-prompt-snapshot",
        "off",
        "--model",
        "claude-test",
    ]
    assert captured["stdin"] == (
        '{"type":"user","message":{"role":"user","content":"caller-message"},'
        '"parent_tool_use_id":null}\n'
    )
    assert Path(captured["cwd"]).name.startswith("claude-proxy-")
    assert captured["cwd_entries"] == []
    assert not Path(captured["cwd"]).exists()
    assert "FAKE_CLAUDE_CAPTURE" in captured["environment_names"]


@pytest.mark.parametrize("mode", ["late", "duplicate"])
def test_stream_rejects_any_event_after_result_without_exposing_completed(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(tmp_path / "capture.json"))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    received: list[BackendEvent] = []

    async def consume() -> None:
        async for event in ClaudePBackend(FAKE_CLAUDE).stream(request()):
            received.append(event)

    with pytest.raises(BackendFailure, match="after result"):
        asyncio.run(consume())

    assert received == [TextDelta("hel"), TextDelta("lo")]


def test_stream_fails_for_malformed_output_without_public_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(tmp_path / "capture.json"))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "malformed")

    async def consume() -> None:
        async for _ in ClaudePBackend(FAKE_CLAUDE).stream(request()):
            pass

    with pytest.raises(BackendFailure, match="invalid claude -p JSON") as error:
        asyncio.run(consume())

    assert "stderr" not in str(error.value)


def test_stream_bounds_stderr_and_does_not_expose_it_on_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(tmp_path / "capture.json"))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "stderr")

    async def consume() -> None:
        async for _ in ClaudePBackend(FAKE_CLAUDE).stream(request()):
            pass

    with pytest.raises(BackendFailure, match="claude -p query failed") as error:
        asyncio.run(consume())

    assert "x" * 100 not in str(error.value)


def test_stderr_drain_keeps_only_the_first_64_kib() -> None:
    async def drain() -> bytes:
        reader = asyncio.StreamReader()
        reader.feed_data(b"x" * (64 * 1024 + 1))
        reader.feed_eof()
        return await _drain_stderr(reader)

    assert asyncio.run(drain()) == b"x" * (64 * 1024)


def test_stream_fails_when_a_zero_exit_omits_the_terminal_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(tmp_path / "capture.json"))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "noresult")

    async def consume() -> None:
        async for _ in ClaudePBackend(FAKE_CLAUDE).stream(request()):
            pass

    with pytest.raises(BackendFailure, match="stream ended without result"):
        asyncio.run(consume())


def test_stream_rejects_claude_builtin_tool_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(tmp_path / "capture.json"))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "tool")

    async def consume() -> None:
        async for _ in ClaudePBackend(FAKE_CLAUDE).stream(request()):
            pass

    with pytest.raises(UnsupportedFeature) as error:
        asyncio.run(consume())

    assert error.value.field == "tools"
    assert error.value.reason == "Claude built-in tool event"


def test_stream_cancellation_terminates_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "terminated"
    monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(tmp_path / "capture.json"))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "hang")
    monkeypatch.setenv("FAKE_CLAUDE_TERM_MARKER", str(marker))

    async def cancel_stream() -> None:
        events = ClaudePBackend(FAKE_CLAUDE).stream(request())
        assert await anext(events) == TextDelta("hel")
        pending = asyncio.create_task(anext(events))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, timeout=2.0)

    asyncio.run(cancel_stream())

    assert marker.exists()


def test_stream_aclose_terminates_and_reaps_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = tmp_path / "capture.json"
    marker = tmp_path / "terminated"
    monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(capture))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "hang")
    monkeypatch.setenv("FAKE_CLAUDE_TERM_MARKER", str(marker))

    async def close_stream() -> None:
        events = ClaudePBackend(FAKE_CLAUDE).stream(request())
        assert await anext(events) == TextDelta("hel")
        await asyncio.wait_for(events.aclose(), timeout=2.0)

    try:
        asyncio.run(close_stream())
        assert marker.exists()
    finally:
        if capture.exists():
            pid = json.loads(capture.read_text(encoding="utf-8"))["pid"]
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass


def test_structural_report_describes_only_documented_capabilities() -> None:
    assert ClaudePBackend(FAKE_CLAUDE).structural_report() == CapabilityReport(
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
