"""
Unit tests for UnifiedResponseParser v3 schema amendment — Story #876.

Tests the three new OPTIONAL sections (branching, ci, release) added by the
Schema v3 amendment. v2-only responses must still parse without error.

All tests exercise UnifiedResponseParser.parse() as the public API, which
internally invokes _validate_optional_sections for any present optional section.

Tests (one concern each):
  1. test_v3_happy_path_all_sections_populated
  2. test_v2_legacy_json_still_parses
  3. test_partial_v3_one_section_missing
  4. test_reject_branching_missing_default_branch
  5. test_reject_branching_invalid_model_enum
  6. test_reject_ci_trigger_events_invalid_item
  7. test_reject_ci_deploy_on_wrong_type
  8. test_reject_release_auto_publish_wrong_type
  9. test_reject_release_versioning_invalid_enum
 10. test_reject_release_artifact_types_invalid_item
 11. test_reject_section_not_dict
 12. test_schema_version_constant_is_3
"""

import json
from typing import Any, Dict

import pytest

from code_indexer.global_repos.unified_response_parser import (
    CURRENT_LIFECYCLE_SCHEMA_VERSION,
    UnifiedResponseParseError,
    UnifiedResponseParser,
    UnifiedResult,
)


# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_V2_LIFECYCLE: Dict[str, str] = {
    "ci_system": "github-actions",
    "deployment_target": "pypi",
    "language_ecosystem": "python/poetry",
    "build_system": "poetry",
    "testing_framework": "pytest",
    "confidence": "high",
}

_BRANCHING_SECTION: Dict[str, Any] = {
    "default_branch": "main",
    "model": "github-flow",
    "release_branch_pattern": None,
    "protected_branches": ["main"],
}

_CI_SECTION: Dict[str, Any] = {
    "trigger_events": ["push", "pull_request"],
    "required_checks": ["lint", "test"],
    "deploy_on": "tag",
    "environments": ["staging", "production"],
}

_RELEASE_SECTION: Dict[str, Any] = {
    "versioning": "semver",
    "version_source": "pyproject.toml",
    "changelog": "CHANGELOG.md",
    "auto_publish": True,
    "artifact_types": ["wheel", "sdist"],
}

_V3_LIFECYCLE: Dict[str, Any] = {
    **_V2_LIFECYCLE,
    "branching": _BRANCHING_SECTION,
    "ci": _CI_SECTION,
    "release": _RELEASE_SECTION,
}

_V3_PAYLOAD: Dict[str, Any] = {
    "description": "A Python service for semantic code search.",
    "lifecycle": _V3_LIFECYCLE,
}

_V2_PAYLOAD: Dict[str, Any] = {
    "description": "A Python service for semantic code search.",
    "lifecycle": _V2_LIFECYCLE,
}


def _raw(obj: Dict[str, Any]) -> str:
    return json.dumps(obj)


def _with_lifecycle_override(section_name: str, section_value: Any) -> str:
    lifecycle = dict(_V3_LIFECYCLE)
    lifecycle[section_name] = section_value
    return _raw({"description": "Some description.", "lifecycle": lifecycle})


# ---------------------------------------------------------------------------
# 12. Schema version constant is 3 (fast guard — run first)
# ---------------------------------------------------------------------------


def test_schema_version_constant_at_least_3() -> None:
    """CURRENT_LIFECYCLE_SCHEMA_VERSION must be at least 3 (AC-V3-1 baseline).

    Written at v3; the >= form survives future version bumps without needing
    a mechanical update every time the schema advances.
    """
    assert CURRENT_LIFECYCLE_SCHEMA_VERSION >= 3


# ---------------------------------------------------------------------------
# 1. v3 happy path — all three optional sections populated
# ---------------------------------------------------------------------------


def test_v3_happy_path_all_sections_populated() -> None:
    """
    JSON with all 6 v2 keys plus branching/ci/release sections parses
    successfully via UnifiedResponseParser.parse(). lifecycle.branching,
    .ci, and .release must all be present in the returned UnifiedResult (AC-V3-3).
    """
    result = UnifiedResponseParser.parse(_raw(_V3_PAYLOAD))

    assert isinstance(result, UnifiedResult)
    assert "branching" in result.lifecycle
    assert "ci" in result.lifecycle
    assert "release" in result.lifecycle
    assert result.lifecycle["branching"]["default_branch"] == "main"
    assert result.lifecycle["ci"]["deploy_on"] == "tag"
    assert result.lifecycle["release"]["versioning"] == "semver"


# ---------------------------------------------------------------------------
# 2. v2 legacy JSON still parses (backward compatibility)
# ---------------------------------------------------------------------------


def test_v2_legacy_json_still_parses() -> None:
    """
    JSON with only the 6 v2 keys (no optional sections) must parse via
    UnifiedResponseParser.parse() without raising. Backward compatibility
    is preserved — optional sections are absent from the result (AC-V3-2).
    """
    result = UnifiedResponseParser.parse(_raw(_V2_PAYLOAD))

    assert isinstance(result, UnifiedResult)
    assert "branching" not in result.lifecycle
    assert "ci" not in result.lifecycle
    assert "release" not in result.lifecycle
    assert result.lifecycle["confidence"] == "high"


# ---------------------------------------------------------------------------
# 3. Partial v3 — one section absent
# ---------------------------------------------------------------------------


def test_partial_v3_one_section_missing() -> None:
    """
    JSON with branching and ci but NO release section must parse
    successfully — each optional section is independently optional.
    """
    lifecycle = {**_V2_LIFECYCLE, "branching": _BRANCHING_SECTION, "ci": _CI_SECTION}
    payload = {"description": "Some description.", "lifecycle": lifecycle}

    result = UnifiedResponseParser.parse(_raw(payload))

    assert isinstance(result, UnifiedResult)
    assert "branching" in result.lifecycle
    assert "ci" in result.lifecycle
    assert "release" not in result.lifecycle


# ---------------------------------------------------------------------------
# 4. Reject: branching present but default_branch missing
# ---------------------------------------------------------------------------


def test_reject_branching_missing_default_branch() -> None:
    """
    branching section present but default_branch field absent must raise
    UnifiedResponseParseError. The error message must mention both
    'branching' and 'default_branch' (AC-V3-4).
    """
    bad_branching = {
        k: v for k, v in _BRANCHING_SECTION.items() if k != "default_branch"
    }
    raw = _with_lifecycle_override("branching", bad_branching)

    with pytest.raises(UnifiedResponseParseError) as exc_info:
        UnifiedResponseParser.parse(raw)
    assert "branching" in str(exc_info.value).lower()
    assert "default_branch" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# 5. Reject: branching.model invalid enum value
# ---------------------------------------------------------------------------


def test_reject_branching_invalid_model_enum() -> None:
    """
    branching.model set to 'gitops' (not in the allowed enum) must raise
    UnifiedResponseParseError. The error message must mention 'branching'
    and 'model' (AC-V3-4).
    """
    bad_branching = {**_BRANCHING_SECTION, "model": "gitops"}
    raw = _with_lifecycle_override("branching", bad_branching)

    with pytest.raises(UnifiedResponseParseError) as exc_info:
        UnifiedResponseParser.parse(raw)
    msg = str(exc_info.value).lower()
    assert "branching" in msg
    assert "model" in msg


# ---------------------------------------------------------------------------
# 6. Reject: ci.trigger_events contains invalid item
# ---------------------------------------------------------------------------


def test_reject_ci_trigger_events_invalid_item() -> None:
    """
    ci.trigger_events with 'invalid_event' (not in enum) must raise
    UnifiedResponseParseError. The error message must mention 'ci' and
    'trigger_events' (AC-V3-4).
    """
    bad_ci = {**_CI_SECTION, "trigger_events": ["push", "invalid_event"]}
    raw = _with_lifecycle_override("ci", bad_ci)

    with pytest.raises(UnifiedResponseParseError) as exc_info:
        UnifiedResponseParser.parse(raw)
    msg = str(exc_info.value).lower()
    assert "ci" in msg
    assert "trigger_events" in msg


# ---------------------------------------------------------------------------
# 7. Reject: ci.deploy_on wrong type (int instead of str)
# ---------------------------------------------------------------------------


def test_reject_ci_deploy_on_wrong_type() -> None:
    """
    ci.deploy_on set to 42 (int, not str) must raise
    UnifiedResponseParseError. The error message must mention 'ci' and
    'deploy_on' (AC-V3-4).
    """
    bad_ci = {**_CI_SECTION, "deploy_on": 42}
    raw = _with_lifecycle_override("ci", bad_ci)

    with pytest.raises(UnifiedResponseParseError) as exc_info:
        UnifiedResponseParser.parse(raw)
    msg = str(exc_info.value).lower()
    assert "ci" in msg
    assert "deploy_on" in msg


# ---------------------------------------------------------------------------
# 8. Reject: release.auto_publish wrong type (string instead of bool)
# ---------------------------------------------------------------------------


def test_reject_release_auto_publish_wrong_type() -> None:
    """
    release.auto_publish set to string "true" (not bool) must raise
    UnifiedResponseParseError. The error message must mention 'release'
    and 'auto_publish' (AC-V3-4).
    """
    bad_release = {**_RELEASE_SECTION, "auto_publish": "true"}
    raw = _with_lifecycle_override("release", bad_release)

    with pytest.raises(UnifiedResponseParseError) as exc_info:
        UnifiedResponseParser.parse(raw)
    msg = str(exc_info.value).lower()
    assert "release" in msg
    assert "auto_publish" in msg


# ---------------------------------------------------------------------------
# 9. Reject: release.versioning invalid enum value
# ---------------------------------------------------------------------------


def test_reject_release_versioning_invalid_enum() -> None:
    """
    release.versioning set to 'rolling' (not in enum) must raise
    UnifiedResponseParseError. The error message must mention 'release'
    and 'versioning' (AC-V3-4).
    """
    bad_release = {**_RELEASE_SECTION, "versioning": "rolling"}
    raw = _with_lifecycle_override("release", bad_release)

    with pytest.raises(UnifiedResponseParseError) as exc_info:
        UnifiedResponseParser.parse(raw)
    msg = str(exc_info.value).lower()
    assert "release" in msg
    assert "versioning" in msg


# ---------------------------------------------------------------------------
# 10. Reject: release.artifact_types contains invalid item
# ---------------------------------------------------------------------------


def test_reject_release_artifact_types_invalid_item() -> None:
    """
    release.artifact_types containing 'zipfile' (not in enum) must raise
    UnifiedResponseParseError. The error message must mention 'release'
    and 'artifact_types' (AC-V3-4).
    """
    bad_release = {**_RELEASE_SECTION, "artifact_types": ["wheel", "zipfile"]}
    raw = _with_lifecycle_override("release", bad_release)

    with pytest.raises(UnifiedResponseParseError) as exc_info:
        UnifiedResponseParser.parse(raw)
    msg = str(exc_info.value).lower()
    assert "release" in msg
    assert "artifact_types" in msg


# ---------------------------------------------------------------------------
# 11. Reject: optional section value is not a dict
# ---------------------------------------------------------------------------


def test_reject_section_not_dict() -> None:
    """
    branching section set to the string "not-a-dict" (not a dict) must
    raise UnifiedResponseParseError. The error message must mention
    'branching' (AC-V3-4).
    """
    raw = _with_lifecycle_override("branching", "not-a-dict")

    with pytest.raises(UnifiedResponseParseError) as exc_info:
        UnifiedResponseParser.parse(raw)
    assert "branching" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# 13. Null / empty-list optional fields parse successfully (AC-V3-8 positive)
# ---------------------------------------------------------------------------


def test_v3_null_and_empty_list_fields_parse_successfully() -> None:
    """
    Optional fields that accept None or empty lists must not raise when set
    to those values (AC-V3-8 positive path):
      - branching.release_branch_pattern = None
      - branching.protected_branches = None
      - ci.required_checks = []
      - ci.environments = None
      - release.version_source = None
      - release.changelog = None
      - release.artifact_types = []
    """
    lifecycle: Dict[str, Any] = {
        **_V2_LIFECYCLE,
        "branching": {
            "default_branch": "main",
            "model": "trunk-based",
            "release_branch_pattern": None,
            "protected_branches": None,
        },
        "ci": {
            "trigger_events": ["push"],
            "required_checks": [],
            "deploy_on": "merge-to-main",
            "environments": None,
        },
        "release": {
            "versioning": "semver",
            "version_source": None,
            "changelog": None,
            "auto_publish": False,
            "artifact_types": [],
        },
    }
    payload = {"description": "Null/empty-list test repo.", "lifecycle": lifecycle}
    result = UnifiedResponseParser.parse(_raw(payload))

    assert isinstance(result, UnifiedResult)
    assert result.lifecycle["branching"]["release_branch_pattern"] is None
    assert result.lifecycle["branching"]["protected_branches"] is None
    assert result.lifecycle["ci"]["required_checks"] == []
    assert result.lifecycle["ci"]["environments"] is None
    assert result.lifecycle["release"]["version_source"] is None
    assert result.lifecycle["release"]["changelog"] is None
    assert result.lifecycle["release"]["artifact_types"] == []


# ---------------------------------------------------------------------------
# 14. Reject: ci section present but deploy_on missing (AC-V3-4 parity)
# ---------------------------------------------------------------------------


def test_reject_ci_missing_deploy_on() -> None:
    """
    ci section present but deploy_on field absent must raise
    UnifiedResponseParseError. The error message must mention both 'ci'
    and 'deploy_on' (parity with test_reject_branching_missing_default_branch).
    """
    bad_ci = {k: v for k, v in _CI_SECTION.items() if k != "deploy_on"}
    raw = _with_lifecycle_override("ci", bad_ci)

    with pytest.raises(UnifiedResponseParseError) as exc_info:
        UnifiedResponseParser.parse(raw)
    msg = str(exc_info.value).lower()
    assert "ci" in msg
    assert "deploy_on" in msg


# ---------------------------------------------------------------------------
# 15. Reject: release section present but versioning missing (AC-V3-4 parity)
# ---------------------------------------------------------------------------


def test_reject_release_missing_versioning() -> None:
    """
    release section present but versioning field absent must raise
    UnifiedResponseParseError. The error message must mention both 'release'
    and 'versioning' (parity with test_reject_branching_missing_default_branch).
    """
    bad_release = {k: v for k, v in _RELEASE_SECTION.items() if k != "versioning"}
    raw = _with_lifecycle_override("release", bad_release)

    with pytest.raises(UnifiedResponseParseError) as exc_info:
        UnifiedResponseParser.parse(raw)
    msg = str(exc_info.value).lower()
    assert "release" in msg
    assert "versioning" in msg


# ---------------------------------------------------------------------------
# 16. Accept: ci.trigger_events with GitLab "merge_request" value
# ---------------------------------------------------------------------------


def test_ci_trigger_events_accepts_merge_request() -> None:
    """
    ci.trigger_events containing "merge_request" must parse successfully.

    Production regression guard: in v9.21.0 the enum omitted "merge_request"
    (GitLab's MR trigger event, analog of GitHub's "pull_request"), causing
    every GitLab-hosted repo in the lifecycle backfill to fail Schema v3
    validation with UnifiedResponseParseError. Fixed in v9.21.1 by adding
    "merge_request" to _CI_TRIGGER_EVENT_ENUM alongside "pull_request".
    Both values coexist so GitHub and GitLab repos can be represented
    without semantic conflation.
    """
    gitlab_ci = {**_CI_SECTION, "trigger_events": ["push", "merge_request"]}
    raw = _with_lifecycle_override("ci", gitlab_ci)

    result = UnifiedResponseParser.parse(raw)

    assert isinstance(result, UnifiedResult)
    assert result.lifecycle["ci"]["trigger_events"] == ["push", "merge_request"]
