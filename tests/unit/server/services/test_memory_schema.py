"""
Unit tests for memory_schema.py — YAML frontmatter schema validator (Story #877 Phase 1).

Tests verify validate_create_payload() and validate_edit_payload() enforce:
- Required fields and types
- Type enum values
- Scope enum values and scope_target / referenced_repo consistency rules
- Summary character cap
- Evidence list bounds and entry format
- Immutable field protection on edit (id, created_by, created_at only)

Immutability contract (per Story #877 spec):
    Immutable on edit: id, created_by, created_at
    Mutable on edit:   type, scope, scope_target, referenced_repo, summary, evidence

scope_target and referenced_repo are independent string fields; there is no
rule that they must be equal. scope_target names the target within the repo
(e.g., a file path for scope=file, or an alias for scope=repo);
referenced_repo names the alias of the owning repository.
"""

import pytest

from code_indexer.server.services.memory_schema import (
    MemorySchemaValidationError,
    validate_create_payload,
    validate_edit_payload,
)

# ---------------------------------------------------------------------------
# Constants — no magic numbers in test bodies
# ---------------------------------------------------------------------------

MAX_SUMMARY_CHARS = 1000
OVER_LIMIT_SUMMARY = "x" * (MAX_SUMMARY_CHARS + 1)
CUSTOM_SUMMARY_CAP = 50
CUSTOM_SUMMARY_OVER_LIMIT = "x" * (CUSTOM_SUMMARY_CAP + 1)
MAX_EVIDENCE_ENTRIES = 10
OVER_MAX_EVIDENCE_ENTRIES = MAX_EVIDENCE_ENTRIES + 1
VALID_UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
VALID_UUID_2 = "b2c3d4e5-f6a7-8901-bcde-f12345678901"
VALID_ISO8601 = "2026-04-21T10:00:00+00:00"
VALID_ISO8601_2 = "2026-04-21T11:00:00+00:00"


# ---------------------------------------------------------------------------
# Helpers — build minimal valid payloads for each scope
# ---------------------------------------------------------------------------


def _global_payload(**overrides):
    """Return a minimal valid global-scope create payload."""
    base = {
        "id": VALID_UUID,
        "type": "architectural-fact",
        "scope": "global",
        "scope_target": None,
        "referenced_repo": None,
        "summary": "Global memory summary.",
        "evidence": [{"file": "src/foo.py", "lines": "1-10"}],
        "created_by": "test-agent",
        "created_at": VALID_ISO8601,
        "edited_by": None,
        "edited_at": None,
    }
    base.update(overrides)
    return base


def _repo_payload(**overrides):
    """Return a minimal valid repo-scope create payload."""
    base = {
        "id": VALID_UUID,
        "type": "gotcha",
        "scope": "repo",
        "scope_target": "my-repo",
        "referenced_repo": "my-repo",
        "summary": "Repo-scoped memory summary.",
        "evidence": [{"commit": "abc1234"}],
        "created_by": "test-agent",
        "created_at": VALID_ISO8601,
        "edited_by": None,
        "edited_at": None,
    }
    base.update(overrides)
    return base


def _file_payload(**overrides):
    """Return a minimal valid file-scope create payload."""
    base = {
        "id": VALID_UUID,
        "type": "config-behavior",
        "scope": "file",
        "scope_target": "src/server/app.py",
        "referenced_repo": "my-repo",
        "summary": "File-scoped memory summary.",
        "evidence": [{"file": "src/server/app.py", "lines": "42-55"}],
        "created_by": "test-agent",
        "created_at": VALID_ISO8601,
        "edited_by": None,
        "edited_at": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestHappyPaths:
    """validate_create_payload accepts all valid scope combinations."""

    def test_global_scope_valid(self):
        """Global scope with null scope_target and null referenced_repo passes."""
        validate_create_payload(_global_payload(), MAX_SUMMARY_CHARS)

    def test_repo_scope_valid(self):
        """Repo scope with non-null scope_target and referenced_repo passes."""
        validate_create_payload(_repo_payload(), MAX_SUMMARY_CHARS)

    def test_file_scope_valid(self):
        """File scope with non-null scope_target and referenced_repo passes."""
        validate_create_payload(_file_payload(), MAX_SUMMARY_CHARS)

    def test_all_type_enum_values_accepted(self):
        """All five type enum values are accepted."""
        valid_types = [
            "architectural-fact",
            "gotcha",
            "config-behavior",
            "api-contract",
            "performance-note",
        ]
        for type_value in valid_types:
            validate_create_payload(_global_payload(type=type_value), MAX_SUMMARY_CHARS)

    def test_evidence_with_commit_entry(self):
        """Evidence entry with only a commit hash is accepted."""
        payload = _global_payload(evidence=[{"commit": "deadbeef"}])
        validate_create_payload(payload, MAX_SUMMARY_CHARS)

    def test_evidence_with_file_and_lines_entry(self):
        """Evidence entry with file + lines is accepted."""
        payload = _global_payload(evidence=[{"file": "src/a.py", "lines": "1-5"}])
        validate_create_payload(payload, MAX_SUMMARY_CHARS)

    def test_evidence_up_to_max_entries(self):
        """Evidence list with exactly MAX_EVIDENCE_ENTRIES entries is accepted."""
        entries = [{"commit": f"abc{i:04d}"} for i in range(MAX_EVIDENCE_ENTRIES)]
        payload = _global_payload(evidence=entries)
        validate_create_payload(payload, MAX_SUMMARY_CHARS)

    def test_summary_at_exact_limit(self):
        """Summary at exactly max_summary_chars is accepted."""
        payload = _global_payload(summary="x" * MAX_SUMMARY_CHARS)
        validate_create_payload(payload, MAX_SUMMARY_CHARS)

    def test_repo_scope_target_and_referenced_repo_may_differ(self):
        """scope_target and referenced_repo are independent — differing values are valid.

        scope_target names what is targeted within the repo (e.g., an alias sub-path);
        referenced_repo names the owning repository alias. They may differ.
        """
        payload = _repo_payload(scope_target="cidx-meta", referenced_repo="other-alias")
        validate_create_payload(payload, MAX_SUMMARY_CHARS)

    def test_file_scope_target_and_referenced_repo_may_differ(self):
        """For file scope, scope_target is a file path; referenced_repo is the alias.
        They are distinct fields and are not required to be equal."""
        payload = _file_payload(
            scope_target="src/deep/path/module.py",
            referenced_repo="different-repo-alias",
        )
        validate_create_payload(payload, MAX_SUMMARY_CHARS)


# ---------------------------------------------------------------------------
# Missing required top-level fields
# ---------------------------------------------------------------------------


class TestMissingRequiredFields:
    """validate_create_payload rejects payloads missing required top-level fields."""

    @pytest.mark.parametrize(
        "field",
        ["id", "type", "scope", "summary", "evidence", "created_by", "created_at"],
    )
    def test_missing_field_raises(self, field):
        payload = _global_payload()
        del payload[field]
        with pytest.raises(MemorySchemaValidationError) as exc_info:
            validate_create_payload(payload, MAX_SUMMARY_CHARS)
        assert exc_info.value.field == field


# ---------------------------------------------------------------------------
# Wrong enum values
# ---------------------------------------------------------------------------


class TestWrongEnumValues:
    """validate_create_payload rejects unknown enum values."""

    def test_unknown_type_raises(self):
        payload = _global_payload(type="unknown-type")
        with pytest.raises(MemorySchemaValidationError) as exc_info:
            validate_create_payload(payload, MAX_SUMMARY_CHARS)
        assert exc_info.value.field == "type"

    def test_unknown_scope_raises(self):
        payload = _global_payload(scope="domain")
        with pytest.raises(MemorySchemaValidationError) as exc_info:
            validate_create_payload(payload, MAX_SUMMARY_CHARS)
        assert exc_info.value.field == "scope"


# ---------------------------------------------------------------------------
# Scope / target consistency rules
# ---------------------------------------------------------------------------


class TestScopeTargetConsistency:
    """Scope-target / referenced_repo consistency rules are enforced.

    Rules:
      - scope=global: scope_target MUST be null; referenced_repo MUST be null
      - scope=repo:   scope_target MUST be non-null; referenced_repo MUST be non-null
      - scope=file:   scope_target MUST be non-null; referenced_repo MUST be non-null
      - scope_target and referenced_repo values are independent (no equality requirement)
    """

    def test_global_scope_with_non_null_scope_target_raises(self):
        """scope=global with non-null scope_target is invalid."""
        payload = _global_payload(scope_target="some-repo")
        with pytest.raises(MemorySchemaValidationError) as exc_info:
            validate_create_payload(payload, MAX_SUMMARY_CHARS)
        assert exc_info.value.field == "scope_target"

    def test_global_scope_with_non_null_referenced_repo_raises(self):
        """scope=global with non-null referenced_repo is invalid."""
        payload = _global_payload(referenced_repo="some-repo")
        with pytest.raises(MemorySchemaValidationError) as exc_info:
            validate_create_payload(payload, MAX_SUMMARY_CHARS)
        assert exc_info.value.field == "referenced_repo"

    @pytest.mark.parametrize(
        "scope, base_payload_fn, field",
        [
            ("repo", _repo_payload, "scope_target"),
            ("repo", _repo_payload, "referenced_repo"),
            ("file", _file_payload, "scope_target"),
            ("file", _file_payload, "referenced_repo"),
        ],
    )
    def test_non_global_scope_null_required_field_raises(
        self, scope, base_payload_fn, field
    ):
        """scope=repo and scope=file require non-null scope_target and referenced_repo."""
        payload = base_payload_fn(**{field: None})
        with pytest.raises(MemorySchemaValidationError) as exc_info:
            validate_create_payload(payload, MAX_SUMMARY_CHARS)
        assert exc_info.value.field == field


# ---------------------------------------------------------------------------
# Summary cap
# ---------------------------------------------------------------------------


class TestSummaryCap:
    """Summary exceeding max_summary_chars is rejected."""

    def test_summary_over_limit_raises(self):
        payload = _global_payload(summary=OVER_LIMIT_SUMMARY)
        with pytest.raises(MemorySchemaValidationError) as exc_info:
            validate_create_payload(payload, MAX_SUMMARY_CHARS)
        assert exc_info.value.field == "summary"

    def test_summary_cap_respected_at_custom_limit(self):
        """Custom max_summary_chars is honoured."""
        payload = _global_payload(summary=CUSTOM_SUMMARY_OVER_LIMIT)
        with pytest.raises(MemorySchemaValidationError) as exc_info:
            validate_create_payload(payload, CUSTOM_SUMMARY_CAP)
        assert exc_info.value.field == "summary"


# ---------------------------------------------------------------------------
# Evidence list bounds
# ---------------------------------------------------------------------------


class TestEvidenceBounds:
    """Evidence list must have 1 to MAX_EVIDENCE_ENTRIES entries."""

    def test_empty_evidence_raises(self):
        payload = _global_payload(evidence=[])
        with pytest.raises(MemorySchemaValidationError) as exc_info:
            validate_create_payload(payload, MAX_SUMMARY_CHARS)
        assert exc_info.value.field == "evidence"

    def test_evidence_over_max_entries_raises(self):
        entries = [{"commit": f"abc{i:04d}"} for i in range(OVER_MAX_EVIDENCE_ENTRIES)]
        payload = _global_payload(evidence=entries)
        with pytest.raises(MemorySchemaValidationError) as exc_info:
            validate_create_payload(payload, MAX_SUMMARY_CHARS)
        assert exc_info.value.field == "evidence"

    def test_evidence_not_a_list_raises(self):
        """evidence must be a list, not a string or dict."""
        payload = _global_payload(evidence="not-a-list")
        with pytest.raises(MemorySchemaValidationError) as exc_info:
            validate_create_payload(payload, MAX_SUMMARY_CHARS)
        assert exc_info.value.field == "evidence"


# ---------------------------------------------------------------------------
# Evidence entry format
# ---------------------------------------------------------------------------


class TestEvidenceEntryFormat:
    """Each evidence entry must be a valid {file+lines} or {commit} dict."""

    def test_evidence_entry_missing_both_file_and_commit_raises(self):
        """Entry with neither 'file' nor 'commit' key is rejected."""
        payload = _global_payload(evidence=[{"path": "src/foo.py"}])
        with pytest.raises(MemorySchemaValidationError) as exc_info:
            validate_create_payload(payload, MAX_SUMMARY_CHARS)
        assert exc_info.value.field == "evidence"

    def test_evidence_file_entry_missing_lines_raises(self):
        """File entry without 'lines' key is rejected."""
        payload = _global_payload(evidence=[{"file": "src/foo.py"}])
        with pytest.raises(MemorySchemaValidationError) as exc_info:
            validate_create_payload(payload, MAX_SUMMARY_CHARS)
        assert exc_info.value.field == "evidence"

    def test_evidence_entry_not_a_dict_raises(self):
        """Evidence entry that is not a dict is rejected."""
        payload = _global_payload(evidence=["just-a-string"])
        with pytest.raises(MemorySchemaValidationError) as exc_info:
            validate_create_payload(payload, MAX_SUMMARY_CHARS)
        assert exc_info.value.field == "evidence"


# ---------------------------------------------------------------------------
# Evidence entry exclusive union enforcement
# ---------------------------------------------------------------------------


class TestEvidenceEntryExclusiveUnion:
    """Evidence entries must be exclusively {file+lines} or {commit} — not mixed or extended."""

    def test_mixed_file_and_commit_raises(self):
        """Entry with both 'file'+'lines' and 'commit' keys is rejected."""
        payload = _global_payload(
            evidence=[{"file": "src/foo.py", "lines": "1-5", "commit": "abc123"}]
        )
        with pytest.raises(MemorySchemaValidationError) as exc_info:
            validate_create_payload(payload, MAX_SUMMARY_CHARS)
        assert exc_info.value.field == "evidence"

    def test_file_entry_with_extra_key_raises(self):
        """File entry with an unrecognised extra key is rejected — only {file, lines} allowed."""
        payload = _global_payload(
            evidence=[{"file": "src/foo.py", "lines": "1-5", "extra": "unexpected"}]
        )
        with pytest.raises(MemorySchemaValidationError) as exc_info:
            validate_create_payload(payload, MAX_SUMMARY_CHARS)
        assert exc_info.value.field == "evidence"

    def test_commit_entry_with_extra_key_raises(self):
        """Commit entry with an unrecognised extra key is rejected — only {commit} allowed."""
        payload = _global_payload(evidence=[{"commit": "abc123", "lines": "1-5"}])
        with pytest.raises(MemorySchemaValidationError) as exc_info:
            validate_create_payload(payload, MAX_SUMMARY_CHARS)
        assert exc_info.value.field == "evidence"


# ---------------------------------------------------------------------------
# Evidence entry value validation
# ---------------------------------------------------------------------------


class TestEvidenceEntryValues:
    """Evidence entry field values must be non-empty strings in correct formats."""

    @pytest.mark.parametrize(
        "lines_value",
        [None, "", 123, "not-a-range"],
        ids=["none", "empty-string", "integer", "no-hyphen"],
    )
    def test_invalid_lines_value_raises(self, lines_value):
        """lines must be a non-empty string matching '<start>-<end>' format."""
        payload = _global_payload(
            evidence=[{"file": "src/foo.py", "lines": lines_value}]
        )
        with pytest.raises(MemorySchemaValidationError) as exc_info:
            validate_create_payload(payload, MAX_SUMMARY_CHARS)
        assert exc_info.value.field == "evidence"

    @pytest.mark.parametrize(
        "commit_value",
        [None, "", 12345],
        ids=["none", "empty-string", "integer"],
    )
    def test_invalid_commit_value_raises(self, commit_value):
        """commit must be a non-empty string."""
        payload = _global_payload(evidence=[{"commit": commit_value}])
        with pytest.raises(MemorySchemaValidationError) as exc_info:
            validate_create_payload(payload, MAX_SUMMARY_CHARS)
        assert exc_info.value.field == "evidence"


# ---------------------------------------------------------------------------
# max_summary_chars parameter validation
# ---------------------------------------------------------------------------


class TestMaxSummaryCharsValidation:
    """validate_create_payload and validate_edit_payload reject invalid max_summary_chars.

    Both public entry points share the same guard. The validator raises:
      - ValueError for negative values (out of range)
      - ValueError for non-integer types (wrong type — validator converts to ValueError
        for a uniform contract regardless of input type)
      - zero is valid as a cap: any non-empty summary exceeds it
    """

    def test_negative_max_summary_chars_raises_value_error_on_create(self):
        """Negative max_summary_chars raises ValueError from validate_create_payload."""
        payload = _global_payload()
        with pytest.raises(ValueError):
            validate_create_payload(payload, -1)

    def test_non_integer_max_summary_chars_raises_value_error_on_create(self):
        """Non-integer max_summary_chars raises ValueError from validate_create_payload."""
        payload = _global_payload()
        with pytest.raises(ValueError):
            validate_create_payload(payload, "one-thousand")

    def test_zero_max_summary_chars_rejects_non_empty_summary(self):
        """max_summary_chars=0 is valid as a cap; any non-empty summary exceeds it."""
        payload = _global_payload(summary="x")
        with pytest.raises(MemorySchemaValidationError) as exc_info:
            validate_create_payload(payload, 0)
        assert exc_info.value.field == "summary"

    def test_negative_max_summary_chars_raises_value_error_on_edit(self):
        """validate_edit_payload also rejects negative max_summary_chars."""
        current = _global_payload()
        edit = {
            "summary": "Updated.",
            "edited_by": "editor",
            "edited_at": VALID_ISO8601_2,
        }
        with pytest.raises(ValueError):
            validate_edit_payload(edit, current, -1)


# ---------------------------------------------------------------------------
# validate_edit_payload — happy path
# ---------------------------------------------------------------------------


class TestEditHappyPath:
    """validate_edit_payload accepts valid edits."""

    def test_edit_summary_accepted(self):
        """Editing only summary is accepted."""
        current = _global_payload()
        edit = {
            "summary": "Updated summary.",
            "edited_by": "editor",
            "edited_at": VALID_ISO8601_2,
        }
        validate_edit_payload(edit, current, MAX_SUMMARY_CHARS)

    def test_edit_evidence_accepted(self):
        """Editing evidence list is accepted."""
        current = _repo_payload()
        edit = {
            "evidence": [{"commit": "newcommit"}],
            "edited_by": "editor",
            "edited_at": VALID_ISO8601_2,
        }
        validate_edit_payload(edit, current, MAX_SUMMARY_CHARS)

    def test_edit_type_accepted(self):
        """type is mutable on edit — changing it is accepted."""
        current = _global_payload()
        edit = {
            "type": "performance-note",
            "edited_by": "editor",
            "edited_at": VALID_ISO8601_2,
        }
        validate_edit_payload(edit, current, MAX_SUMMARY_CHARS)

    def test_edit_scope_accepted(self):
        """scope is mutable on edit — changing it is accepted."""
        current = _global_payload()
        edit = {
            "scope": "repo",
            "scope_target": "some-repo",
            "referenced_repo": "some-repo",
            "edited_by": "editor",
            "edited_at": VALID_ISO8601_2,
        }
        validate_edit_payload(edit, current, MAX_SUMMARY_CHARS)


# ---------------------------------------------------------------------------
# validate_edit_payload — immutable field protection
# ---------------------------------------------------------------------------


class TestEditImmutableFields:
    """validate_edit_payload rejects changes to immutable fields.

    Immutable on edit (per Story #877 spec): id, created_by, created_at
    These are server-controlled on create and must never change afterward.
    All other fields (type, scope, scope_target, referenced_repo, summary,
    evidence) are mutable on edit.
    """

    @pytest.mark.parametrize(
        "field, new_value",
        [
            ("id", VALID_UUID_2),
            ("created_by", "different-agent"),
            ("created_at", VALID_ISO8601_2),
        ],
    )
    def test_changing_immutable_field_raises(self, field, new_value):
        """Attempting to change any immutable field in an edit payload raises on that field."""
        current = _global_payload()
        edit = {
            field: new_value,
            "summary": "New summary.",
            "edited_by": "editor",
            "edited_at": VALID_ISO8601_2,
        }
        with pytest.raises(MemorySchemaValidationError) as exc_info:
            validate_edit_payload(edit, current, MAX_SUMMARY_CHARS)
        assert exc_info.value.field == field
