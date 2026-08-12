"""Tests for IndexingWatchdogConfig (Issue #1530, Priority 3).

Covers: dataclass default value and constructor-kwarg override, plus
ServerConfig's __post_init__ auto-initialization wiring. Follow-up test
classes (dict->dataclass deserialization, validate_config() range check)
land in subsequent RED/GREEN steps.
"""

import pytest

from code_indexer.server.utils.config_manager import (
    IndexingWatchdogConfig,
    ServerConfig,
)


class TestIndexingWatchdogConfigDefaults:
    def test_default_stale_activity_timeout_seconds(self) -> None:
        config = IndexingWatchdogConfig()
        assert config.stale_activity_timeout_seconds == 120.0

    def test_overridable_via_constructor_kwarg(self) -> None:
        config = IndexingWatchdogConfig(stale_activity_timeout_seconds=60.0)
        assert config.stale_activity_timeout_seconds == 60.0


class TestServerConfigWiresIndexingWatchdogConfig:
    def test_auto_initializes_to_real_instance_when_none(self) -> None:
        server_config = ServerConfig(server_dir="/tmp/does-not-matter")
        assert isinstance(
            server_config.indexing_watchdog_config, IndexingWatchdogConfig
        )
        assert (
            server_config.indexing_watchdog_config.stale_activity_timeout_seconds
            == 120.0
        )

    def test_preserves_explicitly_passed_instance(self) -> None:
        custom = IndexingWatchdogConfig(stale_activity_timeout_seconds=45.0)
        server_config = ServerConfig(
            server_dir="/tmp/does-not-matter", indexing_watchdog_config=custom
        )
        assert server_config.indexing_watchdog_config is custom


class TestDictToServerConfigDeserializesIndexingWatchdog:
    """Bug #1368-class regression: _dict_to_server_config must convert a
    raw indexing_watchdog_config dict (as loaded from the runtime DB's JSON
    column) into a real IndexingWatchdogConfig instance -- without this,
    `cfg.stale_activity_timeout_seconds` raises AttributeError on every
    real cluster/solo deployment.
    """

    def test_dict_to_server_config_deserializes_indexing_watchdog_config(
        self, tmp_path
    ) -> None:
        from code_indexer.server.utils.config_manager import ServerConfigManager

        manager = ServerConfigManager(str(tmp_path))
        config_dict = {
            "server_dir": str(tmp_path),
            "indexing_watchdog_config": {"stale_activity_timeout_seconds": 90.0},
        }
        config = manager._dict_to_server_config(config_dict)

        assert isinstance(config.indexing_watchdog_config, IndexingWatchdogConfig), (
            "indexing_watchdog_config must be a real IndexingWatchdogConfig "
            "instance, not a plain dict"
        )
        assert config.indexing_watchdog_config.stale_activity_timeout_seconds == 90.0


def _watchdog_range_manager(tmp_path):
    from code_indexer.server.utils.config_manager import ServerConfigManager

    return ServerConfigManager(str(tmp_path))


def _watchdog_range_base_config(tmp_path) -> ServerConfig:
    return ServerConfig(server_dir=str(tmp_path))


class TestValidateConfigEnforcesIndexingWatchdogRange:
    """Mirrors search_timeouts_config's validate_config min/max range
    pattern -- 0/negative/absurdly-large values must be rejected loudly.
    """

    def test_valid_default_passes_validation(self, tmp_path) -> None:
        manager = _watchdog_range_manager(tmp_path)
        config = _watchdog_range_base_config(tmp_path)
        manager.validate_config(config)  # must not raise

    @pytest.mark.parametrize("bad_value", [0.0, -1.0])
    def test_zero_or_negative_stale_activity_timeout_rejected(
        self, tmp_path, bad_value
    ) -> None:
        manager = _watchdog_range_manager(tmp_path)
        config = _watchdog_range_base_config(tmp_path)
        config.indexing_watchdog_config.stale_activity_timeout_seconds = bad_value
        with pytest.raises(ValueError):
            manager.validate_config(config)

    def test_absurdly_large_stale_activity_timeout_rejected(self, tmp_path) -> None:
        manager = _watchdog_range_manager(tmp_path)
        config = _watchdog_range_base_config(tmp_path)
        # Bug #1218 requires this to remain a DETECT-staleness threshold,
        # never a job-duration timeout -- an absurdly large value would
        # defeat the watchdog's entire purpose (never detecting anything).
        config.indexing_watchdog_config.stale_activity_timeout_seconds = 999999.0
        with pytest.raises(ValueError):
            manager.validate_config(config)


class TestIndexingWatchdogValidationBlockPlacement:
    """Regression guard: the Issue #1530 validation block must be a
    SIBLING of the Story #1398/#1400 `if config.search_timeouts_config:`
    block -- never spliced INTO the middle of it.

    A first implementation inserted it between the
    `temporal_inline_wait_seconds >= 0.0` check and the
    `_temporal_grace_ceiling` cross-field check, which silently
    re-parented that Story #1400 check under
    `if config.indexing_watchdog_config:` -- gating an unrelated
    validation on the wrong config object AND leaving `st` unbound when
    search_timeouts_config is absent.
    """

    def test_validate_config_without_search_timeouts_config_does_not_crash(
        self, tmp_path
    ) -> None:
        manager = _watchdog_range_manager(tmp_path)
        config = _watchdog_range_base_config(tmp_path)
        config.search_timeouts_config = None

        # Must cleanly skip the search-timeouts validations, not blow up
        # with UnboundLocalError from a re-parented `st` reference.
        manager.validate_config(config)

    def test_temporal_grace_budget_check_still_fires_without_watchdog_config(
        self, tmp_path
    ) -> None:
        from code_indexer.server.utils.config_manager import (
            TEMPORAL_RESPONSE_RESERVE_SECONDS,
        )

        manager = _watchdog_range_manager(tmp_path)
        config = _watchdog_range_base_config(tmp_path)
        st = config.search_timeouts_config
        assert st is not None
        # Deliberately over the Story #1400 grace ceiling.
        st.temporal_inline_wait_seconds = float(
            st.search_code_handler_timeout_seconds
            - TEMPORAL_RESPONSE_RESERVE_SECONDS
            + 1.0
        )
        # The watchdog config is unrelated to that check; its absence must
        # never suppress it.
        config.indexing_watchdog_config = None

        with pytest.raises(ValueError, match="grace budget"):
            manager.validate_config(config)
