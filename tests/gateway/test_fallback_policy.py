import json

import pytest

from claude_sdk_proxy.app import create_app
from claude_sdk_proxy.domain import RequestValidationError
from claude_sdk_proxy.fallback_policy import native_allowlist, resolve_fallback
from claude_sdk_proxy.sdk_session import SdkSession
from tests.gateway.asgi_client import lifespan_app, post_json
from tests.gateway.fakes import (
    FakeSdkClient,
    FakeSessionFactory,
    FixedTemporaryDirectory,
)


def test_missing_native_fallback_target_is_rejected() -> None:
    with pytest.raises(RequestValidationError):
        native_allowlist("opus-5", ("opus-5",), "auto")


def test_duplicate_policy_header_is_rejected() -> None:
    with pytest.raises(RequestValidationError):
        resolve_fallback(["auto", "off"], "off")


@pytest.mark.parametrize("value", ["", "AUTO", " off", "auto ", "maybe"])
def test_invalid_policy_header_is_rejected(value: str) -> None:
    with pytest.raises(RequestValidationError):
        resolve_fallback([value], "off")


def test_absent_policy_inherits_default() -> None:
    assert resolve_fallback([], "off") == "off"


def test_strict_allowlist_contains_only_pinned_active_model() -> None:
    assert native_allowlist("sonnet", ("opus-4.8", "sonnet-5"), "off") == (
        "claude-sonnet-5",
    )


def test_auto_allows_only_observed_opus_transition() -> None:
    assert native_allowlist("opus", ("sonnet-5", "opus-4.8", "opus-5"), "auto") == (
        "claude-opus-5",
        "claude-opus-4-8",
    )


def test_auto_does_not_add_a_route_for_direct_non_opus_request() -> None:
    assert native_allowlist("opus-4.8", ("opus-5", "opus-4.8"), "auto") == (
        "claude-opus-4-8",
    )


def test_application_gates_auto_until_buffering_is_integrated() -> None:
    with pytest.raises(ValueError, match="not available"):
        create_app(
            models=("opus-5", "opus-4.8"),
            refusal_fallback="auto",
            session_factory=FakeSessionFactory(()),
        )


@pytest.mark.anyio
async def test_sdk_options_pin_model_and_override_ambient_aliases(tmp_path) -> None:
    client = FakeSdkClient(())
    session = SdkSession(
        "sonnet-5",
        "",
        allowed_backend_models=("claude-sonnet-5",),
        directory_factory=lambda: FixedTemporaryDirectory(tmp_path),
        client_factory=client.capture_options,
    )
    await session.start()
    try:
        assert client.options is not None
        assert client.options.model == "claude-sonnet-5"
        assert json.loads(client.options.settings) == {
            "availableModels": ["claude-sonnet-5"]
        }
        assert client.options.setting_sources == []
    finally:
        await session.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path,body",
    [
        (
            "/v1/chat/completions",
            {"model": "sonnet-5", "messages": [{"role": "user", "content": "hi"}]},
        ),
        (
            "/v1/messages",
            {
                "model": "sonnet-5",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
            },
        ),
    ],
)
async def test_invalid_policy_value_is_400_in_both_dialects(path, body) -> None:
    app = create_app(models=("sonnet-5",), session_factory=FakeSessionFactory(()))
    async with lifespan_app(app):
        response = await post_json(
            app, path, body, {"x-claude-proxy-refusal-fallback": "AUTO"}
        )
    assert response.status == 400
