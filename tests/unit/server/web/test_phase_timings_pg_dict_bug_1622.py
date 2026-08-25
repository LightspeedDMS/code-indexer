"""
Unit tests for Bug #1622: Dep-Map dashboard spurious WARNINGs when
phase_timings_json is already a dict (PostgreSQL) instead of a JSON string
(SQLite).

_render_complete_response() in dependency_map_routes.py assumed
row["phase_timings_json"] was always a JSON string and called json.loads()
unconditionally. On PostgreSQL the JSONB column is deserialized by the
driver into a python dict already, so json.loads(dict) raised TypeError on
every run-history row, logging a spurious WARNING and leaving
phase_timings_parsed = None (per-phase pills never rendered, falling back
to the coarse P1/P2 pills).

Tests target the extracted `_parse_phase_timings_value()` helper directly
(dependency_map_routes.py) — the smallest correct unit boundary for this
fix. This also allows exercising a `bytes` input directly: bytes has no
representation inside the dashboard's JSON-serialized result_json cache
envelope, so it could never be reproduced through the full HTTP endpoint.
"""

import logging

_ROUTES = "code_indexer.server.web.dependency_map_routes"
# Matches both WARNING message variants _parse_phase_timings_value logs:
# "malformed phase_timings_json %r: %s" (json.loads failure) and
# "phase_timings_json parsed to %s (expected dict), ignoring: %r"
# (valid JSON that isn't a dict, e.g. a JSON array).
_WARNING_SUBSTRING = "phase_timings_json"


def _warnings(caplog):
    return [r for r in caplog.records if _WARNING_SUBSTRING in r.getMessage()]


class TestParsePhaseTimingsValueNoneAndDict:
    """None passthrough, and Bug #1622's core fix: dict passthrough."""

    def test_none_value_returns_none_without_warning(self, caplog):
        from code_indexer.server.web.dependency_map_routes import (
            _parse_phase_timings_value,
        )

        with caplog.at_level(logging.WARNING, logger=_ROUTES):
            result = _parse_phase_timings_value(None)

        assert result is None
        assert _warnings(caplog) == []

    def test_dict_value_returned_as_is_without_warning(self, caplog):
        """Bug #1622: a dict value (PostgreSQL JSONB) must be used directly."""
        from code_indexer.server.web.dependency_map_routes import (
            _parse_phase_timings_value,
        )

        raw = {"detect_s": 54.26505970954895, "merge_s": 0.0066}
        with caplog.at_level(logging.WARNING, logger=_ROUTES):
            result = _parse_phase_timings_value(raw)

        assert result == raw
        assert _warnings(caplog) == []


class TestParsePhaseTimingsValueStringAndBytes:
    """Regression: SQLite's TEXT column (str, and str's bytes cousin)."""

    def test_json_string_value_still_parses_without_warning(self, caplog):
        from code_indexer.server.web.dependency_map_routes import (
            _parse_phase_timings_value,
        )

        with caplog.at_level(logging.WARNING, logger=_ROUTES):
            result = _parse_phase_timings_value('{"detect_s": 0.5, "merge_s": 94.1}')

        assert result == {"detect_s": 0.5, "merge_s": 94.1}
        assert _warnings(caplog) == []

    def test_bytes_value_still_parses_without_warning(self, caplog):
        """
        json.loads() also accepts bytes — the dict short-circuit must not
        regress this branch. bytes cannot flow through the HTTP endpoint
        (JSON has no bytes type), so this unit-level call is the only
        boundary able to prove it.
        """
        from code_indexer.server.web.dependency_map_routes import (
            _parse_phase_timings_value,
        )

        with caplog.at_level(logging.WARNING, logger=_ROUTES):
            result = _parse_phase_timings_value(b'{"refine_s": 61.0}')

        assert result == {"refine_s": 61.0}
        assert _warnings(caplog) == []


class TestParsePhaseTimingsValueMalformed:
    """Regression: genuinely malformed values must still WARN and return None."""

    def test_malformed_json_string_warns_and_returns_none(self, caplog):
        from code_indexer.server.web.dependency_map_routes import (
            _parse_phase_timings_value,
        )

        with caplog.at_level(logging.WARNING, logger=_ROUTES):
            result = _parse_phase_timings_value("not-valid-json{")

        assert result is None
        assert len(_warnings(caplog)) == 1

    def test_unexpected_scalar_type_warns_and_returns_none(self, caplog):
        """An int (neither dict nor str/bytes) must still hit the warning path."""
        from code_indexer.server.web.dependency_map_routes import (
            _parse_phase_timings_value,
        )

        with caplog.at_level(logging.WARNING, logger=_ROUTES):
            result = _parse_phase_timings_value(42)

        assert result is None
        assert len(_warnings(caplog)) == 1

    def test_non_dict_json_value_warns_and_returns_none(self, caplog):
        """A JSON array (parses fine, but isn't a dict) must still warn."""
        from code_indexer.server.web.dependency_map_routes import (
            _parse_phase_timings_value,
        )

        with caplog.at_level(logging.WARNING, logger=_ROUTES):
            result = _parse_phase_timings_value("[1, 2, 3]")

        assert result is None
        assert len(_warnings(caplog)) == 1
