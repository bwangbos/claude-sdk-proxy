"""Content-free Phase 0 probe results and fail-closed live probe entry points."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Never, SupportsIndex

from claude_sdk_proxy.attestation import current_attestation_availability

if TYPE_CHECKING:
    from claude_sdk_proxy.path_policy import PathMetadata, PathPolicy, RootKind

REDACTION_MARKER: Final = "__redacted__"
MAX_REDACTED_REPORT_BYTES: Final = 16_384
_MAX_DEPTH: Final = 8
_MAX_ITEMS: Final = 128
_MAX_ENUM_LENGTH: Final = 64
_MAX_CANARY_BYTES: Final = 256

_REPORT_NAMES = frozenset(
    {"purity", "compaction", "path_persistence", "prompt_purity"}
)
_CONTENT_KEYS = frozenset(
    {"system", "systemprompt", "messages", "prompt", "text", "toolinput", "toolresult"}
)
_SECRET_FRAGMENTS = (
    "authorization",
    "bearer",
    "token",
    "secret",
    "password",
    "apikey",
    "cookie",
    "session",
    "credential",
)
_CONTAINER_KEYS = frozenset(
    {
        "shape",
        "nested",
        "counts",
        "eventcounts",
        "pathclasscounts",
        "gates",
    }
)
_INTEGER_KEYS = frozenset({"blocks", "items", "paths", "events"})
_ENUM_KEYS = frozenset(
    {"reason", "reasoncode", "status", "outcome", "state", "eventtype", "pathclass"}
)
_BOOLEAN_SUFFIXES = (
    "available",
    "passed",
    "enabled",
    "disabled",
    "present",
    "observed",
    "succeeded",
    "exhausted",
)
_SAFE_ENUM = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SAFE_OUTPUT_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class ProbeUnavailable(RuntimeError):
    """Raised when a live probe's prerequisite gate is not affirmative."""


class SafeCanaryDigest:
    """Digest explicitly derived from a small proxy-owned benign canary."""

    __slots__ = ("_permit", "_value")
    _permit: object
    _value: str

    def __new__(cls, *_args: object, **_kwargs: object) -> SafeCanaryDigest:
        raise TypeError("SafeCanaryDigest cannot be constructed publicly")

    @classmethod
    def _create(cls, permit: object, value: str) -> SafeCanaryDigest:
        if permit is not _SAFE_CANARY_PERMIT or not re.fullmatch(
            r"[0-9a-f]{64}", value
        ):
            raise ValueError("safe canary digest must be lowercase SHA-256")
        instance = object.__new__(cls)
        instance._permit = permit
        instance._value = value
        return instance

    @property
    def value(self) -> str:
        if self._permit is not _SAFE_CANARY_PERMIT or not re.fullmatch(
            r"[0-9a-f]{64}", self._value
        ):
            raise ValueError("safe canary digest is invalid")
        return self._value

    def __copy__(self) -> Never:
        raise TypeError("SafeCanaryDigest cannot be copied")

    def __deepcopy__(self, _memo: object) -> Never:
        raise TypeError("SafeCanaryDigest cannot be copied")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> Never:
        raise TypeError("SafeCanaryDigest cannot be pickled")


_SAFE_CANARY_PERMIT = object()


def safe_canary_digest(
    canary: bytes,
    *,
    policy: PathPolicy,
    path: Path,
    expected: PathMetadata,
    root: RootKind | None = None,
) -> SafeCanaryDigest:
    """Hash a canary only after a no-follow scan of its proxy-owned file."""
    from claude_sdk_proxy.path_policy import PathPolicy, RootKind

    if not isinstance(canary, bytes) or not canary or len(canary) > _MAX_CANARY_BYTES:
        raise ValueError("safe canary must be 1..256 bytes")
    if not isinstance(policy, PathPolicy):
        raise TypeError("safe canary digest requires a PathPolicy")
    selected_root = RootKind.PROXY_OWNED if root is None else root
    if selected_root is not RootKind.PROXY_OWNED:
        raise ValueError("safe canary digest requires the proxy-owned root")
    if not policy.scan_for_canary(
        path, canary, expected=expected, root=selected_root
    ):
        raise ValueError("safe canary was not present in the verified proxy path")
    return SafeCanaryDigest._create(
        _SAFE_CANARY_PERMIT, hashlib.sha256(canary).hexdigest()
    )


def _normalized_key(key: str) -> str:
    normalized = unicodedata.normalize("NFKC", key).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _is_sensitive_key(normalized: str) -> bool:
    return normalized in _CONTENT_KEYS or any(
        fragment in normalized for fragment in _SECRET_FRAGMENTS
    )


def _is_integer_key(normalized: str) -> bool:
    return (
        normalized in _INTEGER_KEYS
        or normalized.endswith("count")
        or normalized.endswith("counts")
    )


def _is_boolean_key(normalized: str) -> bool:
    return normalized in _BOOLEAN_SUFFIXES or normalized.endswith(_BOOLEAN_SUFFIXES)


def _safe_digest_value(value: SafeCanaryDigest) -> str | None:
    try:
        return value.value
    except (AttributeError, ValueError):
        return None


def _redacted_mapping(
    value: Mapping[Any, Any], *, depth: int, seen: set[int]
) -> dict[str, object] | str:
    if depth > _MAX_DEPTH or id(value) in seen:
        return REDACTION_MARKER
    seen.add(id(value))
    try:
        try:
            entries = list(islice(iter(value.items()), _MAX_ITEMS + 1))
        except BaseException:
            return REDACTION_MARKER
        if len(entries) > _MAX_ITEMS:
            return REDACTION_MARKER
        normalized_entries: list[tuple[str, str, object]] = []
        normalized_seen: set[str] = set()
        for raw_key, child in entries:
            if not isinstance(raw_key, str) or len(raw_key) > _MAX_ENUM_LENGTH:
                return REDACTION_MARKER
            normalized = _normalized_key(raw_key)
            if not normalized or normalized in normalized_seen:
                return REDACTION_MARKER
            normalized_seen.add(normalized)
            normalized_entries.append((raw_key, normalized, child))

        output: dict[str, object] = {}
        redacted_index = 0
        for raw_key, normalized, child in sorted(
            normalized_entries, key=lambda item: item[1]
        ):
            recognized = (
                _is_sensitive_key(normalized)
                or normalized in _CONTAINER_KEYS
                or _is_integer_key(normalized)
                or _is_boolean_key(normalized)
                or normalized in _ENUM_KEYS
                or normalized.endswith("canarysha256")
                or normalized.endswith("sha256")
            )
            if recognized and _SAFE_OUTPUT_KEY.fullmatch(raw_key):
                output_key = raw_key
            elif recognized:
                output_key = f"redacted_field_{redacted_index}"
                redacted_index += 1
            else:
                output_key = f"redacted_field_{redacted_index}"
                redacted_index += 1

            if _is_sensitive_key(normalized):
                output[output_key] = REDACTION_MARKER
            elif normalized in _CONTAINER_KEYS:
                output[output_key] = _redacted_value(
                    child,
                    normalized=normalized,
                    depth=depth + 1,
                    seen=seen,
                    sequence_item=False,
                )
            elif isinstance(child, SafeCanaryDigest) and normalized.endswith(
                "canarysha256"
            ):
                output[output_key] = _safe_digest_value(child) or REDACTION_MARKER
            elif _is_integer_key(normalized):
                output[output_key] = _redacted_value(
                    child,
                    normalized=normalized,
                    depth=depth + 1,
                    seen=seen,
                    sequence_item=False,
                )
            elif _is_boolean_key(normalized) and type(child) is bool:
                output[output_key] = child
            elif normalized in _ENUM_KEYS:
                output[output_key] = (
                    child
                    if isinstance(child, str) and _SAFE_ENUM.fullmatch(child)
                    else REDACTION_MARKER
                )
            else:
                output[output_key] = REDACTION_MARKER
        return output
    finally:
        seen.discard(id(value))


def _redacted_sequence(
    value: Sequence[object], *, depth: int, seen: set[int]
) -> list[object] | str:
    try:
        length = len(value)
    except BaseException:
        return REDACTION_MARKER
    if depth > _MAX_DEPTH or id(value) in seen or length > _MAX_ITEMS:
        return REDACTION_MARKER
    seen.add(id(value))
    try:
        try:
            return [
                _redacted_value(
                    value[index],
                    normalized="",
                    depth=depth + 1,
                    seen=seen,
                    sequence_item=True,
                )
                for index in range(length)
            ]
        except BaseException:
            return REDACTION_MARKER
    finally:
        seen.discard(id(value))


def _redacted_value(
    value: object,
    *,
    normalized: str,
    depth: int,
    seen: set[int],
    sequence_item: bool,
) -> object:
    if depth > _MAX_DEPTH:
        return REDACTION_MARKER
    if isinstance(value, BaseException):
        return REDACTION_MARKER
    if isinstance(value, SafeCanaryDigest):
        digest = _safe_digest_value(value)
        return (
            digest
            if digest is not None and normalized.endswith("canarysha256")
            else REDACTION_MARKER
        )
    if isinstance(value, Mapping):
        return _redacted_mapping(value, depth=depth, seen=seen)
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray, memoryview)
    ):
        return _redacted_sequence(value, depth=depth, seen=seen)
    if type(value) is bool:
        return (
            value
            if sequence_item or _is_boolean_key(normalized)
            else REDACTION_MARKER
        )
    if type(value) is int:
        if (sequence_item or _is_integer_key(normalized)) and 0 <= value <= 2**63 - 1:
            return value
        return REDACTION_MARKER
    if type(value) is float:
        # Floats are never part of the typed evidence schema, including finite ones.
        return REDACTION_MARKER
    if isinstance(value, str) and normalized in _ENUM_KEYS:
        return value if _SAFE_ENUM.fullmatch(value) else REDACTION_MARKER
    return REDACTION_MARKER


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """One bounded, typed, content-free probe verdict."""

    name: str
    passed: bool
    evidence: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.name not in _REPORT_NAMES:
            raise ValueError("unsupported probe result name")
        if type(self.passed) is not bool:
            raise TypeError("probe passed field must be boolean")
        if not isinstance(self.evidence, Mapping):
            raise TypeError("probe evidence must be a mapping")

    def redacted_dict(self) -> dict[str, object]:
        """Return the stable bounded V1 report schema."""
        evidence = _redacted_mapping(self.evidence, depth=0, seen=set())
        report: dict[str, object] = {
            "schema_version": 1,
            "name": self.name,
            "passed": self.passed,
            "evidence": evidence,
        }
        encoded = json.dumps(
            report,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_REDACTED_REPORT_BYTES:
            report["evidence"] = REDACTION_MARKER
        return report


def _require_live_prerequisites() -> Never:
    availability = current_attestation_availability()
    if not availability.core_gate_available:
        raise ProbeUnavailable("child_attestation_unavailable")
    # The false Task 6 tuple has no live implementation. A future affirmative
    # tuple must replace this branch with its reviewed attested-client harness.
    raise ProbeUnavailable("live_probe_harness_unavailable")


def run_prompt_purity_probe() -> ProbeResult:
    """Run the live purity gate only after all Task 6 prerequisites pass."""
    _require_live_prerequisites()


def run_compaction_probe() -> ProbeResult:
    """Run the live compaction gate only after all Task 6 prerequisites pass."""
    _require_live_prerequisites()


__all__ = [
    "MAX_REDACTED_REPORT_BYTES",
    "REDACTION_MARKER",
    "ProbeResult",
    "ProbeUnavailable",
    "SafeCanaryDigest",
    "run_compaction_probe",
    "run_prompt_purity_probe",
    "safe_canary_digest",
]
