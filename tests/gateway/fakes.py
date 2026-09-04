from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, StreamEvent


class FixedTemporaryDirectory:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.cleanup_count = 0

    def __enter__(self) -> str:
        return str(self.path)

    def __exit__(self, *args: object) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        self.cleanup_count += 1


class FakeSdkClient:
    def __init__(self, responses: tuple[tuple[Any, ...], ...]) -> None:
        self._responses = iter(responses)
        self._response: tuple[Any, ...] = ()
        self.connect_count = 0
        self.disconnect_count = 0
        self.prompts: list[str] = []
        self.options: ClaudeAgentOptions | None = None

    def capture_options(self, options: ClaudeAgentOptions) -> FakeSdkClient:
        self.options = options
        return self

    async def connect(self) -> None:
        self.connect_count += 1

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)
        self._response = next(self._responses)

    async def receive_response(self) -> AsyncIterator[Any]:
        for message in self._response:
            yield message

    async def disconnect(self) -> None:
        self.disconnect_count += 1


def sdk_response(text: str, session_id: str) -> tuple[StreamEvent | ResultMessage, ...]:
    return (
        StreamEvent(
            uuid="event-1",
            session_id=session_id,
            event={
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        ResultMessage(
            subtype="success",
            duration_ms=0,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id=session_id,
            stop_reason="end_turn",
            usage={"output_tokens": 1},
        ),
    )
