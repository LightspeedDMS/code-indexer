"""
Unit tests for UnifiedResponseParser — Story #876.

Tests:
  1. Valid JSON -> UnifiedResult with correct fields
  2. All three valid confidence values (high/medium/low) accepted
  3. ANSI-decorated JSON parsed after cleaning
  4. Code-fence-wrapped JSON extracted and parsed
  5. All lifecycle fields present in result
  6. Non-JSON / empty / JSON array inputs raise UnifiedResponseParseError
  7. Missing description / lifecycle / any lifecycle sub-field raises error (one parametrized test)
     — type safety via two str-typed helpers, no Optional/ignore
  8. confidence not in {high,medium,low} raises error with 'confidence' in message
  9. UnifiedResponseParseError exposes .raw attribute and non-empty .validation_errors
  10. All-or-nothing: no partial success when confidence is missing
"""

import json
from typing import Any, Dict

import pytest

from code_indexer.global_repos.unified_response_parser import (
    UnifiedResponseParseError,
    UnifiedResponseParser,
    UnifiedResult,
)


# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

VALID_LIFECYCLE: Dict[str, str] = {
    "ci_system": "github-actions",
    "deployment_target": "kubernetes",
    "language_ecosystem": "python/poetry",
    "build_system": "poetry",
    "testing_framework": "pytest",
    "confidence": "high",
}

VALID_PAYLOAD: Dict[str, Any] = {
    "description": "A Python service for semantic code search.",
    "lifecycle": VALID_LIFECYCLE,
}


def _raw(obj: Dict[str, Any]) -> str:
    return json.dumps(obj)


def _drop_key(d: Dict[str, Any], key: str) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if k != key}


def _payload_missing_top_key(top_key: str) -> Dict[str, Any]:
    """Build a payload with one top-level key removed. Parameter is always str."""
    return _drop_key(VALID_PAYLOAD, top_key)


def _payload_missing_lifecycle_key(lifecycle_key: str) -> Dict[str, Any]:
    """Build a payload with one lifecycle sub-field removed. Parameter is always str."""
    return {
        "description": "Valid description.",
        "lifecycle": _drop_key(VALID_LIFECYCLE, lifecycle_key),
    }


# ---------------------------------------------------------------------------
# 1. Valid JSON -> UnifiedResult
# ---------------------------------------------------------------------------


def test_parse_valid_json_returns_unified_result_with_correct_fields() -> None:
    """Valid JSON with all required fields returns a correctly populated UnifiedResult."""
    result = UnifiedResponseParser.parse(_raw(VALID_PAYLOAD))

    assert isinstance(result, UnifiedResult)
    assert result.description == "A Python service for semantic code search."
    assert result.lifecycle["ci_system"] == "github-actions"
    assert result.lifecycle["deployment_target"] == "kubernetes"
    assert result.lifecycle["language_ecosystem"] == "python/poetry"
    assert result.lifecycle["build_system"] == "poetry"
    assert result.lifecycle["testing_framework"] == "pytest"
    assert result.lifecycle["confidence"] == "high"


# ---------------------------------------------------------------------------
# 2. All three valid confidence values accepted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("confidence", ["high", "medium", "low"])
def test_parse_all_valid_confidence_values_accepted(confidence: str) -> None:
    """confidence in {high, medium, low} is accepted and preserved in result."""
    payload = {**VALID_PAYLOAD, "lifecycle": {**VALID_LIFECYCLE, "confidence": confidence}}
    result = UnifiedResponseParser.parse(_raw(payload))
    assert result.lifecycle["confidence"] == confidence


# ---------------------------------------------------------------------------
# 3. ANSI-decorated JSON parsed after cleaning
# ---------------------------------------------------------------------------


def test_parse_strips_ansi_before_parsing() -> None:
    """ANSI/CSI escape sequences are stripped before JSON parsing."""
    ansi_decorated = f"\x1b[0m\x1b[32m{_raw(VALID_PAYLOAD)}\x1b[0m"
    result = UnifiedResponseParser.parse(ansi_decorated)
    assert isinstance(result, UnifiedResult)
    assert result.description == VALID_PAYLOAD["description"]


# ---------------------------------------------------------------------------
# 4. Code-fence-wrapped JSON extracted and parsed
# ---------------------------------------------------------------------------


def test_parse_strips_code_fence_before_parsing() -> None:
    """JSON wrapped in markdown code fences is extracted and parsed."""
    fenced = f"```json\n{_raw(VALID_PAYLOAD)}\n```"
    result = UnifiedResponseParser.parse(fenced)
    assert isinstance(result, UnifiedResult)
    assert result.description == VALID_PAYLOAD["description"]


# ---------------------------------------------------------------------------
# 5. All lifecycle fields present in result
# ---------------------------------------------------------------------------


def test_parse_all_lifecycle_fields_present_in_result() -> None:
    """All required lifecycle sub-fields survive into the parsed result."""
    result = UnifiedResponseParser.parse(_raw(VALID_PAYLOAD))
    expected_fields = {
        "ci_system",
        "deployment_target",
        "language_ecosystem",
        "build_system",
        "testing_framework",
        "confidence",
    }
    assert expected_fields.issubset(set(result.lifecycle.keys()))


# ---------------------------------------------------------------------------
# 6. Non-JSON / empty / JSON array raise error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_input",
    [
        "this is not json at all",
        "",
        "[1, 2, 3]",
    ],
    ids=["plain_text", "empty_string", "json_array"],
)
def test_parse_invalid_input_type_raises_parse_error(raw_input: str) -> None:
    """Non-JSON, empty string, and JSON-array inputs raise UnifiedResponseParseError."""
    with pytest.raises(UnifiedResponseParseError) as exc_info:
        UnifiedResponseParser.parse(raw_input)
    assert exc_info.value.raw is not None


# ---------------------------------------------------------------------------
# 7. Missing required fields raise error (single parametrized test)
#    Type safety: bool flag routes to str-typed helper — no Optional params
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("is_top_level", "missing_key"),
    [
        (True, "description"),
        (True, "lifecycle"),
        (False, "ci_system"),
        (False, "deployment_target"),
        (False, "language_ecosystem"),
        (False, "build_system"),
        (False, "testing_framework"),
        (False, "confidence"),
    ],
    ids=[
        "missing_description",
        "missing_lifecycle",
        "missing_ci_system",
        "missing_deployment_target",
        "missing_language_ecosystem",
        "missing_build_system",
        "missing_testing_framework",
        "missing_confidence",
    ],
)
def test_parse_missing_required_field_raises_error(
    is_top_level: bool, missing_key: str
) -> None:
    """Missing any required field at top level or inside lifecycle raises UnifiedResponseParseError.

    The bool flag routes to one of two str-typed helpers; no Optional parameters
    or type-ignore escapes are needed.
    """
    if is_top_level:
        payload = _payload_missing_top_key(missing_key)
    else:
        payload = _payload_missing_lifecycle_key(missing_key)
    with pytest.raises(UnifiedResponseParseError):
        UnifiedResponseParser.parse(_raw(payload))


# ---------------------------------------------------------------------------
# 8. Invalid confidence values raise error with 'confidence' in message
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_confidence",
    ["unknown", "excellent", "none", "HIGH", ""],
    ids=["unknown", "excellent", "none", "uppercase_high", "empty"],
)
def test_parse_invalid_confidence_value_raises_error_mentioning_confidence(
    bad_confidence: str,
) -> None:
    """confidence values not in {high, medium, low} raise UnifiedResponseParseError
    whose string representation mentions 'confidence'."""
    payload = {
        "description": "Valid",
        "lifecycle": {**VALID_LIFECYCLE, "confidence": bad_confidence},
    }
    with pytest.raises(UnifiedResponseParseError) as exc_info:
        UnifiedResponseParser.parse(_raw(payload))
    assert "confidence" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# 9. UnifiedResponseParseError exposes .raw and .validation_errors
# ---------------------------------------------------------------------------


def test_parse_error_exposes_raw_response_on_json_failure() -> None:
    """UnifiedResponseParseError.raw preserves the original input on JSON decode failure."""
    raw = "not json"
    with pytest.raises(UnifiedResponseParseError) as exc_info:
        UnifiedResponseParser.parse(raw)
    assert exc_info.value.raw == raw


def test_parse_error_exposes_non_empty_validation_errors_on_schema_violation() -> None:
    """UnifiedResponseParseError.validation_errors is a non-empty list on schema violation."""
    payload = {"description": "Valid", "lifecycle": {**VALID_LIFECYCLE, "confidence": "unknown"}}
    with pytest.raises(UnifiedResponseParseError) as exc_info:
        UnifiedResponseParser.parse(_raw(payload))
    assert isinstance(exc_info.value.validation_errors, list)
    assert len(exc_info.value.validation_errors) > 0


# ---------------------------------------------------------------------------
# 10. All-or-nothing: no partial success
# ---------------------------------------------------------------------------


def test_parse_all_or_nothing_no_partial_success_on_missing_confidence() -> None:
    """
    All-or-nothing contract: if lifecycle.confidence is absent the entire parse
    fails with UnifiedResponseParseError. No UnifiedResult is produced; the
    caller cannot extract the description to write a partial file.
    """
    with pytest.raises(UnifiedResponseParseError):
        UnifiedResponseParser.parse(_raw(_payload_missing_lifecycle_key("confidence")))
