"""Bounded SSE framing and Responses event translation."""

from __future__ import annotations

import codecs
import json
import re
from collections.abc import AsyncIterator
from typing import Any

from claude_sdk_proxy.domain import (
    BackendFailure,
    CanonicalMessage,
    Completed,
    ConversationEvent,
    RefusalDelta,
    TextBlock,
    TextDelta,
    ThinkingBlock,
    ThinkingCompleted,
    ThinkingDelta,
    ToolCall,
    ToolCallBlock,
)

from .replay import encode_reasoning, valid_reasoning

MAX_FRAME_BYTES = 1024 * 1024
MAX_TURN_BYTES = 16 * 1024 * 1024
MAX_ITEMS = 1024


class SubscriptionFailure(BackendFailure):
    """Allowlisted failure category only, never upstream body or exception text."""

    def __init__(
        self, category: str, *, status: int | None = None, transient: bool = False
    ):
        self.category = (
            category
            if category
            in {
                "authentication",
                "rate_limit",
                "access_denied",
                "upstream_error",
                "invalid_response",
                "missing_completion",
                "buffer_limit",
                "timeout",
                "transport",
                "closed",
            }
            else "upstream_error"
        )
        self.status = status
        self.transient = transient
        super().__init__(f"OpenAI subscription request failed ({self.category})")


def safe_identifier(value: Any) -> str | None:
    return (
        value
        if isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value)
        else None
    )


async def parse_sse(chunks: AsyncIterator[bytes]) -> AsyncIterator[dict[str, Any]]:
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    buffer = ""
    data: list[str] = []
    frame_size = total = 0
    try:
        async for chunk in chunks:
            total += len(chunk)
            if total > MAX_TURN_BYTES:
                raise SubscriptionFailure("buffer_limit")
            buffer += decoder.decode(chunk)
            while True:
                match = re.search(r"[\r\n]", buffer)
                if match is None or (
                    buffer[match.start()] == "\r" and match.end() == len(buffer)
                ):
                    break
                i = match.start()
                line = buffer[:i]
                width = 2 if buffer[i : i + 2] == "\r\n" else 1
                buffer = buffer[i + width :]
                frame_size += len(line.encode("utf-8")) + width
                if frame_size > MAX_FRAME_BYTES:
                    raise SubscriptionFailure("buffer_limit")
                if not line:
                    if data:
                        raw = "\n".join(data)
                        if raw == "[DONE]":
                            return
                        event = json.loads(raw)
                        if not isinstance(event, dict) or not isinstance(
                            event.get("type"), str
                        ):
                            raise SubscriptionFailure("invalid_response")
                        yield event
                    data.clear()
                    frame_size = 0
                elif line.startswith("data:"):
                    data.append(line[5:].removeprefix(" "))
            if frame_size + len(buffer.encode("utf-8")) > MAX_FRAME_BYTES:
                raise SubscriptionFailure("buffer_limit")
        decoder.decode(b"", final=True)
        if buffer.strip() or data:
            raise SubscriptionFailure("invalid_response")
    except UnicodeError, ValueError, RecursionError:
        raise SubscriptionFailure("invalid_response") from None


def usage_from_response(value: Any) -> dict[str, int] | None:
    if value is None:
        return None
    try:
        total = value["input_tokens"]
        output = value["output_tokens"]
        cached = value.get("input_tokens_details", {}).get("cached_tokens", 0)
        reasoning = value.get("output_tokens_details", {}).get("reasoning_tokens", 0)
        if (
            any(type(n) is not int or n < 0 for n in (total, output, cached, reasoning))
            or cached > total
            or reasoning > output
        ):
            raise ValueError
        return {
            "input_tokens": total - cached,
            "cache_read_input_tokens": cached,
            "output_tokens": output,
            "reasoning_tokens": reasoning,
        }
    except KeyError, TypeError, AttributeError, ValueError:
        raise SubscriptionFailure("invalid_response") from None


class EventTranslator:
    def __init__(self, account: str, model: str):
        self.account = account
        self.model = model
        self.items: dict[int, dict[str, Any]] = {}
        self.text: dict[int, str] = {}
        self.summaries: dict[int, str] = {}
        self.arguments: dict[int, str] = {}
        self.calls: set[int] = set()
        self.call_ids: set[str] = set()
        self.output: list[dict[str, Any]] = []
        self.finished = False
        self.cacheable = False
        self.refusals: dict[int, str] = {}

    @property
    def refusal(self) -> str:
        return "".join(self.refusals[i] for i in sorted(self.refusals))

    def _index(self, event: dict[str, Any]) -> int:
        index = event.get("output_index")
        if type(index) is not int or not 0 <= index < MAX_ITEMS:
            raise SubscriptionFailure("invalid_response")
        return index

    def _item(self, item: Any) -> dict[str, Any]:
        if not isinstance(item, dict) or item.get("type") not in {
            "message",
            "reasoning",
            "function_call",
        }:
            raise SubscriptionFailure("invalid_response")
        if not safe_identifier(item.get("id")):
            raise SubscriptionFailure("invalid_response")
        return dict(item)

    def _call(self, index: int, item: dict[str, Any]) -> list[ConversationEvent]:
        if index in self.calls:
            return []
        try:
            call_id, name, raw = item["call_id"], item["name"], item["arguments"]
            if (
                not safe_identifier(call_id)
                or not isinstance(name, str)
                or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name) is None
            ):
                raise ValueError
            if call_id in self.call_ids or not isinstance(raw, str):
                raise ValueError
            args = json.loads(raw)
            if not isinstance(args, dict):
                raise ValueError
            event = ToolCall(call_id, name, args)
        except KeyError, TypeError, ValueError, RecursionError:
            raise SubscriptionFailure("invalid_response") from None
        self.calls.add(index)
        self.call_ids.add(call_id)
        return [event]

    def consume(self, event: dict[str, Any]) -> list[ConversationEvent]:
        kind = event["type"]
        if kind in {"error", "response.failed"}:
            detail = event.get("error", event)
            if kind == "response.failed":
                response = event.get("response", {})
                detail = response.get("error", {}) if isinstance(response, dict) else {}
            code = detail.get("code") if isinstance(detail, dict) else None
            raise SubscriptionFailure(
                "upstream_error",
                transient=code in {"server_error", "rate_limit_exceeded"},
            )
        if kind in {"response.completed", "response.incomplete"}:
            return self._finish(event)
        if kind == "response.output_item.added":
            index = self._index(event)
            if index in self.items:
                raise SubscriptionFailure("invalid_response")
            self.items[index] = self._item(event.get("item"))
        elif kind == "response.output_item.done":
            index = self._index(event)
            item = self._item(event.get("item"))
            previous = self.items.get(index)
            if previous is not None and any(
                previous.get(k) != item.get(k)
                for k in ("id", "type", "call_id", "name")
            ):
                raise SubscriptionFailure("invalid_response")
            if index in self.arguments and self.arguments[index] != item.get(
                "arguments"
            ):
                raise SubscriptionFailure("invalid_response")
            self.items[index] = item
            if self.items[index]["type"] == "function_call":
                return self._call(index, self.items[index])
        elif kind in {
            "response.output_text.delta",
            "response.reasoning_summary_text.delta",
            "response.function_call_arguments.delta",
            "response.refusal.delta",
        }:
            index = self._index(event)
            delta = event.get("delta")
            if index not in self.items or not isinstance(delta, str):
                raise SubscriptionFailure("invalid_response")
            expected = {
                "response.output_text.delta": "message",
                "response.refusal.delta": "message",
                "response.reasoning_summary_text.delta": "reasoning",
                "response.function_call_arguments.delta": "function_call",
            }[kind]
            if self.items[index]["type"] != expected:
                raise SubscriptionFailure("invalid_response")
            if kind == "response.output_text.delta":
                self.text[index] = self.text.get(index, "") + delta
                return [TextDelta(delta)]
            if kind == "response.reasoning_summary_text.delta":
                self.summaries[index] = self.summaries.get(index, "") + delta
                return [ThinkingDelta(index, delta)]
            if kind == "response.function_call_arguments.delta":
                self.arguments[index] = self.arguments.get(index, "") + delta
            else:
                self.refusals[index] = self.refusals.get(index, "") + delta
                return [RefusalDelta(delta)]
        return []

    def _finish(self, event: dict[str, Any]) -> list[ConversationEvent]:
        response = event.get("response")
        if (
            not isinstance(response, dict)
            or not isinstance(response.get("output"), list)
            or len(response["output"]) > MAX_ITEMS
        ):
            raise SubscriptionFailure("invalid_response")
        status = response.get("status")
        if status not in {"completed", "incomplete"}:
            raise SubscriptionFailure("upstream_error")
        result: list[ConversationEvent] = []
        output = [self._item(item) for item in response["output"]]
        for index, item in enumerate(output):
            existing = self.items.get(index)
            if existing is not None and (
                existing["id"] != item["id"] or existing["type"] != item["type"]
            ):
                raise SubscriptionFailure("invalid_response")
            if item["type"] == "function_call":
                if index in self.calls and existing != item:
                    raise SubscriptionFailure("invalid_response")
                if index in self.arguments and self.arguments[index] != item.get(
                    "arguments"
                ):
                    raise SubscriptionFailure("invalid_response")
                result.extend(self._call(index, item))
            elif item["type"] == "reasoning":
                if (
                    existing
                    and not item.get("encrypted_content")
                    and existing.get("encrypted_content")
                ):
                    item["encrypted_content"] = existing["encrypted_content"]
                # Terminal output is authoritative and may first supply ciphertext.
                if not valid_reasoning(item):
                    raise SubscriptionFailure("invalid_response")
                summary = "".join(s["text"] for s in item["summary"])
                prior = self.summaries.get(index, "")
                if not summary.startswith(prior):
                    raise SubscriptionFailure("invalid_response")
                if summary[len(prior) :]:
                    result.append(ThinkingDelta(index, summary[len(prior) :]))
                try:
                    signature = encode_reasoning(item, self.account, self.model)
                except ValueError:
                    raise SubscriptionFailure("buffer_limit") from None
                result.append(
                    ThinkingCompleted(index, ThinkingBlock(summary, signature))
                )
            else:
                content = item.get("content")
                if not isinstance(content, list):
                    raise SubscriptionFailure("invalid_response")
                text = ""
                refusal = ""
                for part in content:
                    if not isinstance(part, dict):
                        raise SubscriptionFailure("invalid_response")
                    if part.get("type") == "output_text" and isinstance(
                        part.get("text"), str
                    ):
                        text += part["text"]
                    elif part.get("type") == "refusal" and isinstance(
                        part.get("refusal"), str
                    ):
                        refusal += part["refusal"]
                    else:
                        raise SubscriptionFailure("invalid_response")
                prior = self.text.get(index, "")
                if not text.startswith(prior):
                    raise SubscriptionFailure("invalid_response")
                if text[len(prior) :]:
                    result.append(TextDelta(text[len(prior) :]))
                prior_refusal = self.refusals.get(index, "")
                if not refusal.startswith(prior_refusal):
                    raise SubscriptionFailure("invalid_response")
                if refusal[len(prior_refusal) :]:
                    result.append(RefusalDelta(refusal[len(prior_refusal) :]))
                self.refusals[index] = refusal
        if set(self.items) - set(range(len(output))):
            raise SubscriptionFailure("invalid_response")
        self.output = output
        self.finished = True
        self.cacheable = status == "completed"
        reason = "end_turn"
        if status == "incomplete":
            detail = response.get("incomplete_details", {})
            why = detail.get("reason") if isinstance(detail, dict) else None
            if why == "max_output_tokens":
                reason = "max_tokens"
            elif why == "content_filter":
                reason = "refusal"
            else:
                raise SubscriptionFailure("upstream_error")
        elif self.refusal:
            reason = "refusal"
        elif self.calls:
            reason = "tool_use"
        result.append(
            Completed(
                reason,
                usage_from_response(response.get("usage")),
                provider="openai-subscription",
                refusal=self.refusal or None,
            )
        )
        return result

    def assistant_message(self) -> CanonicalMessage:
        blocks: list[TextBlock | ToolCallBlock] = []
        for item in self.output:
            if item["type"] == "message":
                blocks.extend(
                    TextBlock(p["text"])
                    for p in item["content"]
                    if p["type"] == "output_text"
                )
            elif item["type"] == "function_call":
                blocks.append(
                    ToolCallBlock(
                        item["call_id"], item["name"], json.loads(item["arguments"])
                    )
                )
        if self.refusal:
            blocks.append(TextBlock(self.refusal))
        return CanonicalMessage("assistant", tuple(blocks) or (TextBlock(""),))
