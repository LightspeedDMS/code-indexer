"""
Unit tests for Story #1400 ConfigService changes:

- CRITICAL 6 (FINAL LOCKED DESIGN): validate-copy-then-publish atomicity.
  A rejected update must leave BOTH the persisted value AND the live
  in-memory get_config() value unchanged -- not just the persisted one.
- temporal_inline_wait_seconds wired into search_timeouts (float field).
- temporal_lane_concurrency wired into background_jobs.

TDD: written BEFORE implementation.
"""

import pytest

from code_indexer.server.services.config_service import ConfigService


def _make_service(tmp_path) -> ConfigService:
    return ConfigService(server_dir_path=str(tmp_path))


class TestSearchTimeoutsTemporalInlineWaitWiring:
    def test_section_includes_temporal_inline_wait_seconds(self, tmp_path):
        svc = _make_service(tmp_path)
        section = svc.get_all_settings()["search_timeouts"]
        assert section["temporal_inline_wait_seconds"] == 60.0

    def test_update_accepts_float_value(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.update_setting("search_timeouts", "temporal_inline_wait_seconds", 30.5)
        assert (
            svc.get_config().search_timeouts_config.temporal_inline_wait_seconds == 30.5
        )

    def test_update_persists_float_across_reload(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.update_setting("search_timeouts", "temporal_inline_wait_seconds", 12.25)
        svc2 = _make_service(tmp_path)
        assert (
            svc2.get_config().search_timeouts_config.temporal_inline_wait_seconds
            == 12.25
        )


class TestBackgroundJobsTemporalLaneConcurrencyWiring:
    def test_update_temporal_lane_concurrency(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.update_setting("background_jobs", "temporal_lane_concurrency", 5)
        assert svc.get_config().background_jobs_config.temporal_lane_concurrency == 5

    def test_out_of_range_temporal_lane_concurrency_rejected(self, tmp_path):
        svc = _make_service(tmp_path)
        with pytest.raises(ValueError):
            svc.update_setting("background_jobs", "temporal_lane_concurrency", 999)


class TestAtomicConfigPublishCritical6:
    """CRITICAL 6: validate against a COPY, publish atomically only on
    success. The mandatory regression test from the locked design: after a
    REJECTED update, BOTH the persisted value AND the live in-memory
    get_config() value must be unchanged."""

    def test_rejected_update_leaves_live_config_unchanged(self, tmp_path):
        svc = _make_service(tmp_path)
        original = (
            svc.get_config().search_timeouts_config.search_code_handler_timeout_seconds
        )
        assert original == 180

        with pytest.raises(ValueError):
            svc.update_setting(
                "search_timeouts", "search_code_handler_timeout_seconds", 5000
            )

        # The exact gap CRITICAL 6 found: update_setting's dispatch used to
        # mutate the SHARED live config object in place BEFORE validating,
        # so a rejected value could remain live even though it was never
        # persisted. Assert the live in-memory value is untouched.
        assert (
            svc.get_config().search_timeouts_config.search_code_handler_timeout_seconds
            == original
        )

    def test_rejected_update_leaves_persisted_value_unchanged(self, tmp_path):
        svc = _make_service(tmp_path)
        with pytest.raises(ValueError):
            svc.update_setting(
                "search_timeouts", "search_code_handler_timeout_seconds", 5000
            )

        svc2 = _make_service(tmp_path)
        assert (
            svc2.get_config().search_timeouts_config.search_code_handler_timeout_seconds
            == 180
        )

    def test_rejected_temporal_inline_wait_seconds_leaves_live_config_unchanged(
        self, tmp_path
    ):
        """Same guarantee for the new float field specifically (Scenario 9)."""
        svc = _make_service(tmp_path)
        original = svc.get_config().search_timeouts_config.temporal_inline_wait_seconds

        with pytest.raises(ValueError):
            # 200 >= 180 - 1.0 grace ceiling -> rejected
            svc.update_setting("search_timeouts", "temporal_inline_wait_seconds", 200.0)

        assert (
            svc.get_config().search_timeouts_config.temporal_inline_wait_seconds
            == original
        )

    def test_successful_update_still_publishes(self, tmp_path):
        """Atomicity must not break the ordinary success path."""
        svc = _make_service(tmp_path)
        svc.update_setting(
            "search_timeouts", "search_code_handler_timeout_seconds", 240
        )
        assert (
            svc.get_config().search_timeouts_config.search_code_handler_timeout_seconds
            == 240
        )


class TestUpdateSettingsAtomicBatch:
    """New update_settings_atomic(updates) primitive: applies a whole list
    of (category, key, value) tuples as ONE validated unit -- either all
    apply and publish, or none do."""

    def test_batch_all_valid_applies_all(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.update_settings_atomic(
            [
                ("search_timeouts", "search_code_handler_timeout_seconds", 200),
                ("search_timeouts", "default_handler_timeout_seconds", 45),
            ]
        )
        config = svc.get_config().search_timeouts_config
        assert config.search_code_handler_timeout_seconds == 200
        assert config.default_handler_timeout_seconds == 45

    def test_batch_one_invalid_rejects_whole_batch(self, tmp_path):
        svc = _make_service(tmp_path)
        original = (
            svc.get_config().search_timeouts_config.default_handler_timeout_seconds
        )
        with pytest.raises(ValueError):
            svc.update_settings_atomic(
                [
                    ("search_timeouts", "default_handler_timeout_seconds", 45),
                    (
                        "search_timeouts",
                        "search_code_handler_timeout_seconds",
                        5000,
                    ),  # invalid -> whole batch rejected
                ]
            )
        # Bug this guards against: partial application of a multi-setting
        # batch (e.g. a Web UI section save) when a LATER item in the list
        # fails validation.
        assert (
            svc.get_config().search_timeouts_config.default_handler_timeout_seconds
            == original
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
