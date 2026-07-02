"""
Unit tests for Phase 3-B of Story #877: bootstrap_cidx_meta exclude patching.

After bootstrap_cidx_meta runs, .code-indexer-override.yaml in the cidx-meta
directory MUST contain:
  - ".locks" in add_exclude_dirs  (defense-in-depth; primary lock dir is outside clone)
  - "*.tmp"  in force_exclude_patterns  (atomic write temp files)

These tests use real filesystem (tmp_path) and mock subprocess.run so that
cidx init creates a minimal .code-indexer-override.yaml without requiring
the real cidx CLI to be installed.

Mock justification:
  - subprocess.run for 'cidx init': external CLI subprocess; its real output
    is a side-effect (file creation). We provide that side-effect manually so
    the test controls exactly what config exists at the start of each scenario.
  - golden_repo_manager.register_local_repo: registers metadata in a real DB;
    for these tests we only care about the YAML patch, not registration.

Test inventory (9 test methods across 5 scenario classes):
  TestFreshBootstrapExcludes      — 2 parametrized: .locks and *.tmp added on fresh run
  TestIdempotency                 — 2 parametrized: no duplication on second run
  TestPreservation                — 2 parametrized: pre-existing entries survive
  TestRetrofit                    — 2 parametrized: already-registered gets excludes added
  TestMissingOverrideYaml         — 1: missing YAML does not crash bootstrap
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict
from unittest.mock import MagicMock, patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_OVERRIDE_FILENAME = ".code-indexer-override.yaml"

_MINIMAL_OVERRIDE: Dict[str, list] = {
    "add_extensions": [],
    "remove_extensions": [],
    "add_exclude_dirs": [],
    "add_include_dirs": [],
    "force_include_patterns": [],
    "force_exclude_patterns": [],
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _write_override(cidx_meta_path: Path, data: dict) -> None:
    """Write override YAML dict to cidx-meta directory."""
    (cidx_meta_path / _OVERRIDE_FILENAME).write_text(yaml.safe_dump(data))


def _write_code_indexer_config(cidx_meta_path: Path) -> None:
    """Write a minimal .code-indexer/config.json (cidx init artefact)."""
    code_indexer_dir = cidx_meta_path / ".code-indexer"
    code_indexer_dir.mkdir(parents=True, exist_ok=True)
    (code_indexer_dir / "config.json").write_text(
        '{"codebase_dir": "' + str(cidx_meta_path) + '", "file_extensions": []}'
    )


def _read_override(cidx_meta_path: Path) -> dict:
    """Read and parse the override YAML; return the dict."""
    with open(cidx_meta_path / _OVERRIDE_FILENAME) as f:
        return yaml.safe_load(f) or {}


def _make_manager(already_registered: bool = False) -> MagicMock:
    """Build a minimal mock GoldenRepoManager."""
    mgr = MagicMock()
    mgr.golden_repo_exists.return_value = already_registered
    mgr.register_local_repo.return_value = True
    return mgr


def _fake_cidx_init(cidx_meta_path: Path):
    """Side-effect replacing subprocess.run(['cidx', 'init', ...])."""
    _write_code_indexer_config(cidx_meta_path)
    _write_override(cidx_meta_path, dict(_MINIMAL_OVERRIDE))


# ---------------------------------------------------------------------------
# 1. Fresh bootstrap: excludes are present after first run (2 parametrized)
# ---------------------------------------------------------------------------


class TestFreshBootstrapExcludes:
    @pytest.mark.parametrize(
        "field,expected_value",
        [
            ("add_exclude_dirs", ".locks"),
            ("force_exclude_patterns", "*.tmp"),
        ],
    )
    def test_fresh_bootstrap_adds_exclude_entry(self, tmp_path, field, expected_value):
        """After bootstrap on a fresh dir, each required exclude entry is present."""
        from code_indexer.server.startup.bootstrap import bootstrap_cidx_meta

        cidx_meta_path = tmp_path / "cidx-meta"
        manager = _make_manager(already_registered=False)

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = lambda *a, **kw: _fake_cidx_init(cidx_meta_path)
            bootstrap_cidx_meta(manager, str(tmp_path))

        data = _read_override(cidx_meta_path)
        assert expected_value in data[field]


# ---------------------------------------------------------------------------
# 2. Idempotency: running twice does not duplicate entries (2 parametrized)
# ---------------------------------------------------------------------------


class TestIdempotency:
    @pytest.mark.parametrize(
        "field,expected_value",
        [
            ("add_exclude_dirs", ".locks"),
            ("force_exclude_patterns", "*.tmp"),
        ],
    )
    def test_second_bootstrap_does_not_duplicate_entry(
        self, tmp_path, field, expected_value
    ):
        """Running bootstrap twice yields exactly one copy of each exclude entry."""
        from code_indexer.server.startup.bootstrap import bootstrap_cidx_meta

        cidx_meta_path = tmp_path / "cidx-meta"

        # First run: fresh install
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = lambda *a, **kw: _fake_cidx_init(cidx_meta_path)
            bootstrap_cidx_meta(_make_manager(already_registered=False), str(tmp_path))

        # Second run: already registered
        with patch("subprocess.run"):
            bootstrap_cidx_meta(_make_manager(already_registered=True), str(tmp_path))

        data = _read_override(cidx_meta_path)
        assert data[field].count(expected_value) == 1


# ---------------------------------------------------------------------------
# 3. Preservation: pre-existing entries in those lists survive (2 parametrized)
# ---------------------------------------------------------------------------


class TestPreservation:
    @pytest.mark.parametrize(
        "field,pre_existing,new_entry",
        [
            ("add_exclude_dirs", ["node_modules", "dist"], ".locks"),
            ("force_exclude_patterns", ["*.log", "*.bak"], "*.tmp"),
        ],
    )
    def test_pre_existing_entries_survive(
        self, tmp_path, field, pre_existing, new_entry
    ):
        """Pre-existing entries in each exclude list are preserved after patching."""
        from code_indexer.server.startup.bootstrap import bootstrap_cidx_meta

        cidx_meta_path = tmp_path / "cidx-meta"
        cidx_meta_path.mkdir(parents=True, exist_ok=True)

        existing = dict(_MINIMAL_OVERRIDE)
        existing[field] = list(pre_existing)
        _write_code_indexer_config(cidx_meta_path)
        _write_override(cidx_meta_path, existing)

        with patch("subprocess.run"):
            bootstrap_cidx_meta(_make_manager(already_registered=True), str(tmp_path))

        data = _read_override(cidx_meta_path)
        for entry in pre_existing:
            assert entry in data[field]
        assert new_entry in data[field]


# ---------------------------------------------------------------------------
# 4. Retrofit (upgrade path): already-registered without excludes (2 parametrized)
# ---------------------------------------------------------------------------


class TestRetrofit:
    @pytest.mark.parametrize(
        "field,expected_value",
        [
            ("add_exclude_dirs", ".locks"),
            ("force_exclude_patterns", "*.tmp"),
        ],
    )
    def test_already_registered_gets_exclude_entry(
        self, tmp_path, field, expected_value
    ):
        """Already-registered cidx-meta without the excludes gets them added."""
        from code_indexer.server.startup.bootstrap import bootstrap_cidx_meta

        cidx_meta_path = tmp_path / "cidx-meta"
        cidx_meta_path.mkdir(parents=True, exist_ok=True)

        # Upgrade scenario: override exists but lacks the new entries
        _write_code_indexer_config(cidx_meta_path)
        _write_override(cidx_meta_path, dict(_MINIMAL_OVERRIDE))

        with patch("subprocess.run"):
            bootstrap_cidx_meta(_make_manager(already_registered=True), str(tmp_path))

        data = _read_override(cidx_meta_path)
        assert expected_value in data[field]


# ---------------------------------------------------------------------------
# 5. Missing override YAML → bootstrap skips patching gracefully (1 test)
# ---------------------------------------------------------------------------


class TestMissingOverrideYaml:
    def test_missing_override_yaml_does_not_crash(self, tmp_path):
        """When override YAML is absent (e.g. cidx init failed), bootstrap does not raise."""
        from code_indexer.server.startup.bootstrap import bootstrap_cidx_meta

        cidx_meta_path = tmp_path / "cidx-meta"
        cidx_meta_path.mkdir(parents=True, exist_ok=True)

        # .code-indexer/config.json exists but NO override YAML
        _write_code_indexer_config(cidx_meta_path)

        with patch("subprocess.run"):
            # Must not raise
            bootstrap_cidx_meta(_make_manager(already_registered=True), str(tmp_path))
