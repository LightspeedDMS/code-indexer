"""Story #1418 Phase 2 of 3: CLI child-side embedding-stats bootstrap
installer wiring in cli.py's `index` command.

Bug #1441 (production hotfix): the `if bootstrap_dir:` branch's import of
`install_embedding_stats_writer_from_bootstrap` was a bare, unguarded
top-level import. On any interpreter lacking `psycopg` (confirmed in
production, where the `cidx index` child subprocess's interpreter is
separate from the postgres-capable server venv), that import raises
`ModuleNotFoundError` BEFORE the installer function's own internal
fail-open try/except ever gets a chance to run -- crashing the entire
`cidx index` invocation and aborting real indexing work, in direct
violation of embedding_stats_child_wiring.py's documented "FAIL-OPEN,
never fail-loud" contract.

ROUND 2 CORRECTION: the round-1 fix (a try/except around the bootstrap-dir
branch) was INCOMPLETE -- the except block's OWN fallback import,
`from .server.services.embedding_stats_writer import EmbeddingStatsWriter,
NoOpWriter`, was itself transitively NOT psycopg-free
(embedding_stats_writer -> embedding_call_stats -> connection_pool ->
`import psycopg`), so a genuinely psycopg-less interpreter crashed a
SECOND time, uncaught, inside the except block's own recovery path. The
round-1 regression test used a name-scoped `builtins.__import__` patch
that only poisoned one specific module name AND relied on
`embedding_stats_writer`/`embedding_call_stats` already being cached in
`sys.modules` from earlier imports in the same test process -- so the
fallback import's transitive psycopg dependency never actually re-ran
during that test, producing a false green. The corrected harness below
(`TestEmbeddingStatsModulesImportGenuinelyWithoutPsycopgBug1441`) instead
runs a real sys.meta_path import blocker in a FRESH child subprocess
(psycopg/psycopg_pool genuinely absent from that subprocess's
sys.modules), so the blocker is genuinely exercised rather than bypassed
by module caching.

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
import os
import subprocess
import sys
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


_PSYCOPG_BLOCKER_SITECUSTOMIZE = """\
import sys
from importlib.abc import MetaPathFinder


class _PsycopgBlocker(MetaPathFinder):
    \"\"\"Bug #1441 regression harness: makes `import psycopg`/`psycopg_pool`
    genuinely fail for the WHOLE interpreter, regardless of which module
    tries to import them -- a real import blocker via sys.meta_path, not a
    name-scoped builtins.__import__ patch (which only poisons one specific
    module name and can be masked by sys.modules caching).\"\"\"

    _blocked = ("psycopg", "psycopg_pool", "psycopg2", "psycopg_binary")

    def find_spec(self, fullname, path, target=None):
        if fullname.split(".")[0] in self._blocked:
            raise ModuleNotFoundError(f"No module named {fullname!r}")
        return None


sys.meta_path.insert(0, _PsycopgBlocker())
"""


def _run_in_psycopg_blocked_subprocess(
    tmp_path: Path, code: str, *, env_overrides: dict | None = None
) -> subprocess.CompletedProcess:
    """Run ``code`` in a FRESH child interpreter with a genuine
    sys.meta_path import blocker active (via sitecustomize.py, which the
    ``site`` module auto-imports at interpreter startup) that makes
    ``import psycopg``/``psycopg_pool`` fail for the ENTIRE process --
    proving (or disproving) real psycopg-freedom, not a mocked/patched
    illusion of it. A fresh interpreter guarantees psycopg/psycopg_pool are
    NOT already cached in sys.modules, so the blocker is genuinely
    exercised (the round-1 regression test was a false green precisely
    because in-process module caching bypassed the poisoned import for the
    real second-level failure -- see module docstring)."""
    blocker_dir = tmp_path / "psycopg_blocker"
    blocker_dir.mkdir(exist_ok=True)
    (blocker_dir / "sitecustomize.py").write_text(_PSYCOPG_BLOCKER_SITECUSTOMIZE)

    env = os.environ.copy()
    src_dir = str(_REPO_ROOT / "src")
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        [str(blocker_dir), src_dir]
        + ([existing_pythonpath] if existing_pythonpath else [])
    )
    if env_overrides:
        for key, value in env_overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value

    return subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestPsycopgBlockerHarnessGenuinelyBlocksImport:
    """Sanity check on the regression harness ITSELF: `import psycopg` must
    genuinely fail under the blocker. If this test fails, the tests below
    would be false negatives/positives for the wrong reason (a broken
    harness, not a working or broken fix)."""

    def test_psycopg_import_genuinely_fails_under_blocker(self, tmp_path):
        result = _run_in_psycopg_blocked_subprocess(
            tmp_path,
            "import psycopg\nprint('IMPORTED_PSYCOPG_UNEXPECTEDLY')",
        )
        assert result.returncode != 0, (
            f"Expected `import psycopg` to fail under the blocker, but it "
            f"succeeded. stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "IMPORTED_PSYCOPG_UNEXPECTEDLY" not in result.stdout


class TestEmbeddingStatsModulesImportGenuinelyWithoutPsycopgBug1441:
    """Bug #1441 round 2 regression coverage (see module docstring for the
    full root-cause writeup). Proves, via a genuine sys.meta_path import
    blocker in a fresh subprocess (never a name-scoped mock), that:
      - embedding_stats_writer.py and embedding_call_stats.py import
        cleanly with NO postgres packages installed/importable, and
      - _install_embedding_stats_writer_for_index() does not crash and
        falls back to NoOpWriter under a real psycopg-less interpreter,
        in BOTH the bootstrap-dir-present and bootstrap-dir-absent
        branches."""

    def test_embedding_stats_writer_module_imports_without_psycopg(self, tmp_path):
        result = _run_in_psycopg_blocked_subprocess(
            tmp_path,
            "import code_indexer.server.services.embedding_stats_writer\n"
            "print('IMPORT_OK')",
        )
        assert result.returncode == 0 and "IMPORT_OK" in result.stdout, (
            f"code_indexer.server.services.embedding_stats_writer must "
            f"import cleanly with NO postgres packages installed/"
            f"importable -- NoOpWriter is the last-resort, zero-dependency "
            f"fallback.\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
        )

    def test_embedding_call_stats_module_imports_without_psycopg(self, tmp_path):
        result = _run_in_psycopg_blocked_subprocess(
            tmp_path,
            "import code_indexer.server.services.embedding_call_stats\n"
            "print('IMPORT_OK')",
        )
        assert result.returncode == 0 and "IMPORT_OK" in result.stdout, (
            f"code_indexer.server.services.embedding_call_stats must import "
            f"cleanly with NO postgres packages installed/importable -- "
            f"ConnectionPool is only needed by the postgres-specific "
            f"backend class, not at module import time.\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )

    def test_bootstrap_dir_present_falls_back_to_noop_under_real_psycopg_block(
        self, tmp_path
    ):
        bootstrap_dir = tmp_path / "server_dir"
        bootstrap_dir.mkdir()
        code = (
            "import os\n"
            f"os.environ[{_ENV_VAR_NAME!r}] = {str(bootstrap_dir)!r}\n"
            "from code_indexer.cli import _install_embedding_stats_writer_for_index\n"
            "_install_embedding_stats_writer_for_index()\n"
            "from code_indexer.server.services.embedding_stats_writer import (\n"
            "    EmbeddingStatsWriter,\n"
            "    NoOpWriter,\n"
            ")\n"
            "active = EmbeddingStatsWriter.get_active()\n"
            "print('ACTIVE_TYPE:' + type(active).__name__)\n"
        )
        result = _run_in_psycopg_blocked_subprocess(tmp_path, code)

        assert result.returncode == 0, (
            f"_install_embedding_stats_writer_for_index() must NOT crash "
            f"the process under a real psycopg-less interpreter -- this is "
            f"the exact production incident (#1441).\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        assert "ACTIVE_TYPE:NoOpWriter" in result.stdout, (
            f"Expected the active writer to fall back to NoOpWriter.\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )

    def test_bootstrap_dir_absent_falls_back_to_noop_under_real_psycopg_block(
        self, tmp_path
    ):
        code = (
            "from code_indexer.cli import _install_embedding_stats_writer_for_index\n"
            "_install_embedding_stats_writer_for_index()\n"
            "from code_indexer.server.services.embedding_stats_writer import (\n"
            "    EmbeddingStatsWriter,\n"
            "    NoOpWriter,\n"
            ")\n"
            "active = EmbeddingStatsWriter.get_active()\n"
            "print('ACTIVE_TYPE:' + type(active).__name__)\n"
        )
        result = _run_in_psycopg_blocked_subprocess(
            tmp_path, code, env_overrides={_ENV_VAR_NAME: None}
        )

        assert result.returncode == 0, (
            f"_install_embedding_stats_writer_for_index() (no bootstrap "
            f"dir) must NOT crash under a real psycopg-less interpreter -- "
            f"this latent defect existed even in the else-branch, which "
            f"round 1 never touched.\nstdout={result.stdout!r}\n"
            f"stderr={result.stderr!r}"
        )
        assert "ACTIVE_TYPE:NoOpWriter" in result.stdout, (
            f"Expected the active writer to fall back to NoOpWriter.\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
