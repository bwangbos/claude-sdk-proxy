from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from claude_agent_sdk import (
    InMemorySessionStore,
    SessionStoreEntry,
    project_key_for_directory,
)
from claude_agent_sdk._cli_version import __cli_version__

from claude_sdk_proxy.domain import (
    CanonicalMessage,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from claude_sdk_proxy.tool_contract import JsonValue, plain_json


@dataclass(frozen=True, slots=True)
class SeededHistory:
    store: InMemorySessionStore
    project_key: str
    session_id: str


async def seed_history(
    history: tuple[CanonicalMessage, ...],
    *,
    cwd: Path,
    model: str,
    sdk_tool_names: dict[str, str],
) -> SeededHistory:
    if not history:
        raise ValueError("seed history must not be empty")
    session_id = str(uuid.uuid4())
    project_key = project_key_for_directory(cwd)
    entries = _entries(
        history,
        cwd=cwd,
        model=model,
        session_id=session_id,
        sdk_tool_names=sdk_tool_names,
    )
    store = InMemorySessionStore()
    await store.append({"project_key": project_key, "session_id": session_id}, entries)
    return SeededHistory(store, project_key, session_id)


def _entries(
    history: tuple[CanonicalMessage, ...],
    *,
    cwd: Path,
    model: str,
    session_id: str,
    sdk_tool_names: dict[str, str],
) -> list[SessionStoreEntry]:
    timestamp = (
        datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )
    parent_uuid: str | None = None
    prompt_id: str | None = None
    internal_tools: dict[str, tuple[str, str]] = {}
    entries: list[SessionStoreEntry] = []
    for message in history:
        if message.role == "user":
            if all(isinstance(block, TextBlock) for block in message.blocks):
                prompt_id = str(uuid.uuid4())
            elif prompt_id is None:
                raise ValueError("seed tool result has no prior prompt")
            user_entries = _user_entries(
                message,
                internal_tools,
                cwd=cwd,
                session_id=session_id,
                timestamp=timestamp,
                parent_uuid=parent_uuid,
                prompt_id=prompt_id,
            )
            entries.extend(user_entries)
            parent_uuid = user_entries[-1]["uuid"]
            continue
        assistant_entries = _assistant_entries(
            message,
            internal_tools,
            sdk_tool_names,
            cwd=cwd,
            session_id=session_id,
            timestamp=timestamp,
            parent_uuid=parent_uuid,
            model=model,
        )
        entries.extend(assistant_entries)
        parent_uuid = assistant_entries[-1]["uuid"]
    return entries


def _common_entry(
    *,
    cwd: Path,
    session_id: str,
    timestamp: str,
    entry_uuid: str,
    parent_uuid: str | None,
) -> dict[str, Any]:
    return {
        "cwd": str(cwd),
        "sessionId": session_id,
        "timestamp": timestamp,
        "version": __cli_version__,
        "gitBranch": "HEAD",
        "isSidechain": False,
        "userType": "external",
        "entrypoint": "sdk-py",
        "uuid": entry_uuid,
        "parentUuid": parent_uuid,
    }


def _user_entries(
    message: CanonicalMessage,
    internal_tools: dict[str, tuple[str, str]],
    *,
    cwd: Path,
    session_id: str,
    timestamp: str,
    parent_uuid: str | None,
    prompt_id: str,
) -> list[SessionStoreEntry]:
    if all(isinstance(block, TextBlock) for block in message.blocks):
        entry_uuid = str(uuid.uuid4())
        return [
            cast(
                SessionStoreEntry,
                {
                    **_common_entry(
                        cwd=cwd,
                        session_id=session_id,
                        timestamp=timestamp,
                        entry_uuid=entry_uuid,
                        parent_uuid=parent_uuid,
                    ),
                    "type": "user",
                    "message": {"role": "user", "content": message.require_text()},
                    "permissionMode": "dontAsk",
                    "promptId": prompt_id,
                    "promptSource": "sdk",
                },
            )
        ]
    entries: list[SessionStoreEntry] = []
    for block in message.blocks:
        if not isinstance(block, ToolResultBlock):
            raise ValueError("seed user history is invalid")
        try:
            internal_id, assistant_uuid = internal_tools[block.tool_call_id]
        except KeyError:
            raise ValueError("seed tool result has no prior call") from None
        entry_uuid = str(uuid.uuid4())
        rendered = [{"type": "text", "text": text} for text in block.content]
        result: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": internal_id,
            "content": rendered,
        }
        if block.is_error:
            result["is_error"] = True
        entries.append(
            cast(
                SessionStoreEntry,
                {
                    **_common_entry(
                        cwd=cwd,
                        session_id=session_id,
                        timestamp=timestamp,
                        entry_uuid=entry_uuid,
                        parent_uuid=parent_uuid,
                    ),
                    "type": "user",
                    "message": {"role": "user", "content": [result]},
                    "promptId": prompt_id,
                    "toolUseResult": (
                        "Error: " + "\n".join(block.content)
                        if block.is_error
                        else rendered
                    ),
                    "sourceToolAssistantUUID": assistant_uuid,
                },
            )
        )
        parent_uuid = entry_uuid
    return entries


def _assistant_entries(
    message: CanonicalMessage,
    internal_tools: dict[str, tuple[str, str]],
    sdk_tool_names: dict[str, str],
    *,
    cwd: Path,
    session_id: str,
    timestamp: str,
    parent_uuid: str | None,
    model: str,
) -> list[SessionStoreEntry]:
    has_tools = any(isinstance(block, ToolCallBlock) for block in message.blocks)
    message_id = "msg_rebase_" + uuid.uuid4().hex
    request_id = "req_rebase_" + uuid.uuid4().hex
    entries: list[SessionStoreEntry] = []
    for index, block in enumerate(message.blocks):
        entry_uuid = str(uuid.uuid4())
        content: dict[str, Any]
        if isinstance(block, TextBlock):
            content = {"type": "text", "text": block.text}
        elif isinstance(block, ToolCallBlock):
            internal_id = "toolu_rebase_" + uuid.uuid4().hex
            internal_tools[block.id] = (internal_id, entry_uuid)
            sdk_name = sdk_tool_names.get(
                block.name, f"mcp__caller_tools_v1__{block.name}"
            )
            content = {
                "type": "tool_use",
                "id": internal_id,
                "name": sdk_name,
                "input": plain_json(cast(JsonValue, block.arguments)),
                "caller": {"type": "direct"},
            }
        else:
            raise ValueError("seed assistant history is invalid")
        entries.append(
            cast(
                SessionStoreEntry,
                {
                    **_common_entry(
                        cwd=cwd,
                        session_id=session_id,
                        timestamp=timestamp,
                        entry_uuid=entry_uuid,
                        parent_uuid=parent_uuid,
                    ),
                    "type": "assistant",
                    "apiBlockIndex": index,
                    "requestId": request_id,
                    "message": {
                        "id": message_id,
                        "type": "message",
                        "role": "assistant",
                        "model": model,
                        "content": [content],
                        "stop_reason": "tool_use" if has_tools else "end_turn",
                        "stop_sequence": None,
                        "stop_details": None,
                        "usage": _zero_usage(),
                        "diagnostics": None,
                    },
                },
            )
        )
        parent_uuid = entry_uuid
    return entries


def _zero_usage() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens_details": {"thinking_tokens": 0},
        "server_tool_use": {
            "web_search_requests": 0,
            "web_fetch_requests": 0,
        },
        "service_tier": "standard",
        "cache_creation": {
            "ephemeral_1h_input_tokens": 0,
            "ephemeral_5m_input_tokens": 0,
        },
    }


__all__ = ["SeededHistory", "seed_history"]
