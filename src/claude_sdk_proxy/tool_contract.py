from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Protocol, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]
from jsonschema.validators import validator_for  # type: ignore[import-untyped]
from referencing import Registry, Resource
from referencing.exceptions import CannotDetermineSpecification
from referencing.jsonschema import DRAFT202012, UnknownDialect

from claude_sdk_proxy.domain import (
    RequestValidationError,
    ToolDefinition,
    ToolResultBlock,
)

type JsonValue = (
    None | bool | int | float | str | Mapping[str, JsonValue] | tuple[JsonValue, ...]
)

_MAX_DEFINITIONS = 128
_MAX_DESCRIPTION_BYTES = 8 * 1024
_MAX_SCHEMA_BYTES = 64 * 1024
_MAX_TOTAL_SCHEMA_BYTES = 512 * 1024
_MAX_ARGUMENT_BYTES = 256 * 1024
_MAX_RESULT_BYTES = 256 * 1024
_MAX_TOTAL_RESULT_BYTES = 1024 * 1024
MAX_JSON_CONTAINER_DEPTH = 64
_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_PUBLIC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_REFERENCE_KEYWORDS = ("$ref", "$dynamicRef", "$recursiveRef")
_JSON_DEPTH_REASON = "JSON nesting exceeds its depth limit"


def freeze_json(value: object, *, field: str = "tools") -> JsonValue:
    try:
        return _freeze_json(value, set(), 0, field)
    except RecursionError:
        raise RequestValidationError(field, _JSON_DEPTH_REASON) from None


def _freeze_json(
    value: object, ancestors: set[int], container_depth: int, field: str
) -> JsonValue:
    if value is None or type(value) is bool or isinstance(value, str):
        return cast(JsonValue, value)
    if type(value) is int:
        return cast(JsonValue, value)
    if type(value) is float:
        if not math.isfinite(value):
            raise RequestValidationError(field, "must contain JSON values")
        return value
    if isinstance(value, Mapping):
        return _freeze_mapping(value, ancestors, container_depth, field)
    if isinstance(value, (list, tuple)):
        return _freeze_array(value, ancestors, container_depth, field)
    raise RequestValidationError(field, "must contain JSON values")


def _freeze_mapping(
    value: Mapping[object, object],
    ancestors: set[int],
    container_depth: int,
    field: str,
) -> JsonValue:
    container_depth += 1
    if container_depth > MAX_JSON_CONTAINER_DEPTH:
        raise RequestValidationError(field, _JSON_DEPTH_REASON)
    marker = id(value)
    if marker in ancestors:
        raise RequestValidationError(field, "must contain JSON values")
    ancestors.add(marker)
    try:
        frozen: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise RequestValidationError(field, "must contain JSON values")
            frozen[key] = _freeze_json(item, ancestors, container_depth, field)
        return MappingProxyType(frozen)
    finally:
        ancestors.remove(marker)


def _freeze_array(
    value: list[object] | tuple[object, ...],
    ancestors: set[int],
    container_depth: int,
    field: str,
) -> JsonValue:
    container_depth += 1
    if container_depth > MAX_JSON_CONTAINER_DEPTH:
        raise RequestValidationError(field, _JSON_DEPTH_REASON)
    marker = id(value)
    if marker in ancestors:
        raise RequestValidationError(field, "must contain JSON values")
    ancestors.add(marker)
    try:
        return tuple(
            _freeze_json(item, ancestors, container_depth, field) for item in value
        )
    finally:
        ancestors.remove(marker)


def plain_json(value: JsonValue, *, field: str = "tools") -> object:
    try:
        return _plain_json(value, 0, field)
    except RecursionError:
        raise RequestValidationError(field, _JSON_DEPTH_REASON) from None


def _plain_json(value: JsonValue, container_depth: int, field: str) -> object:
    if isinstance(value, Mapping):
        container_depth += 1
        if container_depth > MAX_JSON_CONTAINER_DEPTH:
            raise RequestValidationError(field, _JSON_DEPTH_REASON)
        return {
            key: _plain_json(item, container_depth, field)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        container_depth += 1
        if container_depth > MAX_JSON_CONTAINER_DEPTH:
            raise RequestValidationError(field, _JSON_DEPTH_REASON)
        return [_plain_json(item, container_depth, field) for item in value]
    return value


def canonical_json(value: JsonValue, *, field: str = "tools") -> str:
    try:
        return json.dumps(
            plain_json(freeze_json(value, field=field), field=field),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except RecursionError:
        raise RequestValidationError(field, _JSON_DEPTH_REASON) from None
    except RequestValidationError:
        raise
    except TypeError, ValueError:
        raise RequestValidationError(field, "must contain JSON values") from None


def validate_tool_definitions(
    definitions: Iterable[ToolDefinition],
) -> tuple[ToolDefinition, ...]:
    normalized = tuple(definitions)
    if len(normalized) > _MAX_DEFINITIONS:
        raise RequestValidationError("tools", "contains too many definitions")
    names: set[str] = set()
    total_schema_bytes = 0
    validated: list[ToolDefinition] = []
    for definition in normalized:
        if not isinstance(definition, ToolDefinition):
            raise RequestValidationError("tools", "definitions must be objects")
        if (
            not isinstance(definition.name, str)
            or _TOOL_NAME.fullmatch(definition.name) is None
        ):
            raise RequestValidationError("tools", "tool name is invalid")
        if definition.name in names:
            raise RequestValidationError("tools", "tool names must be unique")
        names.add(definition.name)
        if not isinstance(definition.description, str):
            raise RequestValidationError("tools", "description must be a string")
        if len(definition.description.encode()) > _MAX_DESCRIPTION_BYTES:
            raise RequestValidationError("tools", "description exceeds its size limit")
        if not isinstance(definition.input_schema, Mapping):
            raise RequestValidationError("tools", "input schema must be an object")
        frozen_schema = freeze_json(definition.input_schema)
        if not isinstance(frozen_schema, Mapping):
            raise RequestValidationError("tools", "input schema must be an object")
        encoded_schema = canonical_json(frozen_schema).encode()
        if len(encoded_schema) > _MAX_SCHEMA_BYTES:
            raise RequestValidationError("tools", "input schema exceeds its size limit")
        total_schema_bytes += len(encoded_schema)
        if total_schema_bytes > _MAX_TOTAL_SCHEMA_BYTES:
            raise RequestValidationError(
                "tools", "input schemas exceed their size limit"
            )
        _validate_schema(frozen_schema)
        validated.append(
            ToolDefinition(
                definition.name,
                definition.description,
                cast(Mapping[str, object], frozen_schema),
            )
        )
    return tuple(sorted(validated, key=lambda item: item.name))


def validate_tool_arguments(
    value: object, *, field: str = "tools"
) -> Mapping[str, JsonValue]:
    frozen = freeze_json(value, field=field)
    if not isinstance(frozen, Mapping):
        raise RequestValidationError(field, "arguments must be an object")
    if len(canonical_json(frozen, field=field).encode()) > _MAX_ARGUMENT_BYTES:
        raise RequestValidationError(field, "arguments exceed their size limit")
    return frozen


def validate_tool_results(
    results: Iterable[ToolResultBlock],
) -> tuple[ToolResultBlock, ...]:
    normalized = tuple(results)
    ids: set[str] = set()
    total_bytes = 0
    validated: list[ToolResultBlock] = []
    for result in normalized:
        if not isinstance(result, ToolResultBlock):
            raise RequestValidationError("messages", "tool results must be blocks")
        if (
            not isinstance(result.tool_call_id, str)
            or _PUBLIC_ID.fullmatch(result.tool_call_id) is None
        ):
            raise RequestValidationError("messages", "tool result ID is invalid")
        if result.tool_call_id in ids:
            raise RequestValidationError("messages", "tool result IDs must be unique")
        ids.add(result.tool_call_id)
        if type(result.is_error) is not bool:
            raise RequestValidationError(
                "messages", "tool result error flag must be boolean"
            )
        if not isinstance(result.content, tuple) or any(
            not isinstance(item, str) for item in result.content
        ):
            raise RequestValidationError("messages", "tool result content must be text")
        text = "".join(result.content)
        encoded = text.encode()
        if result.is_error and not encoded:
            raise RequestValidationError("messages", "error result must not be empty")
        if len(encoded) > _MAX_RESULT_BYTES:
            raise RequestValidationError(
                "messages", "tool result exceeds its size limit"
            )
        total_bytes += len(encoded)
        if total_bytes > _MAX_TOTAL_RESULT_BYTES:
            raise RequestValidationError(
                "messages", "tool results exceed their size limit"
            )
        validated.append(result)
    return tuple(sorted(validated, key=lambda item: item.tool_call_id))


def _validate_schema(schema: Mapping[str, JsonValue]) -> None:
    try:
        plain = cast(Mapping[str, object], plain_json(schema))
        validator = validator_for(plain, default=Draft202012Validator)
        validator.check_schema(plain)
    except SchemaError as error:
        raise RequestValidationError("tools", "input schema is invalid") from error
    except RecursionError:
        raise RequestValidationError("tools", _JSON_DEPTH_REASON) from None
    try:
        resource = (
            Resource.from_contents(plain)
            if "$schema" in plain
            else Resource.from_contents(plain, default_specification=DRAFT202012)
        )
        registry = Registry(retrieve=_no_retrieve).with_resource("", resource).crawl()  # type: ignore[call-arg]
    except (CannotDetermineSpecification, UnknownDialect) as error:
        raise RequestValidationError(
            "tools", "input schema dialect is invalid"
        ) from error
    except RecursionError:
        raise RequestValidationError("tools", _JSON_DEPTH_REASON) from None
    try:
        _walk_schema_references(resource, registry.resolver(resource.id() or ""))
    except RecursionError:
        raise RequestValidationError("tools", _JSON_DEPTH_REASON) from None


def _no_retrieve(uri: str) -> Resource[object]:
    raise RuntimeError(f"retrieval is disabled for {uri}")


class Resolver(Protocol):
    def lookup(self, ref: str) -> object: ...
    def in_subresource(self, resource: Resource[object]) -> Resolver: ...


def _walk_schema_references(resource: Resource[object], resolver: Resolver) -> None:
    contents = resource.contents
    if not isinstance(contents, Mapping):
        return
    for keyword in _REFERENCE_KEYWORDS:
        reference = contents.get(keyword)
        if reference is None:
            continue
        if not isinstance(reference, str) or not reference.startswith("#"):
            raise RequestValidationError("tools", "schema references must be fragments")
        try:
            resolver.lookup(reference)
        except RecursionError:
            raise RequestValidationError("tools", _JSON_DEPTH_REASON) from None
        except Exception:
            raise RequestValidationError(
                "tools", "schema reference is unresolved"
            ) from None
    for child_resource in resource.subresources():
        _walk_schema_references(
            child_resource,
            resolver.in_subresource(child_resource),
        )


__all__ = [
    "JsonValue",
    "MAX_JSON_CONTAINER_DEPTH",
    "canonical_json",
    "freeze_json",
    "plain_json",
    "validate_tool_arguments",
    "validate_tool_definitions",
    "validate_tool_results",
]
