"""Bug #1690: `ConfigManager.create_with_backtrack().get_config()` is blind-
trusted at ~10 call sites across the server, mirroring the exact root-cause
pattern Bug #1683 round 4 fixed for `AutoWatchManager.start_watch`.

`create_with_backtrack()` walks UP the directory tree from a target
directory looking for `.code-indexer/config.json`, and silently falls back
to a bare `Config()` (codebase_dir=".") when nothing is found anywhere. Both
outcomes ("found only an ANCESTOR's config" and "found nothing, defaulted")
leave the returned `Config.codebase_dir` pointing somewhere OTHER than the
caller's intended target directory. A caller that then uses that Config to
resolve settings for "this" directory (embedding provider, vector_store
provider, exclusions, etc.) silently applies an unrelated repo's
configuration instead of failing loud.

`ConfigManager.load_verified_config(target_dir)` is the shared, reusable
fix: load via `create_with_backtrack(target_dir)` as before, but verify the
resolved `config.codebase_dir` is EXACTLY `target_dir` (strict equality --
never "equal-or-ancestor", per Bug #1683 round 4's own lesson that the
ancestor-permitting direction is the unsafe one) before returning it.
Raises `ConfigVerificationError` (a `ValueError` subclass, so it integrates
with existing "orphaned repo -> skip gracefully" `except ValueError`
handling already present at call sites like search_service.py) otherwise.

NOTE on test isolation: this dev machine has real `.code-indexer/config.json`
files at several ancestors of `/tmp` (e.g. `/tmp/.code-indexer`,
`/home/*/.code-indexer`) from unrelated real usage. Mirrors the isolation
approach `test_auto_watch_manager_root_cause_1683.py` established: an
`isolated_tmp_root` fixture rooted under `~/.tmp` (never bare `/tmp`) for the
"ancestor config found" scenario, and the real `CODEBASE_DIR` env-var
override (which `create_with_backtrack()` already supports in production,
bypassing backtracking entirely) for the "no config found anywhere"
scenario.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from code_indexer.config import ConfigManager, ConfigVerificationError


@pytest.fixture
def isolated_tmp_root():
    """A temp directory tree rooted under `~/.tmp` (never `/tmp` -- project
    convention), immune to any real `.code-indexer/config.json` that may
    exist as an ancestor of the system `/tmp`."""
    base = Path.home() / ".tmp"
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=str(base), prefix="test_1690_"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


class TestLoadVerifiedConfigRefusesAncestorOnlyConfig:
    """Bug #1690's discriminating scenario: target_dir has NO
    `.code-indexer/config.json` of its own, but a real ANCESTOR directory
    does. `create_with_backtrack` walks up and silently returns the
    ancestor's config -- `load_verified_config` must refuse it."""

    def test_raises_config_verification_error(self, isolated_tmp_root) -> None:
        ancestor_dir = isolated_tmp_root / "server-data"
        ancestor_dir.mkdir()
        ancestor_config_manager = ConfigManager(
            ancestor_dir / ".code-indexer" / "config.json"
        )
        ancestor_config_manager.create_default_config(codebase_dir=ancestor_dir)

        # Nested UNDER the ancestor but has no config of its own --
        # find_config_path will walk up and find the ancestor's.
        target_dir = ancestor_dir / "activated-repos" / "user1" / "myrepo"
        target_dir.mkdir(parents=True)

        with pytest.raises(ConfigVerificationError):
            ConfigManager.load_verified_config(target_dir)

    def test_is_a_value_error_for_graceful_orphan_handling(
        self, isolated_tmp_root
    ) -> None:
        """ConfigVerificationError must subclass ValueError so it is caught
        by existing `except ValueError` orphaned-repo handling (e.g.
        search_service.py's "Skipping repo: no valid index configured")."""
        ancestor_dir = isolated_tmp_root / "server-data"
        ancestor_dir.mkdir()
        ConfigManager(
            ancestor_dir / ".code-indexer" / "config.json"
        ).create_default_config(codebase_dir=ancestor_dir)
        target_dir = ancestor_dir / "myrepo"
        target_dir.mkdir()

        with pytest.raises(ValueError):
            ConfigManager.load_verified_config(target_dir)


class TestLoadVerifiedConfigRefusesNoConfigFoundAnywhere:
    """No `.code-indexer/config.json` for target_dir or any parent --
    must fail loud, never silently default to a bare Config()."""

    def test_raises_config_verification_error(
        self, isolated_tmp_root, monkeypatch
    ) -> None:
        target_dir = isolated_tmp_root / "never-configured-repo"
        target_dir.mkdir()
        # Force create_with_backtrack's CODEBASE_DIR override path so the
        # real ancestor .code-indexer dirs on this dev machine cannot be
        # accidentally found by directory-walk backtracking.
        monkeypatch.setenv("CODEBASE_DIR", str(target_dir))

        with pytest.raises(ConfigVerificationError):
            ConfigManager.load_verified_config(target_dir)

    def test_raises_when_cwd_equals_target_with_no_config_anywhere(
        self, isolated_tmp_root, monkeypatch
    ) -> None:
        """Code-review finding on #1690: the test above only passes
        INCIDENTALLY, because the pytest process's real CWD (this repo's
        root) happens to differ from target_dir. When the bare-Config()
        fallback fires, its default codebase_dir='.' is a RELATIVE path --
        resolving it produces Path.cwd(), not target_dir. If the caller's
        CWD happens to equal target_dir (e.g. a server process whose CWD IS
        the repo it is querying), Path('.').resolve() == resolved_target
        by pure coincidental path aliasing, spuriously satisfying the
        strict-equality check even though NO real config was ever found.
        This is the exact Bug #1691 failure shape (a stray
        .code-indexer/index materializing relative to CWD). Discriminating
        regression test: chdir into target_dir so this coincidence is
        forced, proving load_verified_config must reject the defaulted
        fallback on its own terms (config_path.exists()), not rely on the
        equality check alone.
        """
        target_dir = isolated_tmp_root / "never-configured-repo-cwd"
        target_dir.mkdir()
        monkeypatch.setenv("CODEBASE_DIR", str(target_dir))
        monkeypatch.chdir(target_dir)

        with pytest.raises(ConfigVerificationError):
            ConfigManager.load_verified_config(target_dir)


class TestLoadVerifiedConfigSucceedsWhenConfigGenuinelyMatchesTarget:
    """Positive control: target_dir has its own real
    `.code-indexer/config.json` -- must succeed and return that Config."""

    def test_returns_config_for_genuine_own_config(self, isolated_tmp_root) -> None:
        target_dir = isolated_tmp_root / "real-repo"
        target_dir.mkdir()
        ConfigManager(
            target_dir / ".code-indexer" / "config.json"
        ).create_default_config(codebase_dir=target_dir)

        config = ConfigManager.load_verified_config(target_dir)

        assert Path(config.codebase_dir).resolve() == target_dir.resolve()
