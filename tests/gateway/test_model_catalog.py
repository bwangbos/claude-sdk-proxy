import pytest

from quaylet.model_catalog import (
    backend_model,
    canonical_model,
    canonical_models,
)
from quaylet.openai_api import parse_openai_request


@pytest.mark.parametrize(
    "given,public,native",
    [
        ("sonnet", "sonnet-5", "claude-sonnet-5"),
        ("sonnet-5", "sonnet-5", "claude-sonnet-5"),
        ("opus", "opus-5", "claude-opus-5"),
        ("opus-5", "opus-5", "claude-opus-5"),
        ("opus-4.8", "opus-4.8", "claude-opus-4-8"),
        ("claude-opus-4-8", "opus-4.8", "claude-opus-4-8"),
        ("haiku", "haiku-4.5", "claude-haiku-4-5-20251001"),
        ("fable-5.1", "fable-5.1", "claude-fable-5-1"),
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


@pytest.mark.parametrize("model", ["fable-5.1", "claude-fable-5-1"])
def test_fable_uses_required_adaptive_thinking_when_unspecified(model):
    from quaylet.anthropic_api import parse_anthropic_request

    for parser in (parse_openai_request, parse_anthropic_request):
        request = parser(
            {
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 128,
            },
            frozenset({"fable-5.1"}),
        )
        assert request.thinking.mode == "adaptive"


def test_fable_rejects_explicit_disabled_thinking():
    from quaylet.anthropic_api import parse_anthropic_request
    from quaylet.domain import RequestValidationError

    for parser, controls in (
        (parse_openai_request, {"reasoning_effort": "none"}),
        (parse_anthropic_request, {"thinking": {"type": "disabled"}}),
    ):
        with pytest.raises(RequestValidationError):
            parser(
                {
                    "model": "fable-5.1",
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 128,
                    **controls,
                },
                frozenset({"fable-5.1"}),
            )


def test_fable_and_opus47_accept_supported_effort():
    for model in ("fable-5.1", "claude-opus-4-7"):
        request = parse_openai_request(
            {
                "model": model,
                "reasoning_effort": "max",
                "messages": [{"role": "user", "content": "hi"}],
            },
            frozenset({model}),
        )
        assert request.thinking.effort == "max"


def test_haiku_supports_budget_thinking_not_adaptive_effort():
    from quaylet.anthropic_api import parse_anthropic_request
    from quaylet.domain import RequestValidationError

    body = {
        "model": "haiku-4.5",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 4096,
    }
    request = parse_anthropic_request(
        {**body, "thinking": {"type": "enabled", "budget_tokens": 2048}},
        frozenset({"haiku-4.5"}),
    )
    assert request.thinking.mode == "enabled"
    assert request.thinking.budget_tokens == 2048
    with pytest.raises(RequestValidationError):
        parse_openai_request(
            {**body, "reasoning_effort": "high"}, frozenset({"haiku-4.5"})
        )
