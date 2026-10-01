"""
Unit tests for the shared `parse_json_column()` helper
(server/storage/json_column.py).

Code-review remediation (F3, on the #1652/#1655 commit): this exact
"accept a value already of the expected type as-is, else json.loads()
str/bytes, fail-soft on anything malformed" pattern had drifted into
three near-identical implementations:

  - dependency_map_routes.py's _parse_phase_timings_value (Bug #1622)
  - wiki_cache.py's _parse_json_cache_value (Bug #1652)
  - activated_repo_manager.py's inlined isinstance/json.loads logic
    (_pg_row_to_metadata, lines ~507-512)

This is the Messi anti-duplication three-strike rule: extract ONE shared
helper. All three call sites are refactored to call parse_json_column()
directly; this file is the canonical, comprehensive test suite for the
helper itself. The per-call-site test files retain only integration-level
coverage (that the call site actually wires the helper in correctly),
not a second copy of these unit-level cases.

Also covers F5: the WARNING log must truncate the raw value's repr() to
exactly 200 characters so a multi-hundred-KB JSONB blob (e.g. a large
wiki sidebar) never gets dumped whole into logs.db. The truncation tests
assert the COMPLETE expected log message (not substring containment) so
an incorrect prefix/suffix/field name cannot slip through.
"""

import json
import logging

_MODULE = "code_indexer.server.storage.json_column"
_MAX_LOGGED_VALUE_LENGTH = 200


def _warnings(caplog):
    return [r for r in caplog.records if r.name == _MODULE]


class TestParseJsonColumnPassthrough:
    """None passthrough, and the core fix: matching-type passthrough."""

    def test_none_value_returns_none_without_warning(self, caplog):
        from code_indexer.server.storage.json_column import parse_json_column

        with caplog.at_level(logging.WARNING, logger=_MODULE):
            result = parse_json_column(None, dict, "phase_timings_json")

        assert result is None
        assert _warnings(caplog) == []

    def test_dict_value_returned_as_is_without_warning(self, caplog):
        """A dict value (PostgreSQL JSONB) must be used directly."""
        from code_indexer.server.storage.json_column import parse_json_column

        raw = {"detect_s": 54.26505970954895, "merge_s": 0.0066}
        with caplog.at_level(logging.WARNING, logger=_MODULE):
            result = parse_json_column(raw, dict, "phase_timings_json")

        assert result == raw
        assert result is raw
        assert _warnings(caplog) == []

    def test_list_value_returned_as_is_without_warning(self, caplog):
        """A list value (PostgreSQL JSONB sidebar_json) must be used directly."""
        from code_indexer.server.storage.json_column import parse_json_column

        raw = [{"title": "Home", "path": "home", "children": []}]
        with caplog.at_level(logging.WARNING, logger=_MODULE):
            result = parse_json_column(raw, list, "sidebar_json")

        assert result == raw
        assert result is raw
        assert _warnings(caplog) == []


class TestParseJsonColumnStringAndBytes:
    """Regression: SQLite's TEXT column (str, and str's bytes cousin)."""

    def test_json_string_dict_still_parses_without_warning(self, caplog):
        from code_indexer.server.storage.json_column import parse_json_column

        with caplog.at_level(logging.WARNING, logger=_MODULE):
            result = parse_json_column(
                '{"detect_s": 0.5, "merge_s": 94.1}', dict, "phase_timings_json"
            )

        assert result == {"detect_s": 0.5, "merge_s": 94.1}
        assert _warnings(caplog) == []

    def test_bytes_value_still_parses_without_warning(self, caplog):
        """
        json.loads() also accepts bytes — the dict short-circuit must not
        regress this branch.
        """
        from code_indexer.server.storage.json_column import parse_json_column

        with caplog.at_level(logging.WARNING, logger=_MODULE):
            result = parse_json_column(
                b'{"refine_s": 61.0}', dict, "phase_timings_json"
            )

        assert result == {"refine_s": 61.0}
        assert _warnings(caplog) == []

    def test_json_string_list_still_parses_without_warning(self, caplog):
        from code_indexer.server.storage.json_column import parse_json_column

        with caplog.at_level(logging.WARNING, logger=_MODULE):
            result = parse_json_column(
                '[{"title": "Home", "path": "home"}]', list, "sidebar_json"
            )

        assert result == [{"title": "Home", "path": "home"}]
        assert _warnings(caplog) == []


class TestParseJsonColumnMalformedJson:
    """Regression: a string that fails json.loads() (or an unexpected
    scalar type) must WARN with the exact expected message and return
    None."""

    def test_malformed_json_string_warns_with_exact_message(self, caplog):
        from code_indexer.server.storage.json_column import parse_json_column

        raw = "not-valid-json{"
        try:
            _ = json.loads(raw)
            raise AssertionError("fixture string must be invalid JSON")
        except (ValueError, TypeError) as exc:
            expected_exc_text = str(exc)

        with caplog.at_level(logging.WARNING, logger=_MODULE):
            result = parse_json_column(raw, dict, "phase_timings_json")

        assert result is None
        records = _warnings(caplog)
        assert len(records) == 1
        expected_message = (
            f"parse_json_column: malformed phase_timings_json value "
            f"{raw!r}: {expected_exc_text}"
        )
        assert records[0].getMessage() == expected_message

    def test_unexpected_scalar_type_warns_with_exact_message(self, caplog):
        """An int (neither expected_type nor str/bytes) must still hit the
        json.loads(TypeError) branch."""
        from code_indexer.server.storage.json_column import parse_json_column

        try:
            _ = json.loads(42)  # type: ignore[arg-type]
            raise AssertionError("fixture value must raise TypeError")
        except (ValueError, TypeError) as exc:
            expected_exc_text = str(exc)

        with caplog.at_level(logging.WARNING, logger=_MODULE):
            result = parse_json_column(42, dict, "phase_timings_json")

        assert result is None
        records = _warnings(caplog)
        assert len(records) == 1
        expected_message = (
            f"parse_json_column: malformed phase_timings_json value "
            f"{42!r}: {expected_exc_text}"
        )
        assert records[0].getMessage() == expected_message


class TestParseJsonColumnWrongShape:
    """Regression: valid JSON that parses to the wrong python type must
    WARN with the exact expected message and return None."""

    def test_json_array_when_dict_expected_warns_with_exact_message(self, caplog):
        from code_indexer.server.storage.json_column import parse_json_column

        raw = "[1, 2, 3]"
        with caplog.at_level(logging.WARNING, logger=_MODULE):
            result = parse_json_column(raw, dict, "phase_timings_json")

        assert result is None
        records = _warnings(caplog)
        assert len(records) == 1
        expected_message = (
            f"parse_json_column: phase_timings_json parsed to list "
            f"(expected dict), ignoring value {raw!r}"
        )
        assert records[0].getMessage() == expected_message

    def test_json_object_when_list_expected_warns_with_exact_message(self, caplog):
        from code_indexer.server.storage.json_column import parse_json_column

        raw = '{"not": "a list"}'
        with caplog.at_level(logging.WARNING, logger=_MODULE):
            result = parse_json_column(raw, list, "sidebar_json")

        assert result is None
        records = _warnings(caplog)
        assert len(records) == 1
        expected_message = (
            f"parse_json_column: sidebar_json parsed to dict "
            f"(expected list), ignoring value {raw!r}"
        )
        assert records[0].getMessage() == expected_message


class TestParseJsonColumnWarningTruncation:
    """F5: the raw value's repr() logged in a WARNING must be capped at
    exactly 200 characters. Assertions reconstruct the COMPLETE expected
    log message so neither a longer nor a shorter truncation, nor an
    unbounded suffix, can slip through."""

    def test_malformed_string_warning_message_is_exactly_truncated(self, caplog):
        from code_indexer.server.storage.json_column import parse_json_column

        huge_malformed = "{" + ("x" * 5000)
        try:
            _ = json.loads(huge_malformed)
            raise AssertionError("fixture string must be invalid JSON")
        except (ValueError, TypeError) as exc:
            expected_exc_text = str(exc)
        raw_repr_truncated = repr(huge_malformed)[:_MAX_LOGGED_VALUE_LENGTH]
        assert len(raw_repr_truncated) == _MAX_LOGGED_VALUE_LENGTH

        with caplog.at_level(logging.WARNING, logger=_MODULE):
            result = parse_json_column(huge_malformed, dict, "sidebar_json")

        assert result is None
        records = _warnings(caplog)
        assert len(records) == 1
        expected_message = (
            f"parse_json_column: malformed sidebar_json value "
            f"{raw_repr_truncated}: {expected_exc_text}"
        )
        assert records[0].getMessage() == expected_message

    def test_wrong_shape_warning_message_is_exactly_truncated(self, caplog):
        from code_indexer.server.storage.json_column import parse_json_column

        huge_array = "[" + ",".join(["9"] * 5000) + "]"
        raw_repr_truncated = repr(huge_array)[:_MAX_LOGGED_VALUE_LENGTH]
        assert len(raw_repr_truncated) == _MAX_LOGGED_VALUE_LENGTH

        with caplog.at_level(logging.WARNING, logger=_MODULE):
            result = parse_json_column(huge_array, dict, "phase_timings_json")

        assert result is None
        records = _warnings(caplog)
        assert len(records) == 1
        expected_message = (
            f"parse_json_column: phase_timings_json parsed to list "
            f"(expected dict), ignoring value {raw_repr_truncated}"
        )
        assert records[0].getMessage() == expected_message
