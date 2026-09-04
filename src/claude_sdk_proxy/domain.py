from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

type CapabilityStatus = Literal["pass", "fail", "untested"]
type Role = Literal["user", "assistant"]


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


type BackendEvent = TextDelta | Completed


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


class UnsupportedFeature(ValueError):
    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"unsupported field {field}: {reason}")
        self.field = field
        self.reason = reason


class RequestValidationError(ValueError):
    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"invalid field {field}: {reason}")
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
        return self.compatibility_proxy_viable and self.structured_tools == "pass"
