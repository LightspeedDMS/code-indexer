"""Tests for Bug #1033: codebase_dir reconciliation on cluster nodes with different mount points."""

import json
import tempfile
from pathlib import Path

from code_indexer.config import ConfigManager


class TestConfigManagerCodebaseDirReconcile:
    """ConfigManager.load() must reconcile stored codebase_dir against config file location."""

    def test_codebase_dir_reconciles_to_actual_path_when_mismatch(self):
        """When stored codebase_dir differs from actual config location, use actual path.

        Cluster scenario: config was saved on node-A with mount /mnt/nfs/project,
        but current node-B mounts the same NFS share at /data/project. The stored
        codebase_dir points to node-A's path; load() must auto-correct to node-B's.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            actual_dir = Path(tmpdir)
            code_indexer_dir = actual_dir / ".code-indexer"
            code_indexer_dir.mkdir()
            config_path = code_indexer_dir / "config.json"

            # Simulate a config saved on a different node with a different mount path
            stored_codebase_dir = "/mnt/nfs/totally/different/node/path"
            config_data = {"codebase_dir": stored_codebase_dir}
            config_path.write_text(json.dumps(config_data))

            manager = ConfigManager(config_path)
            config = manager.load()

            # Must use actual dir, not the stale node-A path
            assert config.codebase_dir == actual_dir.resolve()
            assert str(config.codebase_dir) != stored_codebase_dir

    def test_codebase_dir_unchanged_when_matches_actual_path(self):
        """When stored codebase_dir matches actual config location, no change occurs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            actual_dir = Path(tmpdir).resolve()
            code_indexer_dir = actual_dir / ".code-indexer"
            code_indexer_dir.mkdir()
            config_path = code_indexer_dir / "config.json"

            # Stored path matches where the config actually lives
            config_data = {"codebase_dir": str(actual_dir)}
            config_path.write_text(json.dumps(config_data))

            manager = ConfigManager(config_path)
            config = manager.load()

            assert config.codebase_dir == actual_dir
