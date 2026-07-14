"""Bug #1392 regression guard: CLI import budget must not regress.

The new HNSWCapabilityError / _ensure_hnswlib_capability() code added to
`storage/hnsw_index_manager.py` uses the module's existing top-level
`try: import hnswlib` guard -- it does not add any new eager import. This
test proves `hnswlib` is still absent from `sys.modules` after
`from code_indexer.cli import cli`, mirroring the exact subprocess pattern
used by `tests/unit/xray/test_lazy_load.py` for tree_sitter.

Run in a fresh subprocess (not in-process) because pytest may have already
imported hnswlib via other test modules in the same session.
"""

import subprocess
import sys
from pathlib import Path

SRC_ROOT = str(Path(__file__).parent.parent.parent.parent / "src")
SUBPROCESS_TIMEOUT_SECONDS = 30


class TestHnswlibLazyImportBudget:
    """hnswlib absent from sys.modules after CLI import (subprocess proof)."""

    def test_hnswlib_not_in_modules_after_cli_import(self) -> None:
        code = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "from code_indexer.cli import cli; "
            "print('hnswlib:', 'hnswlib' in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
        assert result.returncode == 0, (
            f"Subprocess failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "hnswlib: False" in result.stdout, (
            f"LAZY-LOAD REGRESSION (Bug #1392): hnswlib was imported at CLI "
            f"startup.\nSubprocess output: {result.stdout!r}"
        )
