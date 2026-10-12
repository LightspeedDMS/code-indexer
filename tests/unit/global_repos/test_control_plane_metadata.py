"""Control-plane metadata in the registry (content seam step 5a).

Aliases and write-mode markers live in the server's registry; the old files
are kept in step for the rollback window. These tests drive the REAL SQLite
registry backend and the REAL filesystem -- no mocked storage -- through the
public views every consumer uses (AliasManager, WriteModeMarkers).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.control_plane_metadata import (
    KIND_ALIAS,
    KIND_WRITE_MODE,
    ControlPlaneMetadata,
    MetadataSettings,
    metadata_for,
)
from code_indexer.global_repos.write_mode_markers import WriteModeMarkers
from code_indexer.server.storage.protocols.control_plane_metadata_backend import (
    format_updated_at,
    metadata_table,
)
from code_indexer.server.storage.sqlite_backends import (
    ControlPlaneMetadataSqliteBackend,
)
from tests.utils.control_plane_metadata_fixtures import (
    build_schema_template,
    scoped_install,
    sqlite_backend_from_template,
)

ALIAS = "example-repo-global"


class _Switches:
    """Mutable settings the component reads on every call."""

    def __init__(self) -> None:
        self.read_source = "registry"
        self.file_dual_write = True

    def __call__(self) -> MetadataSettings:
        return MetadataSettings(
            read_source=self.read_source, file_dual_write=self.file_dual_write
        )


@pytest.fixture
def golden_repos_dir(tmp_path: Path) -> Path:
    path = tmp_path / "golden-repos"
    path.mkdir()
    return path


@pytest.fixture(scope="module")
def schema_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The real server schema, built once; each test gets a copy."""
    return build_schema_template(tmp_path_factory.mktemp("schema"))


@pytest.fixture
def backend(tmp_path: Path, schema_template: Path) -> ControlPlaneMetadataSqliteBackend:
    return sqlite_backend_from_template(schema_template, tmp_path)


@pytest.fixture
def switches() -> _Switches:
    return _Switches()


@pytest.fixture
def metadata(
    golden_repos_dir: Path,
    backend: ControlPlaneMetadataSqliteBackend,
    switches: _Switches,
) -> Iterator[ControlPlaneMetadata]:
    component = ControlPlaneMetadata(
        golden_repos_dir, backend=backend, settings=switches
    )
    component.import_files()  # import done: no read-through to the files
    with scoped_install(component):
        yield component


def _alias_file(golden_repos_dir: Path, name: str = ALIAS) -> Path:
    return golden_repos_dir / "aliases" / f"{name}.json"


def _write_file(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))


# ---------------------------------------------------------------------------
# Registry is the source of truth
# ---------------------------------------------------------------------------


class TestRegistryIsTheSourceOfTruth:
    def test_alias_written_by_alias_manager_is_a_registry_row(
        self, metadata, backend, golden_repos_dir, switches
    ):
        switches.file_dual_write = False
        manager = AliasManager(golden_repos_dir / "aliases")

        manager.create_alias(ALIAS, "/snapshots/v_1")

        assert backend.get(KIND_ALIAS, ALIAS)["target_path"] == "/snapshots/v_1"
        assert not _alias_file(golden_repos_dir).exists()
        assert manager.read_alias(ALIAS) == "/snapshots/v_1"

    def test_registry_read_ignores_a_divergent_file(self, metadata, golden_repos_dir):
        manager = AliasManager(golden_repos_dir / "aliases")
        manager.create_alias(ALIAS, "/snapshots/v_1")
        _write_file(_alias_file(golden_repos_dir), {"target_path": "/stale/file"})

        assert manager.read_alias(ALIAS) == "/snapshots/v_1"

    def test_no_aliases_directory_is_created_when_registry_backed(
        self, metadata, golden_repos_dir, switches
    ):
        switches.file_dual_write = False
        AliasManager(golden_repos_dir / "aliases").create_alias(ALIAS, "/s/v_1")

        assert not (golden_repos_dir / "aliases").exists()

    def test_swap_records_previous_path_in_the_registry(
        self, metadata, backend, golden_repos_dir
    ):
        manager = AliasManager(golden_repos_dir / "aliases")
        manager.create_alias(ALIAS, "/s/v_1")

        manager.swap_alias(ALIAS, "/s/v_2", "/s/v_1")

        assert manager.read_pointer(ALIAS) == ("/s/v_2", "/s/v_1")
        assert backend.get(KIND_ALIAS, ALIAS)["previous_path"] == "/s/v_1"

    def test_swap_with_wrong_old_target_is_refused(self, metadata, golden_repos_dir):
        manager = AliasManager(golden_repos_dir / "aliases")
        manager.create_alias(ALIAS, "/s/v_1")

        with pytest.raises(ValueError):
            manager.swap_alias(ALIAS, "/s/v_2", "/s/not-current")

    def test_list_pointers_filters_by_prefix(self, metadata, golden_repos_dir):
        manager = AliasManager(golden_repos_dir / "aliases")
        manager.create_alias("repo-temporal-a", "/s/a")
        manager.create_alias("repo-temporal-b", "/s/b")
        manager.create_alias("other-global", "/s/o")

        assert manager.list_pointers("repo-temporal-") == {
            "repo-temporal-a": ("/s/a", None),
            "repo-temporal-b": ("/s/b", None),
        }

    def test_prefix_is_literal_not_a_like_pattern(self, metadata, golden_repos_dir):
        """Aliases contain '_' -- a LIKE wildcard. The prefix must match
        literally."""
        manager = AliasManager(golden_repos_dir / "aliases")
        manager.create_alias("a_b-x", "/s/1")
        manager.create_alias("aXb-x", "/s/2")

        assert set(manager.list_pointers("a_b")) == {"a_b-x"}

    def test_delete_removes_the_row(self, metadata, backend, golden_repos_dir):
        manager = AliasManager(golden_repos_dir / "aliases")
        manager.create_alias(ALIAS, "/s/v_1")

        manager.delete_alias(ALIAS)

        assert backend.get(KIND_ALIAS, ALIAS) is None
        assert not manager.alias_exists(ALIAS)


class TestDualWriteKeepsTheFileInStep:
    def test_create_swap_delete_follow_the_registry(self, metadata, golden_repos_dir):
        manager = AliasManager(golden_repos_dir / "aliases")
        manager.create_alias(ALIAS, "/s/v_1")
        manager.swap_alias(ALIAS, "/s/v_2", "/s/v_1")

        on_disk = json.loads(_alias_file(golden_repos_dir).read_text())
        assert on_disk == manager.read_record(ALIAS)

        manager.delete_alias(ALIAS)
        assert not _alias_file(golden_repos_dir).exists()


# ---------------------------------------------------------------------------
# metadata_read_source switch
# ---------------------------------------------------------------------------


class TestFilesReadSource:
    def test_reads_come_from_the_file(self, metadata, golden_repos_dir, switches):
        manager = AliasManager(golden_repos_dir / "aliases")
        manager.create_alias(ALIAS, "/s/v_1")
        switches.read_source = "files"
        _write_file(_alias_file(golden_repos_dir), {"target_path": "/from/file"})

        assert manager.read_alias(ALIAS) == "/from/file"

    def test_write_lands_in_file_and_registry(
        self, metadata, backend, golden_repos_dir, switches
    ):
        switches.read_source = "files"
        switches.file_dual_write = False  # files mode writes the file anyway
        manager = AliasManager(golden_repos_dir / "aliases")

        manager.create_alias(ALIAS, "/s/v_1")

        assert json.loads(_alias_file(golden_repos_dir).read_text())["target_path"] == (
            "/s/v_1"
        )
        assert backend.get(KIND_ALIAS, ALIAS)["target_path"] == "/s/v_1"

    def test_file_failure_leaves_the_registry_untouched(
        self, metadata, backend, golden_repos_dir, switches
    ):
        """File FIRST: if the file cannot be written the call fails before the
        registry changes, so the two never disagree in the registry's favour."""
        switches.read_source = "files"
        (golden_repos_dir / "aliases").write_text("not a directory")
        manager = AliasManager.existing(golden_repos_dir / "aliases")
        assert manager is not None

        with pytest.raises(RuntimeError):
            manager.create_alias(ALIAS, "/s/v_1")
        assert backend.get(KIND_ALIAS, ALIAS) is None


# ---------------------------------------------------------------------------
# Startup import
# ---------------------------------------------------------------------------


def _alias_doc(target: str, when: datetime) -> dict:
    stamp = when.isoformat()
    return {"target_path": target, "created_at": stamp, "last_refresh": stamp}


class TestStartupImport:
    def test_imports_aliases_and_markers(self, golden_repos_dir, backend):
        now = datetime.now(timezone.utc)
        _write_file(_alias_file(golden_repos_dir), _alias_doc("/s/v_1", now))
        _write_file(
            golden_repos_dir / ".write_mode" / "cidx-meta.json",
            {"source_path": "/live", "entered_at": now.isoformat()},
        )
        component = ControlPlaneMetadata(golden_repos_dir, backend=backend)

        report = component.import_files()

        assert report == {"imported": 2, "skipped": 0, "failed": 0}
        assert backend.get(KIND_ALIAS, ALIAS)["target_path"] == "/s/v_1"
        assert backend.get(KIND_WRITE_MODE, "cidx-meta")["source_path"] == "/live"

    def test_is_idempotent(self, golden_repos_dir, backend):
        _write_file(
            _alias_file(golden_repos_dir),
            _alias_doc("/s/v_1", datetime.now(timezone.utc)),
        )
        component = ControlPlaneMetadata(golden_repos_dir, backend=backend)
        component.import_files()

        second = component.import_files()

        assert second == {"imported": 0, "skipped": 1, "failed": 0}

    def test_older_file_never_clobbers_newer_registry_value(
        self, golden_repos_dir, backend
    ):
        backend.put(KIND_ALIAS, ALIAS, {"target_path": "/s/registry-newer"})
        an_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        _write_file(_alias_file(golden_repos_dir), _alias_doc("/s/old", an_hour_ago))

        ControlPlaneMetadata(golden_repos_dir, backend=backend).import_files()

        assert backend.get(KIND_ALIAS, ALIAS)["target_path"] == "/s/registry-newer"

    def test_newer_file_replaces_older_registry_value(self, golden_repos_dir, backend):
        """A file written by a pod still on the old image after the registry
        row was stored is the newer truth."""
        backend.import_if_newer(
            KIND_ALIAS,
            ALIAS,
            {"target_path": "/s/registry-older"},
            format_updated_at(datetime.now(timezone.utc) - timedelta(hours=1)),
        )
        _write_file(
            _alias_file(golden_repos_dir),
            _alias_doc("/s/file-newer", datetime.now(timezone.utc)),
        )

        ControlPlaneMetadata(golden_repos_dir, backend=backend).import_files()

        assert backend.get(KIND_ALIAS, ALIAS)["target_path"] == "/s/file-newer"

    def test_dual_written_file_is_not_reimported(
        self, metadata, backend, golden_repos_dir
    ):
        """The dual-written file is a copy of the registry row, dated before
        it was stored, so a later boot's import leaves the row alone."""
        AliasManager(golden_repos_dir / "aliases").create_alias(ALIAS, "/s/v_1")

        assert metadata.import_files()["imported"] == 0

    def test_undated_file_fills_an_empty_slot_only(self, golden_repos_dir, backend):
        _write_file(_alias_file(golden_repos_dir), {"target_path": "/s/undated"})
        _write_file(_alias_file(golden_repos_dir, "kept"), {"target_path": "/s/f"})
        backend.put(KIND_ALIAS, "kept", {"target_path": "/s/registry"})

        ControlPlaneMetadata(golden_repos_dir, backend=backend).import_files()

        assert backend.get(KIND_ALIAS, ALIAS)["target_path"] == "/s/undated"
        assert backend.get(KIND_ALIAS, "kept")["target_path"] == "/s/registry"

    def test_unreadable_file_is_skipped_not_fatal(self, golden_repos_dir, backend):
        bad = _alias_file(golden_repos_dir, "broken")
        bad.parent.mkdir(parents=True)
        bad.write_text("{not json")
        _write_file(
            _alias_file(golden_repos_dir),
            _alias_doc("/s/v_1", datetime.now(timezone.utc)),
        )

        report = ControlPlaneMetadata(golden_repos_dir, backend=backend).import_files()

        assert report["imported"] == 1
        assert backend.get(KIND_ALIAS, "broken") is None

    def test_reads_fall_back_to_files_until_import_completes(
        self, golden_repos_dir, backend
    ):
        _write_file(
            _alias_file(golden_repos_dir),
            _alias_doc("/s/v_1", datetime.now(timezone.utc)),
        )
        component = ControlPlaneMetadata(golden_repos_dir, backend=backend)
        manager = AliasManager(golden_repos_dir / "aliases", metadata=component)

        assert manager.read_alias(ALIAS) == "/s/v_1"
        assert manager.list_pointers() == {ALIAS: ("/s/v_1", None)}

        component.import_files()
        backend.delete(KIND_ALIAS, ALIAS)  # registry is now authoritative
        assert manager.read_alias(ALIAS) is None

    def test_background_import_runs_to_completion(self, golden_repos_dir, backend):
        _write_file(
            _alias_file(golden_repos_dir),
            _alias_doc("/s/v_1", datetime.now(timezone.utc)),
        )
        component = ControlPlaneMetadata(golden_repos_dir, backend=backend)

        component.start_import().join(timeout=30)

        assert backend.get(KIND_ALIAS, ALIAS)["target_path"] == "/s/v_1"


# ---------------------------------------------------------------------------
# Write-mode markers
# ---------------------------------------------------------------------------


class TestWriteModeMarkersInRegistry:
    def test_enter_read_redirect_and_exit(self, metadata, backend, golden_repos_dir):
        markers = WriteModeMarkers(golden_repos_dir)
        manager = AliasManager(golden_repos_dir / "aliases")
        manager.create_alias("cidx-meta-global", "/s/meta/v_1")

        markers.enter("cidx-meta-global", "/live/cidx-meta")

        assert backend.get(KIND_WRITE_MODE, "cidx-meta")["source_path"] == (
            "/live/cidx-meta"
        )
        assert markers.exists("cidx-meta")
        assert manager.read_alias("cidx-meta-global") == "/live/cidx-meta"
        # read_pointer is never redirected (Bug #1567 Gap 1 hole B)
        assert manager.read_pointer("cidx-meta-global") == ("/s/meta/v_1", None)

        markers.remove("cidx-meta")
        assert not markers.exists("cidx-meta-global")
        assert manager.read_alias("cidx-meta-global") == "/s/meta/v_1"


# ---------------------------------------------------------------------------
# Wiring, path matching, kinds
# ---------------------------------------------------------------------------


class TestWiring:
    def test_metadata_for_another_directory_is_files_only(self, metadata, tmp_path):
        assert metadata_for(metadata.golden_repos_dir) is metadata
        assert not metadata_for(tmp_path / "elsewhere").is_registry_backed

    def test_metadata_for_matches_unnormalized_spelling(self, metadata):
        spelled = Path(str(metadata.golden_repos_dir) + "/./aliases/..")
        assert metadata_for(spelled) is metadata

    def test_existing_never_creates_the_directory_without_a_registry(self, tmp_path):
        assert AliasManager.existing(tmp_path / "nope" / "aliases") is None
        assert not (tmp_path / "nope").exists()

    def test_existing_is_available_with_registry_and_no_directory(
        self, metadata, golden_repos_dir
    ):
        assert AliasManager.existing(golden_repos_dir / "aliases") is not None

    def test_files_only_mode_is_the_old_behaviour(self, tmp_path):
        manager = AliasManager(tmp_path / "aliases")
        manager.create_alias(ALIAS, "/s/v_1")

        on_disk = json.loads((tmp_path / "aliases" / f"{ALIAS}.json").read_text())
        assert on_disk["target_path"] == "/s/v_1"

    def test_unknown_kind_is_refused(self):
        with pytest.raises(ValueError):
            metadata_table("not-a-kind")

    def test_unreadable_settings_fall_back_to_defaults(self, golden_repos_dir, backend):
        def broken() -> MetadataSettings:
            raise RuntimeError("config unavailable")

        component = ControlPlaneMetadata(
            golden_repos_dir, backend=backend, settings=broken
        )
        assert component.current_settings() == MetadataSettings()
