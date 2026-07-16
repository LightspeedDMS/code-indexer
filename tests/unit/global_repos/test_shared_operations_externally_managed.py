"""
Unit tests for GlobalRepoOperations.get_config() surfacing externally_managed
(EVO-64493). This is the read-path the RefreshScheduler uses to decide whether
golden repos are externally owned; it must surface the flag and default to
False when config cannot be loaded.
"""

from unittest.mock import Mock, patch

from code_indexer.global_repos.shared_operations import (
    GlobalRepoOperations,
    DEFAULT_REFRESH_INTERVAL,
)


def test_get_config_surfaces_externally_managed(tmp_path):
    ops = GlobalRepoOperations(str(tmp_path))
    fake_cfg = Mock()
    fake_cfg.golden_repos_config.refresh_interval_seconds = 3600
    fake_cfg.golden_repos_config.externally_managed = True
    fake_service = Mock()
    fake_service.get_config.return_value = fake_cfg

    with patch(
        "code_indexer.server.services.config_service.get_config_service",
        return_value=fake_service,
    ):
        cfg = ops.get_config()

    assert cfg["externally_managed"] is True
    assert cfg["refresh_interval"] == 3600


def test_get_config_defaults_externally_managed_false_on_error(tmp_path):
    ops = GlobalRepoOperations(str(tmp_path))

    with patch(
        "code_indexer.server.services.config_service.get_config_service",
        side_effect=RuntimeError("boom"),
    ):
        cfg = ops.get_config()

    assert cfg["externally_managed"] is False
    assert cfg["refresh_interval"] == DEFAULT_REFRESH_INTERVAL
