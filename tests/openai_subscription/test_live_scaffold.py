import httpx
import pytest

from tests.live_openai.test_subscription import (
    assert_expected_color,
    assert_response_identity,
)


def test_live_identity_requires_proxy_route_and_matching_observed_body():
    response = httpx.Response(
        200,
        headers={
            "x-quaylet-requested-model": "gpt-6-astra",
            "x-quaylet-actual-model": "gpt-6-astra-2026-09-08",
        },
        json={"model": "gpt-6-astra-2026-09-08"},
    )
    assert_response_identity(response, "gpt-6-astra")

    for headers, model in (
        ({"x-quaylet-actual-model": "dated"}, "dated"),
        (
            {
                "x-quaylet-requested-model": "gpt-6-astra",
                "x-quaylet-actual-model": "dated",
            },
            "different",
        ),
    ):
        with pytest.raises(AssertionError):
            assert_response_identity(
                httpx.Response(200, headers=headers, json={"model": model}),
                "gpt-6-astra",
            )


def test_live_compaction_oracle_requires_expected_color_without_contradiction():
    assert_expected_color("The remembered color is RED.", "red")
    for answer in ("I do not know.", "blue", "red, not blue"):
        with pytest.raises(AssertionError):
            assert_expected_color(answer, "red")
