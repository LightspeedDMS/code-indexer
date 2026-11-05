"""Unit tests for build_scip_generator (EVO-65021 scip backend wiring).

The wiring is the cidx-scip feature's (``cidx_scip.server.backend_wiring``)
since modular phase 4d; the removed ``scip_query_backend`` guard stays in the
core (``server/startup/removed_scip_settings.py``), feature or not.
"""

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cidx_scip.indexers.factory import (
    REMOVED_SCIP_BACKENDS,
    ScipBackendFactory,
)
from cidx_scip.indexers.typescript import TypeScriptIndexer
from cidx_scip.server import backend_wiring as scip_backend_wiring
from cidx_scip.server.backend_wiring import (
    STARTUP_CONFIG_ERROR_CODE,
    UnsupportedScipBackendError,
    build_scip_generator,
    build_scip_generator_for_golden_repo,
    check_scip_backend_at_startup,
    validate_scip_backend_config,
)
from code_indexer.server.startup import removed_scip_settings
from code_indexer.server.startup.removed_scip_settings import (
    DELEGATED_SCIP_QUERY_BACKEND_REMOVED,
    UnsupportedScipQueryBackendError,
    check_scip_query_backend_at_startup,
    validate_scip_query_backend_setting,
)

#: The removed backend, configured the way validate/live overlays had it.
_REMOVED = SimpleNamespace(scip_backend="delegated", scip_build_stateless=True)


def _local_config():
    return SimpleNamespace(scip_backend="local", workspace_mcp=None)


class TestLocalDefault:
    def test_local_default_uses_local_indexers(self, tmp_path):
        gen = build_scip_generator(_local_config(), tmp_path, workspace_id="W")
        assert isinstance(gen._indexers["typescript"], TypeScriptIndexer)
        assert gen.workspace_id == "W"

    def test_missing_scip_backend_attr_defaults_local(self, tmp_path):
        gen = build_scip_generator(SimpleNamespace(), tmp_path)
        assert isinstance(gen._indexers["typescript"], TypeScriptIndexer)
        assert gen.workspace_id is None


class TestFailLoudOnBadValue:
    """EVO-65021: an unrecognised scip_backend must RAISE, never silently
    downgrade to a local build (which would come up 'healthy' then fail every
    project with tool-not-found on the toolchain-less indexing pods)."""

    @pytest.mark.parametrize("bad", ["delegatd", "Delegated", "remote", "DELEGATED"])
    def test_unknown_value_fails_loud(self, tmp_path, bad):
        cfg = SimpleNamespace(scip_backend=bad, workspace_mcp=None)
        with pytest.raises(UnsupportedScipBackendError, match="Unsupported"):
            build_scip_generator(cfg, tmp_path, workspace_id="W")

    def test_error_names_the_bad_value(self, tmp_path):
        cfg = SimpleNamespace(scip_backend="delegatd", workspace_mcp=None)
        with pytest.raises(RuntimeError, match="delegatd"):
            build_scip_generator(cfg, tmp_path)

    def test_valid_options_are_local_and_delegated_plugin(self):
        assert ScipBackendFactory._VALID_BACKENDS == ("local", "delegated-plugin")


class TestRemovedDelegatedBackend:
    """``scip_backend='delegated'`` (sandbox_scip) was removed. A deployment
    still configured for it must fail with a message naming the migration,
    not with the generic unknown-value one, and never build anything."""

    def test_validate_raises_the_migration_message(self):
        with pytest.raises(UnsupportedScipBackendError) as exc:
            validate_scip_backend_config(_REMOVED)
        assert str(exc.value) == REMOVED_SCIP_BACKENDS["delegated"]

    @pytest.mark.parametrize(
        "needle",
        [
            "scip_backend='delegated'",
            "was removed",
            "scip_backend='delegated-plugin'",
            "cidx-content-backend",
            "code_indexer.scip_backend",
            "CIDX_SCIP_SANDBOX_MCP_URL",
            "SCIP_RUN_SANDBOX_IMAGE",
            "scip_backend='local'",
            "scip_build_stateless",
        ],
    )
    def test_the_message_names_each_migration_step(self, needle):
        assert needle in REMOVED_SCIP_BACKENDS["delegated"]

    def test_it_is_not_reported_as_an_unknown_value(self):
        with pytest.raises(UnsupportedScipBackendError) as exc:
            validate_scip_backend_config(_REMOVED)
        assert "Unsupported scip_backend" not in str(exc.value)

    def test_build_refuses_before_constructing_anything(self, tmp_path, monkeypatch):
        def explode(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("must not construct an indexer map")

        monkeypatch.setattr(ScipBackendFactory, "create_indexer_map", explode)
        with pytest.raises(UnsupportedScipBackendError, match="was removed"):
            build_scip_generator(_REMOVED, tmp_path, golden_alias="svc")

    def test_golden_repo_build_refuses_too(self, tmp_path):
        with pytest.raises(UnsupportedScipBackendError, match="was removed"):
            build_scip_generator_for_golden_repo(
                _REMOVED, tmp_path, golden_repo_manager=None, alias="svc"
            )

    def test_the_factory_no_longer_builds_it(self, tmp_path):
        with pytest.raises(ValueError, match="Unsupported scip_backend type"):
            ScipBackendFactory.create_indexer_map("delegated", repo_root=tmp_path)

    def test_it_is_not_a_delegated_backend_any_more(self):
        from cidx_scip.indexers.factory import is_delegated_scip_backend

        assert not is_delegated_scip_backend("delegated")


class TestStartupPolicy:
    """check_scip_backend_at_startup: a removed or unknown value STOPS startup
    (the pod crash-loops with the message); every other scip config error is
    logged and startup continues."""

    def test_removed_delegated_fails_startup_with_the_migration_message(self):
        with pytest.raises(UnsupportedScipBackendError) as exc:
            check_scip_backend_at_startup(lambda: _REMOVED)
        assert str(exc.value) == REMOVED_SCIP_BACKENDS["delegated"]

    def test_unknown_value_fails_startup(self):
        with pytest.raises(UnsupportedScipBackendError, match="delegatd"):
            check_scip_backend_at_startup(
                lambda: SimpleNamespace(scip_backend="delegatd")
            )

    def test_local_passes_and_logs_validated(self, caplog):
        with caplog.at_level(logging.INFO, logger=scip_backend_wiring.__name__):
            check_scip_backend_at_startup(_local_config)
        assert "scip_backend config validated at startup" in caplog.text
        assert STARTUP_CONFIG_ERROR_CODE not in caplog.text

    def test_a_broken_plugin_is_logged_not_fatal(self, monkeypatch, caplog):
        def broken(_config: Any) -> None:
            raise RuntimeError("no SCIP backend plugin is installed")

        monkeypatch.setattr(scip_backend_wiring, "validate_scip_backend_config", broken)
        with caplog.at_level(logging.INFO, logger=scip_backend_wiring.__name__):
            check_scip_backend_at_startup(
                lambda: SimpleNamespace(scip_backend="delegated-plugin")
            )
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert STARTUP_CONFIG_ERROR_CODE in errors[0].getMessage()
        assert "no SCIP backend plugin is installed" in errors[0].getMessage()
        assert "validated at startup" not in caplog.text

    def test_a_config_read_failure_is_logged_not_fatal(self, caplog):
        def unreadable() -> Any:
            raise OSError("config.json unreadable")

        with caplog.at_level(logging.ERROR, logger=scip_backend_wiring.__name__):
            check_scip_backend_at_startup(unreadable)
        assert "config.json unreadable" in caplog.text

    def test_unsupported_is_a_runtime_error(self):
        # The per-job funnel catches RuntimeError; a removed value must still
        # fail-soft there rather than crash an index job.
        assert issubclass(UnsupportedScipBackendError, RuntimeError)


class TestValidateScipBackendConfig:
    """EVO-65021: the eager startup config validation (fail-loud call site)."""

    def test_local_config_passes(self):
        validate_scip_backend_config(_local_config())

    def test_missing_attr_defaults_local_and_passes(self):
        validate_scip_backend_config(SimpleNamespace())

    @pytest.mark.parametrize("bad", ["delegatd", "Delegated", "remote", "DELEGATED"])
    def test_unknown_value_raises(self, bad):
        cfg = SimpleNamespace(scip_backend=bad, workspace_mcp=None)
        with pytest.raises(
            UnsupportedScipBackendError, match="Unsupported scip_backend"
        ):
            validate_scip_backend_config(cfg)

    def test_unknown_value_names_the_bad_value(self):
        cfg = SimpleNamespace(scip_backend="delegatd", workspace_mcp=None)
        with pytest.raises(RuntimeError, match="delegatd"):
            validate_scip_backend_config(cfg)


class TestGoldenRepoAllowDegenerate:
    """EVO-65365: golden-repo builds opt into the sandbox allow_degenerate path."""

    def test_golden_repo_generator_has_allow_degenerate_true(self, tmp_path):
        gen = build_scip_generator_for_golden_repo(
            _local_config(),
            tmp_path,
            golden_repo_manager=None,
            alias="svc-foo",
        )
        assert gen is not None
        assert gen.allow_degenerate is True

    def test_non_golden_build_scip_generator_defaults_false(self, tmp_path):
        gen = build_scip_generator(_local_config(), tmp_path, workspace_id="W")
        assert gen.allow_degenerate is False


class TestGoldenRepoBridge:
    def test_local_backend_addresses_neither(self, tmp_path):
        gen = build_scip_generator_for_golden_repo(
            _local_config(),
            tmp_path,
            golden_repo_manager=None,
            alias="svc-foo",
        )
        assert gen is not None
        assert isinstance(gen._indexers["typescript"], TypeScriptIndexer)
        assert gen.workspace_id is None
        assert gen.golden_alias is None


class TestRemovedScipQueryBackend:
    """EVO-64616: scip_query_backend was removed. config.json is read as written
    (ServerConfig no longer has the key); only an absent, empty or 'local'
    value passes."""

    @pytest.mark.parametrize(
        "raw",
        [
            {},
            {"scip_query_backend": "local"},
            {"scip_query_backend": ""},
            {"scip_query_backend": None},
        ],
    )
    def test_absent_empty_or_local_passes(self, raw):
        validate_scip_query_backend_setting(raw)

    def test_delegated_is_refused_with_the_migration_message(self):
        with pytest.raises(UnsupportedScipQueryBackendError) as exc:
            validate_scip_query_backend_setting({"scip_query_backend": "delegated"})
        assert str(exc.value) == DELEGATED_SCIP_QUERY_BACKEND_REMOVED

    @pytest.mark.parametrize(
        "needle",
        [
            "was removed",
            "sandbox_scip_*",
            "Delete scip_query_backend",
            "scip_sandbox",
            "delegated-plugin",
            "index_store_backend='delegated'",
        ],
    )
    def test_the_migration_message_says_what_to_do(self, needle):
        assert needle in DELEGATED_SCIP_QUERY_BACKEND_REMOVED

    @pytest.mark.parametrize("bad", ["Delegated", "remote", "plugin", 1, ["local"]])
    def test_any_other_value_is_refused_and_named(self, bad):
        with pytest.raises(UnsupportedScipQueryBackendError) as exc:
            validate_scip_query_backend_setting({"scip_query_backend": bad})
        assert f"scip_query_backend={bad!r}" in str(exc.value)
        assert str(exc.value) != DELEGATED_SCIP_QUERY_BACKEND_REMOVED

    def test_it_is_a_runtime_error(self):
        # The guard is the core's now (it runs whether or not the scip
        # feature is installed), so it cannot subclass the feature's
        # UnsupportedScipBackendError; startup treats any RuntimeError raised
        # here as fatal.
        assert issubclass(UnsupportedScipQueryBackendError, RuntimeError)


class TestScipQueryBackendStartupPolicy:
    """check_scip_query_backend_at_startup: a value other than 'local' STOPS
    startup; a config.json that cannot be read is logged and startup goes on."""

    def test_delegated_fails_startup(self):
        with pytest.raises(UnsupportedScipQueryBackendError) as exc:
            check_scip_query_backend_at_startup(
                lambda: {"scip_query_backend": "delegated", "scip_sandbox": {}}
            )
        assert str(exc.value) == DELEGATED_SCIP_QUERY_BACKEND_REMOVED

    def test_unknown_value_fails_startup(self):
        with pytest.raises(UnsupportedScipQueryBackendError, match="remote"):
            check_scip_query_backend_at_startup(
                lambda: {"scip_query_backend": "remote"}
            )

    @pytest.mark.parametrize("raw", [None, {}, {"scip_query_backend": "local"}])
    def test_no_file_or_local_passes_silently(self, raw, caplog):
        with caplog.at_level(logging.INFO, logger=removed_scip_settings.__name__):
            check_scip_query_backend_at_startup(lambda: raw)
        assert caplog.records == []

    def test_a_config_read_failure_is_logged_not_fatal(self, caplog):
        def unreadable() -> Any:
            raise OSError("config.json unreadable")

        with caplog.at_level(logging.INFO, logger=removed_scip_settings.__name__):
            check_scip_query_backend_at_startup(unreadable)
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert removed_scip_settings.STARTUP_CONFIG_ERROR_CODE in errors[0].getMessage()
        assert "config.json unreadable" in errors[0].getMessage()

    @pytest.mark.parametrize("raw", [["scip_query_backend"], "delegated", 7])
    def test_a_file_that_is_not_an_object_is_logged_not_fatal(self, raw, caplog):
        with caplog.at_level(logging.ERROR, logger=removed_scip_settings.__name__):
            check_scip_query_backend_at_startup(lambda: raw)
        assert "not an object" in caplog.text
        assert removed_scip_settings.STARTUP_CONFIG_ERROR_CODE in caplog.text


class TestScipQueryBackendCheckRunsBeforeTheStrip:
    """The check reads config.json as written, so it must run before the
    runtime-config initialisation strips config.json to bootstrap keys."""

    def _seed(self, tmp_path: Path) -> Any:
        from code_indexer.server.services.config_service import ConfigService

        svc = ConfigService(server_dir_path=str(tmp_path))
        manager = svc.config_manager
        manager.save_config(manager.create_default_config())
        raw = json.loads(manager.config_file_path.read_text())
        raw["scip_query_backend"] = "delegated"
        raw["scip_sandbox"] = {"endpoint": "http://sandbox-mcp:8200", "api_key": "k"}
        manager.config_file_path.write_text(json.dumps(raw))
        return svc

    def test_a_delegated_config_json_is_refused(self, tmp_path):
        svc = self._seed(tmp_path)
        with pytest.raises(UnsupportedScipQueryBackendError):
            check_scip_query_backend_at_startup(svc.config_manager.read_config_file)

    def test_after_the_strip_the_key_is_gone(self, tmp_path):
        # Why the order matters: initialize_runtime_db rewrites config.json
        # without the removed key, after which nothing could refuse it.
        from code_indexer.server.storage.database_manager import DatabaseSchema

        svc = self._seed(tmp_path)
        db_path = str(tmp_path / "cidx_server.db")
        DatabaseSchema(db_path).initialize_database()
        svc.initialize_runtime_db(db_path)
        stripped = svc.config_manager.read_config_file()
        assert "scip_query_backend" not in stripped
        check_scip_query_backend_at_startup(svc.config_manager.read_config_file)

    def test_service_init_checks_before_initialize_runtime_db(self):
        source = (
            Path(removed_scip_settings.__file__).parent / "service_init.py"
        ).read_text()
        check = source.find(
            "check_scip_query_backend_at_startup(config_service.config_manager.read_config_file)"
        )
        strip = source.find("config_service.initialize_runtime_db(")
        assert check != -1, (
            "service_init.py no longer runs the scip_query_backend check"
        )
        assert strip != -1, "initialize_runtime_db call not found in service_init.py"
        assert check < strip, (
            "check_scip_query_backend_at_startup must run before "
            "initialize_runtime_db strips config.json to bootstrap keys"
        )
