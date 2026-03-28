"""
Tests for DatabaseConnectionManager singleton pattern (Bug #378).

Problem: 12 separate DatabaseConnectionManager instances all point to the same
cidx_server.db, each maintaining its own thread-local connection pool. This
creates up to 12N connections (N = thread count), causing FD exhaustion.

Fix: Singleton pattern via get_instance(db_path) classmethod ensures all
backends sharing the same db_path reuse the same ConnectionManager instance.
"""

import inspect
import os
import re
import tempfile


from code_indexer.server.storage.database_manager import DatabaseConnectionManager


class TestSingletonConnectionManager:
    """Tests for DatabaseConnectionManager singleton pattern (Bug #378)."""

    def setup_method(self):
        """Clear singleton cache between tests to ensure isolation."""
        DatabaseConnectionManager._instances.clear()

    def test_get_instance_returns_same_object_for_same_path(self):
        """Two get_instance calls with same path should return the exact same object."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            mgr1 = DatabaseConnectionManager.get_instance(db_path)
            mgr2 = DatabaseConnectionManager.get_instance(db_path)
            assert mgr1 is mgr2, (
                "Same db_path must return the same instance (singleton)"
            )
        finally:
            os.unlink(db_path)

    def test_get_instance_returns_different_objects_for_different_paths(self):
        """Different db paths should produce independent manager instances."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f1:
            path1 = f1.name
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f2:
            path2 = f2.name
        try:
            mgr1 = DatabaseConnectionManager.get_instance(path1)
            mgr2 = DatabaseConnectionManager.get_instance(path2)
            assert mgr1 is not mgr2, (
                "Different db_paths must return different instances"
            )
        finally:
            os.unlink(path1)
            os.unlink(path2)

    def test_get_instance_resolves_relative_paths_to_same_instance(self):
        """Relative and absolute paths pointing to the same file share one instance."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False, dir=".") as f:
            rel_path = f.name
        abs_path = os.path.abspath(rel_path)
        try:
            mgr1 = DatabaseConnectionManager.get_instance(rel_path)
            mgr2 = DatabaseConnectionManager.get_instance(abs_path)
            assert mgr1 is mgr2, (
                "Relative and absolute paths to the same file must return the same instance"
            )
        finally:
            os.unlink(rel_path)

    def test_get_instance_is_thread_safe(self):
        """Concurrent calls to get_instance with the same path return the same object."""
        import threading

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        results = []
        errors = []

        def get_mgr():
            try:
                mgr = DatabaseConnectionManager.get_instance(db_path)
                results.append(id(mgr))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=get_mgr) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        try:
            assert not errors, f"Thread errors: {errors}"
            assert len(set(results)) == 1, (
                "All concurrent threads must get the same instance id"
            )
        finally:
            os.unlink(db_path)

    def test_direct_constructor_still_works(self):
        """Direct __init__ must still work for backward compatibility with existing tests."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            # Direct construction must not raise
            mgr = DatabaseConnectionManager(db_path)
            assert mgr is not None
            assert mgr.db_path == db_path
        finally:
            os.unlink(db_path)

    def test_instances_dict_is_class_level(self):
        """_instances must be a class-level dict, not instance-level."""
        assert hasattr(DatabaseConnectionManager, "_instances"), (
            "DatabaseConnectionManager must have a class-level _instances dict"
        )
        assert isinstance(DatabaseConnectionManager._instances, dict)

    def test_instance_lock_is_class_level(self):
        """_instance_lock must be a class-level threading.Lock."""
        import threading

        assert hasattr(DatabaseConnectionManager, "_instance_lock"), (
            "DatabaseConnectionManager must have a class-level _instance_lock"
        )
        assert isinstance(
            DatabaseConnectionManager._instance_lock, type(threading.Lock())
        )

    def test_all_sqlite_backends_use_get_instance(self):
        """
        All DatabaseConnectionManager instantiations in sqlite_backends.py
        must use .get_instance(), not direct construction.

        This verifies the fix for Bug #378 is applied to all 10 sites.
        """
        from code_indexer.server.storage import sqlite_backends

        source = inspect.getsource(sqlite_backends)

        # Find lines that instantiate DatabaseConnectionManager
        lines = source.split("\n")
        direct_constructor_lines = [
            (i + 1, line)
            for i, line in enumerate(lines)
            if re.search(r"DatabaseConnectionManager\(", line)
            and "get_instance" not in line
            and "def get_instance" not in line
        ]

        assert direct_constructor_lines == [], (
            f"Found direct DatabaseConnectionManager() construction (not .get_instance()) "
            f"in sqlite_backends.py at lines: "
            f"{[(lineno, line.strip()) for lineno, line in direct_constructor_lines]}"
        )

    def test_job_tracker_uses_get_instance(self):
        """job_tracker.py must use get_instance, not direct construction."""
        from code_indexer.server.services import job_tracker

        source = inspect.getsource(job_tracker)
        lines = source.split("\n")
        direct_constructor_lines = [
            (i + 1, line)
            for i, line in enumerate(lines)
            if re.search(r"DatabaseConnectionManager\(", line)
            and "get_instance" not in line
            and "def get_instance" not in line
        ]

        assert direct_constructor_lines == [], (
            f"Found direct DatabaseConnectionManager() construction in job_tracker.py at lines: "
            f"{[(lineno, line.strip()) for lineno, line in direct_constructor_lines]}"
        )

    def test_repo_category_backend_uses_get_instance(self):
        """repo_category_backend.py must use get_instance, not direct construction."""
        from code_indexer.server.storage import repo_category_backend

        source = inspect.getsource(repo_category_backend)
        lines = source.split("\n")
        direct_constructor_lines = [
            (i + 1, line)
            for i, line in enumerate(lines)
            if re.search(r"DatabaseConnectionManager\(", line)
            and "get_instance" not in line
            and "def get_instance" not in line
        ]

        assert direct_constructor_lines == [], (
            f"Found direct DatabaseConnectionManager() construction in "
            f"repo_category_backend.py at lines: "
            f"{[(lineno, line.strip()) for lineno, line in direct_constructor_lines]}"
        )

    def test_get_instance_stores_instance_in_class_dict(self):
        """After get_instance(), the instance must be stored in _instances keyed by abs path."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        abs_path = os.path.abspath(db_path)
        try:
            mgr = DatabaseConnectionManager.get_instance(db_path)
            assert abs_path in DatabaseConnectionManager._instances, (
                "get_instance must store the instance in _instances keyed by absolute path"
            )
            assert DatabaseConnectionManager._instances[abs_path] is mgr
        finally:
            os.unlink(db_path)
