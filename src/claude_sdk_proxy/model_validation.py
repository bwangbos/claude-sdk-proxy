"""One authoritative exact backend-model identifier validator."""

from __future__ import annotations

from typing import Final, Literal

_MAX_BACKEND_MODEL_ID_BYTES: Final = 256
_GENERIC_MODEL_ALIASES: Final = frozenset(
    {"sonnet", "opus", "haiku", "latest", "default"}
)
_KNOWN_FAMILY_PREFIXES: Final = (
    "claude-sonnet-",
    "claude-opus-",
    "claude-haiku-",
)


class ExactBackendModelError(ValueError):
    """Raised when text cannot identify one exact backend model."""

    def __init__(
        self,
        reason: Literal["unicode", "bounds", "visible_ascii", "moving_alias"],
    ) -> None:
        self.reason = reason
        super().__init__(f"backend model ID is invalid: {reason}")


def require_exact_backend_model(value: object) -> str:
    """Return one bounded visible-ASCII, non-moving exact backend model ID."""
    if type(value) is not str:
        raise TypeError("backend model ID must be exact text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ExactBackendModelError("unicode") from error
    if not encoded or len(encoded) > _MAX_BACKEND_MODEL_ID_BYTES:
        raise ExactBackendModelError("bounds")
    if not value.isascii() or any(
        character < "!" or character > "~" for character in value
    ):
        raise ExactBackendModelError("visible_ascii")

    lowered = value.lower()
    if lowered in _GENERIC_MODEL_ALIASES or lowered.endswith("-latest"):
        raise ExactBackendModelError("moving_alias")
    if lowered.startswith("claude-") and lowered.rsplit("-", 1)[-1] in {
        "sonnet",
        "opus",
        "haiku",
    }:
        raise ExactBackendModelError("moving_alias")
    if lowered.startswith(_KNOWN_FAMILY_PREFIXES):
        final_component = lowered.rsplit("-", 1)[-1]
        if final_component != "exact" and not (
            len(final_component) == 8
            and final_component.isascii()
            and final_component.isdigit()
        ):
            raise ExactBackendModelError("moving_alias")
    return value


__all__ = ["ExactBackendModelError", "require_exact_backend_model"]
