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
    TextRequest,
    ThinkingBlock,
)

PREFIX = "openai-subscription:v1:"
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


def encode_reasoning(item: dict[str, Any], account: str, model: str) -> str:
    if not valid_reasoning(item):
        raise RequestValidationError("messages", "invalid OpenAI reasoning envelope")
    raw = packed(
        {
            "v": 1,
            "provider": "openai-subscription",
            "account": account_scope(account),
            "model": model,
            "item": item,
        }
    )
    signature = PREFIX + base64.urlsafe_b64encode(raw).decode()
    if len(signature) > MAX_SIGNATURE_BYTES:
        raise RequestValidationError(
            "messages", "OpenAI reasoning envelope exceeds limit"
        )
    return signature


def decode_reasoning(signature: str, account: str, model: str) -> dict[str, Any] | None:
    if not signature.startswith(PREFIX):
        return None
    try:
        if len(signature) > MAX_SIGNATURE_BYTES:
            raise ValueError
        envelope = json.loads(
            base64.b64decode(signature[len(PREFIX) :], altchars=b"-_", validate=True)
        )
        if not isinstance(envelope, dict) or set(envelope) != {
            "v",
            "provider",
            "account",
            "model",
            "item",
        }:
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
        return dict(envelope["item"])
    except ValueError, TypeError, RecursionError:
        raise RequestValidationError(
            "messages", "invalid OpenAI reasoning envelope"
        ) from None


def _key(
    request: TextRequest, account: str, messages: tuple[CanonicalMessage, ...]
) -> bytes:
    # Chat has no signature channel. Normalize adjacent text blocks because
    # downstream Chat aggregates them into one assistant content string.
    visible = []
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
        visible.append({"role": message.role, "text": text, "blocks": calls})
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
