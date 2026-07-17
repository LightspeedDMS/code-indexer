"""Story #1418 Phase 2 of 3: CLI child-side embedding-stats bootstrap
installer wiring in cli.py's `index` command.

`_install_embedding_stats_writer_for_index()` (cli.py) is the small,
directly-testable helper the `index` command entrypoint calls. It reads
CIDX_EMBEDDING_STATS_BOOTSTRAP_DIR and either:
  - installs a real CrossProcessBootstrapWriter via
    install_embedding_stats_writer_from_bootstrap(bootstrap_dir), or
  - installs NoOpWriter directly (standalone CLI, no server orchestration)
    WITHOUT ever calling the installer function.

These tests exercise the ACTUAL cli.py helper (not a copy of its logic),
so the branch decision itself is proven, not just the underlying
primitives (which are separately, exhaustively unit-tested in
tests/unit/server/storage/postgres/test_embedding_stats_child_wiring_1418.py).

An AST-based guard (not raw substring search, which would false-positive-
match the `def` line or prose) proves the helper CALL (not its definition)
appears in the `index` command's body BEFORE the `if index_commits:`
statement -- unlike CIDX_TEMPORAL_PG_BOOTSTRAP_DIR (read ONLY inside that
branch), this must also cover plain (non-temporal) indexing.

Note on the module-level EmbeddingStatsWriter._active singleton reset used
in the fixture below: this mirrors the identical, already-established
teardown pattern used throughout
tests/unit/server/services/test_embedding_stats_writer_1418.py and the
other Story #1418 Phase 2 test files -- test-only direct state reset,
never used in production code; this test module runs single-threaded so
no cross-test race is introduced.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLI_PY = _REPO_ROOT / "src" / "code_indexer" / "cli.py"

_ENV_VAR_NAME = "CIDX_EMBEDDING_STATS_BOOTSTRAP_DIR"
_HELPER_NAME = "_install_embedding_stats_writer_for_index"

# Bounded join timeout for the background flush thread during test
# teardown -- matches CrossProcessBootstrapWriter/InProcessAsyncWriter's
# own stop() default elsewhere in this suite.
_TEARDOWN_STOP_TIMEOUT_SECONDS = 2.0


def _find_index_command_function(tree: ast.Module) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "index":
            return node
    raise AssertionError("cli.py has no `def index(` Click command function")


def _find_helper_call_line(func: ast.FunctionDef) -> int:
    """Return the line number of the bare `_install_embedding_stats_writer_for_index()`
    CALL statement inside func's body -- distinct from its `def` line,
    which also contains the name as a substring but is a different AST
    node type (FunctionDef, not Call)."""
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == _HELPER_NAME
        ):
            return node.lineno
    raise AssertionError(
        f"No call to {_HELPER_NAME}() found inside the `index` command body"
    )


def _find_index_commits_if_line(func: ast.FunctionDef) -> int:
    for node in ast.walk(func):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "index_commits"
        ):
            return node.lineno
    raise AssertionError("No `if index_commits:` statement found in `index` command")


@pytest.fixture(autouse=True)
def _reset_active_writer():
    from code_indexer.server.services.embedding_stats_writer import (
        EmbeddingStatsWriter,
    )

    EmbeddingStatsWriter._active = None
    yield
    writer = EmbeddingStatsWriter._active
    if writer is not None and hasattr(writer, "stop"):
        writer.stop(timeout=_TEARDOWN_STOP_TIMEOUT_SECONDS)
    EmbeddingStatsWriter._active = None


class TestCliIndexCallsInstallerHelperBeforeIndexCommitsBranch:
    def test_helper_call_precedes_index_commits_branch(self):
        tree = ast.parse(_CLI_PY.read_text())
        index_fn = _find_index_command_function(tree)

        helper_call_line = _find_helper_call_line(index_fn)
        index_commits_if_line = _find_index_commits_if_line(index_fn)

        assert helper_call_line < index_commits_if_line, (
            f"{_HELPER_NAME}() is called at line {helper_call_line}, which "
            f"must be BEFORE the `if index_commits:` branch at line "
            f"{index_commits_if_line} -- it must cover plain (non-temporal) "
            f"indexing too, not just --index-commits runs."
        )


class TestInstallEmbeddingStatsWriterForIndexHelper:
    """Directly exercises cli.py's own _install_embedding_stats_writer_for_index()
    helper -- the ACTUAL branch decision, not a copy of its logic."""

    def test_bootstrap_dir_present_installs_cross_process_writer(
        self, monkeypatch, tmp_path
    ):
        import json

        from code_indexer.cli import _install_embedding_stats_writer_for_index
        from code_indexer.server.services.embedding_stats_writer import (
            CrossProcessBootstrapWriter,
            EmbeddingStatsWriter,
        )

        bootstrap_dir = tmp_path / "server_dir"
        bootstrap_dir.mkdir()
        (bootstrap_dir / "config.json").write_text(
            json.dumps({"server_dir": str(bootstrap_dir), "storage_mode": "sqlite"})
        )
        monkeypatch.setenv(_ENV_VAR_NAME, str(bootstrap_dir))

        _install_embedding_stats_writer_for_index()

        assert isinstance(
            EmbeddingStatsWriter.get_active(), CrossProcessBootstrapWriter
        )

    def test_env_var_absent_installs_noop_writer_without_calling_installer(
        self, monkeypatch
    ):
        monkeypatch.delenv(_ENV_VAR_NAME, raising=False)

        from code_indexer.cli import _install_embedding_stats_writer_for_index
        from code_indexer.server.services.embedding_stats_writer import (
            EmbeddingStatsWriter,
            NoOpWriter,
        )

        with patch(
            "code_indexer.server.storage.postgres.embedding_stats_child_wiring."
            "install_embedding_stats_writer_from_bootstrap"
        ) as mock_installer:
            _install_embedding_stats_writer_for_index()

        mock_installer.assert_not_called()
        assert isinstance(EmbeddingStatsWriter.get_active(), NoOpWriter)

    def test_bootstrap_dir_present_registers_atexit_flush_callback(
        self, monkeypatch, tmp_path
    ):
        """Story #1418 Component 3: on process exit (normal completion),
        the writer's best-effort final flush must run. atexit.register is
        used so this fires on any normal interpreter shutdown path
        (sys.exit(0), sys.exit(1), uncaught exception) without needing to
        wrap the entire `index` command body in try/finally."""
        import json

        from code_indexer.cli import _install_embedding_stats_writer_for_index
        from code_indexer.server.services.embedding_stats_writer import (
            EmbeddingStatsWriter,
        )

        bootstrap_dir = tmp_path / "server_dir"
        bootstrap_dir.mkdir()
        (bootstrap_dir / "config.json").write_text(
            json.dumps({"server_dir": str(bootstrap_dir), "storage_mode": "sqlite"})
        )
        monkeypatch.setenv(_ENV_VAR_NAME, str(bootstrap_dir))

        registered_callbacks = []

        def _fake_atexit_register(fn, *args, **kwargs):
            registered_callbacks.append(fn)
            return fn

        with patch(
            "code_indexer.cli.atexit.register", side_effect=_fake_atexit_register
        ):
            _install_embedding_stats_writer_for_index()

        assert len(registered_callbacks) == 1
        writer = EmbeddingStatsWriter.get_active()
        assert writer._thread is not None and writer._thread.is_alive()

        registered_callbacks[0]()  # simulate process exit

        assert not writer._thread.is_alive()
