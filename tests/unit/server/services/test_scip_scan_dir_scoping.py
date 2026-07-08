"""Tests for the scip_context/scip_impact bottleneck fix: repository_alias now
scopes the SCIP scan to one repo's scip dir instead of globbing + opening every
repo's .scip.db across the whole golden-repos tree."""

from code_indexer.server.services.scip_query_service import SCIPQueryService


def _svc(golden):
    return SCIPQueryService(golden_repos_dir=str(golden), access_filtering_service=None)


def test_scopes_to_alias_when_scip_dir_exists(tmp_path):
    svc = _svc(tmp_path)
    scoped = tmp_path / "evolution" / ".code-indexer" / "scip"
    scoped.mkdir(parents=True)

    # -global suffix is stripped; resolves to the repo's scip dir.
    assert svc._resolve_scip_scan_dir("evolution-global") == scoped
    assert svc._resolve_scip_scan_dir("evolution") == scoped
    # Full-path form resolves via basename.
    assert svc._resolve_scip_scan_dir(str(tmp_path / "evolution")) == scoped


def test_no_alias_returns_whole_golden_dir(tmp_path):
    svc = _svc(tmp_path)
    assert svc._resolve_scip_scan_dir(None) == tmp_path
    assert svc._resolve_scip_scan_dir("") == tmp_path


def test_unknown_alias_falls_back_to_full_dir(tmp_path):
    # Never silently drop results: an unresolvable alias scans the full dir
    # (previous behaviour) rather than an empty scoped path.
    svc = _svc(tmp_path)
    assert svc._resolve_scip_scan_dir("does-not-exist-global") == tmp_path
