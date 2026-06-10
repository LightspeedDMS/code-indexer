"""SCIP-vs-semantic version consistency (Bug #1084 B2, AC #9).

On the cow-daemon backend the alias ``target_path`` points to a versioned
snapshot under the daemon mount (``{mount}/.versioned/{ns}/v_*``), while the
mutable base clone (``golden-repos/{ns}``) holds an OLDER index. Semantic search
resolves the alias target; SCIP discovery used to scan the ``golden_repos_path``
filesystem and descend into ``.versioned`` only -- so on cow-daemon it read the
mutable base clone, producing cross-index version skew.

Phase B routes SCIP discovery through the alias ``target_path`` (the same
authority semantic uses), so SCIP and semantic resolve the SAME version on every
backend. Repos with no alias JSON keep the filesystem-scan behavior (local
unchanged).
"""

from __future__ import annotations

import json
from pathlib import Path

from code_indexer.server.services.scip_query_service import SCIPQueryService


def _make_alias(aliases_dir: Path, alias_name: str, target: Path) -> None:
    aliases_dir.mkdir(parents=True, exist_ok=True)
    (aliases_dir / f"{alias_name}.json").write_text(
        json.dumps(
            {
                "target_path": str(target),
                "created_at": "2026-01-01T00:00:00+00:00",
                "last_refresh": "2026-01-01T00:00:00+00:00",
                "repo_name": alias_name.removesuffix("-global"),
            }
        )
    )


def _scip_index(repo_root: Path) -> Path:
    scip = repo_root / ".code-indexer" / "scip"
    scip.mkdir(parents=True, exist_ok=True)
    db = scip / "index.scip.db"
    db.touch()
    return db


class TestScipResolvesAliasTargetVersion:
    def test_cow_shaped_alias_target_used_over_mutable_base_clone(self, tmp_path):
        """Alias -> snapshot under a mount: SCIP must read the snapshot, not base."""
        golden = tmp_path / "golden-repos"
        golden.mkdir()

        # Mutable base clone with a STALE scip index.
        base = golden / "flask"
        _scip_index(base)

        # Versioned snapshot under the cow-daemon mount with the CURRENT index.
        mount = tmp_path / "mnt" / "cow-storage"
        snapshot = mount / ".versioned" / "flask" / "v_1717000000"
        snapshot_db = _scip_index(snapshot)

        # Alias points at the snapshot (what semantic resolves).
        _make_alias(golden / "aliases", "flask-global", snapshot)

        service = SCIPQueryService(
            golden_repos_dir=str(golden), access_filtering_service=None
        )

        files = service.find_scip_files(repository_alias="flask-global")

        assert snapshot_db in files, (
            "SCIP must discover the alias-resolved snapshot index "
            f"(same version as semantic). Got: {files}"
        )
        # And MUST NOT read the stale base-clone index for this alias.
        stale = base / ".code-indexer" / "scip" / "index.scip.db"
        assert stale not in files, (
            "SCIP must NOT read the mutable base-clone index on cow-daemon "
            "(version skew vs semantic)."
        )

    def test_bare_alias_only_json_resolves(self, tmp_path):
        """When ONLY a bare '{name}.json' alias exists (no -global), it resolves.

        Covers the bare-alias fallback in _resolve_alias_scip_root.
        """
        golden = tmp_path / "golden-repos"
        golden.mkdir()
        mount = tmp_path / "mnt" / "cow-storage"
        snapshot = mount / ".versioned" / "tooling" / "v_1717000000"
        snapshot_db = _scip_index(snapshot)
        # Only the bare alias exists -- no 'tooling-global.json'.
        _make_alias(golden / "aliases", "tooling", snapshot)

        service = SCIPQueryService(
            golden_repos_dir=str(golden), access_filtering_service=None
        )

        files = service.find_scip_files(repository_alias="tooling")
        assert snapshot_db in files

    def test_bare_alias_promoted_to_global(self, tmp_path):
        """A bare alias filter resolves through the -global alias target."""
        golden = tmp_path / "golden-repos"
        golden.mkdir()
        base = golden / "flask"
        _scip_index(base)

        mount = tmp_path / "mnt" / "cow-storage"
        snapshot = mount / ".versioned" / "flask" / "v_1717000000"
        snapshot_db = _scip_index(snapshot)
        _make_alias(golden / "aliases", "flask-global", snapshot)

        service = SCIPQueryService(
            golden_repos_dir=str(golden), access_filtering_service=None
        )

        files = service.find_scip_files(repository_alias="flask")
        assert snapshot_db in files


class TestLocalUnchangedRegression:
    def test_no_alias_json_falls_back_to_filesystem_scan(self, tmp_path):
        """Repos with no alias JSON: discover via the filesystem scan (local)."""
        golden = tmp_path / "golden-repos"
        golden.mkdir()

        db_a = _scip_index(golden / "repo-a")
        db_b = _scip_index(golden / "repo-b")

        service = SCIPQueryService(
            golden_repos_dir=str(golden), access_filtering_service=None
        )

        files = service.find_scip_files(username=None)
        assert db_a in files
        assert db_b in files
        assert len(files) == 2

    def test_local_versioned_alias_target_still_works(self, tmp_path):
        """Local: alias -> .versioned snapshot in golden-repos; SCIP reads it."""
        golden = tmp_path / "golden-repos"
        golden.mkdir()

        # Local canonical snapshot lives under golden-repos/.versioned
        snapshot = golden / ".versioned" / "flask" / "v_1700000000"
        snapshot_db = _scip_index(snapshot)
        _make_alias(golden / "aliases", "flask-global", snapshot)

        service = SCIPQueryService(
            golden_repos_dir=str(golden), access_filtering_service=None
        )

        files = service.find_scip_files(repository_alias="flask-global")
        assert snapshot_db in files
