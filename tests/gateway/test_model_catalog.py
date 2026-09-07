import pytest

from claude_sdk_proxy.model_catalog import (
    backend_model,
    canonical_model,
    canonical_models,
)
from claude_sdk_proxy.openai_api import parse_openai_request


@pytest.mark.parametrize(
    "given,public,native",
    [
        ("sonnet", "sonnet-5", "claude-sonnet-5"),
        ("sonnet-5", "sonnet-5", "claude-sonnet-5"),
        ("opus", "opus-5", "claude-opus-5"),
        ("opus-5", "opus-5", "claude-opus-5"),
        ("opus-4.8", "opus-4.8", "claude-opus-4-8"),
        ("claude-opus-4-8", "opus-4.8", "claude-opus-4-8"),
    ],
)
def test_pinned_route(given: str, public: str, native: str) -> None:
    assert canonical_model(given) == public
    assert backend_model(given) == native


def test_other_explicit_sdk_id_preserves_existing_routing() -> None:
    assert canonical_model("claude-sonnet-4-6") == "claude-sonnet-4-6"
    assert backend_model("claude-sonnet-4-6") == "claude-sonnet-4-6"


def test_canonical_list_deduplicates_aliases_in_configured_order() -> None:
    assert canonical_models(("sonnet", "opus", "sonnet-5", "opus-4.8")) == (
        "sonnet-5",
        "opus-5",
        "opus-4.8",
    )


def test_parser_returns_canonical_model_for_compatibility_alias() -> None:
    request = parse_openai_request(
        {"model": "sonnet", "messages": [{"role": "user", "content": "hi"}]},
        frozenset({"sonnet-5"}),
    )
    assert request.model == "sonnet-5"
