"""
UnifiedResponseParser — Story #876.

Parses the unified JSON response returned by a single Claude CLI call per repo
(the new lifecycle+description combined prompt). Replaces the previous two-phase
approach (Phase 1 YAML description + Phase 2 yaml.safe_load lifecycle).

Contract:
  - Input is raw Claude CLI output (may contain ANSI/CSI escapes, code fences).
  - Output is a UnifiedResult dataclass on success.
  - Any schema violation raises UnifiedResponseParseError (all-or-nothing — no
    partial writes are possible because no result is returned).
  - confidence must be exactly one of {high, medium, low}. "unknown" is rejected.
  - All lifecycle sub-fields must be non-empty strings.

Usage:
    result = UnifiedResponseParser.parse(raw_claude_output)
    # result.description: str
    # result.lifecycle: dict  (all six sub-fields, each a non-empty string)
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from code_indexer.global_repos.repo_analyzer import _clean_claude_output

# Current lifecycle schema version emitted by this parser.
# Bumped to 3 for Schema v3 amendment (Story #876) — adds optional
# branching/ci/release sections. v2 files continue to parse without change.
CURRENT_LIFECYCLE_SCHEMA_VERSION: int = 3

# Confidence enum accepted by the unified contract.
_VALID_CONFIDENCE = frozenset({"high", "medium", "low"})

# Required top-level keys in the JSON response.
_REQUIRED_TOP_KEYS = ("description", "lifecycle")

# Required keys inside the lifecycle sub-object (all must be non-empty strings).
_REQUIRED_LIFECYCLE_KEYS = (
    "ci_system",
    "deployment_target",
    "language_ecosystem",
    "build_system",
    "testing_framework",
    "confidence",
)

# ---------------------------------------------------------------------------
# v3 optional section schemas (Schema v3 amendment — Story #876)
# ---------------------------------------------------------------------------

_BRANCHING_MODEL_ENUM = (
    "github-flow",
    "gitflow",
    "trunk-based",
    "release-branch",
    "unknown",
)
_CI_TRIGGER_EVENT_ENUM = (
    "push",
    "pull_request",
    "merge_request",
    "tag",
    "schedule",
    "workflow_dispatch",
    "manual",
)
_CI_DEPLOY_ON_ENUM = (
    "tag",
    "merge-to-main",
    "merge-to-release-branch",
    "manual",
    "none",
)
_RELEASE_VERSIONING_ENUM = (
    "semver",
    "calver",
    "custom",
    "none",
    "unknown",
)
_RELEASE_ARTIFACT_TYPE_ENUM = (
    "wheel",
    "sdist",
    "docker",
    "tarball",
    "binary",
    "gem",
    "jar",
    "nupkg",
    "deb",
    "rpm",
    "other",
)

# Schema for each optional section.  Keys are field names; values describe
# the field's expected Python type, optional enum constraint, and whether the
# field is required-within-section (all are required within a present section).
#
# "type": Python type or tuple of types accepted by isinstance().
# "enum": tuple of allowed string values (applied when field is a str).
# "item_enum": tuple of allowed values for *list* items (str items only).
# "item_type": expected Python type for list items (used when no item_enum).
# "required": True — every field is required when the section is present.
_OPTIONAL_SECTION_SCHEMAS: Dict[str, Any] = {
    "branching": {
        "default_branch": {"type": str, "required": True},
        "model": {"type": str, "enum": _BRANCHING_MODEL_ENUM, "required": True},
        "release_branch_pattern": {"type": (str, type(None)), "required": True},
        "protected_branches": {"type": (list, type(None)), "required": True},
    },
    "ci": {
        "trigger_events": {
            "type": list,
            "item_enum": _CI_TRIGGER_EVENT_ENUM,
            "required": True,
        },
        "required_checks": {"type": list, "item_type": str, "required": True},
        "deploy_on": {"type": str, "enum": _CI_DEPLOY_ON_ENUM, "required": True},
        "environments": {"type": (list, type(None)), "required": True},
    },
    "release": {
        "versioning": {
            "type": str,
            "enum": _RELEASE_VERSIONING_ENUM,
            "required": True,
        },
        "version_source": {"type": (str, type(None)), "required": True},
        "changelog": {"type": (str, type(None)), "required": True},
        "auto_publish": {"type": bool, "required": True},
        "artifact_types": {
            "type": list,
            "item_enum": _RELEASE_ARTIFACT_TYPE_ENUM,
            "required": True,
        },
    },
}


# ---------------------------------------------------------------------------
# Public exceptions and result types
# ---------------------------------------------------------------------------


class UnifiedResponseParseError(Exception):
    """
    Raised when the Claude CLI response cannot be parsed as a valid unified
    JSON response (Story #876 — all-or-nothing contract).

    Attributes:
        raw: The original raw input string as received (before cleaning).
        validation_errors: Non-empty list of human-readable validation messages
            when the JSON parses but the schema is violated. Empty list for
            JSON decode errors (the decode error itself is the cause).
    """

    def __init__(
        self,
        message: str,
        raw: Optional[str],
        validation_errors: Optional[List[str]] = None,
    ) -> None:
        super().__init__(message)
        self.raw = raw
        self.validation_errors: List[str] = (
            validation_errors if validation_errors else []
        )


@dataclass
class UnifiedResult:
    """
    Parsed result from a single unified Claude CLI call.

    Fields:
        description: Non-empty repository description string.
        lifecycle: Dict containing all six required lifecycle sub-fields,
                   each a non-empty string.
    """

    description: str
    lifecycle: Dict[str, Any]


# ---------------------------------------------------------------------------
# Parser implementation
# ---------------------------------------------------------------------------


class UnifiedResponseParser:
    """
    Stateless parser for unified Claude CLI JSON responses.

    Call UnifiedResponseParser.parse(raw) — all logic is in the class method
    to keep the API surface minimal and avoid accidental state leakage.
    """

    @classmethod
    def parse(cls, raw: str) -> UnifiedResult:
        """
        Parse raw Claude CLI output into a UnifiedResult.

        Steps:
        1. Validate raw is a non-None string (raises UnifiedResponseParseError otherwise).
        2. Apply _clean_claude_output (existing ANSI/CSI/OSC stripper).
        3. Strip markdown code fences if present.
        4. json.loads strict parse.
        5. Validate schema: required top-level keys, non-empty description,
           lifecycle sub-keys, each sub-field is a non-empty string,
           confidence enum {high, medium, low}.
        6. Return UnifiedResult or raise UnifiedResponseParseError.

        Args:
            raw: Raw string from Claude CLI subprocess stdout.

        Returns:
            UnifiedResult with description and lifecycle fields.

        Raises:
            UnifiedResponseParseError: On any JSON or schema violation.
                .raw always preserves the original input string.
                .validation_errors is non-empty for schema violations.
        """
        if not isinstance(raw, str):
            raise UnifiedResponseParseError(
                f"raw input must be a string, got {type(raw).__name__}",
                raw=str(raw) if raw is not None else None,
            )

        cleaned = _clean_claude_output(raw)
        cleaned = cls._strip_code_fence(cleaned)
        cleaned = cls._strip_preamble(cleaned)

        try:
            obj = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise UnifiedResponseParseError(
                f"not valid JSON: {exc}",
                raw=raw,
            ) from exc

        if not isinstance(obj, dict):
            raise UnifiedResponseParseError(
                f"expected a JSON object, got {type(obj).__name__}",
                raw=raw,
                validation_errors=[
                    f"top-level value is {type(obj).__name__}, expected object"
                ],
            )

        errors = cls._validate(obj)
        if errors:
            raise UnifiedResponseParseError(
                f"schema validation failed: {'; '.join(errors)}",
                raw=raw,
                validation_errors=errors,
            )

        # v3 optional section validation (branching / ci / release).
        # Absent sections are silently skipped; present sections are fully
        # validated. Raises UnifiedResponseParseError on first violation.
        cls._validate_optional_sections(obj["lifecycle"], raw)

        return UnifiedResult(
            description=obj["description"],
            lifecycle=dict(obj["lifecycle"]),
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        """Remove markdown code fences (```json ... ``` or ``` ... ```)."""
        pattern = r"^```(?:json|)\s*\n([\s\S]*?)\n?```\s*$"
        match = re.match(pattern, text.strip())
        if match:
            return match.group(1).strip()
        return text

    @staticmethod
    def _strip_preamble(text: str) -> str:
        """
        Strip any text before the first '{' character.

        Claude CLI may emit prose before the JSON object despite the prompt
        saying "no preamble".  This is a defence-in-depth step: find the
        first opening brace and discard everything before it so json.loads
        receives a clean JSON string.

        If no '{' is found the text is returned unchanged — json.loads will
        fail with a clear error message which propagates as
        UnifiedResponseParseError.
        """
        brace_pos = text.find("{")
        if brace_pos > 0:
            return text[brace_pos:]
        return text

    @staticmethod
    def _check_section_shape(
        section_name: str, section: Any, field_specs: Dict[str, Any], raw: str
    ) -> None:
        """Raise if *section* is not a dict or any required field is absent."""
        if not isinstance(section, dict):
            raise UnifiedResponseParseError(
                f"lifecycle.{section_name} must be an object, "
                f"got {type(section).__name__!r}",
                raw=raw,
                validation_errors=[
                    f"lifecycle.{section_name}: expected dict, "
                    f"got {type(section).__name__!r}"
                ],
            )
        for field_name in field_specs:
            if field_name not in section:
                path = f"lifecycle.{section_name}.{field_name}"
                raise UnifiedResponseParseError(
                    f"missing required field: '{path}'",
                    raw=raw,
                    validation_errors=[f"missing required field: '{path}'"],
                )

    @staticmethod
    def _check_field_value(
        field_path: str, value: Any, spec: Dict[str, Any], raw: str
    ) -> None:
        """Validate *value* against *spec* (type, enum, item_enum, item_type)."""
        expected_type = spec["type"]
        if not isinstance(value, expected_type):
            raise UnifiedResponseParseError(
                f"{field_path} has wrong type: "
                f"expected {expected_type!r}, got {type(value).__name__!r}",
                raw=raw,
                validation_errors=[
                    f"{field_path}: wrong type {type(value).__name__!r}"
                ],
            )
        if isinstance(value, str) and "enum" in spec and value not in spec["enum"]:
            raise UnifiedResponseParseError(
                f"{field_path} value {value!r} not in allowed enum {spec['enum']}",
                raw=raw,
                validation_errors=[f"{field_path}: {value!r} not in {spec['enum']}"],
            )
        if isinstance(value, list):
            item_enum = spec.get("item_enum")
            item_type = spec.get("item_type")
            for item in value:
                if item_enum is not None and item not in item_enum:
                    raise UnifiedResponseParseError(
                        f"{field_path} item {item!r} not in allowed enum {item_enum}",
                        raw=raw,
                        validation_errors=[f"{field_path}: item {item!r} not in enum"],
                    )
                if item_type is not None and not isinstance(item, item_type):
                    raise UnifiedResponseParseError(
                        f"{field_path} item {item!r} has wrong type",
                        raw=raw,
                        validation_errors=[f"{field_path}: item wrong type"],
                    )

    @classmethod
    def _validate_optional_sections(cls, lifecycle: Dict[str, Any], raw: str) -> None:
        """
        Validate optional v3 sections (branching, ci, release) in *lifecycle*.

        Absent sections are silently skipped — each is independently optional
        (backward-compatible with v2 files). For present sections every required
        field is validated for type and enum membership.

        Raises:
            UnifiedResponseParseError: On the first violation, with section and
                field path in the message for operator diagnostics.
        """
        for section_name, field_specs in _OPTIONAL_SECTION_SCHEMAS.items():
            if section_name not in lifecycle:
                continue
            section_value = lifecycle[section_name]
            cls._check_section_shape(section_name, section_value, field_specs, raw)
            for field_name, spec in field_specs.items():
                field_path = f"lifecycle.{section_name}.{field_name}"
                cls._check_field_value(field_path, section_value[field_name], spec, raw)

    @staticmethod
    def _validate(obj: Dict[str, Any]) -> List[str]:
        """
        Validate the parsed object against the unified JSON schema.

        Checks:
        - Required top-level keys present.
        - description is a non-empty string.
        - lifecycle is a dict.
        - Each lifecycle sub-field is present AND is a non-empty string.
        - confidence is exactly one of {high, medium, low}.

        Returns a list of error strings (empty list = valid).
        """
        errors: List[str] = []

        # -- top-level required keys --
        for key in _REQUIRED_TOP_KEYS:
            if key not in obj:
                errors.append(f"missing required field: '{key}'")

        # -- description must be a non-empty string --
        desc = obj.get("description")
        if desc is not None:
            if not isinstance(desc, str) or not desc.strip():
                errors.append("'description' must be a non-empty string")

        # -- lifecycle block must be a dict --
        lifecycle = obj.get("lifecycle")
        if lifecycle is not None:
            if not isinstance(lifecycle, dict):
                errors.append("'lifecycle' must be an object")
                return errors  # cannot validate sub-fields if lifecycle is wrong type

            # -- each required lifecycle sub-field must be a non-empty string --
            for key in _REQUIRED_LIFECYCLE_KEYS:
                if key not in lifecycle:
                    errors.append(f"missing required lifecycle field: '{key}'")
                    continue
                value = lifecycle[key]
                if not isinstance(value, str) or not value.strip():
                    errors.append(
                        f"lifecycle.{key} must be a non-empty string, got: {value!r}"
                    )

            # -- confidence enum check (independent of empty-string check above) --
            confidence = lifecycle.get("confidence")
            if isinstance(confidence, str) and confidence.strip():
                if confidence not in _VALID_CONFIDENCE:
                    errors.append(
                        f"lifecycle.confidence must be one of {sorted(_VALID_CONFIDENCE)}, "
                        f"got: {confidence!r}"
                    )

        return errors
