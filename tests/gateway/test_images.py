from __future__ import annotations

import base64
import re

import pytest

from claude_sdk_proxy.anthropic_api import parse_anthropic_request
from claude_sdk_proxy.app import create_app
from claude_sdk_proxy.domain import (
    BackendFailure,
    CanonicalMessage,
    ImageBlock,
    ImagePrompt,
    RequestValidationError,
    TextBlock,
    ToolResultBlock,
    UnsupportedFeature,
)
from claude_sdk_proxy.images import render_image, result_identity
from claude_sdk_proxy.openai_api import parse_openai_request
from claude_sdk_proxy.sdk_history import seed_history
from claude_sdk_proxy.sdk_session import SdkSession
from claude_sdk_proxy.session_identity import request_fingerprint
from tests.fixtures.image_data import solid_png
from tests.gateway.asgi_client import lifespan_app, post_json
from tests.gateway.fakes import FakeConversationSession


def image_body(dialect="anthropic", image=None):
    image = image or ImageBlock("image/png", solid_png())
    block = (
        render_image(image)
        if dialect == "anthropic"
        else {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + image.data},
        }
    )
    return {
        "model": "sonnet",
        "max_tokens": 128,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "before"},
                    block,
                    {"type": "text", "text": "after"},
                ],
            }
        ],
    }


@pytest.mark.parametrize("dialect", ["anthropic", "openai"])
def test_image_order_and_identity(dialect):
    parser = parse_anthropic_request if dialect == "anthropic" else parse_openai_request
    request = parser(image_body(dialect), frozenset({"sonnet"}))
    assert isinstance(request.next_prompt, ImagePrompt)
    assert request.messages[0].blocks == (
        TextBlock("before"),
        ImageBlock("image/png", solid_png()),
        TextBlock("after"),
    )
    changed = parser(
        image_body(dialect, ImageBlock("image/png", solid_png(0, 0, 255))),
        frozenset({"sonnet"}),
    )
    assert request_fingerprint(request) != request_fingerprint(changed)


@pytest.mark.parametrize(
    "data", ["", "not base64!", "AAAA", base64.b64encode(b"GIF89a").decode()]
)
def test_invalid_png_rejected(data):
    with pytest.raises(RequestValidationError):
        ImageBlock("image/png", data)


@pytest.mark.parametrize("role", ["assistant", "system"])
def test_openai_images_not_accepted_in_instruction_or_assistant_roles(role):
    body = image_body("openai")
    body["messages"][0]["role"] = role
    with pytest.raises((RequestValidationError, UnsupportedFeature)):
        parse_openai_request(body, frozenset({"sonnet"}))


@pytest.mark.parametrize(
    "url", ["https://example.com/a.png", "file:///tmp/a.png", "http://127.0.0.1/a.png"]
)
def test_remote_and_file_urls_rejected(url):
    body = image_body("openai")
    body["messages"][0]["content"][1]["image_url"]["url"] = url
    with pytest.raises(UnsupportedFeature):
        parse_openai_request(body, frozenset({"sonnet"}))


@pytest.mark.parametrize("detail", ["low", "high", None, [], {}])
def test_openai_image_detail_controls_rejected(detail):
    body = image_body("openai")
    body["messages"][0]["content"][1]["image_url"]["detail"] = detail
    with pytest.raises(UnsupportedFeature):
        parse_openai_request(body, frozenset({"sonnet"}))


@pytest.mark.parametrize("dialect", ["anthropic", "openai"])
def test_image_only_user_prompt(dialect):
    body = image_body(dialect)
    body["messages"][0]["content"] = [body["messages"][0]["content"][1]]
    parser = parse_anthropic_request if dialect == "anthropic" else parse_openai_request
    request = parser(body, frozenset({"sonnet"}))
    assert request.next_prompt == ImagePrompt((ImageBlock("image/png", solid_png()),))


def test_image_error_tool_results_are_rejected():
    from claude_sdk_proxy.tool_contract import validate_tool_results

    result = ToolResultBlock("call_a", (ImageBlock("image/png", solid_png()),), True)
    with pytest.raises(RequestValidationError):
        validate_tool_results((result,))


@pytest.mark.parametrize(
    "limit", ["MAX_IMAGES", "MAX_IMAGE_TOTAL_BYTES", "MAX_IMAGE_BYTES"]
)
def test_images_have_bounded_request_budget(monkeypatch, limit):
    import claude_sdk_proxy.images as images

    body = image_body()
    monkeypatch.setattr(images, limit, 0)
    with pytest.raises(RequestValidationError):
        parse_anthropic_request(body, frozenset({"sonnet"}))


def test_nested_tool_images_count_toward_budget(monkeypatch):
    import claude_sdk_proxy.images as images

    image = ImageBlock("image/png", solid_png())
    monkeypatch.setattr(images, "MAX_IMAGES", 0)
    with pytest.raises(RequestValidationError):
        images.validate_image_budget(
            (CanonicalMessage("user", (ToolResultBlock("call_a", (image,), False),)),)
        )


def test_image_echo_cannot_be_spoofed_by_text():
    image = ImageBlock("image/png", solid_png())
    identity = result_identity((image,))
    assert identity != result_identity((str(identity),))
    assert SdkSession._normalize_echo_content([render_image(image)]) == identity
    assert identity != result_identity((ImageBlock("image/png", solid_png(0, 0, 255)),))
    with pytest.raises(BackendFailure):
        SdkSession._normalize_echo_content([{"type": "image", "source": {}}])


@pytest.mark.anyio
async def test_image_history_is_structured_in_ephemeral_store(tmp_path):
    image = ImageBlock("image/png", solid_png())
    seeded = await seed_history(
        (CanonicalMessage("user", (image,)), CanonicalMessage("assistant", "red")),
        cwd=tmp_path,
        model="sonnet",
        sdk_tool_names={},
    )
    entries = await seeded.store.load(
        {"project_key": seeded.project_key, "session_id": seeded.session_id}
    )
    assert entries[0]["message"]["content"] == [render_image(image)]


@pytest.mark.anyio
@pytest.mark.parametrize("stream", [False, True])
async def test_image_http_replay_and_rebase(stream):
    sessions = []

    def factory(*args, **kwargs):
        session = FakeConversationSession("red")
        sessions.append(session)
        return session

    app = create_app(models=("sonnet",), session_factory=factory)
    body = image_body()
    body["stream"] = stream
    headers = {"x-claude-proxy-session": "image"}
    async with lifespan_app(app):
        first = await post_json(app, "/v1/messages", body, headers=headers)
        replay = await post_json(app, "/v1/messages", body, headers=headers)
        assert first.status == replay.status == 200
        assert re.sub(rb"msg_[0-9a-f]+", b"msg_ID", first.body) == re.sub(
            rb"msg_[0-9a-f]+", b"msg_ID", replay.body
        )
        assert len(sessions) == 1
        assert isinstance(sessions[0].prompts[0], ImagePrompt)
        changed = image_body(image=ImageBlock("image/png", solid_png(0, 0, 255)))
        second = await post_json(app, "/v1/messages", changed, headers=headers)
        assert second.status == 200
        assert len(sessions) == 2


def test_anthropic_cache_hints_are_advisory_and_validated():
    body = image_body()
    body["system"] = [
        {"type": "text", "text": "exact", "cache_control": {"type": "ephemeral"}}
    ]
    body["messages"][0]["content"][1]["cache_control"] = {
        "type": "ephemeral",
        "ttl": "5m",
    }
    request = parse_anthropic_request(body, frozenset({"sonnet"}))
    assert request.system == "exact"
    without_hints = image_body()
    without_hints["system"] = "exact"
    plain = parse_anthropic_request(without_hints, frozenset({"sonnet"}))
    assert request_fingerprint(request) == request_fingerprint(plain)
    body["messages"][0]["content"][1]["cache_control"]["ttl"] = []
    with pytest.raises(RequestValidationError):
        parse_anthropic_request(body, frozenset({"sonnet"}))
