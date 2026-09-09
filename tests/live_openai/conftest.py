from __future__ import annotations

import os

import httpx
import pytest


@pytest.fixture(scope="session")
def openai_live_opt_in() -> None:
    if os.environ.get("OPENAI_SUBSCRIPTION_LIVE") != "1":
        pytest.fail(
            "set OPENAI_SUBSCRIPTION_LIVE=1 only after completing "
            "`uv run claude-proxy login openai`"
        )


@pytest.fixture(scope="session")
def openai_live_base_url(openai_live_opt_in: None) -> str:
    del openai_live_opt_in
    return os.environ.get(
        "OPENAI_SUBSCRIPTION_TEST_BASE_URL", "http://127.0.0.1:8318"
    ).rstrip("/")


@pytest.fixture
def openai_live_client(openai_live_base_url: str):
    with httpx.Client(base_url=openai_live_base_url, timeout=120.0) as client:
        health = client.get("/health")
        assert health.status_code == 200, health.text
        yield client
