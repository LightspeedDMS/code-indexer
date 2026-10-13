"""Control-plane metadata wiring and configuration (content seam step 5a).

* The real ``initialize_services()`` -- in a fresh subprocess against an empty
  data dir -- installs the registry-backed component, and the cidx-meta
  bootstrap's own alias lands as a registry row. Proves the capability is
  reachable from the real startup path, not just from tests.
* ``install_registry_metadata`` over the real StorageFactory registry.
* The runtime config category and the Web UI Config section.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from code_indexer.global_repos.control_plane_metadata import (
    MetadataSettings,
    installed_control_plane_metadata,
)
from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.startup.control_plane_metadata_wiring import (
    install_registry_metadata,
    settings_from_config_service,
)
from code_indexer.server.storage.factory import StorageFactory
from tests.utils.control_plane_metadata_fixtures import build_schema_template

_SRC = str(Path(__file__).resolve().parents[4] / "src")

_PROBE = """
import json, sys
from code_indexer.server.startup.service_init import initialize_services
initialize_services()
from code_indexer.global_repos.control_plane_metadata import (
    installed_control_plane_metadata,
)
m = installed_control_plane_metadata()
print("PROBE " + json.dumps({
    "golden_repos_dir": str(m.golden_repos_dir) if m else None,
    "registry_backed": bool(m and m.is_registry_backed),
    "cluster": bool(m and m.cluster),
}))
"""


def test_real_startup_installs_registry_and_bootstrap_alias_is_a_row(tmp_path):
    data_root = tmp_path / "srv"
    env = {
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": _SRC,
        "CIDX_SERVER_DATA_DIR": str(data_root),
    }
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        env=env,
        capture_output=True,
        text=True,
        timeout=220,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    probe_line = [ln for ln in completed.stdout.splitlines() if ln.startswith("PROBE ")]
    assert probe_line, completed.stdout[-2000:]
    probe = json.loads(probe_line[-1][len("PROBE ") :])

    golden_repos_dir = data_root / "data" / "golden-repos"
    assert probe == {
        "golden_repos_dir": str(golden_repos_dir),
        "registry_backed": True,
        "cluster": False,
    }
    with sqlite3.connect(str(data_root / "data" / "cidx_server.db")) as conn:
        rows = dict(conn.execute("SELECT name, payload FROM golden_repo_aliases"))
    assert json.loads(rows["cidx-meta-global"])["target_path"] == str(
        golden_repos_dir / "cidx-meta"
    )


class TestInstallRegistryMetadata:
    @pytest.fixture
    def sqlite_registry(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        build_schema_template(data_dir)
        return StorageFactory.create_backends(
            config={"storage_mode": "sqlite"}, data_dir=str(data_dir)
        )

    @pytest.fixture(autouse=True)
    def _restore(self):
        from code_indexer.global_repos.control_plane_metadata import (
            install_control_plane_metadata,
            uninstall_control_plane_metadata,
        )

        previous = installed_control_plane_metadata()
        yield
        if previous is None:
            uninstall_control_plane_metadata()
        else:
            install_control_plane_metadata(previous)

    def test_installs_solo_component(self, sqlite_registry, tmp_path):
        metadata = install_registry_metadata(
            sqlite_registry,
            tmp_path / "golden-repos",
            storage_mode="sqlite",
            settings=MetadataSettings,
            start_import=False,
        )
        assert metadata is installed_control_plane_metadata()
        assert metadata is not None
        assert metadata.is_registry_backed and not metadata.cluster

    def test_postgres_storage_mode_marks_the_component_cluster(
        self, sqlite_registry, tmp_path
    ):
        metadata = install_registry_metadata(
            sqlite_registry,
            tmp_path / "golden-repos",
            storage_mode="postgres",
            settings=MetadataSettings,
            start_import=False,
        )
        assert metadata is not None and metadata.cluster

    def test_registry_without_backend_installs_nothing(self, tmp_path):
        before = installed_control_plane_metadata()
        result = install_registry_metadata(
            SimpleNamespace(control_plane_metadata=None),
            tmp_path / "golden-repos",
            storage_mode="sqlite",
        )
        assert result is None
        assert installed_control_plane_metadata() is before


def test_settings_provider_reads_runtime_config():
    config = SimpleNamespace(
        control_plane_metadata_config=SimpleNamespace(
            metadata_read_source="files", metadata_file_dual_write=False
        )
    )
    provider = settings_from_config_service(lambda: config)
    assert provider() == MetadataSettings(read_source="files", file_dual_write=False)


class TestConfigCategory:
    def test_defaults(self, tmp_path):
        settings = ConfigService(server_dir_path=str(tmp_path)).get_all_settings()
        assert settings["control_plane_metadata"] == {
            "metadata_read_source": "registry",
            "metadata_file_dual_write": True,
            # Deviation from the design's workspace-mcp default until the
            # workspace-mcp file-write path exists (see sinks.py).
            "metadata_outbox_sink": "local-files",
            "metadata_outbox_drain_timeout_s": 600,
        }

    def test_read_source_and_dual_write_persist(self, tmp_path):
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()
        service.update_setting(
            "control_plane_metadata", "metadata_read_source", "files"
        )
        service.update_setting(
            "control_plane_metadata", "metadata_file_dual_write", "false"
        )
        cpm = service.get_config().control_plane_metadata_config
        assert cpm is not None
        assert cpm.metadata_read_source == "files"
        assert cpm.metadata_file_dual_write is False

    def test_sink_and_drain_timeout_persist(self, tmp_path):
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()
        service.update_setting(
            "control_plane_metadata", "metadata_outbox_sink", "workspace-mcp"
        )
        service.update_setting(
            "control_plane_metadata", "metadata_outbox_drain_timeout_s", "120"
        )
        cpm = service.get_config().control_plane_metadata_config
        assert cpm is not None
        assert cpm.metadata_outbox_sink == "workspace-mcp"
        assert cpm.metadata_outbox_drain_timeout_s == 120

    @pytest.mark.parametrize(
        "key,value",
        [
            ("metadata_outbox_sink", "nfs"),
            ("metadata_outbox_drain_timeout_s", "0"),
        ],
    )
    def test_invalid_outbox_settings_are_rejected(self, tmp_path, key, value):
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()
        with pytest.raises(ValueError):
            service.update_setting("control_plane_metadata", key, value)

    def test_unknown_read_source_is_rejected(self, tmp_path):
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()
        with pytest.raises(ValueError):
            service.update_setting(
                "control_plane_metadata", "metadata_read_source", "nfs"
            )

    def test_persisted_dict_round_trips_to_dataclass(self, tmp_path):
        """The runtime row is stored as a dict; loading it must rebuild the
        dataclass and drop keys a newer node may have written."""
        from code_indexer.server.utils.config_manager import (
            ControlPlaneMetadataConfig,
            ServerConfigManager,
        )

        config = ServerConfigManager(str(tmp_path))._dict_to_server_config(
            {
                "server_dir": str(tmp_path),
                "control_plane_metadata_config": {
                    "metadata_read_source": "files",
                    "unknown_future_key": 1,
                },
            }
        )
        assert config.control_plane_metadata_config == ControlPlaneMetadataConfig(
            metadata_read_source="files"
        )


class TestWebUiSection:
    def test_section_is_valid_and_defaulted(self, tmp_path):
        import unittest.mock as mock

        from code_indexer.server.web import routes

        assert "control_plane_metadata" in routes._VALID_CONFIG_SECTIONS
        with mock.patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=ConfigService(server_dir_path=str(tmp_path)),
        ):
            config = routes._get_current_config()
        assert config["control_plane_metadata"]["metadata_read_source"] == "registry"

    @pytest.mark.parametrize(
        "data",
        [
            {"metadata_outbox_sink": "nfs"},
            {"metadata_outbox_drain_timeout_s": "0"},
            {"metadata_outbox_drain_timeout_s": "soon"},
        ],
    )
    def test_validation_rejects_bad_outbox_settings(self, data):
        from code_indexer.server.web.routes import _validate_config_section

        assert _validate_config_section("control_plane_metadata", data) is not None

    def test_validation_rejects_unknown_read_source(self):
        from code_indexer.server.web.routes import _validate_config_section

        assert (
            _validate_config_section(
                "control_plane_metadata", {"metadata_read_source": "files"}
            )
            is None
        )
        assert "metadata_read_source" in (
            _validate_config_section(
                "control_plane_metadata", {"metadata_read_source": "nfs"}
            )
            or ""
        )

    def test_template_has_the_form(self):
        template = (
            Path(_SRC)
            / "code_indexer"
            / "server"
            / "web"
            / "templates"
            / "partials"
            / "config_section.html"
        ).read_text()
        start = template.find('id="section-control-plane-metadata"')
        assert start != -1
        section = template[start : template.find("</details>", start)]
        assert 'action="/admin/config/control_plane_metadata"' in section
        assert 'name="metadata_read_source"' in section
        assert 'name="metadata_file_dual_write"' in section
        assert 'name="metadata_outbox_sink"' in section
        assert 'name="metadata_outbox_drain_timeout_s"' in section


class TestOutboxSweeperWiring:
    def test_outbox_settings_provider_reads_runtime_config(self):
        from code_indexer.server.services.metadata_outbox.sweeper import (
            OutboxSettings,
        )
        from code_indexer.server.startup.control_plane_metadata_wiring import (
            outbox_settings_from_config_service,
        )

        config = SimpleNamespace(
            control_plane_metadata_config=SimpleNamespace(
                metadata_outbox_sink="workspace-mcp",
                metadata_outbox_drain_timeout_s=30,
            )
        )
        provider = outbox_settings_from_config_service(lambda: config)
        assert provider() == OutboxSettings(sink="workspace-mcp", drain_timeout_s=30)

    def test_started_with_registry_and_gauge_registered(self, tmp_path):
        from code_indexer.global_repos.control_plane_metadata import (
            ControlPlaneMetadata,
        )
        from code_indexer.server.startup.control_plane_metadata_wiring import (
            start_metadata_outbox_sweeper,
            stop_metadata_outbox_sweeper,
        )
        from tests.utils.control_plane_metadata_fixtures import (
            scoped_install,
            sqlite_backend_from_template,
        )

        template = build_schema_template(tmp_path)
        backend_dir = tmp_path / "copy"
        backend_dir.mkdir()
        backend = sqlite_backend_from_template(template, backend_dir)
        gauges = []

        class _Meter:
            def create_observable_gauge(self, **kwargs):
                gauges.append(kwargs["name"])

        telemetry = SimpleNamespace(get_meter=lambda name: _Meter())
        app = SimpleNamespace(state=SimpleNamespace())
        with scoped_install(
            ControlPlaneMetadata(tmp_path / "golden-repos", backend=backend)
        ):
            sweeper = start_metadata_outbox_sweeper(app, telemetry)
            try:
                assert sweeper is app.state.metadata_outbox_sweeper
                assert sweeper is not None
                assert gauges == ["cidx_metadata_outbox_depth"]
            finally:
                stop_metadata_outbox_sweeper(app)

    def test_not_started_without_registry(self):
        from code_indexer.global_repos.control_plane_metadata import (
            install_control_plane_metadata,
            uninstall_control_plane_metadata,
        )
        from code_indexer.server.startup.control_plane_metadata_wiring import (
            start_metadata_outbox_sweeper,
        )

        previous = installed_control_plane_metadata()
        uninstall_control_plane_metadata()
        try:
            app = SimpleNamespace(state=SimpleNamespace())
            assert start_metadata_outbox_sweeper(app) is None
            assert app.state.metadata_outbox_sweeper is None
        finally:
            if previous is not None:
                install_control_plane_metadata(previous)

    def test_lifespan_starts_and_stops_the_sweeper(self):
        source = (
            Path(_SRC) / "code_indexer" / "server" / "startup" / "lifespan.py"
        ).read_text()
        assert "start_metadata_outbox_sweeper(app, telemetry_manager)" in source
        assert "stop_metadata_outbox_sweeper(app)" in source
