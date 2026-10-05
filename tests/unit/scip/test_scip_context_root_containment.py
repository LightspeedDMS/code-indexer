"""SCIP context lines are read only from within the repository root.

``DatabaseBackend`` populates ``QueryResult.context`` by reading the source
line at each result's stored ``documents.relative_path``. Those stored paths
come from the ``.scip.db`` file itself, so the read must resolve the target
(collapsing ``..`` and following symlinks) and confine it to the repository
root that owns the database -- the directory above ``.code-indexer/scip`` --
not to the per-database sub-project directory, so monorepo sub-projects keep
working.

Every test drives the public ``find_definition`` query against a real,
schema-valid ``.scip.db`` with real stored rows.
"""

import logging
import sqlite3
import sys
from pathlib import Path
from typing import List, Optional

import pytest

from code_indexer.scip.database.schema import DatabaseManager
from code_indexer.scip.query.backends import DatabaseBackend
from code_indexer.scip.query.primitives import SCIPQueryEngine

# Full SCIP symbol (language-prefixed) so exact lookup is a plain equality.
SYMBOL = "python example 1.0 example/ExampleService#"
DEFINITION_ROLE = 1
INSIDE_LINE = "class ExampleService:"
OUTSIDE_LINE = "OUTSIDE_VALUE = 1"
BACKENDS_LOGGER = "code_indexer.scip.query.backends"
COMPOSITES_LOGGER = "code_indexer.scip.query.composites"

# Returning context needs opened-file verification through /proc/self/fd.
requires_proc_fd = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="SCIP context verification reads /proc/self/fd (Linux only)",
)


def _build_db(scip_dir: Path, relative_path: str, line: int = 0) -> Path:
    """Create a real .scip.db under ``scip_dir`` with one definition of
    SYMBOL stored at ``relative_path``:``line``."""
    scip_dir.mkdir(parents=True, exist_ok=True)
    manager = DatabaseManager(scip_dir / "index.scip")
    manager.create_schema()
    manager.create_indexes()
    db_path = Path(str(scip_dir / "index.scip") + ".db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO symbols (id, name, display_name, kind) VALUES (1, ?, ?, ?)",
            (SYMBOL, "ExampleService", "Class"),
        )
        conn.execute(
            "INSERT INTO documents (id, relative_path, language) VALUES (1, ?, ?)",
            (relative_path, "python"),
        )
        conn.execute(
            "INSERT INTO occurrences (id, symbol_id, document_id, start_line, "
            "start_char, end_line, end_char, role) VALUES (1, 1, 1, ?, 0, ?, 5, ?)",
            (line, line, DEFINITION_ROLE),
        )
        conn.execute("INSERT INTO symbols_fts(symbols_fts) VALUES('rebuild')")
        conn.commit()
    finally:
        conn.close()
    return db_path


def _add_definition(db_path: Path, doc_id: int, relative_path: str) -> None:
    """Store one more definition of SYMBOL at ``relative_path``:0."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO documents (id, relative_path, language) VALUES (?, ?, ?)",
            (doc_id, relative_path, "python"),
        )
        conn.execute(
            "INSERT INTO occurrences (id, symbol_id, document_id, start_line, "
            "start_char, end_line, end_char, role) VALUES (?, 1, ?, 0, 0, 0, 5, ?)",
            (doc_id, doc_id, DEFINITION_ROLE),
        )
        conn.commit()
    finally:
        conn.close()


def _context_via_engine(db_path: Path) -> Optional[str]:
    results = SCIPQueryEngine(db_path).find_definition(SYMBOL, exact=True)
    assert len(results) == 1
    return results[0].context


def _write_outside_file(tmp_path: Path) -> Path:
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "settings.py"
    outside_file.write_text(OUTSIDE_LINE + "\n")
    return outside_file


def _assert_refused(context: Optional[str], caplog) -> None:
    assert context is None
    assert OUTSIDE_LINE not in caplog.text
    warnings = [
        r
        for r in caplog.records
        if r.name == BACKENDS_LOGGER and r.levelno == logging.WARNING
    ]
    assert warnings, "a refused context read must be logged at WARNING"


class TestOutOfRootPathsAreRefused:
    def test_parent_traversal_path_returns_no_outside_content(self, tmp_path, caplog):
        _write_outside_file(tmp_path)
        repo = tmp_path / "repo"
        db_path = _build_db(repo / ".code-indexer" / "scip", "../outside/settings.py")

        with caplog.at_level(logging.WARNING, logger=BACKENDS_LOGGER):
            context = _context_via_engine(db_path)

        _assert_refused(context, caplog)

    def test_symlink_pointing_outside_root_returns_no_outside_content(
        self, tmp_path, caplog
    ):
        outside_file = _write_outside_file(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "settings_link.py").symlink_to(outside_file)
        db_path = _build_db(repo / ".code-indexer" / "scip", "settings_link.py")

        with caplog.at_level(logging.WARNING, logger=BACKENDS_LOGGER):
            context = _context_via_engine(db_path)

        _assert_refused(context, caplog)

    def test_absolute_stored_path_returns_no_outside_content(self, tmp_path, caplog):
        outside_file = _write_outside_file(tmp_path)
        repo = tmp_path / "repo"
        db_path = _build_db(repo / ".code-indexer" / "scip", str(outside_file))

        with caplog.at_level(logging.WARNING, logger=BACKENDS_LOGGER):
            context = _context_via_engine(db_path)

        _assert_refused(context, caplog)

    def test_subproject_path_escaping_repo_root_returns_no_outside_content(
        self, tmp_path, caplog
    ):
        _write_outside_file(tmp_path)
        repo = tmp_path / "repo"
        (repo / "services" / "backend").mkdir(parents=True)
        db_path = _build_db(
            repo / ".code-indexer" / "scip" / "services" / "backend",
            "../../../outside/settings.py",
        )

        with caplog.at_level(logging.WARNING, logger=BACKENDS_LOGGER):
            context = _context_via_engine(db_path)

        _assert_refused(context, caplog)

    def test_project_root_outside_repo_does_not_widen_confinement(
        self, tmp_path, caplog
    ):
        """project_root may come from index metadata; the confinement root is
        derived from the database's own location, never from project_root."""
        outside_file = _write_outside_file(tmp_path)
        repo = tmp_path / "repo"
        db_path = _build_db(repo / ".code-indexer" / "scip", "settings.py")

        backend = DatabaseBackend(db_path, project_root=str(outside_file.parent))
        with caplog.at_level(logging.WARNING, logger=BACKENDS_LOGGER):
            results = backend.find_definition(SYMBOL, exact=True)

        assert len(results) == 1
        _assert_refused(results[0].context, caplog)


@requires_proc_fd
class TestInRootPathsStillReturnContext:
    def test_top_level_project_path_returns_context(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / "pkg").mkdir(parents=True)
        (repo / "pkg" / "service.py").write_text(INSIDE_LINE + "\n")
        db_path = _build_db(repo / ".code-indexer" / "scip", "pkg/service.py")

        assert _context_via_engine(db_path) == INSIDE_LINE

    def test_monorepo_subproject_path_returns_context(self, tmp_path):
        repo = tmp_path / "repo"
        subproject = repo / "services" / "backend"
        subproject.mkdir(parents=True)
        (subproject / "app.py").write_text("# header\n" + INSIDE_LINE + "\n")
        db_path = _build_db(
            repo / ".code-indexer" / "scip" / "services" / "backend", "app.py", line=1
        )

        assert _context_via_engine(db_path) == INSIDE_LINE

    def test_subproject_path_to_sibling_inside_repo_returns_context(self, tmp_path):
        """Confinement is the REPOSITORY root, not the sub-project directory."""
        repo = tmp_path / "repo"
        (repo / "services" / "backend").mkdir(parents=True)
        (repo / "services" / "shared").mkdir(parents=True)
        (repo / "services" / "shared" / "util.py").write_text(INSIDE_LINE + "\n")
        db_path = _build_db(
            repo / ".code-indexer" / "scip" / "services" / "backend",
            "../shared/util.py",
        )

        assert _context_via_engine(db_path) == INSIDE_LINE

    def test_symlink_resolving_inside_repo_returns_context(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "real.py").write_text(INSIDE_LINE + "\n")
        (repo / "alias.py").symlink_to(repo / "src" / "real.py")
        db_path = _build_db(repo / ".code-indexer" / "scip", "alias.py")

        assert _context_via_engine(db_path) == INSIDE_LINE


class TestServerQueryServiceSurface:
    """The service behind the MCP/REST scip tools picks up any
    ``<repo>/.code-indexer/scip/**/*.scip.db``, including one committed to
    the repository; its serialized results carry only in-root context."""

    def _service_results(self, golden_repos_dir: Path) -> list:
        from code_indexer.server.services.scip_query_service import (
            SCIPQueryService,
        )

        service = SCIPQueryService(
            golden_repos_dir=golden_repos_dir, access_filtering_service=None
        )
        results = service.find_definition(
            SYMBOL, exact=True, repository_alias="example-repo"
        )
        assert len(results) == 1
        return results

    def test_repository_supplied_db_returns_no_outside_content(self, tmp_path):
        _write_outside_file(tmp_path)
        golden = tmp_path / "golden-repos"
        repo = golden / "example-repo"
        _build_db(repo / ".code-indexer" / "scip", "../../outside/settings.py")

        results = self._service_results(golden)

        assert results[0]["context"] is None
        assert OUTSIDE_LINE not in repr(results)

    @requires_proc_fd
    def test_in_repo_path_returns_context(self, tmp_path):
        golden = tmp_path / "golden-repos"
        repo = golden / "example-repo"
        repo.mkdir(parents=True)
        (repo / "service.py").write_text(INSIDE_LINE + "\n")
        _build_db(repo / ".code-indexer" / "scip", "service.py")

        assert self._service_results(golden)[0]["context"] == INSIDE_LINE


def _service(golden_repos_dir: Path):
    from code_indexer.server.services.scip_query_service import SCIPQueryService

    return SCIPQueryService(
        golden_repos_dir=golden_repos_dir, access_filtering_service=None
    )


def _write_index_metadata(scip_dir: Path, project_root: str) -> None:
    """Write the ``index.scip`` protobuf beside the db with the given
    ``metadata.project_root`` (repository-supplied data)."""
    from code_indexer.scip.protobuf import scip_pb2

    # scip_pb2 is protoc-generated: its message classes are built at runtime,
    # so mypy cannot see them.
    index = scip_pb2.Index()  # type: ignore[attr-defined]
    index.metadata.project_root = project_root
    (scip_dir / "index.scip").write_bytes(index.SerializeToString())


def _two_repositories_sharing_one_index(golden: Path) -> Path:
    """``other-repo`` owns a real index; ``example-repo`` commits its
    ``.code-indexer/scip`` as a symlink to it. Returns example-repo's
    (unresolved) scip directory."""
    other = golden / "other-repo"
    other.mkdir(parents=True)
    (other / "service.py").write_text(INSIDE_LINE + "\n")
    _build_db(other / ".code-indexer" / "scip", "service.py")
    repo = golden / "example-repo"
    (repo / ".code-indexer").mkdir(parents=True)
    scip_dir = repo / ".code-indexer" / "scip"
    scip_dir.symlink_to(other / ".code-indexer" / "scip")
    return scip_dir


class TestConfinementRootIsNotSteerable:
    """The confinement root is the repository the index was found in, never
    a location chosen by the repository's own symlinks or index metadata."""

    def test_symlinked_scip_dir_and_index_metadata_touch_nothing_outside(
        self, tmp_path
    ):
        outside_file = _write_outside_file(tmp_path)
        golden = tmp_path / "golden-repos"
        repo = golden / "example-repo"
        real_scip = repo / "build" / "scipdata"
        _build_db(real_scip, "settings.py")
        _write_index_metadata(real_scip, str(outside_file.parent))
        (repo / ".code-indexer").mkdir(parents=True)
        (repo / ".code-indexer" / "scip").symlink_to(real_scip)

        results = _service(golden).find_definition(
            SYMBOL, exact=True, repository_alias="example-repo"
        )

        assert len(results) == 1
        assert results[0]["context"] is None
        assert OUTSIDE_LINE not in repr(results)
        assert not (outside_file.parent / ".code-indexer").exists()

    @requires_proc_fd
    def test_scip_dir_symlinked_to_another_repository_is_refused(
        self, tmp_path, caplog
    ):
        golden = tmp_path / "golden-repos"
        _two_repositories_sharing_one_index(golden)
        service = _service(golden)

        with caplog.at_level(logging.WARNING):
            refused = service.find_definition(
                SYMBOL, exact=True, repository_alias="example-repo"
            )
        own = service.find_definition(SYMBOL, exact=True, repository_alias="other-repo")

        assert refused == []
        assert any(r.levelno == logging.WARNING for r in caplog.records)
        assert len(own) == 1 and own[0]["context"] == INSIDE_LINE

    def test_discovery_lists_no_index_outside_the_selected_repository(
        self, tmp_path, caplog
    ):
        golden = tmp_path / "golden-repos"
        _two_repositories_sharing_one_index(golden)
        service = _service(golden)

        with caplog.at_level(logging.WARNING):
            listed = service.find_scip_files(repository_alias="example-repo")

        assert listed == []
        assert any(r.levelno == logging.WARNING for r in caplog.records)
        assert len(service.find_scip_files(repository_alias="other-repo")) == 1

    def test_engine_refuses_index_resolving_outside_its_repository(self, tmp_path):
        golden = tmp_path / "golden-repos"
        scip_dir = _two_repositories_sharing_one_index(golden)

        with pytest.raises(PermissionError):
            SCIPQueryEngine(scip_dir / "index.scip.db")

    def test_composite_impact_refuses_index_of_another_repository(self, tmp_path):
        from code_indexer.scip.query.composites import analyze_impact

        golden = tmp_path / "golden-repos"
        scip_dir = _two_repositories_sharing_one_index(golden)
        own_dir = golden / "other-repo" / ".code-indexer" / "scip"

        assert analyze_impact("ExampleService", own_dir).target_location is not None
        assert analyze_impact("ExampleService", scip_dir).target_location is None

    @requires_proc_fd
    def test_local_cli_opt_out_queries_relocated_index(self, tmp_path):
        """The local CLI (``confine_to_repo_root=False``) may keep its index
        behind a ``.code-indexer`` symlink that leaves the repository."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "service.py").write_text(INSIDE_LINE + "\n")
        relocated = tmp_path / "relocated" / "ci"
        _build_db(relocated / "scip", "service.py")
        (repo / ".code-indexer").symlink_to(relocated)

        engine = SCIPQueryEngine(
            repo / ".code-indexer" / "scip" / "index.scip.db",
            confine_to_repo_root=False,
        )
        results = engine.find_definition(SYMBOL, exact=True)

        assert len(results) == 1 and results[0].context == INSIDE_LINE

    def test_composite_opt_out_queries_relocated_index(self, tmp_path):
        from code_indexer.scip.query.composites import analyze_impact

        repo = tmp_path / "repo"
        repo.mkdir()
        relocated = tmp_path / "relocated" / "ci"
        _build_db(relocated / "scip", "service.py")
        (repo / ".code-indexer").symlink_to(relocated)
        scip_dir = repo / ".code-indexer" / "scip"

        assert analyze_impact("ExampleService", scip_dir).target_location is None
        opted_out = analyze_impact(
            "ExampleService", scip_dir, confine_to_repo_root=False
        )
        assert opted_out.target_location is not None

    def test_backend_without_repository_root_fails_closed(self, tmp_path, caplog):
        project = tmp_path / "project"
        project.mkdir()
        (project / "service.py").write_text(INSIDE_LINE + "\n")
        db_path = _build_db(tmp_path / "loose", "service.py")

        backend = DatabaseBackend(db_path, project_root=str(project))
        with caplog.at_level(logging.WARNING, logger=BACKENDS_LOGGER):
            results = backend.find_definition(SYMBOL, exact=True)

        assert len(results) == 1 and results[0].context is None
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    @requires_proc_fd
    def test_backend_with_explicit_trusted_root_returns_context(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "service.py").write_text(INSIDE_LINE + "\n")
        db_path = _build_db(tmp_path / "loose", "service.py")

        backend = DatabaseBackend(
            db_path, project_root=str(project), trusted_root=project
        )
        results = backend.find_definition(SYMBOL, exact=True)

        assert len(results) == 1 and results[0].context == INSIDE_LINE


class TestLocalCliFollowsRelocatedIndex:
    """The local ``cidx scip`` commands query the user's own index even when
    ``.code-indexer`` is a symlink that leaves the checkout."""

    @pytest.fixture
    def relocated_checkout(self, tmp_path, monkeypatch):
        from code_indexer.scip.status import (
            GenerationStatus,
            OverallStatus,
            ProjectStatus,
            StatusTracker,
        )

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "service.py").write_text(INSIDE_LINE + "\n")
        relocated = tmp_path / "relocated" / "ci"
        _build_db(relocated / "scip", "service.py")
        (repo / ".code-indexer").symlink_to(relocated)
        StatusTracker(repo / ".code-indexer" / "scip").save(
            GenerationStatus(
                overall_status=OverallStatus.SUCCESS,
                total_projects=1,
                successful_projects=1,
                failed_projects=0,
                projects={
                    ".": ProjectStatus(
                        status=OverallStatus.SUCCESS,
                        language="python",
                        build_system="pip",
                        timestamp="2026-01-01T00:00:00",
                    )
                },
            )
        )
        monkeypatch.chdir(repo)
        return repo

    def test_cli_definition_reads_relocated_index(self, relocated_checkout):
        import click.testing

        from code_indexer.cli_scip import scip_definition

        result = click.testing.CliRunner().invoke(scip_definition, [SYMBOL, "--exact"])

        assert result.exit_code == 0, result.output
        assert "service.py" in result.output

    def test_cli_context_reads_relocated_index(self, relocated_checkout):
        import click.testing

        from code_indexer.cli_scip import scip_context

        result = click.testing.CliRunner().invoke(scip_context, ["ExampleService"])

        assert result.exit_code == 0, result.output
        assert "service.py" in result.output


class TestReadIsVerifiedAfterOpen:
    @requires_proc_fd
    def test_symlink_swapped_after_check_returns_no_outside_content(
        self, tmp_path, monkeypatch
    ):
        """The file actually opened must lie inside the root, not just the
        path that was checked: swap the checked file for an outside symlink
        right after the containment check resolves it."""
        outside_file = _write_outside_file(tmp_path)
        repo = tmp_path / "repo"
        target = repo / "pkg" / "service.py"
        target.parent.mkdir(parents=True)
        target.write_text(INSIDE_LINE + "\n")
        engine = SCIPQueryEngine(
            _build_db(repo / ".code-indexer" / "scip", "pkg/service.py")
        )
        real_resolve = Path.resolve
        swapped: List[bool] = []

        def resolve_then_swap(self, *args, **kwargs):
            result = real_resolve(self, *args, **kwargs)
            if not swapped and self == target:
                swapped.append(True)
                target.unlink()
                target.symlink_to(outside_file)
            return result

        monkeypatch.setattr(Path, "resolve", resolve_then_swap)
        results = engine.find_definition(SYMBOL, exact=True)

        assert swapped, "the containment check must resolve the stored path"
        assert len(results) == 1 and results[0].context is None
        assert OUTSIDE_LINE not in repr(results)

    def test_unavailable_fd_verification_omits_context_with_one_warning(
        self, tmp_path, monkeypatch, caplog
    ):
        """Where the opened file cannot be verified (no /proc fd links, as
        on non-Linux hosts) context is omitted and logged once per call."""
        import os

        repo = tmp_path / "repo"
        (repo / "pkg").mkdir(parents=True)
        (repo / "pkg" / "service.py").write_text(INSIDE_LINE + "\n")
        (repo / "pkg" / "other.py").write_text(INSIDE_LINE + "\n")
        db_path = _build_db(repo / ".code-indexer" / "scip", "pkg/service.py")
        _add_definition(db_path, 2, "pkg/other.py")
        real_readlink = os.readlink

        def readlink_without_proc_fd(path, *args, **kwargs):
            if str(path).startswith("/proc/self/fd/"):
                raise OSError("fd links unavailable")
            return real_readlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "readlink", readlink_without_proc_fd)
        with caplog.at_level(logging.WARNING, logger=BACKENDS_LOGGER):
            results = SCIPQueryEngine(db_path).find_definition(SYMBOL, exact=True)

        assert len(results) == 2 and all(r.context is None for r in results)
        warnings = [
            r
            for r in caplog.records
            if r.name == BACKENDS_LOGGER and r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert "verif" in warnings[0].getMessage()


class TestRefusalLogging:
    def test_one_warning_summarises_all_refused_paths(self, tmp_path, caplog):
        _write_outside_file(tmp_path)
        (tmp_path / "outside" / "other.py").write_text(OUTSIDE_LINE + "\n")
        repo = tmp_path / "repo"
        db_path = _build_db(repo / ".code-indexer" / "scip", "../outside/settings.py")
        _add_definition(db_path, 2, "../outside/other.py")

        with caplog.at_level(logging.WARNING, logger=BACKENDS_LOGGER):
            results = SCIPQueryEngine(db_path).find_definition(SYMBOL, exact=True)

        assert len(results) == 2 and all(r.context is None for r in results)
        warnings = [
            r
            for r in caplog.records
            if r.name == BACKENDS_LOGGER and r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert "2" in warnings[0].getMessage()

    @staticmethod
    def _scip_dir_borrowing_two_indexes(golden: Path) -> Path:
        """example-repo's real scip dir holds symlinked index files pointing
        at two other repositories' indexes; both must be refused. (Files,
        not directories: pathlib's ``**`` glob does not descend into
        symlinked directories.)"""
        scip_dir = golden / "example-repo" / ".code-indexer" / "scip"
        scip_dir.mkdir(parents=True)
        for name in ("first-repo", "second-repo"):
            other = golden / name
            other.mkdir(parents=True)
            other_db = _build_db(other / ".code-indexer" / "scip", "service.py")
            (scip_dir / f"{name}.scip.db").symlink_to(other_db)
        return scip_dir

    @staticmethod
    def _composite_records(caplog, level: int) -> list:
        return [
            r
            for r in caplog.records
            if r.name == COMPOSITES_LOGGER and r.levelno == level
        ]

    def test_impact_target_lookup_summarises_refused_indexes(self, tmp_path, caplog):
        from code_indexer.scip.query.composites import analyze_impact

        scip_dir = self._scip_dir_borrowing_two_indexes(tmp_path / "golden-repos")

        with caplog.at_level(logging.DEBUG, logger=COMPOSITES_LOGGER):
            result = analyze_impact("ExampleService", scip_dir)

        assert result.target_location is None
        assert self._composite_records(caplog, logging.ERROR) == []
        warnings = self._composite_records(caplog, logging.WARNING)
        assert len(warnings) == 1 and "2" in warnings[0].getMessage()

    def test_dependents_walk_summarises_refused_indexes(self, tmp_path, caplog):
        from code_indexer.scip.query.composites import _bfs_traverse_dependents

        scip_dir = self._scip_dir_borrowing_two_indexes(tmp_path / "golden-repos")

        with caplog.at_level(logging.DEBUG, logger=COMPOSITES_LOGGER):
            affected = _bfs_traverse_dependents(
                SYMBOL, scip_dir, 1, None, None, None, None
            )

        assert affected == []
        assert self._composite_records(caplog, logging.ERROR) == []
        warnings = self._composite_records(caplog, logging.WARNING)
        assert len(warnings) == 1 and "2" in warnings[0].getMessage()
