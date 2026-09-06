from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Protocol, cast

type CapabilityStatus = Literal["pass", "fail", "untested"]
type Role = Literal["user", "assistant"]
type Dialect = Literal["anthropic", "openai"]


class RequestValidationError(ValueError):
    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"invalid field {field}: {reason}")
        self.field = field
        self.reason = reason


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str


@dataclass(frozen=True, slots=True)
class ImageBlock:
    media_type: str
    data: str

    def __post_init__(self) -> None:
        from claude_sdk_proxy.images import validate_image

        validate_image(self)


@dataclass(frozen=True, slots=True)
class ImagePrompt:
    blocks: tuple[TextBlock | ImageBlock, ...]


type Prompt = str | ImagePrompt


@dataclass(frozen=True, slots=True)
class ToolCallBlock:
    id: str
    name: str
    arguments: Mapping[str, object]

    def __post_init__(self) -> None:
        from claude_sdk_proxy.tool_contract import freeze_json

        frozen = freeze_json(self.arguments, field="messages")
        object.__setattr__(self, "arguments", cast(Mapping[str, object], frozen))


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    tool_call_id: str
    content: tuple[str | ImageBlock, ...]
    is_error: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", tuple(self.content))


type CanonicalBlock = TextBlock | ImageBlock | ToolCallBlock | ToolResultBlock


@dataclass(frozen=True, slots=True, init=False)
class CanonicalMessage:
    role: Role
    blocks: tuple[CanonicalBlock, ...]

    def __init__(self, role: Role, content: str | tuple[CanonicalBlock, ...]) -> None:
        normalized = (
            (TextBlock(content),) if isinstance(content, str) else tuple(content)
        )
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "blocks", normalized)

    @classmethod
    def user_text(cls, text: str) -> CanonicalMessage:
        return cls("user", text)

    @classmethod
    def assistant_text(cls, text: str) -> CanonicalMessage:
        return cls("assistant", text)

    @property
    def content(self) -> str:
        return self.require_text()

    def require_text(self) -> str:
        if not self.blocks or any(
            not isinstance(item, TextBlock) for item in self.blocks
        ):
            raise ValueError("message content is not text")
        return "".join(cast(TextBlock, item).text for item in self.blocks)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: Mapping[str, object]

    def __post_init__(self) -> None:
        from claude_sdk_proxy.tool_contract import freeze_json

        frozen = freeze_json(self.input_schema)
        object.__setattr__(self, "input_schema", cast(Mapping[str, object], frozen))


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
class ToolCall:
    id: str
    name: str
    arguments: Mapping[str, object]

    def __post_init__(self) -> None:
        from claude_sdk_proxy.tool_contract import freeze_json

        frozen = freeze_json(self.arguments)
        object.__setattr__(self, "arguments", cast(Mapping[str, object], frozen))


@dataclass(frozen=True, slots=True)
class InputUsage:
    input_tokens: int


@dataclass(frozen=True, slots=True)
class Completed:
    stop_reason: str | None
    usage: Mapping[str, Any] | None

    def __post_init__(self) -> None:
        if self.usage is not None:
            object.__setattr__(self, "usage", MappingProxyType(dict(self.usage)))


type BackendEvent = TextDelta | ToolCall | Completed


@dataclass(frozen=True, slots=True)
class TextRequest:
    model: str
    system: str
    messages: tuple[CanonicalMessage, ...]
    max_tokens: int | None
    stream: bool
    include_usage: bool = False
    dialect: Dialect = "anthropic"
    tools: tuple[ToolDefinition, ...] = ()

    def __post_init__(self) -> None:
        from claude_sdk_proxy.tool_contract import (
            validate_tool_arguments,
            validate_tool_definitions,
            validate_tool_results,
        )

        if not self.model.strip():
            raise ValueError("model must not be empty")
        if self.max_tokens is not None and self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if self.dialect not in {"anthropic", "openai"}:
            raise RequestValidationError("messages", "dialect is invalid")
        if not self.messages:
            raise ValueError("messages must not be empty")

        normalized_tools = validate_tool_definitions(self.tools)
        object.__setattr__(self, "tools", normalized_tools)
        normalized_messages: list[CanonicalMessage] = []
        previous_role: Role | None = None
        previous_user_had_tool_results = False
        prior_call_ids: set[str] | None = None
        for message in self.messages:
            if previous_role is None and message.role != "user":
                raise ValueError("conversation must start with a user message")
            if message.role == "assistant" and previous_role != "user":
                raise ValueError("assistant messages must follow a user message")
            if (
                message.role == "user"
                and previous_role == "user"
                and previous_user_had_tool_results
            ):
                raise RequestValidationError(
                    "messages", "tool result turns must be followed by assistant"
                )
            blocks = message.blocks
            if not blocks:
                raise RequestValidationError("messages", "blocks must not be empty")
            if message.role == "user":
                if all(isinstance(item, (TextBlock, ImageBlock)) for item in blocks):
                    if prior_call_ids is not None:
                        raise RequestValidationError(
                            "messages", "tool results must match prior calls"
                        )
                    if (
                        not any(isinstance(item, ImageBlock) for item in blocks)
                        and not message.require_text()
                    ):
                        raise ValueError("message content must not be empty")
                    normalized_messages.append(message)
                    prior_call_ids = None
                elif all(isinstance(item, ToolResultBlock) for item in blocks):
                    results = validate_tool_results(
                        cast(tuple[ToolResultBlock, ...], blocks)
                    )
                    result_ids = {item.tool_call_id for item in results}
                    if prior_call_ids is None or result_ids != prior_call_ids:
                        raise RequestValidationError(
                            "messages", "tool results must match prior calls"
                        )
                    normalized_messages.append(CanonicalMessage("user", results))
                    prior_call_ids = None
                else:
                    raise RequestValidationError(
                        "messages", "user blocks must be all text or all tool results"
                    )
            else:
                if any(isinstance(item, ToolResultBlock) for item in blocks) or any(
                    not isinstance(item, (TextBlock, ToolCallBlock)) for item in blocks
                ):
                    raise RequestValidationError(
                        "messages", "assistant blocks are invalid"
                    )
                calls = [item for item in blocks if isinstance(item, ToolCallBlock)]
                call_ids: set[str] = set()
                for call in calls:
                    if call.id in call_ids or not _valid_public_id(call.id):
                        raise RequestValidationError(
                            "messages", "tool call ID is invalid"
                        )
                    validate_tool_arguments(call.arguments, field="messages")
                    call_ids.add(call.id)
                if (
                    all(isinstance(item, TextBlock) for item in blocks)
                    and not message.require_text()
                ):
                    raise ValueError("message content must not be empty")
                normalized_messages.append(message)
                prior_call_ids = call_ids if call_ids else None
            previous_role = message.role
            previous_user_had_tool_results = message.role == "user" and all(
                isinstance(item, ToolResultBlock) for item in message.blocks
            )
        if normalized_messages[-1].role != "user":
            raise ValueError("conversation must end with a user message")
        object.__setattr__(self, "messages", tuple(normalized_messages))
        from claude_sdk_proxy.images import validate_image_budget

        validate_image_budget(self.messages)

    @property
    def next_input(self) -> Prompt | tuple[ToolResultBlock, ...]:
        last = self.messages[-1]
        if all(isinstance(item, TextBlock) for item in last.blocks):
            return last.require_text()
        if all(isinstance(item, (TextBlock, ImageBlock)) for item in last.blocks):
            return ImagePrompt(cast(tuple[TextBlock | ImageBlock, ...], last.blocks))
        if all(isinstance(item, ToolResultBlock) for item in last.blocks):
            return cast(tuple[ToolResultBlock, ...], last.blocks)
        raise RequestValidationError("messages", "final user blocks are invalid")

    @property
    def next_prompt(self) -> Prompt:
        value = self.next_input
        if isinstance(value, tuple):
            raise ValueError("conversation must end with a text/image user message")
        return value


def _valid_public_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is not None
    )


type ConversationEvent = InputUsage | TextDelta | ToolCall | Completed


class SdkSessionProtocol(Protocol):
    async def start(self) -> None: ...
    def stream_generation(self, prompt: Prompt) -> AsyncIterator[ConversationEvent]: ...
    async def submit_tool_results(
        self, results: Iterable[ToolResultBlock]
    ) -> None: ...
    async def wait_failure(self) -> None: ...
    async def close(self) -> None: ...


class SdkSessionFactory(Protocol):
    def __call__(
        self,
        model: str,
        system: str,
        *,
        tools: tuple[ToolDefinition, ...] = (),
        dialect: Dialect = "anthropic",
        history: tuple[CanonicalMessage, ...] = (),
    ) -> SdkSessionProtocol: ...


class UnsupportedFeature(ValueError):
    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"unsupported field {field}: {reason}")
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
