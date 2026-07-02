"""
Bug #1285 — Production repo activations failing with "Clone operation timed out".

Root cause: ActivatedRepoManager._clone_with_copy_on_write() hardcoded
``timeout=120`` on the call to ``clone_backend.create_clone_at_path()``. Large
golden repos (e.g. a populated ``.code-indexer/`` index with hundreds of
thousands of files) legitimately take longer than 120s to CoW-clone via
``cp --reflink=auto``, so the subprocess is SIGKILLed at 120s and the caught
``subprocess.TimeoutExpired`` is re-raised as
``ActivatedRepoError("Clone operation timed out: ...")`` even though the clone
was healthy and simply needed more time.

The fix drives the deadline from ``resource_config.cow_clone_timeout`` (the
same knob ``GoldenRepoManager`` already uses for its own CoW operations,
default 3600s / 1 hour — bumped from an initial 600s once measured
extrapolation showed large production repos like evolution (~1M files,
~17-19 min) and phoenix (40GB) need more headroom) instead of a fixed low
literal, mirroring the Bug #1218 "no artificial clock on a legitimately
long-running clone/index path" principle.

These tests use a scaled-down proxy for the real 120s/3600s pair (real
wall-clock seconds are used, just small ones) so the suite stays fast:
a fake ``cp`` shell script that sleeps for a controlled duration stands in
for a slow reflink copy of a huge ``.code-indexer/`` tree.
"""

from __future__ import annotations

import ast
import inspect
import os
import stat
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
    ActivatedRepoError,
)
from code_indexer.server.repositories.golden_repo_manager import GoldenRepo
from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend
from code_indexer.server.utils.config_manager import ServerResourceConfig


def _source_text(func) -> str:
    return textwrap.dedent(inspect.getsource(func))


def _write_fake_slow_cp(bin_dir: Path, sleep_seconds: float) -> None:
    """Write a fake `cp` executable that sleeps then delegates to real cp.

    Standing in for a legitimately slow ``cp --reflink=auto -r`` of a large
    ``.code-indexer/`` tree, without needing to build a real multi-GB tree
    in a unit test.
    """
    script = bin_dir / "cp"
    script.write_text(f'#!/bin/sh\nsleep {sleep_seconds}\nexec /bin/cp "$@"\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _write_fake_cp_that_completes_then_hangs(
    bin_dir: Path, hang_seconds: float
) -> None:
    """Write a fake `cp` that performs the REAL copy first, then hangs.

    Standing in for the production leak scenario (Bug #1285 follow-up): the
    reflink copy has already created ``dest_path`` and written data to it,
    but the subprocess is still running (e.g. finishing a huge tree) when
    the parent's ``subprocess.run(timeout=...)`` deadline fires and SIGKILLs
    it. By the time the kill happens, ``dest_path`` already exists on disk
    with real content — exactly the ~15-21GB partial clone left behind in
    production.
    """
    script = bin_dir / "cp"
    script.write_text(f'#!/bin/sh\n/bin/cp "$@"\nsleep {hang_seconds}\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def golden_repo_manager_mock():
    """Mock golden repo manager exposing a REAL ServerResourceConfig.

    Using the real dataclass (not a bare MagicMock) for resource_config is
    load-bearing: the fix reads ``resource_config.cow_clone_timeout`` as a
    real int, and a bare MagicMock would auto-vivify a Mock object there
    instead of raising — masking wiring bugs.
    """
    mock = MagicMock()
    golden_repo = GoldenRepo(
        alias="test-repo",
        repo_url="https://github.com/example/test-repo.git",
        default_branch="main",
        clone_path="/path/to/golden/test-repo",
        created_at="2024-01-01T00:00:00Z",
    )
    mock.golden_repos = {"test-repo": golden_repo}
    mock.resource_config = ServerResourceConfig()
    return mock


@pytest.fixture
def background_job_manager_mock():
    mock = MagicMock()
    mock.submit_job.return_value = "job-123"
    return mock


class TestOldHardcodedTimeoutWouldKillLongClone:
    """(a) Behavioral: prove the OLD hardcoded-low-timeout mechanism kills a
    legitimately slow clone, using a real temp source tree and a real
    subprocess (LocalCloneBackend), not a mock."""

    def test_low_fixed_timeout_kills_slow_clone(self, tmp_path, monkeypatch):
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("hello world\n")
        dest = tmp_path / "dest"

        bin_dir = tmp_path / "fakebin"
        bin_dir.mkdir()
        # Fake `cp` sleeps 3s — stands in for a clone of a huge
        # .code-indexer/ tree that legitimately takes minutes.
        _write_fake_slow_cp(bin_dir, sleep_seconds=3)
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

        backend = LocalCloneBackend()

        # OLD behavior: hardcoded low timeout (proxy for the real 120s)
        # kills a clone that legitimately takes longer.
        with pytest.raises(subprocess.TimeoutExpired):
            backend.create_clone_at_path(
                str(source), str(dest), preserve_attrs=False, timeout=1
            )


class TestNewConfigDrivenTimeoutCompletes:
    """(a) Behavioral: with the NEW config-driven timeout (proxy for the real
    3600s default), an equivalently slow clone completes successfully through
    ActivatedRepoManager._clone_with_copy_on_write — the exact call site that
    used to hardcode 120."""

    def test_activated_repo_manager_completes_slow_clone_with_configured_timeout(
        self,
        tmp_path,
        monkeypatch,
        golden_repo_manager_mock,
        background_job_manager_mock,
    ):
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("hello world\n")
        dest = tmp_path / "dest"

        bin_dir = tmp_path / "fakebin"
        bin_dir.mkdir()
        # Same 3s slow clone that killed the OLD hardcoded-timeout path above.
        _write_fake_slow_cp(bin_dir, sleep_seconds=3)
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

        # Configured cow_clone_timeout (proxy for real 3600s) comfortably
        # exceeds the clone's 3s runtime.
        golden_repo_manager_mock.resource_config = ServerResourceConfig(
            cow_clone_timeout=10
        )

        manager = ActivatedRepoManager(
            data_dir=str(tmp_path / "data"),
            golden_repo_manager=golden_repo_manager_mock,
            background_job_manager=background_job_manager_mock,
            clone_backend=LocalCloneBackend(),
        )

        result = manager._clone_with_copy_on_write(str(source), str(dest))

        assert result is True
        assert dest.exists()
        assert (dest / "file.txt").read_text() == "hello world\n"

    def test_activated_repo_manager_still_raises_on_genuine_timeout(
        self,
        tmp_path,
        monkeypatch,
        golden_repo_manager_mock,
        background_job_manager_mock,
    ):
        """A configured timeout that is STILL exceeded must raise
        ActivatedRepoError('Clone operation timed out...') — the fix relaxes
        the fixed low deadline, it does not remove fail-loud behavior."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("hello world\n")
        dest = tmp_path / "dest"

        bin_dir = tmp_path / "fakebin"
        bin_dir.mkdir()
        _write_fake_slow_cp(bin_dir, sleep_seconds=3)
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

        # Configured timeout shorter than the clone's runtime.
        golden_repo_manager_mock.resource_config = ServerResourceConfig(
            cow_clone_timeout=1
        )

        manager = ActivatedRepoManager(
            data_dir=str(tmp_path / "data"),
            golden_repo_manager=golden_repo_manager_mock,
            background_job_manager=background_job_manager_mock,
            clone_backend=LocalCloneBackend(),
        )

        with pytest.raises(ActivatedRepoError, match="Clone operation timed out"):
            manager._clone_with_copy_on_write(str(source), str(dest))


class TestTimeoutedCloneCleansUpPartialDest:
    """Bug #1285 follow-up: a timed-out CoW clone must not leak the partial
    ``dest_path`` directory. The generic ``except Exception`` branch already
    removes it; the ``except subprocess.TimeoutExpired`` branch re-raised
    without cleanup, leaving a full partial reflink copy (measured 15-21GB
    in production, ~99GB total across orphaned activations) behind forever
    — since the clone never completed, no metadata row exists, so the
    activated-repo reaper never finds it."""

    def test_dest_path_removed_after_timeout_expired(
        self,
        tmp_path,
        monkeypatch,
        golden_repo_manager_mock,
        background_job_manager_mock,
    ):
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("hello world\n")
        dest = tmp_path / "dest"

        bin_dir = tmp_path / "fakebin"
        bin_dir.mkdir()
        # Real cp completes (dest is created + populated), then the fake
        # cp process hangs 3s — the parent's short timeout below fires
        # during the hang, SIGKILLing a process whose copy already landed
        # on disk. This reproduces the production leak shape exactly.
        _write_fake_cp_that_completes_then_hangs(bin_dir, hang_seconds=3)
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

        golden_repo_manager_mock.resource_config = ServerResourceConfig(
            cow_clone_timeout=1
        )

        manager = ActivatedRepoManager(
            data_dir=str(tmp_path / "data"),
            golden_repo_manager=golden_repo_manager_mock,
            background_job_manager=background_job_manager_mock,
            clone_backend=LocalCloneBackend(),
        )

        with pytest.raises(ActivatedRepoError, match="Clone operation timed out"):
            manager._clone_with_copy_on_write(str(source), str(dest))

        assert not dest.exists(), (
            "Bug #1285 follow-up: dest_path still exists on disk after a "
            "timed-out CoW clone. The subprocess.TimeoutExpired branch must "
            "remove the partial clone before re-raising, mirroring the "
            "generic Exception branch, or production leaks tens of GB per "
            "timed-out activation forever (no metadata row -> reaper blind)."
        )

    def test_timeout_expired_branch_contains_cleanup(self):
        """Source guard: the except subprocess.TimeoutExpired branch must
        contain an rmtree cleanup call before re-raising ActivatedRepoError."""
        src = _source_text(ActivatedRepoManager._clone_with_copy_on_write)
        start_marker = "except subprocess.TimeoutExpired:"
        end_marker = "except Exception as e:"
        assert start_marker in src, (
            "Could not find 'except subprocess.TimeoutExpired:' in "
            "_clone_with_copy_on_write to inspect."
        )
        start = src.index(start_marker)
        end = src.index(end_marker, start)
        branch = src[start:end]
        assert "rmtree" in branch, (
            "The 'except subprocess.TimeoutExpired:' branch in "
            "_clone_with_copy_on_write does not clean up dest_path. Bug "
            "#1285 follow-up requires removing the partial clone (rmtree) "
            "before re-raising ActivatedRepoError, mirroring the generic "
            "Exception branch immediately below it."
        )


class TestSourceWiringGuard:
    """(b) Source/wiring guard mirroring the Bug #1218 style: assert
    _clone_with_copy_on_write no longer passes a literal 120 and instead
    derives the timeout from cow_clone_timeout."""

    def test_no_literal_120_timeout_passed_to_create_clone_at_path(self):
        src = _source_text(ActivatedRepoManager._clone_with_copy_on_write)
        tree = ast.parse(src)
        found_call = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "create_clone_at_path"
                ):
                    found_call = True
                    for kw in node.keywords:
                        if kw.arg == "timeout":
                            is_literal_120 = (
                                isinstance(kw.value, ast.Constant)
                                and kw.value.value == 120
                            )
                            assert not is_literal_120, (
                                "_clone_with_copy_on_write still passes a literal "
                                "timeout=120 to create_clone_at_path. Bug #1285 "
                                "requires driving this from resource_config."
                            )
        assert found_call, (
            "Could not find a create_clone_at_path(...) call inside "
            "_clone_with_copy_on_write to inspect."
        )

    def test_cow_clone_timeout_referenced_in_clone_with_copy_on_write(self):
        """The call site must actually read cow_clone_timeout — removing the
        hardcode without wiring the config value would be an incomplete fix."""
        src = _source_text(ActivatedRepoManager._clone_with_copy_on_write)
        assert "cow_clone_timeout" in src, (
            "_clone_with_copy_on_write no longer references cow_clone_timeout. "
            "Bug #1285 requires driving the CoW clone deadline from "
            "resource_config.cow_clone_timeout (mirrors GoldenRepoManager)."
        )


class TestNormalSmallRepoActivationRegression:
    """(c) Regression: a normal, fast small-repo clone still succeeds
    end-to-end through _clone_with_copy_on_write with the real
    LocalCloneBackend (no fake slow cp)."""

    def test_small_repo_clone_still_succeeds(
        self, tmp_path, golden_repo_manager_mock, background_job_manager_mock
    ):
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("small repo content\n")
        dest = tmp_path / "dest"

        manager = ActivatedRepoManager(
            data_dir=str(tmp_path / "data"),
            golden_repo_manager=golden_repo_manager_mock,
            background_job_manager=background_job_manager_mock,
            clone_backend=LocalCloneBackend(),
        )

        result = manager._clone_with_copy_on_write(str(source), str(dest))

        assert result is True
        assert dest.exists()
        assert (dest / "file.txt").read_text() == "small repo content\n"
