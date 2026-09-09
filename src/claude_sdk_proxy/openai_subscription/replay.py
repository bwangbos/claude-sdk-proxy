"""Account-scoped opaque reasoning and exact-transcript, bounded replay.

Signatures are an interchange envelope, not an authenticity claim. Ciphertext
is passed through to its issuer, never decrypted or interpreted by the proxy.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from typing import Any

from claude_sdk_proxy.domain import (
    CanonicalMessage,
    RedactedThinkingBlock,
    RequestValidationError,
    TextBlock,
    TextRequest,
    ThinkingBlock,
    ToolCallBlock,
)

PREFIX = "openai-subscription:v1:"
ASSISTANT_PREFIX = "openai-subscription:assistant:v1:"
MAX_SIGNATURE_BYTES = 1024 * 1024
MAX_CACHE_BYTES = 64 * 1024 * 1024


def plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name)) for f in fields(value)}
    return value


def packed(value: Any) -> bytes:
    return json.dumps(
        plain(value),
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=True,
        allow_nan=False,
    ).encode()


def account_scope(account: str) -> str:
    return hashlib.sha256(account.encode()).hexdigest()


def valid_reasoning(item: Any) -> bool:
    return (
        isinstance(item, dict)
        and not set(item) - {"type", "id", "summary", "encrypted_content", "status"}
        and item.get("type") == "reasoning"
        and isinstance(item.get("id"), str)
        and 0 < len(item["id"]) <= 256
        and isinstance(item.get("summary"), list)
        and all(
            isinstance(s, dict)
            and set(s) == {"type", "text"}
            and s["type"] == "summary_text"
            and isinstance(s["text"], str)
            for s in item["summary"]
        )
        and (
            "encrypted_content" not in item
            or isinstance(item["encrypted_content"], str)
        )
        and (
            "status" not in item
            or item["status"] in ("in_progress", "completed", "incomplete")
        )
    )


def encode_reasoning(
    item: dict[str, Any], account: str, model: str, *, scope: str
) -> str:
    if not valid_reasoning(item):
        raise RequestValidationError("messages", "invalid OpenAI reasoning envelope")
    raw = packed(
        {
            "v": 1,
            "provider": "openai-subscription",
            "account": account_scope(account),
            "model": model,
            "scope": scope,
            "item": item,
        }
    )
    signature = PREFIX + base64.urlsafe_b64encode(raw).decode()
    if len(signature) > MAX_SIGNATURE_BYTES:
        raise RequestValidationError(
            "messages", "OpenAI reasoning envelope exceeds limit"
        )
    return signature


def decode_reasoning(
    signature: str, account: str, model: str, *, scope: str
) -> dict[str, Any] | None:
    if not signature.startswith(PREFIX):
        return None
    try:
        if len(signature) > MAX_SIGNATURE_BYTES:
            raise ValueError
        envelope = json.loads(
            base64.b64decode(signature[len(PREFIX) :], altchars=b"-_", validate=True)
        )
        required = {
            "v",
            "provider",
            "account",
            "model",
            "item",
        }
        if (
            not isinstance(envelope, dict)
            or not required <= set(envelope)
            or set(envelope) - required - {"scope"}
        ):
            raise ValueError
        if (
            type(envelope["v"]) is not int
            or envelope["v"] != 1
            or envelope["provider"] != "openai-subscription"
            or not valid_reasoning(envelope["item"])
        ):
            raise ValueError
        if not isinstance(envelope["account"], str) or not isinstance(
            envelope["model"], str
        ):
            raise ValueError
        if envelope["account"] != account_scope(account) or envelope["model"] != model:
            return None
        if envelope.get("scope") != scope:
            return None
        return dict(envelope["item"])
    except ValueError, TypeError, RecursionError:
        raise RequestValidationError(
            "messages", "invalid OpenAI reasoning envelope"
        ) from None


def _key(
    request: TextRequest,
    account: str,
    messages: tuple[CanonicalMessage, ...],
    *,
    include_summaries: bool = False,
) -> bytes:
    # Chat has no signature channel. Normalize adjacent text blocks because
    # downstream Chat aggregates them into one assistant content string.
    visible: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "user":
            visible.append({"role": "user", "blocks": message.blocks})
            continue
        blocks = [
            b
            for b in message.blocks
            if not isinstance(b, (ThinkingBlock, RedactedThinkingBlock))
        ]
        from claude_sdk_proxy.domain import TextBlock

        text = "".join(b.text for b in blocks if isinstance(b, TextBlock))
        calls = [b for b in blocks if not isinstance(b, TextBlock)]
        normalized: dict[str, Any] = {
            "role": message.role,
            "text": text,
            "blocks": calls,
        }
        if include_summaries:
            normalized["summaries"] = [
                b.thinking for b in message.blocks if isinstance(b, ThinkingBlock)
            ]
        visible.append(normalized)
    return hashlib.sha256(
        packed(
            [
                account_scope(account),
                request.model,
                request.system,
                request.tools,
                visible,
            ]
        )
    ).digest()


def envelope_scope(
    request: TextRequest, account: str, messages: tuple[CanonicalMessage, ...]
) -> str:
    """Full visible boundary, independent of terminal signature block placement.

    Preserve user block order strictly; normalize assistant text, calls and
    ordered summaries into separate channels. Never include opaque signatures.
    """
    return _key(request, account, messages, include_summaries=True).hex()


def native_assistant(
    output: list[dict[str, Any]], *, include_reasoning: bool = False
) -> CanonicalMessage:
    """Visible projection shared by the producer and portable replay validator."""
    blocks: list[TextBlock | ToolCallBlock | ThinkingBlock] = []
    refusals: list[str] = []
    for item in output:
        if item["type"] == "message":
            for part in item["content"]:
                if part["type"] == "output_text":
                    blocks.append(TextBlock(part["text"]))
                elif part["type"] == "refusal":
                    refusals.append(part["refusal"])
        elif item["type"] == "function_call":
            blocks.append(
                ToolCallBlock(
                    item["call_id"], item["name"], json.loads(item["arguments"])
                )
            )
        elif item["type"] == "reasoning" and include_reasoning:
            blocks.append(
                ThinkingBlock(
                    "".join(s["text"] for s in item["summary"]), "scope-placeholder"
                )
            )
    if refusals:
        blocks.append(TextBlock("".join(refusals)))
    return CanonicalMessage("assistant", tuple(blocks) or (TextBlock(""),))


def encode_assistant(
    output: list[dict[str, Any]], account: str, model: str, *, scope: str
) -> str | None:
    """One bounded portable carrier, not a copy in every thinking signature."""
    raw = packed(
        {
            "v": 1,
            "provider": "openai-subscription",
            "account": account_scope(account),
            "model": model,
            "scope": scope,
            "output": output,
        }
    )
    if len(raw) * 4 // 3 + len(ASSISTANT_PREFIX) + 4 > MAX_SIGNATURE_BYTES:
        return None
    return ASSISTANT_PREFIX + base64.urlsafe_b64encode(raw).decode()


def decode_assistant(
    signature: str,
    request: TextRequest,
    account: str,
    boundary: tuple[CanonicalMessage, ...],
) -> list[dict[str, Any]] | None:
    """Malformed, obsolete and incompatible carriers all fail open to visibility."""
    if (
        not signature.startswith(ASSISTANT_PREFIX)
        or len(signature) > MAX_SIGNATURE_BYTES
    ):
        return None
    try:
        value = json.loads(
            base64.b64decode(
                signature[len(ASSISTANT_PREFIX) :], altchars=b"-_", validate=True
            )
        )
        if not isinstance(value, dict) or set(value) != {
            "v",
            "provider",
            "account",
            "model",
            "scope",
            "output",
        }:
            return None
        expected = envelope_scope(request, account, boundary)
        if (
            type(value["v"]) is not int
            or value["v"] != 1
            or value["provider"] != "openai-subscription"
            or value["account"] != account_scope(account)
            or value["model"] != request.model
            or value["scope"] != expected
        ):
            return None
        output = value["output"]
        if not isinstance(output, list) or len(output) > 1024:
            return None
        for item in output:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                return None
            kind = item.get("type")
            if kind == "reasoning":
                if not valid_reasoning(item):
                    return None
            elif kind == "message":
                if (
                    item.get("role") != "assistant"
                    or item.get("phase") not in {None, "commentary", "final_answer"}
                    or not isinstance(item.get("content"), list)
                ):
                    return None
                for part in item["content"]:
                    if not isinstance(part, dict) or not (
                        (
                            part.get("type") == "output_text"
                            and isinstance(part.get("text"), str)
                        )
                        or (
                            part.get("type") == "refusal"
                            and isinstance(part.get("refusal"), str)
                        )
                    ):
                        return None
            elif kind == "function_call":
                if (
                    not isinstance(item.get("call_id"), str)
                    or not isinstance(item.get("name"), str)
                    or not isinstance(item.get("arguments"), str)
                    or not isinstance(json.loads(item["arguments"]), dict)
                ):
                    return None
            else:
                return None
        # Even a caller-edited carrier may not override visible text/calls.
        projected = boundary[:-1] + (native_assistant(output, include_reasoning=True),)
        return (
            output if envelope_scope(request, account, projected) == expected else None
        )
    except ValueError, TypeError, KeyError, RecursionError:
        return None


class ReplayCache:
    def __init__(self, *, max_entries: int = 128, max_bytes: int = MAX_CACHE_BYTES):
        if max_entries < 0 or not 0 <= max_bytes <= MAX_CACHE_BYTES:
            raise ValueError("invalid replay cache bounds")
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.bytes_used = 0
        self._entries: OrderedDict[bytes, bytes | None] = OrderedDict()

    def get(self, request: TextRequest, account: str) -> list[dict[str, Any]] | None:
        key = _key(request, account, request.messages[:-1])
        if key not in self._entries:
            return None
        self._entries.move_to_end(key)
        value = self._entries[key]
        return json.loads(value) if value is not None else None

    def put(
        self,
        request: TextRequest,
        account: str,
        messages: tuple[CanonicalMessage, ...],
        items: list[dict[str, Any]],
    ) -> None:
        key = _key(request, account, messages)
        value: bytes | None = packed(items)
        if key in self._entries:
            old = self._entries.pop(key)
            self.bytes_used -= len(key) + (len(old) if old is not None else 0)
            if old != value:
                value = None  # Ambiguous stays a miss until eviction.
        size = len(key) + (len(value) if value is not None else 0)
        if size > self.max_bytes or not self.max_entries:
            return
        self._entries[key] = value
        self.bytes_used += size
        while len(self._entries) > self.max_entries or self.bytes_used > self.max_bytes:
            k, v = self._entries.popitem(last=False)
            self.bytes_used -= len(k) + (len(v) if v is not None else 0)

    def clear(self) -> None:
        self._entries.clear()
        self.bytes_used = 0
