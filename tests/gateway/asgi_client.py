from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from weakref import WeakKeyDictionary

from starlette.types import ASGIApp, Message, Scope


@dataclass(frozen=True, slots=True)
class AsgiResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def json(self) -> Any:
        return json.loads(self.body)


_LIFESPAN_STATES: WeakKeyDictionary[ASGIApp, dict[str, Any]] = WeakKeyDictionary()


@asynccontextmanager
async def lifespan_app(app: ASGIApp) -> AsyncIterator[None]:
    incoming: asyncio.Queue[Message] = asyncio.Queue()
    outgoing: asyncio.Queue[Message] = asyncio.Queue()
    state: dict[str, Any] = {}

    async def receive() -> Message:
        return await incoming.get()

    async def send(message: Message) -> None:
        await outgoing.put(message)

    scope: Scope = {
        "type": "lifespan",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "state": state,
    }
    task = asyncio.create_task(app(scope, receive, send))
    await incoming.put({"type": "lifespan.startup"})
    started = await outgoing.get()
    if started["type"] != "lifespan.startup.complete":
        await task
        raise RuntimeError(f"lifespan startup failed: {started!r}")
    _LIFESPAN_STATES[app] = state
    try:
        yield
    finally:
        await incoming.put({"type": "lifespan.shutdown"})
        stopped = await outgoing.get()
        if stopped["type"] != "lifespan.shutdown.complete":
            await task
            raise RuntimeError(f"lifespan shutdown failed: {stopped!r}")
        await task
        _LIFESPAN_STATES.pop(app, None)


async def request(
    app: ASGIApp,
    method: str,
    path: str,
    body: bytes = b"",
    headers: Mapping[str, str] | None = None,
    *,
    fail_send_after: int | None = None,
    disconnect_after_start: bool = False,
    disconnect_after_request: bool = False,
    allow_no_response: bool = False,
    block_body_after_start: bool = False,
) -> AsgiResponse | None:
    request_sent = False
    response_started = asyncio.Event()
    never = asyncio.Event()
    sent: list[Message] = []
    body_sends = 0

    async def receive() -> Message:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        if disconnect_after_request:
            return {"type": "http.disconnect"}
        if disconnect_after_start:
            await response_started.wait()
            return {"type": "http.disconnect"}
        await never.wait()
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        nonlocal body_sends
        if message["type"] == "http.response.start":
            response_started.set()
        elif message["type"] == "http.response.body":
            body_sends += 1
            if fail_send_after is not None and body_sends >= fail_send_after:
                raise ConnectionError("synthetic send failure")
            if block_body_after_start:
                await never.wait()
        sent.append(message)

    raw_headers = [
        (key.lower().encode(), value.encode()) for key, value in (headers or {}).items()
    ]
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8317),
        "state": dict(_LIFESPAN_STATES.get(app, {})),
    }
    await app(scope, receive, send)
    starts = [message for message in sent if message["type"] == "http.response.start"]
    if allow_no_response and not starts:
        return None
    assert len(starts) == 1
    start = starts[0]
    response_headers = {
        key.decode().lower(): value.decode() for key, value in start["headers"]
    }
    response_body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return AsgiResponse(start["status"], response_headers, response_body)


async def post_json(
    app: ASGIApp,
    path: str,
    body: object,
    headers: Mapping[str, str] | None = None,
    *,
    fail_send_after: int | None = None,
    disconnect_after_start: bool = False,
    block_body_after_start: bool = False,
) -> AsgiResponse:
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    merged = {"content-type": "application/json", **(headers or {})}
    return await request(
        app,
        "POST",
        path,
        encoded,
        merged,
        fail_send_after=fail_send_after,
        disconnect_after_start=disconnect_after_start,
        block_body_after_start=block_body_after_start,
    )


async def post_json_then_disconnect(
    app: ASGIApp,
    path: str,
    body: object,
    after: asyncio.Event | None = None,
) -> AsgiResponse | None:
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    if after is None:
        return await request(
            app,
            "POST",
            path,
            encoded,
            {"content-type": "application/json"},
            disconnect_after_request=True,
            allow_no_response=True,
        )

    request_sent = False
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": encoded, "more_body": False}
        await after.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8317),
        "state": dict(_LIFESPAN_STATES.get(app, {})),
    }
    await app(scope, receive, send)
    assert not sent
    return None
