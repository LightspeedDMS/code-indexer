"""Unit tests for Story #926 config keys."""

import pytest


def test_cidx_meta_backup_config_defaults():
    """# Story #926 AC1: CidxMetaBackupConfig defaults to disabled with empty remote_url."""
    from code_indexer.server.utils.config_manager import CidxMetaBackupConfig

    cfg = CidxMetaBackupConfig()
    assert cfg.enabled is False
    assert cfg.remote_url == ""


@pytest.fixture
def config_service(tmp_path):
    from code_indexer.server.services.config_service import ConfigService

    server_dir = tmp_path / "cidx-server"
    server_dir.mkdir()
    return ConfigService(server_dir_path=str(server_dir))


def test_update_setting_cidx_meta_backup_enabled(config_service):
    """# Story #926 AC1: ConfigService persists cidx_meta_backup.enabled as a runtime DB-backed setting."""
    config_service.update_setting("cidx_meta_backup", "enabled", True)
    assert config_service.get_all_settings()["cidx_meta_backup"]["enabled"] is True


def test_update_setting_cidx_meta_backup_remote_url(config_service):
    """# Story #926 AC1: ConfigService persists cidx_meta_backup.remote_url."""
    remote_url = "file:///tmp/test.git"
    config_service.update_setting("cidx_meta_backup", "remote_url", remote_url)
    assert (
        config_service.get_all_settings()["cidx_meta_backup"]["remote_url"]
        == remote_url
    )


def test_unknown_cidx_meta_backup_key_raises(config_service):
    """# Story #926 AC1: unknown cidx_meta_backup setting keys raise ValueError."""
    with pytest.raises(ValueError, match="Unknown cidx_meta_backup setting"):
        config_service.update_setting("cidx_meta_backup", "nonexistent", "value")
