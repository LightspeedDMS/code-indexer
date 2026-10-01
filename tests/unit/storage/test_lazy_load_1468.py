"""Lazy-load CI gate test for Bug #1468.

Directly importing `FilesystemVectorStore` (the core CLI/solo storage
class) must NOT eagerly pull in `psycopg`/`fastapi` and their transitive
dependency trees -- `FilesystemVectorStore` is the fundamental storage
primitive used by the standalone CLI in solo mode, which has no legitimate
reason to need PostgreSQL or FastAPI.

Traced culprits (both module-level, try/except-guarded imports in
filesystem_vector_store.py that ARE executed unconditionally at import
time, just tolerant of ImportError):
  - `governed_call` -> `coalescer_registry` -> `config_service` ->
    `code_indexer.server.middleware.correlation`, which -- because Python
    always runs a package's __init__.py before any submodule -- forces
    `middleware/__init__.py`'s eager `from .error_handler import
    GlobalErrorHandler` to run, pulling in fastapi.
  - `embedding_cache_audit` -> `search_embed_event_emit` ->
    `search_embed_event_writer` -> `connection_pool.py`'s eager
    `from psycopg_pool import ConnectionPool`, which pulls in psycopg.

Subprocess-based (mirrors tests/unit/xray/test_lazy_load.py's proven
approach exactly): in-process checks are unreliable because pytest loads
psycopg/fastapi via earlier server-focused test files in the same session.
A fresh subprocess has no such contamination.
"""

import sys
import subprocess
from pathlib import Path

SRC_ROOT = str(Path(__file__).parent.parent.parent.parent / "src")


class TestFilesystemVectorStoreLazyLoad:
    def test_psycopg_not_in_modules_after_fsv_import(self) -> None:
        code = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore; "
            "print('psycopg:', 'psycopg' in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Subprocess failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "psycopg: False" in result.stdout, (
            f"LAZY-LOAD VIOLATION (Bug #1468): psycopg was imported merely by "
            f"importing FilesystemVectorStore.\nSubprocess output: {result.stdout!r}"
        )

    def test_fastapi_not_in_modules_after_fsv_import(self) -> None:
        code = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore; "
            "print('fastapi:', 'fastapi' in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Subprocess failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "fastapi: False" in result.stdout, (
            f"LAZY-LOAD VIOLATION (Bug #1468): fastapi was imported merely by "
            f"importing FilesystemVectorStore.\nSubprocess output: {result.stdout!r}"
        )

    def test_storage_package_import_does_not_leak_either(self) -> None:
        """The original repro: importing ANY code_indexer.storage.* submodule
        triggers code_indexer/storage/__init__.py's eager `from
        .filesystem_vector_store import FilesystemVectorStore`."""
        code = (
            "import sys, json; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "from code_indexer.storage.hnsw_index_manager import HNSWIndexManager; "
            "print(json.dumps({'psycopg': 'psycopg' in sys.modules, "
            "'fastapi': 'fastapi' in sys.modules}))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Subprocess failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        import json as _json

        leaked = _json.loads(result.stdout.strip())
        assert leaked == {"psycopg": False, "fastapi": False}, (
            f"LAZY-LOAD VIOLATION (Bug #1468): importing a code_indexer.storage.* "
            f"submodule leaked: {leaked}"
        )
