"""Story #1082 Scenario 3: codebase_dir mismatch WARNING de-spam.

The per-load codebase_dir reconciliation is LOAD-BEARING for Bug #1033 (NFS
multi-mount: different nodes mount the same share at different paths). It MUST
still run on every load and resolve the correct per-node local path. What must
change is the per-QUERY WARNING spam: the mismatch WARNING is emitted at most
once per distinct config path (log-once), NOT on every load.

No mocks: a real config file on disk drives reconciliation; a logging capture
counts WARNING emissions.
"""

import json
import logging
import tempfile
from pathlib import Path

import pytest

from code_indexer import config as config_module
from code_indexer.config import ConfigManager


@pytest.fixture(autouse=True)
def _reset_warn_memo():
    config_module._reset_codebase_dir_warn_memo_for_tests()
    yield
    config_module._reset_codebase_dir_warn_memo_for_tests()


def _make_mismatched_config(tmpdir: str) -> Path:
    actual_dir = Path(tmpdir)
    code_indexer_dir = actual_dir / ".code-indexer"
    code_indexer_dir.mkdir()
    config_path = code_indexer_dir / "config.json"
    config_path.write_text(
        json.dumps({"codebase_dir": "/mnt/nfs/some/other/node/path"})
    )
    return config_path


def test_reconciliation_still_runs_on_every_load(caplog):
    """Bug #1033: every load resolves to the actual per-node path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = _make_mismatched_config(tmpdir)
        actual_dir = Path(tmpdir).resolve()

        for _ in range(5):
            cfg = ConfigManager(config_path).load()
            assert cfg.codebase_dir == actual_dir  # reconciled every time


def test_mismatch_warning_logged_once_per_config_path(caplog):
    """The mismatch WARNING is emitted at most once per distinct config path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = _make_mismatched_config(tmpdir)

        with caplog.at_level(logging.WARNING, logger=config_module.logger.name):
            for _ in range(10):
                ConfigManager(config_path).load()

        warnings = [
            r for r in caplog.records if "codebase_dir mismatch" in r.getMessage()
        ]
        assert len(warnings) == 1  # de-spam: not 10


def test_distinct_config_paths_each_warn_once(caplog):
    with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
        cp1 = _make_mismatched_config(t1)
        cp2 = _make_mismatched_config(t2)

        with caplog.at_level(logging.WARNING, logger=config_module.logger.name):
            ConfigManager(cp1).load()
            ConfigManager(cp1).load()
            ConfigManager(cp2).load()
            ConfigManager(cp2).load()

        warnings = [
            r for r in caplog.records if "codebase_dir mismatch" in r.getMessage()
        ]
        assert len(warnings) == 2  # one per distinct config path
