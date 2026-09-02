"""Strict, content-free, exact-tuple usage-evidence contracts."""

from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping
from dataclasses import replace

import pytest

from claude_sdk_proxy.usage_evidence import (
    EvidenceSchemaError,
    UsageDerivedField,
    UsageDialect,
    UsageEvidenceSchema,
    UsageFieldSpec,
    UsageIdentityBinding,
    UsageMappingFailure,
    UsageOperationClass,
    UsageScalarKind,
)


def _valid_usage_row_json() -> dict[str, object]:
    return {
        "key": {
            "runtime_digest": "11" * 32,
            "backend_model_id": "claude-sonnet-4-5-20250929",
            "thinking_mode": "null",
            "effort": None,
            "budget_tokens": None,
            "operation_class": "ordinary",
        },
        "fields": [
            {
                "sdk_path": ["input_tokens"],
                "kind": "nonnegative_integer",
                "required": True,
                "nullable": False,
            },
            {
                "sdk_path": ["output_tokens"],
                "kind": "nonnegative_integer",
                "required": True,
                "nullable": False,
            },
        ],
        "sdk_shape_passed": True,
        "dialect_mappings": [
            {
                "dialect": "anthropic",
                "identity_bindings": [
                    {
                        "sdk_path": ["input_tokens"],
                        "public_path": ["input_tokens"],
                    },
                    {
                        "sdk_path": ["output_tokens"],
                        "public_path": ["output_tokens"],
                    },
                ],
                "derived_fields": [],
                "passed": True,
                "failure_reasons": [],
            },
            {
                "dialect": "openai",
                "identity_bindings": [
                    {
                        "sdk_path": ["input_tokens"],
                        "public_path": ["prompt_tokens"],
                    },
                    {
                        "sdk_path": ["output_tokens"],
                        "public_path": ["completion_tokens"],
                    },
                ],
                "derived_fields": [
                    {
                        "public_path": ["total_tokens"],
                        "operation": "checked_sum",
                        "source_sdk_paths": [
                            ["input_tokens"],
                            ["output_tokens"],
                        ],
                    }
                ],
                "passed": True,
                "failure_reasons": [],
            },
        ],
    }


@pytest.fixture
def valid_usage_row_json() -> dict[str, object]:
    return _valid_usage_row_json()


def _load(*rows: dict[str, object]) -> UsageEvidenceSchema:
    return UsageEvidenceSchema.from_json({"schema_version": 1, "rows": list(rows)})


def _mapping(row: dict[str, object], dialect: str) -> dict[str, object]:
    mappings = row["dialect_mappings"]
    assert isinstance(mappings, list)
    return next(
        mapping
        for mapping in mappings
        if isinstance(mapping, dict) and mapping["dialect"] == dialect
    )


def _false_mapping(row: dict[str, object], dialect: str, reason: str) -> None:
    mapping = _mapping(row, dialect)
    mapping["identity_bindings"] = []
    mapping["derived_fields"] = []
    mapping["passed"] = False
    mapping["failure_reasons"] = [reason]


def test_usage_tuple_budget_is_exact_and_round_trips(
    valid_usage_row_json: dict[str, object],
) -> None:
    enabled = {
        **valid_usage_row_json,
        "key": {
            **valid_usage_row_json["key"],  # type: ignore[dict-item]
            "thinking_mode": "enabled",
            "budget_tokens": 4096,
        },
    }
    row = _load(enabled).rows[0]
    assert row.key.budget_tokens == 4096
    assert row.to_json()["key"]["budget_tokens"] == 4096  # type: ignore[index]

    changed_json = copy.deepcopy(enabled)
    assert isinstance(changed_json["key"], dict)
    changed_json["key"]["budget_tokens"] = 4097
    changed = _load(changed_json).rows[0]
    assert changed.key != row.key
    assert changed.row_digest != row.row_digest


def test_usage_tuple_rejects_noncanonical_budget_identity(
    valid_usage_row_json: dict[str, object],
) -> None:
    source_key = valid_usage_row_json["key"]
    assert isinstance(source_key, dict)
    for key in (
        {**source_key, "thinking_mode": "null", "budget_tokens": 4096},
        {**source_key, "thinking_mode": "null", "effort": "high"},
        {**source_key, "thinking_mode": "enabled", "budget_tokens": None},
        {**source_key, "thinking_mode": "enabled", "budget_tokens": 0},
        {**source_key, "thinking_mode": "enabled", "budget_tokens": True},
        {**source_key, "thinking_mode": "adaptive", "budget_tokens": 4096},
        {**source_key, "budget_class": "tokens:4096"},
    ):
        row = {**valid_usage_row_json, "key": key}
        with pytest.raises(
            EvidenceSchemaError,
            match="thinking identity|budget_tokens|effort|unknown field",
        ):
            _load(row)


def test_usage_tuple_rejects_an_older_style_moving_model_alias(
    valid_usage_row_json: dict[str, object],
) -> None:
    row = copy.deepcopy(valid_usage_row_json)
    assert isinstance(row["key"], dict)
    row["key"]["backend_model_id"] = "claude-3-5-sonnet"
    with pytest.raises(EvidenceSchemaError, match="moving alias"):
        _load(row)


def test_direct_leaf_types_reject_mutable_path_aliases() -> None:
    with pytest.raises(EvidenceSchemaError, match="tuple"):
        UsageFieldSpec(
            sdk_path=["input_tokens"],  # type: ignore[arg-type]
            kind=UsageScalarKind.NONNEGATIVE_INTEGER,
            required=True,
            nullable=False,
        )
    with pytest.raises(EvidenceSchemaError, match="tuple"):
        UsageIdentityBinding(
            sdk_path=("input_tokens",),
            public_path=["input_tokens"],  # type: ignore[arg-type]
        )
    with pytest.raises(EvidenceSchemaError, match="tuple"):
        UsageDerivedField(
            public_path=("total_tokens",),
            operation="checked_sum",
            source_sdk_paths=[  # type: ignore[arg-type]
                ("input_tokens",),
                ("output_tokens",),
            ],
        )


def test_frozen_aggregate_types_reject_mutable_container_aliases(
    valid_usage_row_json: dict[str, object],
) -> None:
    schema = _load(valid_usage_row_json)
    row = schema.rows[0]
    mapping = row.dialect_mappings[0]
    for constructor in (
        lambda: replace(schema, rows=list(schema.rows)),  # type: ignore[arg-type]
        lambda: replace(row, fields=list(row.fields)),  # type: ignore[arg-type]
        lambda: replace(
            mapping,
            identity_bindings=list(mapping.identity_bindings),  # type: ignore[arg-type]
        ),
    ):
        with pytest.raises(EvidenceSchemaError, match="tuple"):
            constructor()


def test_round_trip_recomputes_and_verifies_every_digest(
    valid_usage_row_json: dict[str, object],
) -> None:
    schema = _load(valid_usage_row_json)
    encoded = schema.to_json()
    reloaded = UsageEvidenceSchema.from_json(encoded)

    assert reloaded == schema
    assert len(schema.schema_digest) == 64
    assert all(len(row.row_digest) == 64 for row in schema.rows)
    assert all(
        len(mapping.mapping_digest) == 64
        for row in schema.rows
        for mapping in row.dialect_mappings
    )

    for digest_path in ("schema", "row", "mapping"):
        forged = copy.deepcopy(encoded)
        if digest_path == "schema":
            forged["schema_digest"] = "00" * 32
        elif digest_path == "row":
            forged["rows"][0]["row_digest"] = "00" * 32  # type: ignore[index]
        else:
            forged["rows"][0]["dialect_mappings"][0]["mapping_digest"] = (  # type: ignore[index]
                "00" * 32
            )
        with pytest.raises(EvidenceSchemaError, match="digest"):
            UsageEvidenceSchema.from_json(forged)


def test_explicit_null_digests_are_not_treated_as_absent(
    valid_usage_row_json: dict[str, object],
) -> None:
    encoded = _load(valid_usage_row_json).to_json()
    for digest_path in ("schema", "row", "mapping"):
        invalid = copy.deepcopy(encoded)
        if digest_path == "schema":
            invalid["schema_digest"] = None
        elif digest_path == "row":
            invalid["rows"][0]["row_digest"] = None  # type: ignore[index]
        else:
            invalid["rows"][0]["dialect_mappings"][0]["mapping_digest"] = None  # type: ignore[index]
        with pytest.raises(EvidenceSchemaError, match="digest"):
            UsageEvidenceSchema.from_json(invalid)


def test_reordered_input_has_stable_canonical_order_and_digests(
    valid_usage_row_json: dict[str, object],
) -> None:
    original = _load(valid_usage_row_json)
    reordered = copy.deepcopy(valid_usage_row_json)
    reordered["fields"] = list(reversed(reordered["fields"]))  # type: ignore[arg-type]
    reordered["dialect_mappings"] = list(  # type: ignore[arg-type]
        reversed(reordered["dialect_mappings"])
    )
    for mapping in reordered["dialect_mappings"]:  # type: ignore[union-attr]
        assert isinstance(mapping, dict)
        mapping["identity_bindings"] = list(
            reversed(mapping["identity_bindings"])  # type: ignore[arg-type]
        )

    assert _load(reordered) == original


def test_require_mapping_is_exact_and_false_or_missing_never_falls_back(
    valid_usage_row_json: dict[str, object],
) -> None:
    null_row = copy.deepcopy(valid_usage_row_json)
    enabled_row = copy.deepcopy(valid_usage_row_json)
    assert isinstance(enabled_row["key"], dict)
    enabled_row["key"].update(
        thinking_mode="enabled", effort="high", budget_tokens=4096
    )
    _false_mapping(enabled_row, "openai", "unrepresentable_sdk_field")
    schema = _load(null_row, enabled_row)

    row = next(row for row in schema.rows if row.key.budget_tokens == 4096)
    assert schema.require_mapping(row.key, UsageDialect.ANTHROPIC)[0] == row
    with pytest.raises(EvidenceSchemaError, match="mapping is false"):
        schema.require_mapping(row.key, UsageDialect.OPENAI)
    with pytest.raises(EvidenceSchemaError, match="exact usage tuple"):
        schema.require_mapping(
            type(row.key)(
                runtime_digest=row.key.runtime_digest,
                backend_model_id=row.key.backend_model_id,
                thinking_mode="enabled",
                effort="high",
                budget_tokens=4097,
                operation_class=UsageOperationClass.ORDINARY,
            ),
            UsageDialect.ANTHROPIC,
        )


@pytest.mark.parametrize(
    "mutation",
    ["duplicate_sdk", "sdk_prefix", "duplicate_public", "public_prefix"],
)
def test_duplicate_and_prefix_colliding_paths_are_rejected(
    valid_usage_row_json: dict[str, object], mutation: str
) -> None:
    row = copy.deepcopy(valid_usage_row_json)
    fields = row["fields"]
    assert isinstance(fields, list)
    anthropic = _mapping(row, "anthropic")
    bindings = anthropic["identity_bindings"]
    assert isinstance(bindings, list)
    if mutation == "duplicate_sdk":
        fields.append(copy.deepcopy(fields[0]))
    elif mutation == "sdk_prefix":
        fields.append(
            {
                "sdk_path": ["input_tokens", "detail"],
                "kind": "nonnegative_integer",
                "required": True,
                "nullable": False,
            }
        )
    elif mutation == "duplicate_public":
        assert isinstance(bindings[1], dict)
        bindings[1]["public_path"] = ["input_tokens"]
    else:
        assert isinstance(bindings[1], dict)
        bindings[1]["public_path"] = ["input_tokens", "detail"]

    with pytest.raises(EvidenceSchemaError, match="path collision"):
        _load(row)


def test_passing_mapping_must_be_exhaustive_and_failure_rows_are_unusable(
    valid_usage_row_json: dict[str, object],
) -> None:
    missing = copy.deepcopy(valid_usage_row_json)
    anthropic = _mapping(missing, "anthropic")
    anthropic["identity_bindings"] = anthropic["identity_bindings"][:-1]  # type: ignore[index]
    with pytest.raises(EvidenceSchemaError, match="every SDK leaf"):
        _load(missing)

    invalid_false = copy.deepcopy(valid_usage_row_json)
    mapping = _mapping(invalid_false, "openai")
    mapping["passed"] = False
    mapping["failure_reasons"] = ["unrepresentable_sdk_field"]
    with pytest.raises(EvidenceSchemaError, match="false mapping"):
        _load(invalid_false)

    valid_false = copy.deepcopy(valid_usage_row_json)
    _false_mapping(valid_false, "openai", "unrepresentable_sdk_field")
    loaded = _load(valid_false)
    mapping_value = loaded.rows[0].dialect_mappings[1]
    assert mapping_value.passed is False
    assert mapping_value.identity_bindings == ()
    assert mapping_value.failure_reasons == (
        UsageMappingFailure.UNREPRESENTABLE_SDK_FIELD,
    )


def test_duplicate_mapping_failure_reasons_are_rejected_not_collapsed(
    valid_usage_row_json: dict[str, object],
) -> None:
    row = copy.deepcopy(valid_usage_row_json)
    _false_mapping(row, "openai", "unrepresentable_sdk_field")
    mapping = _mapping(row, "openai")
    mapping["failure_reasons"] = [
        "unrepresentable_sdk_field",
        "unrepresentable_sdk_field",
    ]

    with pytest.raises(EvidenceSchemaError, match="sorted and unique"):
        _load(row)


def test_illegal_or_semantically_unproved_public_paths_are_rejected(
    valid_usage_row_json: dict[str, object],
) -> None:
    for public_path in (
        ["future_tokens"],
        ["prompt_tokens_details", "cached_tokens"],
    ):
        row = copy.deepcopy(valid_usage_row_json)
        binding = _mapping(row, "openai")["identity_bindings"][0]  # type: ignore[index]
        assert isinstance(binding, dict)
        binding["public_path"] = public_path
        with pytest.raises(
            EvidenceSchemaError, match="illegal public path|semantic identity"
        ):
            _load(row)


def test_explicit_sdk_total_is_identity_mapped_not_derived(
    valid_usage_row_json: dict[str, object],
) -> None:
    row = copy.deepcopy(valid_usage_row_json)
    fields = row["fields"]
    assert isinstance(fields, list)
    fields.append(
        {
            "sdk_path": ["total_tokens"],
            "kind": "nonnegative_integer",
            "required": True,
            "nullable": False,
        }
    )
    _false_mapping(row, "anthropic", "unrepresentable_sdk_field")
    openai = _mapping(row, "openai")
    openai["identity_bindings"].append(  # type: ignore[union-attr]
        {"sdk_path": ["total_tokens"], "public_path": ["total_tokens"]}
    )
    openai["derived_fields"] = []

    loaded = _load(row).rows[0]
    mapping = next(
        item for item in loaded.dialect_mappings if item.dialect is UsageDialect.OPENAI
    )
    assert mapping.derived_fields == ()
    assert mapping.identity_bindings[-1].public_path == ("total_tokens",)


@pytest.mark.parametrize(
    "source_mutation", ["missing", "optional", "nullable", "string"]
)
def test_checked_sum_requires_exact_required_nonnullable_integer_sources(
    valid_usage_row_json: dict[str, object], source_mutation: str
) -> None:
    row = copy.deepcopy(valid_usage_row_json)
    fields = row["fields"]
    assert isinstance(fields, list)
    derived = _mapping(row, "openai")["derived_fields"][0]  # type: ignore[index]
    assert isinstance(derived, dict)
    if source_mutation == "missing":
        derived["source_sdk_paths"] = [["missing_tokens"], ["output_tokens"]]
    else:
        assert isinstance(fields[0], dict)
        if source_mutation == "optional":
            fields[0]["required"] = False
        elif source_mutation == "nullable":
            fields[0]["nullable"] = True
        else:
            fields[0]["kind"] = "string"
    _false_mapping(row, "anthropic", "unrepresentable_sdk_field")
    with pytest.raises(EvidenceSchemaError, match="checked_sum"):
        _load(row)


def test_checked_sum_runtime_is_exact_and_overflow_checked(
    valid_usage_row_json: dict[str, object],
) -> None:
    schema = _load(valid_usage_row_json)
    mapping = schema.require_mapping(
        schema.rows[0].key, UsageDialect.OPENAI
    )[1]
    derived = mapping.derived_fields[0]

    assert derived.evaluate(
        {("input_tokens",): 3, ("output_tokens",): 4}, integer_bound=7
    ) == 7
    for values, bound in (
        ({("input_tokens",): 3}, 7),
        ({("input_tokens",): None, ("output_tokens",): 4}, 7),
        ({("input_tokens",): True, ("output_tokens",): 4}, 7),
        ({("input_tokens",): -1, ("output_tokens",): 4}, 7),
        ({("input_tokens",): "3", ("output_tokens",): 4}, 7),
        ({("input_tokens",): 4, ("output_tokens",): 4}, 7),
        ({("input_tokens",): 3, ("output_tokens",): 4}, True),
    ):
        with pytest.raises(EvidenceSchemaError, match="checked_sum"):
            derived.evaluate(values, integer_bound=bound)  # type: ignore[arg-type]


def test_budget_token_identity_has_a_finite_integer_bound(
    valid_usage_row_json: dict[str, object],
) -> None:
    row = copy.deepcopy(valid_usage_row_json)
    assert isinstance(row["key"], dict)
    row["key"].update(
        thinking_mode="enabled", budget_tokens=2**63, effort=None
    )
    with pytest.raises(EvidenceSchemaError, match="budget_tokens"):
        _load(row)


def test_nested_sdk_leaves_and_nullable_optionals_are_explicit_not_values(
    valid_usage_row_json: dict[str, object],
) -> None:
    row = copy.deepcopy(valid_usage_row_json)
    fields = row["fields"]
    assert isinstance(fields, list)
    fields.append(
        {
            "sdk_path": ["cache_creation", "ephemeral_1h_input_tokens"],
            "kind": "nonnegative_integer",
            "required": False,
            "nullable": False,
        }
    )
    anthropic = _mapping(row, "anthropic")
    anthropic["identity_bindings"].append(  # type: ignore[union-attr]
        {
            "sdk_path": ["cache_creation", "ephemeral_1h_input_tokens"],
            "public_path": ["cache_creation", "ephemeral_1h_input_tokens"],
        }
    )
    _false_mapping(row, "openai", "unrepresentable_sdk_field")

    schema = _load(row)
    nested = schema.rows[0].fields[0]
    assert nested.sdk_path == ("cache_creation", "ephemeral_1h_input_tokens")
    assert nested.required is False
    assert nested.nullable is False

    nullable_row = copy.deepcopy(valid_usage_row_json)
    nullable_row["fields"].append(  # type: ignore[union-attr]
        {
            "sdk_path": ["observed_nullable_field"],
            "kind": "string",
            "required": False,
            "nullable": True,
        }
    )
    _false_mapping(nullable_row, "anthropic", "nullability_mismatch")
    _false_mapping(nullable_row, "openai", "unrepresentable_sdk_field")
    nullable_schema = _load(nullable_row)
    nullable = next(
        field
        for field in nullable_schema.rows[0].fields
        if field.sdk_path == ("observed_nullable_field",)
    )
    assert nullable.required is False
    assert nullable.nullable is True
    assert "value" not in repr(nullable_schema.to_json())


def test_nullable_sdk_leaf_requires_a_proved_nullable_public_leaf(
    valid_usage_row_json: dict[str, object],
) -> None:
    row = copy.deepcopy(valid_usage_row_json)
    row["fields"].append(  # type: ignore[union-attr]
        {
            "sdk_path": ["service_tier"],
            "kind": "string",
            "required": False,
            "nullable": True,
        }
    )
    _mapping(row, "anthropic")["identity_bindings"].append(  # type: ignore[union-attr]
        {"sdk_path": ["service_tier"], "public_path": ["service_tier"]}
    )
    _false_mapping(row, "openai", "unrepresentable_sdk_field")

    with pytest.raises(EvidenceSchemaError, match="nullability mismatch"):
        _load(row)


def test_unrepresentable_leaf_marks_only_the_exact_dialect_false(
    valid_usage_row_json: dict[str, object],
) -> None:
    row = copy.deepcopy(valid_usage_row_json)
    fields = row["fields"]
    assert isinstance(fields, list)
    fields.append(
        {
            "sdk_path": ["cache_creation_input_tokens"],
            "kind": "nonnegative_integer",
            "required": True,
            "nullable": False,
        }
    )
    anthropic = _mapping(row, "anthropic")
    anthropic["identity_bindings"].append(  # type: ignore[union-attr]
        {
            "sdk_path": ["cache_creation_input_tokens"],
            "public_path": ["cache_creation_input_tokens"],
        }
    )
    _false_mapping(row, "openai", "unrepresentable_sdk_field")
    schema = _load(row)

    evidence_row = schema.rows[0]
    assert schema.require_mapping(evidence_row.key, UsageDialect.ANTHROPIC)
    with pytest.raises(EvidenceSchemaError, match="mapping is false"):
        schema.require_mapping(evidence_row.key, UsageDialect.OPENAI)


def test_false_sdk_shape_is_explicit_for_both_dialects(
    valid_usage_row_json: dict[str, object],
) -> None:
    row = copy.deepcopy(valid_usage_row_json)
    row["fields"] = []
    row["sdk_shape_passed"] = False
    for dialect in ("anthropic", "openai"):
        _false_mapping(row, dialect, "sdk_shape_unstable")
    loaded = _load(row).rows[0]
    assert loaded.sdk_shape_passed is False
    assert all(not mapping.passed for mapping in loaded.dialect_mappings)


@pytest.mark.parametrize(
    ("target", "value", "message"),
    [
        ("schema_version", True, "schema_version"),
        ("sdk_shape_passed", 1, "boolean"),
        ("required", 1, "boolean"),
        ("nullable", 0, "boolean"),
        ("budget_tokens", 1.0, "budget_tokens"),
    ],
)
def test_bool_integer_and_scalar_type_confusion_is_rejected(
    valid_usage_row_json: dict[str, object],
    target: str,
    value: object,
    message: str,
) -> None:
    document: dict[str, object] = {
        "schema_version": 1,
        "rows": [copy.deepcopy(valid_usage_row_json)],
    }
    row = document["rows"][0]  # type: ignore[index]
    assert isinstance(row, dict)
    if target == "schema_version":
        document[target] = value
    elif target == "sdk_shape_passed":
        row[target] = value
    elif target in {"required", "nullable"}:
        row["fields"][0][target] = value  # type: ignore[index]
    else:
        row["key"][target] = value  # type: ignore[index]
        row["key"]["thinking_mode"] = "enabled"  # type: ignore[index]
    with pytest.raises(EvidenceSchemaError, match=message):
        UsageEvidenceSchema.from_json(document)


def test_unknown_fields_oversized_unicode_and_hostile_mappings_fail_closed(
    valid_usage_row_json: dict[str, object],
) -> None:
    unknown = {"schema_version": 1, "rows": [valid_usage_row_json], "values": [1]}
    with pytest.raises(EvidenceSchemaError, match="unknown field"):
        UsageEvidenceSchema.from_json(unknown)

    oversized = copy.deepcopy(valid_usage_row_json)
    assert isinstance(oversized["key"], dict)
    oversized["key"]["backend_model_id"] = "x" * 1025
    with pytest.raises(EvidenceSchemaError, match="string|backend model"):
        _load(oversized)

    invalid_unicode = copy.deepcopy(valid_usage_row_json)
    invalid_unicode["fields"][0]["sdk_path"] = ["bad\ud800"]  # type: ignore[index]
    with pytest.raises(EvidenceSchemaError, match="path"):
        _load(invalid_unicode)

    class HostileMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            raise AssertionError("must not inspect hostile mappings")

        def __iter__(self) -> Iterator[str]:
            raise AssertionError("must not iterate hostile mappings")

        def __len__(self) -> int:
            raise AssertionError("must not size hostile mappings")

    with pytest.raises(EvidenceSchemaError, match="JSON object"):
        UsageEvidenceSchema.from_json(HostileMapping())


def test_exact_tool_operation_rows_reuse_the_same_exhaustive_mapping_validator(
    valid_usage_row_json: dict[str, object],
) -> None:
    rows: list[dict[str, object]] = []
    for operation_class in ("tool_use_boundary", "post_tool_result"):
        row = copy.deepcopy(valid_usage_row_json)
        assert isinstance(row["key"], dict)
        row["key"]["operation_class"] = operation_class
        rows.append(row)
    schema = _load(*rows)

    for operation_class in ("tool_use_boundary", "post_tool_result"):
        row = schema.only_tool_row(operation_class)
        assert row.key.operation_class.value == operation_class
        assert row.sdk_shape_passed is True
        assert {mapping.dialect.value for mapping in row.dialect_mappings} == {
            "anthropic",
            "openai",
        }
        for mapping in row.dialect_mappings:
            if mapping.passed:
                assert {binding.sdk_path for binding in mapping.identity_bindings} == {
                    field.sdk_path for field in row.fields
                }


def test_false_tool_dialect_mapping_does_not_weaken_the_sdk_shape_or_other_dialect(
    valid_usage_row_json: dict[str, object],
) -> None:
    row = copy.deepcopy(valid_usage_row_json)
    assert isinstance(row["key"], dict)
    row["key"]["operation_class"] = "tool_use_boundary"
    _false_mapping(row, "openai", "unrepresentable_sdk_field")
    schema = _load(row)
    tool_row = schema.only_tool_row(UsageOperationClass.TOOL_USE_BOUNDARY)

    assert tool_row.sdk_shape_passed is True
    assert schema.require_mapping(tool_row.key, UsageDialect.ANTHROPIC)
    with pytest.raises(EvidenceSchemaError, match="mapping is false"):
        schema.require_mapping(tool_row.key, UsageDialect.OPENAI)


def test_only_tool_row_never_falls_back_across_process_model_or_operation(
    valid_usage_row_json: dict[str, object],
) -> None:
    boundary = copy.deepcopy(valid_usage_row_json)
    assert isinstance(boundary["key"], dict)
    boundary["key"]["operation_class"] = "tool_use_boundary"
    schema = _load(boundary)

    with pytest.raises(EvidenceSchemaError, match="tool operation row"):
        schema.only_tool_row("post_tool_result")
    with pytest.raises(EvidenceSchemaError, match="tool operation class"):
        schema.only_tool_row("ordinary")

    other_model = copy.deepcopy(boundary)
    assert isinstance(other_model["key"], dict)
    other_model["key"]["backend_model_id"] = "claude-opus-4-1-20250805"
    ambiguous = _load(boundary, other_model)
    with pytest.raises(EvidenceSchemaError, match="exactly one"):
        ambiguous.only_tool_row("tool_use_boundary")
