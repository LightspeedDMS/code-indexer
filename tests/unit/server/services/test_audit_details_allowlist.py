"""The ``details`` allowlist, exercised for every catalog entry.

For every action type: a positive case built from one valid value per
allowlisted field, an unknown field rejected, and a rejection that names
the field but never echoes the value.  Plus the typed-field cases that
guard against URLs and URL userinfo reaching a row.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from code_indexer.server.services.audit_events import (
    AUDIT_ACTION_CATALOG,
    AUDIT_TARGET_ID_TYPE,
    JOB_BASED_ACTION_TYPES,
    AuditDetailsSchemaViolation,
    FieldType,
    build_event,
    conforms,
    validate_details,
)

SECRET_VALUE = "user:s3cr3t-value@example.com"

_SAMPLE_BY_KIND: Dict[str, Any] = {
    "bool": True,
    "int": 3,
    "username": "alice",
    "hostname": "git.example.com",
    "repo_alias": "example-repo",
    "git_ref": "feature/x",
    "opaque_id": "id-123",
    "config_key": "golden_repos.refresh_interval_seconds",
    "opaque_id_or_all": "7",
    "config_key_or_all": "golden_repos",
}


def _sample(ftype: FieldType) -> Any:
    if ftype.kind == "enum":
        return sorted(ftype.values)[0]
    if ftype.kind == "list":
        assert ftype.item is not None
        return [_sample(ftype.item)]
    if ftype.kind == "config_value_map":
        return {"golden_repos.refresh_interval_seconds": [60, 120]}
    return _SAMPLE_BY_KIND[ftype.kind]


_ALLOWLISTED = sorted(
    name
    for name, spec in AUDIT_ACTION_CATALOG.items()
    if spec.details_schema is not None
)


def _valid_details(action_type: str) -> Dict[str, Any]:
    schema = AUDIT_ACTION_CATALOG[action_type].details_schema
    assert schema is not None
    return {key: _sample(ftype) for key, ftype in schema.items()}


@pytest.mark.parametrize("action_type", _ALLOWLISTED)
def test_every_catalog_entry_accepts_its_allowlisted_fields(action_type) -> None:
    spec = AUDIT_ACTION_CATALOG[action_type]
    event = build_event(
        actor="alice",
        action_type=action_type,
        target_type=spec.target_type,
        target_id=_sample(AUDIT_TARGET_ID_TYPE[spec.target_type]),
        outcome="success",
        details=_valid_details(action_type),
    )
    assert event.action_type == action_type


@pytest.mark.parametrize("action_type", _ALLOWLISTED)
def test_every_catalog_entry_rejects_unknown_field(action_type) -> None:
    details = _valid_details(action_type)
    details["exception_text"] = SECRET_VALUE
    with pytest.raises(AuditDetailsSchemaViolation) as info:
        validate_details(action_type, details)
    assert info.value.field == "exception_text"
    assert SECRET_VALUE not in str(info.value)


@pytest.mark.parametrize("action_type", _ALLOWLISTED)
def test_every_typed_field_rejects_a_wrong_value_without_echoing(action_type):
    schema = AUDIT_ACTION_CATALOG[action_type].details_schema
    assert schema is not None
    for key in schema:
        details = _valid_details(action_type)
        details[key] = {"nested": SECRET_VALUE}
        with pytest.raises(AuditDetailsSchemaViolation) as info:
            validate_details(action_type, details)
        assert info.value.field == key
        assert SECRET_VALUE not in str(info.value)


def test_hostname_rejects_url_userinfo() -> None:
    with pytest.raises(AuditDetailsSchemaViolation) as info:
        validate_details(
            "ssh_key_host_assigned", {"key_name": "k1", "host": SECRET_VALUE}
        )
    assert info.value.field == "host"


@pytest.mark.parametrize(
    "value",
    ["https://example.com/org/repo.git", "git@example.com:org/repo", "a b"],
)
def test_repo_alias_rejects_urls(value) -> None:
    assert conforms(AUDIT_TARGET_ID_TYPE["repo"], value) is False
    with pytest.raises(AuditDetailsSchemaViolation):
        validate_details(
            "provider_index_bulk_added",
            {"provider": "cohere", "aliases": [value], "job_ids": ["j1"]},
        )


def test_git_ref_rejects_url() -> None:
    with pytest.raises(AuditDetailsSchemaViolation) as info:
        validate_details(
            "golden_repo_branch_changed",
            {"new_branch": "https://example.com/x", "job_id": "j1"},
        )
    assert info.value.field == "new_branch"


def test_bounded_list_rejects_overlong_list() -> None:
    too_many = [f"repo-{i}" for i in range(101)]
    with pytest.raises(AuditDetailsSchemaViolation) as info:
        validate_details(
            "provider_index_bulk_added",
            {"provider": "cohere", "aliases": too_many, "job_ids": []},
        )
    assert info.value.field == "aliases"


def test_config_values_accept_scalars_only() -> None:
    ok = validate_details(
        "config_changed",
        {"change_kind": "update", "values": {"a.b": [True, False], "c": [1, 2]}},
    )
    assert ok is not None
    with pytest.raises(AuditDetailsSchemaViolation) as info:
        validate_details(
            "config_changed",
            {"change_kind": "update", "values": {"a.b": ["old", SECRET_VALUE]}},
        )
    assert info.value.field == "values"


def test_bool_field_rejects_int_and_int_field_rejects_bool() -> None:
    with pytest.raises(AuditDetailsSchemaViolation):
        validate_details("mfa_activated", {"recovery_codes_issued": 1})
    with pytest.raises(AuditDetailsSchemaViolation):
        validate_details("mfa_recovery_codes_regenerated", {"count": True})


def test_non_identifier_unknown_key_is_not_echoed() -> None:
    with pytest.raises(AuditDetailsSchemaViolation) as info:
        validate_details("user_deleted", {SECRET_VALUE: "x"})
    assert info.value.field == "<non-identifier-field>"
    assert SECRET_VALUE not in str(info.value)


def test_details_must_be_a_mapping() -> None:
    with pytest.raises(AuditDetailsSchemaViolation) as info:
        validate_details("user_deleted", ["deleted_role"])  # type: ignore[arg-type]
    assert info.value.field == "details"


def test_empty_details_serialise_to_none() -> None:
    assert validate_details("user_email_changed", {}) is None
    assert validate_details("user_email_changed", None) is None


def test_job_based_types_are_derived_from_catalog() -> None:
    assert "golden_repo_removed" in JOB_BASED_ACTION_TYPES
    assert "provider_index_bulk_added" in JOB_BASED_ACTION_TYPES
    assert "user_deleted" not in JOB_BASED_ACTION_TYPES


def test_target_ids_accept_all_marker_only_where_allowed() -> None:
    assert conforms(AUDIT_TARGET_ID_TYPE["group"], "*") is True
    assert conforms(AUDIT_TARGET_ID_TYPE["config"], "*") is True
    assert conforms(AUDIT_TARGET_ID_TYPE["repo"], "*") is False
