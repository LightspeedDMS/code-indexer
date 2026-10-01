"""Regression tests for Bug #1630: SCIPQueryEngine project_root miscalculation.

TDD: these tests were written BEFORE the fix, to prove the CURRENT
``scip_dir.parent.parent`` heuristic in ``SCIPQueryEngine.__init__``
(src/code_indexer/scip/query/primitives.py) miscalculates ``project_root``
for a sub-project's ``.scip.db`` file -- it is only correct for the
top-level project's database, where the fixed two-level parent walk
happens to line up with the real repo root.

Database path structure (SCIPGenerator, src/code_indexer/scip/generator.py):
    <repo_root>/.code-indexer/scip/[<relative_path>/]index.scip.db
``<relative_path>`` is empty (".") for the top-level project and an
arbitrary-depth sub-project path (e.g. "backend", "services/backend",
"a/b/c") for a monorepo module discovered by ``ProjectDiscovery``.

WHY ``project_root`` MUST be the SUB-PROJECT's own directory (not the
overall repo root) for a sub-project database: verified empirically
against this repo's own live sub-project database
(``.code-indexer/scip/test-fixtures/scip-typescript-mock/index.scip.db``)
with a direct SQL query --

    sqlite3 .../index.scip.db "SELECT relative_path FROM documents LIMIT 3"
    -> 'src/models/AuthToken.ts', 'src/models/User.ts', ...

``documents.relative_path`` (surfaced verbatim as ``QueryResult.file_path``
via ``d.relative_path AS file_path`` in every query in
``src/code_indexer/scip/database/queries.py`` -- no repo-root prefix is
ever added) is relative to the SUB-PROJECT's own directory
(``<repo_root>/<relative_path>``), because the underlying per-language SCIP
indexer is invoked with ``cwd=project_dir`` (the sub-project directory --
see ``src/code_indexer/scip/indexers/python.py``). So ``project_root`` must
resolve to ``<repo_root>/<relative_path>`` for ``_read_context_lines`` to
find the real source file on disk (``project_root_path / file_path``).

Two residual consequences of the miscalculation (both documented in the
issue, and #2 reproduced LIVE in this repo's own ``.code-indexer/scip/``
tree at the time of filing -- ``.code-indexer/scip/.code-indexer/config.json``
is a bogus directory this exact bug created, nested inside the
``test-fixtures/scip-typescript-mock`` sub-project's OWN scip output
folder, i.e. at ``.code-indexer/scip/<relative_path>/.code-indexer``):

1. ``DatabaseBackend._read_context_lines`` cannot resolve source files for
   sub-project query results (wrong root).
2. ``DatabaseBackend._ensure_migration_complete`` (Story #609) derives
   ``config_path`` from ``project_root``, and on a mutable (non-versioned-
   snapshot) database ``update_scip_db_version`` does ``mkdir -p`` on
   ``config_path.parent`` -- creating a bogus ``.code-indexer`` directory
   inside ``.code-indexer/scip/<relative_path>/`` (the sub-project's own
   scip OUTPUT folder) when ``project_root`` points there instead of at
   the real repo root.

DELIBERATE DESIGN NOTE (read before touching ``_ensure_migration_complete``):
``project_root`` and the Story #609 ``scip_db_version`` config-path lookup
have DIFFERENT correct scopes once sub-projects are considered:
  - ``project_root`` (used for ``QueryResult.project`` and
    ``_read_context_lines``) must be the SUB-PROJECT's own directory (see
    above) -- it varies per database.
  - The ``scip_db_version`` marker is a single, repo-wide value and must
    always live at the ONE real ``<repo_root>/.code-indexer/config.json``,
    regardless of which sub-project's database is being opened.
The fix therefore does NOT derive ``config_path`` from ``self.project_root``
at all -- it independently walks up from ``self.db_path`` to find the real
``.code-indexer`` ancestor. ``TestResidualConsequenceNoBogusConfigDir``
exercises this directly against ``DatabaseBackend`` with an explicit
sub-project-scoped ``project_root`` specifically to prove that
decoupling -- the constructor argument alone must not determine where the
version marker is read from or written to.
"""

from pathlib import Path

from code_indexer.scip.database.migration import get_scip_db_version
from code_indexer.scip.database.schema import DatabaseManager
from code_indexer.scip.query.backends import DatabaseBackend
from code_indexer.scip.query.primitives import QueryResult, SCIPQueryEngine

# Story #609 schema version written by DatabaseBackend._ensure_migration_complete
# once the required indexes exist (see src/code_indexer/scip/database/migration.py).
EXPECTED_SCIP_DB_VERSION = 2

# 0-based line index (per _read_context_lines' documented contract) of
# "def foo():" in the 4-line synthetic source file used by
# TestResidualConsequenceContextLines.
FOO_DEFINITION_LINE = 2


def _create_real_scip_db(scip_path: Path) -> Path:
    """Build a real, schema-valid ``.scip.db`` file at ``scip_path`` + ``.db``.

    Deliberately never creates ``scip_path`` itself (the ``.scip`` protobuf
    file) -- this reproduces the real, everyday production state where the
    protobuf file has already been deleted after conversion (see project
    CLAUDE.md "SCIP Index File Lifecycle"), forcing
    ``SCIPQueryEngine.__init__`` into its "derive project root from db path"
    fallback branch, which is the branch this bug lives in.
    """
    manager = DatabaseManager(scip_path)
    manager.create_schema()
    manager.create_indexes()
    # DatabaseManager.db_path is not type-annotated (mypy infers Any for
    # it project-wide) -- replicate its exact construction here so this
    # helper's return type stays a real Path, not Any.
    return Path(str(scip_path) + ".db")


def _assert_no_bogus_dir_and_real_config_version(repo_root: Path) -> None:
    """Shared assertions for TestResidualConsequenceNoBogusConfigDir: no
    bogus '.code-indexer' directory anywhere inside the repo's
    '.code-indexer/scip/' output tree, and the Story #609 version marker
    recorded at the one real repo-root config.json instead.

    Scanning the whole 'scip/' subtree (rather than guessing one specific
    nested path) is deliberate: the pre-fix buggy formula's bogus location
    varies with sub-project nesting depth (e.g. it lands at
    '<repo_root>/.code-indexer/scip/.code-indexer' for a 2-level-deep
    sub-project, but elsewhere for other depths) -- the real, depth-
    independent invariant is that '.code-indexer' must never appear
    anywhere under 'scip/', only once, directly under repo_root itself.
    """
    scip_base_dir = repo_root / ".code-indexer" / "scip"
    bogus_dirs = list(scip_base_dir.rglob(".code-indexer"))
    assert bogus_dirs == [], (
        f"Bug #1630: bogus '.code-indexer' director(y/ies) created inside "
        f"the repo's .code-indexer/scip/ output tree: {bogus_dirs}"
    )

    real_config_path = repo_root / ".code-indexer" / "config.json"
    assert get_scip_db_version(real_config_path) == EXPECTED_SCIP_DB_VERSION, (
        "Expected the real repo-root .code-indexer/config.json to "
        "record the Story #609 scip_db_version marker instead"
    )


class TestProjectRootTopLevel:
    """Top-level .scip.db: project_root must be the repo root. Already
    correct on the pre-fix code -- must keep working after the fix (no
    regression)."""

    def test_top_level_scip_db_project_root_is_repo_root(self, tmp_path):
        repo_root = tmp_path / "repo"
        scip_dir = repo_root / ".code-indexer" / "scip"
        scip_dir.mkdir(parents=True)
        db_path = _create_real_scip_db(scip_dir / "index.scip")

        engine = SCIPQueryEngine(db_path)

        assert Path(engine.backend.project_root).resolve() == repo_root.resolve()


class TestProjectRootSubprojectDepths:
    """Sub-project .scip.db at multiple nesting depths: project_root must
    resolve to the sub-project's own directory (repo_root / relative_path)
    regardless of how deep relative_path is -- not the fixed two-level
    top-level structure."""

    def test_subproject_scip_db_depth_1_project_root_is_subproject_dir(self, tmp_path):
        """Sub-project nested 1 level deep (e.g. 'backend')."""
        repo_root = tmp_path / "repo"
        relative_path = Path("backend")
        scip_dir = repo_root / ".code-indexer" / "scip" / relative_path
        scip_dir.mkdir(parents=True)
        db_path = _create_real_scip_db(scip_dir / "index.scip")

        engine = SCIPQueryEngine(db_path)

        expected = (repo_root / relative_path).resolve()
        assert Path(engine.backend.project_root).resolve() == expected

    def test_subproject_scip_db_depth_2_project_root_is_subproject_dir(self, tmp_path):
        """Sub-project nested 2 levels deep (e.g. 'services/backend') --
        mirrors the real 'test-fixtures/scip-typescript-mock' layout found
        live in this repo's own .code-indexer/scip/ tree."""
        repo_root = tmp_path / "repo"
        relative_path = Path("services") / "backend"
        scip_dir = repo_root / ".code-indexer" / "scip" / relative_path
        scip_dir.mkdir(parents=True)
        db_path = _create_real_scip_db(scip_dir / "index.scip")

        engine = SCIPQueryEngine(db_path)

        expected = (repo_root / relative_path).resolve()
        assert Path(engine.backend.project_root).resolve() == expected

    def test_subproject_scip_db_depth_3_project_root_is_subproject_dir(self, tmp_path):
        """Sub-project nested 3 levels deep (e.g. 'a/b/c')."""
        repo_root = tmp_path / "repo"
        relative_path = Path("a") / "b" / "c"
        scip_dir = repo_root / ".code-indexer" / "scip" / relative_path
        scip_dir.mkdir(parents=True)
        db_path = _create_real_scip_db(scip_dir / "index.scip")

        engine = SCIPQueryEngine(db_path)

        expected = (repo_root / relative_path).resolve()
        assert Path(engine.backend.project_root).resolve() == expected


class TestResidualConsequenceContextLines:
    """Bug #1630 residual consequence #1: _read_context_lines must resolve
    source files for sub-project query results once project_root is
    computed correctly (as the sub-project's own directory)."""

    def test_context_lines_resolve_for_subproject_query_result(self, tmp_path):
        repo_root = tmp_path / "repo"
        relative_path = Path("backend") / "service"

        # Real source file lives under the sub-project's OWN directory
        # (repo_root/relative_path), matching how ProjectDiscovery/
        # SCIPGenerator actually lay out sub-project source trees
        # (project_dir = repo_root / project.relative_path).
        subproject_source_dir = repo_root / relative_path
        subproject_source_dir.mkdir(parents=True)
        source_file = subproject_source_dir / "app.py"
        source_file.write_text("line0\nline1\ndef foo():\n    pass\n")

        scip_dir = repo_root / ".code-indexer" / "scip" / relative_path
        scip_dir.mkdir(parents=True)
        db_path = _create_real_scip_db(scip_dir / "index.scip")

        engine = SCIPQueryEngine(db_path)

        # file_path is relative to the sub-project's own directory --
        # verified against a real sub-project database in this repo (see
        # module docstring): documents.relative_path values never include
        # the sub-project's own relative_path prefix.
        result = QueryResult(
            symbol="foo",
            project=engine.backend.project_root,
            file_path="app.py",
            line=FOO_DEFINITION_LINE,
            column=0,
            kind="definition",
        )

        engine.backend._read_context_lines([result])

        # Per _read_context_lines' own documented contract (backends.py):
        # "Line numbers in the SCIP database are 0-based; context receives
        # the raw line string with the trailing newline stripped."
        assert result.context == "def foo():"


class TestResidualConsequenceNoBogusConfigDir:
    """Bug #1630 residual consequence #2: on the mutable (non-versioned-
    snapshot) path, opening a sub-project's .scip.db must not mkdir -p a
    bogus '.code-indexer' directory inside the sub-project's own scip/
    output folder -- the Story #609 scip_db_version marker must always be
    read from / written to the one real repo-root config.json instead,
    regardless of what project_root the DatabaseBackend instance uses for
    resolving query-result source paths (see module docstring's DELIBERATE
    DESIGN NOTE for why these two are intentionally decoupled).

    ``_ensure_migration_complete`` is a private method with no public
    trigger other than ``DatabaseBackend.__init__`` itself (see its own
    docstring: "Never called when self.read_only is True" -- it runs
    automatically from __init__ otherwise) -- exercising it through the
    constructor is the same approach the pre-existing Bug #1616 regression
    test ``test_mutable_non_snapshot_db_still_runs_pending_migration`` in
    tests/unit/test_scip_backends.py uses for the same reason.
    """

    def test_mutable_subproject_db_does_not_create_bogus_code_indexer_dir(
        self, tmp_path
    ):
        repo_root = tmp_path / "repo"
        relative_path = Path("services") / "backend"
        scip_dir = repo_root / ".code-indexer" / "scip" / relative_path
        scip_dir.mkdir(parents=True)
        db_path = _create_real_scip_db(scip_dir / "index.scip")

        SCIPQueryEngine(db_path)

        _assert_no_bogus_dir_and_real_config_version(repo_root)

    def test_direct_database_backend_subproject_config_path_targets_repo_root(
        self, tmp_path
    ):
        """Same assertion, exercised directly against DatabaseBackend with
        an explicit sub-project-scoped project_root, proving the fix lives
        in the config-path derivation itself and does not merely happen to
        work because of how SCIPQueryEngine.__init__ computes project_root
        upstream."""
        repo_root = tmp_path / "repo"
        relative_path = Path("services") / "backend"
        scip_dir = repo_root / ".code-indexer" / "scip" / relative_path
        scip_dir.mkdir(parents=True)
        db_path = _create_real_scip_db(scip_dir / "index.scip")

        # Deliberately the SUB-PROJECT's own directory, matching what
        # SCIPQueryEngine.__init__ now passes as project_root (see
        # TestProjectRootSubprojectDepths) -- this is NOT the repo root,
        # and _ensure_migration_complete must not use it to locate
        # config.json.
        subproject_root = repo_root / relative_path

        DatabaseBackend(db_path, project_root=str(subproject_root))

        _assert_no_bogus_dir_and_real_config_version(repo_root)
