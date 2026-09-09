"""Strict content-free usage schemas and exact per-dialect mapping evidence."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal, cast

from quaylet.model_validation import (
    ExactBackendModelError,
    require_exact_backend_model,
)

_MAX_ROWS: Final = 4096
_MAX_FIELDS: Final = 128
_MAX_BINDINGS: Final = 128
_MAX_PATH_DEPTH: Final = 8
_MAX_PATH_SEGMENT_BYTES: Final = 64
_MAX_JSON_DEPTH: Final = 16
_MAX_JSON_NODES: Final = 100_000
_MAX_JSON_STRING_BYTES: Final = 1024
_MAX_USAGE_INTEGER: Final = 2**63 - 1
_PATH_SEGMENT = re.compile(r"[a-z][a-z0-9_]*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
_THINKING_MODES = frozenset({"null", "disabled", "adaptive", "enabled"})


class EvidenceSchemaError(ValueError):
    """Raised when usage evidence is ambiguous, lossy, or noncanonical."""


class UsageScalarKind(StrEnum):
    NONNEGATIVE_INTEGER = "nonnegative_integer"
    STRING = "string"
    BOOLEAN = "boolean"


class UsageOperationClass(StrEnum):
    ORDINARY = "ordinary"
    TOOL_USE_BOUNDARY = "tool_use_boundary"
    POST_TOOL_RESULT = "post_tool_result"


class UsageDialect(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class UsageMappingFailure(StrEnum):
    SDK_SHAPE_UNSTABLE = "sdk_shape_unstable"
    UNREPRESENTABLE_SDK_FIELD = "unrepresentable_sdk_field"
    ILLEGAL_PUBLIC_PATH = "illegal_public_path"
    PATH_COLLISION = "path_collision"
    NULLABILITY_MISMATCH = "nullability_mismatch"
    INVALID_DERIVATION = "invalid_derivation"


def _error(message: str) -> EvidenceSchemaError:
    return EvidenceSchemaError(message)


def _validate_json_tree(value: object) -> None:
    remaining = _MAX_JSON_NODES
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        remaining -= 1
        if remaining < 0:
            raise _error("usage evidence exceeds its JSON item bound")
        if depth > _MAX_JSON_DEPTH:
            raise _error("usage evidence exceeds its JSON depth bound")
        if current is None or type(current) in {bool, int, float}:
            continue
        if type(current) is str:
            try:
                encoded = current.encode("utf-8")
            except UnicodeEncodeError:
                # Known fields report a field-specific Unicode error below;
                # unknown fields are rejected before canonical encoding.
                continue
            if len(encoded) > _MAX_JSON_STRING_BYTES:
                raise _error("usage evidence string exceeds its byte bound")
            continue
        if type(current) is list:
            stack.extend((child, depth + 1) for child in current)
            continue
        if type(current) is dict:
            for key, child in current.items():
                if type(key) is not str:
                    raise _error("usage evidence JSON object keys must be text")
                stack.append((key, depth + 1))
                stack.append((child, depth + 1))
            continue
        raise _error("usage evidence must be an exact JSON object")


def _object(
    value: object,
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
    label: str,
) -> dict[str, object]:
    if type(value) is not dict:
        raise _error(f"{label} must be an exact JSON object")
    result = cast(dict[str, object], value)
    keys = set(result)
    unknown = keys - required - optional
    if unknown:
        raise _error(f"{label} has an unknown field")
    if required - keys:
        raise _error(f"{label} is missing a required field")
    return result


def _array(value: object, *, maximum: int, label: str) -> list[object]:
    if type(value) is not list:
        raise _error(f"{label} must be a JSON array")
    result = cast(list[object], value)
    if len(result) > maximum:
        raise _error(f"{label} exceeds its item bound")
    return result


def _exact_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise _error(f"{label} must be a boolean")
    return value


def _exact_string(value: object, label: str) -> str:
    if type(value) is not str:
        raise _error(f"{label} must be a string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _error(f"{label} must be valid Unicode") from error
    if not encoded or len(encoded) > _MAX_JSON_STRING_BYTES:
        raise _error(f"{label} is outside its string byte bound")
    return value


def _enum_value[T: StrEnum](enum_type: type[T], value: object, label: str) -> T:
    if type(value) is not str:
        raise _error(f"{label} must be an exact enum string")
    try:
        return enum_type(value)
    except ValueError as error:
        raise _error(f"{label} is not supported") from error


def _path(value: object, label: str) -> tuple[str, ...]:
    values = _array(value, maximum=_MAX_PATH_DEPTH, label=label)
    if not values:
        raise _error(f"{label} cannot be empty")
    output: list[str] = []
    for value_part in values:
        part = _exact_string(value_part, label)
        if (
            len(part.encode("utf-8")) > _MAX_PATH_SEGMENT_BYTES
            or not part.isascii()
            or _PATH_SEGMENT.fullmatch(part) is None
        ):
            raise _error(f"{label} contains an invalid path segment")
        output.append(part)
    return tuple(output)


def _path_to_json(value: tuple[str, ...]) -> list[str]:
    return list(value)


def _tuple_path(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise _error(f"{label} must be an immutable tuple")
    return _path(list(cast(tuple[object, ...], value)), label)


def _has_path_collision(paths: tuple[tuple[str, ...], ...]) -> bool:
    ordered = sorted(paths)
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            if len(left) <= len(right) and right[: len(left)] == left:
                return True
    return False


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise _error("usage evidence cannot be canonically encoded") from error


def _digest(domain: bytes, value: object) -> str:
    return hashlib.sha256(domain + b"\0" + _canonical_bytes(value)).hexdigest()


def _supplied_digest(value: object, expected: str, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise _error(f"{label} must be a lowercase SHA-256 digest")
    if value != expected:
        raise _error(f"{label} digest mismatch")
    return value


def _validate_runtime_digest(value: object) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise _error("runtime_digest must be a lowercase SHA-256 digest")
    return value


def _validate_backend_model(value: object) -> str:
    try:
        return require_exact_backend_model(value)
    except TypeError as error:
        raise _error("backend model ID must be exact text") from error
    except ExactBackendModelError as error:
        if error.reason == "moving_alias":
            raise _error(
                "backend model ID must be exact, not a moving alias"
            ) from error
        raise _error("backend model ID must be bounded visible ASCII") from error


@dataclass(frozen=True, slots=True)
class UsageTupleKey:
    runtime_digest: str
    backend_model_id: str
    thinking_mode: Literal["null", "disabled", "adaptive", "enabled"]
    effort: str | None
    budget_tokens: int | None
    operation_class: UsageOperationClass

    def __post_init__(self) -> None:
        _validate_runtime_digest(self.runtime_digest)
        _validate_backend_model(self.backend_model_id)
        if (
            type(self.thinking_mode) is not str
            or self.thinking_mode not in _THINKING_MODES
        ):
            raise _error("thinking identity mode is not supported")
        if self.effort is not None and (
            type(self.effort) is not str or self.effort not in _EFFORTS
        ):
            raise _error("effort is not supported")
        if type(self.operation_class) is not UsageOperationClass:
            raise _error("operation_class is not supported")
        if self.thinking_mode == "null":
            if self.effort is not None or self.budget_tokens is not None:
                raise _error(
                    "thinking identity null requires absent effort and budget_tokens"
                )
        elif self.thinking_mode == "enabled":
            if (
                type(self.budget_tokens) is not int
                or self.budget_tokens <= 0
                or self.budget_tokens > _MAX_USAGE_INTEGER
            ):
                raise _error(
                    "enabled thinking budget_tokens must be a positive integer"
                )
        elif self.budget_tokens is not None:
            raise _error("non-enabled thinking budget_tokens must be null")

    @classmethod
    def from_json(cls, value: object) -> UsageTupleKey:
        raw = _object(
            value,
            required=frozenset(
                {
                    "runtime_digest",
                    "backend_model_id",
                    "thinking_mode",
                    "effort",
                    "budget_tokens",
                    "operation_class",
                }
            ),
            label="usage tuple key",
        )
        thinking_mode = _exact_string(raw["thinking_mode"], "thinking identity")
        effort_value = raw["effort"]
        if effort_value is not None and type(effort_value) is not str:
            raise _error("effort must be a string or null")
        budget_value = raw["budget_tokens"]
        if budget_value is not None and type(budget_value) is not int:
            raise _error("budget_tokens must be an integer or null")
        return cls(
            runtime_digest=_validate_runtime_digest(raw["runtime_digest"]),
            backend_model_id=_validate_backend_model(raw["backend_model_id"]),
            thinking_mode=cast(
                Literal["null", "disabled", "adaptive", "enabled"], thinking_mode
            ),
            effort=effort_value,
            budget_tokens=budget_value,
            operation_class=_enum_value(
                UsageOperationClass, raw["operation_class"], "operation_class"
            ),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "runtime_digest": self.runtime_digest,
            "backend_model_id": self.backend_model_id,
            "thinking_mode": self.thinking_mode,
            "effort": self.effort,
            "budget_tokens": self.budget_tokens,
            "operation_class": self.operation_class.value,
        }


@dataclass(frozen=True, slots=True)
class UsageFieldSpec:
    sdk_path: tuple[str, ...]
    kind: UsageScalarKind
    required: bool
    nullable: bool

    def __post_init__(self) -> None:
        _tuple_path(self.sdk_path, "SDK field path")
        if type(self.kind) is not UsageScalarKind:
            raise _error("SDK scalar kind is not supported")
        _exact_bool(self.required, "SDK field required")
        _exact_bool(self.nullable, "SDK field nullable")

    @classmethod
    def from_json(cls, value: object) -> UsageFieldSpec:
        raw = _object(
            value,
            required=frozenset({"sdk_path", "kind", "required", "nullable"}),
            label="usage field",
        )
        return cls(
            sdk_path=_path(raw["sdk_path"], "SDK field path"),
            kind=_enum_value(UsageScalarKind, raw["kind"], "SDK scalar kind"),
            required=_exact_bool(raw["required"], "SDK field required"),
            nullable=_exact_bool(raw["nullable"], "SDK field nullable"),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "sdk_path": _path_to_json(self.sdk_path),
            "kind": self.kind.value,
            "required": self.required,
            "nullable": self.nullable,
        }


@dataclass(frozen=True, slots=True)
class UsageIdentityBinding:
    sdk_path: tuple[str, ...]
    public_path: tuple[str, ...]

    def __post_init__(self) -> None:
        _tuple_path(self.sdk_path, "identity SDK path")
        _tuple_path(self.public_path, "identity public path")

    @classmethod
    def from_json(cls, value: object) -> UsageIdentityBinding:
        raw = _object(
            value,
            required=frozenset({"sdk_path", "public_path"}),
            label="identity binding",
        )
        return cls(
            sdk_path=_path(raw["sdk_path"], "identity SDK path"),
            public_path=_path(raw["public_path"], "identity public path"),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "sdk_path": _path_to_json(self.sdk_path),
            "public_path": _path_to_json(self.public_path),
        }


@dataclass(frozen=True, slots=True)
class UsageDerivedField:
    public_path: tuple[str, ...]
    operation: Literal["checked_sum"]
    source_sdk_paths: tuple[tuple[str, ...], ...]

    def __post_init__(self) -> None:
        _tuple_path(self.public_path, "derived public path")
        if type(self.operation) is not str or self.operation != "checked_sum":
            raise _error("derived field operation must be checked_sum")
        if type(self.source_sdk_paths) is not tuple:
            raise _error("checked_sum source SDK paths must be an immutable tuple")
        if len(self.source_sdk_paths) < 2 or len(self.source_sdk_paths) > _MAX_FIELDS:
            raise _error("checked_sum requires bounded source SDK paths")
        for path_value in self.source_sdk_paths:
            _tuple_path(path_value, "checked_sum source SDK path")
        if len(set(self.source_sdk_paths)) != len(self.source_sdk_paths):
            raise _error("checked_sum source SDK paths must be unique")

    @classmethod
    def from_json(cls, value: object) -> UsageDerivedField:
        raw = _object(
            value,
            required=frozenset(
                {"public_path", "operation", "source_sdk_paths"}
            ),
            label="derived field",
        )
        operation = _exact_string(raw["operation"], "derived operation")
        sources = _array(
            raw["source_sdk_paths"], maximum=_MAX_FIELDS, label="checked_sum sources"
        )
        return cls(
            public_path=_path(raw["public_path"], "derived public path"),
            operation=cast(Literal["checked_sum"], operation),
            source_sdk_paths=tuple(
                _path(source, "checked_sum source SDK path") for source in sources
            ),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "public_path": _path_to_json(self.public_path),
            "operation": self.operation,
            "source_sdk_paths": [
                _path_to_json(path_value) for path_value in self.source_sdk_paths
            ],
        }

    def evaluate(
        self,
        values: Mapping[tuple[str, ...], object],
        *,
        integer_bound: int = _MAX_USAGE_INTEGER,
    ) -> int:
        """Evaluate this aggregate without coercion or unchecked arithmetic."""
        if type(values) is not dict:
            raise _error("checked_sum values must be an exact mapping")
        if (
            type(integer_bound) is not int
            or integer_bound <= 0
            or integer_bound > _MAX_USAGE_INTEGER
        ):
            raise _error("checked_sum integer bound is invalid")
        total = 0
        for source_path in self.source_sdk_paths:
            if source_path not in values:
                raise _error("checked_sum source is missing")
            source = values[source_path]
            if type(source) is not int or source < 0:
                raise _error("checked_sum source must be a nonnegative integer")
            if source > integer_bound - total:
                raise _error("checked_sum exceeds its configured integer bound")
            total += source
        return total


def _mapping_projection(
    dialect: UsageDialect,
    identity_bindings: tuple[UsageIdentityBinding, ...],
    derived_fields: tuple[UsageDerivedField, ...],
    passed: bool,
    failure_reasons: tuple[UsageMappingFailure, ...],
) -> dict[str, object]:
    return {
        "dialect": dialect.value,
        "identity_bindings": [binding.to_json() for binding in identity_bindings],
        "derived_fields": [field.to_json() for field in derived_fields],
        "passed": passed,
        "failure_reasons": [reason.value for reason in failure_reasons],
    }


@dataclass(frozen=True, slots=True)
class DialectUsageMapping:
    dialect: UsageDialect
    identity_bindings: tuple[UsageIdentityBinding, ...]
    derived_fields: tuple[UsageDerivedField, ...]
    passed: bool
    failure_reasons: tuple[UsageMappingFailure, ...]
    mapping_digest: str

    def __post_init__(self) -> None:
        if type(self.dialect) is not UsageDialect:
            raise _error("usage dialect is not supported")
        if type(self.passed) is not bool:
            raise _error("mapping passed must be a boolean")
        if type(self.identity_bindings) is not tuple:
            raise _error("identity bindings must be an immutable tuple")
        if type(self.derived_fields) is not tuple:
            raise _error("derived fields must be an immutable tuple")
        if type(self.failure_reasons) is not tuple:
            raise _error("mapping failure reasons must be an immutable tuple")
        if type(self.mapping_digest) is not str:
            raise _error("mapping digest must be exact text")
        if len(self.identity_bindings) > _MAX_BINDINGS:
            raise _error("identity bindings exceed their item bound")
        if len(self.derived_fields) > _MAX_BINDINGS:
            raise _error("derived fields exceed their item bound")
        if any(
            type(item) is not UsageIdentityBinding
            for item in self.identity_bindings
        ):
            raise _error("identity binding type is invalid")
        if any(type(item) is not UsageDerivedField for item in self.derived_fields):
            raise _error("derived field type is invalid")
        if any(type(item) is not UsageMappingFailure for item in self.failure_reasons):
            raise _error("mapping failure reason is invalid")
        if tuple(sorted(set(self.failure_reasons), key=lambda item: item.value)) != (
            self.failure_reasons
        ):
            raise _error("mapping failure reasons must be sorted and unique")
        sdk_paths = tuple(binding.sdk_path for binding in self.identity_bindings)
        public_paths = tuple(
            binding.public_path for binding in self.identity_bindings
        ) + tuple(field.public_path for field in self.derived_fields)
        if len(set(sdk_paths)) != len(sdk_paths) or _has_path_collision(public_paths):
            raise _error("mapping path collision")
        if self.passed:
            if self.failure_reasons:
                raise _error("passing mapping cannot have failure reasons")
        elif self.identity_bindings or self.derived_fields or not self.failure_reasons:
            raise _error("false mapping cannot retain usable bindings")
        projection = _mapping_projection(
            self.dialect,
            self.identity_bindings,
            self.derived_fields,
            self.passed,
            self.failure_reasons,
        )
        expected = _digest(b"quaylet:usage-mapping:v1", projection)
        if self.mapping_digest != expected:
            raise _error("mapping digest mismatch")

    @classmethod
    def from_json(cls, value: object) -> DialectUsageMapping:
        raw = _object(
            value,
            required=frozenset(
                {
                    "dialect",
                    "identity_bindings",
                    "derived_fields",
                    "passed",
                    "failure_reasons",
                }
            ),
            optional=frozenset({"mapping_digest"}),
            label="dialect usage mapping",
        )
        dialect = _enum_value(UsageDialect, raw["dialect"], "usage dialect")
        identity_bindings = tuple(
            sorted(
                (
                    UsageIdentityBinding.from_json(item)
                    for item in _array(
                        raw["identity_bindings"],
                        maximum=_MAX_BINDINGS,
                        label="identity bindings",
                    )
                ),
                key=lambda item: (item.sdk_path, item.public_path),
            )
        )
        derived_fields = tuple(
            sorted(
                (
                    UsageDerivedField.from_json(item)
                    for item in _array(
                        raw["derived_fields"],
                        maximum=_MAX_BINDINGS,
                        label="derived fields",
                    )
                ),
                key=lambda item: (item.public_path, item.source_sdk_paths),
            )
        )
        passed = _exact_bool(raw["passed"], "mapping passed")
        parsed_reasons = tuple(
            _enum_value(UsageMappingFailure, item, "mapping failure reason")
            for item in _array(
                raw["failure_reasons"],
                maximum=len(UsageMappingFailure),
                label="mapping failure reasons",
            )
        )
        if len(set(parsed_reasons)) != len(parsed_reasons):
            raise _error("mapping failure reasons must be sorted and unique")
        reasons = tuple(sorted(parsed_reasons, key=lambda item: item.value))
        projection = _mapping_projection(
            dialect, identity_bindings, derived_fields, passed, reasons
        )
        expected = _digest(b"quaylet:usage-mapping:v1", projection)
        return cls(
            dialect=dialect,
            identity_bindings=identity_bindings,
            derived_fields=derived_fields,
            passed=passed,
            failure_reasons=reasons,
            mapping_digest=(
                expected
                if "mapping_digest" not in raw
                else _supplied_digest(raw["mapping_digest"], expected, "mapping")
            ),
        )

    def to_json(self) -> dict[str, object]:
        return {
            **_mapping_projection(
                self.dialect,
                self.identity_bindings,
                self.derived_fields,
                self.passed,
                self.failure_reasons,
            ),
            "mapping_digest": self.mapping_digest,
        }


_ANTHROPIC_PUBLIC_TYPES: Final[
    dict[tuple[str, ...], tuple[UsageScalarKind, bool]]
] = {
    ("input_tokens",): (UsageScalarKind.NONNEGATIVE_INTEGER, False),
    ("output_tokens",): (UsageScalarKind.NONNEGATIVE_INTEGER, False),
    ("cache_creation_input_tokens",): (
        UsageScalarKind.NONNEGATIVE_INTEGER,
        False,
    ),
    ("cache_read_input_tokens",): (UsageScalarKind.NONNEGATIVE_INTEGER, False),
    ("cache_creation", "ephemeral_1h_input_tokens"): (
        UsageScalarKind.NONNEGATIVE_INTEGER,
        False,
    ),
    ("cache_creation", "ephemeral_5m_input_tokens"): (
        UsageScalarKind.NONNEGATIVE_INTEGER,
        False,
    ),
    ("server_tool_use", "web_search_requests"): (
        UsageScalarKind.NONNEGATIVE_INTEGER,
        False,
    ),
    ("service_tier",): (UsageScalarKind.STRING, False),
}
_OPENAI_PUBLIC_TYPES: Final[
    dict[tuple[str, ...], tuple[UsageScalarKind, bool]]
] = {
    ("prompt_tokens",): (UsageScalarKind.NONNEGATIVE_INTEGER, False),
    ("completion_tokens",): (UsageScalarKind.NONNEGATIVE_INTEGER, False),
    ("total_tokens",): (UsageScalarKind.NONNEGATIVE_INTEGER, False),
    ("prompt_tokens_details", "cached_tokens"): (
        UsageScalarKind.NONNEGATIVE_INTEGER,
        False,
    ),
    ("prompt_tokens_details", "audio_tokens"): (
        UsageScalarKind.NONNEGATIVE_INTEGER,
        False,
    ),
    ("completion_tokens_details", "reasoning_tokens"): (
        UsageScalarKind.NONNEGATIVE_INTEGER,
        False,
    ),
    ("completion_tokens_details", "audio_tokens"): (
        UsageScalarKind.NONNEGATIVE_INTEGER,
        False,
    ),
    ("completion_tokens_details", "accepted_prediction_tokens"): (
        UsageScalarKind.NONNEGATIVE_INTEGER,
        False,
    ),
    ("completion_tokens_details", "rejected_prediction_tokens"): (
        UsageScalarKind.NONNEGATIVE_INTEGER,
        False,
    ),
}
_OPENAI_SEMANTIC_BINDINGS: Final[
    dict[tuple[str, ...], tuple[str, ...]]
] = {
    ("input_tokens",): ("prompt_tokens",),
    ("output_tokens",): ("completion_tokens",),
    ("cache_read_input_tokens",): ("prompt_tokens_details", "cached_tokens"),
    ("total_tokens",): ("total_tokens",),
}


def _validate_checked_sum(
    fields_by_path: dict[tuple[str, ...], UsageFieldSpec],
    derived: UsageDerivedField,
) -> None:
    if derived.public_path != ("total_tokens",) or derived.source_sdk_paths != (
        ("input_tokens",),
        ("output_tokens",),
    ):
        raise _error("checked_sum derivation is not the exact OpenAI total")
    for source_path in derived.source_sdk_paths:
        source = fields_by_path.get(source_path)
        if (
            source is None
            or not source.required
            or source.nullable
            or source.kind is not UsageScalarKind.NONNEGATIVE_INTEGER
        ):
            raise _error(
                "checked_sum sources must be required nonnullable integer leaves"
            )


def _validate_public_mapping(
    fields: tuple[UsageFieldSpec, ...], mapping: DialectUsageMapping
) -> None:
    fields_by_path = {field.sdk_path: field for field in fields}
    if {binding.sdk_path for binding in mapping.identity_bindings} != set(
        fields_by_path
    ):
        raise _error("passing mapping must identity-bind every SDK leaf exactly once")
    public_types = (
        _ANTHROPIC_PUBLIC_TYPES
        if mapping.dialect is UsageDialect.ANTHROPIC
        else _OPENAI_PUBLIC_TYPES
    )
    if mapping.dialect is UsageDialect.ANTHROPIC and mapping.derived_fields:
        raise _error("Anthropic mapping has an invalid derivation")
    if mapping.dialect is UsageDialect.OPENAI:
        has_sdk_total = ("total_tokens",) in fields_by_path
        if has_sdk_total:
            if mapping.derived_fields:
                raise _error("SDK total cannot coexist with a derived total")
        else:
            if len(mapping.derived_fields) != 1:
                raise _error("OpenAI mapping requires exactly one total derivation")
            _validate_checked_sum(fields_by_path, mapping.derived_fields[0])

    for binding in mapping.identity_bindings:
        public_spec = public_types.get(binding.public_path)
        if public_spec is None:
            raise _error("mapping contains an illegal public path")
        field = fields_by_path[binding.sdk_path]
        public_kind, public_nullable = public_spec
        if field.kind is not public_kind:
            raise _error("semantic identity binding cannot coerce scalar kinds")
        if field.nullable and not public_nullable:
            raise _error("mapping has a nullability mismatch")
        if mapping.dialect is UsageDialect.ANTHROPIC:
            if binding.sdk_path != binding.public_path:
                raise _error("Anthropic semantic identity is not proven")
        elif _OPENAI_SEMANTIC_BINDINGS.get(binding.sdk_path) != binding.public_path:
            raise _error("OpenAI semantic identity is not proven")


def _row_projection(
    key: UsageTupleKey,
    fields: tuple[UsageFieldSpec, ...],
    sdk_shape_passed: bool,
    mappings: tuple[DialectUsageMapping, ...],
) -> dict[str, object]:
    return {
        "key": key.to_json(),
        "fields": [field.to_json() for field in fields],
        "sdk_shape_passed": sdk_shape_passed,
        "dialect_mappings": [mapping.to_json() for mapping in mappings],
    }


@dataclass(frozen=True, slots=True)
class UsageEvidenceRow:
    key: UsageTupleKey
    fields: tuple[UsageFieldSpec, ...]
    sdk_shape_passed: bool
    dialect_mappings: tuple[DialectUsageMapping, ...]
    row_digest: str

    def __post_init__(self) -> None:
        if type(self.key) is not UsageTupleKey:
            raise _error("usage row key type is invalid")
        if type(self.sdk_shape_passed) is not bool:
            raise _error("sdk_shape_passed must be a boolean")
        if type(self.fields) is not tuple:
            raise _error("usage fields must be an immutable tuple")
        if type(self.dialect_mappings) is not tuple:
            raise _error("dialect mappings must be an immutable tuple")
        if type(self.row_digest) is not str:
            raise _error("row digest must be exact text")
        if not self.sdk_shape_passed and self.fields:
            raise _error("failed SDK shape cannot retain fields")
        if self.sdk_shape_passed and not self.fields:
            raise _error("passing SDK shape must contain fields")
        if len(self.fields) > _MAX_FIELDS:
            raise _error("usage fields exceed their item bound")
        if any(type(field) is not UsageFieldSpec for field in self.fields):
            raise _error("usage field type is invalid")
        field_paths = tuple(field.sdk_path for field in self.fields)
        if len(set(field_paths)) != len(field_paths) or _has_path_collision(
            field_paths
        ):
            raise _error("SDK field path collision")
        if tuple(mapping.dialect for mapping in self.dialect_mappings) != (
            UsageDialect.ANTHROPIC,
            UsageDialect.OPENAI,
        ):
            raise _error("usage row must contain exactly both dialect mappings")
        for mapping in self.dialect_mappings:
            if not self.sdk_shape_passed:
                if (
                    mapping.passed
                    or UsageMappingFailure.SDK_SHAPE_UNSTABLE
                    not in mapping.failure_reasons
                ):
                    raise _error("unstable SDK shape requires false dialect mappings")
            elif mapping.passed:
                _validate_public_mapping(self.fields, mapping)
        expected = _digest(
            b"quaylet:usage-row:v1",
            _row_projection(
                self.key,
                self.fields,
                self.sdk_shape_passed,
                self.dialect_mappings,
            ),
        )
        if self.row_digest != expected:
            raise _error("row digest mismatch")

    @classmethod
    def from_json(cls, value: object) -> UsageEvidenceRow:
        raw = _object(
            value,
            required=frozenset(
                {"key", "fields", "sdk_shape_passed", "dialect_mappings"}
            ),
            optional=frozenset({"row_digest"}),
            label="usage evidence row",
        )
        key = UsageTupleKey.from_json(raw["key"])
        fields = tuple(
            sorted(
                (
                    UsageFieldSpec.from_json(item)
                    for item in _array(
                        raw["fields"], maximum=_MAX_FIELDS, label="usage fields"
                    )
                ),
                key=lambda item: item.sdk_path,
            )
        )
        sdk_shape_passed = _exact_bool(
            raw["sdk_shape_passed"], "sdk_shape_passed"
        )
        mappings = tuple(
            sorted(
                (
                    DialectUsageMapping.from_json(item)
                    for item in _array(
                        raw["dialect_mappings"],
                        maximum=len(UsageDialect),
                        label="dialect mappings",
                    )
                ),
                key=lambda item: item.dialect.value,
            )
        )
        projection = _row_projection(key, fields, sdk_shape_passed, mappings)
        expected = _digest(b"quaylet:usage-row:v1", projection)
        return cls(
            key=key,
            fields=fields,
            sdk_shape_passed=sdk_shape_passed,
            dialect_mappings=mappings,
            row_digest=(
                expected
                if "row_digest" not in raw
                else _supplied_digest(raw["row_digest"], expected, "row")
            ),
        )

    def to_json(self) -> dict[str, object]:
        return {
            **_row_projection(
                self.key,
                self.fields,
                self.sdk_shape_passed,
                self.dialect_mappings,
            ),
            "row_digest": self.row_digest,
        }


def _row_sort_key(row: UsageEvidenceRow) -> tuple[object, ...]:
    key = row.key
    return (
        key.runtime_digest,
        key.backend_model_id,
        key.thinking_mode,
        key.effort is not None,
        key.effort or "",
        key.budget_tokens is not None,
        key.budget_tokens or 0,
        key.operation_class.value,
    )


def _schema_projection(rows: tuple[UsageEvidenceRow, ...]) -> dict[str, object]:
    return {"schema_version": 1, "rows": [row.to_json() for row in rows]}


@dataclass(frozen=True, slots=True)
class UsageEvidenceSchema:
    schema_version: Literal[1]
    rows: tuple[UsageEvidenceRow, ...]
    schema_digest: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise _error("schema_version must be exact integer 1")
        if type(self.rows) is not tuple:
            raise _error("usage schema rows must be an immutable tuple")
        if type(self.schema_digest) is not str:
            raise _error("schema digest must be exact text")
        if not self.rows or len(self.rows) > _MAX_ROWS:
            raise _error("usage schema rows are outside their item bound")
        if any(type(row) is not UsageEvidenceRow for row in self.rows):
            raise _error("usage schema row type is invalid")
        keys = tuple(row.key for row in self.rows)
        if len(set(keys)) != len(keys):
            raise _error("usage schema contains a duplicate exact usage tuple")
        expected = _digest(
            b"quaylet:usage-schema:v1", _schema_projection(self.rows)
        )
        if self.schema_digest != expected:
            raise _error("schema digest mismatch")

    @classmethod
    def from_json(cls, value: object) -> UsageEvidenceSchema:
        if type(value) is not dict:
            raise _error("usage evidence must be an exact JSON object")
        _validate_json_tree(value)
        raw = _object(
            value,
            required=frozenset({"schema_version", "rows"}),
            optional=frozenset({"schema_digest"}),
            label="usage evidence schema",
        )
        if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
            raise _error("schema_version must be exact integer 1")
        rows = tuple(
            sorted(
                (
                    UsageEvidenceRow.from_json(item)
                    for item in _array(
                        raw["rows"], maximum=_MAX_ROWS, label="usage schema rows"
                    )
                ),
                key=_row_sort_key,
            )
        )
        expected = _digest(
            b"quaylet:usage-schema:v1", _schema_projection(rows)
        )
        return cls(
            schema_version=1,
            rows=rows,
            schema_digest=(
                expected
                if "schema_digest" not in raw
                else _supplied_digest(raw["schema_digest"], expected, "schema")
            ),
        )

    def to_json(self) -> dict[str, object]:
        return {
            **_schema_projection(self.rows),
            "schema_digest": self.schema_digest,
        }

    def require_mapping(
        self, key: UsageTupleKey, dialect: UsageDialect
    ) -> tuple[UsageEvidenceRow, DialectUsageMapping]:
        if type(key) is not UsageTupleKey or type(dialect) is not UsageDialect:
            raise _error("exact usage tuple and dialect types are required")
        for row in self.rows:
            if row.key == key:
                for mapping in row.dialect_mappings:
                    if mapping.dialect is dialect:
                        if not row.sdk_shape_passed or not mapping.passed:
                            raise _error("exact usage tuple dialect mapping is false")
                        return row, mapping
                break
        raise _error("exact usage tuple dialect mapping is missing")

    def only_tool_row(self, expected_key: UsageTupleKey) -> UsageEvidenceRow:
        """Return the complete exact tool-operation tuple without fallback."""
        if type(expected_key) is not UsageTupleKey:
            raise _error("tool operation lookup requires an exact UsageTupleKey")
        if expected_key.operation_class not in {
            UsageOperationClass.TOOL_USE_BOUNDARY,
            UsageOperationClass.POST_TOOL_RESULT,
        }:
            raise _error("tool operation class must name a tool result context")
        for row in self.rows:
            if row.key == expected_key:
                return row
        raise _error("exact tool operation row is missing")


__all__ = [
    "DialectUsageMapping",
    "EvidenceSchemaError",
    "UsageDerivedField",
    "UsageDialect",
    "UsageEvidenceRow",
    "UsageEvidenceSchema",
    "UsageFieldSpec",
    "UsageIdentityBinding",
    "UsageMappingFailure",
    "UsageOperationClass",
    "UsageScalarKind",
    "UsageTupleKey",
]
