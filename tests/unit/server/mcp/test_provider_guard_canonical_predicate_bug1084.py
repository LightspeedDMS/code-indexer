"""Provider-index immutability guard via the canonical predicate (Bug #1084 B1).

The WRITE-path corruption vector: on the cow-daemon backend the snapshot path is
``{mount}/.versioned/{ns}/v_<ts>`` (canonical, Phase A) but the OLD detection
``".versioned" in parts`` plus the local-only base-clone arithmetic meant the
guard could be defeated and provider add/reindex could WRITE into an immutable
snapshot. Phase B routes all three repos.py guard/resolver sites through the
canonical predicate ``snapshot_paths.is_versioned_snapshot``.

These tests assert, across path SHAPES (canonical cow, local canonical, base
clone, dotted-alias staging shape, None):

- ``_load_repo_config`` REFUSES (returns None) for any versioned snapshot path
  (the immutability guard, AC #8) and never reads its config.json.
- ``_resolve_provider_job_repo_path`` / ``_resolve_versioned_to_base_clone``
  flag versioned paths and NEVER return the snapshot path itself as a writable
  indexing target (they redirect to a base clone or refuse with an error).
- A mutable base clone passes through untouched on every backend.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_indexer.server.mcp.handlers.repos import (
    _load_repo_config,
    _resolve_provider_job_repo_path,
    _resolve_versioned_to_base_clone,
)


# ---------------------------------------------------------------------------
# _load_repo_config — the immutability GUARD (highest priority)
# ---------------------------------------------------------------------------


def _write_config(repo_dir: Path) -> None:
    cidx = repo_dir / ".code-indexer"
    cidx.mkdir(parents=True, exist_ok=True)
    (cidx / "config.json").write_text(json.dumps({"embedding_provider": "voyage-ai"}))


class TestLoadRepoConfigImmutabilityGuard:
    def test_refuses_canonical_cow_snapshot_path(self, tmp_path):
        """Canonical cow shape {mount}/.versioned/{ns}/v_* must be REFUSED."""
        mount = tmp_path / "mnt" / "cow-storage"
        snapshot = mount / ".versioned" / "langfuse_repo" / "v_1772136021"
        _write_config(snapshot)  # config exists, but guard must still refuse

        assert _load_repo_config(str(snapshot)) is None

    def test_refuses_local_versioned_snapshot_path(self, tmp_path):
        """Local shape {golden}/.versioned/{ns}/v_* must be REFUSED."""
        golden = tmp_path / "golden-repos"
        snapshot = golden / ".versioned" / "flask" / "v_1700000000"
        _write_config(snapshot)

        assert _load_repo_config(str(snapshot)) is None

    def test_refuses_dotted_alias_staging_snapshot(self, tmp_path):
        """Staging-shaped dotted namespace under .versioned must be REFUSED."""
        mount = tmp_path / "mnt" / "cow-storage"
        snapshot = (
            mount
            / ".versioned"
            / "langfuse_Claude_Code_seba_battig_lightspeeddms_com"
            / "v_1717000000"
        )
        _write_config(snapshot)

        assert _load_repo_config(str(snapshot)) is None

    def test_accepts_mutable_base_clone(self, tmp_path):
        """A mutable base clone (no .versioned) must load successfully."""
        golden = tmp_path / "golden-repos"
        base = golden / "flask"
        _write_config(base)

        result = _load_repo_config(str(base))
        assert result is not None
        config_data, config_path = result
        assert config_data["embedding_provider"] == "voyage-ai"
        assert config_path == (base / ".code-indexer" / "config.json").resolve()

    def test_accepts_base_clone_living_under_a_mount(self, tmp_path):
        """A base clone that happens to sit under a mount is NOT a snapshot."""
        mount = tmp_path / "mnt" / "cow-storage"
        base = mount / "flask"  # {mount}/flask — base clone, single component
        _write_config(base)

        result = _load_repo_config(str(base))
        assert result is not None


# ---------------------------------------------------------------------------
# _resolve_provider_job_repo_path — redirect / refuse, never write to snapshot
# ---------------------------------------------------------------------------


class TestResolveProviderJobRepoPath:
    def test_local_snapshot_redirects_to_base_clone(self, tmp_path):
        golden = tmp_path / "golden-repos"
        base = golden / "claude-server"
        base.mkdir(parents=True)
        snapshot = golden / ".versioned" / "claude-server" / "v_1772136021"
        snapshot.mkdir(parents=True)

        actual, alias, is_versioned = _resolve_provider_job_repo_path(
            str(snapshot), "claude-server-global"
        )
        assert is_versioned is True
        assert actual == str(base)
        assert actual != str(snapshot)

    def test_canonical_cow_snapshot_is_flagged_versioned(self, tmp_path):
        """Canonical cow snapshot must be detected as versioned (not a write target)."""
        mount = tmp_path / "mnt" / "cow-storage"
        snapshot = mount / ".versioned" / "langfuse_repo" / "v_1717000000"
        snapshot.mkdir(parents=True)

        actual, alias, is_versioned = _resolve_provider_job_repo_path(
            str(snapshot), "langfuse_repo-global"
        )
        # MUST be flagged versioned, and MUST NOT return the snapshot path as a
        # writable indexing target.
        assert is_versioned is True
        assert actual != str(snapshot)

    def test_non_versioned_passes_through(self, tmp_path):
        golden = tmp_path / "golden-repos"
        repo = golden / "click"
        repo.mkdir(parents=True)

        actual, alias, is_versioned = _resolve_provider_job_repo_path(
            str(repo), "click-global"
        )
        assert is_versioned is False
        assert actual == str(repo)


# ---------------------------------------------------------------------------
# _resolve_versioned_to_base_clone — redirect / refuse, never write to snapshot
# ---------------------------------------------------------------------------


class TestResolveVersionedToBaseClone:
    def test_local_snapshot_redirects_to_base_clone(self, tmp_path):
        golden = tmp_path / "golden-repos"
        base = golden / "claude-server"
        base.mkdir(parents=True)
        snapshot = golden / ".versioned" / "claude-server" / "v_1772136021"
        snapshot.mkdir(parents=True)

        actual, alias, is_versioned, err = _resolve_versioned_to_base_clone(
            str(snapshot), "claude-server-global"
        )
        assert err is None
        assert is_versioned is True
        assert actual == str(base)

    def test_canonical_cow_snapshot_flagged_and_not_writable(self, tmp_path):
        """Canonical cow snapshot: flagged versioned; snapshot path never writable.

        The base clone for a cow-daemon repo lives at golden-repos/{ns}, NOT under
        the mount; the local-shaped arithmetic cannot produce it, so the resolver
        MUST refuse (error) rather than hand back the snapshot path. Either way the
        immutable snapshot is never returned as a writable target (AC #8).
        """
        mount = tmp_path / "mnt" / "cow-storage"
        snapshot = mount / ".versioned" / "langfuse_repo" / "v_1717000000"
        snapshot.mkdir(parents=True)

        actual, alias, is_versioned, err = _resolve_versioned_to_base_clone(
            str(snapshot), "langfuse_repo-global"
        )
        assert is_versioned is True
        # Never returns the snapshot itself as a clean writable target:
        assert not (actual == str(snapshot) and err is None)

    def test_non_versioned_passes_through(self, tmp_path):
        golden = tmp_path / "golden-repos"
        repo = golden / "click"
        repo.mkdir(parents=True)

        actual, alias, is_versioned, err = _resolve_versioned_to_base_clone(
            str(repo), "click-global"
        )
        assert err is None
        assert is_versioned is False
        assert actual == str(repo.resolve())

    def test_none_path_is_guarded(self):
        actual, alias, is_versioned, err = _resolve_versioned_to_base_clone(
            None, "x-global"
        )
        assert is_versioned is False
        assert err is not None


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
