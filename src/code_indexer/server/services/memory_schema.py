"""
memory_schema.py — Validation logic for shared technical memory payloads (Story #877).

Validates create and edit payloads for the shared technical memory store.
No I/O, no YAML parsing — accepts Python dicts and raises exceptions.
"""

from __future__ import annotations

import re

_LINES_RE = re.compile(r"^\d+-\d+$")

__all__ = [
    "MemorySchemaValidationError",
    "validate_create_payload",
    "validate_edit_payload",
]

_VALID_TYPES = frozenset(
    [
        "architectural-fact",
        "gotcha",
        "config-behavior",
        "api-contract",
        "performance-note",
    ]
)

_VALID_SCOPES = frozenset(["global", "repo", "file"])

_REQUIRED_CREATE_FIELDS = [
    "id",
    "type",
    "scope",
    "summary",
    "evidence",
    "created_by",
    "created_at",
]

_IMMUTABLE_FIELDS = frozenset(["id", "created_by", "created_at"])

_MAX_EVIDENCE_ENTRIES = 10


class MemorySchemaValidationError(Exception):
    """Raised when a memory payload violates the schema contract.

    Attributes:
        field: The name of the field that failed validation.
    """

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def _validate_cap(max_summary_chars: object) -> int:
    """Return max_summary_chars as a validated non-negative int or raise ValueError."""
    if not isinstance(max_summary_chars, int) or isinstance(max_summary_chars, bool):
        raise ValueError(
            f"max_summary_chars must be a non-negative integer, got {type(max_summary_chars).__name__!r}"
        )
    if max_summary_chars < 0:
        raise ValueError(f"max_summary_chars must be non-negative, got {max_summary_chars}")
    return max_summary_chars


def validate_create_payload(payload: object, max_summary_chars: object) -> None:
    """Validate a create payload dict against the memory schema.

    Raises:
        ValueError: If max_summary_chars is invalid or payload is not a dict.
        MemorySchemaValidationError: If any field fails schema validation.
    """
    cap: int = _validate_cap(max_summary_chars)
    if not isinstance(payload, dict):
        raise ValueError(f"payload must be a dict, got {type(payload).__name__!r}")
    _run_payload_validation(payload, cap)


def validate_edit_payload(edit: object, current: object, max_summary_chars: object) -> None:
    """Validate an edit payload against the current stored payload.

    Immutable fields (id, created_by, created_at) may not be changed.
    Merges current with edit and runs full create-time validation on the result.

    Raises:
        ValueError: If max_summary_chars is invalid or edit/current are not dicts.
        MemorySchemaValidationError: If an immutable field is changed or the
                                     merged payload fails validation.
    """
    cap: int = _validate_cap(max_summary_chars)
    if not isinstance(edit, dict):
        raise ValueError(f"edit must be a dict, got {type(edit).__name__!r}")
    if not isinstance(current, dict):
        raise ValueError(f"current must be a dict, got {type(current).__name__!r}")
    for field in _IMMUTABLE_FIELDS:
        if field in edit and edit[field] != current.get(field):
            raise MemorySchemaValidationError(
                field, f"field {field!r} is immutable and cannot be changed on edit"
            )
    merged: dict = {**current, **edit}
    _run_payload_validation(merged, cap)


def _run_payload_validation(payload: dict, cap: int) -> None:
    """Orchestrate field-by-field validation of a complete payload dict."""
    for field in _REQUIRED_CREATE_FIELDS:
        if field not in payload:
            raise MemorySchemaValidationError(field, f"required field {field!r} is missing")
    _check_enums_and_scope(payload)
    _check_summary_and_evidence(payload, cap)


def _check_enums_and_scope(payload: dict) -> None:
    """Validate type enum, scope enum, and scope/target consistency."""
    type_val = payload["type"]
    if not isinstance(type_val, str) or type_val not in _VALID_TYPES:
        raise MemorySchemaValidationError(
            "type", f"type must be one of {sorted(_VALID_TYPES)!r}, got {type_val!r}"
        )
    scope_val = payload["scope"]
    if not isinstance(scope_val, str) or scope_val not in _VALID_SCOPES:
        raise MemorySchemaValidationError(
            "scope", f"scope must be one of {sorted(_VALID_SCOPES)!r}, got {scope_val!r}"
        )
    scope_target = payload.get("scope_target")
    referenced_repo = payload.get("referenced_repo")
    if scope_val == "global":
        if scope_target is not None:
            raise MemorySchemaValidationError(
                "scope_target", "scope_target must be null when scope is 'global'"
            )
        if referenced_repo is not None:
            raise MemorySchemaValidationError(
                "referenced_repo", "referenced_repo must be null when scope is 'global'"
            )
    else:
        if scope_target is None:
            raise MemorySchemaValidationError(
                "scope_target", f"scope_target must be non-null when scope is {scope_val!r}"
            )
        if referenced_repo is None:
            raise MemorySchemaValidationError(
                "referenced_repo",
                f"referenced_repo must be non-null when scope is {scope_val!r}",
            )


def _check_summary_and_evidence(payload: dict, cap: int) -> None:
    """Validate summary string length and evidence list contents."""
    summary_val = payload["summary"]
    if not isinstance(summary_val, str):
        raise MemorySchemaValidationError(
            "summary", f"summary must be a string, got {type(summary_val).__name__!r}"
        )
    if len(summary_val) > cap:
        raise MemorySchemaValidationError(
            "summary", f"summary exceeds max {cap} characters (got {len(summary_val)})"
        )
    evidence_val = payload["evidence"]
    if not isinstance(evidence_val, list):
        raise MemorySchemaValidationError(
            "evidence", f"evidence must be a list, got {type(evidence_val).__name__!r}"
        )
    if len(evidence_val) == 0:
        raise MemorySchemaValidationError("evidence", "evidence must have at least one entry")
    if len(evidence_val) > _MAX_EVIDENCE_ENTRIES:
        raise MemorySchemaValidationError(
            "evidence",
            f"evidence must have at most {_MAX_EVIDENCE_ENTRIES} entries, got {len(evidence_val)}",
        )
    for entry in evidence_val:
        _validate_evidence_entry(entry)


def _validate_evidence_entry(entry: object) -> None:
    """Raise MemorySchemaValidationError('evidence') if a single evidence entry is invalid."""
    if not isinstance(entry, dict):
        raise MemorySchemaValidationError("evidence", "each evidence entry must be a dict")
    keys = set(entry.keys())
    has_file = "file" in keys
    has_commit = "commit" in keys
    if has_file and has_commit:
        raise MemorySchemaValidationError(
            "evidence", "evidence entry must be exclusively {file+lines} or {commit}, not both"
        )
    if has_file:
        if keys != {"file", "lines"}:
            raise MemorySchemaValidationError(
                "evidence",
                f"file evidence entry must have exactly keys {{file, lines}}, got {keys!r}",
            )
        if not isinstance(entry["file"], str) or not entry["file"]:
            raise MemorySchemaValidationError(
                "evidence", "evidence 'file' must be a non-empty string"
            )
        lines_val = entry["lines"]
        if not isinstance(lines_val, str) or not _LINES_RE.fullmatch(lines_val):
            raise MemorySchemaValidationError(
                "evidence", "evidence 'lines' must be a non-empty string in '<start>-<end>' format"
            )
        return
    if has_commit:
        if keys != {"commit"}:
            raise MemorySchemaValidationError(
                "evidence",
                f"commit evidence entry must have exactly key {{commit}}, got {keys!r}",
            )
        if not isinstance(entry["commit"], str) or not entry["commit"]:
            raise MemorySchemaValidationError(
                "evidence", "evidence 'commit' must be a non-empty string"
            )
        return
    raise MemorySchemaValidationError(
        "evidence",
        f"evidence entry must have 'file'+'lines' or 'commit', got keys {keys!r}",
    )
