"""Every server start leaves the catalog's private directories (memories and
the index tree) without other-class permission bits."""

import logging
import os
import stat
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from code_indexer.server.storage.shared import catalog_tree_permissions

from code_indexer.server.services.memory_store_service_factory import (
    build_memory_store_service,
)
from code_indexer.server.startup.bootstrap import bootstrap_cidx_meta


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _registered_catalog() -> MagicMock:
    registry = MagicMock()
    registry.golden_repo_exists.return_value = True
    return registry


def test_bootstrap_closes_catalog_private_dirs(tmp_path: Path) -> None:
    catalog = tmp_path / "cidx-meta"
    for name in ("memories", ".code-indexer"):
        (catalog / name).mkdir(parents=True)
        os.chmod(catalog / name, 0o755)
    os.chmod(catalog, 0o755)

    bootstrap_cidx_meta(_registered_catalog(), str(tmp_path))

    assert _mode(catalog / "memories") == 0o750
    assert _mode(catalog / ".code-indexer") == 0o750
    assert _mode(catalog) == 0o755


def test_memory_store_creates_memories_dir_without_other_bits(tmp_path: Path) -> None:
    golden_repos_dir = tmp_path / "golden-repos"
    (golden_repos_dir / "cidx-meta").mkdir(parents=True)

    bundle = build_memory_store_service(
        golden_repos_dir=golden_repos_dir,
        server_data_dir=tmp_path / "data",
        refresh_scheduler=MagicMock(),
        refresh_debouncer=MagicMock(),
    )

    assert _mode(bundle.memories_dir) & stat.S_IRWXO == 0


def test_close_failure_is_logged_at_error_without_failing_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    catalog = tmp_path / "cidx-meta"
    (catalog / "memories").mkdir(parents=True)

    def refuse(fd: Any, mode: int) -> None:
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(catalog_tree_permissions.os, "fchmod", refuse)

    with caplog.at_level(logging.ERROR):
        bootstrap_cidx_meta(_registered_catalog(), str(tmp_path))

    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any(
        "PermissionError" in m and str(catalog / "memories") in m for m in errors
    ), errors


def _symlinked_root_errors(caplog: pytest.LogCaptureFixture) -> list:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "ERROR" and "symbolic link" in r.getMessage()
    ]


def test_symlinked_private_root_is_reported_and_not_followed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    catalog = tmp_path / "cidx-meta"
    (catalog / ".code-indexer").mkdir(parents=True)
    os.chmod(catalog / ".code-indexer", 0o755)
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    os.chmod(outside, 0o755)
    os.symlink(outside, catalog / "memories")

    with caplog.at_level(logging.ERROR):
        bootstrap_cidx_meta(_registered_catalog(), str(tmp_path))

    assert _mode(outside) == 0o755
    assert _mode(catalog / ".code-indexer") == 0o750
    errors = _symlinked_root_errors(caplog)
    assert len(errors) == 1, errors
    assert "'memories'" in errors[0] and "not managed" in errors[0]


def test_private_root_swapped_for_symlink_after_check_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = tmp_path / "cidx-meta"
    memories = catalog / "memories"
    memories.mkdir(parents=True)
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    os.chmod(outside, 0o755)
    real_lstat = Path.lstat
    swapped: list = []

    def lstat_then_swap(self: Path) -> os.stat_result:
        result = real_lstat(self)
        if self == memories and not swapped:
            swapped.append(True)
            os.rename(memories, catalog / "moved-away")
            os.symlink(outside, memories)
        return result

    # ^ invariant guards against a return of lstat-then-pathname-chmod; the hook fires only under that pattern
    monkeypatch.setattr(Path, "lstat", lstat_then_swap)

    catalog_tree_permissions.close_catalog_private_dirs(catalog)

    assert _mode(outside) == 0o755


def test_private_dirs_of_another_owner_are_left_and_reported_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    catalog = tmp_path / "cidx-meta"
    for name in ("memories", ".code-indexer"):
        (catalog / name).mkdir(parents=True)
        os.chmod(catalog / name, 0o755)
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)

    with caplog.at_level(logging.WARNING):
        bootstrap_cidx_meta(_registered_catalog(), str(tmp_path))

    assert _mode(catalog / "memories") == 0o755
    assert _mode(catalog / ".code-indexer") == 0o755
    assert [r for r in caplog.records if r.levelname == "ERROR"] == []
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "WARNING"
        and "not owned by the service user" in r.getMessage()
    ]
    assert len(warnings) == 1, warnings
    assert "memories" in warnings[0] and ".code-indexer" in warnings[0]


def test_bootstrap_without_private_dirs_creates_nothing(tmp_path: Path) -> None:
    catalog = tmp_path / "cidx-meta"
    catalog.mkdir()

    bootstrap_cidx_meta(_registered_catalog(), str(tmp_path))

    assert sorted(p.name for p in catalog.iterdir()) == []
