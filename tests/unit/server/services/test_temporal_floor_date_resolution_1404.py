"""Tests for temporal_floor_date resolution helpers (Story #1404).

Two pure/simple functions shared by all four corrected launch sites:

  - resolve_effective_floor_date(global_floor, per_repo_since_date): the
    "more restrictive wins" precedence rule (Scenario 6, spec-corrections
    item 2). The EFFECTIVE floor for any given repo/launch is
    max(global_floor_date, per_repo_since_date) -- the later/more
    restrictive date. Exactly one value is ever produced -- never two
    flags are emitted by a caller composing from this helper. If either is
    unset/None/empty, the other governs alone; if both are unset, the
    result is None (unbounded, pre-feature no-op, unchanged).

  - resolve_temporal_floor_date(): reads the global floor date from the
    DB-backed ServerConfig via get_config_service() (temporal_indexing_config
    .index_floor_date). Returns None when unset/empty.
"""

from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# resolve_effective_floor_date -- pure precedence logic
# ---------------------------------------------------------------------------


def _resolve(global_floor, per_repo_since_date):
    from code_indexer.server.services.temporal_floor_date import (
        resolve_effective_floor_date,
    )

    return resolve_effective_floor_date(global_floor, per_repo_since_date)


class TestResolveEffectiveFloorDateBothUnset:
    def test_both_none_returns_none(self) -> None:
        assert _resolve(None, None) is None

    def test_both_empty_string_returns_none(self) -> None:
        assert _resolve("", "") is None


class TestResolveEffectiveFloorDateOneUnset:
    def test_only_global_set_governs_alone(self) -> None:
        assert _resolve("2025-01-01", None) == "2025-01-01"

    def test_only_per_repo_set_governs_alone(self) -> None:
        assert _resolve(None, "2025-06-01") == "2025-06-01"

    def test_global_set_per_repo_empty_string(self) -> None:
        assert _resolve("2025-01-01", "") == "2025-01-01"

    def test_per_repo_set_global_empty_string(self) -> None:
        assert _resolve("", "2025-06-01") == "2025-06-01"


class TestResolveEffectiveFloorDateBothSetMoreRestrictiveWins:
    """Scenario 6: 'more restrictive wins' -- the later/newer date governs."""

    def test_per_repo_later_than_global_wins(self) -> None:
        # Given the global floor date is "2024-01-01"
        # And a golden repo has a per-repo since_date of "2025-06-01"
        # Then the effective since-date used is "2025-06-01"
        assert _resolve("2024-01-01", "2025-06-01") == "2025-06-01"

    def test_global_later_than_per_repo_wins(self) -> None:
        # Given the global floor date is "2025-06-01"
        # And the same repo's per-repo since_date is "2024-01-01"
        # Then the effective since-date used is "2025-06-01" (the global floor)
        assert _resolve("2025-06-01", "2024-01-01") == "2025-06-01"

    def test_equal_dates_returns_that_date(self) -> None:
        assert _resolve("2025-01-01", "2025-01-01") == "2025-01-01"


# ---------------------------------------------------------------------------
# resolve_temporal_floor_date -- reads global DB-backed config
# ---------------------------------------------------------------------------


class TestResolveTemporalFloorDate:
    def test_returns_configured_value(self) -> None:
        from code_indexer.server.services.temporal_floor_date import (
            resolve_temporal_floor_date,
        )
        from code_indexer.server.utils.config_manager import (
            ServerConfig,
            TemporalIndexingConfig,
        )

        fake_config = ServerConfig(
            server_dir="/tmp/fake",
            temporal_indexing_config=TemporalIndexingConfig(
                index_floor_date="2025-02-02"
            ),
        )
        fake_service = mock.MagicMock()
        fake_service.get_config.return_value = fake_config

        with mock.patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=fake_service,
        ):
            assert resolve_temporal_floor_date() == "2025-02-02"

    def test_returns_none_when_unset(self) -> None:
        from code_indexer.server.services.temporal_floor_date import (
            resolve_temporal_floor_date,
        )
        from code_indexer.server.utils.config_manager import (
            ServerConfig,
            TemporalIndexingConfig,
        )

        fake_config = ServerConfig(
            server_dir="/tmp/fake",
            temporal_indexing_config=TemporalIndexingConfig(index_floor_date=None),
        )
        fake_service = mock.MagicMock()
        fake_service.get_config.return_value = fake_config

        with mock.patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=fake_service,
        ):
            assert resolve_temporal_floor_date() is None

    def test_returns_none_when_temporal_indexing_config_is_none(self) -> None:
        """Defensive guard: even if temporal_indexing_config somehow ends up
        None (ServerConfig.__post_init__ normally guarantees a real
        TemporalIndexingConfig instance), resolution fails safe to
        unbounded rather than raising AttributeError."""
        from code_indexer.server.services.temporal_floor_date import (
            resolve_temporal_floor_date,
        )
        from code_indexer.server.utils.config_manager import ServerConfig

        fake_config = ServerConfig(server_dir="/tmp/fake")
        fake_config.temporal_indexing_config = None
        fake_service = mock.MagicMock()
        fake_service.get_config.return_value = fake_config

        with mock.patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=fake_service,
        ):
            assert resolve_temporal_floor_date() is None

    def test_returns_none_when_empty_string(self) -> None:
        from code_indexer.server.services.temporal_floor_date import (
            resolve_temporal_floor_date,
        )
        from code_indexer.server.utils.config_manager import (
            ServerConfig,
            TemporalIndexingConfig,
        )

        fake_config = ServerConfig(
            server_dir="/tmp/fake",
            temporal_indexing_config=TemporalIndexingConfig(index_floor_date=""),
        )
        fake_service = mock.MagicMock()
        fake_service.get_config.return_value = fake_config

        with mock.patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=fake_service,
        ):
            assert resolve_temporal_floor_date() is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
