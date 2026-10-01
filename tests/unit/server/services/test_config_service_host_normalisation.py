"""
ConfigService host writes: one normalisation, change-only validation.

- Every layer normalises the host identically: surrounding whitespace is
  stripped once, and the stripped value is validated AND stored. Interior
  whitespace/line breaks are still rejected.
- Whole-config validation must not refuse an unrelated save because the
  persisted host predates the current validator: only a write that CHANGES
  the host validates it.
"""

import json

import pytest

from code_indexer.server.services.config_service import ConfigService

# A host persisted before host validation existed that the validator
# rejects (IPv6 zone id).
_LEGACY_HOST = "fe80::1%eth0"


def _make_service(tmp_path) -> ConfigService:
    return ConfigService(server_dir_path=str(tmp_path))


def _seed_legacy_host(tmp_path) -> ConfigService:
    svc = _make_service(tmp_path)
    svc.get_config()  # materialise config.json with defaults
    config_file = tmp_path / "config.json"
    data = json.loads(config_file.read_text())
    data["host"] = _LEGACY_HOST
    config_file.write_text(json.dumps(data))
    svc = _make_service(tmp_path)
    assert svc.get_config().host == _LEGACY_HOST
    return svc


class TestHostIsStrippedOnceAndStoredStripped:
    def test_padded_host_is_stored_stripped(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.update_setting("server", "host", "  192.0.2.10 \n")

        assert svc.get_config().host == "192.0.2.10"
        assert _make_service(tmp_path).get_config().host == "192.0.2.10"

    @pytest.mark.parametrize(
        "raw", [" 192.0.2.10\nExecStartPre=+/bin/true ", "host .example.com"]
    )
    def test_interior_whitespace_still_rejected(self, tmp_path, raw):
        svc = _make_service(tmp_path)
        original = svc.get_config().host
        with pytest.raises(ValueError):
            svc.update_setting("server", "host", raw)
        assert _make_service(tmp_path).get_config().host == original


class TestUnrelatedSavesWithLegacyHost:
    def test_unrelated_setting_saves_when_persisted_host_is_legacy(self, tmp_path):
        svc = _seed_legacy_host(tmp_path)

        svc.update_setting("server", "log_level", "DEBUG")

        reloaded = _make_service(tmp_path).get_config()
        assert reloaded.log_level == "DEBUG"
        assert reloaded.host == _LEGACY_HOST

    def test_resubmitting_the_unchanged_legacy_host_is_not_refused(self, tmp_path):
        svc = _seed_legacy_host(tmp_path)

        svc.update_settings_atomic(
            [("server", "host", _LEGACY_HOST), ("server", "log_level", "DEBUG")]
        )

        assert _make_service(tmp_path).get_config().log_level == "DEBUG"

    def test_save_all_settings_with_unchanged_legacy_host(self, tmp_path):
        svc = _seed_legacy_host(tmp_path)

        svc.save_all_settings({"server": {"log_level": "DEBUG"}})

        assert _make_service(tmp_path).get_config().log_level == "DEBUG"

    def test_changing_host_to_an_invalid_value_is_still_refused(self, tmp_path):
        svc = _seed_legacy_host(tmp_path)

        with pytest.raises(ValueError):
            svc.update_setting("server", "host", "fe80::2%eth1")

        assert _make_service(tmp_path).get_config().host == _LEGACY_HOST

    def test_changing_legacy_host_to_a_valid_value_succeeds(self, tmp_path):
        svc = _seed_legacy_host(tmp_path)

        svc.update_setting("server", "host", "host.example.com.")

        assert _make_service(tmp_path).get_config().host == "host.example.com."
