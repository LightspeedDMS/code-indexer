"""
Unit tests for UnifiedResponseParser v4 schema amendment — Story #885.

Tests the new `branch_environment_map` field added by the Schema v4 amendment,
including type validation, cross-field consistency HARD REJECT, and backward
compatibility with v3 responses.

Tests (one concern each):
  1. test_schema_version_constant_is_4         (AC-V4-1)
  2. test_branch_environment_map_accepted_when_valid   (AC-V4-2)
  3. test_branch_environment_map_empty_dict_and_omitted_are_semantically_identical
                                                (AC-V4-3 + AC-V4-4 — {} == omitted)
  4. test_branch_environment_map_wrong_type_rejected   (AC-V4-5a — not a dict)
  5. test_branch_environment_map_non_string_values_rejected  (AC-V4-5b — dict[str, int])
  6. test_hard_reject_env_not_in_ci_environments       (AC-V4-6 — HARD REJECT cross-field)
  7. test_hard_reject_names_specific_env_in_message    (AC-V4-6b — error names the bad env)
  8. test_hard_reject_multiple_missing_envs_names_all  (AC-V4-6c — all undeclared envs named)
  9. test_environments_items_must_be_strings           (Algorithm 2 invariant)
 10. test_environments_items_no_leading_trailing_space (Algorithm 2 invariant)
 11. test_environments_items_no_empty_string           (Algorithm 2 invariant)
 12. test_environments_no_duplicate_items              (Algorithm 2 invariant)
 13. test_branch_env_map_keys_no_leading_trailing_space (Algorithm 2 invariant)
 14. test_branch_env_map_values_no_leading_trailing_space (Algorithm 2 invariant)
 15. test_branch_env_map_keys_no_empty                 (Algorithm 2 invariant)
 16. test_branch_env_map_values_no_empty               (Algorithm 2 invariant)
 17. test_v3_legacy_no_branch_environment_map          (legacy compatibility)
 18. test_v3_legacy_no_ci_section                      (legacy compatibility — no ci at all)
"""

import json
from typing import Any, Dict, Optional

import pytest

from code_indexer.global_repos.unified_response_parser import (
    CURRENT_LIFECYCLE_SCHEMA_VERSION,
    SchemaValidationError,
    UnifiedResponseParseError,
    UnifiedResponseParser,
    UnifiedResult,
)


# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_V2_LIFECYCLE: Dict[str, Any] = {
    "ci_system": "github-actions",
    "deployment_target": "pypi",
    "language_ecosystem": "python/poetry",
    "build_system": "poetry",
    "testing_framework": "pytest",
    "confidence": "high",
}

_CI_SECTION: Dict[str, Any] = {
    "trigger_events": ["push", "pull_request"],
    "required_checks": ["lint", "test"],
    "deploy_on": "merge-to-main",
    "environments": ["staging", "production"],
}


def _make_raw(lifecycle_extra: Optional[Dict[str, Any]] = None) -> str:
    """Build a minimal valid unified JSON response string."""
    lifecycle: Dict[str, Any] = dict(_V2_LIFECYCLE)
    if lifecycle_extra:
        lifecycle.update(lifecycle_extra)
    obj = {"description": "test repo", "lifecycle": lifecycle}
    return json.dumps(obj)


# ---------------------------------------------------------------------------
# 1. Schema version constant
# ---------------------------------------------------------------------------


def test_schema_version_constant_is_4() -> None:
    """CURRENT_LIFECYCLE_SCHEMA_VERSION must equal 4 (AC-V4-1)."""
    assert CURRENT_LIFECYCLE_SCHEMA_VERSION == 4


# ---------------------------------------------------------------------------
# 2. Happy path with valid branch_environment_map
# ---------------------------------------------------------------------------


def test_branch_environment_map_accepted_when_valid() -> None:
    """
    AC-V4-2 — branch_environment_map with valid dict[str, str] entries is
    accepted when ci.environments contains all referenced environment values.
    """
    raw = _make_raw(
        {
            "ci": {**_CI_SECTION, "environments": ["staging", "production"]},
            "branch_environment_map": {"main": "production", "develop": "staging"},
        }
    )
    result = UnifiedResponseParser.parse(raw)
    assert isinstance(result, UnifiedResult)
    bem = result.lifecycle.get("branch_environment_map")
    assert bem == {"main": "production", "develop": "staging"}


# ---------------------------------------------------------------------------
# 3. Semantic identity: {} is identical to omitted
# ---------------------------------------------------------------------------


def test_branch_environment_map_empty_dict_and_omitted_are_semantically_identical() -> (
    None
):
    """
    AC-V4-3+V4-4 — empty {} and omitted branch_environment_map are semantically
    identical. The parser must return the same normalized representation for both.

    Normalization rule: both produce a lifecycle dict where branch_environment_map
    is either absent or an empty mapping — callers must not distinguish between
    them. This test verifies that the effective (non-empty) content is the same.
    """
    raw_empty_dict = _make_raw(
        {
            "ci": _CI_SECTION,
            "branch_environment_map": {},
        }
    )
    raw_omitted = _make_raw({"ci": _CI_SECTION})

    result_empty = UnifiedResponseParser.parse(raw_empty_dict)
    result_omitted = UnifiedResponseParser.parse(raw_omitted)

    # Both must succeed
    assert isinstance(result_empty, UnifiedResult)
    assert isinstance(result_omitted, UnifiedResult)

    # Effective branch_environment_map must be semantically identical (both empty)
    bem_empty = result_empty.lifecycle.get("branch_environment_map") or {}
    bem_omitted = result_omitted.lifecycle.get("branch_environment_map") or {}
    assert bem_empty == bem_omitted == {}


# ---------------------------------------------------------------------------
# 4-5. Type validation rejections
# ---------------------------------------------------------------------------


def test_branch_environment_map_wrong_type_rejected() -> None:
    """AC-V4-5a — branch_environment_map that is not a dict must be rejected."""
    raw = _make_raw(
        {
            "ci": _CI_SECTION,
            "branch_environment_map": ["main:production"],
        }
    )
    with pytest.raises((UnifiedResponseParseError, SchemaValidationError)):
        UnifiedResponseParser.parse(raw)


def test_branch_environment_map_non_string_values_rejected() -> None:
    """
    AC-V4-5b — branch_environment_map with non-string values (dict[str, int])
    must be rejected. JSON allows integer values; the parser must enforce str.
    """
    raw = _make_raw(
        {
            "ci": _CI_SECTION,
            "branch_environment_map": {"main": 42},
        }
    )
    with pytest.raises((UnifiedResponseParseError, SchemaValidationError)):
        UnifiedResponseParser.parse(raw)


# ---------------------------------------------------------------------------
# 6-8. HARD REJECT cross-field inconsistency (AC-V4-6)
# ---------------------------------------------------------------------------


def test_hard_reject_env_not_in_ci_environments() -> None:
    """
    AC-V4-6 — When branch_environment_map references an environment value
    not listed in ci.environments, SchemaValidationError is raised (HARD REJECT).
    """
    raw = _make_raw(
        {
            "ci": {**_CI_SECTION, "environments": ["staging"]},
            "branch_environment_map": {
                "main": "production"
            },  # "production" not declared
        }
    )
    with pytest.raises(SchemaValidationError):
        UnifiedResponseParser.parse(raw)


def test_hard_reject_names_specific_env_in_message() -> None:
    """
    AC-V4-6b — The SchemaValidationError message must name the undeclared
    environment so operators can identify the inconsistency.
    """
    raw = _make_raw(
        {
            "ci": {**_CI_SECTION, "environments": ["staging"]},
            "branch_environment_map": {"main": "production"},
        }
    )
    with pytest.raises(SchemaValidationError) as exc_info:
        UnifiedResponseParser.parse(raw)
    assert "production" in str(exc_info.value)


def test_hard_reject_multiple_missing_envs_names_all() -> None:
    """
    AC-V4-6c — When multiple environments are undeclared, the error message
    must name ALL of them (not just one). This ensures operator diagnostics
    are complete — a single lookup is not enough to fix the inconsistency.
    """
    raw = _make_raw(
        {
            "ci": {**_CI_SECTION, "environments": ["staging"]},
            "branch_environment_map": {
                "main": "production",
                "hotfix": "dr-production",
            },
        }
    )
    with pytest.raises(SchemaValidationError) as exc_info:
        UnifiedResponseParser.parse(raw)
    msg = str(exc_info.value)
    assert "production" in msg and "dr-production" in msg


# ---------------------------------------------------------------------------
# 9-12. Algorithm 2 invariants — ci.environments list
# ---------------------------------------------------------------------------


def test_environments_items_must_be_strings() -> None:
    """Algorithm 2 — ci.environments items must be strings."""
    raw = _make_raw(
        {
            "ci": {**_CI_SECTION, "environments": [1, 2]},
        }
    )
    with pytest.raises((UnifiedResponseParseError, SchemaValidationError)):
        UnifiedResponseParser.parse(raw)


def test_environments_items_no_leading_trailing_space() -> None:
    """Algorithm 2 — ci.environments items must equal their stripped value."""
    raw = _make_raw(
        {
            "ci": {**_CI_SECTION, "environments": [" staging "]},
        }
    )
    with pytest.raises((UnifiedResponseParseError, SchemaValidationError)):
        UnifiedResponseParser.parse(raw)


def test_environments_items_no_empty_string() -> None:
    """Algorithm 2 — ci.environments items must be non-empty after strip."""
    raw = _make_raw(
        {
            "ci": {**_CI_SECTION, "environments": [""]},
        }
    )
    with pytest.raises((UnifiedResponseParseError, SchemaValidationError)):
        UnifiedResponseParser.parse(raw)


def test_environments_no_duplicate_items() -> None:
    """Algorithm 2 — ci.environments must not contain duplicate entries."""
    raw = _make_raw(
        {
            "ci": {**_CI_SECTION, "environments": ["staging", "staging"]},
        }
    )
    with pytest.raises((UnifiedResponseParseError, SchemaValidationError)):
        UnifiedResponseParser.parse(raw)


# ---------------------------------------------------------------------------
# 13-16. Algorithm 2 invariants — branch_environment_map key/value cleanliness
# ---------------------------------------------------------------------------


def test_branch_env_map_keys_no_leading_trailing_space() -> None:
    """Algorithm 2 — branch_environment_map keys must equal their stripped value."""
    raw = _make_raw(
        {
            "ci": {**_CI_SECTION, "environments": ["staging"]},
            "branch_environment_map": {" main ": "staging"},
        }
    )
    with pytest.raises((UnifiedResponseParseError, SchemaValidationError)):
        UnifiedResponseParser.parse(raw)


def test_branch_env_map_values_no_leading_trailing_space() -> None:
    """Algorithm 2 — branch_environment_map values must equal their stripped value."""
    raw = _make_raw(
        {
            "ci": {**_CI_SECTION, "environments": ["staging"]},
            "branch_environment_map": {"main": " staging "},
        }
    )
    with pytest.raises((UnifiedResponseParseError, SchemaValidationError)):
        UnifiedResponseParser.parse(raw)


def test_branch_env_map_keys_no_empty() -> None:
    """Algorithm 2 — branch_environment_map keys must be non-empty after strip."""
    raw = _make_raw(
        {
            "ci": {**_CI_SECTION, "environments": ["staging"]},
            "branch_environment_map": {"": "staging"},
        }
    )
    with pytest.raises((UnifiedResponseParseError, SchemaValidationError)):
        UnifiedResponseParser.parse(raw)


def test_branch_env_map_values_no_empty() -> None:
    """Algorithm 2 — branch_environment_map values must be non-empty after strip."""
    raw = _make_raw(
        {
            "ci": {**_CI_SECTION, "environments": ["staging"]},
            "branch_environment_map": {"main": ""},
        }
    )
    with pytest.raises((UnifiedResponseParseError, SchemaValidationError)):
        UnifiedResponseParser.parse(raw)


# ---------------------------------------------------------------------------
# 17-18. Legacy / backward compatibility
# ---------------------------------------------------------------------------


def test_v3_legacy_no_branch_environment_map() -> None:
    """
    v3 legacy — A response with ci.environments populated but no
    branch_environment_map must parse successfully (field is optional).
    """
    raw = _make_raw({"ci": _CI_SECTION})
    result = UnifiedResponseParser.parse(raw)
    assert isinstance(result, UnifiedResult)
    # branch_environment_map is absent (not required)
    assert "branch_environment_map" not in result.lifecycle


def test_v3_legacy_no_ci_section() -> None:
    """
    v3 legacy — A minimal response with no ci section at all (no environments,
    no branch_environment_map) must parse successfully.
    """
    raw = _make_raw()
    result = UnifiedResponseParser.parse(raw)
    assert isinstance(result, UnifiedResult)
