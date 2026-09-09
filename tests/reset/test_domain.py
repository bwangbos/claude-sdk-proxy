import pytest

from quaylet.domain import (
    CanonicalMessage,
    CanonicalRequest,
    CapabilityReport,
    CapabilityStatus,
    TextRequest,
    ToolDefinition,
)


def report(**overrides: CapabilityStatus) -> CapabilityReport:
    values: dict[str, CapabilityStatus] = {
        "authentication": "pass",
        "streaming": "pass",
        "prompt_construction": "pass",
        "multi_turn": "pass",
        "structured_tools": "pass",
    }
    values.update(overrides)
    return CapabilityReport(backend="fake", evidence=(), **values)


def test_single_turn_text_gate_does_not_require_history_or_tools() -> None:
    result = report(multi_turn="fail", structured_tools="fail")
    assert result.single_turn_text_viable is True
    assert result.compatibility_proxy_viable is False
    assert result.agent_harness_viable is False


def test_compatibility_gate_requires_multi_turn() -> None:
    assert report(multi_turn="fail").single_turn_text_viable is True
    assert report(multi_turn="fail").compatibility_proxy_viable is False
    assert report(structured_tools="fail").compatibility_proxy_viable is True


def test_agent_harness_gate_also_requires_tools() -> None:
    assert report(multi_turn="fail").agent_harness_viable is False
    assert report(structured_tools="untested").agent_harness_viable is False
    assert report().agent_harness_viable is True


def test_canonical_request_rejects_an_empty_model() -> None:
    with pytest.raises(ValueError) as error:
        CanonicalRequest(
            model="",
            system="",
            messages=(CanonicalMessage(role="user", content="hello"),),
        )

    assert str(error.value) == "model must not be empty"


def test_canonical_request_rejects_an_empty_message_list() -> None:
    with pytest.raises(ValueError) as error:
        CanonicalRequest(model="model", system="", messages=())

    assert str(error.value) == "messages must not be empty"


def test_canonical_request_rejects_empty_message_content() -> None:
    with pytest.raises(ValueError) as error:
        CanonicalRequest(
            model="model",
            system="",
            messages=(CanonicalMessage(role="user", content=""),),
        )

    assert str(error.value) == "message content must not be empty"


def test_canonical_request_rejects_an_invalid_tool_name() -> None:
    with pytest.raises(ValueError) as error:
        CanonicalRequest(
            model="model",
            system="",
            messages=(CanonicalMessage(role="user", content="hello"),),
            tools=(
                ToolDefinition(
                    name="bad.name",
                    description="",
                    input_schema={},
                ),
            ),
        )

    assert str(error.value) == "tool name is invalid"


def test_text_request_requires_alternating_messages_ending_in_user() -> None:
    with pytest.raises(ValueError, match="alternate"):
        TextRequest(
            model="sonnet",
            system="",
            messages=(
                CanonicalMessage("user", "one"),
                CanonicalMessage("user", "two"),
            ),
            max_tokens=1024,
            stream=False,
            include_usage=False,
        )


def test_text_request_accepts_canonical_text_helpers() -> None:
    request = TextRequest(
        model="sonnet",
        system="",
        messages=(CanonicalMessage.user_text("hello"),),
        max_tokens=1024,
        stream=False,
    )

    assert request.next_prompt == "hello"
